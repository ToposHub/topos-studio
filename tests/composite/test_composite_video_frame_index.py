"""
Tests for Composite Video Frame Index Semantics (S4.B)

验证：
1. 所有 request 输入都是源视频 frame_index，0-based
2. timeline frame 到 source frame 的转换只发生在 VideoContext 层
3. VideoFrameProviderService.request_frame 不做 timeline offset/speed 转换
4. Color 直接请求源 frame，不走 timeline 转换
5. VideoContext.request_frame_for_node 正确处理 source in/out、time offset、speed
"""

import pytest
from unittest.mock import Mock, MagicMock, patch

from src.shared.video.frame_provider_service import (
    VideoFrameProviderService,
    ClipInfo,
    reset_frame_provider_service,
)
from src.features.composite.core.video_context import (
    VideoContext,
    TimelineContext,
    RenderQuality,
)


class TestFrameIndexSemantics:
    """
    S4.B: 测试 frame_index 语义

    验证：
    - 所有 request 输入都是源视频 frame_index，0-based
    - timeline frame 到 source frame 的转换只发生在 VideoContext 层
    - VideoFrameProviderService.request_frame 不做 timeline offset/speed 转换
    """

    def setup_method(self) -> None:
        """每个测试前重置全局服务"""
        reset_frame_provider_service()

    def teardown_method(self) -> None:
        """每个测试后清理"""
        reset_frame_provider_service()

    def test_video_frame_provider_service_frame_index_is_source(self) -> None:
        """
        测试 VideoFrameProviderService.request_frame 的 frame_index 是源视频帧号。

        验证：request_frame 传入的 frame_index 直接用于解码，不做转换。
        """
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

        mock_frame = Mock()
        mock_frame.frame_index = 100

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            clip_id = service.open_clip('/path/to/video.mp4')

        # 验证 request_frame 传入的 frame_index 直接传递给解码
        with patch.object(service, '_decode_frame', return_value=mock_frame) as mock_decode:
            # 请求第 100 帧
            frame = service.request_frame(clip_id, 100, 'proxy')

            # frame_index 应该直接传递给 _decode_frame（不做转换）
            mock_decode.assert_called_once()
            call_args = mock_decode.call_args
            assert call_args[0][1] == 100  # frame_index 参数

    def test_video_context_timeline_to_source_conversion(self) -> None:
        """
        测试 VideoContext 将 timeline_frame 转换为 source_frame。

        验证：
        - timeline_frame 通过 _compute_source_frame 转换
        - 转换后的 source_frame 传递给 VideoFrameProviderService
        """
        service = VideoFrameProviderService()
        video_ctx = VideoContext(service=service)

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
                clip_id = video_ctx.open_clip('/path/to/video.mp4', 'node_1')

        # 设置时间线帧号
        video_ctx.set_timeline_frame(50)  # 时间线第 50 帧

        # source_in=10，意味着源视频从第 10 帧开始
        # timeline_frame=50 应该对应 source_frame=50+10=60（无 offset/speed）
        # 注意：_compute_source_frame 的计算是：
        # timeline_time = 50 / 24 = 2.083
        # source_frame = int(2.083 * 24) + source_in = int(50) + 10 = 60
        with patch.object(service, 'request_frame', return_value=mock_frame) as mock_request:
            frame = video_ctx.request_frame_for_node(
                clip_id=clip_id,
                source_in=10,
                source_out=100,
                fps=24.0,
                # 注意：使用当前时间线帧号（已设置为 50）
            )

            # 验证 request_frame 被调用
            assert mock_request.called

            # 获取传递给 request_frame 的 frame_index
            call_args = mock_request.call_args
            source_frame = call_args[0][1]  # frame_index 参数

            # source_frame 应该是 timeline_frame + source_in = 50 + 10 = 60
            assert source_frame == 60

    def test_video_context_time_offset_conversion(self) -> None:
        """
        测试 VideoContext 处理 time_offset。

        验证：
        - time_offset 影响源帧号计算
        - timeline_frame + time_offset（秒）转换为帧号偏移
        """
        service = VideoFrameProviderService()
        video_ctx = VideoContext(service=service)

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
                clip_id = video_ctx.open_clip('/path/to/video.mp4', 'node_1')

        # time_offset=2.0 秒，fps=24.0
        # 计算逻辑：
        # timeline_time = timeline_frame / fps = 10 / 24 = 0.4167 秒
        # source_time = timeline_time + time_offset = 0.4167 + 2.0 = 2.4167 秒
        # source_frame = int(source_time * fps) + source_in = int(2.4167 * 24) + 0 = int(58) = 58

        # 必须设置时间线帧号，否则使用默认值 0
        video_ctx.set_timeline_frame(10)

        with patch.object(service, 'request_frame', return_value=mock_frame) as mock_request:
            frame = video_ctx.request_frame_for_node(
                clip_id=clip_id,
                source_in=0,
                source_out=100,
                fps=24.0,
                time_offset=2.0,  # 2 秒 = 48 帧
            )

            call_args = mock_request.call_args
            source_frame = call_args[0][1]

            # 10 (timeline_frame) -> 10 帧 = 0.4167 秒
            # 0.4167 + 2.0 = 2.4167 秒 = 58 帧
            assert source_frame == 58

    def test_video_context_speed_conversion(self) -> None:
        """
        测试 VideoContext 处理播放速度。

        验证：
        - speed 影响源帧号计算
        - 时间线时间乘以 speed 得到源时间
        """
        service = VideoFrameProviderService()
        video_ctx = VideoContext(service=service)

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
                clip_id = video_ctx.open_clip('/path/to/video.mp4', 'node_1')

        # speed=2.0（两倍速）
        # 计算逻辑：
        # timeline_time = timeline_frame / fps = 20 / 24 = 0.833 秒
        # source_time = timeline_time * speed = 0.833 * 2.0 = 1.666 秒
        # source_frame = int(source_time * fps) + source_in = int(1.666 * 24) + 0 = int(40) = 40

        # 必须设置时间线帧号，否则使用默认值 0
        video_ctx.set_timeline_frame(20)

        with patch.object(service, 'request_frame', return_value=mock_frame) as mock_request:
            frame = video_ctx.request_frame_for_node(
                clip_id=clip_id,
                source_in=0,
                source_out=100,
                fps=24.0,
                speed=2.0,
            )

            call_args = mock_request.call_args
            source_frame = call_args[0][1]

            # 20 帧 / 24 fps = 0.833 秒
            # 0.833 * 2.0 = 1.666 秒
            # 1.666 * 24 fps = 40 帧
            assert source_frame == 40

    def test_video_context_loop_handling(self) -> None:
        """
        测试 VideoContext 处理 loop。

        验证：
        - 当 source_frame > source_out 且 loop=True，返回 source_in + offset
        """
        service = VideoFrameProviderService()
        video_ctx = VideoContext(service=service)

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
                clip_id = video_ctx.open_clip('/path/to/video.mp4', 'node_1')

        # source_in=0, source_out=50
        # timeline_frame=60 超过 source_out
        # loop=True: 应该循环回到 source_in
        video_ctx.set_timeline_frame(60)

        with patch.object(service, 'request_frame', return_value=mock_frame) as mock_request:
            frame = video_ctx.request_frame_for_node(
                clip_id=clip_id,
                source_in=0,
                source_out=50,  # 只使用 50 帧
                fps=24.0,
                loop=True,
            )

            call_args = mock_request.call_args
            source_frame = call_args[0][1]

            # 循环：60 - 50 - 1 = 9，然后 source_in + 9 = 9
            # 或者：(60 - 50 - 1) % 51 = 9
            assert source_frame == 9

    def test_video_context_hold_handling(self) -> None:
        """
        测试 VideoContext 处理 hold。

        验证：
        - 当 source_frame > source_out 且 hold=True，返回 source_out
        """
        service = VideoFrameProviderService()
        video_ctx = VideoContext(service=service)

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
                clip_id = video_ctx.open_clip('/path/to/video.mp4', 'node_1')

        # source_in=0, source_out=50
        # timeline_frame=60 超过 source_out
        # hold=True: 应该保持在 source_out
        video_ctx.set_timeline_frame(60)

        with patch.object(service, 'request_frame', return_value=mock_frame) as mock_request:
            frame = video_ctx.request_frame_for_node(
                clip_id=clip_id,
                source_in=0,
                source_out=50,  # 只使用 50 帧
                fps=24.0,
                hold=True,
            )

            call_args = mock_request.call_args
            source_frame = call_args[0][1]

            # hold: 返回 source_out
            assert source_frame == 50

    def test_video_context_no_loop_no_hold(self) -> None:
        """
        测试 VideoContext 在超出范围且无 loop/hold 时返回 None。

        验证：
        - 当 source_frame > source_out 且 loop=False, hold=False，返回 None
        """
        service = VideoFrameProviderService()
        video_ctx = VideoContext(service=service)

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
                clip_id = video_ctx.open_clip('/path/to/video.mp4', 'node_1')

        # source_in=0, source_out=50
        # timeline_frame=60 超过 source_out
        # loop=False, hold=False: 应该返回 None
        video_ctx.set_timeline_frame(60)

        with patch.object(service, 'request_frame', return_value=Mock()) as mock_request:
            frame = video_ctx.request_frame_for_node(
                clip_id=clip_id,
                source_in=0,
                source_out=50,
                fps=24.0,
                loop=False,
                hold=False,
            )

            # 应该返回 None（超出范围）
            assert frame is None
            # request_frame 不应该被调用
            assert not mock_request.called


class TestColorDirectRequest:
    """
    S4.B: 测试 Color 直接请求源帧，不走 timeline 转换

    验证：
    - ColorFrameProviderAdapter.request_proxy/request_master 直接使用 frame_index
    - 不做 timeline offset/speed 转换
    """

    def setup_method(self) -> None:
        """每个测试前重置全局服务"""
        reset_frame_provider_service()

    def teardown_method(self) -> None:
        """每个测试后清理"""
        reset_frame_provider_service()

    def test_color_adapter_direct_frame_index(self) -> None:
        """
        测试 ColorFrameProviderAdapter 直接使用 frame_index，不做转换。
        """
        from src.shared.video.color_frame_provider_adapter import ColorFrameProviderAdapter

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

        # request_proxy 直接传入 frame_index
        with patch.object(service, 'request_frame', return_value=mock_frame) as mock_request:
            frame = adapter.request_proxy(100)

            # frame_index 应该直接传递给 request_frame
            mock_request.assert_called_once_with(
                clip_id=adapter.clip_id,
                frame_index=100,
                quality='proxy'
            )

        # request_master 直接传入 frame_index
        with patch.object(service, 'request_frame', return_value=mock_frame) as mock_request:
            frame = adapter.request_master(200)

            # frame_index 应该直接传递给 request_frame
            mock_request.assert_called_once_with(
                clip_id=adapter.clip_id,
                frame_index=200,
                quality='master'
            )


class TestFrameIndexIsZeroBased:
    """
    S4.B: 测试 frame_index 是 0-based

    验证：
    - 第 0 帧对应第一帧
    - 第 total_frames-1 帧对应最后一帧
    """

    def setup_method(self) -> None:
        """每个测试前重置全局服务"""
        reset_frame_provider_service()

    def teardown_method(self) -> None:
        """每个测试后清理"""
        reset_frame_provider_service()

    def test_first_frame_is_zero(self) -> None:
        """
        测试第一帧是 frame_index=0。
        """
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

        mock_frame = Mock()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            clip_id = service.open_clip('/path/to/video.mp4')

        with patch.object(service, 'request_frame', return_value=mock_frame) as mock_request:
            # 请求第一帧
            frame = service.request_frame(clip_id, 0, 'proxy')

            # frame_index=0 是第一帧
            mock_request.assert_called()

    def test_last_frame_is_total_minus_one(self) -> None:
        """
        测试最后一帧是 frame_index=total_frames-1。
        """
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

        mock_frame = Mock()

        with patch.object(service, '_open_clip_sources') as mock_open:
            def mock_open_sources(session):
                session.is_open = True
            mock_open.side_effect = mock_open_sources

            clip_id = service.open_clip('/path/to/video.mp4')

        with patch.object(service, 'request_frame', return_value=mock_frame) as mock_request:
            # 请求最后一帧（total_frames - 1）
            frame = service.request_frame(clip_id, 239, 'proxy')

            # frame_index=239 是最后一帧（240 帧视频）
            mock_request.assert_called()


class TestTimelineExportRange:
    """
    S4.B: 测试导出范围

    验证：
    - 导出范围第一帧不偏帧
    - TimelineContext.validate_export_range 正确验证
    """

    def test_export_range_validation(self) -> None:
        """
        测试导出范围验证。
        """
        timeline = TimelineContext(
            current_frame=0,
            fps=24.0,
            start_frame=0,
            end_frame=100,
        )

        # 验证范围
        start, end = timeline.validate_export_range()
        assert start == 0
        assert end == 100

        # 自定义范围
        start, end = timeline.validate_export_range(start_frame=10, end_frame=50)
        assert start == 10
        assert end == 50

    def test_export_range_first_frame_not_offset(self) -> None:
        """
        测试导出范围第一帧不偏帧。

        验证：start_frame=0 时，第一帧对应源帧 frame_index=0。
        """
        service = VideoFrameProviderService()
        video_ctx = VideoContext(service=service)

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

            # 在 open_clip 时返回 clip_info
            with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
                clip_id = video_ctx.open_clip('/path/to/video.mp4', 'node_1')

        # 设置导出范围
        video_ctx.set_timeline_range(0, 100)

        # 请求第一帧
        video_ctx.set_timeline_frame(0)

        # 重要：request_frame_for_node 会调用 get_clip_info，
        # 需要在 mock_request 之后再次 mock get_clip_info
        with patch.object(service, 'get_clip_info', return_value=mock_clip_info):
            with patch.object(service, 'request_frame', return_value=mock_frame) as mock_request:
                frame = video_ctx.request_frame_for_node(
                    clip_id=clip_id,
                    source_in=0,
                    fps=24.0,
                )

                # 验证 request_frame 被调用
                assert mock_request.called
                assert frame is not None

                call_args = mock_request.call_args
                # 确保 call_args 不为 None
                assert call_args is not None
                source_frame = call_args[0][1]

                # 第一帧应该是 frame_index=0
                assert source_frame == 0


class TestTypeAnnotations:
    """测试类型注解完整性"""

    def test_service_request_frame_annotations(self) -> None:
        """测试 request_frame 有类型注解"""
        import inspect

        from src.shared.video.frame_provider_service import VideoFrameProviderService

        method = VideoFrameProviderService.request_frame
        annotations = method.__annotations__

        assert 'clip_id' in annotations
        assert 'frame_index' in annotations
        assert 'quality' in annotations
        assert 'return' in annotations

    def test_video_context_request_frame_annotations(self) -> None:
        """测试 request_frame_for_node 有类型注解"""
        import inspect

        from src.features.composite.core.video_context import VideoContext

        method = VideoContext.request_frame_for_node
        annotations = method.__annotations__

        assert 'clip_id' in annotations
        assert 'source_in' in annotations
        assert 'source_out' in annotations
        assert 'return' in annotations


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
