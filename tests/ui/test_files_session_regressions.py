"""NR-04 回归：预览/比较/批改名会话安全（FR-13/14/15/17）。"""
from __future__ import annotations

import json

import pytest

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QObject, pyqtSignal
from PyQt6.QtGui import QImage

pytest.importorskip("PyQt6.QtWidgets")

from src.app.ui.native_files_batch_rename import BatchRenameDialog  # noqa: E402
from src.app.ui.native_files_compare import CompareDialog, MAX_PIXMAP_PIXELS  # noqa: E402
from src.app.ui.native_files_preview import PreviewDecoder  # noqa: E402
from src.app.ui.native_files_transport import NativeFilesTransport  # noqa: E402


# ---------------------------------------------------------------------------
# FR-13：批改名——会话隔离 + 模板快照绑定
# ---------------------------------------------------------------------------

def _rename_dialog(qtbot, paths=("a.png", "b.png")):
    dialog = BatchRenameDialog(list(paths))
    qtbot.addWidget(dialog)
    return dialog


def test_fr13_execute_binds_confirmed_template_not_live_text(qtbot):
    dialog = _rename_dialog(qtbot)
    emitted = []
    dialog.preview_requested.connect(lambda t, s: emitted.append(("preview", t)))
    dialog.execute_requested.connect(lambda t, s: emitted.append(("execute", t)))

    dialog.template_edit.setText("{name}_x")
    dialog._preview()
    dialog.show_preview({"session_id": dialog.session_id,
                         "plan": [{"src": "a", "dst": "a_x"}], "errors": []})
    assert dialog.execute_button.isEnabled()

    dialog.template_edit.setText("{name}_Y")     # 预览后改模板
    assert not dialog.execute_button.isEnabled()  # 确认失效：不可执行
    dialog._execute()
    assert ("execute", "{name}_Y") not in emitted  # 未重新预览绝不能执行新模板

    dialog._preview()                              # 重新预览后按快照执行
    dialog.show_preview({"session_id": dialog.session_id,
                         "plan": [{"src": "a", "dst": "a_Y"}], "errors": []})
    dialog._execute()
    assert ("execute", "{name}_Y") in emitted


def test_fr13_foreign_session_results_are_ignored(qtbot):
    dialog_a = _rename_dialog(qtbot)
    dialog_b = _rename_dialog(qtbot)
    assert dialog_a.session_id != dialog_b.session_id

    # B 的预览结果到达 A：A 必须忽略（不启用执行、不覆盖表格）
    dialog_a._preview()
    dialog_a.show_preview({"session_id": dialog_b.session_id,
                           "plan": [{"src": "x", "dst": "y"}], "errors": []})
    assert not dialog_a.execute_button.isEnabled()
    assert dialog_a.table.rowCount() == 0
    dialog_a.show_preview_error("他窗失败", dialog_b.session_id)
    assert "他窗失败" not in dialog_a.summary.text()


# ---------------------------------------------------------------------------
# FR-14：同路径重开预览——旧任务晚到不得顶替新结果
# ---------------------------------------------------------------------------

def _png_bytes(size=4) -> bytes:
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(0xFF00FF00)
    buffer = QBuffer()
    buffer.setData(QByteArray())
    buffer.open(QIODevice.OpenModeFlag.ReadWrite)
    image.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()
    return data


def test_fr14_same_path_reopen_latest_result_wins(qtbot, monkeypatch):
    import threading
    from src.app.ui import native_files_preview as preview_module

    decoder = PreviewDecoder()
    results = []
    decoder.image_ready.connect(lambda p, img: results.append((p, img)))
    errors = []
    decoder.decode_failed.connect(lambda p, e: errors.append((p, e)))

    # 闸住第一个解码任务：确保"旧任务在途"时同路径重开（消除时序抖动）
    release = threading.Event()
    state = {"calls": 0}
    original = preview_module.decode_preview

    def gated(data):
        if state["calls"] == 0:
            state["calls"] += 1
            assert release.wait(2)
        return original(data)

    monkeypatch.setattr(preview_module, "decode_preview", gated)

    decoder.start("/same.png", _png_bytes(2))
    decoder.start("/same.png", _png_bytes(6))   # 同路径立刻重开（旧任务在途）
    release.set()
    try:
        qtbot.waitUntil(lambda: len(results) + len(errors) >= 1, timeout=5000)
        # 只显示新任务的结果；旧 2px 结果被世代作废
        assert len(errors) == 0
        assert len(results) == 1
        assert results[0][1].width() == 6
    finally:
        decoder.shutdown()   # 等待在途解码退出，避免任务包装被 GC 后崩溃


def test_fr14_cancel_discards_late_result(qtbot):
    decoder = PreviewDecoder()
    results = []
    decoder.image_ready.connect(lambda p, img: results.append((p, img)))
    decoder.start("/late.png", _png_bytes(4))
    decoder.cancel()                              # 关闭预览
    qtbot.wait(100)                               # 迟到的完成回调
    assert results == []


# ---------------------------------------------------------------------------
# FR-15/17：transport 比较会话化 + 关闭守卫
# ---------------------------------------------------------------------------

class _FakeReply(QObject):
    finished = pyqtSignal()
    readyRead = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._payload = {}
        self._chunk = b"x"
        self.aborted = False

    def attribute(self, *_):
        return 200

    def readAll(self):
        return json.dumps(self._payload).encode("utf-8")

    def error(self):
        from PyQt6.QtNetwork import QNetworkReply
        return QNetworkReply.NetworkError.NoError

    def errorString(self):
        return ""

    def deleteLater(self):
        pass

    def abort(self):
        self.aborted = True

    def setReadBufferSize(self, _):
        pass

    def read(self, limit):
        return self._chunk[:limit]


class _FakeNetwork:
    def __init__(self):
        self.pending = []

    def get(self, request):
        reply = _FakeReply()
        self.pending.append(reply)
        return reply


def _transport():
    transport = NativeFilesTransport(lambda: "http://127.0.0.1:8765", "token")
    transport._network = _FakeNetwork()
    return transport


def test_fr15_two_compare_sessions_do_not_interleave(qtbot):
    transport = _transport()
    loaded = []
    transport.compare_image_loaded.connect(
        lambda p, data, s: loaded.append((s, p)))
    finished = []
    transport.compare_set_finished.connect(lambda s: finished.append(s))

    transport.load_compare_images(["/a.png", "/b.png"], session_id="s1")
    transport.load_compare_images(["/c.png"], session_id="s2")
    # 两个会话各有自己的在途请求（此前共享单槽会互相吞）
    replies = list(transport._network.pending)
    assert len(replies) == 2

    replies[0]._payload = {}
    replies[0].readyRead.emit()   # 数据就位（进入 state.data）
    replies[0].finished.emit()    # s1 第一张完成 → 自动拉第二张
    qtbot.wait(20)
    sessions_in_flight = {s for s, _ in loaded}
    assert sessions_in_flight == {"s1"}       # s1 的进度不会串到 s2


def test_fr15_cancelled_session_discards_results(qtbot):
    transport = _transport()
    loaded = []
    transport.compare_image_loaded.connect(lambda p, data, s: loaded.append(p))
    transport.load_compare_images(["/x.png"], session_id="gone")
    reply = transport._network.pending[0]
    transport.cancel_compare("gone")           # 关窗
    assert reply.aborted                       # 在途请求被取消
    reply.finished.emit()                      # 迟到响应
    qtbot.wait(20)
    assert loaded == []


def test_fr15_dialog_closed_guard_ignores_results(qtbot):
    dialog = CompareDialog(["/a.png", "/b.png"])
    qtbot.addWidget(dialog)
    foreign = "not-my-session"
    dialog.set_image("/a.png", _png_bytes(4), session_id=foreign)
    dialog.set_error("/a.png", "他窗错误", session_id=foreign)
    assert dialog._images == {}                # 非本会话数据不进视图
    dialog.reject()                            # 关闭
    dialog.set_image("/a.png", _png_bytes(4), session_id=dialog.session_id)
    assert dialog._images == {}                # 关闭后本会话结果也不进


def test_fr15_pixmap_budget_caps_giant_zoom(qtbot):
    dialog = CompareDialog(["/big.png", "/tall.png"])
    qtbot.addWidget(dialog)
    dialog._images["/big.png"] = QImage(6000, 4000, QImage.Format.Format_RGB32)
    dialog._images["/tall.png"] = QImage(2000, 8000, QImage.Format.Format_RGB32)
    dialog._set_zoom(4.0)                      # 最大放大档
    for label in dialog._labels.values():
        pixmap = label.pixmap()
        if pixmap and not pixmap.isNull():
            assert pixmap.width() * pixmap.height() <= MAX_PIXMAP_PIXELS + 4096


def test_fr17_shutdown_aborts_metadata_and_compare(qtbot):
    transport = _transport()

    class _Reply(_FakeReply):
        def readyRead(self):
            pass

    metadata_reply = _FakeReply()
    transport._metadata_replies[metadata_reply] = object()
    transport.load_compare_images(["/m.png"], session_id="s")
    compare_reply = transport._network.pending[0]

    transport.shutdown()
    assert metadata_reply.aborted
    assert compare_reply.aborted
    assert transport._compare_sessions == {}
    assert transport._compare_replies == {}
