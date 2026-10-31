"""
Tests for ColorFrameProviderAdapter (S4.A)

验证：
1. ColorFrameProviderAdapter 正确包装 VideoFrameProviderService
2. 同一视频在 Color/Composite 同时打开时共享 source/cache
3. 引用计数正确（Color adapter + Composite service 共享同一 clip）
4. proxy/master 请求正确
5. 所有接口有完整类型注解
6. frame_index 语义正确（源视频 0-based）
"""

import pytest
from unittest.mock import Mock, MagicMock, patch

from src.shared.video.color_frame_provider_adapter import (
    ColorFrameProviderAdapter,
    _ClipInfoMetadataAdapter,
    _RationalAdapter,
)
from src.shared.video.frame_provider_service import (
    VideoFrameProviderService,
    ClipSession,
    ClipInfo,
    FrameQuality,
    get_frame_provider_service,
    reset_frame_provider_service,
)


class TestColorFrameProviderAdapter:
    """测试 ColorFrameProviderAdapter"""

    def setup_method(self) -> None:
        """每个测试前重置全局服务"""
        reset_frame_provider_service()

    def teardown_method(self) -> None:
        """每个测试后清理"""
        reset_frame_provider_service()

    def test_initialization(self) -> None:
        """测试初始化"""
        adapter = ColorFrameProviderAdapter()
        assert not adapter.is_open
        assert adapter.path is None
        assert adapter.clip_id is None
        assert adapter.clip_info is None
        assert adapter.fps is None
        assert adapter.total_frames is None
        assert adapter.duration is None

    def test_initialization_with_service(self) -> None:
        """测试使用自定义服务初始化"""
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)
        assert adapter._service is service

    def test_initialization_with_global_service(self) -> None:
        """测试使用全局服务初始化"""
        adapter = ColorFrameProviderAdapter()
        global_service = get_frame_provider_service()
        assert adapter._service is global_service

    def test_open_success(self) -> None:
        """测试成功打开视频"""
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)

        # Mock _open_clip_sources 和 get_clip_info
        mock_clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
        )

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                result = adapter.open('/path/to/video.mp4')

        assert result is True
        assert adapter.is_open
        assert adapter.path == '/path/to/video.mp4'
        assert adapter.clip_id is not None
        assert adapter.clip_info == mock_clip_info
        assert adapter.fps == 24.0
        assert adapter.total_frames == 240
        assert adapter.duration == 10.0
        assert adapter.width == 1920
        assert adapter.height == 1080

    def test_open_already_open(self) -> None:
        """测试重复打开"""
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)

        mock_clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
        )

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                # 第一次打开
                result1 = adapter.open('/path/to/video.mp4')
                assert result1 is True

                # 第二次打开（应该失败）
                result2 = adapter.open('/path/to/other.mp4')
                assert result2 is False

                # 路径应该保持第一次的
                assert adapter.path == '/path/to/video.mp4'

    def test_open_failure(self) -> None:
        """测试打开失败"""
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)

        # Mock open_clip 抛出异常
        with patch.object(service, 'open_clip', side_effect=Exception("Failed to open")):
            result = adapter.open('/path/to/video.mp4')

        assert result is False
        assert not adapter.is_open
        assert adapter.path is None

    def test_open_clip_info_none(self) -> None:
        """测试打开后 get_clip_info 返回 None"""
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            # get_clip_info 返回 None
            with patch.object(service, 'get_clip_info', return_value=None):
                with patch.object(service, 'close_clip') as mock_close:
                    result = adapter.open('/path/to/video.mp4')

        assert result is False
        assert not adapter.is_open
        # 应该调用 close_clip
        assert mock_close.called

    def test_close(self) -> None:
        """测试关闭"""
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)

        mock_clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
        )

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                adapter.open('/path/to/video.mp4')

        with patch.object(service, 'close_clip') as mock_close:
            adapter.close()

        assert not adapter.is_open
        assert adapter.path is None
        assert adapter.clip_id is None
        assert adapter.clip_info is None
        assert mock_close.called

    def test_close_not_open(self) -> None:
        """测试关闭未打开的 adapter"""
        adapter = ColorFrameProviderAdapter()
        # 应该不抛异常
        adapter.close()
        assert not adapter.is_open

    def test_request_proxy(self) -> None:
        """测试请求 proxy 帧"""
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)

        mock_clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
        )

        mock_frame = Mock()
        mock_frame.width = 960
        mock_frame.height = 540
        mock_frame.frame_index = 0

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                adapter.open('/path/to/video.mp4')

        with patch.object(service, 'request_frame', return_value=mock_frame) as mock_request:
            frame = adapter.request_proxy(0)

        assert frame == mock_frame
        assert mock_request.called
        mock_request.assert_called_once_with(
            clip_id=adapter.clip_id,
            frame_index=0,
            quality='proxy'
        )
        assert adapter._total_proxy_requests == 1

    def test_request_proxy_not_open(self) -> None:
        """测试未打开时请求 proxy 帧"""
        adapter = ColorFrameProviderAdapter()
        frame = adapter.request_proxy(0)
        assert frame is None

    def test_request_master(self) -> None:
        """测试请求 master 帧"""
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)

        mock_clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
        )

        mock_frame = Mock()
        mock_frame.width = 1920
        mock_frame.height = 1080
        mock_frame.frame_index = 100

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                adapter.open('/path/to/video.mp4')

        with patch.object(service, 'request_frame', return_value=mock_frame) as mock_request:
            frame = adapter.request_master(100, timeout_ms=300)

        assert frame == mock_frame
        assert mock_request.called
        mock_request.assert_called_once_with(
            clip_id=adapter.clip_id,
            frame_index=100,
            quality='master'
        )
        assert adapter._total_master_requests == 1

    def test_request_master_not_open(self) -> None:
        """测试未打开时请求 master 帧"""
        adapter = ColorFrameProviderAdapter()
        frame = adapter.request_master(100, timeout_ms=300)
        assert frame is None

    def test_prefetch_proxy(self) -> None:
        """测试预取 proxy 帧"""
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)

        mock_clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
        )

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                adapter.open('/path/to/video.mp4')

        with patch.object(service, 'prefetch') as mock_prefetch:
            adapter.prefetch_proxy(0, 10)

        assert mock_prefetch.called
        mock_prefetch.assert_called_once_with(
            clip_id=adapter.clip_id,
            start=0,
            end=10,
            quality='proxy'
        )

    def test_prefetch_master(self) -> None:
        """测试预取 master 帧"""
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)

        mock_clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
        )

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                adapter.open('/path/to/video.mp4')

        with patch.object(service, 'prefetch') as mock_prefetch:
            adapter.prefetch_master(0, 5)

        assert mock_prefetch.called
        mock_prefetch.assert_called_once_with(
            clip_id=adapter.clip_id,
            start=0,
            end=5,
            quality='master'
        )

    def test_get_stats(self) -> None:
        """测试获取统计信息"""
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)

        mock_clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
        )

        mock_frame = Mock()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                adapter.open('/path/to/video.mp4')

        with patch.object(service, 'request_frame', return_value=mock_frame):
            adapter.request_proxy(0)
            adapter.request_proxy(1)
            adapter.request_master(100)

        stats = adapter.get_stats()

        assert stats['is_open'] is True
        assert stats['path'] == '/path/to/video.mp4'
        assert stats['clip_id'] == adapter.clip_id
        assert stats['total_proxy_requests'] == 2
        assert stats['total_master_requests'] == 1

    def test_metadata_adapter(self) -> None:
        """测试 metadata 属性（VideoMetadata-like）"""
        adapter = ColorFrameProviderAdapter()

        # 未打开时 metadata 为 None
        assert adapter.metadata is None

        # 打开后测试 metadata
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)

        mock_clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
            bit_depth=10,
            is_hdr=True,
            codec_name='h265',
            color_primaries='bt2020',
            color_transfer='pq',
            color_matrix='bt2020nc',
        )

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                adapter.open('/path/to/video.mp4')

        metadata = adapter.metadata
        assert metadata is not None
        assert metadata.width == 1920
        assert metadata.height == 1080
        assert metadata.total_frames == 240
        assert metadata.duration == 10.0
        assert metadata.bit_depth == 10
        assert metadata.is_hdr is True
        assert metadata.codec_name == 'h265'
        assert metadata.color_primaries == 'bt2020'
        assert metadata.color_transfer == 'pq'
        assert metadata.color_matrix == 'bt2020nc'

        # fps 是 RationalAdapter
        fps_rational = metadata.fps
        assert fps_rational.to_fps() == 24.0
        assert fps_rational.to_float() == 24.0


class TestSharedClipReferenceCount:
    """
    S4.A 验收：测试引用计数

    验证：
    - Color adapter open 一个 path
    - Composite service open 同 path
    - VideoFrameProviderService.active_clip_count == 1
    """

    def setup_method(self) -> None:
        """每个测试前重置全局服务"""
        reset_frame_provider_service()

    def teardown_method(self) -> None:
        """每个测试后清理"""
        reset_frame_provider_service()

    def test_shared_clip_color_and_composite(self) -> None:
        """测试 Color adapter + Composite VideoContext 共享同一 clip"""
        service = VideoFrameProviderService()

        # Color adapter
        color_adapter = ColorFrameProviderAdapter(service=service)

        mock_clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
        )

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                # Color adapter 打开
                color_adapter.open('/path/to/video.mp4')

        # 验证只有一个 active clip
        assert service.active_clip_count == 1
        color_clip_id = color_adapter.clip_id

        # Composite VideoContext 打开同一文件
        from src.features.composite.core.video_context import VideoContext
        video_ctx = VideoContext(service=service)

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                composite_clip_id = video_ctx.open_clip('/path/to/video.mp4', 'node_1')

        # 应该返回相同的 clip_id
        assert composite_clip_id == color_clip_id

        # 引用计数应该为 2（Color + Composite）
        session = service.get_clip_session(color_clip_id)
        assert session is not None
        assert session.ref_count == 2

        # active_clip_count 应该仍为 1（共享）
        assert service.active_clip_count == 1

        # Color 关闭
        color_adapter.close()

        # 引用计数应该为 1
        session = service.get_clip_session(color_clip_id)
        assert session is not None
        assert session.ref_count == 1

        # active_clip_count 应该仍为 1
        assert service.active_clip_count == 1

        # Composite 关闭
        video_ctx.close_clip_for_node('node_1')

        # 引用计数应该为 0
        session = service.get_clip_session(color_clip_id)
        assert session is None  # 已清理

        # active_clip_count 应该为 0
        assert service.active_clip_count == 0

    def test_shared_clip_multiple_color_adapters(self) -> None:
        """测试多个 Color adapter 共享同一 clip"""
        service = VideoFrameProviderService()

        mock_clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
        )

        # 第一个 adapter
        adapter1 = ColorFrameProviderAdapter(service=service)
        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                adapter1.open('/path/to/video.mp4')

        clip_id1 = adapter1.clip_id
        assert service.active_clip_count == 1

        # 第二个 adapter（应该共享）
        adapter2 = ColorFrameProviderAdapter(service=service)
        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                adapter2.open('/path/to/video.mp4')

        clip_id2 = adapter2.clip_id

        # 应该共享相同的 clip_id
        assert clip_id1 == clip_id2
        assert service.active_clip_count == 1

        # 引用计数应该为 2
        session = service.get_clip_session(clip_id1)
        assert session is not None
        assert session.ref_count == 2

        # 第一个关闭
        adapter1.close()
        session = service.get_clip_session(clip_id1)
        assert session is not None
        assert session.ref_count == 1
        assert service.active_clip_count == 1

        # 第二个关闭
        adapter2.close()
        session = service.get_clip_session(clip_id1)
        assert session is None
        assert service.active_clip_count == 0


class TestFrameIndexSemantics:
    """
    S4.B 验证：frame_index 语义

    验证：
    - 所有 request 输入都是源视频 frame_index，0-based
    - Color adapter 不做 timeline 转换
    """

    def setup_method(self) -> None:
        """每个测试前重置全局服务"""
        reset_frame_provider_service()

    def teardown_method(self) -> None:
        """每个测试后清理"""
        reset_frame_provider_service()

    def test_request_proxy_frame_index_0_based(self) -> None:
        """测试 request_proxy 使用 0-based frame_index"""
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)

        mock_clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
        )

        mock_frame = Mock()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                adapter.open('/path/to/video.mp4')

        with patch.object(service, 'request_frame', return_value=mock_frame) as mock_request:
            # 请求第 0 帧
            adapter.request_proxy(0)
            mock_request.assert_called_with(
                clip_id=adapter.clip_id,
                frame_index=0,
                quality='proxy'
            )

            # 请求第 100 帧
            adapter.request_proxy(100)
            mock_request.assert_called_with(
                clip_id=adapter.clip_id,
                frame_index=100,
                quality='proxy'
            )

    def test_request_master_frame_index_0_based(self) -> None:
        """测试 request_master 使用 0-based frame_index"""
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)

        mock_clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
        )

        mock_frame = Mock()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                adapter.open('/path/to/video.mp4')

        with patch.object(service, 'request_frame', return_value=mock_frame) as mock_request:
            # 请求第 0 帧
            adapter.request_master(0)
            mock_request.assert_called_with(
                clip_id=adapter.clip_id,
                frame_index=0,
                quality='master'
            )

            # 请求第 239 帧（最后一帧）
            adapter.request_master(239)
            mock_request.assert_called_with(
                clip_id=adapter.clip_id,
                frame_index=239,
                quality='master'
            )

    def test_prefetch_frame_index_0_based(self) -> None:
        """测试 prefetch 使用 0-based frame_index"""
        service = VideoFrameProviderService()
        adapter = ColorFrameProviderAdapter(service=service)

        mock_clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
        )

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                adapter.open('/path/to/video.mp4')

        with patch.object(service, 'prefetch') as mock_prefetch:
            # 预取第 0-10 帧
            adapter.prefetch_proxy(0, 10)
            mock_prefetch.assert_called_with(
                clip_id=adapter.clip_id,
                start=0,
                end=10,
                quality='proxy'
            )


class TestClipInfoMetadataAdapter:
    """测试 ClipInfo 到 VideoMetadata 的适配器"""

    def test_adapter_properties(self) -> None:
        """测试适配器属性"""
        clip_info = ClipInfo(
            clip_id='test_clip',
            path='/path/to/video.mp4',
            width=1920,
            height=1080,
            fps=24.0,
            total_frames=240,
            duration=10.0,
            bit_depth=10,
            is_hdr=True,
            codec_name='h265',
            color_primaries='bt2020',
            color_transfer='pq',
            color_matrix='bt2020nc',
        )

        adapter = _ClipInfoMetadataAdapter(clip_info)

        assert adapter.width == 1920
        assert adapter.height == 1080
        assert adapter.total_frames == 240
        assert adapter.duration == 10.0
        assert adapter.bit_depth == 10
        assert adapter.is_hdr is True
        assert adapter.codec_name == 'h265'
        assert adapter.color_primaries == 'bt2020'
        assert adapter.color_transfer == 'pq'
        assert adapter.color_matrix == 'bt2020nc'

    def test_rational_adapter(self) -> None:
        """测试 RationalAdapter"""
        rational = _RationalAdapter(24.0)
        assert rational.to_fps() == 24.0
        assert rational.to_float() == 24.0


class TestTypeAnnotations:
    """测试类型注解完整性"""

    def test_adapter_methods_have_annotations(self) -> None:
        """测试 adapter 方法有类型注解"""
        import inspect

        adapter_class = ColorFrameProviderAdapter
        methods_to_check = [
            'open',
            'close',
            'request_proxy',
            'request_master',
            'prefetch_proxy',
            'prefetch_master',
            'get_stats',
        ]

        for method_name in methods_to_check:
            method = getattr(adapter_class, method_name)
            annotations = method.__annotations__

            # 应该有返回类型注解
            assert 'return' in annotations, f"{method_name} missing return annotation"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
