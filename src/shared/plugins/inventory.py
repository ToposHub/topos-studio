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

from dataclasses import dataclass, replace
from typing import Any, Iterable

from src.shared.plugins.manifest import (
    ManifestDiagnostic,
    PluginManifest,
    legacy_python_manifest,
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

    Refresh replaces records atomically and bumps the revision, so a UI
    re-projecting from an older snapshot can detect staleness instead of
    mixing halves of two refreshes. Selection/filter state lives in the UI and
    keys on ``plugin_id``, which never changes across versions.
    """

    def __init__(self, *, host_api: int, topos_version: str, platform: str, arch: str,
                 max_events: int = 200) -> None:
        if max_events < 1:
            raise ValueError("max_events 必须是正整数")
        self._host_facts = (host_api, topos_version, platform, arch)
        self._records: dict[str, PluginRecord] = {}
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    def snapshot(self) -> InventorySnapshot:
        return InventorySnapshot(revision=self._revision,
                                 records=tuple(self._records.values()))

    def refresh(self, records: Iterable[PluginRecord]) -> InventorySnapshot:
        """Replace the whole record set atomically (idempotent per content)."""
        merged: dict[str, PluginRecord] = {}
        diagnostics_by_id: dict[str, list[ManifestDiagnostic]] = {}
        for record in records:
            if record.manifest is not None and record.manifest.plugin_id != record.plugin_id:
                # Identity integrity: a record may never speak for another id.
                record = replace(record, diagnostics=tuple(record.diagnostics) + (
                    ManifestDiagnostic(code="RECORD_ID_MISMATCH", severity="error",
                                       message=f"记录 {record.plugin_id} 携带 "
                                               f"{record.manifest.plugin_id} 的 manifest"),
                ))
            merged[record.plugin_id] = record
            diagnostics_by_id.setdefault(record.plugin_id, []).extend(record.diagnostics)
        manifests = tuple(record.manifest for record in merged.values()
                          if record.manifest is not None)
        host_api, topos_version, platform, arch = self._host_facts
        compatibility = validate_manifest_set(manifests, host_api=host_api,
                                              topos_version=topos_version,
                                              platform=platform, arch=arch)
        incompatible: set[str] = set()
        finalized: dict[str, PluginRecord] = {}
        for plugin_id, record in merged.items():
            findings = tuple(diagnostics_by_id.get(plugin_id, ())) + compatibility.get(plugin_id, ())
            blocking = any(item.severity == "error" for item in findings)
            if blocking and record.runtime_state in ("loaded", "active"):
                # Host facts reject what the legacy runtime already loaded this
                # session: keep the record visible and flagged, never hidden.
                incompatible.add(plugin_id)
            updated = replace(
                record,
                diagnostics=tuple(findings[:_MAX_RECORD_DIAGNOSTICS]),
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
        record = self._records.get(plugin_id)
        if record is None:
            raise KeyError(f"未知插件：{plugin_id}")
        updated = replace(record, desired_enabled=bool(enabled))
        updated = replace(updated, pending_restart=self._needs_restart(updated))
        self._records[plugin_id] = updated
        self._revision += 1
        return updated

    def mark_runtime(self, plugin_id: str, runtime_state: str, *,
                     failure_phase: str = "", error_code: str = "",
                     error_message: str = "") -> PluginRecord:
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
                          error_message=error_message if runtime_state in ("failed", "rejected") else "")
        updated = replace(updated, pending_restart=self._needs_restart(updated))
        self._records[plugin_id] = updated
        self._revision += 1
        return updated

    @staticmethod
    def _needs_restart(record: PluginRecord) -> bool:
        effective = record.runtime_state in ("loaded", "active")
        return effective != record.desired_enabled


def legacy_files_records(catalog_rows: Iterable[Any]) -> list[PluginRecord]:
    """Adapt legacy ``FileExplorerPluginCatalog`` rows into unified records.

    The legacy host only persists "disabled next start"; that maps to
    ``desired_enabled`` while ``runtime_state`` reports what the old loader
    actually did this session. Third-party folders unknown to the builtin
    table arrive as ``discovered`` and default disabled.
    """
    records: list[PluginRecord] = []
    for row in catalog_rows:
        legacy_id = getattr(row, "plugin_id", "")
        diagnostics: tuple[ManifestDiagnostic, ...] = tuple(getattr(row, "diagnostics", ()) or ())
        manifest_path = getattr(row, "path", None)
        manifest = None
        if manifest_path is not None:
            try:
                manifest, _ = legacy_python_manifest(manifest_path)
            except (OSError, ValueError):
                manifest = None
        if manifest is not None:
            plugin_id = manifest.plugin_id
            display_name, version = manifest.display_name, manifest.version
            source = manifest.source
        else:
            plugin_id = f"files.{legacy_id}"
            display_name = str(getattr(row, "name", legacy_id))
            version = str(getattr(row, "version", "0.0.1"))
            source = "builtin"
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
