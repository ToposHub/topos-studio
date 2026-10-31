"""
Stage H integration regression tests.
对应规格书 §20.2 / §21。

这些测试守护新架构的关键不变量，防止后续改动回退到旧的"实时路径 fallback
master / CPU QImage 合成 / 多轨 cursor 共享"模型。

覆盖：
- test_edit_realtime_no_master_fallback: realtime 不解 master
- test_edit_realtime_no_cpu_qimage: realtime 不走 QImage 主路径（GPU compositor）
- test_edit_realtime_seek_generation: seek 后旧帧丢弃
- test_edit_realtime_multitrack_stream_isolation: 多轨同素材 cursor 隔离
- test_edit_proxy_ready_switch: proxy ready 热切换
- test_edit_render_cache_priority: render cache 优先
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, call

import numpy as np
import pytest
from PyQt6.QtCore import QCoreApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.realtime_playback import (
    ClipDecodeStream,
    FrameRingBuffer,
    GpuFramePool,
    LayerSpec,
    PlaybackFrameTelemetry,
    PlaybackMode,
    RealtimePlaybackPolicy,
    RenderCacheAdapter,
    RenderCacheEntry,
    RenderCacheKey,
    SourceKind,
    StreamKey,
    TelemetryAggregator,
    TimelineDecodeScheduler,
    TimelineGpuCompositor,
    TimelineGpuRenderTask,
    GpuLayerInput,
    build_render_hash,
)
from src.features.edit.core.realtime_playback.types import (
    DecodeMiss,
    FrameHandle,
    FrameIdentity,
    LayerFrameBundle,
)


@pytest.fixture(scope="module")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


# =============================================================================
# Helpers
# =============================================================================


class _MockMediaSource:
    def __init__(self, total: int = 1000) -> None:
        self.total = total
        self._current = 0
        self.read_frame_calls = 0
        self.read_next_calls = 0

    @property
    def current_frame(self) -> int:
        return self._current

    def read_frame(self, idx: int):
        self.read_frame_calls += 1
        self._current = idx
        return self._make(idx)

    def read_next_frame(self):
        self.read_next_calls += 1
        self._current += 1
        return self._make(self._current)

    def seek_frame(self, idx: int) -> None:
        self._current = idx

    def close(self) -> None: pass

    def _make(self, idx: int):
        d = MagicMock()
        d.frame_index = idx
        d.pts = idx
        d.data = np.zeros((8, 8, 3), dtype=np.uint8)
        d.pixel_format = "bgr24"
        m = MagicMock(); m.width = 8; m.height = 8
        m.fps.to_fps.return_value = 25.0
        d.metadata = m
        return d


def _make_frame_handle(idx: int = 0, clip_id: str = "c1") -> FrameHandle:
    return FrameHandle(
        identity=FrameIdentity(
            timeline_id="t", clip_id=clip_id, asset_id="a",
            source_path="/x.mov", source_frame=idx, source_kind=SourceKind.DISK_PROXY,
        ),
        frame_index=idx, width=8, height=8,
        pixel_format="bgr24", bit_depth=8, color_space=None,
        pts_seconds=float(idx)/25.0, source_kind=SourceKind.DISK_PROXY,
        payload=MagicMock(), payload_type="gpu_texture",
    )


# =============================================================================
# §20.2 test_edit_realtime_no_master_fallback (end-to-end via policy)
# =============================================================================


class TestRealtimeNoMasterFallback:
    """规格书 §15.2 / §21：实时播放中 trace 不出现 master fallback。"""

    def test_telematology_captures_zero_realtime_master(self) -> None:
        """模拟 60 帧 realtime 播放，telemetry 应 realtime_master_requests==0。"""
        agg = TelemetryAggregator(window=60)
        policy = RealtimePlaybackPolicy()
        # 模拟 disk proxy ready
        class _Proxy:
            has_disk_proxy_ready = True
            is_disk_proxy_generating = False
            has_disk_proxy_failed = False
            has_memory_proxy = False
            disk_proxy_path = "/proxy.mov"
        for i in range(60):
            decision = policy.decide_layer_source(
                mode=PlaybackMode.REALTIME,
                asset_id="a", source_path="/x", timeline_frame=i,
                source_frame=i, clip_id="c", requires_effect_cache=False,
                proxy_state=_Proxy(),
            )
            agg.record(PlaybackFrameTelemetry(
                timeline_frame=i, mode="realtime",
                source_kinds=(decision.source_kind.value,),
                master_requested=decision.allow_master_fallback,  # 应为 False
            ))
        s = agg.summary()
        assert s["realtime_master_requests"] == 0
        passed, _ = agg.assert_realtime_invariants()
        assert passed

    def test_no_master_in_full_state_matrix(self) -> None:
        """遍历所有 proxy 状态，realtime 模式都不解 master。"""
        policy = RealtimePlaybackPolicy()
        states = [
            None,
            type("S", (), {"has_disk_proxy_ready": True, "is_disk_proxy_generating": False,
                           "has_disk_proxy_failed": False, "has_memory_proxy": False,
                           "disk_proxy_path": "/p"})(),
            type("S", (), {"has_disk_proxy_ready": False, "is_disk_proxy_generating": True,
                           "has_disk_proxy_failed": False, "has_memory_proxy": False,
                           "disk_proxy_path": None})(),
            type("S", (), {"has_disk_proxy_ready": False, "is_disk_proxy_generating": False,
                           "has_disk_proxy_failed": True, "has_memory_proxy": False,
                           "disk_proxy_path": None})(),
            type("S", (), {"has_disk_proxy_ready": False, "is_disk_proxy_generating": False,
                           "has_disk_proxy_failed": False, "has_memory_proxy": True,
                           "disk_proxy_path": None})(),
        ]
        for state in states:
            d = policy.decide_layer_source(
                mode=PlaybackMode.REALTIME,
                asset_id="a", source_path="/x", timeline_frame=0,
                source_frame=0, clip_id="c", requires_effect_cache=False,
                proxy_state=state,
            )
            assert d.source_kind is not SourceKind.MASTER
            assert d.allow_master_fallback is False


# =============================================================================
# §20.2 test_edit_realtime_no_cpu_qimage
# =============================================================================


class TestRealtimeNoCpuQimage:
    """规格书 §13.3：realtime trace 中 cpu_qimage_frames==0。"""

    def test_gpu_compositor_stats_zero_qimage(self, qapp) -> None:
        """GPU compositor 统计中 cpu_qimage_frames 必须为 0。"""
        c = TimelineGpuCompositor()
        # 即使提交多帧，cpu_qimage 仍为 0
        backend = MagicMock()
        backend.submit.return_value = True
        c.attach_backend(backend)
        for i in range(30):
            task = TimelineGpuRenderTask(
                timeline_frame=i, generation_id=0,
                output_width=1920, output_height=1080,
                layers=(GpuLayerInput(
                    clip_id="c1", track_index=0, opacity=1.0,
                    transform=None, crop=None, frame=_make_frame_handle(i),
                ),),
            )
            c.submit(task)
        s = c.stats()
        assert s["cpu_qimage_frames"] == 0
        assert s["readback_count"] == 0
        assert s["gpu_composite_frames"] == 30


# =============================================================================
# §20.2 test_edit_realtime_seek_generation
# =============================================================================


class TestRealtimeSeekGeneration:
    """规格书 §14.2 / §21：seek 后旧 generation 的 frame 不展示。"""

    def test_stale_bundle_rejected(self) -> None:
        """request_bundle 用旧 generation 调用时返回 None。"""
        sched = TimelineDecodeScheduler()
        src = _MockMediaSource()
        sched.configure(
            layers=[LayerSpec(clip_id="c1", asset_id="a", timeline_id="t",
                              source_path="/p", source_kind=SourceKind.DISK_PROXY)],
            media_source_factory=lambda key: src,
        )
        sched.prepare_window(center_frame=0, direction=1,
                              radius_before=0, radius_after=10, mode=PlaybackMode.REALTIME)
        # 正常 bundle
        bundle = sched.request_bundle(
            timeline_frame=0, generation_id=0,
            deadline_ns=time.time_ns() + 1_000_000_000,
            mode=PlaybackMode.REALTIME,
        )
        assert bundle is not None
        assert bundle.generation_id == 0
        assert not bundle.is_stale(current_generation_id=0)

        # seek flush 递增 generation
        sched.flush_seek(timeline_frame=500)
        # 旧 generation 请求 → None（stale，不展示）
        stale_bundle = sched.request_bundle(
            timeline_frame=0, generation_id=0,
            deadline_ns=time.time_ns() + 1_000_000_000,
            mode=PlaybackMode.REALTIME,
        )
        assert stale_bundle is None

    def test_ring_flushed_on_seek(self) -> None:
        """seek flush 后 ring buffer 清空，旧帧不残留。"""
        sched = TimelineDecodeScheduler()
        src = _MockMediaSource()
        sched.configure(
            layers=[LayerSpec(clip_id="c1", asset_id="a", timeline_id="t",
                              source_path="/p", source_kind=SourceKind.DISK_PROXY)],
            media_source_factory=lambda key: src,
        )
        sched.prepare_window(center_frame=0, direction=1,
                              radius_before=0, radius_after=10, mode=PlaybackMode.REALTIME)
        stream = sched.get_stream("c1")
        assert stream is not None
        assert stream.ring.size >= 1
        sched.flush_seek(timeline_frame=100)
        assert stream.ring.size == 0


# =============================================================================
# §20.2 test_edit_realtime_multitrack_stream_isolation
# =============================================================================


class TestMultitrackStreamIsolation:
    """规格书 §5.3 / §10.1：多轨同素材 cursor 隔离。"""

    def test_same_asset_different_clips_isolated(self) -> None:
        """同素材两个 clip 各自独立 stream，cursor 不互相 seek。"""
        src1 = _MockMediaSource()
        src2 = _MockMediaSource()
        s1 = ClipDecodeStream(
            StreamKey(timeline_id="t", clip_id="clip-A", asset_id="same-asset",
                      source_kind=SourceKind.DISK_PROXY, source_path="/same.mov",
                      target_width=1920, target_height=1080),
            src1, capacity=20,
        )
        s2 = ClipDecodeStream(
            StreamKey(timeline_id="t", clip_id="clip-B", asset_id="same-asset",
                      source_kind=SourceKind.DISK_PROXY, source_path="/same.mov",
                      target_width=1920, target_height=1080),
            src2, capacity=20,
        )
        s1.request_frame(5, exact=True)
        s2.request_frame(50, exact=True)
        # 各自 ring 只有自己的帧
        assert s1.ring.contains(5) is True
        assert s1.ring.contains(50) is False
        assert s2.ring.contains(50) is True
        assert s2.ring.contains(5) is False

    def test_scheduler_multi_track_no_cursor_collision(self) -> None:
        """scheduler 多轨调度时，stream 之间不互相 seek。"""
        sched = TimelineDecodeScheduler()
        src1 = _MockMediaSource()
        src2 = _MockMediaSource()
        srcs = {"c1": src1, "c2": src2}
        sched.configure(
            layers=[
                LayerSpec(clip_id="c1", asset_id="a", timeline_id="t",
                          source_path="/p", source_kind=SourceKind.DISK_PROXY),
                LayerSpec(clip_id="c2", asset_id="a", timeline_id="t",
                          source_path="/p", source_kind=SourceKind.DISK_PROXY),
            ],
            media_source_factory=lambda key: srcs.get(key.clip_id),
        )
        # 各自预取不同范围
        s1 = sched.get_stream("c1")
        s2 = sched.get_stream("c2")
        s1.prefetch_range(0, 5, direction=1)
        s2.prefetch_range(100, 105, direction=1)
        # c1 只有 0-5，c2 只有 100-105
        assert s1.ring.contains(2) is True
        assert s1.ring.contains(102) is False
        assert s2.ring.contains(102) is True
        assert s2.ring.contains(2) is False


# =============================================================================
# §20.2 test_edit_proxy_ready_switch
# =============================================================================


class TestProxyReadySwitch:
    """规格书 §7.2：proxy ready 后热切换 source。"""

    def test_set_source_switches_and_clears_ring(self) -> None:
        src1 = _MockMediaSource()
        stream = ClipDecodeStream(
            StreamKey(timeline_id="t", clip_id="c", asset_id="a",
                      source_kind=SourceKind.DISK_PROXY, source_path="/p",
                      target_width=1920, target_height=1080),
            src1, capacity=20,
        )
        stream.request_frame(0, exact=True)
        assert stream.ring.size >= 1
        # proxy ready 热切换
        src2 = _MockMediaSource()
        stream.set_source(src2)
        # ring 被清空，cursor 重置
        assert stream.ring.size == 0
        assert stream.is_ready
        # 新 source 可以解码
        stream.request_frame(0, exact=True)
        assert stream.ring.contains(0) is True


# =============================================================================
# §20.2 test_edit_render_cache_priority
# =============================================================================


class TestRenderCachePriority:
    """规格书 §8.3：cache ready 后优先选 RENDER_CACHE。"""

    def test_cache_priority_over_disk_proxy(self) -> None:
        policy = RealtimePlaybackPolicy()
        adapter = RenderCacheAdapter()
        # 注册 ready cache
        from src.features.edit.core.realtime_playback import RenderCacheEntry
        adapter.upsert_entry(RenderCacheEntry(
            key=RenderCacheKey(
                timeline_id="t", clip_id="c", asset_id="a", source_path="/x",
                frame_rate_num=25, frame_rate_den=1, width=1920, height=1080,
                render_hash="rh",
            ),
            status="valid", cache_path="/cache/c.mov",
            width=1920, height=1080, start_frame=0, duration_frames=100,
        ))
        # 同时有 disk proxy ready
        class _Proxy:
            has_disk_proxy_ready = True
            is_disk_proxy_generating = False
            has_disk_proxy_failed = False
            has_memory_proxy = False
            disk_proxy_path = "/proxy.mov"
        d = policy.decide_layer_source(
            mode=PlaybackMode.REALTIME,
            asset_id="a", source_path="/x", timeline_frame=0,
            source_frame=0, clip_id="c", requires_effect_cache=True,
            proxy_state=_Proxy(),
            render_cache_state=adapter,
        )
        assert d.source_kind is SourceKind.RENDER_CACHE


# =============================================================================
# §25 关键断言汇总
# =============================================================================


class TestKeyInvariants:
    """规格书 §25：所有实现都要守住这些断言。"""

    def test_all_invariants_pass_in_clean_session(self) -> None:
        agg = TelemetryAggregator(window=60)
        for i in range(60):
            agg.record(PlaybackFrameTelemetry(
                timeline_frame=i, mode="realtime",
                source_kinds=("disk_proxy",),
                ring_hit=True, total_ms=2.0,
                composite_ms=1.0,
            ))
        passed, msg = agg.assert_realtime_invariants()
        assert passed, msg
        s = agg.summary()
        assert s["realtime_master_requests"] == 0
        assert s["wrong_source_frame"] == 0
        assert s["stale_generation"] == 0

    def test_gpu_compositor_no_cpu_qimage_invariant(self, qapp) -> None:
        c = TimelineGpuCompositor()
        assert c.stats()["cpu_qimage_frames"] == 0
        assert c.stats()["readback_count"] == 0

    def test_gpu_pool_no_decode_thread_gl_invariant(self, qapp) -> None:
        pool = GpuFramePool()
        assert pool.stats()["gl_calls_from_decode_thread"] == 0

    def test_stream_rejects_far_wrong_frame(self) -> None:
        """规格书 §25：wrong_source_frame_presented == 0。"""
        src = _MockMediaSource()
        src.read_frame = lambda idx: src._make(idx + 10)  # 总是偏 +10
        stream = ClipDecodeStream(
            StreamKey(timeline_id="t", clip_id="c", asset_id="a",
                      source_kind=SourceKind.DISK_PROXY, source_path="/p",
                      target_width=1920, target_height=1080),
            src, capacity=20,
        )
        stream.request_frame(5, exact=True)
        # 错帧被拒绝，不缓存
        assert stream.ring.contains(5) is False
