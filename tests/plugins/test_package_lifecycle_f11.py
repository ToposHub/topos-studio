"""F11：安装/更新/卸载编排——停用→事务→重扫；权限重确认；三类资产分开。"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.shared.plugins.discovery import DesiredStateStore
from src.shared.plugins.host_session import PluginHostSession
from src.shared.plugins.installer import LocalPackageInstaller
from src.shared.plugins.package_lifecycle import (
    LifecycleInstallError,
    PackageLifecycle,
)

HOST = dict(host_api=1, topos_version="3.0.0", platform="darwin", arch="x86_64")


def _pkg(root: Path, version="1.0.0", permissions=("path",),
         plugin_id="files.managed_tool") -> Path:
    folder = root / f"pkg-{version}"
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"plugin_id": plugin_id, "display_name": "Managed",
                "version": version, "permissions": list(permissions),
                "capabilities": [{"capability_id": "launch",
                                  "kind": "command"}]}
    (folder / "topos.plugin.json").write_text(json.dumps(manifest),
                                              encoding="utf-8")
    return folder


def _zip_of(folder: Path, target: Path) -> Path:
    with zipfile.ZipFile(target, "w") as archive:
        for path in sorted(folder.rglob("*")):
            archive.write(path, path.relative_to(folder).as_posix())
    return target


def _env(tmp_path: Path):
    installer = LocalPackageInstaller(tmp_path / "installed", host_facts=HOST)
    session = PluginHostSession()
    desired = DesiredStateStore(tmp_path / "desired.json")
    refreshes = []
    lifecycle = PackageLifecycle(
        installer, session, desired_store=desired,
        delete_data=lambda pid: _clear_data(tmp_path, pid),
        revoke_credentials=lambda pid: session.secrets.revoke_all(pid),
        refresh_inventory=lambda: refreshes.append(True))
    return installer, session, desired, lifecycle, refreshes


def _clear_data(tmp_path: Path, plugin_id: str) -> int:
    from src.features.files.plugin_broker_host import PluginConfigStore

    return PluginConfigStore(tmp_path / "plugin_config.json") \
        .clear_namespace(plugin_id)


# ---------------------------------------------------------------------------
# 停用→安装→重扫
# ---------------------------------------------------------------------------

def test_install_then_update_goes_through_retire_and_rescan(tmp_path):
    installer, session, desired, lifecycle, refreshes = _env(tmp_path)
    outcome = lifecycle.install_or_update(_pkg(tmp_path, "1.0.0"))
    assert outcome.outcome == "installed"
    assert refreshes == [True]                    # 安装后 Inventory 重扫
    # 激活后更新：先经停用链路（贡献释放），再换版本
    session.activate_plugin("files.managed_tool",
                            lambda c: c.register("menu", "m", lambda: None))
    outcome2 = lifecycle.install_or_update(_pkg(tmp_path, "2.0.0"))
    assert outcome2.outcome == "updated"
    assert installer.current_version("files.managed_tool") == "2.0.0"
    assert refreshes == [True, True]
    assert not session.lifecycle.accepting_calls("files.managed_tool")


def test_update_refuses_while_plugin_busy(tmp_path):
    installer, session, desired, lifecycle, refreshes = _env(tmp_path)
    lifecycle.install_or_update(_pkg(tmp_path, "1.0.0"))
    session.activate_plugin("files.managed_tool", lambda c: None)
    session.lifecycle.jobs.start("long-job", plugin_id="files.managed_tool")
    with pytest.raises(LifecycleInstallError) as exc:
        lifecycle.install_or_update(_pkg(tmp_path, "2.0.0"))
    assert exc.value.code == "PLUGIN_BUSY"        # 未停稳：如实中止可重试
    assert installer.current_version("files.managed_tool") == "1.0.0"
    session.lifecycle.jobs.confirm_cancelled("long-job")
    session.retire_plugin("files.managed_tool")
    assert lifecycle.install_or_update(
        _pkg(tmp_path, "2.0.0")).outcome == "updated"   # 停稳后重试成功


# ---------------------------------------------------------------------------
# 新增权限重确认
# ---------------------------------------------------------------------------

def test_new_permissions_require_confirmation(tmp_path):
    installer, session, desired, lifecycle, refreshes = _env(tmp_path)
    lifecycle.install_or_update(_pkg(tmp_path, "1.0.0",
                                     permissions=("path",)))
    upgraded = _pkg(tmp_path, "2.0.0", permissions=("path", "network"))
    delta = lifecycle.permission_delta(upgraded)
    assert delta.added == ("network",)            # 新增可见
    # 未提供确认回调 → 拒绝安装
    with pytest.raises(LifecycleInstallError) as exc:
        lifecycle.install_or_update(upgraded)
    assert exc.value.code == "PERMISSION_CONFIRMATION_REQUIRED"
    assert exc.value.added_permissions == ("network",)
    # 确认回调拒绝 → 拒绝安装
    with pytest.raises(LifecycleInstallError) as denied:
        lifecycle.install_or_update(
            upgraded, confirm_permissions=lambda added: False)
    assert denied.value.code == "PERMISSION_CONFIRMATION_REQUIRED"
    # 确认回调放行 → 安装
    seen: list[tuple] = []
    outcome = lifecycle.install_or_update(
        upgraded, confirm_permissions=lambda added: (seen.append(added), True)[1])
    assert outcome.outcome == "updated" and seen == [("network",)]


def test_permission_removal_does_not_require_confirmation(tmp_path):
    installer, session, desired, lifecycle, refreshes = _env(tmp_path)
    lifecycle.install_or_update(_pkg(tmp_path, "1.0.0",
                                     permissions=("path", "network")))
    lighter = _pkg(tmp_path, "2.0.0", permissions=("path",))
    confirmations = []
    outcome = lifecycle.install_or_update(
        lighter, confirm_permissions=lambda added: (
            confirmations.append(added), True)[1])
    assert outcome.outcome == "updated"
    assert confirmations == []                    # 纯移除不触发确认


def test_first_install_permissions_do_not_trigger_confirmation(tmp_path):
    # 首装没有"已安装版本"可比——不构成"新增"
    installer, session, desired, lifecycle, refreshes = _env(tmp_path)
    confirmations = []
    lifecycle.install_or_update(
        _pkg(tmp_path, "1.0.0", permissions=("path", "network")),
        confirm_permissions=lambda added: (confirmations.append(added), True)[1])
    assert confirmations == []


# ---------------------------------------------------------------------------
# 卸载：代码/数据/凭据分开；素材不触及
# ---------------------------------------------------------------------------

def test_uninstall_separates_code_data_credentials(tmp_path):
    from src.features.files.plugin_broker_host import PluginConfigStore

    installer, session, desired, lifecycle, refreshes = _env(tmp_path)
    lifecycle.install_or_update(_pkg(tmp_path, "1.0.0"))
    config = PluginConfigStore(tmp_path / "plugin_config.json")
    config.set("files.managed_tool", "layout", "grid")
    session.secrets.register("token", "V")
    session.secrets.grant("files.managed_tool", "token")
    desired_map = desired.load()
    desired_map["files.managed_tool"] = True
    desired.save(desired_map)
    # 素材文件（库目录内）——卸载绝不触及
    media = tmp_path / "library" / "clip.mov"
    media.parent.mkdir(exist_ok=True)
    media.write_bytes(b"media-bytes")

    report = lifecycle.uninstall("files.managed_tool", remove_data=True)
    assert report["code_removed"] is True
    assert report["data_removed"] == 1            # 用户数据按选项清
    assert report["credentials_revoked"] == 1     # 凭据总是撤
    assert report["desired_cleared"] is True      # 偏好随代码清
    assert not (tmp_path / "installed" / "files.managed_tool").exists()
    assert config.get("files.managed_tool", "layout") is None
    assert session.secrets.resolve_for_host("files.managed_tool", "token") is None
    assert media.read_bytes() == b"media-bytes"   # 素材不触及
    assert refreshes[-1] is True


def test_uninstall_keeps_data_when_remove_data_false(tmp_path):
    from src.features.files.plugin_broker_host import PluginConfigStore

    installer, session, desired, lifecycle, refreshes = _env(tmp_path)
    lifecycle.install_or_update(_pkg(tmp_path, "1.0.0"))
    config = PluginConfigStore(tmp_path / "plugin_config.json")
    config.set("files.managed_tool", "layout", "grid")
    report = lifecycle.uninstall("files.managed_tool", remove_data=False)
    assert report["data_removed"] == 0            # 数据保留（用户选择）
    assert report["credentials_revoked"] == 0     # 从未授权：撤 0
    assert config.get("files.managed_tool", "layout") == "grid"


def test_uninstall_refuses_while_busy(tmp_path):
    installer, session, desired, lifecycle, refreshes = _env(tmp_path)
    lifecycle.install_or_update(_pkg(tmp_path, "1.0.0"))
    session.activate_plugin("files.managed_tool", lambda c: None)
    session.lifecycle.jobs.start("j", plugin_id="files.managed_tool")
    with pytest.raises(LifecycleInstallError) as exc:
        lifecycle.uninstall("files.managed_tool")
    assert exc.value.code == "PLUGIN_BUSY"
    assert installer.current_version("files.managed_tool") == "1.0.0"  # 未动
