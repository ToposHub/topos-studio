"""Resolve-style Master navigation and nested Bins sidebar contracts."""

from __future__ import annotations

import os
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from features.edit.core.project import EditProject
from features.edit.ui.media_pool_panel import BinTreeWidget, MediaPoolPanel


def _make_panel(qapp, with_folders: bool = True) -> tuple[MediaPoolPanel, EditProject]:
    project = EditProject.create("BinTree")
    if with_folders:
        project = project.create_bin("FolderA", project.root_bin_id)
        project = project.create_bin("FolderB", project.root_bin_id)
        a_id = next(bid for bid, b in project.bins.items() if b.name == "FolderA")
        project = project.create_bin("Nested", a_id)
    panel = MediaPoolPanel()
    panel.set_project(project)
    QTest.qWait(150)
    return panel, project


def _walk(item, depth: int = 0):
    """Yield (depth, bin_type, bin_id, display_text) for item and descendants."""
    yield (
        depth,
        item.data(0, BinTreeWidget.BIN_TYPE_ROLE),
        item.data(0, BinTreeWidget.BIN_ID_ROLE),
        item.data(0, Qt.ItemDataRole.DisplayRole) or item.text(0),
    )
    for c in range(item.childCount()):
        yield from _walk(item.child(c), depth + 1)


class TestRootAsTopLevel:
    def test_no_bins_section_header(self, qapp) -> None:
        """The old 'Bins' section header must be gone."""
        panel, _ = _make_panel(qapp)
        bt = panel.bin_tree
        for i in range(bt.topLevelItemCount()):
            top = bt.topLevelItem(i)
            assert not (
                top.data(0, BinTreeWidget.BIN_TYPE_ROLE) == "section"
                and top.data(0, BinTreeWidget.BIN_ID_ROLE) == "__bins__"
            ), "the 'Bins' section header should no longer exist"

    def test_master_bin_is_visible(self, qapp) -> None:
        """Master exposes the existing project root without changing its data."""
        panel, project = _make_panel(qapp)
        bt = panel.bin_tree
        all_ids = set()
        for i in range(bt.topLevelItemCount()):
            for _d, _btype, bid, _txt in _walk(bt.topLevelItem(i)):
                if bid:
                    all_ids.add(bid)
        assert project.root_bin_id in all_ids, (
            "Master should be rendered as a sidebar navigation target"
        )

    def test_folders_are_children_of_master(self, qapp) -> None:
        """Top-level folders appear immediately below Master."""
        panel, project = _make_panel(qapp)
        bt = panel.bin_tree
        master = bt._find_bin_item(project.root_bin_id)
        assert master is not None
        assert master.text(0) == "Master"
        assert not master.flags() & Qt.ItemFlag.ItemIsEditable
        assert not master.flags() & Qt.ItemFlag.ItemIsDragEnabled
        names = [master.child(i).text(0) for i in range(master.childCount())]
        assert names == ["FolderA", "FolderB"]

    def test_nested_folder_under_parent(self, qapp) -> None:
        """A deeply nested folder is still nested under its parent, not Root."""
        panel, project = _make_panel(qapp)
        bt = panel.bin_tree
        folder_a_id = next(bid for bid, b in project.bins.items() if b.name == "FolderA")
        folder_a_item = bt._find_bin_item(folder_a_id)
        assert folder_a_item is not None
        child_names = [
            folder_a_item.child(c).data(0, Qt.ItemDataRole.DisplayRole)
            for c in range(folder_a_item.childCount())
        ]
        assert any("Nested" in (n or "") for n in child_names)

    def test_select_master_bin_returns_true(self, qapp) -> None:
        """Selecting Master restores root navigation without emitting twice."""
        panel, project = _make_panel(qapp)
        bt = panel.bin_tree
        ok = bt.select_bin(project.root_bin_id, is_smart_bin=False)
        assert ok is True

    def test_select_nested_bin_restores(self, qapp) -> None:
        """select_bin still finds deeply nested bins."""
        panel, project = _make_panel(qapp)
        nested_id = next(
            bid for bid, b in project.bins.items() if b.name == "Nested"
        )
        bt = panel.bin_tree
        ok = bt.select_bin(nested_id, is_smart_bin=False)
        assert ok is True
        current = bt.currentItem()
        assert current is not None
        assert current.data(0, BinTreeWidget.BIN_ID_ROLE) == nested_id

    def test_smart_bins_still_present(self, qapp) -> None:
        """Smart Bins section + its entries are unaffected."""
        panel, _ = _make_panel(qapp)
        bt = panel.bin_tree
        types = set()
        for i in range(bt.topLevelItemCount()):
            top = bt.topLevelItem(i)
            for _d, btype, _bid, _txt in _walk(top):
                types.add(btype)
        assert "section" in types  # Smart Bins section
        assert "smart_bin" in types
