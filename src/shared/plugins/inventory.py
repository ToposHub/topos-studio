"""Unified plugin inventory and state projection (PL-02).

Separates install facts from user intent and runtime reality so a single
boolean can never express them all:

    install_state:  installed / discovered / missing (folder removed)
    desired:        user wants it enabled next start (persisted preference)
    runtime:        not_loaded / loaded / active / failed / rejected
    failure_phase:  validate / load / activate / runtime ("" when healthy)
    pending_restart: desired != runtime reality and host cannot hot-swap

Third-party sources default to disabled; local development sources stay
marked unverified. Every record carries a monotonic ``revision`` plus bounded
diagnostics, and identity survives version changes (pins/shortcuts/recents key
on ``plugin_id`` which never contains a version).
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable

from src.shared.plugins.manifest import (
    ManifestDiagnostic,
    PluginManifest,
    legacy_python_manifest,
    parse_manifest_file,
    validate_manifest_set,
)

INSTALL_STATES = ("installed", "discovered", "missing")
RUNTIME_STATES = ("not_loaded", "loaded", "active", "failed", "rejected")
FAILURE_PHASES = ("", "validate", "load", "activate", "runtime")
_MAX_RECORD_DIAGNOSTICS = 50


@dataclass(frozen=True)
class PluginRecord:
    """One plugin's full state; unknown/pending actions stay visible."""

    plugin_id: str
    display_name: str
    version: str
    source: str
    install_state: str
    desired_enabled: bool
    runtime_state: str
    pending_restart: bool = False
    failure_phase: str = ""
    error_code: str = ""
    error_message: str = ""
    # 已安装/声明版本与运行时实际加载版本分开投影（PR-03）：空 = 宿主未上报
    runtime_version: str = ""
    manifest: PluginManifest | None = None
    diagnostics: tuple[ManifestDiagnostic, ...] = ()

    @property
    def installable(self) -> bool:
        return self.install_state != "missing"

    @property
    def unverified(self) -> bool:
        return self.source == "local_dev"

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id, "display_name": self.display_name,
            "version": self.version, "source": self.source,
            "install_state": self.install_state,
            "desired_enabled": self.desired_enabled,
            "runtime_state": self.runtime_state,
            "pending_restart": self.pending_restart,
            "failure_phase": self.failure_phase,
            "error_code": self.error_code, "error_message": self.error_message[:500],
            "runtime_version": self.runtime_version,
            "diagnostics": [item.to_dict() for item in self.diagnostics[:_MAX_RECORD_DIAGNOSTICS]],
        }


@dataclass(frozen=True)
class InventorySnapshot:
    revision: int
    records: tuple[PluginRecord, ...]

    def record(self, plugin_id: str) -> PluginRecord | None:
        return next((item for item in self.records if item.plugin_id == plugin_id), None)

    def by_legacy_id(self, legacy_id: str) -> PluginRecord | None:
        return next((item for item in self.records
                     if item.manifest is not None and legacy_id in item.manifest.legacy_ids), None)


class PluginInventory:
    """Mutable store producing immutable snapshots; safe to refresh concurrently.

    Refresh replaces records atomically and bumps the revision under one lock,
    so a snapshot's revision and records can never mix two refresh halves
    (PR-03). Selection/filter state lives in the UI and keys on
    ``plugin_id``, which never changes across versions.
    """

    def __init__(self, *, host_api: int, topos_version: str, platform: str, arch: str,
                 max_events: int = 200) -> None:
        if max_events < 1:
            raise ValueError("max_events 必须是正整数")
        self._host_facts = (host_api, topos_version, platform, arch)
        self._lock = threading.RLock()
        self._records: dict[str, PluginRecord] = {}
        self._revision = 0
        # Discovery snapshots may have been prepared before a user/runtime update.
        # These fields belong to this authority, never to a stale filesystem scan.
        self._desired_overrides: dict[str, bool] = {}
        self._runtime_overrides: dict[str, dict[str, str]] = {}

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def snapshot(self) -> InventorySnapshot:
        with self._lock:
            return InventorySnapshot(revision=self._revision,
                                     records=tuple(self._records.values()))

    def refresh(self, records: Iterable[PluginRecord]) -> InventorySnapshot:
        """Replace the whole record set atomically (idempotent per content).

        合并前先检测重复身份（PR-03）：同一 ``plugin_id`` 或同一 legacy id
        出现多个来源时显式打诊断并保留首个，拒绝"最后胜出"式静默覆盖。
        """
        with self._lock:
            merged: dict[str, PluginRecord] = {}
            diagnostics_by_id: dict[str, list[ManifestDiagnostic]] = {}
            legacy_owner: dict[str, str] = {}
            for record in records:
                if record.plugin_id in self._desired_overrides:
                    record = replace(record, desired_enabled=self._desired_overrides[record.plugin_id])
                if record.plugin_id in self._runtime_overrides:
                    record = replace(record, **self._runtime_overrides[record.plugin_id])
                diagnostics: list[ManifestDiagnostic] = list(record.diagnostics)
                if record.manifest is not None and record.manifest.plugin_id != record.plugin_id:
                    # Identity integrity: a record may never speak for another id.
                    diagnostics.append(ManifestDiagnostic(
                        code="RECORD_ID_MISMATCH", severity="error",
                        message=f"记录 {record.plugin_id} 携带 "
                                f"{record.manifest.plugin_id} 的 manifest"))
                if record.plugin_id in merged:
                    # 重复身份：保留首个，其余拒绝合并，两边都看得见冲突
                    kept = diagnostics_by_id.setdefault(record.plugin_id, list(merged[record.plugin_id].diagnostics))
                    duplicate_note = ManifestDiagnostic(
                        code="PLUGIN_ID_DUPLICATE", severity="error",
                        message=f"重复的 plugin_id：{record.plugin_id} 由多个来源声明，"
                                "已保留首个并拒绝合并其余")
                    kept.append(duplicate_note)
                    continue
                for legacy_id in (record.manifest.legacy_ids if record.manifest is not None else ()):
                    owner = legacy_owner.get(legacy_id)
                    if owner is not None and owner != record.plugin_id:
                        diagnostics.append(ManifestDiagnostic(
                            code="LEGACY_ID_CONFLICT", severity="error",
                            message=f"legacy id {legacy_id} 同时映射到 {owner} 与 "
                                    f"{record.plugin_id}"))
                    elif owner is None:
                        legacy_owner[legacy_id] = record.plugin_id
                merged[record.plugin_id] = record
                diagnostics_by_id[record.plugin_id] = diagnostics
            manifests = tuple(record.manifest for record in merged.values()
                              if record.manifest is not None)
            host_api, topos_version, platform, arch = self._host_facts
            compatibility = validate_manifest_set(manifests, host_api=host_api,
                                                  topos_version=topos_version,
                                                  platform=platform, arch=arch)
            finalized: dict[str, PluginRecord] = {}
            for plugin_id, record in merged.items():
                findings = tuple(diagnostics_by_id.get(plugin_id, ())) + compatibility.get(plugin_id, ())
                blocking = any(item.severity == "error" for item in findings)
                updated = replace(
                    record,
                    diagnostics=tuple(sorted(findings, key=lambda item: item.severity != "error")
                                      [:_MAX_RECORD_DIAGNOSTICS]),
                    runtime_state="rejected" if blocking and record.runtime_state == "not_loaded"
                    else record.runtime_state,
                    failure_phase="validate" if blocking and record.runtime_state == "not_loaded"
                    else record.failure_phase,
                )
                finalized[plugin_id] = replace(updated, pending_restart=self._needs_restart(updated))
            self._records = finalized
            self._revision += 1
            return self.snapshot()

    def set_desired(self, plugin_id: str, enabled: bool) -> PluginRecord:
        """Persist user intent for next start; runtime state is untouched."""
        with self._lock:
            record = self._records.get(plugin_id)
            if record is None:
                raise KeyError(f"未知插件：{plugin_id}")
            updated = replace(record, desired_enabled=bool(enabled))
            updated = replace(updated, pending_restart=self._needs_restart(updated))
            self._records[plugin_id] = updated
            self._desired_overrides[plugin_id] = updated.desired_enabled
            self._revision += 1
            return updated

    def mark_runtime(self, plugin_id: str, runtime_state: str, *,
                     failure_phase: str = "", error_code: str = "",
                     error_message: str = "", runtime_version: str | None = None) -> PluginRecord:
        with self._lock:
            record = self._records.get(plugin_id)
            if record is None:
                raise KeyError(f"未知插件：{plugin_id}")
            if runtime_state not in RUNTIME_STATES:
                raise ValueError(f"未知 runtime 状态：{runtime_state}")
            if failure_phase and failure_phase not in FAILURE_PHASES:
                raise ValueError(f"未知失败阶段：{failure_phase}")
            updated = replace(record, runtime_state=runtime_state,
                              failure_phase=failure_phase if runtime_state in ("failed", "rejected") else "",
                              error_code=error_code if runtime_state in ("failed", "rejected") else "",
                              error_message=error_message if runtime_state in ("failed", "rejected") else "",
                              runtime_version=(runtime_version if runtime_version is not None
                                               else record.runtime_version)
                              if runtime_state != "not_loaded" else "")
            updated = replace(updated, pending_restart=self._needs_restart(updated))
            self._records[plugin_id] = updated
            self._runtime_overrides[plugin_id] = {
                field: getattr(updated, field) for field in
                ("runtime_state", "failure_phase", "error_code", "error_message", "runtime_version")}
            self._revision += 1
            return updated

    @staticmethod
    def _needs_restart(record: PluginRecord) -> bool:
        effective = record.runtime_state in ("loaded", "active")
        return effective != record.desired_enabled


def legacy_files_records(catalog_rows: Iterable[Any],
                         *, builtin_ids: Iterable[str] = ()) -> list[PluginRecord]:
    """Adapt legacy ``FileExplorerPluginCatalog`` rows into unified records.

    The legacy host only persists "disabled next start"; that maps to
    ``desired_enabled`` while ``runtime_state`` reports what the old loader
    actually did this session. Declarative ``topos.plugin.json`` in the plugin
    folder wins over AST-extracted ``__init__.py`` constants (PR-03); its
    identity must match the folder. ``source`` 是宿主政策（``builtin_ids``），
    包内声明不参与——未知目录默认 local_dev、默认停用。
    """
    builtin = frozenset(builtin_ids)
    records: list[PluginRecord] = []
    for row in catalog_rows:
        legacy_id = getattr(row, "plugin_id", "")
        raw_diagnostics = getattr(row, "diagnostics", ()) or ()
        if not isinstance(raw_diagnostics, (tuple, list)):
            raw_diagnostics = (raw_diagnostics,)
        catalog_diagnostics: list[ManifestDiagnostic] = []
        for diagnostic in raw_diagnostics[:_MAX_RECORD_DIAGNOSTICS]:
            if isinstance(diagnostic, ManifestDiagnostic):
                catalog_diagnostics.append(diagnostic)
            elif isinstance(diagnostic, str):
                catalog_diagnostics.append(ManifestDiagnostic(
                    code="LEGACY_CATALOG_DIAGNOSTIC", severity="warning",
                    message=diagnostic,
                ))
            else:
                catalog_diagnostics.append(ManifestDiagnostic(
                    code="LEGACY_DIAGNOSTIC_INVALID", severity="warning",
                    message=f"旧插件诊断类型不受支持：{type(diagnostic).__name__}",
                ))
        diagnostics: list[ManifestDiagnostic] = []
        manifest_path = getattr(row, "path", None)
        manifest = None
        if manifest_path is not None:
            declared_path = Path(manifest_path) / "topos.plugin.json"
            if declared_path.is_file():
                # 声明式 manifest 优先；身份必须与目录一致，否则忽略声明
                declared, findings = parse_manifest_file(declared_path)
                diagnostics.extend(findings)
                if declared is not None and declared.plugin_id != f"files.{legacy_id}":
                    diagnostics.append(ManifestDiagnostic(
                        code="DECLARED_ID_MISMATCH", severity="error",
                        message=f"声明身份 {declared.plugin_id} 与目录 files.{legacy_id} 不符，已忽略该声明"))
                    declared = None
                manifest = declared
            else:
                try:
                    manifest, findings = legacy_python_manifest(manifest_path)
                    diagnostics.extend(findings)
                except (OSError, ValueError) as exc:
                    manifest = None
                    diagnostics.append(ManifestDiagnostic(
                        code="LEGACY_MANIFEST_UNREADABLE", severity="error",
                        message=f"无法读取旧插件声明：{exc}",
                    ))
        diagnostics.extend(catalog_diagnostics)
        # 来源由宿主管理：内建名单之外一律按未验证处理，第三方默认停用
        source = "builtin" if legacy_id in builtin else "local_dev"
        if manifest is not None:
            plugin_id = manifest.plugin_id
            display_name, version = manifest.display_name, manifest.version
        else:
            plugin_id = f"files.{legacy_id}"
            display_name = str(getattr(row, "name", legacy_id))
            version = str(getattr(row, "version", "0.0.1"))
        if getattr(row, "self_disabled", False):
            runtime_state = "rejected"
            failure_phase, error_code = "validate", "SELF_DISABLED"
        elif getattr(row, "error", ""):
            runtime_state = "failed"
            failure_phase, error_code = "load", "LEGACY_LOAD_FAILED"
        elif getattr(row, "runtime_loaded", False):
            runtime_state, failure_phase, error_code = "loaded", "", ""
        else:
            runtime_state, failure_phase, error_code = "not_loaded", "", ""
        desired = bool(getattr(row, "desired_enabled", False))
        pending = getattr(row, "needs_restart", False)
        records.append(PluginRecord(
            plugin_id=plugin_id, display_name=display_name, version=version,
            source=source, install_state="installed" if legacy_id else "discovered",
            desired_enabled=desired, runtime_state=runtime_state,
            pending_restart=pending, failure_phase=failure_phase,
            error_code=error_code, error_message=str(getattr(row, "error", "")),
            runtime_version=str(getattr(row, "runtime_version", "")),
            manifest=manifest,
            diagnostics=tuple(diagnostics[:_MAX_RECORD_DIAGNOSTICS]),
        ))
    return records


def default_desired_for(source: str, existing_preference: bool | None) -> bool:
    """Third-party/discovered sources default disabled; builtin keeps intent."""
    if existing_preference is not None:
        return existing_preference
    return source == "builtin"


__all__ = [
    "INSTALL_STATES", "RUNTIME_STATES", "FAILURE_PHASES", "PluginRecord",
    "InventorySnapshot", "PluginInventory", "legacy_files_records",
    "default_desired_for",
]
