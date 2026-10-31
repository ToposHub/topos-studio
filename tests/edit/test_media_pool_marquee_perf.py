"""Tests for marquee/multi-select not triggering Source Viewer preview.

Regression guard for the rubber-band lag fix: selecting multiple assets
(or marquee-selecting) must NOT emit ``asset_selected`` (which drives a
full Source Viewer decode). Only a single-asset selection previews.
"""

from __future__ import annotations

import os
import sys

from PyQt6.QtCore import Qt, QItemSelection, QItemSelectionModel
from PyQt6.QtTest import QTest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from features.edit.core.project import EditProject
from features.edit.ui.media_pool_panel import MediaPoolPanel


def _make_panel(qapp, n_assets: int = 3) -> tuple[MediaPoolPanel, list[str]]:
    project = EditProject.create("Marquee")
    asset_ids: list[str] = []
    for i in range(n_assets):
        project = project.import_asset(
            path=f"/tmp/m{i}.mp4",
            media_type="video",
            metadata={},
            display_name=f"m{i}.mp4",
        )
        asset_ids.append(next(iter(project.assets)))
    panel = MediaPoolPanel()
    panel.set_project(project)
    QTest.qWait(150)
    return panel, asset_ids


# ---------------------------------------------------------------------------
# Grid view: multi-select suppresses preview
# ---------------------------------------------------------------------------

class TestGridMultiSelectNoPreview:
    def test_single_asset_selection_emits(self, qapp) -> None:
        panel, asset_ids = _make_panel(qapp, n_assets=3)
        emitted: list[str] = []
        panel.asset_selected.connect(lambda aid: emitted.append(aid))

        model = panel.asset_model
        grid = panel.grid_view
        assert model is not None and grid is not None
        row = model.row_for_asset(asset_ids[0])
        assert row is not None
        idx = model.index(row, 0)

        sel = QItemSelection(idx, idx)
        grid.selectionModel().select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        QTest.qWait(30)

        assert emitted == [asset_ids[0]]

    def test_multi_asset_selection_does_not_emit(self, qapp) -> None:
        panel, asset_ids = _make_panel(qapp, n_assets=3)
        emitted: list[str] = []
        panel.asset_selected.connect(lambda aid: emitted.append(aid))

        model = panel.asset_model
        grid = panel.grid_view
        assert model is not None and grid is not None
        r0 = model.row_for_asset(asset_ids[0])
        r1 = model.row_for_asset(asset_ids[1])
        assert r0 is not None and r1 is not None

        # Select two assets at once (simulates marquee / Ctrl-click).
        sel = QItemSelection()
        sel.select(model.index(r0, 0), model.index(r0, 0))
        sel.select(model.index(r1, 0), model.index(r1, 0))
        grid.selectionModel().select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        QTest.qWait(30)

        assert emitted == [], "multi-select must not trigger asset_selected"

    def test_marquee_then_single_restores_preview(self, qapp) -> None:
        """After a multi-select, narrowing back to one asset previews again."""
        panel, asset_ids = _make_panel(qapp, n_assets=3)
        emitted: list[str] = []
        panel.asset_selected.connect(lambda aid: emitted.append(aid))

        model = panel.asset_model
        grid = panel.grid_view
        assert model is not None and grid is not None
        r0 = model.row_for_asset(asset_ids[0])
        r1 = model.row_for_asset(asset_ids[1])
        assert r0 is not None and r1 is not None

        # Multi-select first (no emit).
        sel = QItemSelection()
        sel.select(model.index(r0, 0), model.index(r0, 0))
        sel.select(model.index(r1, 0), model.index(r1, 0))
        grid.selectionModel().select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        QTest.qWait(30)
        assert emitted == []

        # Narrow to a single asset → emit once.
        single = QItemSelection(model.index(r0, 0), model.index(r0, 0))
        grid.selectionModel().select(single, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        QTest.qWait(30)
        assert emitted == [asset_ids[0]]

    def test_bin_selection_does_not_emit(self, qapp) -> None:
        """Selecting a bin (not an asset) must not preview."""
        panel, asset_ids = _make_panel(qapp, n_assets=1)
        emitted: list[str] = []
        panel.asset_selected.connect(lambda aid: emitted.append(aid))

        model = panel.asset_model
        grid = panel.grid_view
        assert model is not None and grid is not None
        # Find a bin row (the model includes child bins at root level).
        bin_row = None
        for r in range(model.rowCount()):
            if model.data(model.index(r, 0), model.ITEM_TYPE_ROLE) == "bin":
                bin_row = r
                break
        if bin_row is None:
            return  # no bins in this fixture; nothing to assert
        idx = model.index(bin_row, 0)
        sel = QItemSelection(idx, idx)
        grid.selectionModel().select(sel, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        QTest.qWait(30)
        assert emitted == []
