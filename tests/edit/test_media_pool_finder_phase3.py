"""Tests for Phase-3 view/metadata enhancements.

Covers:
- UpdateAssetProductionInfoCommand + EditProject.update_asset_production_info
- Folder import filtering (_filter_valid_media_files expands directories)
- Quick Look dialog construction
- Bin properties dialog construction
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest
from PyQt6.QtTest import QTest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from features.edit.core.commands import (
    CreateBinCommand,
    UpdateAssetProductionInfoCommand,
)
from features.edit.core.project import EditProject, MediaAsset
from features.edit.ui.media_pool_panel import (
    AssetPropertiesDialog,
    BinPropertiesDialog,
    MediaPoolPanel,
    QuickLookDialog,
)


def _import(project: EditProject, name: str = "a.mp4") -> tuple[EditProject, str]:
    project = project.import_asset(
        path=f"/tmp/{name}", media_type="video", metadata={}, display_name=name
    )
    return project, next(iter(project.assets))


# ---------------------------------------------------------------------------
# Production info editing
# ---------------------------------------------------------------------------

class TestProductionInfo:
    def test_update_changes_fields(self, qapp) -> None:
        project, asset_id = _import(EditProject.create("P"), "p.mp4")
        new_project = project.update_asset_production_info(
            asset_id, reel="R1", scene="S1", shot="SH1", take="T1"
        )
        asset = new_project.assets[asset_id]
        assert asset.reel == "R1"
        assert asset.scene == "S1"
        assert asset.shot == "SH1"
        assert asset.take == "T1"

    def test_partial_update_preserves_others(self, qapp) -> None:
        project, asset_id = _import(EditProject.create("P"), "p2.mp4")
        project = project.update_asset_production_info(asset_id, reel="A", scene="B")
        project = project.update_asset_production_info(asset_id, shot="C")
        asset = project.assets[asset_id]
        assert asset.reel == "A"
        assert asset.scene == "B"
        assert asset.shot == "C"
        assert asset.take == ""  # untouched

    def test_command_undo(self, qapp) -> None:
        project, asset_id = _import(EditProject.create("P"), "undo.mp4")
        project = project.update_asset_production_info(asset_id, reel="orig")
        cmd = UpdateAssetProductionInfoCommand.create(
            asset_id, "new", "", "", ""
        )
        project = cmd.apply(project)
        assert project.assets[asset_id].reel == "new"
        project = cmd.undo(project)
        assert project.assets[asset_id].reel == "orig"


# ---------------------------------------------------------------------------
# Folder import filtering
# ---------------------------------------------------------------------------

class TestFolderImport:
    def test_filter_expands_directory(self, qapp, tmp_path) -> None:
        # Create a folder with two fake media files.
        (tmp_path / "clip1.mp4").write_bytes(b"x")
        (tmp_path / "clip2.mp4").write_bytes(b"x")
        (tmp_path / "readme.txt").write_bytes(b"x")  # non-media, ignored
        project = EditProject.create("P")
        _import(project, "seed.mp4")
        panel = MediaPoolPanel()
        panel.set_project(project)
        QTest.qWait(80)
        result = panel._filter_valid_media_files([str(tmp_path)])
        assert len(result) == 2
        assert all(p.endswith(".mp4") for p in result)

    def test_filter_recursive_subfolder(self, qapp, tmp_path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.png").write_bytes(b"x")
        project = EditProject.create("P")
        _import(project, "seed.mp4")
        panel = MediaPoolPanel()
        panel.set_project(project)
        QTest.qWait(80)
        result = panel._filter_valid_media_files([str(tmp_path)])
        assert any(p.endswith("deep.png") for p in result)


# ---------------------------------------------------------------------------
# Quick Look dialog
# ---------------------------------------------------------------------------

def test_quick_look_dialog_constructs(qapp) -> None:
    project, asset_id = _import(EditProject.create("P"), "ql.mp4")
    asset = project.assets[asset_id]
    dlg = QuickLookDialog(asset)
    assert dlg.windowTitle() == "ql.mp4"
    dlg.deleteLater()


# ---------------------------------------------------------------------------
# Bin properties dialog
# ---------------------------------------------------------------------------

def test_bin_properties_dialog_constructs(qapp) -> None:
    project = EditProject.create("P")
    project, asset_id = _import(project, "bp.mp4")
    cmd = CreateBinCommand.create("MyBin", project.root_bin_id)
    project = cmd.apply(project)
    project = project.move_asset_to_bin(asset_id, cmd._created_bin_id)
    bin_obj = project.bins[cmd._created_bin_id]
    dlg = BinPropertiesDialog(bin_obj, project)
    assert dlg.windowTitle()  # has a title
    dlg.deleteLater()


def test_asset_properties_dialog_has_editable_fields(qapp) -> None:
    project, asset_id = _import(EditProject.create("P"), "apd.mp4")
    asset = project.assets[asset_id]
    dlg = AssetPropertiesDialog(asset)
    reel, scene, shot, take = dlg.production_info_values()
    assert reel == ""
    assert scene == ""
    assert shot == ""
    assert take == ""
    dlg.deleteLater()


# ---------------------------------------------------------------------------
# Panel: production info save via properties dialog
# ---------------------------------------------------------------------------

def test_show_properties_saves_production_info(qapp) -> None:
    project, asset_id = _import(EditProject.create("P"), "save.mp4")
    panel = MediaPoolPanel()
    panel.set_project(project)
    QTest.qWait(80)
    asset = panel._project.assets[asset_id]
    # Simulate the user entering values and accepting (monkeypatch the dialog).
    orig = AssetPropertiesDialog

    class FakeDialog(orig):
        def exec(self):  # noqa: D401 - test stub
            return type(panel).DialogCode.Accepted if hasattr(type(panel), 'DialogCode') else 1

        def production_info_values(self):
            return ("R1", "S1", "SH1", "T1")

    import features.edit.ui.media_pool_panel as mod

    mod.AssetPropertiesDialog = FakeDialog
    try:
        panel._show_properties(asset)
        QTest.qWait(40)
        updated = panel._project.assets[asset_id]
        assert updated.reel == "R1"
        assert updated.scene == "S1"
    finally:
        mod.AssetPropertiesDialog = orig
