"""
P1-12 用户自建 Smart Bin（已保存搜索）测试。

覆盖：
- SmartBin 日志过滤条件：keywords（任一/全部）、min_rating、flag、
  color_label、comment_contains、offline、proxy_status
- EditProject.add/remove/rename_smart_bin CRUD 与幂等/冲突语义
- smart_bins 字段在既有工程操作（bin CRUD / relink / 日志编辑 / 时间线操作）
  后不丢失（构造点接线回归）
- CreateSmartBinCommand / DeleteSmartBinCommand 的 apply/undo/redo
- 序列化 round-trip（EditProjectSerializer + edit/bins 风格模块数据）
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    SmartBin,
    relink_asset,
)
from src.features.edit.core.commands import (
    CreateSmartBinCommand,
    DeleteSmartBinCommand,
    UpdateAssetLogCommand,
)
from src.features.edit.core.serialization import EditProjectSerializer


def _asset(asset_id: str, **overrides) -> MediaAsset:
    params = dict(
        asset_id=asset_id,
        path=f"/tmp/{asset_id}.mov",
        media_type="video",
        display_name=f"{asset_id}.mov",
        metadata={"codec": "h264"},
    )
    params.update(overrides)
    return MediaAsset(**params)


def _project_with_logged_assets() -> EditProject:
    return EditProject(
        project_id="p1",
        name="proj",
        assets={
            "a1": _asset("a1", keywords=("interview", "indoor"), rating=4,
                         color_label="blue", comment="备用机位 正片可用"),
            "a2": _asset("a2", keywords=("broll",), rating=2, flag="reject"),
            "a3": _asset("a3", rating=5),
            "a4": _asset("a4", is_offline=True, proxy_status="ready"),
        },
        root_bin_id="root",
    )


def _smart_bin(name: str, **filter_params) -> SmartBin:
    return SmartBin(
        bin_id=f"sb-{name}",
        name=name,
        filter_type="custom",
        filter_params=filter_params,
    )


# ================================================================
# 日志过滤条件
# ================================================================

class TestSmartBinLogFilters:
    def test_keyword_any_match(self) -> None:
        project = _project_with_logged_assets()
        sb = _smart_bin("kw", keywords=["interview", "broll"])
        ids = {a.asset_id for a in sb.get_assets(project)}
        assert ids == {"a1", "a2"}

    def test_keyword_require_all(self) -> None:
        project = _project_with_logged_assets()
        sb = _smart_bin("all", keywords=["interview", "indoor"],
                        require_all_keywords=True)
        ids = {a.asset_id for a in sb.get_assets(project)}
        assert ids == {"a1"}

    def test_min_rating(self) -> None:
        project = _project_with_logged_assets()
        sb = _smart_bin("rated", min_rating=4)
        ids = {a.asset_id for a in sb.get_assets(project)}
        assert ids == {"a1", "a3"}

    def test_flag_reject(self) -> None:
        project = _project_with_logged_assets()
        sb = _smart_bin("rejected", flag="reject")
        ids = {a.asset_id for a in sb.get_assets(project)}
        assert ids == {"a2"}

    def test_color_label(self) -> None:
        project = _project_with_logged_assets()
        sb = _smart_bin("blue", color_label="blue")
        ids = {a.asset_id for a in sb.get_assets(project)}
        assert ids == {"a1"}

    def test_comment_contains_case_insensitive(self) -> None:
        project = _project_with_logged_assets()
        sb = _smart_bin("note", comment_contains="正片")
        ids = {a.asset_id for a in sb.get_assets(project)}
        assert ids == {"a1"}

    def test_offline_condition(self) -> None:
        project = _project_with_logged_assets()
        sb = _smart_bin("offline", offline=True)
        ids = {a.asset_id for a in sb.get_assets(project)}
        assert ids == {"a4"}

    def test_proxy_status_condition(self) -> None:
        project = _project_with_logged_assets()
        sb = _smart_bin("hasproxy", proxy_status="ready")
        ids = {a.asset_id for a in sb.get_assets(project)}
        assert ids == {"a4"}

    def test_combined_with_legacy_conditions(self) -> None:
        project = _project_with_logged_assets()
        sb = _smart_bin("combo", keywords=["interview"], min_rating=3,
                        media_type="video", codec="h264")
        ids = {a.asset_id for a in sb.get_assets(project)}
        assert ids == {"a1"}

    def test_empty_filter_matches_all(self) -> None:
        project = _project_with_logged_assets()
        sb = _smart_bin("everything")
        assert len(sb.get_assets(project)) == 4


# ================================================================
# EditProject Smart Bin CRUD
# ================================================================

class TestSmartBinCrud:
    def test_add_and_conflict(self) -> None:
        project = _project_with_logged_assets()
        sb = _smart_bin("mine", keywords=["x"])
        project2 = project.add_smart_bin(sb)
        assert "sb-mine" in project2.smart_bins
        assert "sb-mine" not in project.smart_bins  # 不可变
        with pytest.raises(ValueError):
            project2.add_smart_bin(sb)

    def test_remove_idempotent(self) -> None:
        project = _project_with_logged_assets().add_smart_bin(_smart_bin("mine"))
        removed = project.remove_smart_bin("sb-mine")
        assert "sb-mine" not in removed.smart_bins
        assert removed.remove_smart_bin("sb-mine") is removed  # 幂等

    def test_rename(self) -> None:
        project = _project_with_logged_assets().add_smart_bin(
            _smart_bin("mine", keywords=["x"]))
        renamed = project.rename_smart_bin("sb-mine", "精选")
        assert renamed.smart_bins["sb-mine"].name == "精选"
        assert renamed.smart_bins["sb-mine"].filter_params == {"keywords": ["x"]}
        with pytest.raises(ValueError):
            renamed.rename_smart_bin("ghost", "x")

    def test_smart_bins_survive_other_project_ops(self) -> None:
        """回归：既有工程操作不得丢 smart_bins（构造点接线）。"""
        project = (
            _project_with_logged_assets()
            .add_smart_bin(_smart_bin("mine"))
            .create_bin("B1")
        )
        project = project.update_asset_log_info("a1", rating=5)
        project = relink_asset(project, "a1", "/tmp/moved.mov")
        assert "sb-mine" in project.smart_bins

        undone_log = UpdateAssetLogCommand.create("a1", rating=1).apply(project)
        assert "sb-mine" in undone_log.smart_bins


# ================================================================
# 命令
# ================================================================

class TestSmartBinCommands:
    def test_create_command_undo_redo(self) -> None:
        project = _project_with_logged_assets()
        cmd = CreateSmartBinCommand.create(_smart_bin("mine", keywords=["k"]))
        applied = cmd.apply(project)
        assert "sb-mine" in applied.smart_bins
        undone = cmd.undo(applied)
        assert "sb-mine" not in undone.smart_bins
        redone = cmd.apply(undone)
        assert "sb-mine" in redone.smart_bins

    def test_delete_command_undo_restores(self) -> None:
        project = _project_with_logged_assets().add_smart_bin(
            _smart_bin("mine", min_rating=4))
        cmd = DeleteSmartBinCommand.create("sb-mine")
        deleted = cmd.apply(project)
        assert "sb-mine" not in deleted.smart_bins
        restored = cmd.undo(deleted)
        assert restored.smart_bins["sb-mine"].filter_params == {"min_rating": 4}

    def test_delete_missing_raises(self) -> None:
        cmd = DeleteSmartBinCommand.create("ghost")
        with pytest.raises(ValueError):
            cmd.apply(_project_with_logged_assets())


# ================================================================
# 持久化
# ================================================================

class TestSmartBinPersistence:
    def test_serializer_round_trip(self) -> None:
        project = (
            _project_with_logged_assets()
            .add_smart_bin(_smart_bin("精选", keywords=["interview"], min_rating=3))
            .add_smart_bin(_smart_bin("离线", offline=True))
        )
        json_str = EditProjectSerializer.to_json(project)
        restored = EditProjectSerializer.from_json(json_str)
        assert set(restored.smart_bins) == {"sb-精选", "sb-离线"}
        params = restored.smart_bins["sb-精选"].filter_params
        assert params["keywords"] == ["interview"]
        assert params["min_rating"] == 3
        assert restored.smart_bins["sb-离线"].filter_params == {"offline": True}

    def test_project_without_smart_bins_loads(self) -> None:
        project = _project_with_logged_assets()
        restored = EditProjectSerializer.from_json(EditProjectSerializer.to_json(project))
        assert restored.smart_bins == {}

    def test_module_payload_round_trip(self) -> None:
        """edit/smart_bins.json 模块数据 → EditProject 加载端解析。"""
        project = _project_with_logged_assets().add_smart_bin(
            _smart_bin("kw", keywords=["broll"], flag="reject"))
        module_data = {
            bin_id: EditProjectSerializer._smart_bin_to_dict(sb)
            for bin_id, sb in project.smart_bins.items()
        }
        parsed = {
            bin_id: EditProjectSerializer._dict_to_smart_bin(d)
            for bin_id, d in module_data.items()
        }
        assert parsed["sb-kw"].name == "kw"
        assert parsed["sb-kw"].filter_type == "custom"
        assert parsed["sb-kw"].filter_params == {
            "keywords": ["broll"], "flag": "reject",
        }
