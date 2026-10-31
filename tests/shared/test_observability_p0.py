"""P0-03/P0-05: GlPassTimer（不可用降级路径）与资源快照单元测试。"""

import threading
import time

import pytest


class _FailingCtx:
    """模拟不支持 timer query 的 GL context。"""

    def query(self, time=False, **kwargs):
        raise RuntimeError("GL_TIME_ELAPSED not supported")


class _RecordingCtx:
    """可注入假 elapsed 的 GL context（验证读回路径）。"""

    def __init__(self, elapsed_ns: int = 2_500_000):
        self.elapsed_ns = elapsed_ns
        self.released = 0

    def query(self, time=False, **kwargs):
        return _FakeQuery(self)


class _FakeQuery:
    def __init__(self, ctx: _RecordingCtx):
        self._ctx = ctx
        self._running = False
        self.elapsed = ctx.elapsed_ns

    def __enter__(self):
        self._running = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self._running = False

    def release(self):
        self._ctx.released += 1


from src.shared.gpu.gl_pass_timer import GlPassTimer
from src.shared.diagnostics.resource_snapshot import (
    RESOURCE_CATEGORIES,
    ResourceSnapshotRegistry,
    ResourceSnapshotSampler,
    get_resource_registry,
)


class TestGlPassTimer:
    def test_unsupported_context_marks_unavailable(self):
        timer = GlPassTimer(_FailingCtx())
        assert timer.unsupported is True
        assert timer.last_ms("composite") is None

    def test_measured_result_roundtrip(self):
        ctx = _RecordingCtx(elapsed_ns=2_500_000)  # 2.5ms
        timer = GlPassTimer(ctx)
        assert timer.unsupported is False
        for _ in range(GlPassTimer.RING_DEPTH + 1):
            timer.begin_frame()
            with timer.measure("composite"):
                pass
            timer.collect()
        ms = timer.last_ms("composite")
        assert ms == pytest.approx(2.5, rel=1e-3)

    def test_ring_depth_no_same_frame_result(self):
        ctx = _RecordingCtx()
        timer = GlPassTimer(ctx)
        timer.begin_frame()
        with timer.measure("composite"):
            pass
        timer.collect()
        # 延迟读回：本帧结果尚未到期
        assert timer.last_ms("composite") is None

    def test_release_clears_queries(self):
        ctx = _RecordingCtx()
        timer = GlPassTimer(ctx)
        timer.begin_frame()
        with timer.measure("composite"):
            pass
        timer.release()
        assert not timer._open


class TestResourceSnapshotRegistry:
    def test_register_and_collect(self):
        reg = ResourceSnapshotRegistry()
        reg.register("playback", "pool", lambda: {"size": 4})
        snap = reg.collect()
        assert snap["playback"]["pool"] == {"size": 4}

    def test_unknown_category_raises(self):
        reg = ResourceSnapshotRegistry()
        with pytest.raises(ValueError):
            reg.register("nope", "x", lambda: {})

    def test_provider_exception_isolated(self):
        reg = ResourceSnapshotRegistry()
        reg.register("playback", "bad", lambda: 1 / 0)
        reg.register("cache", "good", lambda: {"ok": True})
        snap = reg.collect()
        assert "error" in snap["playback"]["bad"]
        assert snap["cache"]["good"] == {"ok": True}

    def test_unregister_owner_prefix(self):
        reg = ResourceSnapshotRegistry()
        reg.register("playback", "edit_a", lambda: {})
        reg.register("playback", "edit_b", lambda: {})
        reg.register("export", "edit_x", lambda: {})
        assert reg.unregister_owner("edit_") == 3
        snap = reg.collect()
        assert snap["playback"] == {}
        assert snap["export"] == {}

    def test_global_registry_categories(self):
        reg = get_resource_registry()
        assert reg is get_resource_registry()
        for cat in RESOURCE_CATEGORIES:
            assert cat in reg.collect()


class TestResourceSnapshotSampler:
    def test_sample_once_system_stats(self):
        sampler = ResourceSnapshotSampler(interval_s=0.2)
        sample = sampler.sample_once()
        assert "ts" in sample
        assert "system" in sample
        assert sample["system"].get("thread_count", 0) >= 1

    def test_background_sampling_and_peaks(self):
        reg = ResourceSnapshotRegistry()
        reg.register("playback", "q", lambda: {"queue_depth": 3})
        sampler = ResourceSnapshotSampler(registry=reg, interval_s=0.1)
        sampler.start()
        time.sleep(0.5)
        sampler.stop()
        samples = sampler.get_samples()
        assert len(samples) >= 2
        peaks = sampler.get_peaks()
        assert peaks.get("playback.q.queue_depth") == 3.0

    def test_export_json(self):
        sampler = ResourceSnapshotSampler(interval_s=0.2)
        sampler.sample_once()
        import json

        data = json.loads(sampler.export_json())
        assert data["summary"]["sample_count"] == 1
