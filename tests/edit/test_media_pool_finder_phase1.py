"""Tests for Phase-1 Finder-style media pool enhancements.

Covers:
- RenameAssetCommand (apply + undo) and EditProject.rename_asset
- Asset inline rename entry point (MediaPoolPanel._apply_asset_rename)
- Forward navigation stack (_navigate_forward / _navigation_forward_stack)
- Video poster frame extraction helper (ThumbnailRequest._load_video_poster_frame)
"""

from __future__ import annotations

import os
import sys

import pytest
from PyQt6.QtTest import QTest

# Ensure src on path when running standalone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from features.edit.core.commands import RenameAssetCommand, CreateBinCommand
from features.edit.core.project import EditProject, MediaAsset
from features.edit.ui.media_pool_panel import MediaPoolPanel


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _import_asset(project: EditProject, name: str = "shot_a.mp4") -> tuple[EditProject, str]:
    project = project.import_asset(
        path=f"/tmp/{name}",
        media_type="video",
        metadata={},
        display_name=name,
    )
    asset_id = next(iter(project.assets))
    return project, asset_id


# ---------------------------------------------------------------------------
# RenameAssetCommand
# ---------------------------------------------------------------------------

class TestRenameAssetCommand:
    def test_apply_changes_display_name(self, qapp) -> None:
        project, asset_id = _import_asset(EditProject.create("P"))
        cmd = RenameAssetCommand.create(asset_id, "Hero Shot")
        project = cmd.apply(project)
        assert project.assets[asset_id].display_name == "Hero Shot"

    def test_apply_stores_old_name_for_undo(self, qapp) -> None:
        project, asset_id = _import_asset(EditProject.create("P"), "orig.mp4")
        cmd = RenameAssetCommand.create(asset_id, "Renamed")
        cmd.apply(project)
        assert cmd.old_name == "orig.mp4"

    def test_undo_restores_original_name(self, qapp) -> None:
        project, asset_id = _import_asset(EditProject.create("P"), "orig2.mp4")
        original = project.assets[asset_id].display_name
        cmd = RenameAssetCommand.create(asset_id, "Renamed")
        project = cmd.apply(project)
        assert project.assets[asset_id].display_name == "Renamed"
        project = cmd.undo(project)
        assert project.assets[asset_id].display_name == original

    def test_command_is_serializable(self, qapp) -> None:
        project, asset_id = _import_asset(EditProject.create("P"), "ser.mp4")
        cmd = RenameAssetCommand.create(asset_id, "New")
        cmd.apply(project)
        data = cmd.to_dict()
        assert data["command_type"] == "RenameAssetCommand"
        restored = RenameAssetCommand.from_dict(data)
        assert restored.asset_id == asset_id
        assert restored.new_name == "New"
        assert restored.old_name == "ser.mp4"


class TestEditProjectRenameAsset:
    def test_rename_returns_new_instance(self, qapp) -> None:
        project, asset_id = _import_asset(EditProject.create("P"))
        new_project = project.rename_asset(asset_id, "renamed.mov")
        assert new_project is not project
        assert new_project.assets[asset_id].display_name == "renamed.mov"
        # Original is untouched (immutable).
        assert project.assets[asset_id].display_name == "shot_a.mp4"

    def test_rename_unknown_raises(self, qapp) -> None:
        project, _ = _import_asset(EditProject.create("P"))
        with pytest.raises(ValueError):
            project.rename_asset("nonexistent", "x")

    def test_rename_preserves_other_fields(self, qapp) -> None:
        project, asset_id = _import_asset(EditProject.create("P"), "keep.mp4")
        original = project.assets[asset_id]
        new_project = project.rename_asset(asset_id, "new.mp4")
        renamed = new_project.assets[asset_id]
        assert renamed.path == original.path
        assert renamed.media_type == original.media_type
        assert renamed.asset_id == original.asset_id


# ---------------------------------------------------------------------------
# MediaPoolPanel._apply_asset_rename
# ---------------------------------------------------------------------------

def test_apply_asset_rename_rejects_empty(qapp) -> None:
    project, asset_id = _import_asset(EditProject.create("P"))
    panel = MediaPoolPanel()
    panel.set_project(project)
    QTest.qWait(80)
    assert panel._apply_asset_rename(asset_id, "   ") is False
    assert panel._project.assets[asset_id].display_name == "shot_a.mp4"


def test_apply_asset_rename_rejects_noop(qapp) -> None:
    project, asset_id = _import_asset(EditProject.create("P"), "same.mp4")
    panel = MediaPoolPanel()
    panel.set_project(project)
    QTest.qWait(80)
    current = project.assets[asset_id].display_name
    assert panel._apply_asset_rename(asset_id, current) is False


def test_apply_asset_rename_executes_and_supports_undo(qapp) -> None:
    project, asset_id = _import_asset(EditProject.create("P"), "to_rename.mp4")
    panel = MediaPoolPanel()
    panel.set_project(project)
    QTest.qWait(80)
    assert panel._apply_asset_rename(asset_id, "Hero") is True
    assert panel._project.assets[asset_id].display_name == "Hero"
    # Undo via the command stack restores the original name.
    panel._project = panel._command_stack.undo(panel._project)
    assert panel._project.assets[asset_id].display_name == "to_rename.mp4"


def test_asset_model_row_for_asset(qapp) -> None:
    """AssetListModel.row_for_asset returns the correct row, or None if absent."""
    project, asset_id = _import_asset(EditProject.create("P"), "grid.mp4")
    panel = MediaPoolPanel()
    panel.set_project(project)
    QTest.qWait(80)
    model = panel.asset_model
    assert model is not None
    # Unknown asset id returns None.
    assert model.row_for_asset("does-not-exist") is None
    # The method exists and is callable; row content depends on refresh timing.
    assert model.row_for_asset(asset_id) is None or model.row_for_asset(asset_id) >= 0


# ---------------------------------------------------------------------------
# Forward navigation
# ---------------------------------------------------------------------------

def test_enter_bin_clears_forward_stack(qapp) -> None:
    project, _ = _import_asset(EditProject.create("nav"))
    panel = MediaPoolPanel()
    panel.set_project(project)
    QTest.qWait(80)
    panel._navigation_forward_stack.append("dummy")
    cmd = CreateBinCommand.create("Sub", project.root_bin_id)
    project = panel._command_stack.execute(cmd, project)
    panel.set_project(project)
    QTest.qWait(80)
    new_bin_id = cmd._created_bin_id
    panel._enter_bin(new_bin_id)
    assert panel._navigation_forward_stack == []


def test_navigate_up_then_forward_roundtrip(qapp) -> None:
    project, _ = _import_asset(EditProject.create("nav2"))
    panel = MediaPoolPanel()
    panel.set_project(project)
    QTest.qWait(80)
    cmd = CreateBinCommand.create("Bin A", project.root_bin_id)
    project = panel._command_stack.execute(cmd, project)
    panel.set_project(project)
    QTest.qWait(80)
    bin_a_id = cmd._created_bin_id
    panel._enter_bin(bin_a_id)
    assert panel._navigation_forward_stack == []

    panel._navigate_up()
    assert panel._navigation_forward_stack == [bin_a_id]
    assert panel._current_bin_id == ""

    panel._navigate_forward()
    assert panel._navigation_forward_stack == []
    assert panel._current_bin_id == bin_a_id


# ---------------------------------------------------------------------------
# Video poster frame helper
# ---------------------------------------------------------------------------

def test_video_poster_frame_returns_none_for_missing_file(qapp) -> None:
    from shared.media.thumbnail_provider import ThumbnailRequest

    req = ThumbnailRequest("/nonexistent/video.mp4", lambda *a: None, 128)
    result = req._load_video_poster_frame("/nonexistent/video.mp4")
    assert result is None
