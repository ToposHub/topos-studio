"""Tests for the Bins sidebar hiding the implicit Root bin.

The project's root bin is an implicit container (like Finder's volume
root or DaVinci's Master Bin) and is NOT shown as a node in the sidebar.
User-created folders whose parent is the root bin are promoted to the
top level; deeper folders nest under their parents as usual. Smart Bins
are still appended below the bin tree.
"""

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

    def test_root_bin_is_hidden(self, qapp) -> None:
        """The project root bin must NOT appear in the sidebar (implicit)."""
        panel, project = _make_panel(qapp)
        bt = panel.bin_tree
        all_ids = set()
        for i in range(bt.topLevelItemCount()):
            for _d, _btype, bid, _txt in _walk(bt.topLevelItem(i)):
                if bid:
                    all_ids.add(bid)
        assert project.root_bin_id not in all_ids, (
            "the root bin should not be rendered as a sidebar node"
        )

    def test_folders_promoted_to_top_level(self, qapp) -> None:
        """User folders under Root appear at the top level (Root is implicit)."""
        panel, project = _make_panel(qapp)
        bt = panel.bin_tree
        top_names = []
        for i in range(bt.topLevelItemCount()):
            top = bt.topLevelItem(i)
            if top.data(0, BinTreeWidget.BIN_TYPE_ROLE) == "bin":
                txt = top.data(0, Qt.ItemDataRole.DisplayRole) or top.text(0)
                top_names.append(txt)
        # FolderA and FolderB are promoted to the top level (Nested is under A).
        assert any("FolderA" in (n or "") for n in top_names)
        assert any("FolderB" in (n or "") for n in top_names)

    def test_nested_folder_under_parent(self, qapp) -> None:
        """A deeply nested folder is still nested under its parent, not Root."""
        panel, project = _make_panel(qapp)
        bt = panel.bin_tree
        # Find FolderA among top-level bins.
        folder_a_item = None
        for i in range(bt.topLevelItemCount()):
            top = bt.topLevelItem(i)
            if (
                top.data(0, BinTreeWidget.BIN_TYPE_ROLE) == "bin"
                and "FolderA" in (top.data(0, Qt.ItemDataRole.DisplayRole) or "")
            ):
                folder_a_item = top
                break
        assert folder_a_item is not None
        child_names = [
            folder_a_item.child(c).data(0, Qt.ItemDataRole.DisplayRole)
            for c in range(folder_a_item.childCount())
        ]
        assert any("Nested" in (n or "") for n in child_names)

    def test_select_root_bin_returns_false(self, qapp) -> None:
        """select_bin cannot find the root bin because it's no longer rendered."""
        panel, project = _make_panel(qapp)
        bt = panel.bin_tree
        ok = bt.select_bin(project.root_bin_id, is_smart_bin=False)
        assert ok is False

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
