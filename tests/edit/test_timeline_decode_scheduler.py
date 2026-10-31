"""
Stage E unit tests: FrameRingBuffer + ClipDecodeStream + TimelineDecodeScheduler.
对应规格书 §9 / §10 / §11 / §20.1。

核心验收：
- FrameRingBuffer: exact get / evict / flush / hit rate 统计。
- ClipDecodeStream: cursor 隔离 / seek flush / 顺序 decode 优化 / 错帧拒绝。
- TimelineDecodeScheduler: 多轨 bundle / prebuffer / generation 隔离。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.realtime_playback import (
    ClipDecodeStream,
    DecodeManagerMediaSource,
    FrameHandle,
    FrameRingBuffer,
    LayerFrameBundle,
    LayerSpec,
    PlaybackMode,
    SourceKind,
    StreamKey,
    TimelineDecodeScheduler,
)
from src.features.edit.core.realtime_playback.types import FrameIdentity
from src.shared.media.frame import DecodedFrame
from src.shared.media.gpu_frame import GpuDecodedFrame, GpuFramePlane, GpuFrameRelease


# =============================================================================
# Helpers
# =============================================================================


def _make_handle(frame_index: int, clip_id: str = "c1") -> FrameHandle:
    return FrameHandle(
        identity=FrameIdentity(
            timeline_id="t1", clip_id=clip_id, asset_id="a",
            source_path="/x.mov", source_frame=frame_index,
            source_kind=SourceKind.DISK_PROXY,
        ),
        frame_index=frame_index,
        width=1920, height=1080,
        pixel_format="bgr24", bit_depth=8,
        color_space=None,
        pts_seconds=float(frame_index) / 25.0,
        source_kind=SourceKind.DISK_PROXY,
        payload=None,
        payload_type="cpu_ndarray",
    )


def _make_stream_key(clip_id: str = "c1", kind: SourceKind = SourceKind.DISK_PROXY) -> StreamKey:
    return StreamKey(
        timeline_id="t1", clip_id=clip_id, asset_id="a",
        source_kind=kind, source_path="/proxy.mov",
        target_width=1920, target_height=1080,
    )


class _MockMediaSource:
    """模拟 MediaSource：read_frame 返回指定 frame_index 的 DecodedFrame-like。"""

    def __init__(self, total_frames: int = 1000) -> None:
        self.total = total_frames
        self._current = 0
        self.read_frame_calls = 0
        self.read_next_calls = 0

    @property
    def current_frame(self) -> int:
        return self._current

    def read_frame(self, frame_index: int):
        self.read_frame_calls += 1
        if frame_index >= self.total:
            raise IndexError("eof")
        self._current = frame_index
        return self._make_decoded(frame_index)

    def read_next_frame(self):
        self.read_next_calls += 1
        self._current += 1
        if self._current >= self.total:
            raise IndexError("eof")
        return self._make_decoded(self._current)

    def seek_frame(self, frame_index: int) -> None:
        self._current = frame_index

    def close(self) -> None:
        pass

    def _make_decoded(self, frame_index: int):
        d = MagicMock()
        d.frame_index = frame_index
        d.pts = frame_index
        d.data = np.zeros((8, 8, 3), dtype=np.uint8)
        d.pixel_format = "bgr24"
        m = MagicMock()
        m.width = 8
        m.height = 8
        m.fps.to_fps.return_value = 25.0
        d.metadata = m
        return d


class _BlockingMediaSource(_MockMediaSource):
    """Media source whose first read can be released by a test."""

    def __init__(self, total_frames: int = 1000) -> None:
        super().__init__(total_frames)
        self.read_started = threading.Event()
        self.read_release = threading.Event()
        self._block_next_read = True

    def read_frame(self, frame_index: int):
        if self._block_next_read:
            self._block_next_read = False
            self.read_started.set()
            if not self.read_release.wait(timeout=2.0):
                raise TimeoutError("test media source was not released")
        return super().read_frame(frame_index)


# =============================================================================
# FrameRingBuffer
# =============================================================================


class TestFrameRingBuffer:
    def test_put_and_get_exact(self) -> None:
        rb = FrameRingBuffer(capacity=10)
        rb.put(_make_handle(5))
        assert rb.get_exact(5) is not None
        assert rb.get_exact(99) is None

    def test_hit_miss_stats(self) -> None:
        rb = FrameRingBuffer(capacity=10)
        rb.put(_make_handle(1))
        rb.get_exact(1)   # hit
        rb.get_exact(2)   # miss
        s = rb.stats()
        assert s["hits"] == 1
        assert s["misses"] == 1
        assert s["hit_rate"] == 0.5

    def test_capacity_eviction(self) -> None:
        # 最小容量被钳制为 4，用更大的容量测试淘汰
        rb = FrameRingBuffer(capacity=10)
        for i in range(15):
            rb.put(_make_handle(i))
        # 容量 10，淘汰最旧 5 帧
        assert rb.size == 10
        assert rb.contains(0) is False
        assert rb.contains(4) is False
        assert rb.contains(5) is True
        assert rb.contains(14) is True
        s = rb.stats()
        assert s["evicts"] >= 5

    def test_min_capacity_enforced(self) -> None:
        # 容量被钳制为最小 4，避免过小导致频繁淘汰
        rb = FrameRingBuffer(capacity=2)
        assert rb.capacity >= 4

    def test_evict_outside_window(self) -> None:
        rb = FrameRingBuffer(capacity=100)
        for i in range(20):
            rb.put(_make_handle(i))
        evicted = rb.evict_outside_window(5, 10)
        assert evicted >= 14  # 0-4 和 11-19 都在窗口外
        for i in range(5, 11):
            assert rb.contains(i) is True

    def test_clear(self) -> None:
        rb = FrameRingBuffer(capacity=10)
        rb.put(_make_handle(1))
        rb.clear("test")
        assert rb.size == 0
        assert rb.stats()["flushes"] >= 1

    def test_contains_no_stats(self) -> None:
        rb = FrameRingBuffer(capacity=10)
        rb.put(_make_handle(1))
        assert rb.contains(1) is True
        # contains 不影响 hit/miss 统计
        assert rb.stats()["hits"] == 0
        assert rb.stats()["misses"] == 0

    def test_peek_newest(self) -> None:
        rb = FrameRingBuffer(capacity=10)
        rb.put(_make_handle(3))
        rb.put(_make_handle(7))
        newest = rb.peek_newest()
        assert newest is not None
        assert newest.frame_index == 7

    def test_take_exact_transfers_ownership(self) -> None:
        """GPU/compositor consumption removes the handle from the ring."""
        rb = FrameRingBuffer(capacity=10)
        rb.put(_make_handle(7))
        taken = rb.take_exact(7)
        assert taken is not None
        assert taken.frame_index == 7
        assert rb.get_exact(7) is None

    def test_clear_releases_dropped_external_gpu_payload(self) -> None:
        released = []
        payload = GpuDecodedFrame(
            frame_index=8,
            pts=None,
            time_seconds=0.0,
            width=8,
            height=8,
            visible_rect=None,
            pixel_format="rgba",
            bit_depth=8,
            planes=(GpuFramePlane(
                component="RGBA", api="opengl", texture_id=3,
                target="2d", width=8, height=8,
            ),),
            release=GpuFrameRelease(
                release_callback=lambda: released.append(True),
                destructor_policy="thread_safe",
            ),
        )
        handle = _make_handle(8)
        handle.payload = payload
        handle.payload_type = "gpu_texture"
        rb = FrameRingBuffer(capacity=10)
        rb.put(handle)
        rb.clear("test gpu ownership")
        assert released == [True]

    def test_overwrite_moves_to_end(self) -> None:
        rb = FrameRingBuffer(capacity=3)
        rb.put(_make_handle(1))
        rb.put(_make_handle(2))
        rb.put(_make_handle(3))
        # 覆盖 1，应该移到末尾，避免被淘汰
        rb.put(_make_handle(1))
        assert rb.contains(1) is True


# =============================================================================
# ClipDecodeStream
# =============================================================================


class TestClipDecodeStream:
    def test_request_exact_decodes_and_caches(self) -> None:
        src = _MockMediaSource()
        stream = ClipDecodeStream(_make_stream_key(), src, capacity=20)
        stream.request_frame(5, exact=True)
        assert stream.ring.contains(5) is True
        handle = stream.get_ready_frame(5)
        assert handle is not None
        assert handle.frame_index == 5

    def test_sequential_uses_read_next(self) -> None:
        """顺序播放优先 read_next_frame，不每帧 seek（规格书 §10.3）。"""
        src = _MockMediaSource()
        stream = ClipDecodeStream(_make_stream_key(), src, capacity=20)
        stream.request_frame(0, exact=True)
        stream.request_frame(1, exact=True)  # cursor+1 → read_next
        stream.request_frame(2, exact=True)  # cursor+1 → read_next
        assert src.read_next_calls >= 2
        assert stream.ring.contains(2) is True

    def test_foreground_decode_has_priority_over_prefetch(self) -> None:
        """前台精确帧不能排在后台预取后面，且媒体游标不能并发访问。"""
        src = _BlockingMediaSource()
        stream = ClipDecodeStream(_make_stream_key(), src, capacity=20)
        prefetch_result: list[bool] = []
        prefetch_done = threading.Event()

        def run_prefetch() -> None:
            prefetch_result.append(stream.request_frame(0, exact=True, priority=10))
            prefetch_done.set()

        prefetch_thread = threading.Thread(target=run_prefetch)
        prefetch_thread.start()
        assert src.read_started.wait(timeout=1.0)

        foreground_result: list[bool] = []
        foreground_done = threading.Event()

        def run_foreground() -> None:
            foreground_result.append(stream.request_frame(5, exact=True, priority=0))
            foreground_done.set()

        foreground_thread = threading.Thread(target=run_foreground)
        foreground_thread.start()
        assert not foreground_done.wait(timeout=0.05)

        src.read_release.set()
        assert prefetch_done.wait(timeout=2.0)
        assert foreground_done.wait(timeout=2.0)
        prefetch_thread.join(timeout=1.0)
        foreground_thread.join(timeout=1.0)

        assert prefetch_result == [True]
        assert foreground_result == [True]
        assert stream.ring.contains(5) is True

    def test_inflight_decode_is_dropped_after_generation_flush(self) -> None:
        """seek 在读盘期间发生时，旧帧不能回填 ring。"""
        src = _BlockingMediaSource()
        stream = ClipDecodeStream(_make_stream_key(), src, capacity=20)
        result: list[bool] = []
        done = threading.Event()
        generation = stream.generation

        def run_decode() -> None:
            result.append(
                stream.request_frame(
                    7,
                    exact=True,
                    priority=10,
                    expected_generation=generation,
                )
            )
            done.set()

        thread = threading.Thread(target=run_decode)
        thread.start()
        assert src.read_started.wait(timeout=1.0)
        stream.flush("test seek")
        src.read_release.set()
        assert done.wait(timeout=2.0)
        thread.join(timeout=1.0)

        assert result == [False]
        assert stream.ring.size == 0

    def test_rejects_far_wrong_frame_exact(self) -> None:
        """exact 模式拒绝错帧（规格书 §25 wrong_source_frame_presented==0）。"""
        src = _MockMediaSource()
        # read_frame 总是返回错帧（偏移 +5）
        src.read_frame = lambda idx: src._make_decoded(idx + 5)  # type: ignore
        src.read_next_frame = lambda: src._make_decoded(src._current + 5)  # type: ignore
        stream = ClipDecodeStream(_make_stream_key(), src, capacity=20)
        stream.request_frame(10, exact=True)
        # 错帧被拒绝，不缓存
        assert stream.ring.contains(10) is False

    def test_seek_flush_discards_old_frames(self) -> None:
        """seek flush 后旧帧被丢弃（规格书 §10.3）。"""
        src = _MockMediaSource()
        stream = ClipDecodeStream(_make_stream_key(), src, capacity=20)
        stream.request_frame(0, exact=True)
        stream.request_frame(1, exact=True)
        assert stream.ring.size >= 1
        n = stream.flush("seek")
        assert n >= 1
        assert stream.ring.size == 0
        assert stream.generation > 0

    def test_close_clears_ring(self) -> None:
        src = _MockMediaSource()
        stream = ClipDecodeStream(_make_stream_key(), src, capacity=20)
        stream.request_frame(0, exact=True)
        stream.close()
        assert stream.is_ready is False
        assert stream.ring.size == 0

    def test_prefetch_range(self) -> None:
        src = _MockMediaSource()
        stream = ClipDecodeStream(_make_stream_key(), src, capacity=50)
        decoded = stream.prefetch_range(0, 9, direction=1)
        assert decoded >= 1
        # 窗口内的帧应 ready
        for f in range(10):
            assert stream.ring.contains(f) is True

    def test_non_sequential_seeks(self) -> None:
        """非顺序请求会触发 read_frame（内部 seek）。"""
        src = _MockMediaSource()
        stream = ClipDecodeStream(_make_stream_key(), src, capacity=20)
        stream.request_frame(0, exact=True)
        stream.request_frame(50, exact=True)  # 大跳变，非 cursor+1
        assert stream.ring.contains(50) is True

    def test_cursor_isolation_between_streams(self) -> None:
        """同素材不同 clip 各自独立 cursor（规格书 §5.3 / §10.1）。"""
        src1 = _MockMediaSource()
        src2 = _MockMediaSource()
        s1 = ClipDecodeStream(_make_stream_key("clip-A"), src1, capacity=20)
        s2 = ClipDecodeStream(_make_stream_key("clip-B"), src2, capacity=20)
        # s1 读到 frame 5
        s1.request_frame(5, exact=True)
        # s2 读到 frame 20，互不影响
        s2.request_frame(20, exact=True)
        assert s1.ring.contains(5) is True
        assert s1.ring.contains(20) is False
        assert s2.ring.contains(20) is True
        assert s2.ring.contains(5) is False

    def test_handle_preserves_planar_payload_metadata(self) -> None:
        """scheduler 不应把平面帧伪装成 CPU packed 数据。"""
        stream = ClipDecodeStream(_make_stream_key(), None, capacity=8)
        decoded = DecodedFrame(
            frame_index=3,
            pts=None,
            time_seconds=0.12,
            planes=(np.zeros((8, 8), dtype=np.uint16),),
            pixel_format="yuv422p10le",
        )
        handle = stream._build_handle(3, decoded)
        assert handle.payload_type == "planar_yuv"
        assert handle.width == 8
        assert handle.height == 8
        assert handle.bit_depth == 10
        assert handle.pts_seconds == pytest.approx(0.12)

    def test_handle_preserves_gpu_payload_type(self) -> None:
        """GPU 解码帧进入 ring 后应保留为 GPU payload。"""
        stream = ClipDecodeStream(_make_stream_key(), None, capacity=8)
        plane = GpuFramePlane(
            component="RGBA", api="opengl", texture_id=7,
            target="2d", width=8, height=8,
        )
        decoded = GpuDecodedFrame(
            frame_index=4,
            pts=None,
            time_seconds=0.16,
            width=8,
            height=8,
            visible_rect=None,
            pixel_format="rgba",
            bit_depth=10,
            planes=(plane,),
        )
        handle = stream._build_handle(4, decoded)
        assert handle.payload_type == "gpu_texture"
        assert handle.bit_depth == 10
        assert handle.width == 8
        assert handle.height == 8


# =============================================================================
# TimelineDecodeScheduler
# =============================================================================


def _make_layer(clip_id: str, source_kind: SourceKind = SourceKind.DISK_PROXY) -> LayerSpec:
    return LayerSpec(
        clip_id=clip_id, asset_id="a", timeline_id="t1",
        source_path="/proxy.mov", source_kind=source_kind,
        target_width=1920, target_height=1080, visible=True,
    )


class TestTimelineDecodeScheduler:
    def test_source_frame_provider_prefetches_clip_mapping(self) -> None:
        """Speed/reverse mappings must be applied before stream decode."""
        sched = TimelineDecodeScheduler()
        src = _MockMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: src,
            source_frame_provider=lambda frame: {"c1": frame * 2},
        )
        sched.prepare_window(
            center_frame=3, direction=1, radius_before=0, radius_after=2,
            mode=PlaybackMode.REALTIME,
        )
        stream = sched.get_stream("c1")
        assert stream is not None
        assert stream.ring.contains(6) is True
        assert stream.ring.contains(8) is True
        assert stream.ring.contains(10) is True
        sched.shutdown()

    def test_complete_bundle_can_be_consumed_by_gpu_path(self) -> None:
        """A complete bundle transfers handles out of each ring exactly once."""
        sched = TimelineDecodeScheduler()
        src = _MockMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: src,
        )
        sched.prepare_window(
            center_frame=4, direction=1, radius_before=0, radius_after=0,
            mode=PlaybackMode.REALTIME,
        )
        bundle = sched.request_bundle(
            timeline_frame=4,
            generation_id=0,
            deadline_ns=0,
            mode=PlaybackMode.REALTIME,
            consume=True,
        )
        assert bundle is not None and bundle.complete is True
        assert bundle.layers[0].frame_index == 4
        stream = sched.get_stream("c1")
        assert stream is not None
        assert stream.ring.contains(4) is False
        sched.shutdown()

    def test_layer_clip_ids_are_stable_for_consumer_alignment(self) -> None:
        sched = TimelineDecodeScheduler()
        sched.configure(layers=[_make_layer("c1"), _make_layer("c2")])
        assert sched.layer_clip_ids == ("c1", "c2")
        sched.shutdown()

    def test_reconfigure_replaces_stream_when_source_path_changes(self) -> None:
        sched = TimelineDecodeScheduler()
        first = _MockMediaSource()
        second = _MockMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: first,
        )
        old_stream = sched.get_stream("c1")
        assert old_stream is not None

        replacement = LayerSpec(
            clip_id="c1", asset_id="a", timeline_id="t1",
            source_path="/proxy-v2.mov", source_kind=SourceKind.DISK_PROXY,
            target_width=1920, target_height=1080,
        )
        sched.configure(
            layers=[replacement],
            media_source_factory=lambda key: second,
        )
        assert sched.get_stream("c1") is not old_stream
        assert old_stream.is_ready is False
        sched.shutdown()

    def test_async_prefetch_is_latest_only(self) -> None:
        """播放 tick 连续到来时，scheduler 不应堆积无限预取任务。"""
        sched = TimelineDecodeScheduler()
        src = _MockMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: src,
        )
        first = sched.prepare_window_async(
            center_frame=0, direction=1, radius_before=0, radius_after=3,
            mode=PlaybackMode.REALTIME, generation_id=0,
        )
        second = sched.prepare_window_async(
            center_frame=20, direction=1, radius_before=0, radius_after=3,
            mode=PlaybackMode.REALTIME, generation_id=0,
        )
        # first 的结果取决于竞速：若在第二个请求到来前完成则 True；
        # 否则按 latest-only 契约（"False 表示被更近的请求淘汰"）被
        # cancel 并返回 False。两者皆合法，不在此断言。
        first.result(timeout=2.0)
        assert second.result(timeout=2.0) is True
        assert sched.get_stream("c1").ring.contains(20) is True
        stats = sched.stats()
        assert stats["prefetch_submitted"] <= 2
        assert stats["prefetch_active"] is False
        sched.shutdown()

    def test_async_prefetch_is_invalidated_by_generation(self) -> None:
        """seek 后旧窗口 Future 必须结束且不能继续占用新 generation。"""
        sched = TimelineDecodeScheduler()
        src = _MockMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: src,
        )
        sched.reset_generation(7)
        future = sched.prepare_window_async(
            center_frame=0, direction=1, radius_before=0, radius_after=3,
            mode=PlaybackMode.REALTIME, generation_id=7,
        )
        sched.reset_generation(8)
        # The worker may have already completed before the seek; in either
        # case the next request must use generation 8 and not stale work.
        future.result(timeout=2.0)
        next_window = sched.prepare_window_async(
            center_frame=30, direction=1, radius_before=0, radius_after=2,
            mode=PlaybackMode.REALTIME, generation_id=8,
        )
        assert next_window.result(timeout=2.0) is True
        assert sched.stats()["generation"] == 8
        sched.shutdown()

    def test_async_prefetch_cancels_inflight_window_on_generation_change(self) -> None:
        """generation 变化必须中止正在读盘的旧窗口，而非只取消 pending。"""
        sched = TimelineDecodeScheduler()
        src = _BlockingMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: src,
        )
        first = sched.prepare_window_async(
            center_frame=0, direction=1, radius_before=0, radius_after=4,
            mode=PlaybackMode.REALTIME, generation_id=0,
        )
        assert src.read_started.wait(timeout=1.0)
        pending = sched.prepare_window_async(
            center_frame=30, direction=1, radius_before=0, radius_after=2,
            mode=PlaybackMode.REALTIME, generation_id=0,
        )
        sched.reset_generation(1)
        assert pending.result(timeout=2.0) is False
        src.read_release.set()
        assert first.result(timeout=2.0) is False

        stream = sched.get_stream("c1")
        assert stream is not None
        assert stream.ring.size == 0
        assert sched.stats()["prefetch_active"] is False
        sched.shutdown()

    def test_async_prefetch_direction_change_keeps_only_latest_window(self) -> None:
        """播放方向改变时，旧窗口让路给最新窗口。"""
        sched = TimelineDecodeScheduler()
        src = _BlockingMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: src,
        )
        first = sched.prepare_window_async(
            center_frame=0, direction=1, radius_before=0, radius_after=4,
            mode=PlaybackMode.REALTIME, generation_id=0,
        )
        assert src.read_started.wait(timeout=1.0)
        latest = sched.prepare_window_async(
            center_frame=20, direction=-1, radius_before=0, radius_after=2,
            mode=PlaybackMode.REALTIME, generation_id=0,
        )
        src.read_release.set()

        assert first.result(timeout=2.0) is False
        assert latest.result(timeout=2.0) is True
        stream = sched.get_stream("c1")
        assert stream is not None
        assert stream.ring.contains(20) is True
        sched.shutdown()

    def test_async_prefetch_repeated_request_supersedes_pending_cancel(self) -> None:
        """A→B→A：再次到达的 A 不得返回已被取消的 A future。

        回归锁定：早返回只看 request 相等，不看 active 的 cancel 是否已
        被 pending 置位——第二次 A 拿到注定 False 的 future，worker 实际
        执行的是更旧的 B，最新意图被静默丢弃。
        """
        sched = TimelineDecodeScheduler()
        src = _BlockingMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: src,
        )
        first = sched.prepare_window_async(
            center_frame=0, direction=1, radius_before=0, radius_after=4,
            mode=PlaybackMode.REALTIME, generation_id=0,
        )
        assert src.read_started.wait(timeout=1.0)
        second = sched.prepare_window_async(
            center_frame=30, direction=-1, radius_before=0, radius_after=2,
            mode=PlaybackMode.REALTIME, generation_id=0,
        )
        src.read_release.set()
        # A 再次到来：active(A) 的 cancel 已被 B 置位，必须重新成为
        # pending（latest-only），而不是复用 first
        again = sched.prepare_window_async(
            center_frame=0, direction=1, radius_before=0, radius_after=4,
            mode=PlaybackMode.REALTIME, generation_id=0,
        )
        assert again is not first

        assert first.result(timeout=2.0) is False
        assert second.result(timeout=2.0) is False
        assert again.result(timeout=2.0) is True
        stream = sched.get_stream("c1")
        assert stream is not None
        assert stream.ring.contains(0) is True
        sched.shutdown()

    def test_request_bundle_empty_when_unconfigured(self) -> None:
        sched = TimelineDecodeScheduler()
        bundle = sched.request_bundle(
            timeline_frame=0, generation_id=0,
            deadline_ns=0, mode=PlaybackMode.REALTIME,
        )
        assert bundle is not None
        assert bundle.complete is False
        assert "no layers" in (bundle.miss_reason or "")

    def test_single_track_bundle_hit(self) -> None:
        sched = TimelineDecodeScheduler()
        src = _MockMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: src if key.clip_id == "c1" else None,
        )
        # 先 prepare（预取）
        sched.prepare_window(
            center_frame=0, direction=1, radius_before=0, radius_after=10,
            mode=PlaybackMode.REALTIME,
        )
        # 然后 request bundle
        bundle = sched.request_bundle(
            timeline_frame=0, generation_id=0,
            deadline_ns=time.time_ns() + 1_000_000_000,
            mode=PlaybackMode.REALTIME,
        )
        assert bundle is not None
        assert bundle.complete is True
        assert bundle.layer_count == 1

    def test_multi_track_bundle(self) -> None:
        sched = TimelineDecodeScheduler()
        src1 = _MockMediaSource()
        src2 = _MockMediaSource()
        srcs = {"c1": src1, "c2": src2}
        sched.configure(
            layers=[_make_layer("c1"), _make_layer("c2")],
            media_source_factory=lambda key: srcs.get(key.clip_id),
        )
        sched.prepare_window(
            center_frame=5, direction=1, radius_before=0, radius_after=5,
            mode=PlaybackMode.REALTIME,
        )
        bundle = sched.request_bundle(
            timeline_frame=5, generation_id=0,
            deadline_ns=time.time_ns() + 1_000_000_000,
            mode=PlaybackMode.REALTIME,
        )
        assert bundle is not None
        assert bundle.complete is True
        assert bundle.layer_count == 2

    def test_bundle_miss_when_stream_not_ready(self) -> None:
        sched = TimelineDecodeScheduler()
        # media_source_factory 返回 None → stream not ready
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: None,
        )
        bundle = sched.request_bundle(
            timeline_frame=0, generation_id=0,
            deadline_ns=time.time_ns() + 1_000_000_000,
            mode=PlaybackMode.REALTIME,
        )
        assert bundle is not None
        assert bundle.complete is False
        assert "not ready" in (bundle.miss_reason or "")

    def test_stale_generation_returns_none(self) -> None:
        """seek 后旧 generation 的 bundle 结果被丢弃（规格书 §9）。"""
        sched = TimelineDecodeScheduler()
        src = _MockMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: src,
        )
        sched.prepare_window(
            center_frame=0, direction=1, radius_before=0, radius_after=5,
            mode=PlaybackMode.REALTIME,
        )
        # flush_seek 递增 generation
        sched.flush_seek(timeline_frame=100)
        # 用旧 generation 请求 → 返回 None（stale）
        bundle = sched.request_bundle(
            timeline_frame=0, generation_id=0,  # 旧 generation
            deadline_ns=time.time_ns() + 1_000_000_000,
            mode=PlaybackMode.REALTIME,
        )
        assert bundle is None

    def test_realtime_miss_does_not_block(self) -> None:
        """realtime 模式 miss 时不阻塞等待（规格书 §9）。"""
        sched = TimelineDecodeScheduler()
        src = _MockMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: src,
        )
        # 不 prepare，直接 request 一个未预取的远端帧
        bundle = sched.request_bundle(
            timeline_frame=200, generation_id=0,
            deadline_ns=time.time_ns() + 1_000_000,  # 1ms deadline
            mode=PlaybackMode.REALTIME,
        )
        # realtime 应该 miss 返回，不阻塞
        assert bundle is not None
        assert bundle.complete is False

    def test_paused_mode_allows_blocking_decode(self) -> None:
        """paused 模式允许同步 exact 解码（规格书 §9.1）。"""
        sched = TimelineDecodeScheduler()
        src = _MockMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: src,
        )
        # 不预取，paused 模式允许触发 exact decode
        bundle = sched.request_bundle(
            timeline_frame=10, generation_id=0,
            deadline_ns=time.time_ns() + 5_000_000_000,
            mode=PlaybackMode.PAUSED,
        )
        assert bundle is not None
        assert bundle.complete is True  # paused 触发了 exact decode

    def test_flush_seek_clears_all_streams(self) -> None:
        sched = TimelineDecodeScheduler()
        src = _MockMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: src,
        )
        sched.prepare_window(
            center_frame=0, direction=1, radius_before=0, radius_after=5,
            mode=PlaybackMode.REALTIME,
        )
        stream = sched.get_stream("c1")
        assert stream is not None
        assert stream.ring.size >= 1
        n = sched.flush_seek(timeline_frame=100)
        assert n >= 1
        assert stream.ring.size == 0

    def test_evict_outside_window(self) -> None:
        sched = TimelineDecodeScheduler()
        src = _MockMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: src,
        )
        sched.prepare_window(
            center_frame=0, direction=1, radius_before=0, radius_after=20,
            mode=PlaybackMode.REALTIME,
        )
        evicted = sched.evict_outside_window(5, 10)
        assert evicted >= 1

    def test_stats(self) -> None:
        sched = TimelineDecodeScheduler()
        src = _MockMediaSource()
        sched.configure(
            layers=[_make_layer("c1"), _make_layer("c2")],
            media_source_factory=lambda key: src,
        )
        sched.prepare_window(
            center_frame=0, direction=1, radius_before=0, radius_after=5,
            mode=PlaybackMode.REALTIME,
        )
        sched.request_bundle(
            timeline_frame=0, generation_id=0,
            deadline_ns=time.time_ns() + 1_000_000_000,
            mode=PlaybackMode.REALTIME,
        )
        s = sched.stats()
        assert s["layers"] == 2
        assert s["streams"] == 2
        assert s["bundle_hits"] + s["bundle_misses"] >= 1

    def test_shutdown_closes_streams(self) -> None:
        sched = TimelineDecodeScheduler()
        src = _MockMediaSource()
        sched.configure(
            layers=[_make_layer("c1")],
            media_source_factory=lambda key: src,
        )
        sched.shutdown()
        assert sched.get_stream("c1") is None
        s = sched.stats()
        assert s["streams"] == 0


def test_decode_manager_media_source_reuses_gpu_payloads_only() -> None:
    """The scheduler adapter must reject CPU-packed manager results."""
    class _Manager:
        def __init__(self) -> None:
            self.calls = []

        def get_frame(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            if args[1] == 1:
                return ("packed", "cpu")
            return ("planar-frame", "planar")

    key = _make_stream_key("adapter-clip")
    manager = _Manager()
    source = DecodeManagerMediaSource(manager, key)
    assert source.read_frame(1) is None
    assert source.read_frame(2) == "planar-frame"
    assert source.current_frame == 2
    assert manager.calls[1][1]["stream_key"] == "adapter-clip"
    source.close()
