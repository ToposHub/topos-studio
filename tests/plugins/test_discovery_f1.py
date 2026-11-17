"""F1（PL-02 收口）：单一发现聚合——builtin/已安装/显式开发源 → 一份 Inventory。

口径：
* 受管安装根与发现根闭环：install → scan 即见；current 指针损坏 →
  ``missing`` + 诊断（不假装在账）；
* 显式开发源注册表持久化（增删原子、重复登记幂等）；
* 三源重复身份 → inventory 打 PLUGIN_ID_DUPLICATE 诊断保留首个（builtin
  > installed > dev 的明确优先级）；
* 声明/运行版本分槽、来源标签直达 UI 文案。
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.shared.plugins.discovery import (
    DevSourceRegistry, PluginSourceConfig, dev_records, installed_records,
    scan_sources, source_label,
)
from src.shared.plugins.installer import LocalPackageInstaller
from src.shared.plugins.inventory import PluginInventory

HOST = dict(host_api=1, topos_version="1.6.0", platform="darwin", arch="x86_64")


def _pkg(root: Path, plugin_id="files.my_tool", version="1.0.0") -> Path:
    folder = root / f"pkg-{version}"
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"plugin_id": plugin_id, "display_name": "My Tool",
                "version": version,
                "capabilities": [{"capability_id": "launch", "kind": "command"}]}
    (folder / "topos.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def _install(root: Path, plugin_id="files.my_tool", version="1.0.0") -> None:
    installer = LocalPackageInstaller(root / "installed", host_facts=HOST)
    installer.install(_pkg(root, plugin_id=plugin_id, version=version))


# ---------------------------------------------------------------------------
# 已安装（受管安装根）——闭环读侧
# ---------------------------------------------------------------------------

def test_installed_records_read_managed_root_after_install(tmp_path):
    _install(tmp_path)
    records = installed_records(tmp_path / "installed", host_facts=HOST)
    assert len(records) == 1
    record = records[0]
    assert record.plugin_id == "files.my_tool"
    assert record.source == "installed" and record.install_state == "installed"
    assert record.version == "1.0.0" and record.manifest is not None
    # 第三方安装默认停用（宿主政策），运行版本空槽（宿主未上报）
    assert record.desired_enabled is False and record.runtime_version == ""
    assert record.runtime_state == "not_loaded"


def test_installed_records_survive_update_and_close_the_loop(tmp_path):
    _install(tmp_path, version="1.0.0")
    _install(tmp_path, version="2.0.0")
    records = installed_records(tmp_path / "installed", host_facts=HOST)
    assert records[0].version == "2.0.0"   # current 指针换新后读侧即见


def test_broken_pointer_is_missing_with_diagnostic(tmp_path):
    _install(tmp_path)
    pointer = tmp_path / "installed" / "files.my_tool" / "current"
    pointer.unlink()
    records = installed_records(tmp_path / "installed", host_facts=HOST)
    assert len(records) == 1
    record = records[0]
    assert record.install_state == "missing"
    assert record.error_code == "INSTALL_POINTER_BROKEN"
    assert any(item.code == "INSTALL_POINTER_BROKEN"
               for item in record.diagnostics)


def test_installed_records_empty_for_absent_root(tmp_path):
    assert installed_records(tmp_path / "nowhere", host_facts=HOST) == []


# ---------------------------------------------------------------------------
# 显式开发源：注册表 + 扫描
# ---------------------------------------------------------------------------

def test_dev_source_registry_persists_and_dedupes(tmp_path):
    registry = DevSourceRegistry(tmp_path / "config" / "dev_sources.json")
    assert registry.list() == []
    assert registry.add(tmp_path / "dev-a") is True
    assert registry.add(tmp_path / "dev-a") is False          # 重复登记幂等
    assert registry.add(tmp_path / "dev-b") is True
    assert [item.name for item in registry.list()] == ["dev-a", "dev-b"]
    # 新实例（重启语义）从持久化文件读回
    assert [item.name for item in DevSourceRegistry(
        tmp_path / "config" / "dev_sources.json").list()] == ["dev-a", "dev-b"]
    assert registry.remove(tmp_path / "dev-a") is True
    assert registry.remove(tmp_path / "dev-a") is False
    assert [item.name for item in registry.list()] == ["dev-b"]


def test_dev_records_scan_manifest_dirs(tmp_path):
    dev = tmp_path / "dev"
    (dev / "files.cool").mkdir(parents=True)
    (dev / "files.cool" / "topos.plugin.json").write_text(json.dumps({
        "plugin_id": "files.cool", "display_name": "Cool", "version": "0.3.0",
        "capabilities": [{"capability_id": "launch", "kind": "command"}]}),
        encoding="utf-8")
    records = dev_records([dev])
    assert len(records) == 1
    record = records[0]
    assert record.plugin_id == "files.cool" and record.source == "local_dev"
    assert record.install_state == "discovered"
    assert record.desired_enabled is False    # 未验证开发源默认停用


def test_dev_records_report_missing_root_and_bad_manifest(tmp_path):
    missing = dev_records([tmp_path / "gone"])
    assert missing[0].error_code == "DEV_ROOT_MISSING"
    dev = tmp_path / "dev2"
    (dev / "empty").mkdir(parents=True)
    bad = dev_records([dev])
    assert bad[0].error_code == "DEV_MANIFEST_MISSING"
    assert bad[0].install_state == "discovered"   # 诊断可见，不静默跳过


# ---------------------------------------------------------------------------
# 单一聚合 + 重复身份诊断
# ---------------------------------------------------------------------------

def _legacy_row(plugin_id="video_pro", **extra):
    base = dict(plugin_id=plugin_id, name=plugin_id, version="1.0.0",
                path=None, diagnostics=(), desired_enabled=True,
                runtime_loaded=True, self_disabled=False, needs_restart=False,
                error="", runtime_version="")
    base.update(extra)
    return SimpleNamespace(**base)


def test_scan_sources_merges_three_sources(tmp_path):
    _install(tmp_path, plugin_id="files.installed_tool")
    dev = tmp_path / "dev"
    (dev / "files.dev_tool").mkdir(parents=True)
    (dev / "files.dev_tool" / "topos.plugin.json").write_text(json.dumps({
        "plugin_id": "files.dev_tool", "display_name": "Dev", "version": "0.1.0",
        "capabilities": [{"capability_id": "launch", "kind": "command"}]}),
        encoding="utf-8")
    config = PluginSourceConfig.for_host(
        builtin_ids={"video_pro"},
        install_root=tmp_path / "installed",
        dev_roots=[dev])
    records = scan_sources([_legacy_row("video_pro")], config, host_facts=HOST)
    by_id = {record.plugin_id: record for record in records}
    assert by_id["files.video_pro"].source == "builtin"
    assert by_id["files.installed_tool"].source == "installed"
    assert by_id["files.dev_tool"].source == "local_dev"


def test_duplicate_identity_across_sources_gets_diagnosed(tmp_path):
    # 同一 plugin_id 同时来自 builtin 目录与受管安装根：保留首个（builtin
    # 优先），冲突两边都可见——不做静默的最后胜出
    _install(tmp_path, plugin_id="files.video_pro")
    config = PluginSourceConfig.for_host(
        builtin_ids={"video_pro"}, install_root=tmp_path / "installed")
    inventory = PluginInventory(host_api=1, topos_version="1.6.0",
                                platform="darwin", arch="x86_64")
    records = scan_sources([_legacy_row("video_pro")], config, host_facts=HOST)
    snapshot = inventory.refresh(records)
    record = snapshot.record("files.video_pro")
    assert record is not None
    assert record.source == "builtin"        # 优先级明确：builtin 保留
    duplicates = [item for item in record.diagnostics
                  if item.code == "PLUGIN_ID_DUPLICATE"]
    assert duplicates and "多个来源" in duplicates[0].message


def test_runtime_version_slot_survives_refresh(tmp_path):
    """声明/运行版本分槽：宿主 mark_runtime 后再 refresh 不丢运行版本。"""
    _install(tmp_path, plugin_id="files.installed_tool")
    inventory = PluginInventory(**{k: v for k, v in HOST.items()
                                   if k != "host_api"} | {"host_api": 1})
    records = scan_sources([], PluginSourceConfig.for_host(
        builtin_ids=set(), install_root=tmp_path / "installed"), host_facts=HOST)
    inventory.refresh(records)
    inventory.mark_runtime("files.installed_tool", "active",
                           runtime_version="1.0.0")
    # 再 refresh（安装根重扫）：发现快照的空运行版本不得覆盖宿主事实
    snapshot = inventory.refresh(scan_sources(
        [], PluginSourceConfig.for_host(builtin_ids=set(),
                                        install_root=tmp_path / "installed"),
        host_facts=HOST))
    assert snapshot.record("files.installed_tool").runtime_version == "1.0.0"


def test_source_labels_for_ui():
    assert source_label("builtin") == "内建"
    assert source_label("installed") == "已安装"
    assert source_label("local_dev") == "开发源"
    assert source_label("mystery") == "mystery"   # 未知来源原样展示
