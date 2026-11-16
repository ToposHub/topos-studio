"""F13/F14/F15/F16：批量改名预览、悬停胶片条、并排比较、旗标筛选。"""
from __future__ import annotations

import time

import pytest
from PyQt6.QtGui import QImage

from src.app.ui.native_files_batch_rename import BatchRenameDialog
from src.app.ui.native_files_compare import CompareDialog
from src.app.ui.native_files_panel import NativeFilesPanel
from src.features.files.contracts import Entry, TechnicalMetadata, UserMetadata


def _entry(path: str, name: str, *, kind: str = "image", flag: int = 0,
           duration: float = 0.0) -> Entry:
    return Entry(path=path, name=name, kind=kind, is_dir=False, size_bytes=1024,
                 mtime_ts=time.time(), technical=TechnicalMetadata(duration_seconds=duration),
                 user=UserMetadata(flag=flag))


@pytest.fixture()
def panel(qtbot):
    widget = NativeFilesPanel(home_path="/Users/creator", preferences=None)
    qtbot.addWidget(widget)
    widget.set_entries("/Users/creator/Media", [
        _entry("/Users/creator/Media/a.jpg", "a.jpg", flag=1),
        _entry("/Users/creator/Media/b.jpg", "b.jpg", flag=2),
        _entry("/Users/creator/Media/c.mov", "c.mov", kind="video", duration=12.0),
    ])
    return widget


def test_flag_filter_keeps_rejected_only(panel):
    panel.flag_combo.setCurrentIndex(2)  # 已拒收
    names = [panel.proxy.index(row, 0).data(panel_module_role()).name
             for row in range(panel.proxy.rowCount())]
    assert names == ["b.jpg"]


def panel_module_role():
    from src.app.ui.native_files_panel import FilesEntryModel
    return FilesEntryModel.ENTRY_ROLE


def test_batch_rename_action_offered_for_two_files(panel):
    captured = []
    panel.batch_rename_requested.connect(lambda paths: captured.append(paths))
    panel.selection.select(panel.proxy.index(0, 0),
                           panel.selection.SelectionFlag.ClearAndSelect
                           | panel.selection.SelectionFlag.Rows)
    panel.selection.select(panel.proxy.index(1, 0),
                           panel.selection.SelectionFlag.Select
                           | panel.selection.SelectionFlag.Rows)
    menu = panel._build_context_menu(
        panel.icon_view, panel.icon_view.visualRect(panel.proxy.index(1, 0)).center())
    labels = [action.text() for action in menu.actions()]
    assert any("批量改名" in label for label in labels)
    assert any("并排比较" in label for label in labels)


def test_hover_delays_strip_request_for_video(panel, qtbot):
    captured = []
    panel.strip_requested.connect(lambda path, duration: captured.append((path, duration)))
    panel.hover_index(panel.proxy.index(2, 0))
    assert captured == []
    qtbot.wait(420)
    assert captured == [("/Users/creator/Media/c.mov", 12.0)]
    panel.clear_hover()


def test_hover_non_video_clears(panel):
    panel.hover_index(panel.proxy.index(0, 0))
    assert panel._hover_path == ""


def test_batch_rename_dialog_preview_and_conflict(qtbot):
    dialog = BatchRenameDialog(["/a.jpg", "/b.jpg"])
    qtbot.addWidget(dialog)
    dialog._preview()
    session = {"session_id": dialog._preview_request_id}
    dialog.show_preview({**session, "plan": [
        {"src_name": "a.jpg", "dst_name": "a_001.jpg", "conflict": False},
        {"src_name": "b.jpg", "dst_name": "b_001.jpg", "conflict": True},
    ], "errors": []})
    assert dialog.table.rowCount() == 2
    assert dialog.execute_button.isEnabled()
    assert "1 项冲突" in dialog.summary.text()
    dialog._preview()
    session = {"session_id": dialog._preview_request_id}
    dialog.show_preview({**session, "plan": [
        {"src_name": "a.jpg", "dst_name": "a_001.jpg", "conflict": False},
    ], "errors": []})
    assert dialog.execute_button.isEnabled()


def test_batch_rename_dialog_result_lists_failures(qtbot):
    dialog = BatchRenameDialog(["/a.jpg"])
    qtbot.addWidget(dialog)
    dialog.show_result({"success": True, "processed": 1,
                        "session_id": dialog.session_id,
                        "errors": ["x.jpg: 只读"]})
    assert "已处理 1 项" in dialog.summary.text()
    assert "只读" in dialog.summary.text()


def test_compare_dialog_loads_and_zooms(qtbot):
    dialog = CompareDialog(["/a.jpg", "/b.jpg"])
    qtbot.addWidget(dialog)
    asked = []
    dialog.images_requested.connect(lambda paths: asked.append(paths))
    dialog.show()
    assert asked == [["/a.jpg", "/b.jpg"]]
    image = QImage(8, 6, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
    buffer = QBuffer()
    buffer.setData(QByteArray())
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    dialog.set_image("/a.jpg", bytes(buffer.data()))
    dialog._set_zoom(2.0)
    assert dialog.summary.text().find("200%") >= 0
    dialog.set_error("/b.jpg", "不支持的图片格式")
    assert dialog._labels["/b.jpg"].text() == "不支持的图片格式"
