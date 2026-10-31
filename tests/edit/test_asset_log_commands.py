"""
P1-12 素材日志命令测试。

覆盖：
- UpdateAssetLogCommand：apply/undo（快照）、redo（重复 apply）、部分更新、
  未知素材报错、to_dict/from_dict round-trip
- BatchUpdateAssetLogCommand：关键词合并语义（追加去重/移除）、批量评级、
  单次 undo 整体回滚、缺失素材跳过、apply→undo→apply 循环快照正确性
- ProjectCommandStack 集成：批量日志编辑可一次 undo 全部回滚
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.commands import (
    BatchUpdateAssetLogCommand,
    ProjectCommandStack,
    UpdateAssetLogCommand,
)


def _asset(asset_id: str, **overrides) -> MediaAsset:
    params = dict(
        asset_id=asset_id,
        path=f"/tmp/{asset_id}.mov",
        media_type="video",
        display_name=f"{asset_id}.mov",
        metadata={},
    )
    params.update(overrides)
    return MediaAsset(**params)


@pytest.fixture
def project() -> EditProject:
    return EditProject(
        project_id="p1",
        name="proj",
        assets={
            "a1": _asset("a1", keywords=("raw",), rating=2),
            "a2": _asset("a2", keywords=("raw", "interview")),
            "a3": _asset("a3"),
        },
        root_bin_id="root",
    )


# ================================================================
# UpdateAssetLogCommand
# ================================================================

class TestUpdateAssetLogCommand:
    def test_apply_sets_fields(self, project: EditProject) -> None:
        cmd = UpdateAssetLogCommand.create(
            "a1", keywords=("dailies",), rating=5, flag="reject", comment="c"
        )
        result = cmd.apply(project)
        asset = result.assets["a1"]
        assert asset.keywords == ("dailies",)
        assert asset.rating == 5
        assert asset.flag == "reject"
        assert asset.comment == "c"

    def test_none_keeps_current_values(self, project: EditProject) -> None:
        cmd = UpdateAssetLogCommand.create("a1", rating=4)
        result = cmd.apply(project)
        asset = result.assets["a1"]
        assert asset.rating == 4
        assert asset.keywords == ("raw",)  # 未指定 → 保留

    def test_undo_restores_snapshot(self, project: EditProject) -> None:
        cmd = UpdateAssetLogCommand.create("a1", keywords=("x",), rating=5)
        applied = cmd.apply(project)
        undone = cmd.undo(applied)
        assert undone.assets["a1"].keywords == ("raw",)
        assert undone.assets["a1"].rating == 2

    def test_redo_keeps_original_snapshot(self, project: EditProject) -> None:
        cmd = UpdateAssetLogCommand.create("a1", rating=5)
        applied = cmd.apply(project)
        undone = cmd.undo(applied)
        reapplied = cmd.apply(undone)
        assert reapplied.assets["a1"].rating == 5
        undone2 = cmd.undo(reapplied)
        assert undone2.assets["a1"].rating == 2

    def test_apply_unknown_asset_raises(self, project: EditProject) -> None:
        cmd = UpdateAssetLogCommand.create("ghost", rating=1)
        with pytest.raises(ValueError):
            cmd.apply(project)

    def test_dict_round_trip(self) -> None:
        cmd = UpdateAssetLogCommand.create(
            "a1", keywords=("k",), rating=3, flag="reject",
            color_label="blue", comment="note",
        )
        restored = UpdateAssetLogCommand.from_dict(cmd.to_dict())
        assert restored.asset_id == "a1"
        assert restored.new_keywords == ("k",)
        assert restored.new_rating == 3
        assert restored.new_flag == "reject"
        assert restored.new_color_label == "blue"
        assert restored.new_comment == "note"


# ================================================================
# BatchUpdateAssetLogCommand
# ================================================================

class TestBatchUpdateAssetLogCommand:
    def test_add_keywords_merges_without_duplicates(self, project: EditProject) -> None:
        cmd = BatchUpdateAssetLogCommand.create(
            ("a1", "a2", "a3"), add_keywords=("selects", "raw")
        )
        result = cmd.apply(project)
        assert result.assets["a1"].keywords == ("raw", "selects")
        assert result.assets["a2"].keywords == ("raw", "interview", "selects")
        assert result.assets["a3"].keywords == ("selects", "raw")

    def test_remove_keywords(self, project: EditProject) -> None:
        cmd = BatchUpdateAssetLogCommand.create(("a1", "a2"), remove_keywords=("raw",))
        result = cmd.apply(project)
        assert result.assets["a1"].keywords == ()
        assert result.assets["a2"].keywords == ("interview",)

    def test_overwrite_rating_and_flag(self, project: EditProject) -> None:
        cmd = BatchUpdateAssetLogCommand.create(
            ("a1", "a2"), rating=5, flag="reject", color_label="red"
        )
        result = cmd.apply(project)
        for asset_id in ("a1", "a2"):
            asset = result.assets[asset_id]
            assert asset.rating == 5
            assert asset.flag == "reject"
            assert asset.color_label == "red"
        # 未包含在批次内的素材不受影响
        assert result.assets["a3"].rating == 0

    def test_single_undo_reverts_all(self, project: EditProject) -> None:
        cmd = BatchUpdateAssetLogCommand.create(
            ("a1", "a2"), add_keywords=("sel",), rating=4
        )
        applied = cmd.apply(project)
        undone = cmd.undo(applied)
        assert undone.assets["a1"].keywords == ("raw",)
        assert undone.assets["a1"].rating == 2
        assert undone.assets["a2"].keywords == ("raw", "interview")
        assert undone.assets["a2"].rating == 0

    def test_missing_assets_skipped(self, project: EditProject) -> None:
        cmd = BatchUpdateAssetLogCommand.create(("a1", "ghost"), rating=5)
        result = cmd.apply(project)
        assert result.assets["a1"].rating == 5
        assert "ghost" not in result.assets

    def test_apply_undo_apply_cycle(self, project: EditProject) -> None:
        cmd = BatchUpdateAssetLogCommand.create(("a1",), add_keywords=("k",))
        applied = cmd.apply(project)
        undone = cmd.undo(applied)
        assert undone.assets["a1"].keywords == ("raw",)
        reapplied = cmd.apply(undone)
        assert reapplied.assets["a1"].keywords == ("raw", "k")
        undone2 = cmd.undo(reapplied)
        assert undone2.assets["a1"].keywords == ("raw",)

    def test_empty_operations_noop(self, project: EditProject) -> None:
        cmd = BatchUpdateAssetLogCommand.create(("a1", "a2"))
        result = cmd.apply(project)
        assert result.assets["a1"].keywords == ("raw",)
        assert result.assets["a2"].rating == 0

    def test_dict_round_trip(self) -> None:
        cmd = BatchUpdateAssetLogCommand.create(
            ("a1", "a2"), add_keywords=("k",), rating=3, flag="reject"
        )
        restored = BatchUpdateAssetLogCommand.from_dict(cmd.to_dict())
        assert restored.asset_ids == ("a1", "a2")
        assert restored.add_keywords == ("k",)
        assert restored.rating == 3
        assert restored.flag == "reject"

    def test_single_asset_id_string_accepted(self, project: EditProject) -> None:
        cmd = BatchUpdateAssetLogCommand.create("a1", rating=5)
        result = cmd.apply(project)
        assert result.assets["a1"].rating == 5


# ================================================================
# ProjectCommandStack 集成
# ================================================================

class TestStackIntegration:
    def test_batch_edit_single_undo_on_stack(self, project: EditProject) -> None:
        stack = ProjectCommandStack()
        cmd = BatchUpdateAssetLogCommand.create(
            ("a1", "a2", "a3"), add_keywords=("approved",), rating=5
        )
        current = stack.execute(cmd, project)
        for asset_id in ("a1", "a2", "a3"):
            assert current.assets[asset_id].rating == 5
            assert "approved" in current.assets[asset_id].keywords

        undone = stack.undo(current)
        assert undone is not None
        assert undone.assets["a1"].rating == 2
        assert "approved" not in undone.assets["a1"].keywords

        redone = stack.redo(undone)
        assert redone is not None
        assert redone.assets["a3"].rating == 5
