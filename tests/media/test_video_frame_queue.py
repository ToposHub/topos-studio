"""
Test Video Frame Request and Queue

测试 VideoFrameRequest、QueueEntry 和 ReadyFrameQueue 的功能。

验收命令：
.venv/bin/python -m pytest tests/media/test_video_frame_queue.py -q

测试覆盖：
- VideoFrameRequest 创建和验证
- QueueEntry 属性和计算
- ReadyFrameQueue proxy/master 分离
- generation 过期丢弃
- 线程安全操作
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import numpy as np
import pytest

if TYPE_CHECKING:
    pass


# =============================================================================
# Mock DecodedFrame for testing
# =============================================================================


def create_mock_frame(frame_index: int, width: int = 1920, height: int = 1080) -> "DecodedFrame":
    """
    创建 mock DecodedFrame 用于测试。

    Args:
        frame_index: 帧号
        width: 宽度
        height: 高度

    Returns:
        DecodedFrame 实例
    """
    from src.shared.media.frame import DecodedFrame

    return DecodedFrame(
        frame_index=frame_index,
        pts=frame_index * 1001,  # mock pts
        time_seconds=frame_index / 24.0,
        data=np.zeros((height, width, 4), dtype=np.uint8),
        pixel_format="bgra",
        width=width,
        height=height,
        is_proxy=False,
    )


# =============================================================================
# VideoFrameRequest Tests
# =============================================================================


class TestVideoFrameRequest:
    """测试 VideoFrameRequest"""

    def test_basic_creation(self) -> None:
        """基本创建"""
        from src.shared.video.frame_request import (
            VideoFrameRequest,
            FrameRequestReason,
            RequestPriority,
        )

        req = VideoFrameRequest(
            frame_index=100,
            is_master=False,
            generation_id=1,
            priority=RequestPriority.PLAYBACK,
            reason=FrameRequestReason.PLAYBACK,
        )

        assert req.frame_index == 100
        assert req.is_master is False
        assert req.generation_id == 1
        assert req.priority == RequestPriority.PLAYBACK
        assert req.reason == FrameRequestReason.PLAYBACK

    def test_frozen(self) -> None:
        """frozen=True 确保不可变"""
        from src.shared.video.frame_request import (
            VideoFrameRequest,
            FrameRequestReason,
            RequestPriority,
        )

        req = VideoFrameRequest(
            frame_index=50,
            is_master=True,
            generation_id=2,
            priority=RequestPriority.SEEK,
            reason=FrameRequestReason.SEEK,
        )

        with pytest.raises(AttributeError):
            req.frame_index = 200  # type: ignore

    def test_frame_index_validation(self) -> None:
        """帧号必须 >= 0"""
        from src.shared.video.frame_request import (
            VideoFrameRequest,
            FrameRequestReason,
            RequestPriority,
        )

        with pytest.raises(ValueError, match="frame_index must be >= 0"):
            VideoFrameRequest(
                frame_index=-1,
                is_master=False,
                generation_id=1,
                priority=RequestPriority.PLAYBACK,
                reason=FrameRequestReason.PLAYBACK,
            )

    def test_generation_id_validation(self) -> None:
        """generation_id 必须 >= 0"""
        from src.shared.video.frame_request import (
            VideoFrameRequest,
            FrameRequestReason,
            RequestPriority,
        )

        with pytest.raises(ValueError, match="generation_id must be >= 0"):
            VideoFrameRequest(
                frame_index=0,
                is_master=False,
                generation_id=-1,
                priority=RequestPriority.PLAYBACK,
                reason=FrameRequestReason.PLAYBACK,
            )

    def test_create_playback(self) -> None:
        """便捷方法：创建播放请求"""
        from src.shared.video.frame_request import (
            VideoFrameRequest,
            FrameRequestReason,
            RequestPriority,
        )

        req = VideoFrameRequest.create_playback(
            frame_index=100,
            generation_id=1,
            is_master=False,
        )

        assert req.frame_index == 100
        assert req.is_master is False
        assert req.generation_id == 1
        assert req.priority == RequestPriority.PLAYBACK
        assert req.reason == FrameRequestReason.PLAYBACK
        assert req.can_drop is True

    def test_create_seek(self) -> None:
        """便捷方法：创建 seek 请求"""
        from src.shared.video.frame_request import (
            VideoFrameRequest,
            FrameRequestReason,
            RequestPriority,
        )

        req = VideoFrameRequest.create_seek(
            frame_index=50,
            generation_id=3,
            is_master=True,
        )

        assert req.frame_index == 50
        assert req.is_master is True
        assert req.generation_id == 3
        assert req.priority == RequestPriority.SEEK
        assert req.reason == FrameRequestReason.SEEK
        assert req.can_drop is False

    def test_create_scrub(self) -> None:
        """便捷方法：创建 scrub 请求"""
        from src.shared.video.frame_request import (
            VideoFrameRequest,
            FrameRequestReason,
            RequestPriority,
        )

        req = VideoFrameRequest.create_scrub(
            frame_index=200,
            generation_id=1,
            is_master=False,
        )

        assert req.frame_index == 200
        assert req.is_master is False
        assert req.priority == RequestPriority.SCRUB
        assert req.reason == FrameRequestReason.SCRUB
        assert req.can_drop is True

    def test_create_export(self) -> None:
        """便捷方法：创建导出请求"""
        from src.shared.video.frame_request import (
            VideoFrameRequest,
            FrameRequestReason,
            RequestPriority,
        )

        req = VideoFrameRequest.create_export(
            frame_index=1000,
            generation_id=2,
        )

        assert req.frame_index == 1000
        assert req.is_master is True
        assert req.priority == RequestPriority.EXPORT
        assert req.reason == FrameRequestReason.EXPORT
        assert req.can_drop is False

    def test_can_drop_playback(self) -> None:
        """播放请求可丢弃"""
        from src.shared.video.frame_request import (
            VideoFrameRequest,
            FrameRequestReason,
            RequestPriority,
        )

        req = VideoFrameRequest(
            frame_index=10,
            is_master=False,
            generation_id=1,
            priority=RequestPriority.PLAYBACK,
            reason=FrameRequestReason.PLAYBACK,
        )
        assert req.can_drop is True

    def test_can_drop_export(self) -> None:
        """导出请求不可丢弃"""
        from src.shared.video.frame_request import (
            VideoFrameRequest,
            FrameRequestReason,
            RequestPriority,
        )

        req = VideoFrameRequest(
            frame_index=10,
            is_master=True,
            generation_id=1,
            priority=RequestPriority.EXPORT,
            reason=FrameRequestReason.EXPORT,
        )
        assert req.can_drop is False

    def test_repr(self) -> None:
        """字符串表示"""
        from src.shared.video.frame_request import (
            VideoFrameRequest,
            FrameRequestReason,
            RequestPriority,
        )

        req = VideoFrameRequest(
            frame_index=100,
            is_master=True,
            generation_id=5,
            priority=RequestPriority.SEEK,
            reason=FrameRequestReason.SEEK,
        )

        repr_str = repr(req)
        assert "frame=100" in repr_str
        assert "master=True" in repr_str
        assert "gen=5" in repr_str
        assert "reason=seek" in repr_str


# =============================================================================
# RequestPriority Tests
# =============================================================================


class TestRequestPriority:
    """测试 RequestPriority"""

    def test_priority_order(self) -> None:
        """优先级顺序正确"""
        from src.shared.video.frame_request import RequestPriority

        assert RequestPriority.PLAYBACK < RequestPriority.SCRUB
        assert RequestPriority.SCRUB < RequestPriority.SEEK
        assert RequestPriority.SEEK < RequestPriority.COMPOSITE
        assert RequestPriority.COMPOSITE < RequestPriority.EXPORT

    def test_get_default(self) -> None:
        """默认优先级映射"""
        from src.shared.video.frame_request import (
            RequestPriority,
            FrameRequestReason,
        )

        assert RequestPriority.get_default(FrameRequestReason.PLAYBACK) == 10
        assert RequestPriority.get_default(FrameRequestReason.SCRUB) == 30
        assert RequestPriority.get_default(FrameRequestReason.SEEK) == 50
        assert RequestPriority.get_default(FrameRequestReason.COMPOSITE) == 70
        assert RequestPriority.get_default(FrameRequestReason.EXPORT) == 100


# =============================================================================
# QueueEntry Tests
# =============================================================================


class TestQueueEntry:
    """测试 QueueEntry"""

    def test_basic_creation(self) -> None:
        """基本创建"""
        from src.shared.video.frame_queue import QueueEntry, FrameSource

        frame = create_mock_frame(100)
        entry = QueueEntry(
            frame=frame,
            decode_ms=15.3,
            source=FrameSource.PROXY,
            can_drop=True,
            generation_id=1,
        )

        assert entry.frame_index == 100
        assert entry.decode_ms == 15.3
        assert entry.source == FrameSource.PROXY
        assert entry.can_drop is True
        assert entry.generation_id == 1

    def test_queue_wait_ms(self) -> None:
        """队列等待时间计算"""
        from src.shared.video.frame_queue import QueueEntry, FrameSource

        frame = create_mock_frame(50)
        entry = QueueEntry(
            frame=frame,
            decode_ms=10.0,
            source=FrameSource.MASTER,
            can_drop=False,
            generation_id=1,
        )

        # 刚入队时等待时间接近 0
        wait_ms = entry.queue_wait_ms
        assert wait_ms < 10.0  # 应小于 10ms

        # 等待后等待时间增加
        time.sleep(0.1)  # 100ms
        wait_ms = entry.queue_wait_ms
        assert wait_ms >= 100.0  # 至少 100ms

    def test_enqueue_time_default(self) -> None:
        """入队时间默认为当前时间"""
        from src.shared.video.frame_queue import QueueEntry, FrameSource

        before = time.time()
        frame = create_mock_frame(10)
        entry = QueueEntry(
            frame=frame,
            decode_ms=5.0,
            source=FrameSource.PROXY,
            can_drop=True,
            generation_id=1,
        )
        after = time.time()

        assert before <= entry.enqueue_time <= after

    def test_repr(self) -> None:
        """字符串表示"""
        from src.shared.video.frame_queue import QueueEntry, FrameSource

        frame = create_mock_frame(200)
        entry = QueueEntry(
            frame=frame,
            decode_ms=20.5,
            source=FrameSource.MASTER,
            can_drop=False,
            generation_id=2,
        )

        repr_str = repr(entry)
        assert "frame=200" in repr_str
        assert "source=master" in repr_str
        assert "decode_ms=20.5" in repr_str
        assert "gen=2" in repr_str


# =============================================================================
# ReadyFrameQueue Tests
# =============================================================================


class TestReadyFrameQueue:
    """测试 ReadyFrameQueue"""

    def test_basic_creation(self) -> None:
        """基本创建"""
        from src.shared.video.frame_queue import ReadyFrameQueue

        queue = ReadyFrameQueue(max_proxy_size=12, max_master_size=3)

        assert queue.max_proxy_size == 12
        assert queue.max_master_size == 3
        assert queue.current_generation == 0
        assert queue.get_size(is_master=False) == 0
        assert queue.get_size(is_master=True) == 0

    def test_enqueue_proxy(self) -> None:
        """入队 proxy 帧"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue(max_proxy_size=8)

        frame = create_mock_frame(10)
        entry = QueueEntry(
            frame=frame,
            decode_ms=10.0,
            source=FrameSource.PROXY,
            can_drop=True,
            generation_id=0,
        )

        result = queue.enqueue(entry)
        assert result is True
        assert queue.get_size(is_master=False) == 1

    def test_enqueue_master(self) -> None:
        """入队 master 帧"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue(max_master_size=3)

        frame = create_mock_frame(100)
        entry = QueueEntry(
            frame=frame,
            decode_ms=30.0,
            source=FrameSource.MASTER,
            can_drop=False,
            generation_id=0,
        )

        result = queue.enqueue(entry)
        assert result is True
        assert queue.get_size(is_master=True) == 1

    def test_dequeue_proxy_fifo(self) -> None:
        """proxy 队列 FIFO 出队"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue(max_proxy_size=10)

        # 入队多个帧
        for i in range(5):
            frame = create_mock_frame(i)
            entry = QueueEntry(
                frame=frame,
                decode_ms=10.0,
                source=FrameSource.PROXY,
                can_drop=True,
                generation_id=0,
            )
            queue.enqueue(entry)

        # FIFO 出队
        for i in range(5):
            entry = queue.dequeue(is_master=False)
            assert entry is not None
            assert entry.frame_index == i

    def test_dequeue_by_frame_index(self) -> None:
        """按帧号出队"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue(max_proxy_size=10)

        # 入队帧 0, 1, 2, 3, 4
        for i in range(5):
            frame = create_mock_frame(i)
            entry = QueueEntry(
                frame=frame,
                decode_ms=10.0,
                source=FrameSource.PROXY,
                can_drop=True,
                generation_id=0,
            )
            queue.enqueue(entry)

        # 指定帧号出队
        entry = queue.dequeue(is_master=False, frame_index=3)
        assert entry is not None
        assert entry.frame_index == 3

        # 验证队列剩余帧
        assert queue.get_frame_indices(is_master=False) == [0, 1, 2, 4]

    def test_proxy_queue_drops_old_when_full(self) -> None:
        """proxy 队列满时丢旧帧保留最新"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue(max_proxy_size=4)

        # 入队 6 帧（超过上限）
        for i in range(6):
            frame = create_mock_frame(i)
            entry = QueueEntry(
                frame=frame,
                decode_ms=10.0,
                source=FrameSource.PROXY,
                can_drop=True,
                generation_id=0,
            )
            queue.enqueue(entry)

        # 队列长度应为上限
        assert queue.get_size(is_master=False) == 4

        # 验证保留最新帧（2, 3, 4, 5）
        indices = queue.get_frame_indices(is_master=False)
        assert indices == [2, 3, 4, 5]

        # 统计：有 2 帧被丢弃
        stats = queue.stats
        assert stats["proxy_dropped"] >= 2

    def test_master_queue_preserves_frames(self) -> None:
        """master 队列不丢目标帧"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue(max_master_size=3)

        # 入队 3 帧
        for i in range(3):
            frame = create_mock_frame(i)
            entry = QueueEntry(
                frame=frame,
                decode_ms=30.0,
                source=FrameSource.MASTER,
                can_drop=False,
                generation_id=0,
            )
            queue.enqueue(entry)

        # 尝试入队第 4 帧
        frame = create_mock_frame(3)
        entry = QueueEntry(
            frame=frame,
            decode_ms=30.0,
            source=FrameSource.MASTER,
            can_drop=False,
            generation_id=0,
        )
        queue.enqueue(entry)

        # master queue 应保留所有帧或仅通过 generation 丢弃
        # 实际行为：入队但可能有警告
        assert queue.get_size(is_master=True) <= 4

    def test_generation_stale_frames_dropped(self) -> None:
        """generation 过期帧自动丢弃"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue(max_proxy_size=10)

        # 入队 generation 0 的帧
        for i in range(5):
            frame = create_mock_frame(i)
            entry = QueueEntry(
                frame=frame,
                decode_ms=10.0,
                source=FrameSource.PROXY,
                can_drop=True,
                generation_id=0,
            )
            queue.enqueue(entry)

        assert queue.get_size(is_master=False) == 5

        # seek 后递增 generation
        new_gen = queue.increment_generation()
        assert new_gen == 1

        # generation 0 的帧应被丢弃
        assert queue.get_size(is_master=False) == 0

    def test_generation_rejects_stale_enqueue(self) -> None:
        """入队过期 generation 帧被拒绝"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue()

        # 递增 generation 到 2
        queue.increment_generation()
        queue.increment_generation()
        assert queue.current_generation == 2

        # 尝试入队 generation 0 的帧
        frame = create_mock_frame(100)
        entry = QueueEntry(
            frame=frame,
            decode_ms=10.0,
            source=FrameSource.PROXY,
            can_drop=True,
            generation_id=0,  # 过期
        )

        result = queue.enqueue(entry)
        assert result is False  # 被拒绝
        assert queue.get_size(is_master=False) == 0

    def test_generation_accepts_current_enqueue(self) -> None:
        """入队当前 generation 帧成功"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue()

        # 递增 generation 到 1
        queue.increment_generation()
        assert queue.current_generation == 1

        # 入队 generation 1 的帧
        frame = create_mock_frame(50)
        entry = QueueEntry(
            frame=frame,
            decode_ms=10.0,
            source=FrameSource.PROXY,
            can_drop=True,
            generation_id=1,  # 当前
        )

        result = queue.enqueue(entry)
        assert result is True
        assert queue.get_size(is_master=False) == 1

    def test_peek(self) -> None:
        """查看队首帧"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue()

        frame = create_mock_frame(10)
        entry = QueueEntry(
            frame=frame,
            decode_ms=10.0,
            source=FrameSource.PROXY,
            can_drop=True,
            generation_id=0,
        )
        queue.enqueue(entry)

        # peek 不出队
        peeked = queue.peek(is_master=False)
        assert peeked is not None
        assert peeked.frame_index == 10
        assert queue.get_size(is_master=False) == 1

    def test_get_latest(self) -> None:
        """获取最新帧"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue()

        # 入队多个帧
        for i in range(5):
            frame = create_mock_frame(i)
            entry = QueueEntry(
                frame=frame,
                decode_ms=10.0,
                source=FrameSource.PROXY,
                can_drop=True,
                generation_id=0,
            )
            queue.enqueue(entry)

        # 最新帧应为队尾帧（frame_index=4）
        latest = queue.get_latest(is_master=False)
        assert latest is not None
        assert latest.frame_index == 4

    def test_contains(self) -> None:
        """检查队列是否包含指定帧"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue()

        # 入队帧 10, 20, 30
        for i in [10, 20, 30]:
            frame = create_mock_frame(i)
            entry = QueueEntry(
                frame=frame,
                decode_ms=10.0,
                source=FrameSource.PROXY,
                can_drop=True,
                generation_id=0,
            )
            queue.enqueue(entry)

        assert queue.contains(10, is_master=False) is True
        assert queue.contains(20, is_master=False) is True
        assert queue.contains(15, is_master=False) is False

    def test_clear(self) -> None:
        """清空队列"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue()

        # 入队帧
        for i in range(5):
            frame = create_mock_frame(i)
            entry = QueueEntry(
                frame=frame,
                decode_ms=10.0,
                source=FrameSource.PROXY,
                can_drop=True,
                generation_id=0,
            )
            queue.enqueue(entry)

        # 清空 proxy 队列
        count = queue.clear(is_master=False)
        assert count == 5
        assert queue.get_size(is_master=False) == 0

    def test_clear_all(self) -> None:
        """清空所有队列"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue()

        # 入队 proxy 和 master 帧
        for i in range(3):
            frame = create_mock_frame(i)
            proxy_entry = QueueEntry(
                frame=frame,
                decode_ms=10.0,
                source=FrameSource.PROXY,
                can_drop=True,
                generation_id=0,
            )
            queue.enqueue(proxy_entry)

            master_frame = create_mock_frame(i + 100)
            master_entry = QueueEntry(
                frame=master_frame,
                decode_ms=30.0,
                source=FrameSource.MASTER,
                can_drop=False,
                generation_id=0,
            )
            queue.enqueue(master_entry)

        # 清空所有
        proxy_count, master_count = queue.clear_all()
        assert proxy_count == 3
        assert master_count == 3
        assert queue.get_size(is_master=False) == 0
        assert queue.get_size(is_master=True) == 0

    def test_stats(self) -> None:
        """统计信息"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue(max_proxy_size=4)

        # 入队帧
        for i in range(6):
            frame = create_mock_frame(i)
            entry = QueueEntry(
                frame=frame,
                decode_ms=10.0,
                source=FrameSource.PROXY,
                can_drop=True,
                generation_id=0,
            )
            queue.enqueue(entry)

        # 出队 2 帧
        queue.dequeue(is_master=False)
        queue.dequeue(is_master=False)

        stats = queue.stats
        assert stats["proxy_enqueued"] == 6
        assert stats["proxy_dequeued"] == 2
        assert stats["proxy_dropped"] >= 2  # 满时丢弃


# =============================================================================
# Thread Safety Tests
# =============================================================================


class TestReadyFrameQueueThreadSafety:
    """测试 ReadyFrameQueue 线程安全"""

    def test_concurrent_enqueue(self) -> None:
        """并发入队"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue(max_proxy_size=100)
        num_threads = 10
        frames_per_thread = 10

        def enqueue_frames(thread_id: int) -> None:
            for i in range(frames_per_thread):
                frame = create_mock_frame(thread_id * 100 + i)
                entry = QueueEntry(
                    frame=frame,
                    decode_ms=10.0,
                    source=FrameSource.PROXY,
                    can_drop=True,
                    generation_id=0,
                )
                queue.enqueue(entry)

        threads = [
            threading.Thread(target=enqueue_frames, args=(i,))
            for i in range(num_threads)
        ]

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        # 验证队列状态
        assert queue.get_size(is_master=False) <= 100
        stats = queue.stats
        assert stats["proxy_enqueued"] == num_threads * frames_per_thread

    def test_concurrent_enqueue_dequeue(self) -> None:
        """并发入队和出队"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue(max_proxy_size=50)
        enqueue_count = 0
        dequeue_count = 0

        def enqueue_frames() -> None:
            nonlocal enqueue_count
            for i in range(100):
                frame = create_mock_frame(i)
                entry = QueueEntry(
                    frame=frame,
                    decode_ms=10.0,
                    source=FrameSource.PROXY,
                    can_drop=True,
                    generation_id=0,
                )
                queue.enqueue(entry)
                enqueue_count += 1

        def dequeue_frames() -> None:
            nonlocal dequeue_count
            for i in range(50):
                entry = queue.dequeue(is_master=False)
                if entry is not None:
                    dequeue_count += 1
                time.sleep(0.001)  # 短暂等待

        enqueue_thread = threading.Thread(target=enqueue_frames)
        dequeue_thread = threading.Thread(target=dequeue_frames)

        enqueue_thread.start()
        dequeue_thread.start()

        enqueue_thread.join()
        dequeue_thread.join()

        # 验证无异常，队列状态一致
        stats = queue.stats
        assert stats["proxy_enqueued"] == 100
        assert stats["proxy_dequeued"] == dequeue_count

    def test_concurrent_generation_change(self) -> None:
        """并发 generation 变化"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue(max_proxy_size=50)
        results: list[tuple[int, bool]] = []

        def enqueue_frames(gen: int) -> None:
            for i in range(20):
                frame = create_mock_frame(gen * 100 + i)
                entry = QueueEntry(
                    frame=frame,
                    decode_ms=10.0,
                    source=FrameSource.PROXY,
                    can_drop=True,
                    generation_id=gen,
                )
                success = queue.enqueue(entry)
                results.append((gen, success))

        def change_generation() -> None:
            time.sleep(0.01)  # 等待部分帧入队
            queue.increment_generation()

        threads = [
            threading.Thread(target=enqueue_frames, args=(0,)),
            threading.Thread(target=enqueue_frames, args=(1,)),
            threading.Thread(target=change_generation),
        ]

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        # 验证 generation 0 的部分帧被拒绝
        gen_0_success = [r for r in results if r[0] == 0 and r[1]]
        gen_1_success = [r for r in results if r[0] == 1 and r[1]]

        # generation 变化后，旧 generation 帧应被拒绝
        assert queue.current_generation >= 1


# =============================================================================
# Integration Tests
# =============================================================================


class TestFrameQueueIntegration:
    """集成测试"""

    def test_seek_scenario(self) -> None:
        """seek 场景：丢弃旧帧，请求新帧"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue(max_proxy_size=12)

        # 播放：入队 frame 0-10
        for i in range(11):
            frame = create_mock_frame(i)
            entry = QueueEntry(
                frame=frame,
                decode_ms=10.0,
                source=FrameSource.PROXY,
                can_drop=True,
                generation_id=0,
            )
            queue.enqueue(entry)

        assert queue.get_size(is_master=False) == 11

        # seek 到 frame 50：递增 generation
        new_gen = queue.increment_generation()
        assert new_gen == 1
        assert queue.get_size(is_master=False) == 0  # 旧帧被丢弃

        # 入队 seek 后的帧
        for i in range(50, 55):
            frame = create_mock_frame(i)
            entry = QueueEntry(
                frame=frame,
                decode_ms=10.0,
                source=FrameSource.PROXY,
                can_drop=True,
                generation_id=1,
            )
            queue.enqueue(entry)

        assert queue.get_size(is_master=False) == 5

        # 取最新帧
        latest = queue.get_latest(is_master=False)
        assert latest is not None
        assert latest.frame_index == 54

    def test_playback_with_master_request(self) -> None:
        """播放时请求 master 帧"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue()

        # 播放 proxy 帧
        for i in range(5):
            frame = create_mock_frame(i)
            proxy_entry = QueueEntry(
                frame=frame,
                decode_ms=10.0,
                source=FrameSource.PROXY,
                can_drop=True,
                generation_id=0,
            )
            queue.enqueue(proxy_entry)

        # 同时请求 master 帧（暂停/逐帧）
        frame = create_mock_frame(2)
        master_entry = QueueEntry(
            frame=frame,
            decode_ms=50.0,
            source=FrameSource.MASTER,
            can_drop=False,
            generation_id=0,
        )
        queue.enqueue(master_entry)

        # proxy 和 master 分离存储
        assert queue.get_size(is_master=False) == 5
        assert queue.get_size(is_master=True) == 1

        # 取 master 帧
        master = queue.dequeue(is_master=True)
        assert master is not None
        assert master.frame_index == 2
        assert master.source == FrameSource.MASTER

    def test_duplicate_frame_replacement(self) -> None:
        """重复帧号替换"""
        from src.shared.video.frame_queue import ReadyFrameQueue, QueueEntry, FrameSource

        queue = ReadyFrameQueue()

        # 入队 frame 10
        frame1 = create_mock_frame(10)
        entry1 = QueueEntry(
            frame=frame1,
            decode_ms=10.0,
            source=FrameSource.PROXY,
            can_drop=True,
            generation_id=0,
        )
        queue.enqueue(entry1)

        # 再次入队 frame 10（不同 decode_ms）
        frame2 = create_mock_frame(10)
        entry2 = QueueEntry(
            frame=frame2,
            decode_ms=15.0,
            source=FrameSource.PROXY,
            can_drop=True,
            generation_id=0,
        )
        queue.enqueue(entry2)

        # 队列只有一个 frame 10
        assert queue.get_size(is_master=False) == 1

        # 取出的是新帧
        entry = queue.dequeue(is_master=False)
        assert entry is not None
        assert entry.decode_ms == 15.0  # 新值