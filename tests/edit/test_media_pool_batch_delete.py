"""Regression tests for media-pool multi-selection deletion."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import (
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QPoint,
    QPointF,
    Qt,
)
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QMenu
from PyQt6.QtTest import QTest

from src.features.edit.core.project import EditProject
from src.features.edit.ui.media_pool_panel import MediaPoolPanel


def _make_panel(qapp: Any, count: int = 3) -> tuple[MediaPoolPanel, list[str]]:
    project = EditProject.create("Media pool batch delete")
    asset_ids: list[str] = []
    for index in range(count):
        project = project.import_asset(
            path=f"/tmp/batch-delete-{index}.mp4",
            media_type="video",
            metadata={},
            display_name=f"batch-delete-{index}.mp4",
        )
        asset_ids.append(next(asset_id for asset_id in project.assets if asset_id not in asset_ids))

    panel = MediaPoolPanel()
    panel.set_project(project)
    panel.resize(800, 600)
    panel.show()
    QTest.qWait(150)
    return panel, asset_ids


def _select_assets(panel: MediaPoolPanel, asset_ids: list[str]) -> None:
    assert panel.asset_model is not None
    assert panel.grid_view is not None
    selection = QItemSelection()
    for asset_id in asset_ids:
        row = panel.asset_model.row_for_asset(asset_id)
        assert row is not None
        index = panel.asset_model.index(row, 0)
        selection.select(index, index)
    panel.grid_view.selectionModel().select(
        selection,
        QItemSelectionModel.SelectionFlag.ClearAndSelect,
    )


def test_delete_key_removes_all_selected_grid_assets(qapp: Any) -> None:
    panel, asset_ids = _make_panel(qapp)
    _select_assets(panel, asset_ids)
    assert panel.grid_view is not None
    panel.grid_view.setFocus()

    QTest.keyClick(panel.grid_view, Qt.Key.Key_Delete)
    QTest.qWait(50)

    assert panel._project is not None
    assert panel._project.assets == {}
    panel.close()


def test_marquee_then_delete_key_removes_all_grid_assets(qapp: Any) -> None:
    panel, asset_ids = _make_panel(qapp)
    assert panel.grid_view is not None
    grid = panel.grid_view

    blank = next(
        (
            QPoint(x, y)
            for y in range(0, grid.viewport().height(), 8)
            for x in range(0, grid.viewport().width(), 8)
            if not grid.indexAt(QPoint(x, y)).isValid()
        ),
        None,
    )
    assert blank is not None
    grid.clearFocus()

    viewport = grid.viewport()

    # Send the events to the real viewport child, which is the same route used
    # by the live QListView.  Calling the view handler directly would miss
    # viewport focus/event forwarding bugs.
    QApplication.sendEvent(
        viewport,
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(blank),
            QPointF(blank),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    assert grid.hasFocus()

    end = QPoint(0, 0)
    QApplication.sendEvent(
        viewport,
        QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(end),
            QPointF(end),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    QApplication.sendEvent(
        viewport,
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(end),
            QPointF(end),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    QTest.qWait(20)

    selected_ids = {
        index.data(panel.asset_model.ITEM_ID_ROLE)
        for index in grid.selectedIndexes()
    }
    assert selected_ids == set(asset_ids)

    QTest.keyClick(viewport, Qt.Key.Key_Delete)
    QTest.qWait(50)
    assert panel._project is not None
    assert panel._project.assets == {}
    panel.close()


def test_context_target_keeps_existing_grid_multi_selection(qapp: Any) -> None:
    panel, asset_ids = _make_panel(qapp)
    _select_assets(panel, asset_ids)
    assert panel.asset_model is not None
    assert panel.grid_view is not None
    target = panel.asset_model.index(panel.asset_model.row_for_asset(asset_ids[0]), 0)

    panel._select_single_context_grid_item(target)

    selected_ids = {
        index.data(panel.asset_model.ITEM_ID_ROLE)
        for index in panel.grid_view.selectedIndexes()
    }
    assert selected_ids == set(asset_ids)
    panel.close()


def test_context_target_keeps_existing_list_multi_selection(qapp: Any) -> None:
    panel, asset_ids = _make_panel(qapp)
    tree = panel.tree
    for row in range(tree.topLevelItemCount()):
        tree.topLevelItem(row).setSelected(True)
    target = tree.topLevelItem(0)
    assert target is not None

    panel._select_single_context_tree_item(target)

    selected_ids = {
        item.data(0, Qt.ItemDataRole.UserRole)
        for item in tree.selectedItems()
    }
    assert selected_ids == set(asset_ids)
    panel.close()


def test_grid_context_menu_offers_batch_delete(qapp: Any, monkeypatch: Any) -> None:
    panel, asset_ids = _make_panel(qapp)
    _select_assets(panel, asset_ids)
    assert panel.asset_model is not None
    assert panel.grid_view is not None
    target = panel.asset_model.index(panel.asset_model.row_for_asset(asset_ids[0]), 0)

    labels: list[str] = []

    class MenuSpy(QMenu):
        def exec(self, *args: Any, **kwargs: Any) -> object | None:
            del args, kwargs
            labels.extend(action.text() for action in self.actions())
            self.actions()[0].trigger()
            return self.actions()[0]

    monkeypatch.setattr("src.features.edit.ui.media_pool_panel.QMenu", MenuSpy)
    QApplication.sendEvent(
        panel.grid_view.viewport(),
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            QPointF(panel.grid_view.visualRect(target).center()),
            QPointF(panel.grid_view.visualRect(target).center()),
            Qt.MouseButton.RightButton,
            Qt.MouseButton.RightButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    panel._on_grid_context_menu(panel.grid_view.visualRect(target).center())

    assert labels
    assert "3" in labels[0]
    QTest.qWait(50)
    assert panel._project is not None
    assert panel._project.assets == {}
    panel.close()
