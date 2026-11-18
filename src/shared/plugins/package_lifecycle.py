"""F11：安装/更新/卸载接生命周期与 Inventory；权限重确认；三类资产分开。

复用既有契约：``LocalPackageInstaller``（PL-08 事务/回滚/指针）、
``PluginHostSession``（F5/F6 停用链路与贡献/授权撤除）、``DesiredStateStore``
（F2 启停偏好）。本模块补**编排**：

* **停用→安装→重扫**：更新/卸载前先走停用链路（停新→按 owner 取消→
  确认终止→清理贡献）；未完全停用即中止并如实报告（可重试），绝不带
  着活插件换版本；
* **新增权限重确认**：新版本 manifest 的 ``permissions`` 声明相对已安装
  版本出现**新增项**时，必须经宿主确认回调放行——未确认即中止
  （``PERMISSION_CONFIRMATION_REQUIRED``）；纯移除不触发；
* **卸载三类资产分开**：代码（安装根 versions/current 树）经 installer
  事务删除；**用户数据**（配置命名空间）仅在 ``remove_data`` 时清；
  **凭据**（宿主侧 secret 授权）总是撤除（不属于插件目录，也不随数据
  删除选项）；**素材不触及**（编排层不触碰任何媒体路径——由构造保证
  并被测试锁定）；desired 偏好随代码一并清除。
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.shared.plugins.installer import (
    InstallOutcome,
    InstallerError,
    LocalPackageInstaller,
)
from src.shared.plugins.manifest import PluginManifest, parse_manifest_file, parse_manifest_mapping


class LifecycleInstallError(RuntimeError):
    """编排层拒绝（权限未确认/插件未停稳）——可诊断、可重试。"""

    def __init__(self, code: str, message: str, *,
                 added_permissions: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.added_permissions = added_permissions


@dataclass(frozen=True)
class PermissionDelta:
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackageReview:
    plugin_id: str
    version: str
    permissions: tuple[str, ...]
    added_permissions: tuple[str, ...]


def _package_manifest_raw(package: Path) -> dict[str, Any] | None:
    """读包内 ``topos.plugin.json`` 原始 dict（zip 或目录）。"""
    package = Path(package)
    candidate = package / "topos.plugin.json"
    try:
        if package.is_symlink() or candidate.is_symlink():
            return None
        if candidate.is_file():
            if candidate.stat().st_size > 1024 * 1024:
                return None
            with candidate.open("rb") as handle:
                raw = json.loads(handle.read(1024 * 1024 + 1))
            return raw if isinstance(raw, dict) else None
        if package.is_file() and package.suffix.lower() == ".zip":
            with zipfile.ZipFile(package) as archive:
                if archive.getinfo("topos.plugin.json").file_size > 1024 * 1024:
                    return None
                with archive.open("topos.plugin.json") as handle:
                    raw = json.loads(handle.read(1024 * 1024 + 1))
                return raw if isinstance(raw, dict) else None
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return None
    return None


def _permissions_of(raw: dict[str, Any] | None) -> frozenset[str]:
    if not isinstance(raw, dict):
        return frozenset()
    declared = raw.get("permissions")
    if not isinstance(declared, (list, tuple)):
        return frozenset()
    return frozenset(str(item) for item in declared
                     if isinstance(item, (str, int)))


def _installed_permissions(installer: LocalPackageInstaller,
                           plugin_id: str) -> frozenset[str]:
    try:
        manifest: PluginManifest | None = installer.installed_manifest(plugin_id)
    except InstallerError:
        return frozenset()
    if manifest is None:
        return frozenset()
    raw = dict(manifest.unknown_fields or {})
    meta = dict(manifest.metadata or {})
    return _permissions_of(raw) | _permissions_of(meta)


class PackageLifecycle:
    """安装/更新/卸载编排（停用→事务→重扫；三类资产分开）。"""

    def __init__(self, installer: LocalPackageInstaller, session: Any, *,
                 desired_store: Any = None,
                 delete_data: Callable[[str], int] | None = None,
                 revoke_credentials: Callable[[str], int] | None = None,
                 refresh_inventory: Callable[[], Any] | None = None) -> None:
        self.installer = installer
        self.session = session
        self._desired_store = desired_store
        self._delete_data = delete_data
        self._revoke_credentials = revoke_credentials
        self._refresh = refresh_inventory

    # ------------------------------------------------------------ 权限对比
    def review(self, package: Path) -> PackageReview:
        """Bounded declarative preview; no plugin execution or activation."""
        raw = _package_manifest_raw(package)
        manifest, diagnostics = parse_manifest_mapping(raw)
        if manifest is None or any(item.severity == "error" for item in diagnostics):
            raise LifecycleInstallError("MANIFEST_INVALID", "包声明不可读取或校验失败")
        permissions = _permissions_of(raw) | _permissions_of(dict(manifest.metadata))
        delta = self.permission_delta(package, expected_plugin_id=manifest.plugin_id)
        return PackageReview(manifest.plugin_id, manifest.version, tuple(sorted(permissions)), delta.added)

    def permission_delta(self, package: Path,
                         *, expected_plugin_id: str | None = None) -> PermissionDelta:
        raw = _package_manifest_raw(package)
        plugin_id = expected_plugin_id or str(
            (raw or {}).get("plugin_id", ""))
        if not plugin_id:
            return PermissionDelta()
        installed = None
        try:
            installed = self.installer.installed_manifest(plugin_id)
        except InstallerError:
            installed = None   # 指针损坏：按重装处理（无基线可比）
        if installed is None:
            # 首装没有"已安装版本"基线——安装同意即权限同意，不构成
            # "新增"（重确认只针对更新场景的新增项）
            return PermissionDelta()
        old = _installed_permissions(self.installer, plugin_id)
        new = _permissions_of(raw) | _permissions_of((raw or {}).get("metadata"))
        return PermissionDelta(
            added=tuple(sorted(new - old)),
            removed=tuple(sorted(old - new)))

    # ------------------------------------------------------------ 安装/更新
    def install_or_update(self, package: Path, *,
                          expected_plugin_id: str | None = None,
                          allow_downgrade: bool = False,
                          confirm_permissions:
                          Callable[[tuple[str, ...]], bool] | None = None,
                          ) -> InstallOutcome:
        """停用→（权限重确认）→安装事务→重扫 Inventory。"""
        raw = _package_manifest_raw(package)
        plugin_id = expected_plugin_id or str((raw or {}).get("plugin_id", ""))
        if plugin_id:
            self._retire_or_refuse(plugin_id, action="更新")
        if plugin_id:
            delta = self.permission_delta(
                package, expected_plugin_id=expected_plugin_id)
            if delta.added:
                # 更新新增权限必须重确认：没有确认入口（回调缺位）同样
                # 拒绝——不因宿主未接线而静默放行
                confirmed = (confirm_permissions(delta.added)
                             if confirm_permissions is not None else False)
                if not confirmed:
                    raise LifecycleInstallError(
                        "PERMISSION_CONFIRMATION_REQUIRED",
                        f"新版本新增权限需确认后才能安装：{', '.join(delta.added)}",
                        added_permissions=delta.added)
        outcome = self.installer.install(
            package, expected_plugin_id=expected_plugin_id,
            allow_downgrade=allow_downgrade)
        if self._refresh is not None:
            self._refresh()
        return outcome

    # ------------------------------------------------------------ 卸载
    def uninstall(self, plugin_id: str, *, remove_data: bool = False) -> dict[str, Any]:
        """停用→卸载（代码）→数据（可选）/凭据（总是）分开清理→重扫。"""
        self._retire_or_refuse(plugin_id, action="卸载")
        data_counter = {"removed": 0}

        def _do_delete(pid: str) -> None:
            if self._delete_data is not None:
                data_counter["removed"] += int(self._delete_data(pid))

        outcome = self.installer.uninstall(
            plugin_id,
            is_active=lambda pid: (
                self.session.lifecycle.accepting_calls(pid)
                or bool(self.session.lifecycle.jobs.active_for(pid))),
            remove_data=remove_data,
            delete_data=_do_delete)
        data_removed = data_counter["removed"]
        credentials_revoked = (int(self._revoke_credentials(plugin_id))
                               if self._revoke_credentials is not None else 0)
        desired_cleared = False
        if self._desired_store is not None:
            desired = self._desired_store.load()
            if desired.pop(plugin_id, None) is not None:
                self._desired_store.save(desired)
                desired_cleared = True
        if self._refresh is not None:
            self._refresh()
        return {"outcome": outcome.outcome, "code_removed": True,
                "data_removed": data_removed,
                "credentials_revoked": credentials_revoked,
                "desired_cleared": desired_cleared}

    # ------------------------------------------------------------ 内部
    def _retire_or_refuse(self, plugin_id: str, *, action: str) -> None:
        """停用链路：未完全停稳即中止（如实报告，可重试）。"""
        accepted = self.session.lifecycle.accepting_calls(plugin_id)
        has_contributions = (self.session.shortcut_count(plugin_id) > 0
                             or bool(self.session.windows.windows_for(plugin_id)))
        if not accepted and not has_contributions:
            return   # 未激活：无需停用
        report = self.session.retire_plugin(plugin_id)
        if not report.get("fully_stopped"):
            raise LifecycleInstallError(
                "PLUGIN_BUSY",
                f"{action}中止：插件任务尚未确认终止或清理未完成"
                f"（残留：{report.get('jobs_still_terminating') or report.get('cleanup_failures')}），可重试")


__all__ = ["PackageLifecycle", "PermissionDelta", "LifecycleInstallError"]
