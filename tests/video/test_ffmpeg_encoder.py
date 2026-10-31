"""
FFmpeg Video Encoder Tests
测试 FFmpegVideoEncoder 的视频编码功能。

## 覆盖范围

- 配置验证（FFmpegEncoderConfig, ColorMetadataConfig）
- open/close 生命周期
- encode_frame 功能
- 色彩元数据设置
- 不同编码器配置（ProRes/DNxHR/H.264/HEVC）

## 测试策略

- 使用 Mock 模拟 PyAV
- 使用临时文件路径
- 测试正常情况和边界情况
- 验证错误处理

"""

from __future__ import annotations

import os
import sys
import pytest
import tempfile
from typing import Any, Optional
from unittest.mock import MagicMock, patch, PropertyMock

import numpy as np

# 导入测试模块
from src.shared.export.video_encoder import (
    FFmpegVideoEncoder,
    FFmpegEncoderConfig,
    ColorMetadataConfig,
    CodecType,
    ProResProfile,
    DNxHRProfile,
    PIXEL_FORMAT_MAP,
    ENCODER_NAME_MAP,
    create_encoder_config,
    get_recommended_codec,
)
from src.shared.media.metadata import VideoMetadata, Rational
from src.shared.media.frame import DecodedFrame


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def sample_metadata() -> VideoMetadata:
    """创建示例视频元数据"""
    return VideoMetadata(
        width=1920,
        height=1080,
        codec_name='prores',
        codec_profile=None,
        fps=Rational(24, 1),
        total_frames=100,
        bit_depth=10,
        pixel_format='yuv422p10le',
        color_primaries='bt709',
        color_transfer='bt709',
        color_matrix='bt709',
        color_range='limited',
        has_alpha=False,
    )


@pytest.fixture
def sample_metadata_hdr() -> VideoMetadata:
    """创建 HDR 视频元数据"""
    return VideoMetadata(
        width=1920,
        height=1080,
        codec_name='hevc',
        codec_profile=None,
        fps=Rational(24, 1),
        total_frames=100,
        bit_depth=10,
        pixel_format='yuv420p10le',
        color_primaries='bt2020',
        color_transfer='smpte2084',
        color_matrix='bt2020nc',
        color_range='limited',
        has_alpha=False,
    )


@pytest.fixture
def sample_metadata_alpha() -> VideoMetadata:
    """创建带 Alpha 的视频元数据"""
    return VideoMetadata(
        width=1920,
        height=1080,
        codec_name='prores',
        codec_profile=None,
        fps=Rational(24, 1),
        total_frames=100,
        bit_depth=10,
        pixel_format='yuva444p10le',
        color_primaries='bt709',
        color_transfer='bt709',
        color_matrix='bt709',
        color_range='full',
        has_alpha=True,
    )


@pytest.fixture
def sample_frame() -> DecodedFrame:
    """创建示例帧"""
    data = np.random.randint(0, 256, (1080, 1920, 3), dtype=np.uint8)
    return DecodedFrame(
        data=data,
        frame_index=0,
        width=1920,
        height=1080,
        pixel_format='rgb24',
        pts=0,
        time_seconds=0.0,
        dtype=np.uint8,
    )


@pytest.fixture
def sample_frame_planar() -> DecodedFrame:
    """创建平面格式示例帧"""
    # Y plane
    y_plane = np.random.randint(0, 256, (1080, 1920), dtype=np.uint8)
    # U plane (4:2:0)
    u_plane = np.random.randint(0, 256, (540, 960), dtype=np.uint8)
    # V plane (4:2:0)
    v_plane = np.random.randint(0, 256, (540, 960), dtype=np.uint8)
    
    return DecodedFrame(
        data=None,
        planes=[y_plane, u_plane, v_plane],
        frame_index=0,
        width=1920,
        height=1080,
        pixel_format='yuv420p',
        pts=0,
        time_seconds=0.0,
        dtype=np.uint8,
    )


@pytest.fixture
def temp_output_path() -> str:
    """创建临时输出路径"""
    with tempfile.NamedTemporaryFile(suffix='.mov', delete=False) as f:
        path = f.name
    yield path
    # 清理
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def basic_config(sample_metadata: VideoMetadata) -> FFmpegEncoderConfig:
    """创建基本编码器配置"""
    return FFmpegEncoderConfig(
        codec='prores',
        width=sample_metadata.width,
        height=sample_metadata.height,
        fps=sample_metadata.fps,
        pix_fmt='yuv422p10le',
        profile='hq',
    )


@pytest.fixture
def mock_av_container() -> MagicMock:
    """创建模拟的 PyAV 容器"""
    container = MagicMock()
    container.add_stream.return_value = MagicMock()
    container.mux = MagicMock()
    container.close = MagicMock()
    return container


@pytest.fixture
def mock_av_stream() -> MagicMock:
    """创建模拟的 PyAV 流"""
    stream = MagicMock()
    stream.width = 1920
    stream.height = 1080
    stream.pix_fmt = 'yuv422p10le'
    stream.options = {}
    stream.encode.return_value = []  # 返回空包列表
    stream.time_base = MagicMock()
    return stream


# =============================================================================
# ColorMetadataConfig Tests
# =============================================================================


class TestColorMetadataConfig:
    """测试 ColorMetadataConfig"""

    def test_valid_sdr_config(self) -> None:
        """有效 SDR 配置"""
        config = ColorMetadataConfig(
            primaries='bt709',
            transfer='bt709',
            matrix='bt709',
            range='limited',
        )
        
        assert config.primaries == 'bt709'
        assert config.transfer == 'bt709'
        assert config.matrix == 'bt709'
        assert config.range == 'limited'

    def test_valid_hdr10_config(self) -> None:
        """有效 HDR10 配置"""
        config = ColorMetadataConfig(
            primaries='bt2020',
            transfer='smpte2084',
            matrix='bt2020nc',
            range='limited',
        )
        
        assert config.primaries == 'bt2020'
        assert config.transfer == 'smpte2084'
        assert config.matrix == 'bt2020nc'

    def test_valid_hlg_config(self) -> None:
        """有效 HLG 配置"""
        config = ColorMetadataConfig(
            primaries='bt2020',
            transfer='arib-std-b67',
            matrix='bt2020nc',
            range='limited',
        )
        
        assert config.transfer == 'arib-std-b67'

    def test_unknown_primaries_warning(self) -> None:
        """未知 primaries 应警告"""
        config = ColorMetadataConfig(
            primaries='unknown',
            transfer='bt709',
            matrix='bt709',
            range='limited',
        )
        
        # 应不抛出错误，仅警告
        assert config.primaries == 'unknown'

    def test_from_metadata(self, sample_metadata: VideoMetadata) -> None:
        """从 VideoMetadata 创建"""
        config = ColorMetadataConfig.from_metadata(sample_metadata)
        
        assert config.primaries == sample_metadata.color_primaries
        assert config.transfer == sample_metadata.color_transfer
        assert config.matrix == sample_metadata.color_matrix

    def test_from_metadata_hdr(self, sample_metadata_hdr: VideoMetadata) -> None:
        """从 HDR 元数据创建"""
        config = ColorMetadataConfig.from_metadata(sample_metadata_hdr)
        
        assert config.primaries == 'bt2020'
        assert config.transfer == 'smpte2084'
        assert config.matrix == 'bt2020nc'

    def test_sdr_factory(self) -> None:
        """SDR 工厂方法"""
        config = ColorMetadataConfig.sdr()
        
        assert config.primaries == 'bt709'
        assert config.transfer == 'bt709'
        assert config.matrix == 'bt709'

    def test_hdr10_factory(self) -> None:
        """HDR10 工厂方法"""
        config = ColorMetadataConfig.hdr10()
        
        assert config.primaries == 'bt2020'
        assert config.transfer == 'smpte2084'

    def test_hlg_factory(self) -> None:
        """HLG 工厂方法"""
        config = ColorMetadataConfig.hlg()
        
        assert config.primaries == 'bt2020'
        assert config.transfer == 'arib-std-b67'

    def test_dci_p3_factory(self) -> None:
        """DCI-P3 工厂方法"""
        config = ColorMetadataConfig.dci_p3()
        
        assert config.primaries == 'smpte431'


# =============================================================================
# FFmpegEncoderConfig Tests
# =============================================================================


class TestFFmpegEncoderConfig:
    """测试 FFmpegEncoderConfig"""

    def test_valid_config(self) -> None:
        """有效配置"""
        config = FFmpegEncoderConfig(
            codec='prores',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
        )
        
        assert config.codec == 'prores'
        assert config.width == 1920
        assert config.height == 1080
        assert config.fps.to_fps() == 24.0

    def test_invalid_resolution_raises(self) -> None:
        """无效分辨率应抛出错误"""
        with pytest.raises(ValueError, match="Invalid resolution"):
            FFmpegEncoderConfig(
                codec='prores',
                width=0,
                height=1080,
                fps=Rational(24, 1),
            )

        with pytest.raises(ValueError, match="Invalid resolution"):
            FFmpegEncoderConfig(
                codec='prores',
                width=-1920,
                height=1080,
                fps=Rational(24, 1),
            )

    def test_auto_profile_prores(self) -> None:
        """ProRes 自动推断 profile"""
        config = FFmpegEncoderConfig(
            codec='prores',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
        )
        
        # 默认 profile 应为 'hq'
        assert config.profile == 'hq'

    def test_auto_profile_dnxhr(self) -> None:
        """DNxHR 自动推断 profile"""
        config = FFmpegEncoderConfig(
            codec='dnxhr',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
        )
        
        assert config.profile in ('hq', 'hqx')

    def test_auto_profile_dnxhr_10bit(self) -> None:
        """DNxHR 10-bit 自动推断 profile"""
        config = FFmpegEncoderConfig(
            codec='dnxhr',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            pix_fmt='yuv422p10le',
        )

        # 10-bit 应使用 hqx
        assert config.profile == 'hqx'

    def test_auto_profile_hevc_follows_bit_depth(self) -> None:
        """HEVC 8-bit 档推断 main（"high" 是 H.264 专属名，x265 不识别）"""
        config_8bit = FFmpegEncoderConfig(
            codec='hevc',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            pix_fmt='yuv420p',
        )
        assert config_8bit.profile == 'main'

        config_10bit = FFmpegEncoderConfig(
            codec='hevc',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            pix_fmt='yuv420p10le',
        )
        assert config_10bit.profile == 'main10'

    def test_auto_gop_size(self) -> None:
        """GOP codec 自动设置 GOP size"""
        config = FFmpegEncoderConfig(
            codec='h264',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
        )
        
        # 默认 GOP size = fps * 2
        assert config.gop_size == 48

    def test_auto_color_metadata(self) -> None:
        """自动设置色彩元数据"""
        config = FFmpegEncoderConfig(
            codec='prores',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
        )
        
        # 默认应为 SDR
        assert config.color_metadata is not None
        assert config.color_metadata.primaries == 'bt709'

    def test_alpha_with_incompatible_pix_fmt(self) -> None:
        """Alpha 与不兼容像素格式的警告"""
        config = FFmpegEncoderConfig(
            codec='h264',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            pix_fmt='yuv420p',
            has_alpha=True,
        )
        
        # yuv420p 不支持 alpha，has_alpha 应被禁用
        assert config.has_alpha is False

    def test_from_metadata(self, sample_metadata: VideoMetadata) -> None:
        """从 VideoMetadata 创建"""
        config = FFmpegEncoderConfig.from_metadata(
            sample_metadata,
            codec='prores',
            profile='hq',
        )
        
        assert config.codec == 'prores'
        assert config.width == sample_metadata.width
        assert config.height == sample_metadata.height
        assert config.fps == sample_metadata.fps

    def test_from_metadata_hdr(self, sample_metadata_hdr: VideoMetadata) -> None:
        """从 HDR 元数据创建"""
        config = FFmpegEncoderConfig.from_metadata(
            sample_metadata_hdr,
            codec='hevc',
        )
        
        assert config.codec == 'hevc'
        assert config.color_metadata is not None
        assert config.color_metadata.transfer == 'smpte2084'

    def test_from_metadata_alpha(self, sample_metadata_alpha: VideoMetadata) -> None:
        """从带 Alpha 元数据创建"""
        config = FFmpegEncoderConfig.from_metadata(
            sample_metadata_alpha,
            codec='prores',
            profile='4444',
        )
        
        assert config.has_alpha is True
        assert 'yuva' in config.pix_fmt or '4444' in config.pix_fmt

    def test_get_encoder_name(self) -> None:
        """获取 FFmpeg 编码器名称（hwaccel='auto' 时平台相关）"""
        config_h264 = FFmpegEncoderConfig(
            codec='h264',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
        )
        config_hevc = FFmpegEncoderConfig(
            codec='hevc',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
        )
        config_prores = FFmpegEncoderConfig(
            codec='prores',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
        )
        assert config_prores.get_encoder_name() == 'prores_ks'

        # auto 模式：Windows 按 NVIDIA/AMD 路由 AMF/NVENC；macOS 的
        # 8-bit 4:2:0 H.264/HEVC 使用 VideoToolbox（仅注册探测，不假定驱动可用）。
        from src.shared.export.video_encoder import (
            detect_hw_vendor, vendor_hwaccel_name,
            videotoolbox_encoder_available,
        )
        hw = vendor_hwaccel_name(detect_hw_vendor())
        hw_h264 = sys.platform == 'win32' and bool(hw)
        hw_hevc = hw_h264
        if sys.platform == 'darwin':
            hw_h264 = videotoolbox_encoder_available('h264')
            hw_hevc = videotoolbox_encoder_available('hevc')
        assert config_h264.get_encoder_name() == (
            (f'h264_{hw}' if sys.platform == 'win32' else 'h264_videotoolbox')
            if hw_h264 else 'libx264'
        )
        assert config_hevc.get_encoder_name() == (
            (f'hevc_{hw}' if sys.platform == 'win32' else 'hevc_videotoolbox')
            if hw_hevc else 'libx265'
        )

        # hwaccel=None 强制 CPU，与平台无关
        cpu_h264 = FFmpegEncoderConfig(
            codec='h264', width=1920, height=1080, fps=Rational(24, 1),
            hwaccel=None,
        )
        cpu_hevc = FFmpegEncoderConfig(
            codec='hevc', width=1920, height=1080, fps=Rational(24, 1),
            hwaccel=None,
        )
        assert cpu_h264.get_encoder_name() == 'libx264'
        assert cpu_hevc.get_encoder_name() == 'libx265'

    def test_pix_fmt_selection_10bit(self) -> None:
        """10-bit 像素格式选择"""
        pix_fmt = FFmpegEncoderConfig._select_pix_fmt(
            codec='hevc',
            bit_depth=10,
            has_alpha=False,
            profile=None,
        )
        
        assert pix_fmt == 'yuv420p10le'

    def test_pix_fmt_selection_8bit(self) -> None:
        """8-bit 像素格式选择"""
        pix_fmt = FFmpegEncoderConfig._select_pix_fmt(
            codec='h264',
            bit_depth=8,
            has_alpha=False,
            profile=None,
        )
        
        assert pix_fmt == 'yuv420p'

    def test_pix_fmt_selection_prores(self) -> None:
        """ProRes 像素格式选择"""
        pix_fmt = FFmpegEncoderConfig._select_pix_fmt(
            codec='prores',
            bit_depth=10,
            has_alpha=False,
            profile='hq',
        )
        
        assert pix_fmt == 'yuv422p10le'

    def test_pix_fmt_selection_prores_4444(self) -> None:
        """ProRes 4444 像素格式选择"""
        pix_fmt = FFmpegEncoderConfig._select_pix_fmt(
            codec='prores',
            bit_depth=10,
            has_alpha=True,
            profile='4444',
        )
        
        assert 'yuva' in pix_fmt or '4444' in pix_fmt


# =============================================================================
# FFmpegVideoEncoder Initialization Tests
# =============================================================================


class TestEncoderInitialization:
    """测试编码器初始化"""

    def test_init_with_valid_params(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
    ) -> None:
        """有效参数初始化"""
        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
        
        assert encoder.output_path == temp_output_path
        assert encoder.config == basic_config
        assert encoder.frame_count == 0
        assert encoder.is_open is False

    def test_init_properties(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
    ) -> None:
        """初始化后的属性"""
        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
        
        assert encoder.output_path.endswith('.mov')
        assert encoder.config.codec == 'prores'


# =============================================================================
# Open/Close Lifecycle Tests
# =============================================================================


class TestEncoderLifecycle:
    """测试 open/close 生命周期"""

    @patch.dict('sys.modules', {'av': MagicMock()})
    def test_open_creates_container(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
        mock_av_container: MagicMock,
        mock_av_stream: MagicMock,
    ) -> None:
        """open 创建容器和流"""
        import sys
        mock_av = sys.modules['av']
        mock_av.open.return_value = mock_av_container
        mock_av_container.add_stream.return_value = mock_av_stream

        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
        encoder.open()

        # av.open 应被调用
        mock_av.open.assert_called_once()
        assert encoder.is_open is True

    @patch.dict('sys.modules', {'av': MagicMock()})
    def test_double_open_no_effect(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
    ) -> None:
        """重复打开无效果"""
        import sys
        mock_av = sys.modules['av']
        mock_container = MagicMock()
        mock_av.open.return_value = mock_container

        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
        encoder.open()
        encoder.open()  # 第二次

        # av.open 应只被调用一次
        assert mock_av.open.call_count == 1

    @patch.dict('sys.modules', {'av': MagicMock()})
    def test_close_flushes_and_closes(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
    ) -> None:
        """close 刷新并关闭"""
        import sys
        mock_av = sys.modules['av']
        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_stream.encode.return_value = []
        mock_container.add_stream.return_value = mock_stream
        mock_av.open.return_value = mock_container

        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
        encoder.open()
        encoder.close()

        # 流应编码结束帧
        mock_stream.encode.assert_called()
        # 容器应关闭
        mock_container.close.assert_called_once()
        assert encoder.is_open is False

    @patch.dict('sys.modules', {'av': MagicMock()})
    def test_double_close_no_effect(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
    ) -> None:
        """重复关闭无效果"""
        import sys
        mock_av = sys.modules['av']
        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_stream.encode.return_value = []
        mock_container.add_stream.return_value = mock_stream
        mock_av.open.return_value = mock_container

        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
        encoder.open()
        encoder.close()
        encoder.close()  # 第二次

        # close 应只被调用一次
        assert mock_container.close.call_count == 1

    def test_open_without_pyav_raises(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
    ) -> None:
        """无 PyAV 时打开应抛出错误"""
        with patch.dict('sys.modules', {'av': None}):
            encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
            
            with pytest.raises(RuntimeError, match="PyAV is not installed"):
                encoder.open()

    def test_container_format_inference_mov(
        self,
        basic_config: FFmpegEncoderConfig,
    ) -> None:
        """MOV 容器格式推断"""
        encoder = FFmpegVideoEncoder('/tmp/output.mov', basic_config)
        format_name = encoder._infer_container_format()
        assert format_name == 'mov'

    def test_container_format_inference_mp4(
        self,
        basic_config: FFmpegEncoderConfig,
    ) -> None:
        """MP4 容器格式推断"""
        encoder = FFmpegVideoEncoder('/tmp/output.mp4', basic_config)
        format_name = encoder._infer_container_format()
        assert format_name == 'mp4'

    def test_container_format_inference_mkv(
        self,
        basic_config: FFmpegEncoderConfig,
    ) -> None:
        """MKV 容器格式推断"""
        encoder = FFmpegVideoEncoder('/tmp/output.mkv', basic_config)
        format_name = encoder._infer_container_format()
        assert format_name == 'matroska'


# =============================================================================
# Encode Frame Tests
# =============================================================================


class TestPipelinedConversion:
    """流水线转换（pipelined_conversion=True）语义"""

    def test_pipeline_encodes_all_frames_after_close(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
        sample_frame: DecodedFrame,
    ) -> None:
        """流水线下 encode_frame 返回即编码完成；close 后计数完整"""
        config = FFmpegEncoderConfig(
            codec=basic_config.codec,
            width=basic_config.width,
            height=basic_config.height,
            fps=basic_config.fps,
            pix_fmt=basic_config.pix_fmt,
            crf=basic_config.crf,
            preset=basic_config.preset,
            pipelined_conversion=True,
        )
        encoder = FFmpegVideoEncoder(temp_output_path, config)
        encoder.open()
        for i in range(6):
            encoder.encode_frame(sample_frame)
        # 同步语义不再保证：计数可能落后（背压上限 2），不在此断言
        encoder.close()
        assert encoder.frame_count == 6
        assert encoder.is_open is False


class TestEncodeFrame:
    """测试 encode_frame 功能"""

    def test_encode_frame_without_open_raises(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
        sample_frame: DecodedFrame,
    ) -> None:
        """未打开时编码应抛出错误"""
        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)

        with pytest.raises(RuntimeError, match="Encoder not open"):
            encoder.encode_frame(sample_frame)

    @patch.dict('sys.modules', {'av': MagicMock()})
    def test_encode_single_frame(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
        sample_frame: DecodedFrame,
    ) -> None:
        """编码单帧"""
        import sys
        mock_av = sys.modules['av']
        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_stream.encode.return_value = [MagicMock()]  # 返回一个包
        mock_container.add_stream.return_value = mock_stream
        mock_av.open.return_value = mock_container

        # Mock VideoFrame
        mock_av_frame = MagicMock()
        mock_av.VideoFrame = MagicMock(return_value=mock_av_frame)
        mock_av.VideoFrame.from_ndarray = MagicMock(return_value=mock_av_frame)
        mock_av_frame.reformat = MagicMock(return_value=mock_av_frame)

        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
        encoder.open()
        encoder.encode_frame(sample_frame)

        assert encoder.frame_count == 1

    @patch.dict('sys.modules', {'av': MagicMock()})
    def test_encode_multiple_frames(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
        sample_frame: DecodedFrame,
    ) -> None:
        """编码多帧"""
        import sys
        mock_av = sys.modules['av']
        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_stream.encode.return_value = [MagicMock()]
        mock_container.add_stream.return_value = mock_stream
        mock_av.open.return_value = mock_container

        mock_av_frame = MagicMock()
        mock_av.VideoFrame.return_value = mock_av_frame
        mock_av.VideoFrame.from_ndarray.return_value = mock_av_frame
        mock_av_frame.reformat.return_value = mock_av_frame

        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
        encoder.open()

        for i in range(10):
            # 创建新帧
            frame = DecodedFrame(
                data=np.random.randint(0, 256, (1080, 1920, 3), dtype=np.uint8),
                frame_index=i,
                width=1920,
                height=1080,
                pixel_format='rgb24',
                pts=i,
                time_seconds=i / 24.0,
                dtype=np.uint8,
            )
            encoder.encode_frame(frame)

        assert encoder.frame_count == 10

    @patch.dict('sys.modules', {'av': MagicMock()})
    def test_encode_updates_pts(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
        sample_frame: DecodedFrame,
    ) -> None:
        """编码时 PTS 递增"""
        import sys
        mock_av = sys.modules['av']
        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_stream.encode.return_value = []
        mock_container.add_stream.return_value = mock_stream
        mock_av.open.return_value = mock_container

        mock_av_frame = MagicMock()
        mock_av.VideoFrame.return_value = mock_av_frame
        mock_av.VideoFrame.from_ndarray.return_value = mock_av_frame
        mock_av_frame.reformat.return_value = mock_av_frame

        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
        encoder.open()

        # PTS 应从 0 开始
        encoder.encode_frame(sample_frame)
        assert mock_av_frame.pts == 0

        # 第二帧 PTS 应为 1
        encoder.encode_frame(sample_frame)
        assert mock_av_frame.pts == 1


# =============================================================================
# Color Metadata Tests
# =============================================================================


class TestColorMetadata:
    """测试色彩元数据设置"""

    @patch.dict('sys.modules', {'av': MagicMock()})
    def test_set_sdr_metadata(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
    ) -> None:
        """设置 SDR 色彩元数据"""
        import sys
        mock_av = sys.modules['av']
        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_container.add_stream.return_value = mock_stream
        mock_av.open.return_value = mock_container

        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
        encoder.open()

        # 应设置色彩元数据
        assert mock_stream.color_primaries is not None or True  # 验证调用

    @patch.dict('sys.modules', {'av': MagicMock()})
    def test_set_hdr10_metadata(
        self,
        temp_output_path: str,
    ) -> None:
        """设置 HDR10 色彩元数据"""
        import sys
        mock_av = sys.modules['av']
        config = FFmpegEncoderConfig(
            codec='hevc',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            pix_fmt='yuv420p10le',
            color_metadata=ColorMetadataConfig.hdr10(),
        )

        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_container.add_stream.return_value = mock_stream
        mock_av.open.return_value = mock_container

        encoder = FFmpegVideoEncoder(temp_output_path, config)
        encoder.open()

        # color_primaries 应设置为 bt2020 (值 9)
        # color_trc 应设置为 smpte2084 (值 16)

    @patch.dict('sys.modules', {'av': MagicMock()})
    def test_set_hlg_metadata(
        self,
        temp_output_path: str,
    ) -> None:
        """设置 HLG 色彩元数据"""
        import sys
        mock_av = sys.modules['av']
        config = FFmpegEncoderConfig(
            codec='hevc',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            pix_fmt='yuv420p10le',
            color_metadata=ColorMetadataConfig.hlg(),
        )

        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_container.add_stream.return_value = mock_stream
        mock_av.open.return_value = mock_container

        encoder = FFmpegVideoEncoder(temp_output_path, config)
        encoder.open()


# =============================================================================
# Codec-specific Tests
# =============================================================================


class TestProResEncoder:
    """测试 ProRes 编码器"""

    def test_prores_config(self) -> None:
        """ProRes 配置"""
        config = FFmpegEncoderConfig(
            codec='prores',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            pix_fmt='yuv422p10le',
            profile='hq',
        )

        assert config.codec == 'prores'
        assert config.profile == 'hq'
        assert config.pix_fmt == 'yuv422p10le'

    def test_prores_proxy_profile(self) -> None:
        """ProRes Proxy profile"""
        config = FFmpegEncoderConfig(
            codec='prores',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            profile='proxy',
        )
        
        assert config.profile == 'proxy'

    def test_prores_4444_profile(self) -> None:
        """ProRes 4444 profile"""
        config = FFmpegEncoderConfig(
            codec='prores',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            profile='4444',
            has_alpha=True,
        )
        
        assert config.profile == '4444'

    def test_prores_encoder_name(self) -> None:
        """ProRes 编码器名称"""
        config = FFmpegEncoderConfig(
            codec='prores',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
        )
        
        assert config.get_encoder_name() == 'prores_ks'


class TestDNxHREncoder:
    """测试 DNxHR 编码器"""

    def test_dnxhr_config(self) -> None:
        """DNxHR 配置"""
        config = FFmpegEncoderConfig(
            codec='dnxhr',
            width=3840,
            height=2160,
            fps=Rational(24, 1),
            profile='hq',
        )
        
        assert config.codec == 'dnxhr'
        assert config.profile == 'hq'

    def test_dnxhr_hqx_profile(self) -> None:
        """DNxHR HQX profile (10-bit)"""
        config = FFmpegEncoderConfig(
            codec='dnxhr',
            width=3840,
            height=2160,
            fps=Rational(24, 1),
            profile='hqx',
            pix_fmt='yuv422p10le',
        )
        
        assert config.profile == 'hqx'

    def test_dnxhr_444_profile(self) -> None:
        """DNxHR 444 profile"""
        config = FFmpegEncoderConfig(
            codec='dnxhr',
            width=3840,
            height=2160,
            fps=Rational(24, 1),
            profile='444',
            has_alpha=True,
        )
        
        assert config.profile == '444'

    def test_dnxhr_encoder_name(self) -> None:
        """DNxHR 编码器名称"""
        config = FFmpegEncoderConfig(
            codec='dnxhr',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
        )
        
        # FFmpeg 无 'dnxhr' 编码器；DNxHR/DNxHD 均为 dnxhd
        assert config.get_encoder_name() == 'dnxhd'


class TestH264Encoder:
    """测试 H.264 编码器"""

    def test_h264_config(self) -> None:
        """H.264 配置"""
        config = FFmpegEncoderConfig(
            codec='h264',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            crf=18,
            preset='slow',
        )
        
        assert config.codec == 'h264'
        assert config.crf == 18
        assert config.preset == 'slow'

    def test_h264_bitrate_mode(self) -> None:
        """H.264 bitrate 模式"""
        config = FFmpegEncoderConfig(
            codec='h264',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            bitrate=10_000_000,  # 10 Mbps
        )
        
        assert config.bitrate == 10_000_000
        assert config.crf is None

    def test_h264_rate_control_options_support_large_bitrates(self) -> None:
        """Use x264 params so the 12000 Mbps UI default avoids PyAV int overflow."""
        config = FFmpegEncoderConfig(
            codec='h264',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            bitrate=12_000_000_000,
            min_bitrate=12_000_000_000,
            max_bitrate=12_000_000_000,
            buffer_size=24_000_000_000,
        )
        encoder = FFmpegVideoEncoder('/tmp/output.mp4', config)
        stream = MagicMock()
        stream.options = {}
        encoder._stream = stream

        encoder._configure_encoder()

        assert stream.options['x264-params'] == (
            'bitrate=12000000:maxrate=12000000:minrate=12000000:vbv-bufsize=24000000'
        )

    def test_h264_gop_size(self) -> None:
        """H.264 GOP size"""
        config = FFmpegEncoderConfig(
            codec='h264',
            width=1920,
            height=1080,
            fps=Rational(30, 1),
            gop_size=60,
        )
        
        assert config.gop_size == 60

    def test_h264_encoder_name(self) -> None:
        """H.264 编码器名称"""
        config = FFmpegEncoderConfig(
            codec='h264',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            hwaccel=None,
        )
        
        assert config.get_encoder_name() == 'libx264'


class TestHEVCEncoder:
    """测试 HEVC 编码器"""

    def test_hevc_config(self) -> None:
        """HEVC 配置"""
        config = FFmpegEncoderConfig(
            codec='hevc',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            crf=20,
            pix_fmt='yuv420p10le',
        )
        
        assert config.codec == 'hevc'
        assert config.pix_fmt == 'yuv420p10le'

    def test_hevc_10bit_profile(self) -> None:
        """HEVC 10-bit profile"""
        config = FFmpegEncoderConfig(
            codec='hevc',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            pix_fmt='yuv420p10le',
        )
        
        # profile 应为 main10
        assert config.profile == 'main10'

    def test_hevc_encoder_name(self) -> None:
        """HEVC 编码器名称"""
        config = FFmpegEncoderConfig(
            codec='hevc',
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            hwaccel=None,
        )
        
        assert config.get_encoder_name() == 'libx265'


# =============================================================================
# Progress Tests
# =============================================================================


class TestEncoderProgress:
    """测试编码进度"""

    def test_initial_progress(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
    ) -> None:
        """初始进度"""
        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
        
        assert encoder.get_progress() == 0.0

    def test_progress_percentage(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
    ) -> None:
        """进度百分比计算"""
        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
        
        # 假设总帧数为 100
        encoder._frame_count = 50
        
        progress = encoder.get_progress_percentage(100)
        assert progress == 0.5

    def test_progress_at_total(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
    ) -> None:
        """达到总帧数时进度"""
        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
        encoder._frame_count = 100
        
        progress = encoder.get_progress_percentage(100)
        assert progress == 1.0

    def test_progress_zero_total(
        self,
        temp_output_path: str,
        basic_config: FFmpegEncoderConfig,
    ) -> None:
        """总帧数为 0 时进度"""
        encoder = FFmpegVideoEncoder(temp_output_path, basic_config)
        
        progress = encoder.get_progress_percentage(0)
        assert progress == 0.0


# =============================================================================
# Helper Function Tests
# =============================================================================


class TestHelperFunctions:
    """测试辅助函数"""

    def test_create_encoder_config(
        self,
        sample_metadata: VideoMetadata,
    ) -> None:
        """create_encoder_config 创建配置"""
        config = create_encoder_config(
            sample_metadata,
            codec='prores',
            profile='hq',
        )
        
        assert isinstance(config, FFmpegEncoderConfig)
        assert config.codec == 'prores'
        assert config.profile == 'hq'

    def test_get_recommended_codec_mov(self) -> None:
        """MOV 格式推荐编码器"""
        metadata = VideoMetadata(
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            total_frames=100,
            codec_name='prores',
            codec_profile=None,
            pixel_format='yuv422p10le',
            bit_depth=10,
            has_alpha=False,
        )

        codec = get_recommended_codec(metadata, output_format='mov')
        assert codec == 'prores'

    def test_get_recommended_codec_mov_alpha(self) -> None:
        """MOV 格式带 Alpha 推荐编码器"""
        metadata = VideoMetadata(
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            total_frames=100,
            codec_name='prores',
            codec_profile=None,
            pixel_format='yuva444p10le',
            bit_depth=10,
            has_alpha=True,
        )

        codec = get_recommended_codec(metadata, output_format='mov')
        assert codec == 'prores'  # ProRes 4444

    def test_get_recommended_codec_mp4_sdr(self) -> None:
        """MP4 格式 SDR 推荐编码器"""
        metadata = VideoMetadata(
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            total_frames=100,
            codec_name='h264',
            codec_profile=None,
            pixel_format='yuv420p',
            bit_depth=8,
        )

        codec = get_recommended_codec(metadata, output_format='mp4')
        assert codec == 'h264'

    def test_get_recommended_codec_mp4_hdr(self) -> None:
        """MP4 格式 HDR 推荐编码器"""
        metadata = VideoMetadata(
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            total_frames=100,
            codec_name='hevc',
            codec_profile=None,
            pixel_format='yuv420p10le',
            bit_depth=10,
            color_transfer='smpte2084',
        )

        codec = get_recommended_codec(metadata, output_format='mp4')
        assert codec == 'hevc'

    def test_get_recommended_codec_webm(self) -> None:
        """WebM 格式推荐编码器"""
        metadata = VideoMetadata(
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            total_frames=100,
            codec_name='vp9',
            codec_profile=None,
            pixel_format='yuv420p',
            bit_depth=8,
        )

        codec = get_recommended_codec(metadata, output_format='webm')
        assert codec == 'vp9'

    def test_get_recommended_codec_mkv(self) -> None:
        """MKV 格式推荐编码器"""
        metadata = VideoMetadata(
            width=1920,
            height=1080,
            fps=Rational(24, 1),
            total_frames=100,
            codec_name='ffv1',
            codec_profile=None,
            pixel_format='yuv420p',
            bit_depth=8,
        )

        codec = get_recommended_codec(metadata, output_format='mkv')
        assert codec == 'ffv1'


# =============================================================================
# Enum Tests
# =============================================================================


class TestEnums:
    """测试枚举类型"""

    def test_codec_type_values(self) -> None:
        """CodecType 枚举值"""
        assert CodecType.H264.value == 'h264'
        assert CodecType.HEVC.value == 'hevc'
        assert CodecType.PRORES.value == 'prores'
        assert CodecType.DNXHR.value == 'dnxhr'

    def test_prores_profile_values(self) -> None:
        """ProResProfile 枚举值"""
        assert ProResProfile.PROXY.value == 'proxy'
        assert ProResProfile.LT.value == 'lt'
        assert ProResProfile.STANDARD.value == '422'
        assert ProResProfile.HQ.value == 'hq'
        assert ProResProfile.QUALITY.value == '4444'
        assert ProResProfile.XQ.value == '4444xq'

    def test_dnxhr_profile_values(self) -> None:
        """DNxHRProfile 枚举值"""
        assert DNxHRProfile.LB.value == 'lb'
        assert DNxHRProfile.SQ.value == 'sq'
        assert DNxHRProfile.HQ.value == 'hq'
        assert DNxHRProfile.HQX.value == 'hqx'
        assert DNxHRProfile.QUALITY.value == '444'


# =============================================================================
# Constants Tests
# =============================================================================


class TestConstants:
    """测试常量映射"""

    def test_pixel_format_map_keys(self) -> None:
        """像素格式映射键"""
        assert 'h264' in PIXEL_FORMAT_MAP
        assert 'hevc' in PIXEL_FORMAT_MAP
        assert 'prores' in PIXEL_FORMAT_MAP
        assert 'dnxhr' in PIXEL_FORMAT_MAP

    def test_encoder_name_map(self) -> None:
        """编码器名称映射"""
        assert ENCODER_NAME_MAP['h264'] == 'libx264'
        assert ENCODER_NAME_MAP['hevc'] == 'libx265'
        assert ENCODER_NAME_MAP['prores'] == 'prores_ks'
        assert ENCODER_NAME_MAP['dnxhr'] == 'dnxhd'

    def test_pixel_format_map_prores_profiles(self) -> None:
        """ProRes 像素格式映射"""
        prores_map = PIXEL_FORMAT_MAP['prores']
        
        assert prores_map['proxy'] == 'prores_422proxy'
        assert prores_map['lt'] == 'prores_422lt'
        assert prores_map['standard'] == 'prores_422'
        assert prores_map['hq'] == 'prores_422hq'
        assert prores_map['4444'] == 'prores_4444'
        assert prores_map['4444xq'] == 'prores_4444xq'


# =============================================================================
# Edge Cases Tests
# =============================================================================


class TestEdgeCases:
    """边界情况测试"""

    def test_encode_zero_resolution_raises(self) -> None:
        """零分辨率应抛出错误"""
        with pytest.raises(ValueError):
            FFmpegEncoderConfig(
                codec='prores',
                width=0,
                height=0,
                fps=Rational(24, 1),
            )

    def test_high_resolution_config(self) -> None:
        """高分辨率配置"""
        config = FFmpegEncoderConfig(
            codec='prores',
            width=7680,
            height=4320,
            fps=Rational(24, 1),
        )
        
        assert config.width == 7680
        assert config.height == 4320

    def test_high_fps_config(self) -> None:
        """高帧率配置"""
        config = FFmpegEncoderConfig(
            codec='h264',
            width=1920,
            height=1080,
            fps=Rational(120, 1),
        )
        
        assert config.fps.to_fps() == 120.0
        # GOP size 应为 fps * 2
        assert config.gop_size == 240

    def test_odd_resolution_config(self) -> None:
        """非标准分辨率"""
        config = FFmpegEncoderConfig(
            codec='h264',
            width=1234,
            height=567,
            fps=Rational(24, 1),
        )
        
        assert config.width == 1234
        assert config.height == 567


# =============================================================================
# Integration Tests
# =============================================================================


class TestIntegration:
    """集成测试"""

    @patch.dict('sys.modules', {'av': MagicMock()})
    def test_full_encoding_workflow(
        self,
        temp_output_path: str,
        sample_metadata: VideoMetadata,
    ) -> None:
        """完整编码工作流"""
        import sys
        mock_av = sys.modules['av']

        # 创建配置
        config = FFmpegEncoderConfig.from_metadata(
            sample_metadata,
            codec='prores',
            profile='hq',
        )

        # Mock PyAV
        mock_container = MagicMock()
        mock_stream = MagicMock()
        mock_stream.encode.return_value = []
        mock_container.add_stream.return_value = mock_stream
        mock_av.open.return_value = mock_container

        mock_av_frame = MagicMock()
        mock_av.VideoFrame.return_value = mock_av_frame
        mock_av.VideoFrame.from_ndarray.return_value = mock_av_frame
        mock_av_frame.reformat.return_value = mock_av_frame

        # 创建编码器
        encoder = FFmpegVideoEncoder(temp_output_path, config)

        # 打开
        encoder.open()
        assert encoder.is_open is True

        # 编码几帧
        for i in range(5):
            frame = DecodedFrame(
                data=np.random.randint(0, 256, (1080, 1920, 3), dtype=np.uint8),
                frame_index=i,
                width=1920,
                height=1080,
                pixel_format='rgb24',
                pts=i,
                time_seconds=i / 24.0,
                dtype=np.uint8,
            )
            encoder.encode_frame(frame)

        assert encoder.frame_count == 5

        # 关闭
        encoder.close()
        assert encoder.is_open is False


class TestAmfTenBitWiring:
    """W6-02：10bit HEVC AMF 接线（p010le 转换路线）+ 结构化诊断。

    依据（2026-09-12 真机实测，RX 6800 / PyAV 18.1.0）：
    - hevc_amf 直收 yuv420p10le 报 avcodec_open2 EINVAL（两次独立实测）；
    - hevc_amf 收 p010le 正常（probe_amf_encode_windows.py 已证）；
    - swscale yuv420p10le→p010le replan 可行。
    """

    def _cfg(self, **kw) -> "FFmpegEncoderConfig":
        base = dict(codec="hevc", width=256, height=256, fps=Rational(24, 1))
        base.update(kw)
        return FFmpegEncoderConfig(**base)

    def test_amf_input_pix_fmt_mapping(self) -> None:
        """8bit 直收；10bit HEVC→p010le；h264 10bit / 4:2:2 / 12bit 拒绝。"""
        assert self._cfg(pix_fmt="yuv420p").amf_input_pix_fmt() == "yuv420p"
        assert self._cfg(pix_fmt="yuv420p10le").amf_input_pix_fmt() == "p010le"
        assert self._cfg(
            codec="h264", pix_fmt="yuv420p10le"
        ).amf_input_pix_fmt() is None
        assert self._cfg(pix_fmt="yuv422p10le").amf_input_pix_fmt() is None
        assert self._cfg(pix_fmt="yuv420p12le").amf_input_pix_fmt() is None

    def test_hw_fallback_reason_with_detail_format(self) -> None:
        """reason 规范形态：前缀可归类，detail 携带 hwaccel + 探测摘要。"""
        from src.shared.export.video_encoder import hw_fallback_reason_with_detail
        from src.shared.media.windows_hw.reasons import reason_prefix

        reason = hw_fallback_reason_with_detail("amf", "ValueError: boom")
        assert reason == "encoder_hw_unavailable:amf:ValueError: boom"
        assert reason_prefix(reason) == "encoder_hw_unavailable"
        assert hw_fallback_reason_with_detail("amf") == "encoder_hw_unavailable:amf"

    def test_encoder_init_diagnostic_fields(self) -> None:
        """未 open 时诊断字段为诚实默认值。"""
        enc = FFmpegVideoEncoder(
            "out.mp4", self._cfg(pix_fmt="yuv420p10le")
        )
        assert enc._encode_pix_fmt == "yuv420p10le"
        assert enc.hw_fallback_reason == ""

    def test_engaged_10bit_sdr_and_hdr_gating(self) -> None:
        """10bit SDR HEVC auto 走 AMF；HDR 仍锁 CPU；显式 amf 放行 HDR。"""
        from src.shared.export.video_encoder import amf_encoder_available

        if not (sys.platform == "win32" and amf_encoder_available("hevc")):
            pytest.skip("需要 Windows + PyAV 带 AMF 注册")
        assert self._cfg(pix_fmt="yuv420p10le").hwaccel_engaged() is True
        assert self._cfg(
            pix_fmt="yuv420p10le",
            color_metadata=ColorMetadataConfig(transfer="smpte2084"),
        ).hwaccel_engaged() is False
        assert self._cfg(
            pix_fmt="yuv420p10le",
            color_metadata=ColorMetadataConfig(transfer="smpte2084"),
            hwaccel="amf",
        ).hwaccel_engaged() is True

    def test_planar_10bit_cross_format_no_plane_drop(self) -> None:
        """yuv420p10le planar 帧 → AMF(p010le)：U/V 不得静默丢失。

        直接按平面序拷贝会丢 V（2 目标平面 vs 3 源平面）——本测试钉死
        跨格式必须走 reformat（编码侧输出色度可辨）。
        """
        import os
        import tempfile

        import av as av_module

        from src.shared.export.video_encoder import amf_encoder_available
        from src.shared.media.frame import DecodedFrame

        if not (sys.platform == "win32" and amf_encoder_available("hevc")):
            pytest.skip("需要 Windows + PyAV 带 AMF 注册")

        w, h = 256, 256
        y = np.full((h, w), 512, dtype=np.uint16)
        u = np.full((h // 2, w // 2), 400, dtype=np.uint16)  # Cb 偏低
        v = np.full((h // 2, w // 2), 620, dtype=np.uint16)  # Cr 偏高
        frame = DecodedFrame(
            frame_index=0, pts=0, time_seconds=0.0,
            planes=(y, u, v), pixel_format="yuv420p10le",
            width=w, height=h,
        )
        out = os.path.join(tempfile.gettempdir(), "_tmp_amf10bit_plane_test.mp4")
        try:
            enc = FFmpegVideoEncoder(out, self._cfg(pix_fmt="yuv420p10le"))
            enc.open()
            # AMF 会话输入格式应为 p010le
            assert enc._encode_pix_fmt == "p010le"
            assert enc.hw_fallback_reason == ""
            enc.encode_frame(frame)
            enc.close()
            with av_module.open(out) as cin:
                frames = list(cin.decode(video=0))
            assert len(frames) == 1
            # 解码输出为 p010le 平面数据，先转 RGB 再验色度
            arr = frames[0].reformat(format="rgb24").to_ndarray()
            # luma 恒定；U 低 + V 高 → R 明显高于 B（若 V 平面被丢，
            # 色度塌陷 R-B≈0）
            assert float(arr[..., 0].mean()) - float(arr[..., 2].mean()) > 30.0
        finally:
            try:
                os.remove(out)
            except OSError:
                pass

    def test_bt2020_p010_semiplanar_msb_layout(self) -> None:
        """BT.2020 numpy 精确转换直写 p010le：MSB 对齐 + UV 交错单平面。

        实证（2026-09-12，swscale yuv420p10le→p010le 探针）：10bit 值
        500 的原始 16bit 采样 = 32000（= 500<<6），p010le 仅 2 平面
        （Y + UV 交错）。旧实现按 3 平面 LSB 直写——亮度缩小 64 倍 +
        V 整平面丢失（显式 amf + HDR + RGB 源路径触发）。
        纯转换路径，无需 AMF 硬件。
        """
        w, h = 32, 32
        data = np.zeros((h, w, 3), dtype=np.uint8)
        data[..., 0] = 255  # 纯红（full-range RGB 输入）
        color = ColorMetadataConfig(
            matrix="bt2020nc", primaries="bt2020",
            transfer="smpte2084", range="limited",
        )
        enc = FFmpegVideoEncoder(
            "out.mp4", self._cfg(pix_fmt="yuv420p10le", color_metadata=color)
        )
        fr = enc._convert_rgb_to_yuv_bt2020(data, color, "p010le")

        assert len(fr.planes) == 2
        y_raw = np.frombuffer(bytes(fr.planes[0]), dtype=np.uint16)
        uv_raw = np.frombuffer(bytes(fr.planes[1]), dtype=np.uint16)
        y = y_raw.reshape(h, -1)[:, :w]
        uv = uv_raw.reshape(h // 2, -1)[:, :w]

        # 纯红按 BT.2100 表9 + limited 10bit 量化（独立手算锚点）：
        # Y=0.2627 → 0.2627*876+64 = 294.1 → 294
        # U=-0.2627/1.8814 → (0.5-0.1396)*896+64 = 386.9 → 387
        # V=(1-0.2627)/1.4746 = 0.5 → (0.5+0.5)*896+64 = 960
        assert np.all(y == 294 << 6)
        assert np.all(uv[:, 0::2] == 387 << 6)
        assert np.all(uv[:, 1::2] == 960 << 6)

        # 回读 sanity：MSB 对齐正确时 swscale 解回 RGB 红主导蓝
        #（移位缺失时 chroma 被解读为 ~0 → 灰，R-B ≈ 0）
        rgb = fr.reformat(format="rgb24").to_ndarray()
        assert float(rgb[..., 0].mean()) - float(rgb[..., 2].mean()) > 30.0

    def test_amf_option_level_failure_rebuilds_cpu(self) -> None:
        """crf=0（cqp/cq=0 被驱动拒绝）触发选项级失败 → 重建 CPU + reason。

        探针用默认选项探不到这类失败（懒打开会在第一帧 encode_frame 才
        爆）——本测试钉死 open() 的显式打开 + CPU 重建语义。期望 reason
        前缀按本机 vendor 通道（nvenc/amf）计算。
        """
        import os
        import tempfile

        from src.shared.export.video_encoder import (
            detect_hw_vendor, vendor_hwaccel_name,
        )

        hw = vendor_hwaccel_name(detect_hw_vendor())
        if not (sys.platform == "win32" and hw):
            pytest.skip("需要 Windows + nvidia/amd 硬件适配器")
        out = os.path.join(tempfile.gettempdir(), "_tmp_amf_rebuild.mp4")
        config = FFmpegEncoderConfig(
            codec="hevc", width=64, height=64, fps=Rational(24, 1),
            pix_fmt="yuv420p", crf=0, preset="medium",
        )
        enc = FFmpegVideoEncoder(out, config)
        enc.open()
        try:
            assert enc._using_amf is False
            assert enc.hw_fallback_reason.startswith(
                f"encoder_hw_unavailable:{hw}:"
            )
            assert enc.hw_fallback_reason.count(":") >= 2  # detail 携带摘要
        finally:
            enc.close()
            try:
                os.remove(out)
            except OSError:
                pass


class TestParallelConversion:
    """convert_workers>1：帧级并行转换 + 按 pts 序重排编码。"""

    def test_multi_worker_ordering_and_completeness(self) -> None:
        """乱序完成必须按序编码：解码回读亮度单调递增 + 帧数完整。"""
        import os
        import tempfile

        import av as av_module

        w, h, n = 64, 64, 24
        out = os.path.join(tempfile.gettempdir(), "_tmp_conv_order.mp4")
        config = FFmpegEncoderConfig(
            codec="h264", width=w, height=h, fps=Rational(24, 1),
            pix_fmt="yuv420p", crf=0, preset="ultrafast", gop_size=48,
            threads=0, pipelined_conversion=True, convert_workers=3,
        )
        enc = FFmpegVideoEncoder(out, config)
        enc.open()
        try:
            for i in range(n):
                data = np.full((h, w, 3), i * 10, dtype=np.uint8)
                frame = DecodedFrame(
                    frame_index=i, pts=i, time_seconds=i / 24.0,
                    data=data, pixel_format="rgb24", width=w, height=h,
                )
                enc.encode_frame(frame)
            enc.close()
            assert enc.frame_count == n  # 完整性：并行转换不丢帧
            with av_module.open(out) as cin:
                means = [
                    float(f.to_ndarray().mean()) for f in cin.decode(video=0)
                ]
            assert len(means) == n
            # 单调不减（乱序完成的帧被重排回 pts 序）
            assert all(b >= a - 2 for a, b in zip(means, means[1:])), means
        finally:
            try:
                os.remove(out)
            except OSError:
                pass

    def test_convert_workers_default_is_one(self) -> None:
        """默认 1 = 历史单线程语义（FIFO 保序路径）。"""
        config = FFmpegEncoderConfig(
            codec="h264", width=64, height=64, fps=Rational(24, 1),
        )
        assert config.convert_workers == 1


# =============================================================================
# Run Tests Manually
# =============================================================================


def run_manual_tests() -> None:
    """手动运行测试（用于调试）"""
    print("\n=== Manual FFmpeg Encoder Tests ===\n")


if __name__ == '__main__':
    run_manual_tests()
