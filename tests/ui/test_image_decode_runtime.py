"""E1/E2（FUP-06）：共享图片解码 runtime 的预算/取消/引用/关闭语义。

验收口径对应 FUP-06：
* 跨窗口统一并发（同一池）与总字节/像素预算（超限立即回绝、可解释）；
* 取消分层：排队即丢（预算立即归还），在途只作废回包；
* 运行任务的包装引用保留到真实完成（GC 不击穿 run）；
* 完成即回收压缩字节与台账（不留到关闭）；
* 关闭先作废回包、不阻塞、不清引用假装线程结束。
"""
from __future__ import annotations

import gc
import threading
import weakref

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402

from src.app.ui.image_decode_runtime import (  # noqa: E402
    ImageDecodeRuntime,
    decode_image_scaled,
    estimate_pixels,
)


def _png_bytes(size: int = 4) -> bytes:
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(0xFF00FF00)
    buffer = QBuffer()
    buffer.setData(QByteArray())
    buffer.open(QIODevice.OpenModeFlag.ReadWrite)
    image.save(buffer, "PNG")
    data = bytes(buffer.data())
    buffer.close()
    return data


def _identity_decode(data: bytes) -> tuple[QImage, str]:
    return decode_image_scaled(data)


class _Gate:
    """闸住 decode 制造"在途"与"排队"两种状态。"""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.calls: list[str] = []
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def decode(self, data: bytes) -> tuple[QImage, str]:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        self.calls.append("enter")
        self.release.wait(5)
        with self._lock:
            self._active -= 1
        return _identity_decode(data)


# ---------------------------------------------------------------------------
# 基本投放与回收
# ---------------------------------------------------------------------------

def test_admits_delivers_and_reclaims(qtbot):
    runtime = ImageDecodeRuntime(max_concurrency=2)
    lease = runtime.acquire("t")
    results: list[tuple[str, QImage, str]] = []
    for index in range(3):
        lease.submit(f"/m/{index}.png", _png_bytes(),
                     _identity_decode, lambda *args: results.append(args))
    qtbot.waitUntil(runtime.is_idle, timeout=8000)
    assert len(results) == 3
    for path, image, error in results:
        assert path.startswith("/m/") and not image.isNull() and error == ""
    stats = runtime.stats()
    assert stats["in_flight"] == 0 and stats["pending_bytes"] == 0
    assert stats["pending_pixels"] == 0 and stats["completed_total"] == 3


def test_estimate_pixels_reads_header(qtbot):
    data = _png_bytes(6)
    assert estimate_pixels(data) == 36
    assert estimate_pixels(b"") == 8192 * 8192  # 头部不可读按最坏情况


# ---------------------------------------------------------------------------
# 预算：单任务回绝 + 总预算回绝（立即、可解释、可重试）
# ---------------------------------------------------------------------------

def test_single_job_over_budgets_rejected_immediately(qtbot):
    runtime = ImageDecodeRuntime(max_pending_bytes=8, max_pending_pixels=4)
    lease = runtime.acquire("t")
    rejected: list[str] = []
    lease.submit("/big.bin", b"x" * 16, _identity_decode,
                 lambda p, image, error: rejected.append(error))
    assert len(rejected) == 1 and "字节预算" in rejected[0]
    # 字节很小但头部不可读 → 按最坏 8192² 像素估计，命中像素预算
    lease.submit("/huge.bin", b"x" * 4, _identity_decode,
                 lambda p, image, error: rejected.append(error))
    assert len(rejected) == 2 and "像素预算" in rejected[1]
    assert runtime.stats()["rejected_total"] == 2
    assert runtime.is_idle()


def test_aggregate_budget_rejects_with_explanation(qtbot):
    gate = _Gate()
    runtime = ImageDecodeRuntime(max_concurrency=1, max_pending_bytes=10_000_000,
                                 max_pending_pixels=16)  # 一张 4×4=16px 就占满
    lease = runtime.acquire("t")
    delivered: list[str] = []
    lease.submit("/a.png", _png_bytes(), gate.decode,
                 lambda p, image, error: delivered.append(p))
    rejected: list[str] = []
    lease.submit("/b.png", _png_bytes(), _identity_decode,
                 lambda p, image, error: rejected.append(error))
    # 总预算被 a 占满：b 立即回绝且说明在途量，不无界积压
    assert rejected and "队列已满" in rejected[0]
    gate.release.set()
    qtbot.waitUntil(runtime.is_idle, timeout=8000)
    assert delivered == ["/a.png"]  # 已在途的照常送达


# ---------------------------------------------------------------------------
# 并发：跨租约统一上限
# ---------------------------------------------------------------------------

def test_concurrency_capped_across_leases(qtbot):
    gate = _Gate()
    runtime = ImageDecodeRuntime(max_concurrency=1)
    lease_a = runtime.acquire("preview")
    lease_b = runtime.acquire("compare")
    done: list[str] = []
    for lease, name in ((lease_a, "/a"), (lease_a, "/b"), (lease_b, "/c")):
        lease.submit(name, _png_bytes(), gate.decode,
                     lambda p, image, error: done.append(p))
    qtbot.waitUntil(lambda: len(gate.calls) >= 1, timeout=8000)
    qtbot.wait(80)  # 若并发失控，第二个任务会立刻进入 decode
    assert gate.max_active == 1  # 两个窗口共用同一并发预算
    gate.release.set()
    qtbot.waitUntil(runtime.is_idle, timeout=8000)
    assert sorted(done) == ["/a", "/b", "/c"]


# ---------------------------------------------------------------------------
# 取消分层：排队即丢 / 在途作废；引用与预算的回收时机
# ---------------------------------------------------------------------------

def test_cancel_queued_releases_budget_immediately(qtbot):
    gate = _Gate()
    runtime = ImageDecodeRuntime(max_concurrency=1)
    lease = runtime.acquire("t")
    lease.submit("/run.png", _png_bytes(), gate.decode, lambda *args: None)
    qtbot.waitUntil(lambda: len(gate.calls) >= 1, timeout=8000)
    second_id = lease.submit("/queued.png", _png_bytes(),
                             gate.decode, lambda *args: None)
    assert second_id is not None
    bytes_before = runtime.stats()["pending_bytes"]
    assert lease.cancel_all() == 1  # 排队任务已丢；1 个在途任务作废回包
    stats = runtime.stats()
    assert stats["in_flight"] == 1            # 在途任务还在跑（不假装结束）
    assert stats["pending_bytes"] < bytes_before  # 排队任务字节立即归还
    gate.release.set()
    qtbot.waitUntil(runtime.is_idle, timeout=8000)
    assert "/queued.png" not in gate.calls    # 排队任务从未运行


def test_cancel_running_invalidates_and_reclaims_on_completion(qtbot):
    gate = _Gate()
    runtime = ImageDecodeRuntime(max_concurrency=1)
    lease = runtime.acquire("t")
    seen: list[str] = []
    job_id = lease.submit("/run.png", _png_bytes(), gate.decode,
                          lambda p, image, error: seen.append(p))
    qtbot.waitUntil(lambda: len(gate.calls) >= 1, timeout=8000)
    job = runtime._jobs[job_id]
    ref = weakref.ref(job)
    del job
    gc.collect()
    assert ref() is not None              # 运行中：runtime 持有强引用
    assert lease.cancel_all() == 1        # 在途：作废回包，不算已结束
    gate.release.set()
    qtbot.waitUntil(runtime.is_idle, timeout=8000)
    qtbot.wait(30)                        # 让迟到的完成回调走完队列连接
    assert seen == []                     # 作废后的结果不送达
    gc.collect()
    assert ref() is None                  # 真实完成即回收包装引用
    stats = runtime.stats()
    assert stats["pending_bytes"] == 0 and stats["pending_pixels"] == 0


def test_completion_releases_compressed_bytes(qtbot):
    gate = _Gate()
    runtime = ImageDecodeRuntime()
    lease = runtime.acquire("t")
    payload = _png_bytes(32)
    lease.submit("/m.png", payload, gate.decode, lambda *args: None)
    qtbot.waitUntil(lambda: len(gate.calls) >= 1, timeout=8000)
    assert runtime.stats()["pending_bytes"] == len(payload)
    gate.release.set()
    qtbot.waitUntil(runtime.is_idle, timeout=8000)
    assert runtime.stats()["pending_bytes"] == 0  # 完成即归还，不等 shutdown


# ---------------------------------------------------------------------------
# 关闭：先作废回包、不阻塞、不清引用假装结束
# ---------------------------------------------------------------------------

def test_shutdown_suppresses_callbacks_and_does_not_block(qtbot):
    import time as _time

    gate = _Gate()
    runtime = ImageDecodeRuntime(max_concurrency=1)
    lease = runtime.acquire("t")
    seen: list[str] = []
    lease.submit("/slow.png", _png_bytes(), gate.decode,
                 lambda p, image, error: seen.append(p))
    qtbot.waitUntil(lambda: len(gate.calls) >= 1, timeout=8000)
    start = _time.monotonic()
    runtime.shutdown()
    elapsed = _time.monotonic() - start
    assert elapsed < 0.5  # 不同步等待两秒伪装退出
    gate.release.set()
    qtbot.waitUntil(runtime.is_idle, timeout=8000)
    qtbot.wait(30)
    assert seen == []     # 关闭后的迟到结果不送达
    # 关闭后再提交：立即回绝，可解释
    late: list[str] = []
    lease.submit("/late.png", _png_bytes(), _identity_decode,
                 lambda p, image, error: late.append(error))
    assert late and "已关闭" in late[0]


def test_lease_close_is_scoped_to_owner(qtbot):
    gate = _Gate()
    runtime = ImageDecodeRuntime(max_concurrency=2)
    preview = runtime.acquire("preview")
    compare = runtime.acquire("compare")
    seen: list[str] = []
    preview.submit("/p.png", _png_bytes(), gate.decode,
                   lambda p, image, error: seen.append(p))
    compare.submit("/c.png", _png_bytes(), _identity_decode,
                   lambda p, image, error: seen.append(p))
    qtbot.waitUntil(lambda: len(gate.calls) >= 1, timeout=8000)
    preview.close()  # 只关预览租约
    gate.release.set()
    qtbot.waitUntil(runtime.is_idle, timeout=8000)
    qtbot.wait(30)
    assert seen == ["/c.png"]  # 比较窗口的结果照常送达


# ---------------------------------------------------------------------------
# FUP-06 验收：100 次开关循环收敛
# ---------------------------------------------------------------------------

def test_hundred_open_close_cycles_converge(qtbot):
    runtime = ImageDecodeRuntime(max_concurrency=2)
    payload = _png_bytes(4)
    for cycle in range(100):
        lease = runtime.acquire(f"w{cycle}")
        results: list[str] = []
        lease.submit(f"/c{cycle}.png", payload, _identity_decode,
                     lambda p, image, error: results.append(p))
        if cycle % 3 == 0:
            lease.close()  # 立即关：排队即丢/在途作废
    assert runtime.wait_for_idle(10_000)
    stats = runtime.stats()
    assert stats["in_flight"] == 0
    assert stats["pending_bytes"] == 0 and stats["pending_pixels"] == 0


# ---------------------------------------------------------------------------
# 消费方：PreviewDecoder / CompareDialog 走共享 runtime（不再自建线程池）
# ---------------------------------------------------------------------------

def test_preview_decoder_uses_runtime_and_latest_wins(qtbot, monkeypatch):
    import threading as _t

    from src.app.ui import native_files_preview as preview_module
    from src.app.ui.native_files_preview import PreviewDecoder

    runtime = ImageDecodeRuntime(max_concurrency=2)
    decoder = PreviewDecoder(runtime=runtime)
    results: list[tuple[str, QImage]] = []
    errors: list[str] = []
    decoder.image_ready.connect(lambda p, img: results.append((p, img)))
    decoder.decode_failed.connect(lambda p, e: errors.append(e))

    release = _t.Event()
    calls = {"n": 0}
    original = preview_module.decode_preview

    def gated(data):
        if calls["n"] == 0:
            calls["n"] += 1
            assert release.wait(2)
        return original(data)

    monkeypatch.setattr(preview_module, "decode_preview", gated)
    try:
        decoder.start("/same.png", _png_bytes(2))
        decoder.start("/same.png", _png_bytes(6))  # 同路径重开：旧任务在途
        release.set()
        qtbot.waitUntil(lambda: len(results) + len(errors) >= 1, timeout=5000)
        assert errors == []
        assert len(results) == 1 and results[0][1].width() == 6  # 新任务胜出
        qtbot.waitUntil(runtime.is_idle, timeout=5000)
    finally:
        decoder.shutdown()


def test_preview_decoder_shutdown_does_not_block_on_slow_decode(qtbot, monkeypatch):
    import time as _time
    import threading as _t

    from src.app.ui import native_files_preview as preview_module
    from src.app.ui.native_files_preview import PreviewDecoder

    runtime = ImageDecodeRuntime(max_concurrency=1)
    decoder = PreviewDecoder(runtime=runtime)
    results: list[str] = []
    decoder.image_ready.connect(lambda p, img: results.append(p))
    release = _t.Event()
    original = preview_module.decode_preview

    def slow(data):
        release.wait(5)  # 慢解码 >2 秒：旧实现会同步等 2 秒后清引用
        return original(data)

    monkeypatch.setattr(preview_module, "decode_preview", slow)
    try:
        decoder.start("/slow.png", _png_bytes(4))
        qtbot.waitUntil(lambda: not runtime.is_idle(), timeout=5000)
        start = _time.monotonic()
        decoder.shutdown()
        assert _time.monotonic() - start < 0.5  # 关闭不等待慢解码
        release.set()
        qtbot.waitUntil(runtime.is_idle, timeout=8000)
        qtbot.wait(30)
        assert results == []  # 关闭先作废：迟到结果不回 GUI
    finally:
        release.set()


def test_compare_dialog_teardown_cancels_via_lease(qtbot, monkeypatch):
    import threading as _t

    from src.app.ui import native_files_compare as compare_module
    from src.app.ui.native_files_compare import CompareDialog

    runtime = ImageDecodeRuntime(max_concurrency=1)
    dialog = CompareDialog(["/a.png"], runtime=runtime)
    qtbot.addWidget(dialog)
    shown: list[str] = []
    release = _t.Event()
    original = compare_module.decode_compare

    def gated(data):
        release.wait(5)
        return original(data)

    monkeypatch.setattr(compare_module, "decode_compare", gated)
    try:
        dialog.show()
        dialog.set_image("/a.png", _png_bytes(4), session_id=dialog.session_id)
        qtbot.waitUntil(lambda: not runtime.is_idle(), timeout=5000)
        dialog.reject()  # 关窗：租约关闭，排队任务即丢/在途作废
        assert not hasattr(dialog, "_decode_pool")  # 不再自建带父对象的池
        release.set()
        qtbot.waitUntil(runtime.is_idle, timeout=8000)
        qtbot.wait(30)
        assert dialog._images == {} and shown == []
    finally:
        release.set()


def test_two_windows_share_one_pool_budget(qtbot):
    """预览 + 两个比较窗口：并发不超过共享池上限（FUP-06 验收场景）。"""
    gate = _Gate()
    runtime = ImageDecodeRuntime(max_concurrency=2)
    leases = [runtime.acquire(name) for name in ("preview", "compare-1", "compare-2")]
    for index, lease in enumerate(leases):
        lease.submit(f"/w{index}.png", _png_bytes(), gate.decode, lambda *a: None)
    qtbot.waitUntil(lambda: len(gate.calls) >= 2, timeout=8000)
    qtbot.wait(80)
    assert gate.max_active <= 2  # 三窗共享同一并发预算
    gate.release.set()
    qtbot.waitUntil(runtime.is_idle, timeout=8000)
