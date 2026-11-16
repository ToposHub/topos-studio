"""Panel-level contracts for four layouts, shared selection and real thumbnails.

Synthetic images exercise the presentation path; QSettings always points to a
temporary INI. No service, media probing, or user preferences are touched.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot
from PyQt6.QtCore import (
    QBuffer, QIODevice, QItemSelectionModel, QModelIndex, QPersistentModelIndex,
    QSettings, Qt,
)
from PyQt6.QtGui import QColor, QImage, QPainter

from src.app.ui.native_files_panel import FilesEntryModel, NativeFilesPanel
from src.app.ui.native_files_prefs import NativeFilesPreferences


MODES = ("adaptive", "masonry", "grid", "list")


def _entries() -> list[dict[str, object]]:
    return [
        {"name": "A-wide.png", "path": "/media/A-wide.png", "type": "image",
         "width": 320, "height": 80},
        {"name": "B-tall.png", "path": "/media/B-tall.png", "type": "image",
         "width": 80, "height": 240},
        {"name": "C-late.png", "path": "/media/C-late.png", "type": "image"},
        {"name": "D-square.png", "path": "/media/D-square.png", "type": "image",
         "width": 160, "height": 160},
    ]


def _png(width: int = 320, height: int = 80, *, edges: bool = False) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#e811e8"))
    if edges:
        painter = QPainter(image)
        painter.fillRect(0, 0, width // 6, height, QColor("#ff0000"))
        painter.fillRect(width - width // 6, 0, width // 6, height, QColor("#0000ff"))
        painter.end()
    buffer = QBuffer()
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(buffer.data())


def _index(panel: NativeFilesPanel, name: str) -> QModelIndex:
    for row in range(panel.proxy.rowCount()):
        index = panel.proxy.index(row, 0)
        if index.data() == name:
            return index
    raise AssertionError(f"Missing displayed file: {name}")


def _select(panel: NativeFilesPanel, *names: str) -> None:
    panel.selection.clearSelection()
    for name in names:
        panel.selection.select(_index(panel, name),
                               QItemSelectionModel.SelectionFlag.Select
                               | QItemSelectionModel.SelectionFlag.Rows)
    if names:
        panel.selection.setCurrentIndex(_index(panel, names[0]),
                                        QItemSelectionModel.SelectionFlag.NoUpdate)


def _mode(panel: NativeFilesPanel, qtbot: QtBot, mode: str) -> None:
    index = panel.view_combo.findData(mode)
    assert index >= 0
    panel.view_combo.setCurrentIndex(index)
    qtbot.waitUntil(lambda: panel.pages.currentIndex() == (1 if mode == "list" else 0))
    if mode != "list" and panel.proxy.rowCount():
        qtbot.waitUntil(lambda: panel.icon_view.visualRect(panel.proxy.index(0, 0)).isValid())


@pytest.fixture
def panel(qtbot: QtBot, tmp_path: Path) -> Iterator[NativeFilesPanel]:
    settings = QSettings(str(tmp_path / "layout.ini"), QSettings.Format.IniFormat)
    prefs = NativeFilesPreferences(settings)
    prefs.sidebar_preference = False
    prefs.inspector_preference = False
    widget = NativeFilesPanel(preferences=prefs)
    qtbot.addWidget(widget)
    widget.set_entries("/media", _entries())
    widget.resize(1000, 700)
    widget.show()
    qtbot.waitUntil(lambda: widget.icon_view.visualRect(widget.proxy.index(0, 0)).isValid())
    yield widget
    widget.shutdown()


def test_four_layouts_keep_selection_drag_urls_and_open_preview(
    panel: NativeFilesPanel, qtbot: QtBot,
) -> None:
    assert [panel.view_combo.itemData(i) for i in range(panel.view_combo.count())] == list(MODES)
    preview_requests: list[str] = []
    panel.preview_requested.connect(preview_requests.append)
    assert panel.open_path_native("/media/A-wide.png")
    _select(panel, "A-wide.png", "B-tall.png")
    expected_urls = ["/media/A-wide.png", "/media/B-tall.png"]
    current = QPersistentModelIndex(panel.selection.currentIndex())

    for mode in (*MODES, "adaptive"):
        _mode(panel, qtbot, mode)
        assert panel.icon_view.model() is panel.detail_view.model() is panel.proxy
        assert panel.icon_view.selectionModel() is panel.detail_view.selectionModel() is panel.selection
        assert [url.toLocalFile() for url in panel.mime_data_for_selection().urls()] == expected_urls
        assert panel.selection.currentIndex() == current
        assert panel._preview_path == "/media/A-wide.png"
        assert panel._preview.is_open
    assert preview_requests == ["/media/A-wide.png"]


@pytest.mark.parametrize("mode", MODES[:3])
def test_gallery_keyboard_navigation_and_activation_use_displayed_identity(
    panel: NativeFilesPanel, qtbot: QtBot, mode: str,
) -> None:
    _mode(panel, qtbot, mode)
    _select(panel, "A-wide.png")
    panel.icon_view.setFocus()
    qtbot.keyClick(panel.icon_view, Qt.Key.Key_Right)
    current = panel.selection.currentIndex()
    assert current.isValid()
    assert current != _index(panel, "A-wide.png")
    activated: list[str] = []
    panel.file_activated.connect(activated.append)
    qtbot.keyClick(panel.icon_view, Qt.Key.Key_Return)
    assert activated == [current.data(FilesEntryModel.ENTRY_ROLE).path]
    assert panel.mime_data_for_selection().hasUrls()


def test_late_thumbnail_changes_geometry_without_replacing_selected_file(
    panel: NativeFilesPanel, qtbot: QtBot,
) -> None:
    _mode(panel, qtbot, "masonry")
    _select(panel, "C-late.png")
    index = _index(panel, "C-late.png")
    identity = QPersistentModelIndex(index)
    before = panel.icon_view.visualRect(index)
    panel.set_thumbnail("/media/C-late.png", _png(80, 240))
    qtbot.waitUntil(lambda: panel.icon_view.visualRect(index).height() != before.height())
    assert identity.isValid()
    assert identity.data(FilesEntryModel.ENTRY_ROLE).path == "/media/C-late.png"
    assert panel.selection.currentIndex() == identity
    assert [url.toLocalFile() for url in panel.mime_data_for_selection().urls()] == ["/media/C-late.png"]


def _colors(image: QImage) -> set[str]:
    return {image.pixelColor(x, y).name()
            for x in range(image.width()) for y in range(image.height())}


def test_list_paints_delivered_thumbnail_instead_of_generic_icon(
    panel: NativeFilesPanel, qtbot: QtBot,
) -> None:
    _mode(panel, qtbot, "list")
    index = _index(panel, "A-wide.png")
    rect = panel.detail_view.visualRect(index)
    assert "#e811e8" not in _colors(panel.detail_view.viewport().grab(rect).toImage())
    panel.set_thumbnail("/media/A-wide.png", _png())
    panel.detail_view.viewport().repaint()
    assert "#e811e8" in _colors(panel.detail_view.viewport().grab(rect).toImage())


@pytest.mark.parametrize("mode", MODES[:3])
def test_fit_fill_changes_only_grid_pixels_and_never_file_geometry(
    panel: NativeFilesPanel, qtbot: QtBot, mode: str,
) -> None:
    panel.set_thumbnail("/media/A-wide.png", _png(edges=True))
    _mode(panel, qtbot, mode)
    index = _index(panel, "A-wide.png")
    rect = panel.icon_view.visualRect(index)
    panel._set_grid_fill(False)
    panel.icon_view.viewport().repaint()
    fit = panel.icon_view.viewport().grab(rect).toImage()
    assert {"#ff0000", "#0000ff"} <= _colors(fit)
    panel._set_grid_fill(True)
    panel.icon_view.viewport().repaint()
    fill = panel.icon_view.viewport().grab(rect).toImage()
    assert panel.icon_view.visualRect(index) == rect
    if mode == "grid":
        assert not {"#ff0000", "#0000ff"} & _colors(fill)
    else:
        assert fill == fit


@pytest.mark.parametrize(("legacy", "expected"), [(0, "grid"), (1, "list")])
def test_legacy_view_preference_migrates_and_new_layout_survives_reopen(
    qtbot: QtBot, tmp_path: Path, legacy: int, expected: str,
) -> None:
    path = str(tmp_path / "legacy.ini")
    settings = QSettings(path, QSettings.Format.IniFormat)
    settings.setValue("files/native/view_index", legacy)
    settings.sync()
    first = NativeFilesPanel(preferences=NativeFilesPreferences(settings))
    qtbot.addWidget(first)
    try:
        assert first.view_combo.currentData() == expected
        assert first.pages.currentIndex() == (1 if expected == "list" else 0)
        _mode(first, qtbot, "masonry")
    finally:
        first.shutdown()
    reopened = NativeFilesPanel(preferences=NativeFilesPreferences(
        QSettings(path, QSettings.Format.IniFormat)))
    qtbot.addWidget(reopened)
    try:
        assert reopened.view_combo.currentData() == "masonry"
        assert reopened.pages.currentIndex() == 0
        assert reopened.icon_view.layoutMode() == "masonry"
    finally:
        reopened.shutdown()


def test_short_inspector_scroll_and_narrow_cover_preserve_edit_and_full_image(
    panel: NativeFilesPanel, qtbot: QtBot,
) -> None:
    panel.resize(1180, 460)
    panel._toggle_inspector(True)
    panel.set_thumbnail("/media/A-wide.png", _png())
    _select(panel, "A-wide.png")
    qtbot.waitUntil(lambda: not panel.preview_cover.pixmap().isNull())
    panel.comment_edit.setText("unfinished draft")
    panel.inspector_scroll.ensureWidgetVisible(panel.comment_edit)
    qtbot.waitUntil(lambda: panel.inspector_scroll.verticalScrollBar().maximum() > 0)
    viewport = panel.inspector_scroll.viewport()
    comment_rect = panel.comment_edit.rect().translated(panel.comment_edit.mapTo(viewport, panel.comment_edit.rect().topLeft()))
    assert viewport.rect().intersects(comment_rect)
    panel.splitter.setSizes([0, 970, 200])
    qtbot.waitUntil(lambda: panel.preview_cover.pixmap().width() <= panel.preview_cover.contentsRect().width())
    assert panel.comment_edit.text() == "unfinished draft"
    pixmap = panel.preview_cover.pixmap()
    assert pixmap.height() <= panel.preview_cover.contentsRect().height()
    assert abs(pixmap.width() / pixmap.height() - 4.0) < 0.1
