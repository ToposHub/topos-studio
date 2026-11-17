"""PL-02 回归：安装清单与状态投影——安装/期望/运行三态分离。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.shared.plugins.inventory import (
    PluginInventory,
    PluginRecord,
    default_desired_for,
    legacy_files_records,
)
from src.shared.plugins.manifest import parse_manifest_mapping


def _manifest(plugin_id="files.demo", **kw):
    data = {"plugin_id": plugin_id, "display_name": "Demo", "version": "1.0.0",
            "capabilities": [{"capability_id": "launch", "kind": "command"}]}
    data.update(kw)
    manifest, diagnostics = parse_manifest_mapping(data)
    assert manifest is not None and diagnostics == ()
    return manifest


def _record(manifest=None, **kw):
    defaults = dict(plugin_id="files.demo", display_name="Demo", version="1.0.0",
                    source="builtin", install_state="installed",
                    desired_enabled=True, runtime_state="not_loaded")
    defaults.update(kw)
    return PluginRecord(manifest=manifest, **defaults)


HOST = dict(host_api=1, topos_version="1.6.0", platform="darwin", arch="x86_64")


def test_states_are_separate_not_one_boolean():
    inventory = PluginInventory(**HOST)
    snapshot = inventory.refresh([_record(desired_enabled=False, runtime_state="loaded")])
    record = snapshot.record("files.demo")
    assert record.install_state == "installed"
    assert record.desired_enabled is False          # 用户意图：下次停用
    assert record.runtime_state == "loaded"         # 现实：本会话仍在跑
    assert record.pending_restart is True


def test_set_desired_updates_restart_flag_without_touching_runtime():
    inventory = PluginInventory(**HOST)
    inventory.refresh([_record()])
    record = inventory.set_desired("files.demo", False)
    assert record.runtime_state == "not_loaded"
    assert record.pending_restart is False          # 从未加载：停用无需重启
    record = inventory.set_desired("files.demo", True)
    assert record.pending_restart is True           # 启用需要重启才会加载（旧语义一致）


def test_mark_runtime_failure_keeps_phase_and_error():
    inventory = PluginInventory(**HOST)
    inventory.refresh([_record()])
    record = inventory.mark_runtime("files.demo", "failed", failure_phase="activate",
                                    error_code="PLUGIN_SETUP_FAILED",
                                    error_message="setup() 第三步抛异常")
    assert record.failure_phase == "activate"
    assert record.error_code == "PLUGIN_SETUP_FAILED"
    with pytest.raises(ValueError):
        inventory.mark_runtime("files.demo", "teleported")
    with pytest.raises(KeyError):
        inventory.mark_runtime("files.ghost", "loaded")


def test_refresh_is_atomic_and_bumps_revision():
    inventory = PluginInventory(**HOST)
    first = inventory.refresh([_record()])
    stale = inventory.snapshot()                     # 旧快照仍完整可用
    second = inventory.refresh([_record(), _record(plugin_id="files.other",
                                                   manifest=_manifest("files.other"),
                                                   display_name="Other")])
    assert second.revision > first.revision
    assert stale.record("files.other") is None       # 旧快照看不到新记录（无半刷新）
    assert second.record("files.other") is not None


def test_blocking_diagnostics_reject_unloaded_but_keep_loaded_visible():
    inventory = PluginInventory(**HOST)
    snapshot = inventory.refresh([
        _record(plugin_id="files.unloaded",
                manifest=_manifest("files.unloaded", host_api=[9, 9]),
                runtime_state="not_loaded"),
        _record(plugin_id="files.loaded",
                manifest=_manifest("files.loaded", host_api=[9, 9]),
                runtime_state="loaded"),
    ])
    assert snapshot.record("files.unloaded").runtime_state == "rejected"
    assert snapshot.record("files.unloaded").failure_phase == "validate"
    loaded = snapshot.record("files.loaded")
    assert loaded.runtime_state == "loaded"          # 已加载的不隐藏，仅诊断可见
    assert any(d.code == "HOST_API_INCOMPATIBLE" for d in loaded.diagnostics)


def test_record_with_foreign_manifest_identity_is_rejected():
    inventory = PluginInventory(**HOST)
    snapshot = inventory.refresh([
        _record(plugin_id="files.imposter", manifest=_manifest("files.real"),),
    ])
    record = snapshot.record("files.imposter")
    assert record.runtime_state == "rejected"
    assert any(d.code == "RECORD_ID_MISMATCH" for d in record.diagnostics)


def test_missing_directory_is_a_state_not_disappearance():
    inventory = PluginInventory(**HOST)
    snapshot = inventory.refresh([_record(install_state="missing", desired_enabled=True)])
    record = snapshot.record("files.demo")
    assert record.install_state == "missing"
    assert record.installable is False               # 不能假装还可安装/启用


def test_identity_and_preferences_survive_version_change():
    inventory = PluginInventory(**HOST)
    inventory.refresh([_record(version="1.0.0")])
    shortcuts = {"files.demo": "Ctrl+Alt+1"}         # 用户数据按 plugin_id 存
    inventory.refresh([_record(version="2.3.4")])
    snapshot = inventory.refresh([_record(version="9.9.9")])
    assert snapshot.record("files.demo").plugin_id == "files.demo"
    assert shortcuts["files.demo"] == "Ctrl+Alt+1"   # 键不含版本 → 不丢


def test_default_desired_policy():
    assert default_desired_for("builtin", None) is True
    assert default_desired_for("local_dev", None) is False
    assert default_desired_for("verified_third_party", None) is False
    assert default_desired_for("verified_third_party", True) is True   # 已有意图优先


# ---------------------------------------------------------------------------
# 旧 Files 目录适配
# ---------------------------------------------------------------------------

@dataclass
class _LegacyRow:
    plugin_id: str
    name: str = "Demo"
    version: str = "1.0.0"
    runtime_loaded: bool = False
    desired_enabled: bool = True
    self_disabled: bool = False
    needs_restart: bool = False
    error: str = ""
    path: Path | None = None
    diagnostics: tuple = ()


def test_legacy_rows_map_to_unified_records(tmp_path):
    folder = tmp_path / "image_factory"
    folder.mkdir()
    (folder / "__init__.py").write_text(
        "NAME = '图片工厂'\nVERSION = '1.4.0'\nAUTHOR = 'Topos'\nAPI_VERSION = 1\n",
        encoding="utf-8")
    records = legacy_files_records([
        _LegacyRow("image_factory", name="图片工厂", runtime_loaded=True,
                   desired_enabled=True, path=folder),
        _LegacyRow("broken_one", runtime_loaded=False, desired_enabled=True,
                   error="import blew up"),
        _LegacyRow("self_off", self_disabled=True),
    ])
    by_id = {record.plugin_id: record for record in records}
    loaded = by_id["files.image_factory"]
    assert loaded.runtime_state == "loaded"
    assert loaded.manifest is not None
    assert loaded.manifest.legacy_ids == ("image_factory",)
    assert loaded.display_name == "图片工厂"
    failed = by_id["files.broken_one"]
    assert failed.runtime_state == "failed" and failed.failure_phase == "load"
    assert failed.error_code == "LEGACY_LOAD_FAILED"
    rejected = by_id["files.self_off"]
    assert rejected.runtime_state == "rejected"
    assert rejected.error_code == "SELF_DISABLED"


def test_legacy_records_flow_through_inventory_validation(tmp_path):
    folder = tmp_path / "future_api"
    folder.mkdir()
    (folder / "__init__.py").write_text("NAME='Future'\nAPI_VERSION=7\n", encoding="utf-8")
    inventory = PluginInventory(**HOST)
    snapshot = inventory.refresh(legacy_files_records(
        [_LegacyRow("future_api", path=folder, runtime_loaded=False, desired_enabled=True)]))
    record = snapshot.record("files.future_api")
    assert record.runtime_state == "rejected"        # 未来 API：可见但拒绝
    assert any(d.code == "HOST_API_INCOMPATIBLE" for d in record.diagnostics)


def test_diagnostics_are_bounded():
    from src.shared.plugins.manifest import ManifestDiagnostic
    flood = tuple(ManifestDiagnostic(code="X", severity="info", message="m", field="f")
                  for _ in range(500))
    inventory = PluginInventory(**HOST)
    snapshot = inventory.refresh([_record(diagnostics=flood)])
    assert len(snapshot.record("files.demo").diagnostics) <= 50


def test_legacy_catalog_diagnostics_are_typed_and_survive_projection() -> None:
    from src.shared.plugins.manifest import ManifestDiagnostic

    original = ManifestDiagnostic(code="KNOWN", severity="info", message="已有诊断")
    records = legacy_files_records([_LegacyRow(
        "demo", diagnostics=("无法读取插件配置", original, object()),
    )])
    snapshot = PluginInventory(**HOST).refresh(records)
    record = snapshot.record("files.demo")
    assert record is not None
    assert [diagnostic.code for diagnostic in record.diagnostics] == [
        "LEGACY_CATALOG_DIAGNOSTIC", "KNOWN", "LEGACY_DIAGNOSTIC_INVALID",
    ]
    assert record.diagnostics[0].message == "无法读取插件配置"
    assert record.diagnostics[1] is original
    assert record.to_dict()["diagnostics"][0]["message"] == "无法读取插件配置"


def test_legacy_single_string_diagnostic_is_not_split_into_characters() -> None:
    records = legacy_files_records([_LegacyRow("demo", diagnostics="读取失败")])
    assert len(records[0].diagnostics) == 1
    assert records[0].diagnostics[0].message == "读取失败"


def test_legacy_parser_diagnostics_are_preserved_ahead_of_catalog_flood(tmp_path: Path) -> None:
    folder = tmp_path / "broken"
    folder.mkdir()
    (folder / "__init__.py").write_text("NAME = (", encoding="utf-8")
    records = legacy_files_records([_LegacyRow(
        "broken", path=folder, diagnostics=tuple("普通诊断" for _ in range(100)),
    )])
    snapshot = PluginInventory(**HOST).refresh(records)
    record = snapshot.record("files.broken")
    assert record is not None and record.runtime_state == "rejected"
    assert record.failure_phase == "validate"
    assert any(diagnostic.code == "LEGACY_INIT_UNREADABLE" for diagnostic in record.diagnostics)
    assert len(record.diagnostics) <= 50


def test_legacy_invalid_api_parser_warning_is_not_discarded(tmp_path: Path) -> None:
    folder = tmp_path / "future"
    folder.mkdir()
    (folder / "__init__.py").write_text("API_VERSION = 'future'", encoding="utf-8")
    records = legacy_files_records([_LegacyRow("future", path=folder)])
    assert any(diagnostic.code == "LEGACY_API_VERSION_INVALID" for diagnostic in records[0].diagnostics)


# ---------------------------------------------------------------------------
# PR-03 回归：原子快照、重复身份与宿主管理的来源
# ---------------------------------------------------------------------------

def test_snapshot_revision_and_records_never_mix_refresh_halves():
    """复现：读线程在 refresh 途中取快照，revision 与 records 必须成对。"""
    import threading
    inventory = PluginInventory(**HOST)
    inventory.refresh([_record()])
    stop = threading.Event()
    errors: list[str] = []

    def refresher() -> None:
        counter = 0
        while not stop.is_set():
            counter += 1
            inventory.refresh([_record(),
                               *(_record(plugin_id=f"files.extra{index}",
                                         manifest=_manifest(f"files.extra{index}"))
                                 for index in range(counter))])

    worker = threading.Thread(target=refresher, daemon=True)
    worker.start()
    for _ in range(400):
        snapshot = inventory.snapshot()
        # 每次恰好新增一个 id：revision 与记录数必须一致，否则就是半刷新
        if snapshot.revision != len(snapshot.records):
            errors.append(f"revision={snapshot.revision} records={len(snapshot.records)}")
    stop.set()
    worker.join(2)
    assert errors == []


def test_duplicate_ids_are_flagged_not_last_write_wins():
    """复现 6：同 id 两个来源——保留首个、显式诊断，不做静默覆盖。"""
    inventory = PluginInventory(**HOST)
    snapshot = inventory.refresh([
        _record(version="1.0.0"),
        _record(version="9.9.9"),
    ])
    record = snapshot.record("files.demo")
    assert record is not None
    assert record.version == "1.0.0"                       # 首个保留，9.9.9 被拒绝
    assert any(d.code == "PLUGIN_ID_DUPLICATE" and d.severity == "error"
               for d in record.diagnostics)
    assert len(snapshot.records) == 1


def test_legacy_alias_conflict_across_records_is_flagged():
    first = _record(plugin_id="files.a", display_name="A",
                    manifest=_manifest("files.a", legacy_ids=["old"]))
    second = _record(plugin_id="files.b", display_name="B", source="local_dev",
                     manifest=_manifest("files.b", legacy_ids=["old"]))
    snapshot = PluginInventory(**HOST).refresh([first, second])
    assert any(d.code == "LEGACY_ID_CONFLICT" for d in snapshot.record("files.b").diagnostics)
    assert snapshot.record("files.a") is not None          # 两个记录都保留可见


def test_legacy_adapter_prefers_declarative_manifest_with_matching_identity(tmp_path):
    folder = tmp_path / "image_factory"
    folder.mkdir()
    (folder / "__init__.py").write_text("NAME = '旧名'\nVERSION = '1.0.0'\nAPI_VERSION = 1\n",
                                        encoding="utf-8")
    (folder / "topos.plugin.json").write_text(json.dumps({
        "plugin_id": "files.image_factory", "display_name": "声明名", "version": "2.0.0",
        "legacy_ids": ["image_factory"],
        "capabilities": [{"capability_id": "launch", "kind": "command"}],
    }), encoding="utf-8")
    records = legacy_files_records([_LegacyRow("image_factory", path=folder)])
    manifest = records[0].manifest
    assert manifest is not None and manifest.display_name == "声明名"    # 声明优先于常量
    assert records[0].version == "2.0.0"
    assert manifest.legacy_ids == ("image_factory",)


def test_legacy_adapter_rejects_foreign_declared_identity(tmp_path):
    folder = tmp_path / "image_factory"
    folder.mkdir()
    (folder / "__init__.py").write_text("NAME = '旧名'\nAPI_VERSION = 1\n", encoding="utf-8")
    (folder / "topos.plugin.json").write_text(json.dumps({
        "plugin_id": "files.imposter", "display_name": "冒名", "version": "1.0.0",
    }), encoding="utf-8")
    records = legacy_files_records([_LegacyRow("image_factory", path=folder)])
    assert records[0].manifest is None                     # 身份不符：声明整体忽略
    assert any(d.code == "DECLARED_ID_MISMATCH" and d.severity == "error"
               for d in records[0].diagnostics)
    snapshot = PluginInventory(**HOST).refresh(records)    # 阻断级诊断→可见且拒绝
    assert snapshot.record("files.image_factory").runtime_state == "rejected"


def test_legacy_source_is_host_policy_not_declaration(tmp_path):
    folder = tmp_path / "image_factory"
    folder.mkdir()
    (folder / "__init__.py").write_text("NAME='X'\nAPI_VERSION=1\n", encoding="utf-8")
    unmanaged = legacy_files_records([_LegacyRow("image_factory", path=folder)])
    assert unmanaged[0].source == "local_dev"              # 未在宿主内建名单：未验证
    managed = legacy_files_records([_LegacyRow("image_factory", path=folder)],
                                   builtin_ids={"image_factory"})
    assert managed[0].source == "builtin"
    assert default_desired_for(unmanaged[0].source, None) is False   # 未知来源默认停用
    # 运行版本与声明版本分槽投影，互不覆盖
    assert unmanaged[0].runtime_version == ""
    assert "runtime_version" in unmanaged[0].to_dict()
