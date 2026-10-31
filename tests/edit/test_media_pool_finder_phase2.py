"""Tests for Phase-2 drag/clipboard enhancements.

Covers:
- MoveBinCommand (reparent) + cycle detection in EditProject.reparent_bin
- RemoveAssetFromBinCommand / RemoveTimelineFromBinCommand
- DuplicateAssetCommand + EditProject.duplicate_asset
- Bin drag MIME builders (build_bin_drag_mime / parse_bin_drag_mime)
- _is_internal_media_pool_drag recognises bin MIME
"""

from __future__ import annotations

import os
import sys

import pytest
from PyQt6.QtTest import QTest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from features.edit.core.commands import (
    CreateBinCommand,
    DuplicateAssetCommand,
    MoveBinCommand,
    MoveAssetToBinCommand,
    RemoveAssetFromBinCommand,
)
from features.edit.core.project import EditProject
from features.edit.ui.media_pool_panel import (
    MediaPoolPanel,
    build_bin_drag_mime,
    parse_bin_drag_mime,
    _is_internal_media_pool_drag,
)
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.scene_store import get_shared_scene_store


def _import(project: EditProject, name: str = "a.mp4") -> tuple[EditProject, str]:
    project = project.import_asset(
        path=f"/tmp/{name}", media_type="video", metadata={}, display_name=name
    )
    return project, next(iter(project.assets))


# ---------------------------------------------------------------------------
# MoveBinCommand / reparent
# ---------------------------------------------------------------------------

class TestMoveBin:
    def test_reparent_changes_parent(self, qapp) -> None:
        project = EditProject.create("P")
        cmd_parent = CreateBinCommand.create("Parent", project.root_bin_id)
        project = cmd_parent.apply(project)
        cmd_child = CreateBinCommand.create("Child", project.root_bin_id)
        project = cmd_child.apply(project)
        parent_id = cmd_parent._created_bin_id
        child_id = cmd_child._created_bin_id

        assert project.bins[child_id].parent_id == project.root_bin_id
        move = MoveBinCommand.create(child_id, parent_id)
        project = move.apply(project)
        assert project.bins[child_id].parent_id == parent_id

    def test_cycle_detection_self(self, qapp) -> None:
        project = EditProject.create("P")
        cmd = CreateBinCommand.create("B", project.root_bin_id)
        project = cmd.apply(project)
        bid = cmd._created_bin_id
        with pytest.raises(ValueError):
            project.reparent_bin(bid, bid)

    def test_cycle_detection_descendant(self, qapp) -> None:
        project = EditProject.create("P")
        cmd_a = CreateBinCommand.create("A", project.root_bin_id)
        project = cmd_a.apply(project)
        a_id = cmd_a._created_bin_id
        cmd_b = CreateBinCommand.create("B", a_id)
        project = cmd_b.apply(project)
        b_id = cmd_b._created_bin_id
        # Moving A into B (its own child) must raise.
        with pytest.raises(ValueError):
            project.reparent_bin(a_id, b_id)

    def test_move_bin_undo(self, qapp) -> None:
        project = EditProject.create("P")
        cmd_parent = CreateBinCommand.create("P2", project.root_bin_id)
        project = cmd_parent.apply(project)
        cmd_child = CreateBinCommand.create("C", project.root_bin_id)
        project = cmd_child.apply(project)
        parent_id = cmd_parent._created_bin_id
        child_id = cmd_child._created_bin_id
        move = MoveBinCommand.create(child_id, parent_id)
        project = move.apply(project)
        assert project.bins[child_id].parent_id == parent_id
        project = move.undo(project)
        assert project.bins[child_id].parent_id == project.root_bin_id


# ---------------------------------------------------------------------------
# Remove from bin
# ---------------------------------------------------------------------------

class TestRemoveFromBin:
    def test_remove_asset_from_bin(self, qapp) -> None:
        project, asset_id = _import(EditProject.create("P"))
        cmd_bin = CreateBinCommand.create("Bin", project.root_bin_id)
        project = cmd_bin.apply(project)
        bin_id = cmd_bin._created_bin_id
        project = MoveAssetToBinCommand.create(asset_id, bin_id).apply(project)
        assert asset_id in project.bins[bin_id].asset_ids

        project = RemoveAssetFromBinCommand.create(asset_id, bin_id).apply(project)
        assert asset_id not in project.bins[bin_id].asset_ids
        # Asset still exists in project.
        assert asset_id in project.assets

    def test_remove_undo_restores_reference(self, qapp) -> None:
        project, asset_id = _import(EditProject.create("P"), "r.mp4")
        cmd_bin = CreateBinCommand.create("Bin", project.root_bin_id)
        project = cmd_bin.apply(project)
        bin_id = cmd_bin._created_bin_id
        project = MoveAssetToBinCommand.create(asset_id, bin_id).apply(project)
        cmd = RemoveAssetFromBinCommand.create(asset_id, bin_id)
        project = cmd.apply(project)
        assert asset_id not in project.bins[bin_id].asset_ids
        project = cmd.undo(project)
        assert asset_id in project.bins[bin_id].asset_ids


# ---------------------------------------------------------------------------
# Duplicate asset
# ---------------------------------------------------------------------------

class TestDuplicateAsset:
    def test_duplicate_creates_new_id_sharing_path(self, qapp) -> None:
        project, asset_id = _import(EditProject.create("P"), "dup.mp4")
        original = project.assets[asset_id]
        new_project, new_id = project.duplicate_asset(asset_id)
        assert new_id != asset_id
        new_asset = new_project.assets[new_id]
        assert new_asset.path == original.path
        assert new_asset.media_type == original.media_type
        assert new_asset.asset_id != original.asset_id

    def test_duplicate_command_undo_removes_copy(self, qapp) -> None:
        project, asset_id = _import(EditProject.create("P"), "dcmd.mp4")
        cmd = DuplicateAssetCommand.create(asset_id, project.root_bin_id)
        project = cmd.apply(project)
        new_id = cmd.created_asset_id
        assert new_id is not None
        assert new_id in project.assets
        project = cmd.undo(project)
        assert new_id not in project.assets

    def test_motion_graphic_duplicate_gets_independent_scene_and_numbered_name(self, qapp) -> None:
        scene = TeachingSceneDocument.create_default("Motion Graphic Clip")
        store = get_shared_scene_store()
        store.register(scene)
        project, asset_id = EditProject.create("MG").create_motion_graphic_asset(scene)

        copied_project, copied_id = project.duplicate_asset(asset_id)
        original = copied_project.assets[asset_id]
        copied = copied_project.assets[copied_id]

        assert copied_id != asset_id
        assert copied.media_type == "motion_graphic"
        assert copied.metadata["scene_id"] != original.metadata["scene_id"]
        assert copied.display_name == "Motion Graphic Clip 2"
        assert store.get(copied.metadata["scene_id"]) is not None


# ---------------------------------------------------------------------------
# Bin drag MIME helpers
# ---------------------------------------------------------------------------

class TestBinDragMime:
    def test_build_and_parse_roundtrip(self, qapp) -> None:
        mime = build_bin_drag_mime(["bin-1", "bin-2"])
        assert _is_internal_media_pool_drag(mime) is True
        parsed = parse_bin_drag_mime(mime)
        assert parsed == ["bin-1", "bin-2"]

    def test_parse_empty_returns_empty(self, qapp) -> None:
        from PyQt6.QtCore import QMimeData

        assert parse_bin_drag_mime(QMimeData()) == []

    def test_dedup_in_build(self, qapp) -> None:
        mime = build_bin_drag_mime(["x", "x", "y"])
        assert parse_bin_drag_mime(mime) == ["x", "y"]


# ---------------------------------------------------------------------------
# Panel: _handle_drop_bin_into_bin (logic via command stack)
# ---------------------------------------------------------------------------

class TestPanelBinReparent:
    def test_handle_drop_bin_into_bin(self, qapp) -> None:
        project = EditProject.create("P")
        _import(project, "x.mp4")
        panel = MediaPoolPanel()
        panel.set_project(project)
        QTest.qWait(80)
        # Create two bins.
        cmd_a = CreateBinCommand.create("A", panel._project.root_bin_id)
        panel._project = panel._command_stack.execute(cmd_a, panel._project)
        QTest.qWait(40)
        a_id = cmd_a._created_bin_id
        cmd_b = CreateBinCommand.create("B", panel._project.root_bin_id)
        panel._project = panel._command_stack.execute(cmd_b, panel._project)
        QTest.qWait(40)
        b_id = cmd_b._created_bin_id
        # Drop B into A.
        assert panel._handle_drop_bin_into_bin(a_id, [b_id]) is True
        assert panel._project.bins[b_id].parent_id == a_id

    def test_drop_bin_into_itself_rejected(self, qapp) -> None:
        project = EditProject.create("P")
        panel = MediaPoolPanel()
        panel.set_project(project)
        QTest.qWait(80)
        cmd = CreateBinCommand.create("Solo", panel._project.root_bin_id)
        panel._project = panel._command_stack.execute(cmd, panel._project)
        QTest.qWait(40)
        bid = cmd._created_bin_id
        # Dropping a bin into itself is a no-op (returns False).
        assert panel._handle_drop_bin_into_bin(bid, [bid]) is False


# ---------------------------------------------------------------------------
# Panel: clipboard duplicate
# ---------------------------------------------------------------------------

def test_panel_duplicate_selected(qapp) -> None:
    project, asset_id = _import(EditProject.create("P"), "sel.mp4")
    panel = MediaPoolPanel()
    panel.set_project(project)
    QTest.qWait(80)
    before = len(panel._project.assets)
    # Duplicate operates on selection; directly duplicate the known id.
    panel._clipboard_asset_ids = [asset_id]
    panel._clipboard_is_cut = False
    panel._paste_assets()
    QTest.qWait(40)
    assert len(panel._project.assets) == before + 1
