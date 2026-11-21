"""Column-count zoom model — shared controller + main-app panel integration.

The controller tiles an icon-mode view with N columns whose width derives
from the live viewport (flush right, no dead strip).  These tests pin the
math on a bare view first, then verify the layouts used by Media Pool,
Library, Presets, and 3D LUT.
"""

from __future__ import annotations

import time

import pytest
from PyQt6.QtCore import QSettings, QSize, Qt
from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from src.shared.ui.column_grid import ColumnGridController

_ALL_KEYS = ("probe_columns", "library_columns",
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
    monkeypatch.chdir(tmp_path)
    from src.features.color.ui import presets_panel
    monkeypatch.setattr(presets_panel, "_resolve_presets_root",
                        lambda: str(tmp_path / "Presets"))
    from src.features.color.ui.presets_panel import PresetsPanel
    panel = PresetsPanel()
    for index in range(12):
        panel.list_widget.addItem(QListWidgetItem(f"preset {index}"))
    panel._column_grid.pin_items()
    panel.resize(520, 320)
    panel.show()
    _process_ui(qapp)
    try:
        panel.zoom_slider.setValue(160)
        _process_ui(qapp, rounds=3)
        assert _row_zero_count(panel.list_widget) == 2
        icon = panel.list_widget.iconSize()
        assert icon.width() == 160
        assert icon.height() == 90
        assert panel.list_widget.gridSize().height() > icon.height()
    finally:
        panel.hide()
        panel.deleteLater()


@pytest.mark.parametrize("legacy_columns, saved_width, expected_width", [
    (None, None, 180), (4, None, 144), (8, ["180"], 96),
])
def test_media_pool_thumbnail_width_migrates_and_persists(qapp, tmp_path, monkeypatch,
                                                          legacy_columns, saved_width, expected_width):
    pytest.importorskip("src.features.edit.ui.media_pool_panel")
    from src.features.edit.ui import media_pool_panel

    def settings(org: str, app: str) -> QSettings:
        return QSettings(str(tmp_path / f"{app}.ini"), QSettings.Format.IniFormat)

    monkeypatch.setattr(media_pool_panel, "QSettings", settings)
    legacy = settings("ToposColor", "ColumnGrids")
    if legacy_columns is not None:
        legacy.setValue("media_pool_columns", legacy_columns)
    if saved_width is not None:
        settings("ToposColor", "MediaPool").setValue("thumbnail_width", saved_width)
    panel = media_pool_panel.MediaPoolPanel()
    restored = None
    try:
        assert panel.get_thumb_size() == expected_width
        assert panel.thumb_slider.minimum() == 96
        assert panel.thumb_slider.maximum() == 320
        assert not panel.thumb_slider.invertedAppearance()
        panel.set_thumb_size(176)
        assert panel.thumb_slider.value() == 176
        restored = media_pool_panel.MediaPoolPanel()
        assert restored.get_thumb_size() == 176
        assert restored.grid_view.iconSize() == QSize(176, 99)
    finally:
        panel.close()
        panel.deleteLater()
        if restored is not None:
            restored.close()
            restored.deleteLater()


def test_library_reuses_files_four_layout_model(qapp, tmp_path, monkeypatch):
    from src.app.ui.library_panel import LibraryPanel
    from src.app.ui.native_files_panel import NativeFilesPanel
    from src.features.files.repository import FilesDataLocator
    locator = FilesDataLocator.for_config_root(tmp_path / "settings")
    monkeypatch.setattr(FilesDataLocator, "default", classmethod(lambda cls: locator))
    monkeypatch.setattr(NativeFilesPanel, "is_browsable_path", lambda self, path: True)
    panel = LibraryPanel(start_path=str(tmp_path))
    try:
        assert panel.model is panel._native_panel.model
        assert panel.proxy_model is panel._native_panel.proxy
        for mode in ("adaptive", "masonry", "grid", "list"):
            panel.set_view_mode(mode)
            assert panel._native_panel.view_combo.currentData() == mode
    finally:
        panel.shutdown()
        panel.deleteLater()
