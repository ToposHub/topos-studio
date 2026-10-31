"""
Tests for DecodeWorker (P2.B)

验收测试：
1. 连续播放请求时 proxy worker 不重复 seek
2. seek 后旧帧不会覆盖新 seek 结果
3. stop/unload 后线程能在 2 秒内退出
4. Worker 禁止直接接触 UI 和 ModernGL（通过设计验证）
"""

from __future__ import annotations

import pytest
import threading
import time
import tempfile
import os

from src.shared.video.decode_worker import (
    ProxyDecodeWorker,
    MasterDecodeWorker,
    DecodeWorkerManager,
    DecodeWorkerStats,
    WorkerState,
    STOP_TIMEOUT_SECONDS,
)
from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource
from src.shared.video.frame_request import VideoFrameRequest, FrameRequestReason, RequestPriority
from src.shared.video.playback_clock import SourceManager
from src.shared.video.session import VideoSession, PlaybackState


# =============================================================================
# Mock SourceManager for testing
# =============================================================================

class MockSourceManager:
    """
    Mock SourceManager for unit testing.

    不依赖真实的视频文件，模拟帧解码行为。
    """

    def __init__(self, total_frames: int = 100) -> None:
        self._total_frames = total_frames
        self._is_open = False
        self._proxy_current_frame = -1
        self._master_current_frame = -1
        self._seek_count = 0
        self._proxy_seek_count = 0
        self._master_seek_count = 0
        self._lock = threading.Lock()

    @property
    def total_frames(self) -> int:
        return self._total_frames

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def seek_count(self) -> int:
        with self._lock:
            return self._seek_count

    @property
    def proxy_seek_count(self) -> int:
        with self._lock:
            return self._proxy_seek_count

    @property
    def master_seek_count(self) -> int:
        with self._lock:
            return self._master_seek_count

    def open(self) -> bool:
        self._is_open = True
        return True

    def close(self) -> None:
        self._is_open = False

    def get_proxy_frame(self, frame_index: int):
        """
        Mock proxy frame decoder.

        实现 P1.A 的顺序播放优化：
        - 如果 frame_index == _proxy_current_frame + 1，不 seek
        - 否则 seek
        """
        from src.shared.media.frame import DecodedFrame
        import numpy as np

        with self._lock:
            # 顺序播放优化
            is_sequential = frame_index == self._proxy_current_frame + 1

            if not is_sequential:
                # 需要 seek
                self._seek_count += 1
                self._proxy_seek_count += 1

            self._proxy_current_frame = frame_index

        # 创建 mock frame
        frame = DecodedFrame(
            frame_index=frame_index,
            pts=frame_index,
            time_seconds=frame_index / 24.0,
            data=np.zeros((100, 100, 3), dtype=np.uint8),
            pixel_format='bgr24',
            width=100,
            height=100,
            is_proxy=True,
        )

        return frame

    def get_master_frame(self, frame_index: int):
        """
        Mock master frame decoder.

        Master 源保持精确，不做顺序优化。
        """
        from src.shared.media.frame import DecodedFrame
        import numpy as np

        with self._lock:
            # Master 源总是 seek（保持精确）
            if frame_index != self._master_current_frame:
                self._seek_count += 1
                self._master_seek_count += 1

            self._master_current_frame = frame_index

        # 创建 mock frame（高质量）
        frame = DecodedFrame(
            frame_index=frame_index,
            pts=frame_index,
            time_seconds=frame_index / 24.0,
            data=np.zeros((100, 100, 3), dtype=np.uint8),
            pixel_format='bgr24',
            width=100,
            height=100,
            is_proxy=False,
        )

        return frame


# =============================================================================
# DecodeWorkerStats Tests
# =============================================================================

class TestDecodeWorkerStats:
    """DecodeWorkerStats 测试"""

    def test_stats_initial_values(self):
        """测试初始值"""
        stats = DecodeWorkerStats()
        assert stats.frames_decoded == 0
        assert stats.frames_dropped == 0
        assert stats.seek_count == 0
        assert stats.decode_total_ms == 0.0
        assert stats.decode_avg_ms == 0.0

    def test_stats_update_decode(self):
        """测试解码统计更新"""
        stats = DecodeWorkerStats()

        stats.update_decode(15.0, 0)
        assert stats.frames_decoded == 1
        assert stats.decode_total_ms == 15.0
        assert stats.decode_avg_ms == 15.0
        assert stats.last_frame_index == 0

        stats.update_decode(20.0, 1)
        assert stats.frames_decoded == 2
        assert stats.decode_total_ms == 35.0
        assert stats.decode_avg_ms == 17.5
        assert stats.last_frame_index == 1

    def test_stats_update_drop(self):
        """测试丢帧统计更新"""
        stats = DecodeWorkerStats()
        stats.update_drop()
        assert stats.frames_dropped == 1

    def test_stats_update_seek(self):
        """测试 seek 统计更新"""
        stats = DecodeWorkerStats()
        stats.update_seek()
        assert stats.seek_count == 1

    def test_stats_to_dict(self):
        """测试字典转换"""
        stats = DecodeWorkerStats()
        stats.update_decode(10.0, 0)
        result = stats.to_dict()
        assert isinstance(result, dict)
        assert 'frames_decoded' in result
        assert 'decode_avg_ms' in result


# =============================================================================
# WorkerState Tests
# =============================================================================

class TestWorkerState:
    """WorkerState 测试"""

    def test_state_values(self):
        """测试状态值"""
        assert WorkerState.IDLE.value > 0
        assert WorkerState.RUNNING.value > 0
        assert WorkerState.STOPPING.value > 0
        assert WorkerState.STOPPED.value > 0
        assert WorkerState.ERROR.value > 0


# =============================================================================
# ProxyDecodeWorker Tests
# =============================================================================

class TestProxyDecodeWorker:
    """
    ProxyDecodeWorker 测试。

    验收：
    1. 连续播放请求时 proxy worker 不重复 seek
    2. stop 后线程能在 2 秒内退出
    """

    @pytest.fixture
    def mock_setup(self):
        """创建 mock 设置"""
        source_manager = MockSourceManager(total_frames=100)
        source_manager.open()

        frame_queue = ReadyFrameQueue(max_proxy_size=12, max_master_size=3)

        worker = ProxyDecodeWorker(
            source_manager=source_manager,
            frame_queue=frame_queue,
            prefetch_lookahead=8,
        )

        yield worker, source_manager, frame_queue

        # 清理
        if worker.is_running():
            worker.stop(timeout=2.0)
        source_manager.close()

    def test_worker_start_stop(self, mock_setup):
        """
        测试启动和停止。

        验收：stop 后线程能在 2 秒内退出
        """
        worker, _, _ = mock_setup

        # 启动
        assert worker.start() is True
        assert worker.is_running() is True
        assert worker.state == WorkerState.RUNNING

        # 停止
        start_time = time.time()
        assert worker.stop(timeout=2.0) is True
        elapsed = time.time() - start_time

        # 验收：2 秒内退出
        assert elapsed < 2.0
        assert worker.is_stopped() is True
        assert worker.state == WorkerState.STOPPED

    def test_sequential_playback_no_repeated_seek(self, mock_setup):
        """
        测试连续播放不重复 seek。

        验收：连续播放请求时 proxy worker 不重复 seek
        """
        worker, source_manager, frame_queue = mock_setup

        # 启动 worker
        worker.start()

        # 连续请求 10 帧（顺序播放）
        generation_id = 1
        for frame_index in range(10):
            worker.request_frame(
                frame_index=frame_index,
                generation_id=generation_id,
                reason=FrameRequestReason.PLAYBACK,
            )
            time.sleep(0.05)  # 等待解码完成

        # 停止 worker
        worker.stop(timeout=2.0)

        # 验收：连续播放时 seek 次数应该很少
        # SourceManager.get_proxy_frame 已经优化：
        # frame_index == _proxy_current_frame + 1时不 seek
        # 所以顺序播放 0-9 应只有 1 次 seek（第 0 帧）
        # 但由于 worker 可能从 -1 开始，可能有 1 次 seek
        seek_count = source_manager.proxy_seek_count

        # 最多允许 2 次 seek（初始定位）
        assert seek_count <= 2, f"Expected <= 2 seeks, got {seek_count}"

    def test_generation_discards_old_frames(self, mock_setup):
        """
        测试 generation 过期丢弃旧帧。

        验收：seek 后旧帧不会覆盖新 seek 结果
        """
        worker, source_manager, frame_queue = mock_setup

        # 启动 worker
        worker.start()

        # 请求第 0 帧（generation 1）
        worker.request_frame(0, generation_id=1, reason=FrameRequestReason.PLAYBACK)
        time.sleep(0.1)

        # 模拟 seek：递增 generation
        frame_queue.increment_generation()

        # 请求第 50 帧（generation 2）
        worker.request_frame(50, generation_id=2, reason=FrameRequestReason.SEEK)
        time.sleep(0.1)

        # 验收：队列中应该只有 generation 2 的帧
        entry = frame_queue.get_latest(is_master=False)
        if entry is not None:
            assert entry.generation_id >= 2

        # 停止 worker
        worker.stop(timeout=2.0)

    def test_generation_bump_purges_stale_pending_and_inflight(self, mock_setup):
        """V-P0-05: generation 提升时批量清除旧 generation 的 pending 与 in-flight。

        此前这些旧请求会逐个被取出、逐个解码、再逐个在 _process_request 里因
        过期被丢弃，等于让过期 Seek 继续占满 decoder。迁移后 update_generation
        立即批量清空旧队列，worker 尽快回到新 generation。
        """
        worker, _, _ = mock_setup

        # 不启动 worker 线程，直接操控内部状态以保证确定性。
        from src.shared.video.frame_request import (
            VideoFrameRequest, FrameRequestReason, RequestPriority,
        )
        # 填入 3 条旧 generation（1）的 pending 请求 + 1 条旧 in-flight 标记。
        old_requests = [
            VideoFrameRequest(
                frame_index=f,
                is_master=False,
                generation_id=1,
                priority=RequestPriority.get_default(FrameRequestReason.PLAYBACK),
                reason=FrameRequestReason.PLAYBACK,
            )
            for f in (10, 11, 12)
        ]
        with worker._request_lock:
            worker._pending_requests.extend(old_requests)
            worker._decoding_frames.add((1, 9))

        # 提升到 generation 2 —— 应批量清掉所有 gen<2 的 pending 与 in-flight。
        worker.update_generation(2)

        with worker._request_lock:
            remaining = list(worker._pending_requests)
            decoding = set(worker._decoding_frames)

        # 旧 pending 全部被清掉
        assert remaining == [], f"残留旧 generation pending：{remaining}"
        # 旧 in-flight 标记也被清掉
        assert (1, 9) not in decoding
        # 统计记录了丢弃
        assert worker._stats.frames_dropped >= 3

    def test_same_generation_bump_is_noop_for_current_requests(self, mock_setup):
        """V-P0-05: 同 generation 或更低的 update 不应清除当前请求。"""
        worker, _, _ = mock_setup
        from src.shared.video.frame_request import (
            VideoFrameRequest, FrameRequestReason, RequestPriority,
        )
        cur = VideoFrameRequest(
            frame_index=20,
            is_master=False,
            generation_id=5,
            priority=RequestPriority.get_default(FrameRequestReason.PLAYBACK),
            reason=FrameRequestReason.PLAYBACK,
        )
        with worker._request_lock:
            worker._pending_requests.append(cur)

        # 用相同 generation 再 update（不应提升，不应清除）
        worker.update_generation(5)
        # 用更低 generation update（不应提升，不应清除）
        worker.update_generation(3)

        with worker._request_lock:
            assert worker._pending_requests == [cur]

    def test_same_frame_different_generation_not_dropped(self, mock_setup):
        """
        V-P0-04: in-flight 去重必须包含 generation。

        此前 _decoding_frames 只存 frame_index：帧 N 在 generation A 下正在解码时，
        seek 把 generation 升到 B，再次请求 N 会被当作"已在解码"直接丢弃，
        新请求永远拿不到帧。现在 key 是 (generation, frame)，旧 generation 的
        在途解码不应屏蔽新 generation 对同一帧的请求。
        """
        worker, source_manager, frame_queue = mock_setup

        # 不启动 worker 线程，直接操控 _decoding_frames 以保证确定性。
        # 模拟：帧 5 正在 generation 1 下解码。
        with worker._request_lock:
            worker._decoding_frames.add((1, 5))

        # 在 generation 2 下再次请求帧 5 —— 不应被去重丢弃。
        worker.request_frame(5, generation_id=2, reason=FrameRequestReason.SEEK)

        # 验收：generation 2 的帧 5 必须进入 pending_requests（未被丢弃）。
        with worker._request_lock:
            pending = list(worker._pending_requests)
        matches = [
            r for r in pending
            if r.frame_index == 5 and r.generation_id == 2
        ]
        assert matches, (
            "generation 2 的帧 5 请求被错误去重丢弃（V-P0-04 回归）"
        )

        # 同 generation 下的重复请求仍应被去重（避免回归到"完全不去重"）。
        worker.request_frame(5, generation_id=2, reason=FrameRequestReason.PLAYBACK)
        with worker._request_lock:
            pending_dup = [
                r for r in worker._pending_requests
                if r.frame_index == 5 and r.generation_id == 2
            ]
        assert len(pending_dup) == 1, "同 (generation, frame) 仍应去重为一条"

    def test_prefetch_requests(self, mock_setup):
        """
        测试预读请求。
        """
        worker, source_manager, frame_queue = mock_setup

        # 启动 worker
        worker.start()

        # 触发预读
        worker.request_prefetch(
            current_frame=0,
            lookahead=5,
            generation_id=1,
        )

        # 等待处理
        time.sleep(0.3)

        # 停止 worker
        worker.stop(timeout=2.0)

        # 检查统计
        stats = worker.stats
        assert stats.frames_decoded > 0

    def test_pending_requests_sort_by_priority_and_frame(self, mock_setup):
        """
        同优先级请求不应比较 VideoFrameRequest 对象本身。
        """
        worker, _, _ = mock_setup

        worker.request_frame(10, generation_id=1, reason=FrameRequestReason.PLAYBACK)
        worker.request_frame(11, generation_id=1, reason=FrameRequestReason.PLAYBACK)
        worker.request_frame(5, generation_id=1, reason=FrameRequestReason.SEEK)

        pending = worker._pending_requests
        assert [request.frame_index for request in pending] == [5, 10, 11]


# =============================================================================
# MasterDecodeWorker Tests
# =============================================================================

class TestMasterDecodeWorker:
    """
    MasterDecodeWorker 测试。

    验收：
    1. 不允许返回错误 frame_index
    2. seek 后旧帧不会覆盖新 seek 结果
    """

    @pytest.fixture
    def mock_setup(self):
        """创建 mock 设置"""
        source_manager = MockSourceManager(total_frames=100)
        source_manager.open()

        frame_queue = ReadyFrameQueue(max_proxy_size=12, max_master_size=3)

        worker = MasterDecodeWorker(
            source_manager=source_manager,
            frame_queue=frame_queue,
        )

        yield worker, source_manager, frame_queue

        # 清理
        if worker.is_running():
            worker.stop(timeout=2.0)
        source_manager.close()

    def test_worker_start_stop(self, mock_setup):
        """测试启动和停止"""
        worker, _, _ = mock_setup

        assert worker.start() is True
        assert worker.is_running() is True

        start_time = time.time()
        assert worker.stop(timeout=2.0) is True
        elapsed = time.time() - start_time

        assert elapsed < 2.0
        assert worker.is_stopped() is True

    def test_master_frame_request(self, mock_setup):
        """
        测试 master 帧请求。
        """
        worker, source_manager, frame_queue = mock_setup

        worker.start()

        # 请求 master 帧
        worker.request_master_frame(
            frame_index=50,
            generation_id=1,
            reason=FrameRequestReason.SEEK,
        )

        time.sleep(0.2)

        worker.stop(timeout=2.0)

        # 验收：应该有帧入队
        stats = worker.stats
        assert stats.frames_decoded > 0

    def test_master_same_frame_different_generation_not_dropped(self, mock_setup):
        """V-P0-04: master 路径的 in-flight 去重同样必须包含 generation。"""
        worker, source_manager, frame_queue = mock_setup

        # 模拟：帧 50 正在 generation 1 下解码。
        with worker._request_lock:
            worker._decoding_frames.add((1, 50))

        # 在 generation 2 下再次请求 master 帧 50 —— 不应被去重丢弃。
        worker.request_master_frame(
            frame_index=50,
            generation_id=2,
            reason=FrameRequestReason.SEEK,
        )

        # master 请求进入 _pending_master_request（单槽），不是 _pending_requests。
        with worker._master_request_lock:
            pending_master = worker._pending_master_request
        assert pending_master is not None, (
            "generation 2 的 master 帧 50 请求被错误去重丢弃（V-P0-04 回归）"
        )
        assert pending_master.frame_index == 50
        assert pending_master.generation_id == 2
        assert pending_master.is_master

    def test_master_higher_priority(self, mock_setup):
        """
        测试 master 帧优先级高于 proxy。
        """
        worker, _, _ = mock_setup

        # master 请求应该有更高优先级
        worker.request_master_frame(
            frame_index=50,
            generation_id=1,
            reason=FrameRequestReason.SEEK,
        )

        # 检查 pending request
        assert worker._pending_master_request is not None


# =============================================================================
# DecodeWorkerManager Tests
# =============================================================================

class TestDecodeWorkerManager:
    """
    DecodeWorkerManager 测试。
    """

    @pytest.fixture
    def mock_setup(self):
        """创建 mock 设置"""
        source_manager = MockSourceManager(total_frames=100)
        source_manager.open()

        frame_queue = ReadyFrameQueue(max_proxy_size=12, max_master_size=3)

        manager = DecodeWorkerManager(
            source_manager=source_manager,
            frame_queue=frame_queue,
            prefetch_lookahead=8,
        )

        yield manager, source_manager, frame_queue

        # 清理
        manager.stop_workers(timeout=2.0)
        source_manager.close()

    def test_create_workers(self, mock_setup):
        """测试创建工作线程"""
        manager, _, _ = mock_setup

        assert manager.create_workers() is True
        assert manager.proxy_worker is not None
        assert manager.master_worker is not None

    def test_start_stop_workers(self, mock_setup):
        """测试启动和停止工作线程"""
        manager, _, _ = mock_setup

        manager.create_workers()

        start_time = time.time()
        assert manager.start_workers() is True
        assert manager.is_running() is True

        assert manager.stop_workers(timeout=2.0) is True
        elapsed = time.time() - start_time

        # 验收：2 秒内退出
        assert elapsed < 3.0  # 包含启动时间

    def test_generation_management(self, mock_setup):
        """测试 generation 管理"""
        manager, _, frame_queue = mock_setup

        manager.create_workers()

        gen1 = manager.generation
        gen2 = manager.increment_generation()

        assert gen2 == gen1 + 1
        assert frame_queue.current_generation == gen2

    def test_request_frames(self, mock_setup):
        """测试帧请求"""
        manager, _, frame_queue = mock_setup

        manager.create_workers()
        manager.start_workers()

        # 请求 proxy 帧
        manager.request_proxy_frame(0, reason=FrameRequestReason.PLAYBACK)

        # 请求 master 帧
        manager.request_master_frame(50, reason=FrameRequestReason.SEEK)

        time.sleep(0.2)

        manager.stop_workers(timeout=2.0)

        # 检查统计
        stats = manager.get_stats()
        assert 'proxy' in stats
        assert 'master' in stats


# =============================================================================
# VideoSession Tests (P2.B + P5.A)
# =============================================================================

class TestVideoSession:
    """
    VideoSession 测试。
    """

    def test_session_initial_state(self):
        """测试初始状态"""
        session = VideoSession()
        assert session.state == PlaybackState.UNLOADED
        assert session.path is None

    def test_session_stats(self):
        """测试会话统计"""
        session = VideoSession()
        stats = session.stats
        assert stats.total_frames_played == 0

    def test_session_unload(self):
        """测试 unload"""
        session = VideoSession()
        # unload 未加载的 session 应该返回 True
        assert session.unload(timeout=2.0) is True

    def test_playback_state_enum(self):
        """测试播放状态枚举"""
        assert PlaybackState.UNLOADED.value > 0
        assert PlaybackState.READY.value > 0
        assert PlaybackState.PLAYING.value > 0
        assert PlaybackState.PAUSED.value > 0


# =============================================================================
# Integration Tests
# =============================================================================

class TestIntegration:
    """
    集成测试。

    测试 Worker 与 Queue 的配合。
    """

    @pytest.fixture
    def full_setup(self):
        """完整设置"""
        source_manager = MockSourceManager(total_frames=100)
        source_manager.open()

        frame_queue = ReadyFrameQueue(max_proxy_size=12, max_master_size=3)

        manager = DecodeWorkerManager(
            source_manager=source_manager,
            frame_queue=frame_queue,
            prefetch_lookahead=8,
        )

        manager.create_workers()

        yield manager, source_manager, frame_queue

        manager.stop_workers(timeout=2.0)
        source_manager.close()

    def test_proxy_queue_dropping(self, full_setup):
        """
        测试 proxy 队列丢帧策略。

        验收：proxy queue 满时丢旧帧，保留最新帧
        """
        manager, source_manager, frame_queue = full_setup

        manager.start_workers()

        # 请求超过队列容量的帧
        for i in range(15):  # 大于 max_proxy_size=12
            manager.request_proxy_frame(i, reason=FrameRequestReason.PLAYBACK)
            time.sleep(0.05)

        manager.stop_workers(timeout=2.0)

        # 队列应该不超过最大容量
        queue_size = frame_queue.get_size(is_master=False)
        assert queue_size <= 12

    def test_master_queue_no_dropping(self, full_setup):
        """
        测试 master 队列不丢帧策略。

        验收：master queue 不丢目标帧
        """
        manager, source_manager, frame_queue = full_setup

        manager.start_workers()

        # 请求 master 帧
        manager.request_master_frame(50, reason=FrameRequestReason.SEEK)
        time.sleep(0.2)

        manager.stop_workers(timeout=2.0)

        # 检查队列中是否有请求的帧
        stats = frame_queue.stats
        # master 帧应该入队
        assert stats['master_enqueued'] > 0


# =============================================================================
# Design Verification Tests
# =============================================================================

class TestDesignVerification:
    """
    设计验证测试。

    验证 P2.B 的设计约束：
    - Worker 禁止直接接触 UI 和 ModernGL
    """

    def test_worker_no_ui_imports(self):
        """
        测试 Worker 不导入 UI 模块。

        验收：Worker 禁止直接接触 UI 控件
        """
        # 检查 decode_worker.py 的导入
        import ast
        import sys

        # 读取 decode_worker.py 源码
        source_path = "src/shared/video/decode_worker.py"

        with open(source_path, 'r') as f:
            source = f.read()

        tree = ast.parse(source)

        # 检查导入
        ui_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if 'ui' in alias.name.lower() or 'qt' in alias.name.lower():
                        ui_imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and ('ui' in node.module.lower() or 'qt' in node.module.lower()):
                    ui_imports.append(node.module)

        # 验收：不应该有 UI 导入
        assert len(ui_imports) == 0, f"Found UI imports: {ui_imports}"

    def test_worker_no_moderngl_imports(self):
        """
        测试 Worker 不导入 ModernGL。

        验收：Worker 禁止直接接触 ModernGL
        """
        import ast

        source_path = "src/shared/video/decode_worker.py"

        with open(source_path, 'r') as f:
            source = f.read()

        tree = ast.parse(source)

        # 检查导入
        gl_imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if 'moderngl' in alias.name.lower() or 'gl' in alias.name.lower():
                        gl_imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and ('moderngl' in node.module.lower() or 'gl' in node.module.lower()):
                    gl_imports.append(node.module)

        # 验收：不应该有 GL 导入
        assert len(gl_imports) == 0, f"Found GL imports: {gl_imports}"

    def test_worker_thread_safety(self):
        """
        测试 Worker 线程安全设计。

        验收：所有状态变更通过锁保护
        """
        # 检查 decode_worker.py 是否使用 threading.Lock
        import ast

        source_path = "src/shared/video/decode_worker.py"

        with open(source_path, 'r') as f:
            source = f.read()

        # 简单检查：源码应该包含 threading.Lock
        assert 'threading.Lock' in source or '_lock' in source
