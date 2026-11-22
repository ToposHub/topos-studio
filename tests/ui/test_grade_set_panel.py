"""Grade set cards use the active dock height and request painted photos only."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PyQt6.QtCore import QBuffer, QIODevice, QItemSelectionModel, QMimeData, QObject, QPoint, QPointF, QRect, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QDropEvent, QImage, QPainter
from PyQt6.QtWidgets import QFrame, QStyleOptionViewItem

import src.app.ui.grade_set_panel as grade_set_panel
from src.app.ui.grade_set_panel import GradeSetPanel
from src.app.ui.native_files_gallery_view import GALLERY_MARGIN


class _Sets(QObject):
    changed = pyqtSignal()
    ratings_changed = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, paths: tuple[str, ...]) -> None:
        super().__init__()
        self.active_id = 1
        self.collections = (SimpleNamespace(id=1, name="Portraits", count=len(paths)),
                            SimpleNamespace(id=2, name="Landscape", count=1))
        self.paths = paths
        self.ratings: dict[str, int] = {}
        self.added: list[tuple[int, list[str]]] = []

    def select(self, collection_id: int) -> None:
        self.active_id = collection_id
        self.paths = ("/tmp/other.png",)
        self.changed.emit()

    def add(self, collection_id: int, paths: list[str]) -> None:
        self.added.append((collection_id, paths))


class _ThumbnailRequests(QObject):
    thumbnail_loaded = pyqtSignal(str, bytes)
    thumbnail_failed = pyqtSignal(str)

    def __init__(self, url, token, parent, *, facade_workers: int) -> None:
        super().__init__(parent)
        self.requests: list[str] = []
        self.cancel_count = 0
        self.closed = False

    def request_thumbnail(self, path: str) -> None:
        self.requests.append(path)

    def release_thumbnail(self, path: str) -> None:
        pass

    def cancel(self) -> None:
        self.cancel_count += 1

    def shutdown(self) -> None:
        self.closed = True


def _build(qtbot, monkeypatch, *, count: int = 120):
    monkeypatch.setattr(grade_set_panel, "NativeFilesTransport", _ThumbnailRequests)
    model = _Sets(tuple(f"/tmp/image_{i}.png" for i in range(count)))
    panel = GradeSetPanel(model)
    qtbot.addWidget(panel)
    return panel, model


def test_cards_read_left_to_right_and_wrap_at_dock_width(qtbot, monkeypatch):
    panel, _sets = _build(qtbot, monkeypatch)
    panel.resize(430, 1)
    panel.show()
    def rect(row: int) -> QRect:
        return panel.view.visualRect(panel.view.model().index(row, 0))

    qtbot.waitUntil(lambda: rect(1).x() > rect(0).x() and rect(3).y() > rect(0).y())
    assert rect(0).y() == rect(1).y() == rect(2).y()
    assert rect(0).x() == rect(3).x()
    assert rect(1).left() - rect(0).right() - 1 == 3
    assert rect(3).top() - rect(0).bottom() - 1 == 3
    assert panel.view.viewport().rect().contains(rect(0))
    assert not panel.view.viewport().rect().intersects(rect(3))

    panel.resize(430, 425)
    qtbot.waitUntil(lambda: panel.view.viewport().rect().contains(rect(3)))
    assert rect(0).y() == rect(2).y() < rect(3).y() == rect(4).y()
    assert panel.view.verticalScrollBar().maximum() > 0
    assert not panel.view.horizontalScrollBar().isVisible()

    panel.resize(770, 390)
    qtbot.waitUntil(lambda: rect(4).y() == rect(0).y())
    first_row = [cell for row in range(10) if (cell := rect(row)).y() == rect(0).y()]
    assert first_row[-1].right() == panel.view.viewport().width() - GALLERY_MARGIN - 1


def test_zoom_changes_card_pitch_and_preserves_the_current_photo(qtbot, monkeypatch):
    panel, sets = _build(qtbot, monkeypatch)
    panel.resize(430, 1)
    panel.show()
    view = panel.view
    model = view.model()
    first, third = model.index(0, 0), model.index(2, 0)
    qtbot.waitUntil(lambda: view.visualRect(first).isValid())
    panel.set_active_path(sets.paths[0])
    view.setCurrentIndex(first)
    initial = view.visualRect(first).size()
    panel.size_slider.setValue(180)
    qtbot.waitUntil(lambda: view.visualRect(first).width() > initial.width())
    assert view.visualRect(third).y() > view.visualRect(first).y()
    assert view.viewport().rect().contains(view.visualRect(first))
    assert view.currentIndex().data(grade_set_panel._PATH_ROLE) == panel.active_path
    panel.size_slider.setValue(panel.size_slider.minimum())
    qtbot.waitUntil(lambda: view.visualRect(third).x() > view.visualRect(first).x())
    assert view.visualRect(first).width() < initial.width()


def test_compact_toolbar_stays_on_one_row_and_zoom_respects_dock_width(qtbot, monkeypatch):
    panel, _sets = _build(qtbot, monkeypatch)
    panel.resize(170, 330)
    panel.show()
    view = panel.view
    first = view.model().index(0, 0)
    qtbot.waitUntil(lambda: view.visualRect(first).isValid())
    controls = [panel.set_selector, panel.size_slider, panel.filter_button, panel.sort_button, panel.layout_button]
    assert all(widget.isVisible() for widget in controls)
    assert max(widget.geometry().top() for widget in controls) <= min(widget.geometry().bottom() for widget in controls)
    assert all(first.geometry().right() < second.geometry().left()
               for first, second in zip(controls, controls[1:]))
    divider = panel.findChild(QFrame, "gradeSetToolbarDivider")
    assert max(widget.geometry().bottom() for widget in controls) < divider.y() < view.y()
    assert divider.height() == 1 and view.frameWidth() == 0
    narrow_width = panel.width()
    panel.size_slider.setValue(panel.size_slider.maximum())
    assert panel.width() == narrow_width
    narrow_card_width = view.visualRect(first).width()
    assert narrow_card_width < panel.size_slider.value()
    assert view.viewport().rect().contains(view.visualRect(first))
    panel.resize(430, 330)
    qtbot.waitUntil(lambda: view.visualRect(first).width() > narrow_card_width)
    assert view.thumbnailSize() == panel.size_slider.value()


def test_async_aspects_justify_rows_through_zoom_and_layout_changes(qtbot, monkeypatch):
    panel, sets = _build(qtbot, monkeypatch, count=16)
    panel.resize(550, 500)
    panel.show()
    view = panel.view
    model = view.model()
    adaptive = next(action for action in panel.layout_button.menu().actions() if action.data() == "adaptive")
    assert adaptive.isChecked() and view.layoutMode() == "adaptive"
    qtbot.waitUntil(lambda: len(panel._pending) >= 2)
    first, second = model.index(0, 0), model.index(1, 0)
    for path, size in zip(sets.paths, (QSize(80, 120), QSize(180, 120))):
        image = QImage(size, QImage.Format.Format_RGB32)
        image.fill(QColor("blue"))
        raw = QBuffer()
        raw.open(QIODevice.OpenModeFlag.WriteOnly)
        assert image.save(raw, "PNG")
        panel._transport.thumbnail_loaded.emit(path, bytes(raw.data()))

    qtbot.waitUntil(lambda: view.visualRect(second).width() > view.visualRect(first).width())
    panel.set_active_path(sets.paths[0])
    view.setCurrentIndex(first)

    def first_row_fills() -> bool:
        cells = [view.visualRect(model.index(row, 0)) for row in range(model.rowCount())]
        row_end = max(cell.right() for cell in cells if cell.top() == cells[0].top())
        return row_end == view.viewport().width() - GALLERY_MARGIN - 1

    for size in (82, 122, 192):
        panel.size_slider.setValue(size)
        qtbot.waitUntil(first_row_fills)
        first_rect, second_rect = view.visualRect(first), view.visualRect(second)
        assert first_rect.top() == second_rect.top()
        assert first_rect.right() < second_rect.left()
        spacing = view.itemDelegate().spacing
        for rect, ratio in ((first_rect, 2 / 3), (second_rect, 1.5)):
            image_height = rect.height() - 2 * spacing.inset - spacing.caption_height
            assert abs(rect.width() - 2 * spacing.inset - ratio * image_height) <= 2

    grid = next(action for action in panel.layout_button.menu().actions() if action.data() == "grid")
    grid.trigger()
    assert view.layoutMode() == "grid"
    qtbot.waitUntil(first_row_fills)
    assert abs(view.visualRect(first).width() - view.visualRect(second).width()) <= 1
    adaptive.trigger()
    assert adaptive.isChecked() and view.layoutMode() == "adaptive"
    assert view.currentIndex() == first and panel.active_path == sets.paths[0]
    with qtbot.waitSignal(panel.image_requested, check_params_cb=lambda path: path == sets.paths[1]):
        qtbot.keyClick(view, Qt.Key.Key_Right)


def test_grade_card_paints_photo_without_always_visible_background(qtbot, monkeypatch):
    panel, sets = _build(qtbot, monkeypatch, count=1)
    preview = QImage(80, 160, QImage.Format.Format_RGB32)
    preview.fill(QColor("orange"))
    panel.set_graded_thumbnail(sets.paths[0], preview)
    canvas = QImage(180, 200, QImage.Format.Format_RGB32)
    backdrop = QColor("steelblue")
    canvas.fill(backdrop)
    option = QStyleOptionViewItem()
    option.rect = canvas.rect()
    index = panel.view.model().index(0, 0)
    with QPainter(canvas) as painter:
        panel.view.itemDelegate().paint(painter, option, index)

    assert canvas.pixelColor(10, 80) == backdrop  # Space beside a portrait in a wide cell.
    assert canvas.pixelColor(4, 190) == backdrop  # Card beneath the photo.
    assert canvas.pixelColor(90, 80) == QColor("orange")


def test_switching_set_discards_pending_requests_and_only_open_success_marks_active(qtbot, monkeypatch):
    panel, model = _build(qtbot, monkeypatch)
    panel.resize(420, 175)
    panel.show()
    qtbot.waitUntil(lambda: panel._transport is not None and bool(panel._transport.requests))
    transport = panel._transport
    assert len(transport.requests) <= 4
    assert model.paths[-1] not in transport.requests
    source = model.paths[0]
    opened: list[str] = []
    panel.image_requested.connect(opened.append)
    first = panel.view.visualRect(panel.view.model().index(0, 0)).center()
    qtbot.mouseClick(panel.view.viewport(), Qt.MouseButton.LeftButton, pos=first)
    assert opened == [source]
    assert panel.active_path is None
    panel.set_active_path(source)
    qtbot.mouseClick(panel.view.viewport(), Qt.MouseButton.LeftButton, pos=first)
    assert opened == [source]

    panel.set_selector.setCurrentIndex(1)
    assert model.active_id == 2
    assert panel.view.model().rowCount() == 1
    assert transport.cancel_count == 1
    assert source not in panel._pending
    panel.shutdown()
    assert transport.closed


def test_drop_adds_local_stills_passed_as_references(qtbot, monkeypatch, tmp_path: Path):
    panel, model = _build(qtbot, monkeypatch)
    photo = tmp_path / "portrait.dng"
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(photo)),
                  QUrl.fromLocalFile(str(tmp_path / "clip.mov")),
                  QUrl("https://example.com/remote.png")])
    drop = QDropEvent(QPointF(30, 30), Qt.DropAction.MoveAction | Qt.DropAction.CopyAction,
                      mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    panel.view.dropEvent(drop)
    assert drop.isAccepted()
    assert drop.dropAction() == Qt.DropAction.CopyAction
    assert model.added == [(model.active_id, [str(photo)])]


def test_empty_grade_set_can_add_images_from_context_menu(qtbot, monkeypatch):
    panel, sets = _build(qtbot, monkeypatch, count=0)
    monkeypatch.setattr(grade_set_panel.QFileDialog, "getOpenFileNames",
                        lambda *_args: (["/tmp/portrait.png"], "Images"))
    menu = panel._build_context_menu(QPoint(10, 10))
    menu.findChild(QAction, "gradeSetAddImages").trigger()
    assert sets.added == [(sets.active_id, ["/tmp/portrait.png"])]


def test_card_names_start_hidden_and_toggle_per_panel_from_context_menu(qtbot, monkeypatch):
    panel, sets = _build(qtbot, monkeypatch, count=4)
    panel.resize(500, 325)
    panel.show()
    view = panel.view
    first = view.model().index(0, 0)
    view.setCurrentIndex(first)
    initial_height = view.visualRect(first).height()
    assert view.itemDelegate().spacing.caption_height == 0

    with qtbot.waitSignal(panel.active_photo_name_changed, check_params_cb=lambda name: name == "image_0.png"):
        panel.set_active_path(sets.paths[0])

    menu = panel._build_context_menu(view.visualRect(first).center())
    action = menu.findChild(QAction, "gradeSetShowFilenames")
    assert action.isCheckable() and not action.isChecked()
    with qtbot.waitSignal(panel.filename_visibility_changed, check_params_cb=lambda visible: visible):
        action.trigger()
    assert view.visualRect(first).height() == initial_height + view.itemDelegate().spacing.caption_height
    assert view.itemDelegate().title_font_size == 9.0
    assert view.currentIndex() == first and panel.active_path == sets.paths[0]

    menu = panel._build_context_menu(QPoint(1, 1))
    action = menu.findChild(QAction, "gradeSetShowFilenames")
    assert action.isChecked()
    action.trigger()
    assert view.visualRect(first).height() == initial_height
    with qtbot.waitSignal(panel.active_photo_name_changed, check_params_cb=lambda name: name == ""):
        panel.set_active_path(None)


def test_graded_preview_keeps_portrait_aspect(qtbot, monkeypatch):
    panel, model = _build(qtbot, monkeypatch)
    source = model.paths[0]
    portrait = QImage(QSize(100, 200), QImage.Format.Format_RGB32)
    portrait.fill(QColor("orange"))
    panel.set_graded_thumbnail(source, portrait)
    thumbnail = panel.thumbnail_for_paint(source)
    assert thumbnail is not None and thumbnail.size() == QSize(100, 200)
    original = QImage(100, 200, QImage.Format.Format_RGB32)
    original.fill(QColor("blue"))
    panel._source_previews[source] = original
    panel.invalidate_graded_thumbnail(source)
    assert panel.thumbnail_for_paint(source) is original


def test_graded_preview_owns_rendered_pixel_buffer(qtbot, monkeypatch):
    panel, sets = _build(qtbot, monkeypatch)
    pixels = np.full((80, 60, 3), 100, dtype=np.uint8)
    preview = QImage(pixels.data, 60, 80, pixels.strides[0], QImage.Format.Format_RGB888)
    panel.set_graded_thumbnail(sets.paths[0], preview)
    pixels.fill(0)
    assert panel.thumbnail_for_paint(sets.paths[0]).pixelColor(30, 40) == QColor(100, 100, 100)


def test_card_grade_actions_point_to_right_clicked_photo(qtbot, monkeypatch):
    panel, sets = _build(qtbot, monkeypatch, count=4)
    panel.resize(500, 200)
    panel.show()
    model = panel.view.model()
    qtbot.waitUntil(lambda: panel.view.visualRect(model.index(1, 0)).isValid())
    panel.set_active_path(sets.paths[0])
    select = QItemSelectionModel.SelectionFlag.Select
    for row in (0, 1):
        panel.view.selectionModel().select(model.index(row, 0), select)
    copied, pasted = [], []
    panel.copy_grade_requested.connect(copied.append)
    panel.paste_grade_requested.connect(pasted.append)

    def actions_at(row: int) -> tuple[QAction, QAction]:
        pos = panel.view.visualRect(model.index(row, 0)).center()
        menu = panel._build_context_menu(pos)
        return (menu.findChild(QAction, "gradeSetCopyGrade"),
                menu.findChild(QAction, "gradeSetPasteGrade"))

    copy, paste = actions_at(0)
    assert copy.isEnabled() and not paste.isEnabled()
    copy.trigger()
    assert copied == [sets.paths[0]]
    panel.set_grade_clipboard_available(True)
    copy, paste = actions_at(1)
    assert not copy.isEnabled() and paste.isEnabled()
    paste.trigger()
    assert pasted == [sets.paths[1]]


def test_file_rating_updates_card_data_without_resetting_selection(qtbot, monkeypatch):
    panel, sets = _build(qtbot, monkeypatch)
    model = panel.view.model()
    first = model.index(0, 0)
    panel.view.setCurrentIndex(first)
    assert first.data(grade_set_panel._RATING_ROLE) == 0
    with qtbot.waitSignal(model.dataChanged):
        sets.ratings[sets.paths[0]] = 4
        sets.ratings_changed.emit()
    assert first.data(grade_set_panel._RATING_ROLE) == 4
    assert "4" in first.data(Qt.ItemDataRole.AccessibleDescriptionRole)
    assert panel.view.currentIndex() == first


def test_combined_filters_and_sort_update_when_ratings_change(qtbot, monkeypatch):
    panel, sets = _build(qtbot, monkeypatch, count=4)
    zebra, birch, alder, cove = sets.paths = ("/tmp/Zebra.jpg", "/tmp/birch.dng", "/tmp/Alder.jpg", "/tmp/Cove.png")
    sets.ratings = {zebra: 4, birch: 3, alder: 2}
    sets.changed.emit()
    panel.show()
    panel.set_active_path(zebra)
    model = panel.view.model()
    panel.view.setCurrentIndex(model.index(0, 0))

    def displayed_paths() -> tuple[str, ...]:
        return tuple(model.index(row, 0).data(grade_set_panel._PATH_ROLE) for row in range(model.rowCount()))

    def choose(actions: list[QAction], data: str | int) -> None:
        next(action for action in actions if action.data() == data).trigger()

    sort_menu = panel.sort_button.menu()
    choose(sort_menu.actions(), "name")
    assert displayed_paths() == (alder, birch, cove, zebra)
    assert panel.view.currentIndex().data(grade_set_panel._PATH_ROLE) == zebra
    choose(sort_menu.actions(), "name_desc")
    assert displayed_paths() == (zebra, cove, birch, alder)
    choose(sort_menu.actions(), "rating")
    choose(panel._formats.actions(), "JPG")
    choose(panel._ratings.actions(), 3)
    assert displayed_paths() == (zebra,)
    sets.ratings[alder] = 5
    sets.ratings_changed.emit()
    assert displayed_paths() == (alder, zebra)
    sets.ratings[zebra] = 5
    sets.ratings_changed.emit()
    assert displayed_paths() == (zebra, alder)  # Rating ties keep the addition order.
    choose(panel._ratings.actions(), 0)
    assert not displayed_paths() and panel.message.isVisible()
    assert panel.active_path == zebra
    panel._clear_filter_action.trigger()
    assert displayed_paths() == (zebra, alder, birch, cove)
    assert not panel.message.isVisible()
    choose(sort_menu.actions(), "added")
    assert displayed_paths() == sets.paths == (zebra, birch, alder, cove)
