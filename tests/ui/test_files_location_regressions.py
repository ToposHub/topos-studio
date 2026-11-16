"""NR-02 回归：Location/查询/标记/任务状态一致性（FR-08/09/11/12/16）。

面板级：位置身份、虚拟位置拒绝目录写入、防抖、能力门控、快照回显；
transport 级：在途搜索作废、多任务轮询互不覆盖。不启动真实 HTTP——
QNetworkAccessManager 用假 reply 替身。
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtCore import QItemSelectionModel

pytest.importorskip("PyQt6.QtWidgets")

from src.app.ui.native_files_panel import NativeFilesPanel  # noqa: E402
from src.app.ui.native_files_transport import NativeFilesTransport  # noqa: E402


def _dir_entries():
    return [
        {"name": "A.mov", "path": "/media/A.mov", "type": "video", "size_bytes": 10,
         "tags": "interview"},
        {"name": "B.png", "path": "/media/B.png", "type": "image", "size_bytes": 5},
    ]


def _search_payload():
    return {"files": [
        {"name": "sunset.mov", "path": "/media/sunset.mov", "type": "video"},
        {"name": "aerial.png", "path": "/other/aerial.png", "type": "image"},
    ], "total": 2, "truncated": False}


def _panel(qtbot):
    panel = NativeFilesPanel()
    qtbot.addWidget(panel)
    panel.set_entries("/media", _dir_entries())
    return panel


def _select_first(panel):
    panel.selection.select(
        panel.proxy.index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect
        | QItemSelectionModel.SelectionFlag.Rows)


# ---------------------------------------------------------------------------
# FR-08：统一 Location——虚拟位置绝不假装上一个目录
# ---------------------------------------------------------------------------

def test_fr08_search_results_rebuild_path_lookup(qtbot):
    panel = _panel(qtbot)
    panel.show_library_results(_search_payload())
    # 新素材的路径索引已重建：lookup/strip 门槛不再丢新结果
    assert "/media/sunset.mov" in panel._entry_paths
    assert panel._entry_rows.get("/other/aerial.png") is not None
    assert panel._entry_for_path("/media/sunset.mov") is not None
    assert panel._view_location["kind"] == "library-search"


def test_fr08_virtual_location_rejects_directory_writes(qtbot):
    panel = _panel(qtbot)
    panel.show_library_results(_search_payload())
    # 虚拟位置：不提供"当前目录"——mkdir/粘贴不得落到上一个真实目录
    assert panel._current_directory() == ""
    panel.request_new_folder()          # 拒绝发生在 QInputDialog 之前
    assert "真实文件夹" in panel.meta_status.text()


def test_fr08_collection_location_identity(qtbot):
    panel = _panel(qtbot)
    panel._pending_collection_name = "精选"
    panel._pending_collection_id = 7
    panel.show_collection_results({"files": [
        {"name": "pick.mov", "path": "/media/pick.mov", "type": "video"}]})
    assert panel._view_location["kind"] == "collection"
    assert panel._view_location["collection_id"] == 7
    assert panel._view_location["writable"] is False
    assert panel._current_directory() == ""


def test_fr08_directory_restores_real_location_and_filter(qtbot):
    panel = _panel(qtbot)
    panel.show_library_results(_search_payload())
    panel.search_edit.setText("zzz-no-match")  # 本地过滤在虚拟视图不生效
    assert panel.proxy.query == ""
    panel.set_entries("/media", _dir_entries())  # 回到目录
    assert panel._view_location["kind"] == "directory"
    assert panel._current_directory() == "/media"
    panel.search_edit.setText("A")
    assert panel.proxy.query == "a"  # 目录视图恢复本地过滤语义


def test_fr08_refresh_replays_current_location(qtbot):
    panel = _panel(qtbot)
    emitted = []
    panel.library_search_requested.connect(
        lambda q, limit, offset: emitted.append(q))
    panel.show_library_results(_search_payload())
    panel._view_location["query"] = "sunset"
    panel.refresh()
    assert emitted == ["sunset"]  # 刷新重放搜索，而不是退回上一个目录

    emitted.clear()
    panel.collection_requested.connect(lambda cid, name: emitted.append(cid))
    panel._pending_collection_name = "精选"
    panel._pending_collection_id = 7
    panel.show_collection_results({"files": []})
    panel.refresh()
    assert emitted == [7]


# ---------------------------------------------------------------------------
# FR-09：连续输入防抖
# ---------------------------------------------------------------------------

def test_fr09_search_input_is_debounced(qtbot):
    panel = _panel(qtbot)
    panel.library_button.setChecked(True)
    emitted = []
    panel.library_search_requested.connect(
        lambda q, limit, offset: emitted.append(q))
    for text in ("s", "su", "sun", "sunset"):
        panel.search_edit.setText(text)
        panel._filter_changed()
    assert emitted == []  # 防抖窗口内不查询
    qtbot.wait(300)
    assert emitted == ["sunset"]  # 只发最后一次输入


# ---------------------------------------------------------------------------
# FR-11：标签替换语义 + 服务快照回显
# ---------------------------------------------------------------------------

def test_fr11_tags_submit_uses_replace_op(qtbot):
    panel = _panel(qtbot)
    _select_first(panel)
    emitted = []
    panel.metadata_patch_requested.connect(
        lambda paths, patch, op: emitted.append((patch, op)))
    panel.tag_edit.setText("夜,航拍")
    panel._tags_committed()
    assert emitted == [({"tags": "夜,航拍"}, "replace")]

    emitted.clear()
    panel.tag_edit.setText("")
    panel._tags_committed()
    assert emitted == [({"tags": ""}, "clear")]  # 空输入 = 清空（显式语义）


def test_fr11_snapshot_overrides_local_display(qtbot):
    panel = _panel(qtbot)
    _select_first(panel)
    panel.apply_metadata_snapshot(
        {"/media/A.mov": {"tags": "final,server", "rating": 4}})
    entry = panel._entry_for_path("/media/A.mov")
    assert entry.user.tag_text == "final,server"
    assert entry.user.rating == 4
    assert panel.tag_edit.text() == "final,server"  # 控件按服务快照回显


# ---------------------------------------------------------------------------
# FR-12：能力禁用优先于选择状态；提交复核
# ---------------------------------------------------------------------------

def test_fr12_capability_disables_and_stays_disabled_on_selection(qtbot):
    panel = _panel(qtbot)
    _select_first(panel)
    assert panel.tag_edit.isEnabled()  # 未拿到清单前不误禁
    panel.apply_capabilities([{"key": "metadata.patch", "available": False,
                               "reason": "旧服务"}])
    assert not panel.tag_edit.isEnabled()
    # 换选择 / 再回填：不得按"有选择"重新启用
    panel._fill_metadata_controls(panel._selected_entries())
    assert not panel.tag_edit.isEnabled()
    # 提交入口复核：不发写请求
    emitted = []
    panel.metadata_patch_requested.connect(lambda *args: emitted.append(args))
    panel._submit_patch({"rating": 5})
    assert emitted == []


# ---------------------------------------------------------------------------
# FR-09/16（transport）：在途搜索作废 + 多任务轮询
# ---------------------------------------------------------------------------

class _FakeReply(QObject):
    finished = pyqtSignal()

    def __init__(self, payload: dict | None = None, status: int = 200):
        super().__init__()
        self._payload = payload
        self._status = status
        self.aborted = False
        self.deleted = False

    def attribute(self, *_):
        return self._status

    def readAll(self):
        return json.dumps(self._payload or {}).encode("utf-8")

    def error(self):
        from PyQt6.QtNetwork import QNetworkReply
        return QNetworkReply.NetworkError.NoError

    def errorString(self):
        return ""

    def deleteLater(self):
        self.deleted = True

    def abort(self):
        self.aborted = True


class _FakeNetwork:
    def __init__(self):
        self.pending: list[_FakeReply] = []

    def get(self, request):
        reply = _FakeReply()
        self.pending.append(reply)
        return reply


def _transport():
    transport = NativeFilesTransport(lambda: "http://127.0.0.1:8765", "token")
    transport._network = _FakeNetwork()
    return transport


def test_fr09_stale_search_reply_is_discarded(qtbot):
    transport = _transport()
    loaded = []
    transport.search_loaded.connect(lambda payload: loaded.append(payload))
    transport.search_library("old")
    transport.search_library("new")
    stale, current = transport._network.pending
    assert stale.aborted  # 新请求作废旧请求
    current._payload = _search_payload()
    current.finished.emit()  # 新响应先到
    assert len(loaded) == 1
    loaded.clear()
    stale._payload = _search_payload()
    stale.finished.emit()  # 旧响应晚到：不得覆盖
    qtbot.wait(50)
    assert loaded == []


def test_fr16_two_jobs_poll_independently(qtbot):
    transport = _transport()
    finished = []
    transport.task_finished.connect(lambda job: finished.append(job["job_id"]))
    transport._start_task_poll("job-a")
    transport._start_task_poll("job-b")
    assert sorted(transport.active_task_jobs()) == ["job-a", "job-b"]
    # 两个 job 各自的轮询都已发出（不覆盖）
    replies = list(transport._network.pending)
    assert len(replies) == 2
    first, second = replies
    first._payload = {"job": {"job_id": "job-a", "status": "running",
                              "total": 2, "completed": 0}}
    second._payload = {"job": {"job_id": "job-b", "status": "running",
                              "total": 1, "completed": 0}}
    first.finished.emit()
    second.finished.emit()
    qtbot.wait(30)
    assert transport._task_polls.get("job-a") is not None  # running 不终止
    # 手动推进下一轮：两个任务仍各自有轮询请求
    transport._poll_task_once()
    second_round = transport._network.pending[2:]
    assert len(second_round) == 2
    reply_a, reply_b = second_round
    reply_a._payload = {"job": {"job_id": "job-a", "status": "completed",
                                "total": 2, "completed": 2}}
    reply_b._payload = {"job": {"job_id": "job-b", "status": "cancelled",
                                "total": 1, "completed": 0}}
    reply_a.finished.emit()
    qtbot.wait(30)
    assert finished == ["job-a"]
    assert "job-b" in transport.active_task_jobs()  # 第二个任务不受影响
    reply_b.finished.emit()
    qtbot.wait(30)
    assert sorted(finished) == ["job-a", "job-b"]
    assert transport.active_task_jobs() == []
