"""M16.2 · 解码分域指标 + worker 生命周期（离屏口径）。"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.core.realtime_playback.decode_metrics import (
    DecodeMetrics,
)


class TestDecodeMetrics:
    def test_domain_counters_and_snapshot(self):
        m = DecodeMetrics()
        m.record_cache("display", hit=True)
        m.record_cache("display", hit=True)
        m.record_cache("display", hit=False)
        m.record_cache("prefetch", hit=False)
        m.record_decode("display", 5_000_000)
        m.record_lock_wait("display", 1_000_000)
        m.record_lock_wait("display", 0)
        m.record_duplicate_source_open()
        snap = m.snapshot()
        d = snap["domains"]["display"]
        assert d["cache_hit"] == 2 and d["cache_miss"] == 1
        assert d["decode_ns"] == 5_000_000 and d["decode_count"] == 1
        assert d["lock_wait_ns"] == 1_000_000
        assert d["lock_contended"] == 1  # 等待>0 才计
        assert snap["domains"]["prefetch"]["cache_miss"] == 1
        assert snap["duplicate_source_open"] == 1

    def test_reset_zeroes(self):
        m = DecodeMetrics()
        m.record_cache("display", hit=True)
        m.record_decode("prefetch", 1)
        m.reset()
        snap = m.snapshot()
        assert all(v == 0
                   for slot in snap["domains"].values()
                   for v in slot.values())
        assert snap["duplicate_source_open"] == 0

    def test_timed_stream_lock_records_contention(self):
        m = DecodeMetrics()
        lock = threading.Lock()
        lock.acquire()  # 制造竞争
        holder = threading.Thread(
            target=lambda: (time.sleep(0.05), lock.release()))
        holder.start()
        with DecodeMetrics.timed_stream_lock(lock, "display", m):
            pass
        holder.join()
        d = m.snapshot()["domains"]["display"]
        assert d["lock_contended"] == 1
        assert d["lock_wait_ns"] > 0

    def test_unknown_domain_ignored(self):
        m = DecodeMetrics()
        m.record_cache("voodoo", hit=True)   # 不崩不计
        m.record_decode("voodoo", 1)
        assert "voodoo" not in m.snapshot()["domains"]

    def test_thread_safety_under_concurrency(self):
        m = DecodeMetrics()
        def _hammer():
            for _ in range(500):
                m.record_cache("display", hit=True)
                m.record_decode("prefetch", 1)
        threads = [threading.Thread(target=_hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap = m.snapshot()
        assert snap["domains"]["display"]["cache_hit"] == 2000
        assert snap["domains"]["prefetch"]["decode_count"] == 2000


class TestWorkerLifecycle:
    """M16.2：source_viewer CPU fallback worker cancel→in-flight 结束→
    显式 close→线程退出（join）——离屏生命周期证据。"""

    @staticmethod
    def _worker():
        from src.features.edit.ui.source_viewer import (
            _CpuFallbackDecodeWorker,
        )
        return _CpuFallbackDecodeWorker()

    def test_stop_joins_and_disposes(self):
        worker = self._worker()
        worker.start()
        assert worker._thread is not None and worker._thread.is_alive()
        # in-flight 请求后立即 stop：请求被清空（latest-wins 丢弃），
        # 线程感知退出并在线程内 dispose 解码器
        worker.request("/nonexistent.mp4", 0, 0, None)
        worker.stop()
        worker._thread.join(timeout=5.0)
        assert not worker._thread.is_alive()
        assert worker._decoder is None and worker._decoder_key is None

    def test_close_sentinel_disposes_decoder_in_thread(self):
        worker = self._worker()
        # 注入伪解码器（只在 worker 线程触碰——经请求路径触发 key 变化
        # 会真开源，这里直接预置状态后发 CLOSE 哨兵）
        worker.start()
        closed = []
        worker._decoder = NS(close=lambda: closed.append(1))
        worker._decoder_key = ("k", None)
        worker.close_decoder()
        deadline = time.time() + 5.0
        while not closed and time.time() < deadline:
            time.sleep(0.01)
        assert closed, "CLOSE 哨兵应触发解码器在线程内显式 close"
        worker.stop()
        worker._thread.join(timeout=5.0)
        assert not worker._thread.is_alive()

    def test_repeated_cycles_no_thread_leak(self):
        """重复开关（stop→start）回到单线程基线——不依赖 GC。"""
        worker = self._worker()
        for _ in range(5):
            worker.start()
            worker.stop()
            worker._thread.join(timeout=5.0)
            assert not worker._thread.is_alive()
        alive = [t for t in threading.enumerate()
                 if t.name == "SourceViewerCpuFallback"]
        assert len(alive) <= 1  # 只有（可选的）当前 worker 线程
