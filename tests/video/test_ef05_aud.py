"""批 EF-05：src/shared/video 的 24 条 Suggestion 级审计发现修复验证。

覆盖 AUD-38803/38804/38902/38907/39006/39108/39111/39204/39214/39304/
39311/39317/39402/39403/39408/39412/39512(BLOCKED 不测)/39513/39514/
39515/39610/39715/39810/39811。

原则：
- 不真实解码视频（全部 mock / 合成数据），不弹模态对话框；
- 修复回退时对应测试必须失败（结构断言 + 行为断言双重锚定）。
"""

from __future__ import annotations

import inspect
import threading
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.shared.video.decode_worker import (
    BaseDecodeWorker,
    DecodeWorkerManager,
    DecodeWorkerStats,
    ProxyDecodeWorker,
)
from src.shared.video.frame_provider_service import VideoFrameProviderService
from src.shared.video.frame_queue import (
    FrameSource,
    QueueEntry,
    ReadyFrameQueue,
    gpu_frame_release_thread_id,
    set_gpu_frame_release_thread,
)
from src.shared.video.fallback_types import (
    FallbackEvent,
    VideoPipelineFallbackReason,
    clear_fallback_events,
    get_recent_fallback_events,
    log_fallback,
)
import src.shared.video.fallback_types as fallback_types_module
from src.shared.video.monitor_output import (
    DeckLinkMonitorConfig,
    FfmpegDeckLinkMonitorOutput,
    NativeDeckLinkSDKMonitorOutput,
)
from src.shared.video.pixel_copy_counters import (
    CopyKind,
    add_pixel_copy,
    reset_pixel_copies,
    snapshot_pixel_copies,
)
from src.shared.video.playback_clock import PlaybackClock
from src.shared.video.playback_frame_trace import (
    EditPlaybackFrameTrace,
    FrameTraceCollector,
)
from src.shared.video.playback_state import PlaybackCoordinator
from src.shared.video.session import VideoSession
from src.shared.video.video_async_renderer import VideoAsyncRenderer as VideoRenderThread
from src.shared.video.video_playback import ProxyPrefetchWorker
from src.shared.video.video_upload import YuvPlaneUploader
from src.shared.video.yuv_gpu_upload import YUVGPUUploader
from src.shared.video.denoise.frame_types import TemporalFrameWindowCache
import src.shared.video.denoise.window_provider as window_provider_module


# =============================================================================
# 辅助
# =============================================================================

class SpyLock:
    """记录 acquire 次数的假锁（结构断言：修复路径确实过锁）。"""

    def __init__(self) -> None:
        self.acquisitions: List[int] = []

    def __enter__(self) -> "SpyLock":
        self.acquisitions.append(1)
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _make_proxy_worker() -> ProxyDecodeWorker:
    return ProxyDecodeWorker(
        frame_provider=MagicMock(),
        frame_queue=ReadyFrameQueue(),
    )


def _queue_entry(frame_index: int, generation_id: int = 0) -> QueueEntry:
    return QueueEntry(
        frame=SimpleNamespace(frame_index=frame_index),
        decode_ms=1.0,
        source=FrameSource.PROXY,
        can_drop=True,
        generation_id=generation_id,
    )


# =============================================================================
# AUD-38803: DecodeWorkerStats 跨线程读写加锁
# =============================================================================

class TestAud38803DecodeWorkerStatsLock:
    def test_all_mutators_and_snapshot_go_through_lock(self) -> None:
        stats = DecodeWorkerStats()
        spy = SpyLock()
        stats._stats_lock = spy  # type: ignore[assignment]

        stats.update_decode(3.0, 5)
        stats.update_drop()
        stats.update_seek()
        stats.update_queue_full()
        stats.update_error()
        stats.update_dedup()
        summary = stats.to_dict()

        # 6 次更新 + 1 次快照，全部持锁
        assert len(spy.acquisitions) == 7
        assert summary['frames_decoded'] == 1
        assert summary['decode_avg_ms'] == pytest.approx(3.0)
        assert summary['last_frame_index'] == 5

    def test_concurrent_updates_do_not_lose_counts(self) -> None:
        stats = DecodeWorkerStats()
        n_threads, per_thread = 8, 400
        barrier = threading.Barrier(n_threads)

        def worker() -> None:
            barrier.wait()
            for _ in range(per_thread):
                stats.update_drop()

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)

        assert stats.frames_dropped == n_threads * per_thread


# =============================================================================
# AUD-38804: _last_clock_frame/_prefetch_lookahead/_current_decode_frame 同步
# =============================================================================

class TestAud38804ClockStateSync:
    def test_proxy_worker_clock_writes_go_through_lock(self) -> None:
        worker = _make_proxy_worker()
        spy = SpyLock()
        worker._clock_state_lock = spy  # type: ignore[assignment]
        worker.request_prefetch = MagicMock()  # type: ignore[method-assign]

        worker.set_prefetch_lookahead(4)
        assert len(spy.acquisitions) == 1

        worker.update_clock_position(10, generation_id=1)
        assert len(spy.acquisitions) == 2
        worker.request_prefetch.assert_called_once_with(
            current_frame=10, lookahead=4, generation_id=1,
        )

        # 相同帧号不重复触发预读
        worker.update_clock_position(10, generation_id=1)
        assert len(spy.acquisitions) == 3
        assert worker.request_prefetch.call_count == 1

    def test_current_decode_frame_read_write_locked(self) -> None:
        worker = _make_proxy_worker()
        spy = SpyLock()
        worker._decode_position_lock = spy  # type: ignore[assignment]

        worker._set_current_decode_frame(42)
        assert worker.current_decode_frame == 42
        # 写 1 次 + 读 1 次都过锁
        assert len(spy.acquisitions) == 2

    def test_manager_prefetch_lookahead_write_locked(self) -> None:
        manager = DecodeWorkerManager(
            frame_provider=MagicMock(),
            frame_queue=ReadyFrameQueue(),
        )
        spy = SpyLock()
        manager._prefetch_lookahead_lock = spy  # type: ignore[assignment]

        manager.set_prefetch_lookahead(3)
        assert len(spy.acquisitions) == 1
        assert manager._prefetch_lookahead == 3


# =============================================================================
# AUD-38902: TemporalFrameWindowCache 状态注记与不可变契约
# =============================================================================

class TestAud38902WindowCacheDocstring:
    def test_docstring_marks_unused_status_and_immutability(self) -> None:
        doc = TemporalFrameWindowCache.__doc__ or ''
        # 回退（删除状态注记）→ 此断言失败
        assert 'AUD-38902' in doc
        assert '不可变契约' in doc
        assert '无生产' in doc or '无消费者' in doc

    def test_put_get_invalidate_roundtrip(self) -> None:
        cache = TemporalFrameWindowCache(memory_budget_mb=1.0)
        window = SimpleNamespace(
            all_frames_ordered=lambda: [SimpleNamespace(data=np.zeros((2, 2), np.uint8))],
        )
        cache.put('c', 0, 'proxy', 1, 'yuv420p', (64, 64), window)
        assert cache.get('c', 0, 'proxy', 1, 'yuv420p', (64, 64)) is window
        cache.invalidate_generation('c', 2)
        assert cache.get('c', 0, 'proxy', 1, 'yuv420p', (64, 64)) is None


# =============================================================================
# AUD-38907: window_provider docstring 宽度上限与常量一致
# =============================================================================

class TestAud38907WidthDoc:
    def test_docstring_matches_max_neighbor_width(self) -> None:
        doc = window_provider_module.__doc__ or ''
        assert '2560' in doc
        # 回退（恢复 1920 描述）→ 此断言失败
        assert '宽度上限 1920' not in doc
        assert window_provider_module._MAX_NEIGHBOR_WIDTH == 2560


# =============================================================================
# AUD-39006: fallback_types docstring 示例可执行
# =============================================================================

class TestAud39006FallbackDocstring:
    def test_docstring_example_uses_kwarg_api(self) -> None:
        doc = fallback_types_module.__doc__ or ''
        # 回退（恢复错误的 log_fallback(event) 用法）→ 此断言失败
        assert 'log_fallback(event)' not in doc
        assert 'event = log_fallback(' in doc

    def test_documented_example_executes(self) -> None:
        clear_fallback_events()
        try:
            event = log_fallback(
                reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
                codec='prores',
                pixel_format='yuv422p10le',
                backend='ffmpeg',
                source_type='master_only',
                exception=RuntimeError('PyAV context creation failed'),
                user_forced=False,
            )
            assert isinstance(event, FallbackEvent)
            events = get_recent_fallback_events(limit=10)
            assert events, "示例调用应记录事件"
            assert events[-1].reason == VideoPipelineFallbackReason.V3_INIT_FAILED
            # to_log_string 不抛 AttributeError（旧示例照抄会在此炸）
            assert 'v3_init_failed' in event.to_log_string()
        finally:
            clear_fallback_events()


# =============================================================================
# AUD-39108: 全局统计计数加锁
# =============================================================================

class TestAud39108ServiceStatsLock:
    def test_cache_hit_rate_reads_under_stats_lock(self) -> None:
        service = VideoFrameProviderService()
        spy = SpyLock()
        service._stats_lock = spy  # type: ignore[assignment]
        service._total_cache_hits = 3
        service._total_cache_misses = 1

        rate = service.cache_hit_rate
        assert rate == pytest.approx(0.75)
        # 回退（无锁读取）→ acquisitions 为空 → 失败
        assert spy.acquisitions, "cache_hit_rate 应经 _stats_lock 读取"

    def test_total_frames_served_reads_under_stats_lock(self) -> None:
        service = VideoFrameProviderService()
        spy = SpyLock()
        service._stats_lock = spy  # type: ignore[assignment]
        service._total_frames_served = 7

        assert service.total_frames_served == 7
        assert len(spy.acquisitions) == 1


# =============================================================================
# AUD-39111: prefetch 同步语义文档 + 循环内滚动淘汰
# =============================================================================

class TestAud39111Prefetch:
    def _make_service_with_stub_session(self) -> tuple:
        service = VideoFrameProviderService()
        evict_centers: List[int] = []
        cached_frames: List[int] = []
        session = SimpleNamespace(
            is_open=True,
            touch=lambda: None,
            resolve_cache_quality=lambda q: q,
            decode_waiters_lock=threading.Lock(),
            decode_waiters=0,
            decode_lock=threading.Lock(),
            get_cached_frame=lambda idx, q: None,
            cache_frame=lambda idx, frame, q: cached_frames.append(idx),
            evict_old_frames=lambda center, q: evict_centers.append(center),
        )
        service._clips['c1'] = session
        service._decode_frame = (  # type: ignore[method-assign]
            lambda session_, idx, q: SimpleNamespace(index=idx)
        )
        return service, evict_centers, cached_frames

    def test_prefetch_docstring_documents_synchronous_caller_duty(self) -> None:
        doc = VideoFrameProviderService.prefetch.__doc__ or ''
        # 回退（恢复"在后台预解码"的误导描述）→ 失败
        assert '调用线程同步' in doc
        assert '在后台预解码帧' not in doc

    def test_prefetch_evicts_inside_loop(self) -> None:
        service, evict_centers, cached_frames = self._make_service_with_stub_session()
        service.prefetch('c1', 0, 4, 'proxy')

        assert cached_frames == [0, 1, 2, 3, 4]
        # 回退（淘汰只在循环后执行一次，center=(start+end)//2）→
        # evict_centers == [2]，断言失败
        assert evict_centers == [0, 1, 2, 3, 4]


# =============================================================================
# AUD-39204: 过期帧/更旧帧清除单次遍历重建
# =============================================================================

class TestAud39204QueueRebuild:
    def test_drop_stale_single_pass_structure(self) -> None:
        src = inspect.getsource(ReadyFrameQueue._drop_stale_frames)
        # 回退（恢复逐个 list.remove）→ 失败
        assert '.remove(' not in src
        assert 'queue[:] =' in src

    def test_dequeue_latest_single_pass_structure(self) -> None:
        src = inspect.getsource(ReadyFrameQueue.dequeue_latest_before_or_at)
        assert '.remove(' not in src
        assert 'queue[:] =' in src

    def test_drop_stale_behavior_preserved(self) -> None:
        q = ReadyFrameQueue()
        q._current_generation = 2
        queue = [_queue_entry(1, 1), _queue_entry(2, 2), _queue_entry(3, 1)]

        dropped = q._drop_stale_frames(queue)

        assert dropped == 2
        assert [e.frame_index for e in queue] == [2]

    def test_dequeue_latest_drops_older_frames(self) -> None:
        q = ReadyFrameQueue()
        q._proxy_queue = [
            _queue_entry(1), _queue_entry(2), _queue_entry(5),
        ]

        result = q.dequeue_latest_before_or_at(5, is_master=False)

        assert result.found is True
        assert result.actual_frame_index == 5
        assert result.older_frames_discarded == 2
        assert q._proxy_queue == []


# =============================================================================
# AUD-39214: 丢帧计数加锁
# =============================================================================

class _AliveThread:
    """存活线程上下文（is_running 判定需要 alive thread）。"""

    def __enter__(self) -> threading.Thread:
        self._thread = threading.Thread(target=lambda: None)
        self._event = threading.Event()
        self._thread = threading.Thread(
            target=self._event.wait, daemon=True)
        self._thread.start()
        return self._thread

    def __exit__(self, *exc: Any) -> None:
        self._event.set()
        self._thread.join(timeout=5.0)


class TestAud39214DroppedCounterLock:
    def test_ffmpeg_backend_drop_path_locked(self) -> None:
        output = FfmpegDeckLinkMonitorOutput(DeckLinkMonitorConfig(queue_size=2))
        spy = SpyLock()
        output._drop_counter_lock = spy  # type: ignore[assignment]

        with _AliveThread() as thread:
            output._thread = thread
            output._started = True
            # 填满队列
            output._queue.put((0, b'f1'))
            output._queue.put((1, b'f2'))
            assert output.submit_frame_payload(2, b'f3') is True

        # 队列满 → 淘汰 1 帧 → 计数经锁自增（回退 → AttributeError/空）
        assert output.dropped_frames == 1
        assert len(spy.acquisitions) == 2  # 1 自增 + 1 属性读取

    def test_native_backend_has_drop_counter_lock(self) -> None:
        output = NativeDeckLinkSDKMonitorOutput(DeckLinkMonitorConfig(queue_size=2))
        assert hasattr(output, '_drop_counter_lock')

        spy = SpyLock()
        output._drop_counter_lock = spy  # type: ignore[assignment]
        with _AliveThread() as thread:
            output._thread = thread
            output._running = True
            output._queue.put((0, b'f1'))
            output._queue.put((1, b'f2'))
            assert output.submit_frame_payload(2, b'f3') is True

        assert output._dropped_frames == 1
        assert len(spy.acquisitions) == 1


# =============================================================================
# AUD-39304: 负数字节拒绝
# =============================================================================

class TestAud39304NegativeBytesRejected:
    def setup_method(self) -> None:
        reset_pixel_copies()

    def teardown_method(self) -> None:
        reset_pixel_copies()

    def test_negative_bytes_raise(self) -> None:
        # 回退（恢复静默 return）→ 不抛异常 → 失败
        with pytest.raises(ValueError, match='negative'):
            add_pixel_copy(CopyKind.CPU_UPLOAD, -5)

    def test_negative_bytes_do_not_corrupt_counters(self) -> None:
        add_pixel_copy(CopyKind.CPU_UPLOAD, 100)
        with pytest.raises(ValueError):
            add_pixel_copy(CopyKind.CPU_UPLOAD, -1)
        snap = snapshot_pixel_copies()
        assert snap[CopyKind.CPU_UPLOAD] == {'bytes': 100, 'events': 1}

    def test_zero_bytes_silently_skipped(self) -> None:
        add_pixel_copy(CopyKind.CPU_UPLOAD, 0)
        snap = snapshot_pixel_copies()
        assert snap[CopyKind.CPU_UPLOAD] == {'bytes': 0, 'events': 0}

    def test_unknown_kind_still_rejected(self) -> None:
        with pytest.raises(ValueError, match='unknown copy kind'):
            add_pixel_copy('not_a_kind', 10)

    def test_positive_bytes_counted(self) -> None:
        add_pixel_copy(CopyKind.TEXTURE_WRITE, 256)
        snap = snapshot_pixel_copies()
        assert snap[CopyKind.TEXTURE_WRITE]['bytes'] == 256
        assert snap[CopyKind.TEXTURE_WRITE]['events'] == 1


# =============================================================================
# AUD-39311: fps=0 除零防护
# =============================================================================

class TestAud39311ZeroFpsSafety:
    def test_zero_fps_clock_operations_do_not_crash(self) -> None:
        clock = PlaybackClock(fps=0.0, total_frames=100)
        # 回退（恢复裸除法）→ ZeroDivisionError → 失败
        clock.seek_to_frame(10)
        clock.start()
        assert clock.get_time_for_frame(50) == pytest.approx(2.0)
        # start() 后 clock_time = current_frame / 安全帧率(25)
        assert clock.get_state().clock_time == pytest.approx(10 / 25.0)
        assert clock.get_remaining_time() == pytest.approx(90 / 25.0)

    def test_normal_fps_unaffected(self) -> None:
        clock = PlaybackClock(fps=50.0, total_frames=100)
        clock.seek_to_frame(10)
        clock.start()
        assert clock.get_time_for_frame(50) == pytest.approx(1.0)
        assert clock.get_state().clock_time == pytest.approx(10 / 50.0)
        assert clock.get_remaining_time() == pytest.approx(90 / 50.0)

    def test_negative_fps_uses_fallback(self) -> None:
        clock = PlaybackClock(fps=-1.0, total_frames=100)
        clock.seek_to_frame(25)
        assert clock.get_time_for_frame(25) == pytest.approx(1.0)


# =============================================================================
# AUD-39317: export_json 同一快照
# =============================================================================

class _DepthTrackingRLock:
    """记录当前持锁深度的可重入锁。"""

    def __init__(self) -> None:
        self._rl = threading.RLock()
        self.depth = 0

    def __enter__(self) -> None:
        self._rl.acquire()
        self.depth += 1

    def __exit__(self, *exc: Any) -> bool:
        self.depth -= 1
        self._rl.release()
        return False


class TestAud39317ExportJsonSnapshot:
    def test_summary_computed_while_holding_lock(self) -> None:
        import json

        collector = FrameTraceCollector()
        collector.start_session('ef05')
        collector.record(EditPlaybackFrameTrace(frame_index=1))

        tracking = _DepthTrackingRLock()
        collector._lock = tracking  # type: ignore[assignment]
        original_summary = collector.get_summary
        observed_depths: List[int] = []

        def spy_summary(last_n: Any = None) -> dict:
            observed_depths.append(tracking.depth)
            return original_summary(last_n)

        collector.get_summary = spy_summary  # type: ignore[method-assign]
        payload = json.loads(collector.export_json())

        assert 'summary' in payload and 'traces' in payload
        assert payload['summary']['total_frames'] == 1
        assert observed_depths == [1]
        # 回退（两次独立无锁调用）→ observed_depths == [0] → 失败


# =============================================================================
# AUD-39402: 阻塞操作不持 _session_lock（已被 AUD-39400 修复，回归守卫）
# =============================================================================

class TestAud39402SeekOutsideSessionLock:
    def test_blocking_seek_does_not_hold_session_lock(self) -> None:
        coordinator = PlaybackCoordinator()
        session_lock = coordinator._session_lock
        acquired_inside: List[bool] = []

        def fake_seek(frame: int, blocking: bool = True,
                      churn_generation: bool = True) -> bool:
            # 会话的阻塞 seek 期间协调器锁必须已释放
            acquired_inside.append(session_lock.acquire(blocking=False))
            if acquired_inside[-1]:
                session_lock.release()
            return True

        coordinator._session = SimpleNamespace(  # type: ignore[assignment]
            seek_to_frame=fake_seek,
        )

        assert coordinator.seek_to_frame(7) is True
        # 回退（持锁执行 session.seek_to_frame）→ False → 失败
        assert acquired_inside == [True]

    def test_unload_does_not_hold_session_lock(self) -> None:
        coordinator = PlaybackCoordinator()
        session_lock = coordinator._session_lock
        acquired_inside: List[bool] = []

        def fake_unload(timeout: float = 2.0) -> bool:
            acquired_inside.append(session_lock.acquire(blocking=False))
            if acquired_inside[-1]:
                session_lock.release()
            return True

        coordinator._session = SimpleNamespace(unload=fake_unload)  # type: ignore[assignment]

        assert coordinator.unload(timeout=0.1) is True
        assert acquired_inside == [True]


# =============================================================================
# AUD-39403: 死代码 tick 回调删除 + 回调字段锁纪律
# =============================================================================

class TestAud39403TickCallbackRemoved:
    def test_dead_tick_api_removed(self) -> None:
        coordinator = PlaybackCoordinator()
        # 回退（恢复 set_tick_callback/_tick_callback/_tick_lock）→ 失败
        assert not hasattr(coordinator, 'set_tick_callback')
        assert not hasattr(coordinator, '_tick_callback')
        assert not hasattr(coordinator, '_tick_lock')

    def test_callbacks_cleared_by_cleanup_under_lock(self) -> None:
        coordinator = PlaybackCoordinator()
        spy = SpyLock()
        coordinator._callback_lock = spy  # type: ignore[assignment]

        coordinator.set_on_state_change(lambda state: None)
        coordinator.set_on_frame_ready(lambda frame, idx: None)
        assert len(spy.acquisitions) == 2, "回调写入应持锁"

        coordinator.cleanup()

        assert coordinator._on_state_change is None
        assert coordinator._on_frame_ready is None
        assert len(spy.acquisitions) == 3, "cleanup 清空回调应持锁（一次锁块清空两个回调）"

    def test_state_change_callback_invoked_from_snapshot(self) -> None:
        coordinator = PlaybackCoordinator()
        fired: List[str] = []
        coordinator.set_on_state_change(fired.append)

        class _State:
            name = 'READY'

        coordinator._on_session_state_change(_State())
        assert fired == ['READY']


# =============================================================================
# AUD-39408: _notify_state_change docstring 与锁外调用一致
# =============================================================================

class TestAud39408NotifyDocstring:
    def test_docstring_documents_lockfree_contract(self) -> None:
        doc = VideoSession._notify_state_change.__doc__ or ''
        # 回退（恢复 "Must be called while _state_lock is held"）→ 失败
        assert 'OUTSIDE' in doc
        assert 'while _state_lock is held' not in doc

    def test_notify_works_without_state_lock(self) -> None:
        session = VideoSession(use_shared_frame_provider=False)
        fired = []
        session._on_state_change = fired.append  # type: ignore[assignment]

        session._notify_state_change()

        assert fired == [session._state]


# =============================================================================
# AUD-39412: load() 文档与默认值一致（默认关闭共享帧服务）
# =============================================================================

class TestAud39412SharedProviderDefault:
    def test_load_docstring_documents_default_off(self) -> None:
        doc = VideoSession.load.__doc__ or ''
        # 回退（恢复 "P7.A: 默认使用 ColorFrameProviderAdapter"）→ 失败
        assert 'P7.A: 默认使用 ColorFrameProviderAdapter' not in doc
        assert '默认' in doc and '禁用' in doc

    def test_constructor_fallback_is_false(self) -> None:
        src = inspect.getsource(VideoSession.__init__)
        assert 'value if value is not None else False' in src

    def test_explicit_false_uses_independent_mode(self) -> None:
        session = VideoSession(use_shared_frame_provider=False)
        assert session._use_shared_frame_provider is False


# =============================================================================
# AUD-39513: _get_texture_format 死分支清理
# =============================================================================

class TestAud39513TextureFormat:
    def test_single_return_policy(self) -> None:
        src = inspect.getsource(VideoRenderThread._get_texture_format)
        # 回退（恢复 5 个恒等分支）→ return 数量 > 1 → 失败
        assert src.count('return') == 1
        assert "'f2'" in src
        # 精度决策必须被文档化
        assert 'AUD-39513' in src

    def test_returns_f2_for_all_inputs(self) -> None:
        dummy = SimpleNamespace()
        for dtype in ('uint8', 'uint16', 'float32', 'float16', 'unknown'):
            assert VideoRenderThread._get_texture_format(
                dummy, {'dtype': dtype}) == 'f2'


# =============================================================================
# AUD-39514: 命令合并经 Queue mutex
# =============================================================================

class TestAud39514CommandMergeLock:
    def test_peek_guarded_by_queue_mutex(self) -> None:
        src = inspect.getsource(VideoRenderThread.run)
        # 回退（恢复裸 peek self.command_queue.queue[0]）→ 无 mutex 守卫 → 失败
        assert 'with self.command_queue.mutex:' in src
        idx_with = src.index('with self.command_queue.mutex:')
        idx_peek = src.index('.queue[0]')
        assert idx_with < idx_peek, "队首窥视必须位于 Queue mutex 持锁块内"
        # 消费走公共 API
        assert 'get_nowait()' in src


# =============================================================================
# AUD-39515: 调用方自维护帧号
# =============================================================================

class TestAud39515CallerSideFrameIndex:
    @staticmethod
    def _make_renderer() -> Any:
        """绕开 GL 上下文构造：仅提供 upload_yuv_and_render_frame 所需状态。"""
        renderer = VideoRenderThread.__new__(VideoRenderThread)
        renderer._submit_frame_index_lock = threading.Lock()
        renderer._submitted_frame_index = -1
        renderer.current_frame_index = -1
        renderer._convert_buffer = None
        renderer._buffer_size = (0, 0)
        captured: List[dict] = []

        def fake_submit(cmd: Any, data: Any = None) -> None:
            if isinstance(data, dict):
                captured.append(data)

        renderer.submit = fake_submit  # type: ignore[method-assign]
        return renderer, captured

    def test_auto_frame_index_monotonic_without_reading_render_state(self) -> None:
        renderer, captured = self._make_renderer()

        y_plane = np.zeros((4, 4, 3), dtype=np.float32)
        renderer.upload_yuv_and_render_frame(y_plane, (4, 4))
        renderer.upload_yuv_and_render_frame(y_plane, (4, 4))
        # 显式帧号后继续自动递增
        renderer.upload_yuv_and_render_frame(y_plane, (4, 4), frame_index=7)
        renderer.upload_yuv_and_render_frame(y_plane, (4, 4))

        idxs = [d['frame_index'] for d in captured if 'frame_index' in d]
        # 回退（读渲染线程 current_frame_index=-1）→ [0, 0, 7, 0] → 失败
        assert idxs == [0, 1, 7, 8]
        # 渲染线程状态全程未被读取/写入
        assert renderer.current_frame_index == -1

    def test_upload_method_does_not_read_current_frame_index(self) -> None:
        src = inspect.getsource(VideoRenderThread.upload_yuv_and_render_frame)
        # 回退（恢复 self.current_frame_index + 1）→ 失败
        assert 'self.current_frame_index' not in src


# =============================================================================
# AUD-39610: ProxyPrefetchWorker 锁异常路径必释放
# =============================================================================

class TestAud39610MutexReleasedOnException:
    def _make_worker(self, qapp) -> ProxyPrefetchWorker:
        return ProxyPrefetchWorker(
            source_manager=MagicMock(),
            playback_clock=SimpleNamespace(current_frame=0),
            max_queue_size=8,
            prefetch_lookahead=4,
        )

    def test_add_frame_releases_mutex_when_min_raises(self, qapp) -> None:
        worker = self._make_worker(qapp)
        # 畸形元组让 min(key=x[0]) 在比较时抛 TypeError
        worker._ready_frames = [("a",), (None,)]  # type: ignore[assignment]
        worker._max_queue_size = 1

        with pytest.raises(TypeError):
            worker._add_frame(5, np.zeros((2, 2, 3), np.uint8),
                              worker._get_generation())

        # 回退（无 try/finally）→ 锁仍被持有 → tryLock 返回 False → 失败
        assert worker._ready_frames_mutex.tryLock() is True
        worker._ready_frames_mutex.unlock()

    def test_remove_frame_releases_mutex_when_items_malformed(self, qapp) -> None:
        worker = self._make_worker(qapp)
        worker._ready_frames = [42]  # type: ignore[assignment]  # 解包抛 TypeError

        with pytest.raises(TypeError):
            worker._remove_frame(42)

        assert worker._ready_frames_mutex.tryLock() is True
        worker._ready_frames_mutex.unlock()

    def test_has_frame_early_return_no_deadlock(self, qapp) -> None:
        worker = self._make_worker(qapp)
        gen = worker._get_generation()
        worker._ready_frames = [(5, np.zeros((2, 2, 3), np.uint8), gen)]
        assert worker._has_frame(5) is True
        assert worker._has_frame(99) is False
        # 锁已释放
        assert worker._ready_frames_mutex.tryLock() is True
        worker._ready_frames_mutex.unlock()

    def test_get_queue_depth_releases_mutex(self, qapp) -> None:
        worker = self._make_worker(qapp)
        worker._ready_frames = [(1, 'f', 0)]
        assert worker.get_queue_depth() == 1
        assert worker._ready_frames_mutex.tryLock() is True
        worker._ready_frames_mutex.unlock()


# =============================================================================
# AUD-39715: upload_planes 异常路径纹理登记
# =============================================================================

class _FakeTexture:
    def __init__(self, name: str, fail_write: bool = False) -> None:
        self.name = name
        self.fail_write = fail_write
        self.released = False

    def write(self, data: Any) -> None:
        if self.fail_write:
            raise RuntimeError('GL write failed')

    def release(self) -> None:
        self.released = True


def _make_plane_frame() -> SimpleNamespace:
    return SimpleNamespace(
        is_planar=True,
        pixel_format='yuv420p',
        planes=[
            np.zeros((4, 4), dtype=np.uint8),
            np.zeros((2, 2), dtype=np.uint8),
            np.zeros((2, 2), dtype=np.uint8),
        ],
        format_info=SimpleNamespace(
            bit_depth=8,
            chroma_width_divisor=2,
            chroma_height_divisor=2,
        ),
    )


def _make_plane_uploader(
    fail_on_call: int = 0,
    fail_write_index: int = -1,
) -> tuple:
    uploader = YuvPlaneUploader.__new__(YuvPlaneUploader)
    uploader._program = None
    uploader._yuv_matrix = 'bt709'
    uploader._color_range = 'limited'
    uploader._textures = {}
    created: List[_FakeTexture] = []
    calls = {'n': 0}

    def fake_texture(size: tuple, components: int, dtype: str = 'f1') -> _FakeTexture:
        calls['n'] += 1
        if fail_on_call and calls['n'] == fail_on_call:
            raise RuntimeError('GL texture creation failed')
        tex = _FakeTexture(f'tex{calls["n"]}',
                           fail_write=(calls['n'] == fail_write_index))
        created.append(tex)
        return tex

    uploader._ctx = SimpleNamespace(texture=fake_texture)
    return uploader, created


class TestAud39715RegisterTexturesImmediately:
    def test_creation_failure_keeps_registered_textures(self) -> None:
        uploader, created = _make_plane_uploader(fail_on_call=2)
        with pytest.raises(RuntimeError, match='GL texture creation failed'):
            uploader.upload_planes(_make_plane_frame())
        # 回退（先建后登记）→ 异常时 _textures 为空 → 失败
        assert 'y' in uploader._textures
        assert uploader._textures['y'] is created[0]

    def test_write_failure_keeps_registered_textures(self) -> None:
        uploader, created = _make_plane_uploader(fail_write_index=2)
        with pytest.raises(RuntimeError, match='GL write failed'):
            uploader.upload_planes(_make_plane_frame())
        # Y 已登记；U 已创建并登记（write 阶段失败）
        assert 'y' in uploader._textures
        assert 'u' in uploader._textures
        assert uploader._textures['u'] is created[1]

    def test_success_registers_all_three(self) -> None:
        uploader, created = _make_plane_uploader()
        result = uploader.upload_planes(_make_plane_frame())
        assert len(result) == 3
        assert set(uploader._textures.keys()) == {'y', 'u', 'v'}
        # 全部可经 _release_textures 释放
        uploader._release_textures()
        assert all(t.released for t in created)


# =============================================================================
# AUD-39810: 三处 IBO 登记并随 release() 释放
# =============================================================================

class TestAud39810QuadIboTracked:
    def test_init_sites_store_ibo_members(self) -> None:
        for method in ('_init_quad_buffer', '_init_r16ui_quad',
                       '_create_nv12_r16ui_shader'):
            src = inspect.getsource(getattr(YUVGPUUploader, method))
            # 回退（恢复局部 ibo）→ 失败
            assert 'self._ctx.buffer(indices.tobytes())' in src
            assert re_site_pattern(method) .search(src), (
                f'{method} 应把 IBO 登记为成员'
            )
            assert '\n        ibo = ' not in src

    def test_release_releases_all_three_ibos(self) -> None:
        uploader = YUVGPUUploader.__new__(YUVGPUUploader)
        released: List[str] = []

        class _GL:
            def __init__(self, tag: str) -> None:
                self.tag = tag

            def release(self) -> None:
                released.append(self.tag)

        uploader._pool = SimpleNamespace(
            release_all=lambda: released.append('pool'))
        uploader._r8_pool = None
        uploader._r16ui_pool = None
        uploader._program = None
        uploader._r16ui_program = None
        uploader._r16ui_quad_vao = None
        uploader._r16ui_quad_ibo = _GL('r16ui_quad_ibo')
        uploader._nv12_r16ui_program = None
        uploader._nv12_r16ui_vao = None
        uploader._nv12_r16ui_ibo = _GL('nv12_r16ui_ibo')
        uploader._nv12_program = None
        uploader._nv12_quad_ibo = None
        uploader._nv12_vao_cache = {}
        uploader._quad_buffer = None
        uploader._quad_ibo = _GL('quad_ibo')
        uploader._quad_vao = None
        uploader._staging_buffers = {}
        uploader._staging_rings = {}
        uploader._staging_ring_idx = {}
        uploader._pbo_pool = None
        uploader._pbo_enabled = False
        uploader._pbo_slot_count = 0

        uploader.release()

        # 回退（IBO 不释放）→ 集合缺失 → 失败
        assert {'quad_ibo', 'r16ui_quad_ibo', 'nv12_r16ui_ibo'} <= set(released)
        assert uploader._quad_ibo is None
        assert uploader._r16ui_quad_ibo is None
        assert uploader._nv12_r16ui_ibo is None


def re_site_pattern(method: str) -> Any:
    """各初始化方法中 IBO 成员登记的目标属性名。"""
    import re

    mapping = {
        '_init_quad_buffer': r'self\._quad_ibo\s*=',
        '_init_r16ui_quad': r'self\._r16ui_quad_ibo\s*=',
        '_create_nv12_r16ui_shader': r'self\._nv12_r16ui_ibo\s*=',
    }
    return re.compile(mapping[method])


# =============================================================================
# AUD-39811: enable_pbo 线程契约守卫
# =============================================================================

class TestAud39811EnablePboThreadGuard:
    def test_refused_when_not_on_registered_gl_thread(self, qapp) -> None:
        uploader = YUVGPUUploader.__new__(YUVGPUUploader)
        uploader._ctx = MagicMock()
        released: List[int] = []
        uploader._pbo_pool = SimpleNamespace(release=lambda: released.append(1))
        uploader._pbo_slot_count = 0
        uploader._pbo_enabled = False

        # 注册一个"别的"渲染线程 ident
        set_gpu_frame_release_thread(threading.get_ident() + 1)
        try:
            # 回退（无守卫）→ 会触碰 GL/释放池 → 失败
            uploader.enable_pbo(True)
            uploader._ctx.buffer.assert_not_called()
            assert released == []
            assert uploader._pbo_enabled is False
        finally:
            set_gpu_frame_release_thread(None)

    def test_allowed_when_no_render_thread_registered(self, qapp) -> None:
        uploader = YUVGPUUploader.__new__(YUVGPUUploader)
        uploader._ctx = MagicMock()
        released: List[int] = []
        uploader._pbo_pool = SimpleNamespace(release=lambda: released.append(1))
        uploader._pbo_slot_count = 2
        uploader._pbo_enabled = True

        # 未注册渲染线程（启动配置阶段/单测环境）→ 行为不变
        uploader.enable_pbo(False)

        assert released == [1]
        assert uploader._pbo_pool is None
        assert uploader._pbo_enabled is False
        uploader._ctx.buffer.assert_not_called()

    def test_thread_registry_getter(self) -> None:
        assert gpu_frame_release_thread_id() is None
        set_gpu_frame_release_thread(123456)
        try:
            assert gpu_frame_release_thread_id() == 123456
        finally:
            set_gpu_frame_release_thread(None)
        assert gpu_frame_release_thread_id() is None
