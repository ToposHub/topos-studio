"""D2：回收站对话框 + 面板集合写入/回收站入口（意图直达 transport 语义）。"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QItemSelectionModel  # noqa: E402
from PyQt6.QtWidgets import QMessageBox, QInputDialog  # noqa: E402

from src.app.ui.native_files_panel import NativeFilesPanel  # noqa: E402
from src.app.ui.native_files_trash import TrashDialog  # noqa: E402


def _entries():
    return [
        {"name": "A.mov", "path": "/media/A.mov", "type": "video", "size_bytes": 10},
        {"name": "B.png", "path": "/media/B.png", "type": "image", "size_bytes": 5},
    ]


def _panel(qtbot):
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.set_entries("/media", _entries())
    return panel


def _select(panel, row=0):
    panel.selection.select(
        panel.proxy.index(row, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect
        | QItemSelectionModel.SelectionFlag.Rows)


# ---------------------------------------------------------------------------
# TrashDialog
# ---------------------------------------------------------------------------

_RECORDS = [
    {"id": 3, "item_name": "clip.mov", "original_path": "/media/clip.mov",
     "is_dir": False, "deleted_at": 1700000000.0, "expire_at": 1730000000.0,
     "library_id": "lib"},
    {"id": 5, "item_name": "文件夹", "original_path": "/media/文件夹",
     "is_dir": True, "deleted_at": 1700000000.0, "expire_at": 1730000000.0,
     "library_id": "lib"},
]


def test_trash_dialog_lists_and_emits_intents(qtbot, monkeypatch):
    dialog = TrashDialog()
    qtbot.addWidget(dialog)
    intents = []
    dialog.restore_requested.connect(lambda rid, policy: intents.append(("restore", rid, policy)))
    dialog.purge_requested.connect(lambda rid: intents.append(("purge", rid)))

    dialog.show_items(list(_RECORDS))
    assert dialog.table.rowCount() == 2
    assert dialog.table.item(0, 0).text() == "clip.mov"
    assert dialog.table.item(1, 2).text() == "文件夹"
    assert not dialog.restore_button.isEnabled()  # 未选择不可操作

    dialog.table.selectRow(0)
    assert dialog.restore_button.isEnabled()
    dialog._restore_selected()
    dialog._rename_restore_selected()
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    dialog._purge_selected()
    assert intents == [("restore", 3, "error"), ("restore", 3, "rename"),
                       ("purge", 3)]

    dialog.show_task_result({"operation": "restore", "successes": [],
                             "failures": [{"path": "x", "error": "目标已存在",
                                           "code": "DEST_EXISTS"}]})
    assert "改名恢复" in dialog.summary.text()  # 冲突显式指引替代策略

    dialog.show_items([])
    assert "空" in dialog.summary.text()


def test_trash_dialog_purge_declined_sends_nothing(qtbot, monkeypatch):
    dialog = TrashDialog()
    qtbot.addWidget(dialog)
    dialog.show_items(list(_RECORDS))
    dialog.table.selectRow(1)
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    intents = []
    dialog.purge_requested.connect(intents.append)
    dialog._purge_selected()
    assert intents == []


# ---------------------------------------------------------------------------
# 面板：入口与意图
# ---------------------------------------------------------------------------

def test_panel_trash_button_emits_intent(qtbot):
    panel = _panel(qtbot)
    emitted = []
    panel.trash_requested.connect(lambda: emitted.append(True))
    panel.trash_button.click()
    assert emitted == [True]


def test_panel_collection_create_from_selection(qtbot, monkeypatch):
    panel = _panel(qtbot)
    _select(panel, 0)
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("精选", True)))
    emitted = []
    panel.collection_create_requested.connect(
        lambda name, paths: emitted.append((name, paths)))
    panel._create_collection_from_selection()
    assert emitted == [("精选", ["/media/A.mov"])]


def test_panel_collection_create_without_selection_builds_empty(qtbot, monkeypatch):
    panel = _panel(qtbot)  # 不选择
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("空集", True)))
    emitted = []
    panel.collection_create_requested.connect(
        lambda name, paths: emitted.append((name, paths)))
    panel._create_collection_from_selection()
    assert emitted == [("空集", [])]


def test_panel_collection_rename_and_delete_intents(qtbot, monkeypatch):
    panel = _panel(qtbot)
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("新名", True)))
    renames = []
    panel.collection_rename_requested.connect(
        lambda cid, name: renames.append((cid, name)))
    panel._rename_collection(7, "旧名")
    assert renames == [(7, "新名")]

    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    deletes = []
    panel.collection_delete_requested.connect(deletes.append)
    panel._confirm_delete_collection(7, "旧名")
    assert deletes == [7]


def test_panel_collection_add_requires_selection(qtbot):
    panel = _panel(qtbot)
    emitted = []
    panel.collection_add_members_requested.connect(
        lambda cid, paths: emitted.append((cid, paths)))
    panel._add_selection_to_collection(7, "精选")
    assert emitted == []  # 没选择：拒绝在提交之前
    assert "精选" in panel.meta_status.text()

    _select(panel, 1)
    panel._add_selection_to_collection(7, "精选")
    assert emitted == [(7, ["/media/B.png"])]


def test_panel_collection_view_remove_members_intent(qtbot):
    panel = _panel(qtbot)
    panel._pending_collection_name = "精选"
    panel._pending_collection_id = 7
    panel.show_collection_results({"files": [
        {"name": "A.mov", "path": "/media/A.mov", "type": "video"}]})
    _select(panel, 0)
    emitted = []
    panel.collection_remove_members_requested.connect(
        lambda cid, paths: emitted.append((cid, paths)))
    panel._remove_selection_from_collection()
    assert emitted == [(7, ["/media/A.mov"])]

    # 集合视图的右键菜单包含"从集合移除"
    menu = panel._build_context_menu(panel.icon_view, panel.icon_view.viewport().rect().center())
    labels = [action.text() for action in menu.actions() if action.text()]
    assert any("从集合移除" in label for label in labels)


def test_panel_collections_menu_has_create_entry(qtbot):
    panel = _panel(qtbot)
    panel._collections = [{"id": 1, "name": "既有", "item_count": 2}]
    # 不 exec：拦截 QMenu.exec 捕获菜单内容
    from PyQt6.QtWidgets import QMenu
    original_exec = QMenu.exec
    captured = {}
    QMenu.exec = lambda self, *a, **k: captured.setdefault("menu", self) or None
    try:
        panel.request_collections()
    finally:
        QMenu.exec = original_exec
    menu = captured.get("menu")
    assert menu is not None
    labels = [action.text() for action in menu.actions() if action.text()]
    assert any("新建" in label for label in labels)
    submenus = [action.menu() for action in menu.actions() if action.menu()]
    assert submenus, "每个集合应有子菜单（打开/加入/改名/删除）"
    sub_labels = [a.text() for a in submenus[0].actions() if a.text()]
    assert any("打开" in t for t in sub_labels)
    assert any("改名" in t for t in sub_labels)
    assert any("删除" in t for t in sub_labels)


def test_panel_collection_change_feedback(qtbot):
    panel = _panel(qtbot)
    panel.show_collection_change(True, "已创建集合「X」")
    assert "X" in panel.meta_status.text()
    panel.show_collection_change(False, "集合名不能为空")
    assert "集合名" in panel.meta_status.text()
