"""
Tests for VideoSession.get_master_frame_blocking() (S0.C)

验收测试：
1. worker 未启动时 get_master_frame_blocking(0) 返回帧
2. worker 运行时请求目标帧并等待成功
3. timeout 时返回 None，不死等

S0.C: 首帧、seek、暂停必须能拿到 master 帧
"""

from __future__ import annotations

import pytest
import threading
import time
import tempfile
import os
from unittest.mock import MagicMock

from src.shared.video.session import VideoSession, PlaybackState
from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource
from src.shared.video.decode_worker import DecodeWorkerManager
from src.shared.video.playback_clock import SourceManager


# =============================================================================
# Mock SourceManager for testing
# =============================================================================

class MockSourceManager:
    """
    Mock SourceManager for unit testing.

    不依赖真实的视频文件，模拟帧解码行为。
    """

    def __init__(self, total_frames: int = 100, decode_delay_ms: float = 10.0) -> None:
        self._total_frames = total_frames
        self._decode_delay_ms = decode_delay_ms
        self._is_open = False
        self._proxy_current_frame = -1
        self._master_current_frame = -1
        self._metadata = None
        self._cache = None
        self._lock = threading.Lock()

    @property
    def total_frames(self) -> int:
        return self._total_frames

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def metadata(self) -> any:
        """Mock metadata"""
        from src.shared.media.metadata import VideoMetadata, Rational

        if self._metadata is None:
            self._metadata = VideoMetadata(
                width=1920,
                height=1080,
                fps=Rational(24, 1),
                total_frames=self._total_frames,
                codec_name='h264',
                codec_profile='High',
                pixel_format='yuv420p',
                bit_depth=8,
            )
        return self._metadata

    @property
    def cache(self) -> any:
        """Mock cache"""
        return self._cache

    def open(self) -> bool:
        self._is_open = True
        return True

    def close(self) -> None:
        self._is_open = False

    def get_proxy_frame(self, frame_index: int):
        """
        Mock proxy frame decoder.
        """
        from src.shared.media.frame import DecodedFrame
        import numpy as np

        # 模拟解码延迟
        time.sleep(self._decode_delay_ms / 1000.0)

        frame = DecodedFrame(
            frame_index=frame_index,
            pts=frame_index,
            time_seconds=frame_index / 24.0,
            data=np.zeros((480, 640, 3), dtype=np.uint8),
            pixel_format='bgr24',
            width=640,
            height=480,
            is_proxy=True,
        )

        return frame

    def get_master_frame(self, frame_index: int):
        """
        Mock master frame decoder.
        """
        from src.shared.media.frame import DecodedFrame
        import numpy as np

        # 模拟解码延迟
        time.sleep(self._decode_delay_ms / 1000.0)

        frame = DecodedFrame(
            frame_index=frame_index,
            pts=frame_index,
            time_seconds=frame_index / 24.0,
            data=np.zeros((1080, 1920, 3), dtype=np.uint8),
            pixel_format='bgr24',
            width=1920,
            height=1080,
            is_proxy=False,
        )

        return frame

    def seek_frame(self, frame_index: int) -> bool:
        """Mock seek"""
        return True


# =============================================================================
# Mock FrameQueue for testing
# =============================================================================

class MockFrameQueue:
    """
    Mock ReadyFrameQueue for testing.
    """

    def __init__(self) -> None:
        self._master_queue: list[QueueEntry] = []
        self._proxy_queue: list[QueueEntry] = []
        self._current_generation: int = 0
        self._lock = threading.Lock()

    def enqueue(self, entry: QueueEntry) -> bool:
        """入队"""
        with self._lock:
            self._master_queue.append(entry)
        return True

    def dequeue(self, is_master: bool = False, frame_index: int = None) -> QueueEntry | None:
        """出队"""
        with self._lock:
            queue = self._master_queue if is_master else self._proxy_queue
            if not queue:
                return None

            if frame_index is not None:
                for i, entry in enumerate(queue):
                    if entry.frame_index == frame_index:
                        return queue.pop(i)
                return None

            return queue.pop(0) if queue else None

    def peek(self, is_master: bool = False, frame_index: int = None) -> QueueEntry | None:
        """查看"""
        with self._lock:
            queue = self._master_queue if is_master else self._proxy_queue
            if not queue:
                return None

            if frame_index is not None:
                for entry in queue:
                    if entry.frame_index == frame_index:
                        return entry
                return None

            return queue[0] if queue else None

    def get_latest(self, is_master: bool = False) -> QueueEntry | None:
        """获取最新"""
        with self._lock:
            queue = self._master_queue if is_master else self._proxy_queue
            return queue[-1] if queue else None

    def increment_generation(self) -> int:
        """递增 generation"""
        with self._lock:
            self._current_generation += 1
        return self._current_generation

    def clear_all(self) -> tuple[int, int]:
        """清空"""
        with self._lock:
            master_count = len(self._master_queue)
            proxy_count = len(self._proxy_queue)
            self._master_queue.clear()
            self._proxy_queue.clear()
            return (master_count, proxy_count)

    @property
    def stats(self) -> dict:
        return {}


class TestPlaybackProxyRequests:
    """播放 tick 应该请求当前帧，而不只预读未来帧。"""

    def test_start_playback_requests_current_proxy_frame(self):
        from src.shared.video.frame_request import FrameRequestReason

        session = VideoSession()
        session._state = PlaybackState.READY
        session._current_frame = 7
        session._playback_clock = MagicMock()
        session._frame_queue = MagicMock()
        session._frame_queue.contains.return_value = False
        session._worker_manager = MagicMock()

        assert session.playback_clock is session._playback_clock
        assert session.start_playback() is True

        session._worker_manager.request_proxy_frame.assert_called_with(
            frame_index=7,
            reason=FrameRequestReason.PLAYBACK,
        )
        session._worker_manager.update_clock_position.assert_called_once_with(7)

    def test_paused_master_request_starts_only_master_worker(self):
        """Paused Color preview must not start proxy prefetch just for one frame."""
        session = VideoSession()
        session._state = PlaybackState.PAUSED
        session._worker_manager = MagicMock()

        session.request_master_frame(12)

        session._worker_manager.start_master_worker.assert_called_once_with()
        session._worker_manager.request_master_frame.assert_called_once()

    def test_tick_requests_exact_frame_before_dequeue(self):
        from src.shared.video.frame_request import FrameRequestReason
        from src.shared.video.frame_queue import LatestFrameResult

        session = VideoSession()
        session._current_frame = 0
        session._playback_clock = MagicMock()
        session._playback_clock.tick.return_value = 12
        session._frame_queue = MagicMock()
        session._frame_queue.contains.return_value = False
        session._frame_queue.dequeue.return_value = None
        session._frame_queue.get_latest.return_value = None
        # 返回 LatestFrameResult(found=False)，表示没有可用帧
        session._frame_queue.dequeue_latest_before_or_at.return_value = LatestFrameResult(found=False)
        session._worker_manager = MagicMock()

        assert session.tick() is None

        session._worker_manager.request_proxy_frame.assert_called_with(
            frame_index=12,
            reason=FrameRequestReason.PLAYBACK,
        )
        session._worker_manager.update_clock_position.assert_called_once_with(12)


class TestPlaybackMonotonicDisplay:
    """播放显示帧号只能前进，不能被晚到旧帧拉回去。"""

    def test_tick_holds_without_re_request_when_clock_has_not_advanced(self):
        """A faster UI timer must not repeatedly decode the displayed frame."""
        session = VideoSession()
        session._playback_clock = MagicMock()
        session._playback_clock.tick.return_value = 12
        session._frame_queue = MagicMock()
        session._worker_manager = MagicMock()
        session._last_displayed_proxy_frame_index = 12

        assert session.tick() is None

        session._frame_queue.dequeue.assert_not_called()
        session._frame_queue.dequeue_latest_before_or_at.assert_not_called()
        session._worker_manager.request_proxy_frame.assert_not_called()
        session._worker_manager.update_clock_position.assert_not_called()

    def test_tick_drops_late_fallback_older_than_last_displayed(self):
        from src.shared.media.frame import DecodedFrame

        class FakeClock:
            def tick(self):
                return 12

        class FakeWorker:
            def update_clock_position(self, frame):
                pass

            def request_proxy_frame(self, frame_index, reason):
                pass

        current_frame = DecodedFrame(
            frame_index=10,
            pts=10,
            time_seconds=10 / 24.0,
            data=None,
            pixel_format="bgr24",
            width=640,
            height=480,
            is_proxy=True,
        )
        late_frame = DecodedFrame(
            frame_index=8,
            pts=8,
            time_seconds=8 / 24.0,
            data=None,
            pixel_format="bgr24",
            width=640,
            height=480,
            is_proxy=True,
        )

        session = VideoSession()
        session._playback_clock = FakeClock()
        session._frame_queue = ReadyFrameQueue(max_proxy_size=4, max_master_size=1)
        session._worker_manager = FakeWorker()
        session._current_proxy_frame = current_frame
        session._last_displayed_proxy_frame_index = 10

        session._frame_queue.enqueue(
            QueueEntry(
                frame=late_frame,
                decode_ms=1.0,
                source=FrameSource.PROXY,
                can_drop=True,
                generation_id=0,
            )
        )

        result = session.tick()

        assert result is current_frame
        assert session._last_displayed_proxy_frame_index == 10


class TestClipSwitchFlashGuard:
    """切换片段后按播放不应闪现上一个片段的最后一帧。

    根因：seek_to_frame() 设置了 _current_master_frame（用于静态预览），
    但没有重置 _current_proxy_frame。当播放开始后，proxy 队列暂时为空
    （ProRes/DNxHR 首帧解码慢），tick() 的 hold-last-frame fallback
    （session.py:939 附近）返回了过期的 _current_proxy_frame（旧片段
    的尾帧），导致画布和外监都闪一帧旧片段画面。
    """

    def test_seek_clears_stale_proxy_hold_frame(self):
        """seek_to_frame 必须清除旧片段的 proxy hold frame。"""
        from src.shared.media.frame import DecodedFrame

        old_clip_frame = DecodedFrame(
            frame_index=200,
            pts=200,
            time_seconds=200 / 24.0,
            data=None,
            pixel_format="bgr24",
            width=640,
            height=480,
            is_proxy=True,
        )

        session = VideoSession()
        session._source_manager = MagicMock()
        session._frame_queue = MagicMock()
        session._frame_queue.increment_generation.return_value = 1
        session._playback_clock = MagicMock()
        session._worker_manager = MagicMock()
        # 模拟旧片段播放结束时 _current_proxy_frame 指向旧片段的尾帧
        session._current_proxy_frame = old_clip_frame
        session._current_frame = 200
        session._state = PlaybackState.PAUSED

        # seek 到新片段的首帧 300
        session.seek_to_frame(300, blocking=False)

        # seek 后 _current_proxy_frame 必须被清除，
        # 否则 tick() 的 hold-last-frame fallback 会返回旧片段的帧
        assert session._current_proxy_frame is None

    def test_tick_returns_none_when_hold_frame_is_stale(self):
        """即使 _current_proxy_frame 过期残留，tick 也不应返回它。

        Defensive guard：_current_proxy_frame 的帧号落后于
        _last_displayed_proxy_frame_index 时，返回 None 而不是旧帧。
        """
        from src.shared.media.frame import DecodedFrame
        from src.shared.video.frame_queue import LatestFrameResult

        stale_hold = DecodedFrame(
            frame_index=200,  # 旧片段尾帧
            pts=200,
            time_seconds=200 / 24.0,
            data=None,
            pixel_format="bgr24",
            width=640,
            height=480,
            is_proxy=True,
        )

        session = VideoSession()
        session._playback_clock = MagicMock()
        session._playback_clock.tick.return_value = 301  # 新片段的下一帧
        session._frame_queue = MagicMock()
        session._frame_queue.dequeue.return_value = None  # 队列为空（解码中）
        session._frame_queue.dequeue_latest_before_or_at.return_value = LatestFrameResult(found=False)
        session._worker_manager = MagicMock()
        # 模拟防御场景：hold frame 残留为旧片段的帧 200
        session._current_proxy_frame = stale_hold
        session._last_displayed_proxy_frame_index = 300  # seek 到了 300

        result = session.tick()

        # 不应返回旧片段的帧，而应返回 None 让上层 hold 住画布
        assert result is None

    def test_tick_returns_none_after_seek_when_queue_empty(self):
        """seek 后第一个 tick（队列暂空）不应返回旧片段的帧。"""
        from src.shared.media.frame import DecodedFrame
        from src.shared.video.frame_queue import LatestFrameResult

        old_clip_frame = DecodedFrame(
            frame_index=200,
            pts=200,
            time_seconds=200 / 24.0,
            data=None,
            pixel_format="bgr24",
            width=640,
            height=480,
            is_proxy=True,
        )

        session = VideoSession()
        session._source_manager = MagicMock()
        session._frame_queue = MagicMock()
        session._frame_queue.increment_generation.return_value = 1
        session._frame_queue.dequeue.return_value = None
        session._frame_queue.dequeue_latest_before_or_at.return_value = LatestFrameResult(found=False)
        session._playback_clock = MagicMock()
        session._playback_clock.tick.return_value = 301
        session._worker_manager = MagicMock()
        session._current_proxy_frame = old_clip_frame
        session._current_frame = 200
        session._state = PlaybackState.PAUSED

        # seek 到新片段的首帧 300（清除 hold frame）
        session.seek_to_frame(300, blocking=False)

        # 播放开始后第一个有效 tick
        result = session.tick()

        # 不应闪现旧片段的帧
        assert result is None


class TestPlaybackClockResumePosition:
    """播放时钟启动时应从当前帧继续，而不是重置到 0。"""

    def test_start_preserves_seeked_frame_position(self):
        from src.shared.video.playback_clock import PlaybackClock

        clock = PlaybackClock(fps=24.0, total_frames=100)
        clock.seek_to_frame(48)
        clock.start()

        assert clock.current_frame == 48
        assert clock._state.clock_time == pytest.approx(2.0)


class TestPlaybackClockUnknownDuration:
    """V-P0-09: 未知 total_frames 不应被当作有限上界。

    此前 total_frames=0 时，seek/tick 的 clamp 变成 min(frame, -1) → max(0,-1)=0，
    任何 seek 都回到第 0 帧、播放立即停止。迁移后未知时长（0/None）只做下界
    保护，结束由明确 EOS 驱动。
    """

    def test_seek_beyond_known_end_is_clamped(self):
        from src.shared.video.playback_clock import PlaybackClock

        clock = PlaybackClock(fps=24.0, total_frames=100)
        clock.seek_to_frame(999)
        assert clock.current_frame == 99  # clamp 到最后一帧

    def test_seek_with_unknown_duration_not_clamped_to_zero(self):
        from src.shared.video.playback_clock import PlaybackClock

        # 未知时长：total_frames=0
        clock = PlaybackClock(fps=24.0, total_frames=0)
        clock.seek_to_frame(50)
        # 关键：不被 clamp 到 0
        assert clock.current_frame == 50

    def test_tick_advances_with_unknown_duration(self):
        from src.shared.video.playback_clock import PlaybackClock

        # 未知时长：total_frames=0。此前 ideal_frame 被 clamp 到 -1，lag<=0，
        # tick 永远停在 current_frame=0 不前进。
        clock = PlaybackClock(fps=24.0, total_frames=0)
        clock.start()
        frames = [clock.tick(dt=1.0 / 24.0) for _ in range(10)]
        # 关键：帧必须单调前进，不能卡在 0（V-P0-09 回归会全为 0）。
        assert frames[-1] > frames[0], f"未知时长下 tick 未推进：{frames}"
        assert frames[-1] >= 5

    def test_get_frame_for_time_with_unknown_duration(self):
        from src.shared.video.playback_clock import PlaybackClock

        clock = PlaybackClock(fps=24.0, total_frames=0)
        # 2.5 秒 @ 24fps = 第 60 帧；未知时长不应 clamp 回 0/-1。
        assert clock.get_frame_for_time(2.5) == 60


class TestPlaybackClockRateAnchor:
    """V-P0-02: 改速不应让目标帧瞬间跳变。

    此前 tick 用 ideal_frame = clock_time * fps * speed，把新 speed 乘到"以旧
    speed 累计的 clock_time"上，中段改速会瞬间跳到错误帧。锚点公式后，改速只
    影响"此后"的推进速度，当前位置不跳变。
    """

    def test_speed_change_does_not_jump_position(self):
        """改速后短窗口内推进量应≈ new_speed * elapsed，而非被旧累计时间放大。

        旧公式 ideal = clock_time * fps * speed 把 2x 乘到"以 1x 累计的 1 秒"
        上，0.5s 后会跑到 base+~48 而不是 base+24。锚点公式后位置由"改速那一刻
        起"重新计时。
        """
        from src.shared.video.playback_clock import PlaybackClock

        clock = PlaybackClock(fps=24.0, total_frames=1000)
        clock.start()
        # 以 1x 推进 1 秒（24 帧）。
        for _ in range(24):
            clock.tick(dt=1.0 / 24.0)
        base = clock.current_frame
        assert base >= 20  # 已到约第 24 帧

        clock.set_speed(2.0)
        # 2x 下再推进 0.5 秒（12 tick）：应前进约 24 帧（2 * 0.5s * 24fps）。
        for _ in range(12):
            clock.tick(dt=1.0 / 24.0)
        advanced = clock.current_frame - base
        # 旧公式会把 advance 放大到 ~48（base=23 -> 71）。锚点公式下应 ~24。
        assert advanced <= 32, (
            f"改速后推进被旧累计时间放大：base={base} advanced={advanced}"
            f"（V-P0-02 回归，应≈24）"
        )
        assert advanced >= 18, f"改速后推进不足：advanced={advanced}"

    def test_speed_change_then_advance_uses_new_rate(self):
        """改速后，后续推进按新速度进行（锚点公式仍尊重 speed）。"""
        from src.shared.video.playback_clock import PlaybackClock

        clock = PlaybackClock(fps=24.0, total_frames=1000)
        clock.start()
        for _ in range(24):
            clock.tick(dt=1.0 / 24.0)
        base = clock.current_frame

        clock.set_speed(2.0)
        # 推进 1 秒（24 个 1/24 tick），2x 速度应前进约 48 帧。
        for _ in range(24):
            clock.tick(dt=1.0 / 24.0)
        advanced = clock.current_frame - base
        assert advanced >= 40, f"2x 速度下 1 秒应前进约 48 帧，实际 {advanced}"

    def test_speed_change_at_frame_zero_does_not_jump(self):
        """起点 0 改速不应异常跳变。"""
        from src.shared.video.playback_clock import PlaybackClock

        clock = PlaybackClock(fps=24.0, total_frames=1000)
        clock.start()
        clock.set_speed(4.0)
        # 改速后第一个 tick：从 0 出发，不应跳到很大的帧。
        f = clock.tick(dt=1.0 / 24.0)
        assert f <= 5, f"起点改速后首 tick 异常跳变到 {f}"


# =============================================================================
# Test: worker 未启动时
# =============================================================================

class TestGetMasterFrameBlockingWorkerNotStarted:
    """测试 worker 未启动时 get_master_frame_blocking()"""

    def test_returns_frame_from_source_manager_directly(self):
        """
        S0.C 测试：worker 未启动时，get_master_frame_blocking(0) 应该
        直接从 SourceManager.get_master_frame(0) 返回帧。

        这是首帧显示场景：load() 创建 worker 但不启动。
        """
        # 创建 mock source manager
        source_manager = MockSourceManager(total_frames=100, decode_delay_ms=5.0)
        source_manager.open()

        # 创建 mock frame queue
        frame_queue = MockFrameQueue()

        # 创建 VideoSession（不调用 load()，手动设置内部组件）
        session = VideoSession()
        session._source_manager = source_manager
        session._frame_queue = frame_queue
        session._current_frame = 0
        session._worker_manager = None  # worker 未创建

        # 调用 get_master_frame_blocking(0)
        # 因为 worker_manager = None，应该直接从 SourceManager 获取
        frame = session.get_master_frame_blocking(0, timeout_ms=100)

        # 验证：应该返回帧
        assert frame is not None
        assert frame.frame_index == 0
        assert frame.width == 1920
        assert frame.height == 1080
        assert not frame.is_proxy  # master frame

        # 验证：_current_master_frame 应该被更新
        assert session._current_master_frame is not None
        assert session._current_master_frame.frame_index == 0

        # 清理
        source_manager.close()

    def test_returns_cached_frame_if_available(self):
        """
        S0.C 测试：如果 _current_master_frame 已经缓存了目标帧，
        应该直接返回缓存帧，不重新解码。
        """
        from src.shared.media.frame import DecodedFrame
        import numpy as np

        # 创建 mock source manager
        source_manager = MockSourceManager(total_frames=100, decode_delay_ms=50.0)
        source_manager.open()

        # 创建 mock frame queue
        frame_queue = MockFrameQueue()

        # 创建 VideoSession
        session = VideoSession()
        session._source_manager = source_manager
        session._frame_queue = frame_queue
        session._current_frame = 0

        # 设置缓存帧（模拟已经获取过帧 0）
        cached_frame = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            data=np.zeros((1080, 1920, 3), dtype=np.uint8),
            pixel_format='bgr24',
            width=1920,
            height=1080,
            is_proxy=False,
        )
        session._current_master_frame = cached_frame

        # 调用 get_master_frame_blocking(0)
        # 应该返回缓存帧，不触发解码（decode_delay_ms=50.0 会导致明显延迟）
        start_time = time.perf_counter()
        frame = session.get_master_frame_blocking(0, timeout_ms=100)
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        # 验证：应该快速返回（< 5ms，因为没有解码）
        assert frame is not None
        assert frame.frame_index == 0
        assert elapsed_ms < 10.0  # 应该在 10ms 内返回

        # 清理
        source_manager.close()


# =============================================================================
# Test: worker 运行时
# =============================================================================

class TestGetMasterFrameBlockingWorkerRunning:
    """测试 worker 运行时 get_master_frame_blocking()"""

    def test_requests_frame_and_waits_success(self):
        """
        S0.C 测试：worker 运行时，请求目标帧后等待成功。

        这是暂停/seek 场景：worker 正在运行，需要等待帧就绪。
        """
        from src.shared.media.frame import DecodedFrame
        from src.shared.video.decode_worker import MasterDecodeWorker
        import numpy as np

        # 创建 mock source manager（解码延迟 20ms）
        source_manager = MockSourceManager(total_frames=100, decode_delay_ms=20.0)
        source_manager.open()

        # 创建真实的 frame queue
        frame_queue = ReadyFrameQueue(max_master_size=3)

        # 创建 worker manager 和 master worker
        worker_manager = DecodeWorkerManager(
            source_manager=source_manager,
            frame_queue=frame_queue,
            prefetch_lookahead=4,
        )
        worker_manager.create_workers()

        # 启动 master worker
        master_worker = worker_manager.master_worker
        master_worker.start()

        # 等待 worker 启动
        time.sleep(0.1)

        # 创建 VideoSession
        session = VideoSession()
        session._source_manager = source_manager
        session._frame_queue = frame_queue
        session._worker_manager = worker_manager
        session._current_frame = 0

        # 调用 get_master_frame_blocking(10)
        # Worker 运行中，应该发请求并等待
        frame = session.get_master_frame_blocking(10, timeout_ms=500)

        # 验证：应该返回帧
        assert frame is not None
        assert frame.frame_index == 10
        assert not frame.is_proxy

        # 清理：停止 worker
        worker_manager.stop_workers(timeout=2.0)
        source_manager.close()

    def test_uses_queue_frame_if_already_available(self):
        """
        S0.C 测试：如果队列中已有目标帧，应该立即返回，不等待。
        """
        from src.shared.media.frame import DecodedFrame
        import numpy as np

        # 创建 mock source manager
        source_manager = MockSourceManager(total_frames=100, decode_delay_ms=10.0)
        source_manager.open()

        # 创建真实的 frame queue
        frame_queue = ReadyFrameQueue(max_master_size=3)

        # 手动将帧 5 入队
        mock_frame = DecodedFrame(
            frame_index=5,
            pts=5,
            time_seconds=5 / 24.0,
            data=np.zeros((1080, 1920, 3), dtype=np.uint8),
            pixel_format='bgr24',
            width=1920,
            height=1080,
            is_proxy=False,
        )

        entry = QueueEntry(
            frame=mock_frame,
            decode_ms=10.0,
            source=FrameSource.MASTER,
            can_drop=False,
            generation_id=0,
        )
        frame_queue.enqueue(entry)

        # 创建 worker manager（但不启动）
        worker_manager = DecodeWorkerManager(
            source_manager=source_manager,
            frame_queue=frame_queue,
            prefetch_lookahead=4,
        )
        worker_manager.create_workers()

        # 创建 VideoSession
        session = VideoSession()
        session._source_manager = source_manager
        session._frame_queue = frame_queue
        session._worker_manager = worker_manager
        session._current_frame = 5

        # 调用 get_master_frame_blocking(5)
        # 队列中已有帧 5，应该立即返回
        start_time = time.perf_counter()
        frame = session.get_master_frame_blocking(5, timeout_ms=100)
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        # 验证：应该立即返回（< 5ms）
        assert frame is not None
        assert frame.frame_index == 5
        assert elapsed_ms < 5.0

        # 清理
        source_manager.close()


# =============================================================================
# Test: timeout
# =============================================================================

class TestGetMasterFrameBlockingTimeout:
    """测试 timeout 时返回 None"""

    def test_returns_frame_or_none_on_timeout_without_hanging(self):
        """
        S0.C 测试：timeout 时返回帧或 None，不死等。

        这是极端场景：worker 无法及时解码帧。

        Note:
            Worker 未启动时，会 fallback 到 SourceManager.get_master_frame()。
            所以实际返回帧的可能性很大，主要验证不会死等。
        """
        # 创建 mock source manager（解码延迟中等）
        source_manager = MockSourceManager(total_frames=100, decode_delay_ms=15.0)
        source_manager.open()

        # 创建 mock frame queue（不自动入队）
        frame_queue = MockFrameQueue()

        # 创建 worker manager（但不启动）
        worker_manager = DecodeWorkerManager(
            source_manager=source_manager,
            frame_queue=frame_queue,
            prefetch_lookahead=4,
        )
        worker_manager.create_workers()

        # 创建 VideoSession
        session = VideoSession()
        session._source_manager = source_manager
        session._frame_queue = frame_queue
        session._worker_manager = worker_manager
        session._current_frame = 0

        # 调用 get_master_frame_blocking(0, timeout_ms=50)
        # Worker 未启动，应该 fallback 到 SourceManager
        # SourceManager 的 decode_delay_ms=15ms，应该能完成
        start_time = time.perf_counter()
        frame = session.get_master_frame_blocking(0, timeout_ms=50)
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        # 验证：不应该死等，应该在合理时间内返回
        # 因为 fallback 到 SourceManager，解码延迟 15ms，总时间应该在 20-30ms
        assert elapsed_ms < 50.0  # 应该在 50ms 内返回

        # 验证：可能返回帧（fallback 成功）或 None（fallback 失败）
        # 主要验证不会死等超过 timeout
        if frame is not None:
            assert frame.frame_index == 0

        # 清理
        source_manager.close()


# =============================================================================
# Test: seek_to_frame
# =============================================================================

class TestSeekToFrameUsesBlocking:
    """测试 seek_to_frame() 使用 blocking 方法"""

    def test_seek_updates_current_master_frame(self):
        """
        S0.C 测试：seek_to_frame() 应该使用 blocking 方法获取目标帧，
        并更新 _current_master_frame。
        """
        # 创建 mock source manager
        source_manager = MockSourceManager(total_frames=100, decode_delay_ms=10.0)
        source_manager.open()

        # 创建真实的 frame queue
        frame_queue = ReadyFrameQueue(max_master_size=3, max_proxy_size=12)

        # 创建播放时钟
        from src.shared.video.playback_clock import PlaybackClock
        playback_clock = PlaybackClock(fps=24.0, total_frames=100)

        # 创建 VideoSession
        session = VideoSession()
        session._source_manager = source_manager
        session._frame_queue = frame_queue
        session._playback_clock = playback_clock
        session._metadata = source_manager.metadata
        session._current_frame = 0

        # 设置状态为 READY
        session._state = PlaybackState.READY

        # 调用 seek_to_frame(50)
        result = session.seek_to_frame(50)

        # 验证：应该成功
        assert result is True
        assert session._current_frame == 50

        # 验证：_current_master_frame 应该被更新
        assert session._current_master_frame is not None
        assert session._current_master_frame.frame_index == 50
        assert not session._current_master_frame.is_proxy

        # 清理
        source_manager.close()


# =============================================================================
# Test: get_current_frame
# =============================================================================

class TestGetCurrentFrameUsesBlocking:
    """测试 get_current_frame(is_master=True) 使用 blocking 方法"""

    def test_get_current_frame_master_returns_blocking_result(self):
        """
        S0.C 测试：get_current_frame(is_master=True) 应该
        调用 get_master_frame_blocking() 返回高质量帧。
        """
        # 创建 mock source manager
        source_manager = MockSourceManager(total_frames=100, decode_delay_ms=10.0)
        source_manager.open()

        # 创建 mock frame queue
        frame_queue = MockFrameQueue()

        # 创建 VideoSession
        session = VideoSession()
        session._source_manager = source_manager
        session._frame_queue = frame_queue
        session._current_frame = 20
        session._worker_manager = None

        # 调用 get_current_frame(is_master=True)
        frame = session.get_current_frame(is_master=True)

        # 验证：应该返回帧 20
        assert frame is not None
        assert frame.frame_index == 20
        assert not frame.is_proxy

        # 清理
        source_manager.close()


# =============================================================================
# Integration Test: Full workflow
# =============================================================================

class TestIntegrationMasterFrameWorkflow:
    """集成测试：完整的首帧/seek/pause 流程"""

    def test_first_frame_via_coordinator_integration(self):
        """
        S0.C 集成测试：通过 PlaybackCoordinator 获取首帧。

        模拟 _show_first_frame_via_coordinator() 调用流程。
        """
        from src.shared.video.playback_state import PlaybackCoordinator

        # 创建 mock source manager
        source_manager = MockSourceManager(total_frames=100, decode_delay_ms=10.0)
        source_manager.open()

        # 创建 PlaybackCoordinator
        coordinator = PlaybackCoordinator()

        # 手动创建 VideoSession（避免调用 load()）
        session = VideoSession()
        session._source_manager = source_manager
        session._frame_queue = ReadyFrameQueue()
        session._metadata = source_manager.metadata
        session._current_frame = 0
        session._state = PlaybackState.READY

        coordinator._session = session

        # 模拟 _show_first_frame_via_coordinator() 调用
        frame = coordinator.get_current_frame_data(is_master=True)

        # 验证：应该返回帧 0
        assert frame is not None
        assert frame.frame_index == 0
        assert not frame.is_proxy

        # 清理
        source_manager.close()


class TestStateChangeCallbackNotUnderLock:
    """V-P0-11: 状态变更回调必须在 _state_lock 外触发。

    此前 _notify_state_change() 在持有 _state_lock 时调用外部 callback；
    若 callback 重入读取 session.state（需 _state_lock），普通 Lock 不可重入，
    会死锁。迁移后 callback 在锁外触发。
    """

    def test_state_change_callback_can_reenter_state_lock(self):
        """回调内读取 session.state（需 _state_lock）不应死锁。"""
        session = VideoSession()
        seen = []

        def reentrant_callback(state):
            # 故意重入需要 _state_lock 的属性——迁移前这里会死锁。
            seen.append((state, session.state))

        session.set_on_state_change(reentrant_callback)

        # 把状态置为 PLAYING，再触发通知（模拟 pause/stop 内部的状态迁移）。
        with session._state_lock:
            session._state = PlaybackState.PLAYING

        # 在子线程里跑通知，加超时保护：死锁会让 join 超时。
        done = threading.Event()

        def run():
            session._notify_state_change()
            done.set()

        t = threading.Thread(target=run)
        t.start()
        assert done.wait(timeout=3.0), (
            "状态变更回调在锁内重入 _state_lock 导致死锁（V-P0-11 回归）"
        )
        t.join(timeout=2.0)
        assert not t.is_alive()

        assert len(seen) == 1
        notified_state, observed_state = seen[0]
        assert notified_state is PlaybackState.PLAYING
        assert observed_state is PlaybackState.PLAYING


class TestStopPlaybackKeepsWorkersAlive:
    """V-P0-06: stop_playback 不应销毁工作线程，只有 unload 才销毁。

    此前 stop_playback 每次 stop_workers()，pause/seek/scrub 高频调用会反复
    销毁重建线程、丢失热解码器缓存，造成恢复延迟与线程抖动。迁移后
    stop_playback 只 pause_workers（清空待处理请求、保留线程），unload 仍
    stop_workers 彻底销毁。
    """

    def _make_playing_session(self):
        session = VideoSession()
        session._state = PlaybackState.PLAYING
        session._playback_clock = MagicMock()
        session._worker_manager = MagicMock()
        return session

    def test_stop_playback_pauses_not_stops_workers(self):
        session = self._make_playing_session()

        assert session.stop_playback() is True

        session._worker_manager.pause_workers.assert_called_once()
        # 关键：不应销毁工作线程
        session._worker_manager.stop_workers.assert_not_called()
        assert session._state is PlaybackState.PAUSED

    def test_unload_still_stops_workers(self):
        session = self._make_playing_session()
        # unload() 的 _cleanup() 会把 _worker_manager 置 None，先保存引用。
        worker_manager = session._worker_manager

        assert session.unload() is True

        # unload 仍彻底销毁线程
        worker_manager.stop_workers.assert_called_once()


class TestPauseWorkersClearsRequests:
    """V-P0-06: DecodeWorkerManager.pause_workers 清空待处理请求但保留线程。"""

    def test_pause_workers_clears_requests_without_stopping(self):
        from src.shared.video.decode_worker import DecodeWorkerManager

        manager = DecodeWorkerManager.__new__(DecodeWorkerManager)
        proxy = MagicMock()
        master = MagicMock()
        manager._proxy_worker = proxy
        manager._master_worker = master

        manager.pause_workers()

        proxy.clear_requests.assert_called_once()
        master.clear_requests.assert_called_once()
        # 不应停止线程
        proxy.stop.assert_not_called()
        master.stop.assert_not_called()


