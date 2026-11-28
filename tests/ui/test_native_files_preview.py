"""B04 / U03：图片预览容器与解码契约（真实解码，无磁盘 IO）。"""
from __future__ import annotations

import pytest

from PyQt6.QtCore import QBuffer, QEvent, QIODevice, QItemSelectionModel, QObject, QPoint, QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QWheelEvent
from PyQt6.QtWidgets import QApplication, QPushButton, QWidget

from src.app.ui import native_files_panel
from src.app.ui.native_files_panel import NativeFilesPanel
from src.app.ui.native_files_preview import NativeFilesPreview, decode_preview
from src.features.files.contracts import Capability


def _png_bytes(width: int = 40, height: int = 24) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor("#3a7bd5"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()
    return data


def _select_path(panel: NativeFilesPanel, path: str) -> None:
    for row in range(panel.proxy.rowCount()):
        index = panel.proxy.index(row, 0)
        if index.data(Qt.ItemDataRole.UserRole + 1) and getattr(
                index.data(panel.model.ENTRY_ROLE), "path", None) == path:
            panel.selection.select(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows)
            return
    raise AssertionError(f"row not found: {path}")


def test_decode_preview_reads_real_png_and_reports_empty():
    image, error = decode_preview(_png_bytes())
    assert error == ""
    assert (image.width(), image.height()) == (40, 24)
    empty, empty_error = decode_preview(b"")
    assert empty.isNull()
    assert "没有可预览内容" in empty_error


def test_decode_preview_rejects_garbage_with_reason():
    image, error = decode_preview(b"not-an-image")
    assert image.isNull()
    assert error  # 必须给出原因，不能静默空白


def test_library_lightbox_file_info_hides_and_returns_on_next_photo(qtbot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.resize(960, 620)
    parent.show()
    preview = NativeFilesPreview(parent)
    preview.open_for("first.jpg", can_grade=True)
    assert preview._info_timer.interval() == 2000
    assert preview.title.isVisible() and preview.meta.isVisible()
    viewport = preview.scroll.geometry()
    preview._info_timer.start(20)
    qtbot.waitUntil(preview._file_info.isHidden)
    assert preview.scroll.geometry() == viewport
    assert not preview.grade_button.isVisible() and preview.close_button.isVisible()

    def move_pointer(y: int) -> None:
        point = preview.rect().topLeft()
        point.setY(y)
        at = QPointF(point)
        QApplication.sendEvent(preview, QMouseEvent(QEvent.Type.MouseMove, at,
                                                    QPointF(preview.mapToGlobal(point)), Qt.MouseButton.NoButton,
                                                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))

    move_pointer(4)
    assert preview.title.isVisible() and preview.grade_button.isVisible() and preview.fit_button.isVisible()
    assert not preview._info_timer.isActive()
    move_pointer(100)
    assert preview._info_timer.isActive()
    preview._info_timer.start(20)
    qtbot.waitUntil(preview._file_info.isHidden)
    assert preview.grade_button.isHidden()
    preview.open_for("second.jpg", can_grade=True)
    assert preview.title.isVisible() and preview.meta.isVisible()
    preview.close_preview()
    assert not preview._info_timer.isActive()


def test_lightbox_places_navigation_arrows_on_each_side(qtbot) -> None:
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.resize(960, 620)
    parent.show()
    preview = NativeFilesPreview(parent)
    preview.open_for("photo.jpg")
    assert preview.prev_button.isVisible() and preview.next_button.isVisible()
    assert preview.prev_button.geometry().center().y() == preview.next_button.geometry().center().y()
    assert abs(preview.prev_button.geometry().center().y() - preview.rect().center().y()) <= 1
    assert preview.prev_button.x() < 30
    assert preview.next_button.geometry().right() > preview.width() - 30
    assert preview._file_info.geometry().top() < 30
    assert preview._file_info.width() <= 360
    for button, signal in ((preview.prev_button, preview.previous_requested),
                           (preview.next_button, preview.next_requested)):
        assert button.text() == "" and not button.icon().isNull()
        with qtbot.waitSignal(signal):
            qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    preview.set_steppers_enabled(False)
    assert preview.prev_button.isHidden() and preview.next_button.isHidden()


def test_lightbox_wheel_preserves_cursor_anchor_and_middle_drag_pans(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.resize(900, 620)
    parent.show()
    preview = NativeFilesPreview(parent)
    preview.open_for("photo.jpg")
    image = QImage(880, 600, QImage.Format.Format_RGB32)
    image.fill(QColor("#a65c65"))
    preview.show_image("photo.jpg", image)
    viewport = preview.scroll.viewport()
    point = QPoint(340, 225)
    anchor = preview._image_fraction(point)
    source_key = preview.body.pixmap().cacheKey()
    initial_width = preview.body.width()
    for _ in range(3):
        QApplication.sendEvent(viewport, QWheelEvent(
            QPointF(point), QPointF(viewport.mapToGlobal(point)), QPoint(), QPoint(0, 120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))
    assert preview.body.width() > initial_width
    assert preview.body.pixmap().cacheKey() == source_key
    assert preview._image_fraction(point).x() == pytest.approx(anchor.x(), abs=0.002)
    assert preview._image_fraction(point).y() == pytest.approx(anchor.y(), abs=0.002)
    horizontal, vertical = preview.scroll.horizontalScrollBar(), preview.scroll.verticalScrollBar()
    scroll_before = QPoint(horizontal.value(), vertical.value())
    delta = QPoint(26, 18)
    qtbot.mousePress(viewport, Qt.MouseButton.MiddleButton, pos=point)
    QApplication.sendEvent(viewport, QMouseEvent(
        QEvent.Type.MouseMove, QPointF(point + delta), QPointF(viewport.mapToGlobal(point + delta)),
        Qt.MouseButton.NoButton, Qt.MouseButton.MiddleButton, Qt.KeyboardModifier.NoModifier))
    qtbot.mouseRelease(viewport, Qt.MouseButton.MiddleButton, pos=point + delta)
    assert QPoint(horizontal.value(), vertical.value()) == scroll_before - delta
    assert preview._pan_last is None
    preview.toggle_fit()
    assert preview.body.width() == initial_width
    assert horizontal.value() == vertical.value() == 0
    preview.close_preview()


def test_preview_overlay_fit_toggle_and_escape(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.resize(900, 600)
    preview = NativeFilesPreview(parent)
    preview.open_for("/m/a.png", "a.png")
    assert preview.is_open
    preview.show_image("/m/a.png", decode_preview(_png_bytes())[0])
    assert "40 × 24" in preview.meta.text()
    preview.toggle_fit()
    assert "原尺寸" in preview.meta.text()
    with qtbot.waitSignal(preview.closed) as blocker:
        event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        preview.keyPressEvent(event)
    assert blocker.args == []
    assert not preview.is_open


def test_video_mode_reuses_overlay_and_resets_to_image(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.resize(900, 600)
    preview = NativeFilesPreview(parent)
    preview.open_video("/m/a.mov", "a.mov")
    assert preview.is_open
    assert preview.is_video
    assert preview.close_button.isVisibleTo(preview)
    assert preview.fit_button.isHidden()
    assert not preview.video_slot.isHidden()
    preview.open_for("/m/a.png", "a.png")
    assert not preview.is_video
    assert not preview._header_widget.isHidden()
    with qtbot.waitSignal(preview.closed):
        preview.close_preview()


def test_panel_space_on_image_requests_preview_and_ignores_stale_bytes(qtbot):
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.set_entries("/media", [
        {"name": "B.png", "path": "/media/B.png", "type": "image", "size_bytes": 20},
    ])
    _select_path(panel, "/media/B.png")
    with qtbot.waitSignal(panel.preview_requested) as blocker:
        panel.preview_selected()
    assert blocker.args == ["/media/B.png"]
    assert panel._preview.is_open
    # 其他路径的字节到达时必须被丢弃，不能把上一张图贴到新会话上
    panel.show_preview_bytes("/media/other.png", _png_bytes())
    assert panel._preview._image is None
    panel.show_preview_bytes("/media/B.png", _png_bytes())
    qtbot.waitUntil(lambda: panel._preview._image is not None, timeout=4000)
    assert panel._preview._image.width() == 40


def test_lightbox_paints_cached_card_then_loads_fit_and_original(qtbot, monkeypatch, tmp_path):
    import os

    from src.features.files.services.thumbnail_cache import GradedThumbnailStamp

    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    payload = _png_bytes(2200, 1200)
    first.write_bytes(payload)
    second.write_bytes(payload)
    revision = GradedThumbnailStamp.revision(str(first))
    stat = os.stat

    def no_preview_stat(path, *args, **kwargs):
        assert str(path) != str(first), "The lightbox must not probe the preview path"
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", no_preview_stat)
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.resize(900, 620)
    panel.show()
    panel.show_libraries([{"path": str(tmp_path), "library_id": "test-preview"}])
    panel.set_entries(str(tmp_path), [
        {"name": path.name, "path": str(path), "type": "image"}
        for path in (first, second)
    ])
    panel.set_thumbnail(str(first), _png_bytes())
    requested = []
    panel.preview_requested.connect(requested.append)

    assert panel.open_path_native(str(first))
    preview = panel._preview
    assert not preview.body.pixmap().isNull()
    assert preview._image is None and preview._decode_pending
    panel.show_preview_bytes(str(first), payload, revision)
    qtbot.waitUntil(lambda: preview._image is not None)
    assert preview._image.width() < 2200
    assert not preview._has_detail

    preview.toggle_fit()
    assert requested == [str(first), str(first)]
    assert not preview.body.pixmap().isNull()
    panel.preview_load_failed(str(first), "detail failed")
    assert preview._fit and not preview.body.pixmap().isNull()
    preview.toggle_fit()
    assert requested == [str(first), str(first), str(first)]
    panel.show_preview_bytes(str(first), payload)
    qtbot.waitUntil(lambda: preview._has_detail)
    assert preview._image.width() == 2200

    preview.close_preview()
    assert panel.open_path_native(str(first))
    assert len(requested) == 4
    assert panel.preview_cache_revisions(str(first)) == (revision,)
    panel.show_preview_bytes(str(first), None, revision)
    assert preview._image is not None and not preview._decode_pending
    panel.preview_load_failed(str(first), "late error")
    assert "late error" not in preview.meta.text()
    first.write_bytes(_png_bytes(1800, 1200))
    assert panel.open_path_native(str(first))
    assert requested[-1] == str(first) and len(requested) == 5
    assert preview._image is None


def test_panel_routes_video_to_native_preview(qtbot, monkeypatch):
    played = []

    class FakeHost(QObject):
        closed = pyqtSignal()
        prev_requested = pyqtSignal()
        next_requested = pyqtSignal()
        failed = pyqtSignal(str)
        notice = pyqtSignal(str)

        def __init__(self, container, parent=None, *, lightbox=False):
            super().__init__(parent)
            assert lightbox
            self.container = container
            self.player_button = QPushButton(container)
            self.player_button.clicked.connect(lambda: played.append("activated"))

        def set_lightbox_tools(self, visible):
            pass

        def play(self, path):
            played.append(path)
            self.player_button.show()
            self.player_button.setFocus()

        def stop(self):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr(native_files_panel, "NativePlayerHost", FakeHost)
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.set_entries("/media", [
        {"name": "C.mov", "path": "/media/C.mov", "type": "video", "size_bytes": 20},
    ])
    _select_path(panel, "/media/C.mov")
    external = []
    panel.preview_external_requested.connect(external.append)
    panel.preview_selected()
    assert played == ["/media/C.mov"]
    assert external == []
    assert panel._preview.is_open
    assert panel._preview.is_video
    with qtbot.waitSignal(panel.preview_closed):
        qtbot.keyClick(panel._video_host.player_button, Qt.Key.Key_Space)
    assert not panel._preview.is_open
    assert played == ["/media/C.mov"]


def test_preview_steps_only_between_images(qtbot):
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.set_entries("/media", [
        {"name": "A.png", "path": "/media/A.png", "type": "image"},
        {"name": "B.mov", "path": "/media/B.mov", "type": "video"},
        {"name": "C.png", "path": "/media/C.png", "type": "image"},
    ])
    _select_path(panel, "/media/A.png")
    panel.preview_selected()
    with qtbot.waitSignal(panel.preview_requested) as blocker:
        panel._step_preview(1)
    assert blocker.args == ["/media/C.png"]
    assert panel._preview_path == "/media/C.png"


@pytest.mark.parametrize(("name", "kind"), [("A.png", "image"), ("A.docx", "doc")])
def test_space_opens_and_closes_selected_file_lightbox(qtbot, name, kind):
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.show()
    path = f"/media/{name}"
    panel.set_entries("/media", [{"name": name, "path": path, "type": kind}])
    _select_path(panel, path)

    qtbot.keyClick(panel.pages.currentWidget(), Qt.Key.Key_Space)
    assert panel._preview.is_open
    assert panel._preview.current_path == path
    with qtbot.waitSignal(panel.preview_closed):
        qtbot.keyClick(panel._preview, Qt.Key.Key_Space)
    assert not panel._preview.is_open


def test_preview_closed_signal_cancels_session(qtbot):
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.set_entries("/media", [{"name": "A.png", "path": "/media/A.png", "type": "image"}])
    _select_path(panel, "/media/A.png")
    panel.preview_selected()
    with qtbot.waitSignal(panel.preview_closed):
        panel.close_preview()
    assert panel._preview_path == ""
    assert not panel._preview.is_open


@pytest.mark.parametrize("capability", [
    {"key": "metadata.patch", "available": False, "reason": "只读挂载，标记不可写"},
    Capability(key="metadata.patch", available=False, reason="只读挂载，标记不可写"),
])
def test_capabilities_disable_metadata_controls_with_reason(qtbot, capability):
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.apply_capabilities([
        capability,
        {"key": "preview.image", "available": True},
    ])
    assert not panel.rating_buttons[0].isEnabled()
    assert not panel.tag_edit.isEnabled()
    assert "只读挂载" in panel.meta_status.text()
    panel.apply_capabilities([{"key": "metadata.patch", "available": True}])
    assert not panel.rating_buttons[0].isEnabled()  # 能力恢复后仍需有效选区
    panel.set_entries("/media", [
        {"name": "A.png", "path": "/media/A.png", "type": "image"},
    ])
    _select_path(panel, "/media/A.png")
    assert panel.rating_buttons[0].isEnabled()
