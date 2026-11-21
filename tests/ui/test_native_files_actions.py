"""F10 / B05 / B11：原生面板的文件任务、全库搜索与集合结果。

只验证"面板发出真实意图 / 显示真实结果"，不替服务端宣布成功。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QPoint, QSettings
from PyQt6.QtWidgets import QMenu
from pytestqt.qtbot import QtBot

from src.app.ui import native_files_panel as panel_module
from src.app.ui.native_files_panel import NativeFilesPanel
from src.app.ui.native_files_prefs import NativeFilesPreferences
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


def test_rename_uses_input_dialog_and_emits_task(panel, monkeypatch):
    captured = []
    panel.file_task_requested.connect(lambda op, targets, options: captured.append((op, targets, options)))
    panel.selection.select(panel.proxy.index(0, 0),
                           panel.selection.SelectionFlag.ClearAndSelect
                           | panel.selection.SelectionFlag.Rows)
    monkeypatch.setattr(panel_module.QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("renamed.mov", True)))
    panel.request_rename()
    assert captured[-1][0] == "rename"
    assert captured[-1][2]["options"]["new_name"] == "renamed.mov"


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


def test_libraries_list_populates_and_navigates(panel):
    navigated = []
    panel.directory_requested.connect(lambda path: navigated.append(path))
    panel.show_libraries([{"path": "/Users/creator/Media", "name": "Media"}])
    assert panel.library_list.count() == 1
    row = panel.library_list.item(0)
    panel._open_list_path(row)
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
