"""NR-04 回归：预览/比较/批改名会话安全（FR-13/14/15/17）。"""
from __future__ import annotations

import json
import threading

import pytest

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QObject, pyqtSignal
from PyQt6.QtGui import QImage

pytest.importorskip("PyQt6.QtWidgets")

from src.app.ui.native_files_batch_rename import BatchRenameDialog  # noqa: E402
from src.app.ui.native_files_compare import CompareDialog, MAX_PIXMAP_PIXELS  # noqa: E402
from src.app.ui.native_files_preview import PreviewDecoder  # noqa: E402
from src.app.ui.native_files_transport import NativeFilesTransport  # noqa: E402
from src.features.files.repository import FilesDataLocator  # noqa: E402
from src.features.files.repository.catalog import FilesCatalog  # noqa: E402
from src.features.files.services.facade import FilesFacade as _FilesFacade  # noqa: E402


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
    dialog.show_preview({"session_id": dialog._preview_request_id,
                         "plan": [{"src": "a", "dst": "a_x"}], "errors": []})
    assert dialog.execute_button.isEnabled()

    dialog.template_edit.setText("{name}_Y")     # 预览后改模板
    assert not dialog.execute_button.isEnabled()  # 确认失效：不可执行
    dialog._execute()
    assert ("execute", "{name}_Y") not in emitted  # 未重新预览绝不能执行新模板

    dialog._preview()                              # 重新预览后按快照执行
    dialog.show_preview({"session_id": dialog._preview_request_id,
                         "plan": [{"src": "a", "dst": "a_Y"}], "errors": []})
    dialog._execute()
    assert ("execute", "{name}_Y") in emitted


def test_fr13_preview_reply_cannot_confirm_edited_or_newer_request(qtbot) -> None:
    dialog = _rename_dialog(qtbot)
    dialog.template_edit.setText("{name}_OLD")
    dialog._preview()
    old_request = dialog._preview_request_id
    dialog.template_edit.setText("{name}_NEW")
    dialog.show_preview({"session_id": old_request, "plan": [{"src": "a", "dst": "old"}]})
    assert not dialog.execute_button.isEnabled()
    assert dialog._confirmed_template is None

    dialog._preview()
    first_request = dialog._preview_request_id
    dialog._preview()  # 相同模板重复请求，仍需区分代次
    latest_request = dialog._preview_request_id
    dialog.show_preview({"session_id": latest_request, "plan": [{"src": "a", "dst": "new"}]})
    dialog.show_preview_error("旧请求失败", first_request)
    dialog.show_preview({"session_id": first_request, "plan": [], "errors": ["旧错误"]})
    assert dialog.execute_button.isEnabled()
    assert dialog._confirmed_template == "{name}_NEW"
    assert "旧" not in dialog.summary.text()


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
    metaDataChanged = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._payload = {}
        self._chunk = b"x"
        self._status = 200
        self._error = None
        self.aborted = False

    def attribute(self, *_):
        return self._status

    def header(self, *_):
        return len(self._chunk)

    def readAll(self):
        return json.dumps(self._payload).encode("utf-8")

    def error(self):
        from PyQt6.QtNetwork import QNetworkReply
        return self._error or QNetworkReply.NetworkError.NoError

    def errorString(self):
        return ""

    def deleteLater(self):
        pass

    def abort(self):
        self.aborted = True

    def setReadBufferSize(self, _):
        pass

    def read(self, limit):
        chunk, self._chunk = self._chunk[:limit], self._chunk[limit:]
        return chunk


class _FakeNetwork:
    def __init__(self):
        self.pending = []

    def get(self, request):
        reply = _FakeReply()
        self.pending.append(reply)
        return reply

    def post(self, request, payload):
        return self.get(request)


def _transport():
    transport = NativeFilesTransport(lambda: "http://127.0.0.1:8765", "token")
    transport._network = _FakeNetwork()
    return transport


@pytest.mark.parametrize("failure", [None, "unconfirmed", "missing_source"])
def test_fr13_execute_reply_keeps_session_and_reports_transport_failure(
        qtbot, tmp_path, failure) -> None:
    """D1 换轨口径：执行结果携带会话身份；失败如实上报不假成功。"""
    catalog, library, lib = _facade_env(tmp_path)
    a = lib / "a.png"
    b = lib / "b.png"
    a.write_bytes(b"a")
    if failure != "missing_source":
        b.write_bytes(b"b")
    transport = NativeFilesTransport(
        lambda: "", None, facade=_FilesFacade(catalog, max_workers=1))
    results = []
    transport.batch_rename_finished.connect(results.append)
    try:
        if failure == "missing_source":
            # 源缺失：预览失败如实上报（不是空的"可执行计划"）
            with qtbot.waitSignal(transport.batch_rename_preview_failed, timeout=8000) as bad:
                transport.preview_batch_rename(
                    [str(a), str(b)], "template", {"template": "{name}_x.{ext}"},
                    session_id="window-a")
            assert bad.args[1] == "window-a" and bad.args[0]
        elif failure != "unconfirmed":
            with qtbot.waitSignal(transport.batch_rename_preview_ready, timeout=8000):
                transport.preview_batch_rename(
                    [str(a), str(b)], "template", {"template": "{name}_x.{ext}"},
                    session_id="window-a")
        transport.execute_batch_rename(
            [str(a), str(b)], "template", {"template": "{name}_x.{ext}"},
            session_id="window-a")
        qtbot.waitUntil(lambda: bool(results), timeout=8000)
        assert len(results) == 1
        assert results[0]["session_id"] == "window-a"
        if failure is None:
            assert results[0]["success"] is True
            assert (lib / "a_x.png").exists()
        else:
            assert results[0]["success"] is False
            assert results[0]["message"]
    finally:
        transport.shutdown()
        catalog.close()


def _facade_env(tmp_path):
    locator = FilesDataLocator.for_config_root(tmp_path)
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    catalog.open()
    library = catalog.register_library(tmp_path / "lib")
    lib = tmp_path / "lib"
    lib.mkdir()
    return catalog, library, lib


def test_fr14_cancel_discards_late_fetch(qtbot, tmp_path, monkeypatch):
    """取消预览后，晚到的字节不得成为新的解码任务。"""
    from src.features.files.services import facade as facade_module
    catalog, library, lib = _facade_env(tmp_path)
    asset = lib / "late.png"
    asset.write_bytes(b"LATE")
    release = threading.Event()
    original = facade_module.read_file_bytes

    def gated(path, max_bytes):
        release.wait(5)
        return original(path, max_bytes)

    monkeypatch.setattr(facade_module, "read_file_bytes", gated)
    transport = NativeFilesTransport(
        lambda: "", None, facade=_FilesFacade(catalog, max_workers=1))
    loaded = []
    transport.preview_loaded.connect(lambda *args: loaded.append(args))
    try:
        transport.load_preview(str(asset))
        transport.cancel_preview()
        release.set()
        with qtbot.assertNotEmitted(transport.preview_loaded):
            qtbot.wait(200)
        assert loaded == []
    finally:
        release.set()
        transport.shutdown()
        catalog.close()


def test_fr15_two_compare_sessions_do_not_interleave(qtbot, tmp_path):
    catalog, library, lib = _facade_env(tmp_path)
    a = lib / "a.png"
    b = lib / "b.png"
    c = lib / "c.png"
    for asset, payload in ((a, b"AAA"), (b, b"BBB"), (c, b"CCC")):
        asset.write_bytes(payload)
    transport = NativeFilesTransport(
        lambda: "", None, facade=_FilesFacade(catalog, max_workers=1))
    loaded = []
    transport.compare_image_loaded.connect(
        lambda p, data, s: loaded.append((s, p)))
    finished = []
    transport.compare_set_finished.connect(finished.append)
    try:
        transport.load_compare_images([str(a), str(b)], session_id="s1")
        transport.load_compare_images([str(c)], session_id="s2")
        qtbot.waitUntil(lambda: len(loaded) == 3, timeout=8000)
        qtbot.waitUntil(lambda: sorted(finished) == ["s1", "s2"], timeout=8000)
        by_session = {}
        for session, path in loaded:
            by_session.setdefault(session, set()).add(path)
        assert by_session == {"s1": {str(a), str(b)}, "s2": {str(c)}}
    finally:
        transport.shutdown()
        catalog.close()


def test_fr15_cancelled_session_discards_results(qtbot, tmp_path, monkeypatch):
    from src.features.files.services import facade as facade_module
    catalog, library, lib = _facade_env(tmp_path)
    asset = lib / "x.png"
    asset.write_bytes(b"X")
    release = threading.Event()
    original = facade_module.read_file_bytes

    def gated(path, max_bytes):
        release.wait(5)
        return original(path, max_bytes)

    monkeypatch.setattr(facade_module, "read_file_bytes", gated)
    transport = NativeFilesTransport(
        lambda: "", None, facade=_FilesFacade(catalog, max_workers=1))
    loaded = []
    transport.compare_image_loaded.connect(lambda *args: loaded.append(args))
    try:
        transport.load_compare_images([str(asset)], session_id="gone")
        transport.cancel_compare("gone")           # 关窗
        release.set()
        with qtbot.assertNotEmitted(transport.compare_image_loaded):
            qtbot.wait(200)
        assert loaded == []
    finally:
        release.set()
        transport.shutdown()
        catalog.close()


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


def test_fr17_shutdown_suppresses_metadata_and_compare(qtbot, tmp_path, monkeypatch):
    """关闭后：比较/元数据的迟到回调一律静默（投递门，不再是 HTTP abort）。"""
    from src.features.files.services.facade import FacadeOutcome, FilesFacade
    catalog, library, lib = _facade_env(tmp_path)
    captured = []
    facade = FilesFacade(catalog, max_workers=1)
    monkeypatch.setattr(
        facade, "run",
        lambda key, request_id, work, listener: captured.append(listener))
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    metadata_applied, compare_loaded = [], []
    transport.metadata_applied.connect(lambda *args: metadata_applied.append(args))
    transport.compare_image_loaded.connect(lambda *args: compare_loaded.append(args))
    try:
        (lib / "m.png").write_bytes(b"m")
        catalog.upsert_asset(library.library_id, lib / "m.png")
        transport.patch_metadata([str(lib / "m.png")], {"rating": 3})
        transport.load_compare_images([str(lib / "m.png")], session_id="s")
        assert len(captured) == 2
        transport.shutdown()
        captured[0](FacadeOutcome(request_id="m", key="meta:x", generation=1,
                                  result=None))  # 元数据迟到成功
        outcome = FacadeOutcome(request_id="c", key="compare:s:1",
                                generation=1, result=b"BYTES")
        captured[1](outcome)                     # 比较迟到字节
        with qtbot.assertNotEmitted(transport.metadata_applied):
            qtbot.wait(100)
        assert metadata_applied == [] and compare_loaded == []
        assert transport._compare_sessions == {}
    finally:
        transport.shutdown()
        catalog.close()
