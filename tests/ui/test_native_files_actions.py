"""F10 / B05 / B11：原生面板的文件任务、全库搜索与集合结果。

只验证"面板发出真实意图 / 显示真实结果"，不替服务端宣布成功。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QMimeData, QPoint, QPointF, QSettings, Qt, QUrl
from PyQt6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PyQt6.QtWidgets import QApplication, QAbstractItemView, QLineEdit, QMenu
from pytestqt.qtbot import QtBot

from src.app.ui import native_files_panel as panel_module
from src.app.ui.native_files_panel import NativeFilesPanel
from src.app.ui.native_files_locations import MountedVolume
from src.app.ui.native_files_prefs import NativeFilesPreferences
from src.app.ui.native_files_transfer import cut_token, local_file_paths
from src.features.files.contracts import Entry


def _entry(path: str, name: str, *, is_dir: bool = False, kind: str = "video") -> Entry:
    return Entry(path=path, name=name, kind="dir" if is_dir else kind, is_dir=is_dir,
                 size_bytes=1024, mtime_ts=time.time())


@pytest.fixture()
def panel(qtbot):
    widget = NativeFilesPanel(home_path="/Users/creator", preferences=None)
    qtbot.addWidget(widget)
    widget.set_entries("/Users/creator/Media", [
        _entry("/Users/creator/Media/a.mov", "a.mov"),
        _entry("/Users/creator/Media/b.mov", "b.mov"),
        _entry("/Users/creator/Media/Shots", "Shots", is_dir=True),
    ])
    return widget


def test_delete_key_requests_trash_for_selection(panel):
    captured: list[tuple] = []
    panel.file_task_requested.connect(lambda op, targets, options: captured.append((op, targets, options)))
    panel.selection.select(panel.proxy.index(0, 0),
                           panel.selection.SelectionFlag.ClearAndSelect
                           | panel.selection.SelectionFlag.Rows)
    panel.request_trash_selection()
    assert captured
    operation, targets, options = captured[-1]
    assert operation == "trash"
    assert len(targets) == 1
    assert options["policy"] == "error"


def test_delete_without_selection_does_not_call_service(panel):
    captured = []
    panel.file_task_requested.connect(lambda *args: captured.append(args))
    panel.selection.clear()
    panel.request_trash_selection()
    assert captured == []
    assert "先选择" in panel.meta_status.text()


@pytest.mark.parametrize("trigger", ["caption", "menu", "f2", "details"])
def test_rename_edits_filename_in_place_and_emits_task(panel, qtbot, trigger):
    panel.resize(1100, 700)
    panel.show()
    panel.view_combo.setCurrentIndex(panel.view_combo.findData("list" if trigger == "details" else "adaptive"))
    view = panel.pages.currentWidget()
    panel.activateWindow()
    view.setFocus()
    index = panel.proxy.mapFromSource(panel.model.index(0, 0))
    panel.selection.select(index, panel.selection.SelectionFlag.ClearAndSelect
                           | panel.selection.SelectionFlag.Rows)
    if trigger == "menu":
        menu = panel._build_context_menu(view, view.visualRect(index).center())
        action = next(action for action in menu.actions()
                      if action.text() == panel_module._tr("rename_title", "重命名"))
        action.trigger()
    elif trigger == "f2":
        qtbot.keyClick(view, Qt.Key.Key_F2)
    else:
        name_rect = view.itemDelegate().name_rect(view.visualRect(index))
        qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=name_rect.center())
    qtbot.waitUntil(lambda: view.findChild(QLineEdit, "filesNameEditor") is not None)
    editor = view.findChild(QLineEdit, "filesNameEditor")
    assert editor.text() == "a.mov"
    editor.setText("renamed.mov")
    with qtbot.waitSignal(panel.file_task_requested) as task:
        qtbot.keyClick(editor, Qt.Key.Key_Return)
    assert task.args == ["rename", ["/Users/creator/Media/a.mov"],
                         {"policy": "error", "options": {"new_name": "renamed.mov"}}]
    assert panel.model.entries[0].name == "a.mov"


def test_inline_rename_escape_discards_draft(panel, qtbot):
    panel.show()
    panel.rename_visible_file("/Users/creator/Media/a.mov")
    view = panel.pages.currentWidget()
    editor = view.findChild(QLineEdit, "filesNameEditor")
    editor.setText("draft.mov")
    with qtbot.assertNotEmitted(panel.file_task_requested):
        qtbot.keyClick(editor, Qt.Key.Key_Escape)
    assert panel.model.entries[0].name == "a.mov"


def test_new_folder_emits_mkdir_in_current_directory(panel):
    captured = []
    panel.file_task_requested.connect(lambda op, targets, options: captured.append((op, targets, options)))
    panel_module.QInputDialog.getText = staticmethod(lambda *a, **k: ("New Bin", True))
    panel.request_new_folder()
    assert captured[-1][0] == "mkdir"
    assert captured[-1][2]["options"]["name"] == "New Bin"


def test_task_result_reports_partial_failures(panel):
    panel.apply_task_result({
        "operation": "trash", "successes": ["/a.mov"],
        "failures": [{"source": "/b.mov", "error": "目标已存在"}],
        "cancelled": False,
    })
    text = panel.meta_status.text()
    assert "失败 1" in text
    assert "目标已存在" in text


def test_library_results_replace_rows_and_report_truncation(panel):
    payload = {
        "files": [
            {"name": "hit.mov", "path": "/library/hit.mov", "type": "video",
             "size_bytes": 10, "mtime": time.time(), "rating": 4},
            {"name": "hit2.mov", "path": "/library/hit2.mov", "type": "video",
             "size_bytes": 10, "mtime": time.time(), "rating": 2},
        ],
        "total": 7, "truncated": True,
    }
    panel.show_library_results(payload)
    assert panel.model.rowCount() == 2
    assert "7" in panel.breadcrumb_button.text()
    assert "截断" in panel.status.text()


def test_collection_results_show_members(panel):
    panel._pending_collection_name = "Best of 2026"
    panel.show_collection_results({
        "files": [{"name": "keep.mov", "path": "/library/keep.mov", "type": "video"}],
        "total": 1, "truncated": False,
    })
    assert panel.model.rowCount() == 1
    assert "Best of 2026" in panel.breadcrumb_button.text()


def test_request_collections_emits_only_when_cache_empty(panel):
    calls = []
    panel.collections_requested.connect(lambda: calls.append(True))
    panel.request_collections()
    assert calls == [True]


def test_paste_uses_clipboard_files_and_no_overwrite(panel):
    from PyQt6.QtCore import QMimeData, QUrl
    from PyQt6.QtWidgets import QApplication

    captured = []
    panel.file_task_requested.connect(lambda op, targets, options: captured.append((op, targets, options)))
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile("/Users/creator/Inbox/clip.mov")])
    QApplication.clipboard().setMimeData(mime)
    panel.paste_files()
    operation, targets, options = captured[-1]
    assert operation == "copy"
    assert targets == ["/Users/creator/Inbox/clip.mov"]
    assert options["policy"] == "rename"
    assert options["options"]["destination"] == "/Users/creator/Media"


def test_paste_without_urls_reports_and_does_not_call_service(panel):
    from PyQt6.QtWidgets import QApplication

    captured = []
    panel.file_task_requested.connect(lambda *args: captured.append(args))
    QApplication.clipboard().clear()
    panel.paste_files()
    assert captured == []
    assert "剪贴板" in panel.meta_status.text()


def _drop(view: QAbstractItemView, point: QPoint, mime: QMimeData, *, source: QAbstractItemView | None = None,
          modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> Qt.DropAction:
    class Drop(QDropEvent):
        def source(self) -> QAbstractItemView | None:
            return source

    actions = Qt.DropAction.CopyAction if source is not None else Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
    button = Qt.MouseButton.LeftButton
    QApplication.sendEvent(view.viewport(), QDragEnterEvent(point, actions, mime, button, modifiers))
    QApplication.sendEvent(view.viewport(), QDragMoveEvent(point, actions, mime, button, modifiers))
    drop = Drop(QPointF(point), actions, mime, button, modifiers)
    QApplication.sendEvent(view.viewport(), drop)
    return drop.dropAction() if drop.isAccepted() else Qt.DropAction.IgnoreAction


def test_drop_on_sidebar_folders_home_and_disk(panel: NativeFilesPanel, qtbot: QtBot) -> None:
    panel.resize(1000, 650)
    panel.show()
    library = "/media/footage_01"
    pictures = library + "/Pictures"
    disk = "/Volumes/Archive"
    panel.show_libraries([{"path": library, "name": "footage_01", "library_id": "test"}])
    tree = panel.locations
    library_index = tree.model().index(0, 0)
    tree.set_children(library, [{"path": pictures, "name": "Pictures"}])
    tree.expand(library_index)
    tree.set_volumes((MountedVolume(disk, "Archive", b"disk"),))
    disk_group = next(tree.model().index(row, 0) for row in range(tree.model().rowCount())
                      if tree.model().index(row, 0).data() in {"磁盘", "Drives"})
    tree.expand(disk_group)
    calls = []
    panel.file_task_requested.connect(lambda *args: calls.append(args))
    mime = QMimeData()
    source = "/media/Desktop/shot #1.mov"
    mime.setUrls([QUrl.fromLocalFile(source), QUrl.fromLocalFile(source), QUrl("https://example.test/shot.mov")])
    for index, destination in ((tree.model().index(0, 0, library_index), pictures),
                               (tree.home_item.index(), panel.home_path),
                               (tree.model().index(0, 0, disk_group), disk)):
        tree.scrollTo(index)
        qtbot.waitUntil(lambda: tree.visualRect(index).isValid())
        assert _drop(tree, tree.visualRect(index).center(), mime) == Qt.DropAction.CopyAction
        assert calls[-1] == ("copy", [source], {"policy": "rename", "options": {"destination": destination}})

    assert _drop(tree, tree.visualRect(disk_group).center(), mime) == Qt.DropAction.IgnoreAction
    assert len(calls) == 3


@pytest.mark.parametrize("layout", ("adaptive", "masonry", "grid", "list"))
def test_gallery_drop_copies_foreign_files_and_moves_own_files_with_shift(
    panel: NativeFilesPanel, layout: str,
) -> None:
    panel.resize(1000, 650)
    panel.show()
    panel.view_combo.setCurrentIndex(panel.view_combo.findData(layout))
    view = panel.pages.currentWidget()
    index = next(panel.proxy.index(row, 0) for row in range(panel.proxy.rowCount())
                 if panel.proxy.index(row, 0).data(panel_module.FilesEntryModel.ENTRY_ROLE).is_dir)
    point = view.visualRect(index).center()
    calls = []
    panel.file_task_requested.connect(lambda *args: calls.append(args))
    mime = QMimeData()
    path = "/tmp/Desktop/clip.mov"
    mime.setUrls([QUrl.fromLocalFile(path)])
    assert _drop(view, point, mime, modifiers=Qt.KeyboardModifier.ShiftModifier) == Qt.DropAction.CopyAction
    assert _drop(view, point, mime, source=view, modifiers=Qt.KeyboardModifier.ShiftModifier) == Qt.DropAction.CopyAction
    assert [op for op, _, _ in calls] == ["copy", "move"]
    assert all(args[1:] == ([path], {"policy": "rename", "options": {"destination": "/Users/creator/Media/Shots"}})
               for args in calls)

    panel.show_library_results({"files": [], "total": 0})
    assert _drop(view, point, mime) == Qt.DropAction.IgnoreAction
    assert len(calls) == 2


def test_cut_and_sidebar_paste_move_only_selected_files(panel: NativeFilesPanel, qtbot: QtBot) -> None:
    path = "/Users/creator/Media/a.mov"
    index = next(panel.proxy.index(row, 0) for row in range(panel.proxy.rowCount())
                 if panel.proxy.index(row, 0).data() == "a.mov")
    panel.selection.select(index, panel.selection.SelectionFlag.Select | panel.selection.SelectionFlag.Rows)
    widget = panel.pages.currentWidget()
    qtbot.keyClick(widget, Qt.Key.Key_X, modifier=Qt.KeyboardModifier.ControlModifier)
    token = cut_token(QApplication.clipboard().mimeData())
    calls = []
    panel.file_task_requested.connect(lambda *args: calls.append(args))
    panel.locations.setCurrentIndex(panel.locations.home_item.index())
    qtbot.keyClick(panel.locations, Qt.Key.Key_V, modifier=Qt.KeyboardModifier.ControlModifier)
    assert token
    assert calls == [("move", [path], {"policy": "rename",
                                      "options": {"destination": panel.home_path, "cut_token": token}})]
    panel.apply_task_result({"operation": "move", "cut_token": token,
                             "successes": [{"path": path, "to": panel.home_path + "/a.mov"}]})
    assert not local_file_paths(QApplication.clipboard().mimeData())


def test_libraries_list_populates_and_navigates(panel):
    navigated = []
    panel.directory_requested.connect(lambda path: navigated.append(path))
    panel.show_libraries([{"path": "/Users/creator/Media", "name": "Media", "library_id": "media"}])
    tree = panel.locations
    index = tree.item_model.index(0, 0)
    assert tree.library_root(index)
    tree.clicked.emit(index)
    assert navigated[-1] == "/Users/creator/Media"


def test_pin_current_directory_emits_path(panel):
    pinned = []
    panel.library_pin_requested.connect(lambda path: pinned.append(path))
    panel.pin_current_directory()
    assert pinned == ["/Users/creator/Media"]


def test_tools_menu_opens_recent_location_and_starts_scan(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefs = NativeFilesPreferences(QSettings(str(tmp_path / "prefs.ini"), QSettings.Format.IniFormat))
    path = "/Users/creator/Design & Photos"
    prefs.remember_location(path)
    panel = NativeFilesPanel(home_path="/Users/creator", preferences=prefs)
    qtbot.addWidget(panel)

    def choose_tools(menu: QMenu, _position: QPoint) -> None:
        recent = next(action.menu() for action in menu.actions() if action.text() == "最近访问")
        location = recent.actions()[0]
        assert location.text() == "Design && Photos"
        assert location.toolTip() == path
        location.trigger()
        next(action for action in menu.actions() if action.text() == "开始索引").trigger()

    monkeypatch.setattr(panel_module.QMenu, "exec", choose_tools)
    with qtbot.waitSignal(panel.directory_requested) as requested, qtbot.waitSignal(panel.scan_requested):
        panel.more_button.click()
    assert requested.args == [path]


def test_scan_status_toggles_button(panel, qtbot):
    assert panel.scan_label.isHidden()
    assert panel.scan_button.isHidden()
    panel.show_scan_status({"running": True, "phase": "枚举", "scanned": 3, "total": 9})
    assert not panel.scan_label.isHidden()
    assert not panel.scan_button.isHidden()
    assert "取消" in panel.scan_button.text()
    assert "3/9" in panel.scan_label.text()
    with qtbot.waitSignal(panel.scan_cancel_requested):
        panel.scan_button.click()
    panel.show_scan_status({"running": False, "phase": "完成",
                            "last_result": {"added": 4}})
    assert panel.scan_button.isHidden()
    assert "开始索引" in panel.scan_button.text()
    assert "新增 4" in panel.scan_label.text()
    panel.show_scan_status({"running": True, "phase": "索引中"})
    panel.scan_failed("读取失败")
    assert panel.scan_button.isHidden()
    assert "读取失败" in panel.scan_label.toolTip()
    with qtbot.waitSignal(panel.scan_requested):
        panel.toggle_scan()
