"""Variable gallery geometry, model selection, and viewport virtualization."""
from __future__ import annotations

import time

import pytest
from pytestqt.qtbot import QtBot
from PyQt6.QtCore import QItemSelectionModel, QModelIndex, QPoint, QRect, Qt
from PyQt6.QtWidgets import QAbstractItemView

from src.app.ui.native_files_gallery_view import FilesGalleryView, gallery_rectangles
from src.app.ui.native_files_panel import FilesEntryModel


@pytest.mark.parametrize("mode", ["adaptive", "masonry", "grid"])
def test_mixed_aspects_have_nonoverlapping_bounded_cells(mode: str) -> None:
    ratios = [2 / 3, 16 / 9, 1.0, 3 / 2, 9 / 16, 6.0, 0.02] * 8
    cells, height = gallery_rectangles(ratios, 840, 160, mode)
    assert len(cells) == len(ratios)
    for row, cell in enumerate(cells):
        assert cell.left() >= 0 and cell.right() < 840
        assert cell.top() >= 0 and cell.height() > 44
        assert cell.bottom() < height
        assert all(not cell.intersects(other) for other in cells[row + 1:])


def test_adaptive_fills_rows_without_stretching_short_last_row() -> None:
    cells, _ = gallery_rectangles([1.0] * 7, 700, 180, "adaptive")
    first_top = cells[0].top()
    first_row = [cell for cell in cells if cell.top() == first_top]
    assert max(cell.right() for cell in first_row) == 691
    last = cells[-1]
    assert last.height() <= 180 + 56


def _view(qtbot: QtBot, count: int = 32) -> tuple[FilesGalleryView, FilesEntryModel]:
    model = FilesEntryModel()
    model.set_entries([
        {"name": f"{row:04}.png", "path": f"/fixture/{row:04}.png", "type": "image",
         "width": (200, 160, 100)[row % 3], "height": (100, 240, 100)[row % 3]}
        for row in range(count)])
    view = FilesGalleryView()
    qtbot.addWidget(view)
    view.setModel(model)
    view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.resize(720, 450)
    view.show()
    qtbot.waitUntil(lambda: bool(view.visibleIndexes()))
    return view, model


@pytest.mark.parametrize("mode", ["adaptive", "masonry", "grid"])
def test_all_items_hit_test_and_tail_scroll(qtbot: QtBot, mode: str) -> None:
    view, model = _view(qtbot)
    view.setLayoutMode(mode)
    for row in range(model.rowCount()):
        index = model.index(row, 0)
        view.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
        assert view.indexAt(view.visualRect(index).center()) == index
    view.scrollTo(model.index(model.rowCount() - 1, 0), QAbstractItemView.ScrollHint.PositionAtBottom)
    view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
    assert model.index(model.rowCount() - 1, 0) in view.visibleIndexes()


def test_layout_switch_keeps_selection_and_rectangle_selection(qtbot: QtBot) -> None:
    view, model = _view(qtbot)
    chosen = model.index(5, 0)
    view.selectionModel().select(chosen, QItemSelectionModel.SelectionFlag.ClearAndSelect
                                 | QItemSelectionModel.SelectionFlag.Rows)
    view.setLayoutMode("masonry")
    assert view.selectionModel().selectedRows() == [chosen]
    view.setLayoutMode("grid")
    first = model.index(0, 0)
    second = model.index(1, 0)
    region = view.visualRect(first).united(view.visualRect(second))
    view.setSelection(region, QItemSelectionModel.SelectionFlag.ClearAndSelect)
    assert {index.row() for index in view.selectionModel().selectedRows()} == {0, 1}
    view.setCurrentIndex(first)
    assert view.moveCursor(QAbstractItemView.CursorAction.MoveRight, Qt.KeyboardModifier.NoModifier) == second
    qtbot.keyClick(view, Qt.Key.Key_Right)
    assert view.currentIndex() == second
    qtbot.keyClick(view, Qt.Key.Key_End)
    assert view.currentIndex() == model.index(model.rowCount() - 1, 0)


def test_model_reset_clears_stale_geometry(qtbot: QtBot) -> None:
    view, model = _view(qtbot)
    view.scrollTo(model.index(31, 0))
    model.set_entries([{"name": "new", "path": "/new", "type": "image"}])
    qtbot.waitUntil(lambda: len(view._rectangles) == 1)
    assert view.verticalScrollBar().value() == 0
    assert view.indexAt(view.visualRect(model.index(0, 0)).center()) == model.index(0, 0)
    assert not view.indexAt(QPoint(700, 400)).isValid()


def test_thumbnail_hints_coalesce_and_metadata_has_priority(qtbot: QtBot) -> None:
    view, model = _view(qtbot, 3)
    known = view.visualRect(model.index(0, 0))
    view.setAspectRatio("/fixture/0000.png", 0.25)
    assert not view._layout_timer.isActive()
    assert view.visualRect(model.index(0, 0)) == known
    model.set_entries([{"name": str(row), "path": f"/unknown/{row}", "type": "image"}
                       for row in range(30)])
    qtbot.waitUntil(lambda: len(view._rectangles) == 30)
    for row in range(30):
        view.setAspectRatio(f"/unknown/{row}", 2.0)
    assert view._layout_timer.isActive()
    qtbot.waitUntil(lambda: not view._layout_timer.isActive())
    first = view.visualRect(model.index(0, 0))
    assert first.width() > first.height() - 56


@pytest.mark.parametrize("mode", ["adaptive", "masonry", "grid"])
def test_ten_thousand_entries_keep_only_viewport_candidates(qtbot: QtBot, mode: str) -> None:
    view, model = _view(qtbot, 10_000)
    start = time.monotonic()
    view.setLayoutMode(mode)
    view.doItemsLayout()
    elapsed = time.monotonic() - start
    assert len(view._rectangles) == 10_000
    assert len(view.visibleIndexes()) < 100
    assert len(view.findChildren(FilesGalleryView)) == 0
    assert elapsed < 5.0  # Guards accidental quadratic layout, not a wall-clock FPS claim.
    index = model.index(9_999, 0)
    view.scrollTo(index)
    assert view.indexAt(view.visualRect(index).center()) == index


def test_resize_keeps_top_visible_path_anchor(qtbot: QtBot) -> None:
    view, model = _view(qtbot, 100)
    view.scrollTo(model.index(50, 0), QAbstractItemView.ScrollHint.PositionAtTop)
    old = view._capture_anchor()
    assert old is not None
    view.resize(560, 450)
    qtbot.waitUntil(lambda: not view._layout_timer.isActive())
    anchor_row = next(row for row in range(model.rowCount())
                      if model.index(row, 0).data(FilesEntryModel.ENTRY_ROLE).path == old[0])
    assert abs(view.visualRect(model.index(anchor_row, 0)).top() - old[1]) < 2


@pytest.mark.parametrize("mode", ["adaptive", "masonry", "grid"])
def test_real_mouse_single_toggle_and_shift_range(qtbot: QtBot, mode: str) -> None:
    view, model = _view(qtbot, 18)
    view.setThumbnailSize(100)
    view.setLayoutMode(mode)
    qtbot.wait(20)
    viewport = view.viewport()
    qtbot.mouseClick(viewport, Qt.MouseButton.LeftButton,
                     pos=view.visualRect(model.index(0, 0)).center())
    assert {index.row() for index in view.selectionModel().selectedRows()} == {0}
    qtbot.mouseClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier,
                     pos=view.visualRect(model.index(2, 0)).center())
    assert {index.row() for index in view.selectionModel().selectedRows()} == {0, 2}
    qtbot.mouseClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier,
                     pos=view.visualRect(model.index(2, 0)).center())
    assert {index.row() for index in view.selectionModel().selectedRows()} == {0}
    qtbot.mouseClick(viewport, Qt.MouseButton.LeftButton,
                     pos=view.visualRect(model.index(0, 0)).center())
    qtbot.mouseClick(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
                     pos=view.visualRect(model.index(5, 0)).center())
    assert {index.row() for index in view.selectionModel().selectedRows()} == set(range(6))


@pytest.mark.parametrize("mode", ["adaptive", "masonry", "grid"])
def test_real_blank_drag_selects_intersecting_cells(qtbot: QtBot, mode: str) -> None:
    view, model = _view(qtbot, 18)
    view.setThumbnailSize(100)
    view.setLayoutMode(mode)
    qtbot.wait(20)
    first = view.visualRect(model.index(0, 0))
    second = view.visualRect(model.index(1, 0))
    start = QPoint(2, 2)
    end = first.united(second).bottomRight() + QPoint(2, 2)
    expected = {index.row() for index in view.visibleIndexes()
                if view.visualRect(index).intersects(QRect(start, end))}
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(view.viewport(), end, delay=20)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert {index.row() for index in view.selectionModel().selectedRows()} == expected


def test_keyboard_shift_range_matches_model_order(qtbot: QtBot) -> None:
    view, model = _view(qtbot, 18)
    view.setThumbnailSize(100)
    view.setLayoutMode("masonry")
    view.setCurrentIndex(model.index(2, 0))
    qtbot.keyClick(view, Qt.Key.Key_End, Qt.KeyboardModifier.ShiftModifier)
    assert {index.row() for index in view.selectionModel().selectedRows()} == set(range(2, 18))


def test_blank_drag_auto_scroll_stops_on_release(qtbot: QtBot) -> None:
    view, _ = _view(qtbot, 100)
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    end = QPoint(view.viewport().width() // 2, view.viewport().height() - 2)
    qtbot.mouseMove(view.viewport(), end, delay=20)
    qtbot.waitUntil(lambda: view.verticalScrollBar().value() > 0)
    assert view._rubber_active
    assert view._rubber_timer.isActive()
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    value = view.verticalScrollBar().value()
    qtbot.wait(80)
    assert view.verticalScrollBar().value() == value
    assert not view._rubber_timer.isActive()
    assert not view._rubber_active


def test_blank_click_clears_selection_and_double_click_activates(qtbot: QtBot) -> None:
    view, model = _view(qtbot, 5)
    view.setCurrentIndex(model.index(0, 0))
    assert view.selectionModel().selectedRows()
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    assert not view.selectionModel().selectedRows()
    point = view.visualRect(model.index(0, 0)).center()
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    with qtbot.waitSignal(view.doubleClicked):
        qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)


def test_native_drag_starts_from_selected_item(qtbot: QtBot, monkeypatch: pytest.MonkeyPatch) -> None:
    view, model = _view(qtbot, 5)
    view.setDragEnabled(True)
    view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
    drags: list[Qt.DropAction] = []
    monkeypatch.setattr(view, "startDrag", drags.append)
    start = view.visualRect(model.index(0, 0)).center()
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    qtbot.mouseMove(view.viewport(), start + QPoint(40, 0), delay=20)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=start + QPoint(40, 0))
    assert len(drags) == 1


def test_grid_column_count_tracks_slider_target() -> None:
    cells, _ = gallery_rectangles([1.0] * 20, 904, 160, "grid")
    first_row = [cell for cell in cells if cell.top() == cells[0].top()]
    assert len(first_row) == 5
    assert abs(first_row[0].width() - 12 - 160) < 8


@pytest.mark.parametrize("change", ["hide", "reset", "layout"])
def test_rubber_drag_cancelled_before_view_or_model_changes(qtbot: QtBot, change: str) -> None:
    view, model = _view(qtbot, 100)
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(2, 2))
    end = QPoint(view.viewport().width() // 2, view.viewport().height() - 2)
    qtbot.mouseMove(view.viewport(), end, delay=20)
    qtbot.waitUntil(lambda: view._rubber_timer.isActive())
    if change == "hide":
        view.hide()
    elif change == "reset":
        model.set_entries([{"name": "replacement", "path": "/replacement", "type": "image"}])
    else:
        model.layoutAboutToBeChanged.emit()
        model.layoutChanged.emit()
    assert not view._rubber_timer.isActive()
    assert view._rubber_origin is None
    assert not view._range_anchor.isValid()
    value = view.verticalScrollBar().value()
    qtbot.wait(80)
    if change == "reset":
        assert not view.selectionModel().selectedRows()
        assert view.verticalScrollBar().value() == 0
    else:
        assert view.verticalScrollBar().value() == value
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)


def test_resize_reuses_metadata_but_changed_dimensions_reflow(qtbot: QtBot) -> None:
    """Large-library resizing must not re-query every media entry on each frame."""
    from dataclasses import replace

    class CountingModel(FilesEntryModel):
        reads = 0

        def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> object:
            if role == self.ENTRY_ROLE:
                self.reads += 1
            return super().data(index, role)

    model = CountingModel()
    model.set_entries([{"name": str(row), "path": f"/cached/{row}", "type": "image",
                        "width": 400, "height": 200} for row in range(1000)])
    view = FilesGalleryView()
    qtbot.addWidget(view)
    view.setModel(model)
    view.resize(800, 600)
    view.show()
    view.setLayoutMode("masonry")
    qtbot.waitUntil(lambda: not view._layout_timer.isActive())
    model.reads = 0
    view.resize(720, 500)
    qtbot.waitUntil(lambda: not view._layout_timer.isActive())
    assert model.reads < 100  # Visible paint reads are fine; a full rescan is not.
    index = model.index(0, 0)
    previous = view.visualRect(index)
    entry = model.entries[0]
    model.entries[0] = replace(entry, technical=replace(entry.technical, width=100, height=400))
    model.dataChanged.emit(index, index, [model.ENTRY_ROLE])
    qtbot.waitUntil(lambda: view.visualRect(index).height() > previous.height())
