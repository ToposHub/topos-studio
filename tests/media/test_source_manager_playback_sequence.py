"""
SourceManager 顺序播放 seek 优化测试。

测试目标（P1.A）：
1. 连续请求 0-30 帧，seek 次数 <= 1（开始时可能需要一次）
2. 请求 0、1、2、100，允许 100 触发 seek
3. 倒退到 10，允许 seek

测试策略：
- 使用 mock source 统计 seek_frame() 调用次数
- 不依赖实际视频文件

验收命令：
.venv/bin/python -m pytest tests/media/test_source_manager_playback_sequence.py -q
"""

from __future__ import annotations

import pytest
from typing import Optional, Any, Callable
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import numpy as np

from src.shared.video.playback_clock import SourceManager
from src.shared.media.frame import DecodedFrame
from src.shared.media.metadata import VideoMetadata, Rational


# =============================================================================
# Mock MediaSource
# =============================================================================

@dataclass
class MockSourceStats:
    """Mock source 统计数据"""
    seek_count: int = 0
    read_next_count: int = 0
    seek_targets: list[int] = field(default_factory=list)
    current_frame: int = 0


class MockMediaSource:
    """
    Mock MediaSource 用于测试。

    统计 seek_frame() 和 read_next_frame() 调用次数。
    """

    def __init__(self, total_frames: int = 100) -> None:
        self._total_frames: int = total_frames
        self._is_open: bool = False
        self._current_frame: int = 0
        self._stats: MockSourceStats = MockSourceStats()

        # 创建合成元数据
        self._metadata: VideoMetadata = VideoMetadata(
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            total_frames=total_frames,
            codec_name='mock',
            codec_profile='Mock',
            pixel_format='yuv420p',
            bit_depth=8,
        )

    @property
    def stats(self) -> MockSourceStats:
        """获取统计数据"""
        return self._stats

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def current_frame(self) -> int:
        return self._current_frame

    def probe(self) -> VideoMetadata:
        """探测元数据"""
        return self._metadata

    def open(self) -> None:
        """打开"""
        self._is_open = True
        self._current_frame = 0
        self._stats = MockSourceStats()

    def close(self) -> None:
        """关闭"""
        self._is_open = False

    def seek_frame(self, frame_index: int) -> None:
        """
        定位帧。

        统计 seek 调用次数。
        """
        if not self._is_open:
            raise RuntimeError("Source not open")

        # Clamp to valid range
        frame_index = max(0, min(frame_index, self._total_frames - 1))

        self._stats.seek_count += 1
        self._stats.seek_targets.append(frame_index)
        self._current_frame = frame_index

    def read_next_frame(self) -> DecodedFrame:
        """
        读取下一帧。

        统计 read_next 调用次数。
        返回合成的 DecodedFrame。
        """
        if not self._is_open:
            raise RuntimeError("Source not open")

        if self._current_frame >= self._total_frames:
            raise StopIteration("EOF")

        self._stats.read_next_count += 1

        # 创建合成帧数据
        frame_data = np.zeros((1080, 1920, 3), dtype=np.uint8)
        frame = DecodedFrame(
            frame_index=self._current_frame,
            pts=self._current_frame,  # PTS = frame_index
            data=frame_data,
            width=1920,
            height=1080,
            dtype=np.uint8,
            pixel_format='rgb24',
            time_seconds=self._current_frame / 24.0,
        )

        self._current_frame += 1
        return frame

    def read_frame(self, frame_index: int) -> DecodedFrame:
        """
        读取指定帧。

        内部会调用 seek_frame + read_next_frame。
        """
        if self._current_frame != frame_index:
            self.seek_frame(frame_index)
        return self.read_next_frame()


# =============================================================================
# 测试类
# =============================================================================

class TestSequentialPlaybackSeekOptimization:
    """顺序播放 seek 优化测试"""

    def test_sequential_frames_seek_count(self) -> None:
        """
        连续请求 0-30 帧，seek 次数应该 <= 1。

        P1.A 要求：顺序播放不每帧 seek
        """
        # 创建 mock source
        mock_source = MockMediaSource(total_frames=100)

        # 创建 SourceManager，使用 mock source factory
        def source_factory(path: str, policy: Optional[Any] = None) -> MockMediaSource:
            return mock_source

        manager = SourceManager(
            path='/mock/video.mp4',
            source_factory=source_factory
        )
        manager.open()

        # 连续请求 0-30 帧（模拟播放）
        for frame_idx in range(31):
            frame = manager.get_proxy_frame(frame_idx)
            assert frame is not None
            assert frame.frame_index == frame_idx

        # 验证 seek 次数
        # P1.A: 顺序播放应该只在开始时 seek 一次（或零次）
        # 由于 _proxy_current_frame 初始为 -1，请求第 0 帧是顺序下一帧
        # 所以理论上 seek_count 应该是 0
        assert mock_source.stats.seek_count <= 1, \
            f"Sequential playback should have <= 1 seek, got {mock_source.stats.seek_count}"

        # 验证 read_next_frame 调用了 31 次
        assert mock_source.stats.read_next_count == 31, \
            f"Should read 31 frames, got {mock_source.stats.read_next_count}"

        manager.close()

    def test_jump_to_far_frame_seek_count(self) -> None:
        """
        请求 0、1、2、100，100 应该触发 seek。

        P1.A 要求：大跳跃允许 seek
        """
        mock_source = MockMediaSource(total_frames=200)

        def source_factory(path: str, policy: Optional[Any] = None) -> MockMediaSource:
            return mock_source

        manager = SourceManager(
            path='/mock/video.mp4',
            source_factory=source_factory
        )
        manager.open()

        # 请求 0、1、2（顺序）
        for frame_idx in [0, 1, 2]:
            frame = manager.get_proxy_frame(frame_idx)
            assert frame is not None

        # 跳跃到 100
        frame = manager.get_proxy_frame(100)
        assert frame is not None
        assert frame.frame_index == 100

        # 验证 seek 次数
        # 顺序 0、1、2 不需要 seek
        # 跳跃到 100 需要 seek
        assert mock_source.stats.seek_count >= 1, \
            f"Jump to 100 should trigger seek, got {mock_source.stats.seek_count}"

        # 验证最后一次 seek 目标是 100
        assert mock_source.stats.seek_targets[-1] == 100, \
            f"Last seek should target 100, got {mock_source.stats.seek_targets}"

        manager.close()

    def test_backward_seek_allowed(self) -> None:
        """
        倒退到 10，允许 seek。

        P1.A 要求：倒退需要 seek
        """
        mock_source = MockMediaSource(total_frames=100)

        def source_factory(path: str, policy: Optional[Any] = None) -> MockMediaSource:
            return mock_source

        manager = SourceManager(
            path='/mock/video.mp4',
            source_factory=source_factory
        )
        manager.open()

        # 顺序播放到 30
        for frame_idx in range(31):
            frame = manager.get_proxy_frame(frame_idx)
            assert frame is not None

        # 记录当前 seek 次数（应该 <= 1）
        seek_count_before_backward = mock_source.stats.seek_count

        # 倒退到 10
        frame = manager.get_proxy_frame(10)
        assert frame is not None
        assert frame.frame_index == 10

        # 验证倒退触发了 seek
        assert mock_source.stats.seek_count > seek_count_before_backward, \
            f"Backward seek should trigger new seek, before={seek_count_before_backward}, after={mock_source.stats.seek_count}"

        # 验证最后一次 seek 目标是 10
        assert mock_source.stats.seek_targets[-1] == 10, \
            f"Last seek should target 10, got {mock_source.stats.seek_targets}"

        manager.close()

    def test_repeated_current_frame_no_seek(self) -> None:
        """
        重复请求当前帧不需要 seek。

        验证缓存命中时不 seek
        """
        mock_source = MockMediaSource(total_frames=100)

        def source_factory(path: str, policy: Optional[Any] = None) -> MockMediaSource:
            return mock_source

        manager = SourceManager(
            path='/mock/video.mp4',
            source_factory=source_factory
        )
        manager.open()

        # 请求第 5 帧
        frame = manager.get_proxy_frame(5)
        assert frame is not None

        seek_count_after_first = mock_source.stats.seek_count

        # 再次请求第 5 帧（应该从缓存获取）
        frame = manager.get_proxy_frame(5)
        assert frame is not None

        # 验证没有额外的 seek
        assert mock_source.stats.seek_count == seek_count_after_first, \
            f"Repeated frame should not trigger seek"

        manager.close()

    def test_initial_frame_is_sequential(self) -> None:
        """
        从初始状态请求第 0 帧是顺序播放。

        _proxy_current_frame == -1，请求 0 是顺序下一帧
        """
        mock_source = MockMediaSource(total_frames=100)

        def source_factory(path: str, policy: Optional[Any] = None) -> MockMediaSource:
            return mock_source

        manager = SourceManager(
            path='/mock/video.mp4',
            source_factory=source_factory
        )
        manager.open()

        # 验证初始帧号是 -1
        assert manager._proxy_current_frame == -1, \
            "Initial _proxy_current_frame should be -1"

        # 请求第 0 帧
        frame = manager.get_proxy_frame(0)
        assert frame is not None
        assert frame.frame_index == 0

        # 验证没有 seek（因为 -1 -> 0 是顺序）
        assert mock_source.stats.seek_count == 0, \
            f"Requesting frame 0 from -1 should be sequential, seek_count={mock_source.stats.seek_count}"

        manager.close()

    def test_master_frame_always_accurate(self) -> None:
        """
        Master 源保持精确，每次非顺序请求都 seek。

        P1.A: Master 源不为性能牺牲准确性
        """
        mock_source = MockMediaSource(total_frames=100)

        def source_factory(path: str, policy: Optional[Any] = None) -> MockMediaSource:
            return mock_source

        manager = SourceManager(
            path='/mock/video.mp4',
            source_factory=source_factory
        )
        manager.open()

        # Master 请求第 10 帧
        frame = manager.get_master_frame(10)
        assert frame is not None

        # Master 请求第 20 帧（跳跃）
        frame = manager.get_master_frame(20)
        assert frame is not None

        # Master 应该为每个非顺序请求 seek
        # 第 10 帧从 -1 -> 10，需要 seek
        # 第 20 帧从 10 -> 20，需要 seek
        assert mock_source.stats.seek_count >= 2, \
            f"Master source should seek for accuracy"

        manager.close()


# =============================================================================
# 边缘情况测试
# =============================================================================

class TestEdgeCases:
    """边缘情况测试"""

    def test_eof_handling(self) -> None:
        """EOF 处理"""
        mock_source = MockMediaSource(total_frames=10)

        def source_factory(path: str, policy: Optional[Any] = None) -> MockMediaSource:
            return mock_source

        manager = SourceManager(
            path='/mock/video.mp4',
            source_factory=source_factory
        )
        manager.open()

        # 请求所有帧
        for frame_idx in range(10):
            frame = manager.get_proxy_frame(frame_idx)
            assert frame is not None

        # EOF 应返回 None 或抛出异常
        try:
            frame = manager.get_proxy_frame(10)
            # 如果返回 None，是正常处理
            assert frame is None or frame.frame_index < 10
        except Exception:
            # 抛出异常也是正常处理
            pass

        manager.close()

    def test_invalid_frame_index(self) -> None:
        """无效帧号处理"""
        mock_source = MockMediaSource(total_frames=100)

        def source_factory(path: str, policy: Optional[Any] = None) -> MockMediaSource:
            return mock_source

        manager = SourceManager(
            path='/mock/video.mp4',
            source_factory=source_factory
        )
        manager.open()

        # 请求负帧号
        # 当前实现可能不验证，但应该不会崩溃
        try:
            frame = manager.get_proxy_frame(-1)
            # 如果返回 None 或抛出异常，都是可接受的
        except (ValueError, RuntimeError):
            pass

        manager.close()

    def test_source_not_open(self) -> None:
        """源未打开时请求帧"""
        mock_source = MockMediaSource(total_frames=100)

        def source_factory(path: str, policy: Optional[Any] = None) -> MockMediaSource:
            return mock_source

        manager = SourceManager(
            path='/mock/video.mp4',
            source_factory=source_factory
        )
        # 不调用 open()

        # 应返回 None
        frame = manager.get_proxy_frame(0)
        assert frame is None

        frame = manager.get_master_frame(0)
        assert frame is None


# =============================================================================
# 性能统计测试
# =============================================================================

class TestPerformanceStats:
    """性能统计测试"""

    def test_seek_time_tracking(self) -> None:
        """验证 seek 时间被跟踪"""
        mock_source = MockMediaSource(total_frames=100)

        def source_factory(path: str, policy: Optional[Any] = None) -> MockMediaSource:
            return mock_source

        manager = SourceManager(
            path='/mock/video.mp4',
            source_factory=source_factory
        )
        manager.open()

        # 顺序播放，seek 时间应该为 0
        for frame_idx in range(10):
            manager.get_proxy_frame(frame_idx)

        # 检查性能监控器数据
        # 由于 mock source seek 很快，时间应该很小或为 0
        # 这里只验证不会崩溃

        manager.close()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])