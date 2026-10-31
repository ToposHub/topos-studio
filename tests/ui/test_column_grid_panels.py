"""Column-count zoom model — shared controller + main-app panel integration.

The controller tiles an icon-mode view with N columns whose width derives
from the live viewport (flush right, no dead strip).  These tests pin the
math on a bare view first, then verify each ported panel (Media Pool,
Library, Presets, 3D LUT) actually drives its grid through it.
"""

from __future__ import annotations

import time

import pytest
from PyQt6.QtCore import QSettings, QSize, Qt
from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from src.shared.ui.column_grid import ColumnGridController

_ALL_KEYS = ("probe_columns", "media_pool_columns", "library_columns",
             "presets_columns", "lut_columns")


@pytest.fixture()
def clean_column_settings():
    settings = QSettings("ToposColor", "ColumnGrids")
    for key in _ALL_KEYS:
        settings.remove(key)
    yield
    for key in _ALL_KEYS:
        settings.remove(key)


def _process_ui(qapp, rounds: int = 8) -> None:
    for _ in range(rounds):
        qapp.processEvents()
        time.sleep(0.01)


def _make_grid(parent: QWidget, items: int) -> QListWidget:
    view = QListWidget(parent)
    view.setViewMode(QListWidget.ViewMode.IconMode)
    view.setResizeMode(QListWidget.ResizeMode.Adjust)
    view.setMovement(QListWidget.Movement.Static)
    for index in range(items):
        view.addItem(QListWidgetItem(f"item {index}"))
    QVBoxLayout(parent).addWidget(view)
    return view


def _row_zero_count(view) -> int:
    """Number of items laid out on the first row."""
    model = view.model()
    if model is None:
        return 0
    count = 0
    for row in range(model.rowCount()):
        rect = view.visualRect(model.index(row, 0))
        if not rect.isValid():
            continue
        if rect.y() == 0:
            count += 1
        else:
            break
    return count


def _assert_flush(view, columns: int) -> None:
    viewport_width = view.viewport().width()
    cell = view.gridSize().width()
    assert viewport_width - cell * columns < 2 + columns


# ---------------------------------------------------------------------- #
# Controller math on a bare view
# ---------------------------------------------------------------------- #

def test_controller_tiles_columns_flush_right(qapp, clean_column_settings):
    holder = QWidget()
    view = _make_grid(holder, items=20)
    holder.resize(500, 420)
    holder.show()
    controller = ColumnGridController(view, settings_key="probe_columns",
                                      gap=6, aspect=9 / 16, extra_height=24)
    _process_ui(qapp)
    try:
        for columns in (1, 3, 8):
            controller.apply_columns(columns)
            _process_ui(qapp, rounds=3)
            _assert_flush(view, columns)
            assert _row_zero_count(view) == columns
        # The gap lives inside the pitch: the icon is inset within its cell.
        assert view.gridSize().width() - view.iconSize().width() == 6
        # The preference round-trips through QSettings.
        controller.apply_columns(5)
        assert int(QSettings("ToposColor", "ColumnGrids").value("probe_columns")) == 5
    finally:
        holder.hide()
        holder.deleteLater()


def test_controller_suspends_and_pins_fresh_items(qapp, clean_column_settings):
    holder = QWidget()
    view = _make_grid(holder, items=4)
    holder.resize(400, 300)
    holder.show()
    applied: list[QSize] = []
    controller = ColumnGridController(view, settings_key=None,
                                      on_columns_applied=lambda icon, cell: applied.append(icon))
    _process_ui(qapp)
    try:
        icon_before = view.iconSize()
        # Same geometry → the apply hook must not re-fire (consumers use it
        # to re-request thumbnails; per-tick storms are not acceptable).
        controller.apply_columns(controller.columns)
        assert applied == [icon_before]
        # Suspending freezes the geometry (Library list mode).
        controller.set_active(False)
        frozen = view.gridSize()
        controller.apply_columns(7)
        assert view.gridSize() == frozen
        controller.set_active(True)
        _process_ui(qapp, rounds=3)
        assert view.gridSize() != frozen
        # Fresh items get pinned to the current cell.
        view.addItem(QListWidgetItem("fresh"))
        controller.pin_items()
        cell = view.gridSize()
        assert view.item(view.count() - 1).sizeHint() == cell
    finally:
        holder.hide()
        holder.deleteLater()


# ---------------------------------------------------------------------- #
# Panel integrations
# ---------------------------------------------------------------------- #

def test_lut_gallery_column_zoom(qapp, clean_column_settings):
    from src.shared.ui.widgets.lut_gallery import LutGallery
    gallery = LutGallery()
    while gallery.list_widget.count() < 12:
        gallery.list_widget.addItem(QListWidgetItem("probe lut"))
    gallery._column_grid.pin_items()
    gallery.resize(520, 320)
    gallery.show()
    _process_ui(qapp)
    try:
        gallery.zoom_slider.setValue(3)
        _process_ui(qapp, rounds=3)
        _assert_flush(gallery.list_widget, 3)
        assert _row_zero_count(gallery.list_widget) == 3
        assert gallery.zoom_value_label.text() == "3"
    finally:
        gallery.hide()
        gallery.deleteLater()


def test_presets_panel_column_zoom(qapp, monkeypatch, tmp_path,
                                   clean_column_settings):
    monkeypatch.chdir(tmp_path)  # presets_root = cwd/Presets stays in tmp
    from src.features.color.ui.presets_panel import PresetsPanel
    panel = PresetsPanel()
    for index in range(12):
        panel.list_widget.addItem(QListWidgetItem(f"preset {index}"))
    panel._column_grid.pin_items()
    panel.resize(520, 320)
    panel.show()
    _process_ui(qapp)
    try:
        panel.zoom_slider.setValue(2)
        _process_ui(qapp, rounds=3)
        _assert_flush(panel.list_widget, 2)
        assert _row_zero_count(panel.list_widget) == 2
        # Square cards: the delegate paints the caption inside the card.
        icon = panel.list_widget.iconSize()
        assert icon.width() == icon.height()
    finally:
        panel.hide()
        panel.deleteLater()


def test_media_pool_column_zoom(qapp, clean_column_settings):
    pytest.importorskip("src.features.edit.ui.media_pool_panel")
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel
    panel = MediaPoolPanel()
    panel.resize(560, 360)
    panel.show()
    _process_ui(qapp)
    try:
        assert panel.thumb_slider.minimum() == 1
        assert panel.thumb_slider.maximum() == 8
        panel._column_grid.apply_columns(3)
        _process_ui(qapp, rounds=3)
        view = panel.grid_view
        _assert_flush(view, 3)
        # The delegate's thumb_size (drives paint + sizeHint) follows the
        # applied icon, 3:2 aspect.
        assert panel.grid_delegate.thumb_size[1] == round(
            panel.grid_delegate.thumb_size[0] * 2 / 3)
        assert panel._thumb_width == view.iconSize().width()
    finally:
        panel.hide()
        panel.close()
        panel.deleteLater()


def test_library_panel_column_zoom_and_list_mode(qapp, tmp_path,
                                                 clean_column_settings):
    for index in range(12):
        (tmp_path / f"img_{index:02d}.jpg").write_bytes(b"")
    from src.app.ui.library_panel import LibraryPanel
    panel = LibraryPanel(start_path=str(tmp_path))
    panel.resize(520, 360)
    panel.show()
    # QFileSystemModel populates asynchronously.
    for _ in range(60):
        _process_ui(qapp, rounds=1)
        if panel.list_view.model() and panel.list_view.model().rowCount() >= 12:
            break
    try:
        panel._column_grid.apply_columns(3)
        _process_ui(qapp, rounds=3)
        _assert_flush(panel.list_view, 3)
        # List mode suspends the controller and owns its geometry.
        panel.set_view_mode("list")
        assert panel.list_view.viewMode() == panel.list_view.viewMode().ListMode
        assert not panel.zoom_slider.isEnabled()
        panel.set_view_mode("grid")
        _process_ui(qapp, rounds=3)
        assert panel.zoom_slider.isEnabled()
        _assert_flush(panel.list_view, panel._column_grid.columns)
    finally:
        panel.hide()
        panel.deleteLater()
