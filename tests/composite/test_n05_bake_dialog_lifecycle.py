"""N05 · Bake 对话框线程边界与生命周期测试。

审计句：worker 直接读/写 QProgressBar（跨线程控件访问）；关闭/reject
不取消不等在途；图改写在 worker 线程直接变更。修复后契约：

- 进度经 ``frame_rendered`` 信号，UI 线程槽更新（bar 值只随信号前进）；
- 事务 ``apply_rewrite=False``；发布成功后 ``rewrite_fn`` 在 UI 线程
  （结果槽内）执行，未提供时如实显示"图未改写"；
- 取消协作：cancel 后 render_fn 轮询点中止；
- 关闭在途任务：取消 + 有限等待；超时拒绝关闭（保活）。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest.importorskip("PyQt6")

import src.features.composite.ui.bake_progress_dialog as bpd  # noqa: E402
from src.features.composite.ui.bake_progress_dialog import (  # noqa: E402
    BakeProgressDialog,
)


@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _drain_events(qapp, rounds: int = 10) -> None:
    """投递已排队的 Qt 信号（无独立事件循环的测试进程）。"""
    for _ in range(rounds):
        qapp.processEvents()


def _result(ok=True, error="", stage=""):
    return type("R", (), {"ok": ok, "published": ["a", "b"] if ok else [],
                          "written": [], "error": error, "stage": stage})()


class _Graph:
    """最小图替身。"""


def _make_dialog(qapp, render_fn, *, rewrite_fn=None, on_done=None):
    return BakeProgressDialog(
        _Graph(), "node-1", "/tmp/unused-bake-out", 0, 2, render_fn,
        rewrite_fn=rewrite_fn, on_done=on_done)


def test_progress_via_signal_and_no_rewrite_in_txn(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """worker 只发信号；事务 apply_rewrite=False（改写不在 worker 线程）。"""
    recorded = {}

    def fake_execute_bake(**kwargs):
        recorded.update(kwargs)
        for i in range(kwargs["frame_start"], kwargs["frame_end"] + 1):
            kwargs["render_fn"]("node-1", i, f"/tmp/f{i}.exr")
        return _result()

    monkeypatch.setattr(bpd, "execute_bake", fake_execute_bake)
    dialog = _make_dialog(qapp, lambda n, f, p: True)
    dialog.start()
    assert dialog._worker.wait(5000)
    _drain_events(qapp)
    assert dialog._bar.value() == 3  # 0..2 共 3 帧，全部经信号计数
    assert recorded["apply_rewrite"] is False  # 事务不改图


def test_rewrite_runs_on_ui_thread_after_publish(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    threads = []

    def fake_execute_bake(**kwargs):
        for i in range(kwargs["frame_start"], kwargs["frame_end"] + 1):
            kwargs["render_fn"]("node-1", i, f"/tmp/f{i}.exr")
        return _result()

    monkeypatch.setattr(bpd, "execute_bake", fake_execute_bake)

    def rewrite_fn(result):
        threads.append(threading.current_thread().ident)
        return True

    dialog = _make_dialog(qapp, lambda n, f, p: True, rewrite_fn=rewrite_fn)
    dialog.start()
    assert dialog._worker.wait(5000)
    _drain_events(qapp)
    assert dialog.rewrite_applied is True
    # 改写执行于 UI（主）线程，非 worker 线程
    worker_thread = dialog._worker.threadId() if hasattr(
        dialog._worker, "threadId") else None
    assert threads == [threading.current_thread().ident]
    if worker_thread is not None:
        assert threads[0] != int(worker_thread)


def test_no_rewrite_fn_reports_honestly(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bpd, "execute_bake", lambda **kw: _result())
    dialog = _make_dialog(qapp, lambda n, f, p: True)
    dialog.start()
    assert dialog._worker.wait(5000)
    _drain_events(qapp)
    assert dialog.rewrite_applied is False
    assert "图未改写" in dialog._stage_label.text()


def test_cancel_cooperative(qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """run 前置取消 → 事务在轮询点中止，失败如实呈现。"""
    monkeypatch.setattr(
        bpd, "execute_bake",
        lambda **kw: (_result(ok=False, error="cancelled",
                              stage="render")
                      if kw["is_cancelled"]() else _result()))
    dialog = _make_dialog(qapp, lambda n, f, p: True)
    orig_run = bpd._BakeWorker.run

    def run_precancelled(self):
        self.cancel()
        orig_run(self)

    monkeypatch.setattr(bpd._BakeWorker, "run", run_precancelled)
    dialog.start()
    assert dialog._worker.wait(5000)
    _drain_events(qapp)
    assert dialog.result is not None and dialog.result.ok is False
    assert "失败" in dialog._stage_label.text()


def test_close_timeout_keeps_alive_then_close_after_finish(
        qapp, monkeypatch: pytest.MonkeyPatch) -> None:
    """取消等待超时拒绝关闭（保活防半途销毁）；任务结束后允许关闭。"""
    hold = threading.Event()

    def stuck_render(node_id, frame, out_path):
        hold.wait(5.0)
        return True

    def fake_execute_bake(**kwargs):
        # 必须真正进入 render_fn——worker 阻塞在 hold 上才有"在途"可等
        kwargs["render_fn"]("node-1", 0, "/tmp/f0.exr")
        return _result(ok=False, error="cancelled", stage="render")

    monkeypatch.setattr(bpd, "execute_bake", fake_execute_bake)
    monkeypatch.setattr(bpd, "_CLOSE_WAIT_MS", 100)
    dialog = _make_dialog(qapp, stuck_render)
    dialog.start()
    time.sleep(0.05)
    try:
        assert dialog._request_close() is False  # 超时：拒绝关闭
        assert dialog.worker_running  # 保活
    finally:
        dialog._worker.cancel()
        hold.set()
        assert dialog._worker.wait(5000)
        _drain_events(qapp)
        assert dialog._request_close() is True  # 结束后允许关闭
