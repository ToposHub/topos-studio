"""B04 / U03：图片预览容器与解码契约（真实解码，无磁盘 IO）。"""
from __future__ import annotations

import pytest
from PyQt6.QtCore import QBuffer, QIODevice, QItemSelectionModel, Qt
from PyQt6.QtGui import QColor, QImage, QKeyEvent
from PyQt6.QtWidgets import QWidget

from src.app.ui.native_files_panel import NativeFilesPanel
from src.app.ui.native_files_preview import NativeFilesPreview, decode_preview


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


def test_panel_routes_video_to_existing_player(qtbot):
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.set_entries("/media", [
        {"name": "C.mov", "path": "/media/C.mov", "type": "video", "size_bytes": 20},
    ])
    _select_path(panel, "/media/C.mov")
    with qtbot.waitSignal(panel.preview_external_requested) as blocker:
        panel.preview_selected()
    assert blocker.args == ["/media/C.mov"]
    assert not panel._preview.is_open


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


def test_capabilities_disable_metadata_controls_with_reason(qtbot):
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.apply_capabilities([
        {"key": "metadata.patch", "available": False, "reason": "只读挂载，标记不可写"},
        {"key": "preview.image", "available": True},
    ])
    assert not panel.rating_combo.isEnabled()
    assert not panel.tag_edit.isEnabled()
    assert "只读挂载" in panel.meta_status.text()
    panel.apply_capabilities([{"key": "metadata.patch", "available": True}])
    assert panel.rating_combo.isEnabled()
