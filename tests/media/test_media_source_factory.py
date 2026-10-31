"""
MediaSourceFactory 重复 open 问题修复测试。

测试目标（P3.A）：
1. MediaSourceFactory.create() 只创建不 open
2. SourceManager.open() 调用一次，不重复 open
3. mock source 记录 open 次数，验证各只 open 一次

测试策略：
- 使用 MockMediaSource 记录 open 调用次数
- 使用 source_factory 绕过 MediaSourceFactory 路径验证
- 测试 SourceManager.open() 不重复调用
"""

from __future__ import annotations

import pytest
import os
import tempfile
from typing import Optional, ClassVar
from unittest.mock import MagicMock, patch

from src.shared.media.factory import MediaSourceFactory, MediaBackend
from src.shared.media.source import MediaSource, MediaOpenError
from src.shared.media.metadata import VideoMetadata, FPS_24, Rational
from src.shared.media.frame import DecodedFrame
from src.shared.video.playback_clock import SourceManager
from src.shared.media.proxy import DEFAULT_PROXY_CONFIG


# =============================================================================
# Mock MediaSource（记录 open 次数）
# =============================================================================

class MockMediaSource(MediaSource):
    """
    Mock MediaSource，记录 open 调用次数。

    用于验证 create() 和 SourceManager 的 open 行为。
    """

    # 统计 open 调用次数（实例级别）
    _open_count: int = 0
    _close_count: int = 0

    def __init__(
        self,
        path: str,
        target_size: Optional[tuple[int, int]] = None,
        should_fail_open: bool = False
    ) -> None:
        """初始化 Mock MediaSource"""
        self._path = path
        self._target_size = target_size
        self._should_fail_open = should_fail_open
        self._is_open = False
        self._metadata: Optional[VideoMetadata] = None
        self._current_frame = 0
        self._instance_open_count = 0  # 实例级别计数

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def open_count(self) -> int:
        """获取此实例的 open 调用次数"""
        return self._instance_open_count

    def open(self) -> None:
        """打开媒体源，记录调用次数"""
        self._instance_open_count += 1
        MockMediaSource._open_count += 1

        if self._should_fail_open:
            raise MediaOpenError("Mock open failed", source_path=self._path)

        if self._is_open:
            return  # 已打开，不重复设置状态

        self._is_open = True

        # 创建 mock metadata（包含所有必需参数）
        self._metadata = VideoMetadata(
            width=1920,
            height=1080,
            fps=FPS_24,
            total_frames=240,
            codec_name='mock_codec',
            codec_profile='High',  # 添加必需参数
            pixel_format='yuv420p',
            bit_depth=8,
        )

    def close(self) -> None:
        """关闭媒体源，记录调用次数"""
        MockMediaSource._close_count += 1
        self._is_open = False

    def probe(self) -> VideoMetadata:
        """探测元数据"""
        if self._metadata is None:
            self._metadata = VideoMetadata(
                width=1920,
                height=1080,
                fps=FPS_24,
                total_frames=240,
                codec_name='mock_codec',
                codec_profile='High',
                pixel_format='yuv420p',
                bit_depth=8,
            )
        return self._metadata

    def read_frame(self, frame_index: int) -> DecodedFrame:
        """读取帧"""
        if not self._is_open:
            raise MediaOpenError("Source not open", source_path=self._path)
        self._current_frame = frame_index
        return DecodedFrame(
            frame_index=frame_index,
            time_seconds=frame_index / 24.0,
            pixel_format='yuv420p',
            width=1920,
            height=1080,
            dtype='uint8',
            is_packed=False,
        )

    def read_next_frame(self) -> DecodedFrame:
        """读取下一帧"""
        return self.read_frame(self._current_frame + 1)

    def seek_frame(self, frame_index: int) -> None:
        """定位到指定帧"""
        self._current_frame = frame_index

    @classmethod
    def reset_counters(cls) -> None:
        """重置全局计数器"""
        cls._open_count = 0
        cls._close_count = 0


# =============================================================================
# MediaSourceFactory Tests（直接测试 _create_with_backend）
# =============================================================================

class TestMediaSourceFactoryCreateNoAutoOpen:
    """测试 create() 不自动 open"""

    @pytest.fixture(autouse=True)
    def setup_mock_backend(self) -> None:
        """每个测试前重置计数器"""
        MockMediaSource.reset_counters()
        # 注册 mock backend（P3.C: 签名需要包含 preserve_planes 参数）
        def create_mock(path: str, target_size: Optional[tuple[int, int]], preserve_planes: bool = False) -> MockMediaSource:
            return MockMediaSource(path, target_size)
        MediaSourceFactory.register_backend('mock_test', create_mock, lambda: True)

    def test_create_with_backend_no_auto_open(self) -> None:
        """_create_with_backend 应返回未打开的 source"""
        # 直接调用 _create_with_backend
        source = MediaSourceFactory._create_with_backend('mock_test', '/test/video.mp4', None)

        # 验证未打开
        assert source.is_open is False
        # open_count 应为 0（没有调用 open）
        assert source.open_count == 0

    def test_open_source_method(self) -> None:
        """open_source() 方法应打开 source"""
        source = MediaSourceFactory._create_with_backend('mock_test', '/test/video.mp4', None)
        assert source.is_open is False
        assert source.open_count == 0

        # 使用 open_source() 打开
        opened_source = MediaSourceFactory.open_source(source)

        # 验证已打开
        assert opened_source.is_open is True
        assert source.open_count == 1

    def test_open_source_handles_failure(self) -> None:
        """open_source() 应正确处理打开失败"""
        # 注册失败 backend（P3.C: 签名需要包含 preserve_planes 参数）
        def create_mock_fail(path: str, target_size: Optional[tuple[int, int]], preserve_planes: bool = False) -> MockMediaSource:
            return MockMediaSource(path, target_size, should_fail_open=True)
        MediaSourceFactory.register_backend('mock_fail_test', create_mock_fail, lambda: True)

        source = MediaSourceFactory._create_with_backend('mock_fail_test', '/test/video.mp4', None)
        assert source.is_open is False

        # 使用 open_source() 应抛出异常
        with pytest.raises(MediaOpenError):
            MediaSourceFactory.open_source(source)

        # 失败后 close_count 应 >= 1（清理资源）
        assert MockMediaSource._close_count >= 1


# =============================================================================
# SourceManager Tests（核心验收）
# =============================================================================

class TestSourceManagerNoDuplicateOpen:
    """测试 SourceManager 不重复 open"""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """每个测试前重置计数器"""
        MockMediaSource.reset_counters()

    def test_source_manager_single_open(self) -> None:
        """SourceManager.open() 应只调用一次 open"""
        # 创建 mock factory 函数
        def mock_factory(path: str, policy=None) -> MockMediaSource:
            return MockMediaSource(path)

        # 创建 SourceManager
        manager = SourceManager(
            path='/test/video.mp4',
            proxy_config=DEFAULT_PROXY_CONFIG,
            source_factory=mock_factory
        )

        # 打开 SourceManager（source_factory 不受路径验证影响）
        success = manager.open()

        # 验证成功打开
        assert success is True
        assert manager.is_open is True

        # master_source 应只 open 一次
        # 由于 proxy_source 使用同一个源（不需要降级），总共只 open 一次
        master_source = manager._master_source
        assert isinstance(master_source, MockMediaSource)
        assert master_source.open_count == 1

        # 关闭
        manager.close()

    def test_source_manager_master_and_proxy_same_source(self) -> None:
        """当不需要降级时，master 和 proxy 使用同一个源，只 open 一次"""
        # 创建 mock factory 函数
        def mock_factory(path: str, policy=None) -> MockMediaSource:
            return MockMediaSource(path)

        manager = SourceManager(
            path='/test/video.mp4',
            proxy_config=DEFAULT_PROXY_CONFIG,
            source_factory=mock_factory
        )

        success = manager.open()
        assert success is True

        # master 和 proxy 应该是同一个实例
        assert manager._master_source == manager._proxy_source

        # 只调用一次 open
        master_source = manager._master_source
        assert isinstance(master_source, MockMediaSource)
        assert master_source.open_count == 1

        manager.close()

    def test_source_manager_failure_cleanup(self) -> None:
        """SourceManager.open() 失败时应清理资源"""
        # 创建 mock factory 函数（会失败）
        def mock_factory_fail(path: str, policy=None) -> MockMediaSource:
            return MockMediaSource(path, should_fail_open=True)

        manager = SourceManager(
            path='/test/video.mp4',
            proxy_config=DEFAULT_PROXY_CONFIG,
            source_factory=mock_factory_fail
        )

        # 打开应失败
        success = manager.open()

        assert success is False
        assert manager.is_open is False

        # close_count 应 >= 1（清理资源）
        assert MockMediaSource._close_count >= 1

    def test_source_manager_multiple_instances(self) -> None:
        """多次创建 SourceManager，每个实例只 open 一次"""
        def mock_factory(path: str, policy=None) -> MockMediaSource:
            return MockMediaSource(path)

        # 创建第一个 SourceManager
        manager1 = SourceManager(
            path='/test/video1.mp4',
            proxy_config=DEFAULT_PROXY_CONFIG,
            source_factory=mock_factory
        )
        success1 = manager1.open()
        assert success1 is True
        source1 = manager1._master_source
        assert isinstance(source1, MockMediaSource)
        assert source1.open_count == 1

        # 创建第二个 SourceManager
        MockMediaSource.reset_counters()
        manager2 = SourceManager(
            path='/test/video2.mp4',
            proxy_config=DEFAULT_PROXY_CONFIG,
            source_factory=mock_factory
        )
        success2 = manager2.open()
        assert success2 is True
        source2 = manager2._master_source
        assert isinstance(source2, MockMediaSource)
        assert source2.open_count == 1

        # 清理
        manager1.close()
        manager2.close()


# =============================================================================
# Integration Tests（使用 source_factory 绕过路径验证）
# =============================================================================

class TestIntegration:
    """集成测试"""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """每个测试前重置计数器"""
        MockMediaSource.reset_counters()

    def test_video_decoder_adapter_pattern(self) -> None:
        """VideoDecoderAdapter 模式：create() + open()"""
        # 模拟 VideoDecoderAdapter 的行为：
        # 1. create() 返回未打开的 source
        # 2. 显式调用 open()
        source = MockMediaSource('/test/video.mp4')
        assert source.is_open is False

        # 显式 open
        source.open()
        assert source.is_open is True
        assert source.open_count == 1

        # 关闭
        source.close()

    def test_video_frame_node_pattern(self) -> None:
        """VideoFrameNode 模式：create() 然后 open()"""
        # 注册 mock backend（P3.C: 签名需要包含 preserve_planes 参数）
        def create_mock(path: str, target_size: Optional[tuple[int, int]], preserve_planes: bool = False) -> MockMediaSource:
            return MockMediaSource(path, target_size)
        MediaSourceFactory.register_backend('mock_node', create_mock, lambda: True)

        # 模拟 VideoFrameNode 的行为：create() 然后 open()
        source = MediaSourceFactory._create_with_backend('mock_node', '/test/video.mp4', None)
        assert source.is_open is False

        # 显式 open
        source.open()
        assert source.is_open is True
        assert source.open_count == 1

        # 关闭
        source.close()


# =============================================================================
# 运行测试
# =============================================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v'])