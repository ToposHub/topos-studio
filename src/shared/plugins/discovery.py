"""F1（PL-02 收口）：单一发现聚合——builtin / 已安装 / 显式开发源。

三个来源合成一份 ``PluginRecord`` 交给 ``PluginInventory.refresh``：

* **builtin**：宿主内建目录（legacy catalog 行经 ``legacy_files_records``，
  来源政策 = 宿主 builtin_ids 名单）；
* **installed**：受管安装根（installer 的 ``<id>/versions/<v> + current``
  布局 + 安装器自记来源 sidecar——包声明的 source 不参与信任）；
* **local_dev**：显式开发源注册表（``DevSourceRegistry`` 持久化宿主配
  置；目录内 ``topos.plugin.json`` 身份权威）。

重复身份（同一 plugin_id 出现于多个来源）由 ``PluginInventory.refresh``
打 ``PLUGIN_ID_DUPLICATE`` 诊断并保留首个——两边都看得见冲突，不做
"最后胜出"式静默覆盖。安装根与发现根闭环：install/uninstall 之后的
``scan_sources`` 即得新事实；读侧与 installer 共用同一 current 指针语
义（指针损坏 → ``install_state="missing"`` + 诊断，不假装在账）。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from src.shared.plugins.inventory import (
    PluginRecord,
    default_desired_for,
    legacy_files_records,
)
from src.shared.plugins.installer import InstallerError, LocalPackageInstaller
from src.shared.plugins.manifest import (
    ManifestDiagnostic,
    parse_manifest_file,
)

_SOURCE_LABELS = {"builtin": "内建", "installed": "已安装", "local_dev": "开发源"}

# [F1] 宿主事实：插件清单兼容校验（host_api/最低版本/平台/架构）的权威输入
HOST_API = 1


def current_host_facts() -> dict[str, Any]:
    """当前进程的宿主事实（平台/架构实测，版本优先读包元数据）。"""
    import platform as _platform

    try:
        from importlib.metadata import version as _pkg_version
        topos_version = str(_pkg_version("topos"))
    except Exception:
        topos_version = "3.0.0"   # 与 pyproject.toml 同步的兜底
    system = _platform.system().lower()
    machine = (_platform.machine() or "").lower()
    return {"host_api": HOST_API, "topos_version": topos_version,
            "platform": system, "arch": machine or "unknown"}


def source_label(source: str) -> str:
    """UI 直读的来源短标签（未知来源原样展示，不冒充内建）。"""
    return _SOURCE_LABELS.get(source, source or "未知")


# ---------------------------------------------------------------------------
# 显式开发源注册表（宿主配置持久化）
# ---------------------------------------------------------------------------

class DevSourceRegistry:
    """显式开发源目录清单：宿主配置里持久化，增删原子写。"""

    def __init__(self, config_file: Path) -> None:
        self._path = Path(config_file)
        self._lock = threading.Lock()

    def list(self) -> list[Path]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        roots = []
        for item in raw.get("dev_roots", []) if isinstance(raw, dict) else []:
            try:
                roots.append(Path(str(item)).expanduser())
            except (TypeError, ValueError):
                continue
        return roots

    def add(self, root: Path) -> bool:
        """登记开发源（解析为绝对路径、去重）；返回是否新登记。"""
        resolved = Path(root).expanduser().resolve()
        with self._lock:
            roots = self.list()
            if resolved in roots:
                return False
            self._write([*roots, resolved])
        return True

    def remove(self, root: Path) -> bool:
        resolved = Path(root).expanduser().resolve()
        with self._lock:
            roots = self.list()
            if resolved not in roots:
                return False
            self._write([item for item in roots if item != resolved])
        return True

    def _write(self, roots: list[Path]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(
            {"dev_roots": [str(item) for item in roots]}, ensure_ascii=False,
            indent=2), encoding="utf-8")
        os.replace(tmp, self._path)


class DesiredStateStore:
    """[F2] 新模型插件的启停偏好持久化（plugin_id → desired_enabled）。

    legacy 目录插件的启停仍走旧 catalog 配置；受管安装/开发源插件的
    desired 存这里——按新 id 模型建档，不做旧 id 别名或旧偏好迁移。
    """

    def __init__(self, config_file: Path) -> None:
        self._path = Path(config_file)
        self._lock = threading.Lock()

    def load(self) -> dict[str, bool]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(key): bool(value) for key, value in raw.items()
                if isinstance(value, bool)}

    def save(self, desired: dict[str, bool]) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(desired, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self._path)


# ---------------------------------------------------------------------------
# 已安装（受管安装根）与开发源扫描
# ---------------------------------------------------------------------------

def installed_records(install_root: Path, *,
                      host_facts: dict[str, Any],
                      desired_by_id: dict[str, bool] | None = None) -> list[PluginRecord]:
    """枚举受管安装根 → ``source="installed"`` 记录（闭环的读侧）。

    与 installer 共用 current 指针语义：``<id>/current`` 指向有效版本才
    在账；指针缺失/损坏 → ``install_state="missing"`` + 诊断（可见，不
    假装安装着）。来源读安装器自记 provenance（包声明不参与）。
    """
    root = Path(install_root)
    if not root.is_dir():
        return []
    installer = LocalPackageInstaller(root, host_facts=host_facts)
    records: list[PluginRecord] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.is_symlink() or entry.name.startswith("."):
            continue
        plugin_id = entry.name
        diagnostics: list[ManifestDiagnostic] = []
        try:
            manifest = installer.installed_manifest(plugin_id)
            provenance = installer.installed_provenance(plugin_id) or {}
        except (InstallerError, OSError):
            manifest = None
            provenance = {}
            diagnostics.append(ManifestDiagnostic(
                code="INSTALL_POINTER_BROKEN", severity="error",
                message=f"{plugin_id} 的 current 指针缺失或损坏"))
        if manifest is None:
            # current 指针缺失/损坏或 manifest 不可读：missing + 诊断
            # （_read_pointer 对缺失指针静默返回空，这里统一补齐可见诊断）
            if not any(item.code == "INSTALL_POINTER_BROKEN" for item in diagnostics):
                diagnostics.append(ManifestDiagnostic(
                    code="INSTALL_POINTER_BROKEN", severity="error",
                    message=f"{plugin_id} 的 current 指针缺失或损坏"))
            records.append(PluginRecord(
                plugin_id=plugin_id, display_name=plugin_id,
                version=provenance.get("version", ""),
                source="installed", install_state="missing",
                desired_enabled=False, runtime_state="not_loaded",
                failure_phase="validate", origin_path=str(entry),
                error_code="INSTALL_POINTER_BROKEN",
                error_message="安装指针损坏：请重装或卸载该插件",
                diagnostics=tuple(diagnostics)))
            continue
        desired = (desired_by_id or {}).get(
            plugin_id, default_desired_for("installed", None))
        source_note = str(provenance.get("source", "") or "")
        if source_note:
            diagnostics.append(ManifestDiagnostic(
                code="INSTALL_PROVENANCE", severity="info",
                message=f"安装来源：{source_note}（安装器自记）"))
        records.append(PluginRecord(
            plugin_id=manifest.plugin_id,
            display_name=manifest.display_name,
            version=manifest.version,
            source="installed", install_state="installed",
            desired_enabled=desired, runtime_state="not_loaded",
            origin_path=str(entry / "current"), manifest=manifest,
            diagnostics=tuple(diagnostics)))
    return records


def dev_records(dev_roots: Iterable[Path]) -> list[PluginRecord]:
    """显式开发源目录 → ``source="local_dev"``（未验证、默认停用）。

    目录内 ``topos.plugin.json`` 身份权威（不再要求 files.<folder> 旧
    约束——开发源是新模型插件的入口）；manifest 缺失/畸形 → 诊断记录
    保留可见，不静默跳过。
    """
    records: list[PluginRecord] = []
    seen_dirs: set[Path] = set()
    for root in dev_roots:
        base = Path(root).expanduser()
        if not base.is_dir():
            records.append(PluginRecord(
                plugin_id=f"dev.{abs(hash(str(base))) & 0xFFFFFF:x}",
                display_name=str(base), version="", source="local_dev",
                install_state="missing", desired_enabled=False,
                runtime_state="not_loaded", failure_phase="validate",
                origin_path=str(base),
                error_code="DEV_ROOT_MISSING",
                error_message=f"开发源目录不存在：{base}"))
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or entry.is_symlink() or entry.name.startswith("."):
                continue
            if entry in seen_dirs:   # 注册表重复登记同一目录：只扫一次
                continue
            seen_dirs.add(entry)
            declared, findings = parse_manifest_file(entry / "topos.plugin.json")
            diagnostics = list(findings)
            if declared is None:
                records.append(PluginRecord(
                    plugin_id=f"dev.{entry.name}", display_name=entry.name,
                    version="", source="local_dev", install_state="discovered",
                    desired_enabled=False, runtime_state="not_loaded",
                    failure_phase="validate", origin_path=str(entry),
                    error_code="DEV_MANIFEST_MISSING",
                    error_message="开发源缺少可解析的 topos.plugin.json",
                    diagnostics=tuple(diagnostics)))
                continue
            records.append(PluginRecord(
                plugin_id=declared.plugin_id,
                display_name=declared.display_name,
                version=declared.version,
                source="local_dev", install_state="discovered",
                desired_enabled=default_desired_for("local_dev", None),
                runtime_state="not_loaded", origin_path=str(entry),
                manifest=declared,
                diagnostics=tuple(diagnostics)))
    return records


@dataclass(frozen=True)
class PluginSourceConfig:
    """一次发现聚合的全部输入（宿主接线决定具体值）。"""

    builtin_ids: frozenset[str]
    install_root: Path
    dev_roots: tuple[Path, ...]

    @classmethod
    def for_host(cls, *, builtin_ids: Iterable[str], install_root: Path,
                 dev_roots: Iterable[Path] = ()) -> "PluginSourceConfig":
        return cls(builtin_ids=frozenset(builtin_ids),
                   install_root=Path(install_root),
                   dev_roots=tuple(Path(item) for item in dev_roots))


def scan_sources(catalog_rows: Iterable[Any], config: PluginSourceConfig, *,
                 host_facts: dict[str, Any],
                 desired_by_id: dict[str, bool] | None = None) -> list[PluginRecord]:
    """单一聚合入口：legacy 目录（builtin+未识别）+ 安装根 + 开发源。

    顺序即冲突时的保留优先级（inventory 保留首个）：builtin 先于已安装
    先于开发源——宿主政策明确，不做静默的最后胜出。
    """
    records = legacy_files_records(catalog_rows, builtin_ids=config.builtin_ids)
    records.extend(installed_records(config.install_root, host_facts=host_facts,
                                     desired_by_id=desired_by_id))
    records.extend(dev_records(config.dev_roots))
    return records


__all__ = [
    "DevSourceRegistry", "DesiredStateStore", "PluginSourceConfig", "dev_records",
    "installed_records", "scan_sources", "source_label",
    "current_host_facts", "HOST_API",
]
