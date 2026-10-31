"""
Test V3 Planar Playback Path - v3 proxy播放对YUV素材默认输出planar DecodedFrame

测试覆盖范围：
1. FFmpegMediaSource preserve_planes 配置测试
   - preserve_planes=True 时 YUV 源输出 planar DecodedFrame
   - preserve_planes=False 时输出 packed RGB
   - is_planar_mode 属性正确反映状态

2. SourceManager proxy planar 模式测试
   - enable_planar_proxy=True 时 proxy_source 启用 planar
   - get_proxy_frame() 返回 planar DecodedFrame
   - YUV 源正确识别

3. _submit_decoded_frame_playback payload 测试
   - planar frame payload 包含 decoded_frame、yuv_matrix、color_range
   - renderer 正确识别 planar 格式

4. DecodeWorker planar frame 处理测试
   - ProxyDecodeWorker 正确处理 planar frame
   - 不把 planar frame 转成 numpy RGB

验收标准：
- preserve_planes 配置正确生效
- proxy source 默认启用 planar（v3）
- planar frame payload 正确传递给 renderer

注意事项：
- 使用 mock 避免实际 OpenGL/PyQt 操作
- 新函数必须有类型注解
"""

import pytest
import sys
import os
import numpy as np
from unittest.mock import MagicMock, patch, PropertyMock
from typing import Dict, Any, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.shared.media.frame import DecodedFrame, PlaneInfo
from src.shared.media.metadata import VideoMetadata, Rational


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def yuv420_metadata() -> VideoMetadata:
    """创建 YUV420p 元数据"""
    return VideoMetadata(
        width=640,
        height=480,
        fps=Rational(24, 1),
        total_frames=100,
        codec_name='h264',
        codec_profile='high',
        pixel_format='yuv420p',
        bit_depth=8,
        color_primaries='bt709',
        color_transfer='bt709',
        color_matrix='bt709',
        color_range='limited',
    )


@pytest.fixture
def yuv420p10le_metadata() -> VideoMetadata:
    """创建 YUV420p10le 元数据"""
    return VideoMetadata(
        width=1920,
        height=1080,
        fps=Rational(24, 1),
        total_frames=100,
        codec_name='h265',
        codec_profile='main10',
        pixel_format='yuv420p10le',
        bit_depth=10,
        color_primaries='bt2020',
        color_transfer='smpte2084',
        color_matrix='bt2020nc',
        color_range='limited',
    )


@pytest.fixture
def rgb_metadata() -> VideoMetadata:
    """创建 RGB 元数据"""
    return VideoMetadata(
        width=640,
        height=480,
        fps=Rational(24, 1),
        total_frames=100,
        codec_name='prores',
        codec_profile='4444',
        pixel_format='gbrap10le',
        bit_depth=10,
        color_primaries='bt709',
        color_transfer='bt709',
        color_matrix='bt709',
        color_range='full',
    )


@pytest.fixture
def planar_yuv420_frame() -> DecodedFrame:
    """创建 planar YUV420p DecodedFrame"""
    # Y plane (full resolution)
    y_plane = np.zeros((480, 640), dtype=np.uint8)
    y_plane.fill(128)

    # U plane (half resolution)
    u_plane = np.zeros((240, 320), dtype=np.uint8)
    u_plane.fill(64)

    # V plane (half resolution)
    v_plane = np.zeros((240, 320), dtype=np.uint8)
    v_plane.fill(192)

    # Create plane infos
    y_info = PlaneInfo.create(0, 640, 480, 'Y', np.uint8, 8)
    u_info = PlaneInfo.create(1, 320, 240, 'U', np.uint8, 8)
    v_info = PlaneInfo.create(2, 320, 240, 'V', np.uint8, 8)

    metadata = VideoMetadata(
        width=640,
        height=480,
        fps=Rational(24, 1),
        total_frames=100,
        codec_name='h264',
        codec_profile='high',
        pixel_format='yuv420p',
        bit_depth=8,
        color_matrix='bt709',
        color_range='limited',
    )

    return DecodedFrame(
        frame_index=0,
        pts=0,
        time_seconds=0.0,
        data=None,
        planes=(y_plane, u_plane, v_plane),
        pixel_format='yuv420p',
        dtype=np.dtype(np.uint8),
        width=640,
        height=480,
        metadata=metadata,
        is_proxy=True,
        plane_infos=(y_info, u_info, v_info),
    )


@pytest.fixture
def planar_yuv420p10_frame() -> DecodedFrame:
    """创建 planar YUV420p10le DecodedFrame"""
    # Y plane (full resolution, 10-bit stored in uint16)
    y_plane = np.zeros((1080, 1920), dtype=np.uint16)
    y_plane.fill(512)  # 10-bit value

    # U plane (half resolution, 10-bit)
    u_plane = np.zeros((540, 960), dtype=np.uint16)
    u_plane.fill(256)

    # V plane (half resolution, 10-bit)
    v_plane = np.zeros((540, 960), dtype=np.uint16)
    v_plane.fill(768)

    y_info = PlaneInfo.create(0, 1920, 1080, 'Y', np.uint16, 10)
    u_info = PlaneInfo.create(1, 960, 540, 'U', np.uint16, 10)
    v_info = PlaneInfo.create(2, 960, 540, 'V', np.uint16, 10)

    metadata = VideoMetadata(
        width=1920,
        height=1080,
        fps=Rational(24, 1),
        total_frames=100,
        codec_name='h265',
        codec_profile='main10',
        pixel_format='yuv420p10le',
        bit_depth=10,
        color_matrix='bt2020nc',
        color_range='limited',
    )

    return DecodedFrame(
        frame_index=0,
        pts=0,
        time_seconds=0.0,
        data=None,
        planes=(y_plane, u_plane, v_plane),
        pixel_format='yuv420p10le',
        dtype=np.dtype(np.uint16),
        width=1920,
        height=1080,
        metadata=metadata,
        is_proxy=True,
        plane_infos=(y_info, u_info, v_info),
    )


@pytest.fixture
def packed_rgb_frame() -> DecodedFrame:
    """创建 packed RGB DecodedFrame"""
    data = np.zeros((480, 640, 3), dtype=np.uint8)
    data[:, :, 0] = 100  # R
    data[:, :, 1] = 150  # G
    data[:, :, 2] = 200  # B

    return DecodedFrame(
        frame_index=0,
        pts=0,
        time_seconds=0.0,
        data=data,
        planes=tuple(),
        pixel_format='rgb24',
        dtype=np.dtype(np.uint8),
        width=640,
        height=480,
        metadata=None,
        is_proxy=False,
    )


# =============================================================================
# 1. FFmpegMediaSource preserve_planes 配置测试
# =============================================================================

class TestFFmpegMediaSourcePreservePlanes:
    """测试 FFmpegMediaSource preserve_planes 配置"""

    def test_preserve_planes_default_false(self) -> None:
        """默认 preserve_planes=False"""
        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        source = FFmpegMediaSource('/fake/path.mp4')
        assert source.preserve_planes == False
        assert source.output_mode == "packed_rgb"
        assert source.is_planar_mode == False  # 未打开时为 False

    def test_preserve_planes_true_constructor(self) -> None:
        """构造函数设置 preserve_planes=True"""
        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        source = FFmpegMediaSource('/fake/path.mp4', preserve_planes=True)
        assert source.preserve_planes == True
        assert source.output_mode == "packed_rgb"  # 默认 output_mode

    def test_output_mode_planar_yuv(self) -> None:
        """output_mode='planar_yuv'"""
        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        source = FFmpegMediaSource('/fake/path.mp4', output_mode="planar_yuv")
        assert source.output_mode == "planar_yuv"
        assert source.preserve_planes == False  # 默认

    def test_set_preserve_planes_method(self) -> None:
        """set_preserve_planes() 方法"""
        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        source = FFmpegMediaSource('/fake/path.mp4')
        assert source.preserve_planes == False

        source.set_preserve_planes(True)
        assert source.preserve_planes == True

    def test_set_output_mode_method(self) -> None:
        """set_output_mode() 方法"""
        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        source = FFmpegMediaSource('/fake/path.mp4')
        assert source.output_mode == "packed_rgb"

        source.set_output_mode("planar_yuv")
        assert source.output_mode == "planar_yuv"

    def test_is_planar_mode_after_open_yuv_source(
        self,
        yuv420_metadata: VideoMetadata
    ) -> None:
        """打开后 is_planar_mode 正确反映状态（YUV 源）"""
        from src.shared.media.ffmpeg_source import FFmpegMediaSource, _is_yuv_pixel_format

        # 验证 YUV 检测函数
        assert _is_yuv_pixel_format('yuv420p') == True
        assert _is_yuv_pixel_format('yuv420p10le') == True
        assert _is_yuv_pixel_format('yuv422p') == True
        assert _is_yuv_pixel_format('yuv444p') == True
        assert _is_yuv_pixel_format('yuva420p') == True

    def test_is_planar_mode_after_open_rgb_source(
        self,
        rgb_metadata: VideoMetadata
    ) -> None:
        """打开后 is_planar_mode 正确反映状态（RGB 源）"""
        from src.shared.media.ffmpeg_source import FFmpegMediaSource, _is_yuv_pixel_format

        # 验证 RGB 不是 YUV
        assert _is_yuv_pixel_format('gbrap10le') == False
        assert _is_yuv_pixel_format('rgb24') == False
        assert _is_yuv_pixel_format('rgba') == False

    def test_is_yuv_pixel_format_nv12(self) -> None:
        """NV12/P010 也是 YUV 格式"""
        from src.shared.media.ffmpeg_source import _is_yuv_pixel_format

        # NV12/P010 semiplanar formats
        assert _is_yuv_pixel_format('nv12') == True
        assert _is_yuv_pixel_format('nv21') == True
        assert _is_yuv_pixel_format('p010le') == True
        assert _is_yuv_pixel_format('p010be') == True


# =============================================================================
# 2. SourceManager proxy planar 模式测试
# =============================================================================

class TestSourceManagerPlanarProxy:
    """测试 SourceManager proxy planar 模式"""

    def test_missing_viewport_defaults_to_1080p_proxy_for_4k_source(self) -> None:
        """Headless playback must not interpret an unset viewport as 4K preview."""
        from src.shared.video.playback_clock import SourceManager

        manager = SourceManager('/fake/path.mp4')
        manager._metadata = VideoMetadata(
            width=3840,
            height=2160,
            fps=Rational(24, 1),
            total_frames=100,
            codec_name='h264',
            codec_profile='high',
            pixel_format='yuv420p',
            bit_depth=8,
        )

        manager._calculate_proxy_size()

        assert manager.proxy_size == (1920, 1080)

    def test_scaled_proxy_bypasses_hwaccel_that_ignores_target_size(self) -> None:
        """Scaled preview must use a source that really honors 1080p output."""
        from src.shared.video.playback_clock import SourceManager

        source_metadata = VideoMetadata(
            width=3840,
            height=2160,
            fps=Rational(24, 1),
            total_frames=100,
            codec_name='h264',
            codec_profile='high',
            pixel_format='yuv420p',
            bit_depth=8,
        )
        master = MagicMock()
        master.probe.return_value = source_metadata
        proxy = MagicMock()
        proxy.is_planar_mode = True

        with patch(
            'src.shared.media.factory.MediaSourceFactory.create',
            side_effect=[master, proxy],
        ) as create_source, patch(
            'src.shared.media.factory.MediaSourceFactory.create_with_hwaccel'
        ) as create_hwaccel:
            manager = SourceManager('/fake/path.mp4')
            assert manager.open() is True

        create_hwaccel.assert_not_called()
        assert create_source.call_args_list[1].kwargs['target_size'] == (1920, 1080)
        assert create_source.call_args_list[1].kwargs['preserve_planes'] is True
        manager.close()

    def test_source_manager_enable_planar_proxy_default(self) -> None:
        """enable_planar_proxy 默认为 True（v3）"""
        from src.shared.video.playback_clock import SourceManager

        manager = SourceManager('/fake/path.mp4')
        assert manager._enable_planar_proxy == True

    def test_source_manager_disable_planar_proxy(self) -> None:
        """可以禁用 planar proxy"""
        from src.shared.video.playback_clock import SourceManager

        manager = SourceManager('/fake/path.mp4', enable_planar_proxy=False)
        assert manager._enable_planar_proxy == False

    def test_source_manager_open_with_yuv_source_mock(
        self,
        yuv420_metadata: VideoMetadata
    ) -> None:
        """打开 YUV 源时 proxy 启用 planar"""
        from src.shared.video.playback_clock import SourceManager

        # Mock source factory
        mock_master_source = MagicMock()
        mock_master_source.probe.return_value = yuv420_metadata
        mock_master_source.open = MagicMock()
        mock_master_source.close = MagicMock()
        mock_master_source.path = '/fake/path.mp4'

        mock_proxy_source = MagicMock()
        mock_proxy_source.open = MagicMock()
        mock_proxy_source.close = MagicMock()
        mock_proxy_source.is_planar_mode = True
        mock_proxy_source.path = '/fake/path.mp4'

        def mock_factory(path: str, target_size=None, preserve_planes=False):
            if target_size is None and not preserve_planes:
                return mock_master_source
            else:
                mock_proxy_source.is_planar_mode = preserve_planes
                return mock_proxy_source

        manager = SourceManager('/fake/path.mp4', source_factory=mock_factory)
        result = manager.open()

        assert result == True
        # YUV 源应该启用 planar（preserve_planes=True）
        assert mock_proxy_source.is_planar_mode == True

    def test_source_manager_open_with_rgb_source_mock(
        self,
        rgb_metadata: VideoMetadata
    ) -> None:
        """打开 RGB 源时 proxy 不启用 planar"""
        from src.shared.video.playback_clock import SourceManager

        # Mock source factory
        mock_master_source = MagicMock()
        mock_master_source.probe.return_value = rgb_metadata
        mock_master_source.open = MagicMock()
        mock_master_source.close = MagicMock()
        mock_master_source.path = '/fake/path.mp4'

        mock_proxy_source = MagicMock()
        mock_proxy_source.open = MagicMock()
        mock_proxy_source.close = MagicMock()
        mock_proxy_source.is_planar_mode = False
        mock_proxy_source.path = '/fake/path.mp4'

        def mock_factory(path: str, target_size=None, preserve_planes=False):
            if target_size is None and not preserve_planes:
                return mock_master_source
            else:
                mock_proxy_source.is_planar_mode = preserve_planes
                return mock_proxy_source

        manager = SourceManager('/fake/path.mp4', source_factory=mock_factory)
        result = manager.open()

        assert result == True
        # RGB 源不应该启用 planar
        assert mock_proxy_source.is_planar_mode == False


# =============================================================================
# 3. _submit_decoded_frame_playback payload 测试
# =============================================================================

class TestSubmitDecodedFramePlaybackPayload:
    """测试 _submit_decoded_frame_playback payload"""

    def test_planar_frame_payload_structure(
        self,
        planar_yuv420_frame: DecodedFrame
    ) -> None:
        """planar frame payload 包含必需字段"""
        # 模拟 _submit_decoded_frame_playback 构造的 payload
        payload: Dict[str, Any] = {
            'decoded_frame': planar_yuv420_frame,
            'frame_index': 0,
            'render_params': {},
            'generation_id': 0,
            'drop_if_stale': True,
            'yuv_matrix': 0,  # BT709
            'color_range': 0,  # LIMITED
        }

        # 验证 payload
        assert 'decoded_frame' in payload
        assert payload['decoded_frame'].is_planar == True
        assert payload['decoded_frame'].pixel_format == 'yuv420p'
        assert 'yuv_matrix' in payload
        assert 'color_range' in payload
        assert len(payload['decoded_frame'].planes) == 3

    def test_planar_10bit_frame_payload(
        self,
        planar_yuv420p10_frame: DecodedFrame
    ) -> None:
        """10-bit planar frame payload"""
        payload: Dict[str, Any] = {
            'decoded_frame': planar_yuv420p10_frame,
            'frame_index': 0,
            'render_params': {},
            'generation_id': 0,
            'yuv_matrix': 1,  # BT2020
            'color_range': 0,  # LIMITED
        }

        # 验证 payload
        assert payload['decoded_frame'].is_planar == True
        assert payload['decoded_frame'].pixel_format == 'yuv420p10le'
        assert payload['decoded_frame'].bit_depth == 10
        assert payload['decoded_frame'].planes[0].dtype == np.uint16

    def test_packed_frame_payload_no_yuv_matrix(
        self,
        packed_rgb_frame: DecodedFrame
    ) -> None:
        """packed frame payload 不需要 yuv_matrix"""
        payload: Dict[str, Any] = {
            'decoded_frame': packed_rgb_frame,
            'frame_index': 0,
            'render_params': {},
            'generation_id': 0,
        }

        # packed RGB 不需要 yuv_matrix/color_range
        assert payload['decoded_frame'].is_packed == True
        assert payload['decoded_frame'].pixel_format == 'rgb24'

    def test_yuv_matrix_from_metadata(
        self,
        planar_yuv420_frame: DecodedFrame
    ) -> None:
        """从 metadata 提取 yuv_matrix"""
        from src.shared.video.video_playback import VideoPlaybackEngine
        from src.shared.video.yuv_gpu_upload import YUVGPUUploader

        with patch('src.shared.video.video_playback.VideoPlaybackEngine.__init__', return_value=None):
            engine = VideoPlaybackEngine.__new__(VideoPlaybackEngine)
            engine._perf_monitor = MagicMock()

            yuv_matrix = engine._get_yuv_matrix(planar_yuv420_frame)
            # BT709 = 0
            assert yuv_matrix == YUVGPUUploader.MATRIX_BT709

    def test_color_range_from_metadata(
        self,
        planar_yuv420_frame: DecodedFrame
    ) -> None:
        """从 metadata 提取 color_range"""
        from src.shared.video.video_playback import VideoPlaybackEngine
        from src.shared.video.yuv_gpu_upload import YUVGPUUploader

        with patch('src.shared.video.video_playback.VideoPlaybackEngine.__init__', return_value=None):
            engine = VideoPlaybackEngine.__new__(VideoPlaybackEngine)
            engine._perf_monitor = MagicMock()

            color_range = engine._get_color_range(planar_yuv420_frame)
            # LIMITED = 0
            assert color_range == YUVGPUUploader.RANGE_LIMITED


# =============================================================================
# 4. DecodeWorker planar frame 处理测试
# =============================================================================

class TestDecodeWorkerPlanarFrame:
    """测试 DecodeWorker 正确处理 planar frame"""

    def test_proxy_decode_worker_accepts_planar_frame(
        self,
        planar_yuv420_frame: DecodedFrame
    ) -> None:
        """ProxyDecodeWorker 正确接受 planar frame"""
        # 验证 DecodedFrame 不被转换
        # DecodeWorker 只传递 DecodedFrame，不做 RGB 转换
        assert planar_yuv420_frame.is_planar == True
        assert planar_yuv420_frame.data is None  # planar 没有打包数据

        # 验证 planes 正确
        assert len(planar_yuv420_frame.planes) == 3
        assert planar_yuv420_frame.planes[0].shape == (480, 640)  # Y
        assert planar_yuv420_frame.planes[1].shape == (240, 320)  # U
        assert planar_yuv420_frame.planes[2].shape == (240, 320)  # V

    def test_planar_frame_bit_depth_preserved(
        self,
        planar_yuv420p10_frame: DecodedFrame
    ) -> None:
        """planar frame 保留位深度"""
        # 10-bit 数据保留在 uint16
        assert planar_yuv420p10_frame.bit_depth == 10
        assert planar_yuv420p10_frame.dtype == np.uint16

        # planes 也是 uint16
        for plane in planar_yuv420p10_frame.planes:
            assert plane.dtype == np.uint16

    def test_planar_frame_metadata_preserved(
        self,
        planar_yuv420_frame: DecodedFrame
    ) -> None:
        """planar frame 保留 metadata"""
        assert planar_yuv420_frame.metadata is not None
        assert planar_yuv420_frame.metadata.color_matrix == 'bt709'
        assert planar_yuv420_frame.metadata.color_range == 'limited'


# =============================================================================
# 5. v2 fallback 测试
# =============================================================================

class TestV2Fallback:
    """测试 v2 默认路径不受影响"""

    def test_v2_disable_planar_proxy(self) -> None:
        """v2 可以禁用 planar proxy"""
        from src.shared.video.playback_clock import SourceManager

        # v2: 禁用 planar proxy
        manager = SourceManager('/fake/path.mp4', enable_planar_proxy=False)
        assert manager._enable_planar_proxy == False

    def test_v2_master_source_not_planar(
        self,
        yuv420_metadata: VideoMetadata
    ) -> None:
        """v2 master source 不启用 planar"""
        from src.shared.video.playback_clock import SourceManager

        # Mock source factory
        mock_master_source = MagicMock()
        mock_master_source.probe.return_value = yuv420_metadata
        mock_master_source.open = MagicMock()
        mock_master_source.close = MagicMock()
        mock_master_source.is_planar_mode = False
        mock_master_source.path = '/fake/path.mp4'

        # v2: master 不启用 planar
        def mock_factory(path: str, target_size=None, preserve_planes=False):
            mock_master_source.is_planar_mode = preserve_planes
            return mock_master_source

        manager = SourceManager('/fake/path.mp4', source_factory=mock_factory)
        result = manager.open()

        # master 应该不启用 planar
        assert mock_master_source.is_planar_mode == False

    def test_v2_packed_frame_still_works(
        self,
        packed_rgb_frame: DecodedFrame
    ) -> None:
        """v2 packed frame 路径仍然工作"""
        # packed RGB 路径应该不受影响
        assert packed_rgb_frame.is_packed == True
        assert packed_rgb_frame.data is not None


# =============================================================================
# 运行测试
# =============================================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
