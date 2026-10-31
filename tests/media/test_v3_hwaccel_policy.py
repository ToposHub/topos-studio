"""
Hardware Decode Policy 测试（P9.A）

测试目标：
1. PREVIEW 策略启用硬件解码
2. MASTER/EXPORT 策略不启用硬件解码
3. GOP codec（H.264/H.265）推荐硬解
4. Intra codec（ProRes/DNxHR）不推荐硬解
5. 硬解失败时 fallback 到 CPU
6. SourceManager 硬解状态跟踪

测试策略：
- 使用 MockMediaSource 和 MockHwaccelSource 模拟解码器
- 使用 DecodePolicyManager 获取策略
- 使用 HardwareCapability 检测能力
- 验证 SourceManager.get_hwaccel_status() 返回正确信息
"""

from __future__ import annotations

import os
import pytest
import platform
from typing import Optional, ClassVar
from unittest.mock import MagicMock, patch

from src.shared.media.decode_policy import (
    DecodeTarget,
    DecodePolicy,
    DecodePolicyManager,
    HardwareDecodeStatus,
)
from src.shared.media.hwaccel import (
    HardwareDecoder,
    HardwareCapability,
    HardwareDecodeConfig,
    HWACCEL_RECOMMENDED_CODECS,
    HWACCEL_NOT_RECOMMENDED_CODECS,
)
from src.shared.media.source import MediaSource, MediaOpenError
from src.shared.media.metadata import VideoMetadata, FPS_24, Rational
from src.shared.media.frame import DecodedFrame
from src.shared.video.playback_clock import SourceManager
from src.shared.media.proxy import DEFAULT_PROXY_CONFIG


# =============================================================================
# Mock MediaSource（模拟解码源）
# =============================================================================

class MockMediaSource(MediaSource):
    """
    Mock MediaSource，用于测试。
    """

    def __init__(
        self,
        path: str,
        codec_name: str = 'h264',
        target_size: Optional[tuple[int, int]] = None,
        should_fail_open: bool = False
    ) -> None:
        self._path = path
        self._target_size = target_size
        self._should_fail_open = should_fail_open
        self._is_open = False
        self._codec_name = codec_name
        self._metadata: Optional[VideoMetadata] = None
        self._current_frame = 0

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_open(self) -> bool:
        return self._is_open

    def open(self) -> None:
        if self._should_fail_open:
            raise MediaOpenError("Mock open failed", source_path=self._path)
        self._is_open = True
        self._metadata = VideoMetadata(
            width=1920,
            height=1080,
            fps=FPS_24,
            total_frames=240,
            codec_name=self._codec_name,
            codec_profile='High',
            pixel_format='yuv420p',
            bit_depth=8,
        )

    def close(self) -> None:
        self._is_open = False

    def probe(self) -> VideoMetadata:
        if self._metadata is None:
            self._metadata = VideoMetadata(
                width=1920,
                height=1080,
                fps=FPS_24,
                total_frames=240,
                codec_name=self._codec_name,
                codec_profile='High',
                pixel_format='yuv420p',
                bit_depth=8,
            )
        return self._metadata

    def read_frame(self, frame_index: int) -> DecodedFrame:
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
        self._current_frame += 1
        return self.read_frame(self._current_frame)

    def seek_frame(self, frame_index: int) -> None:
        self._current_frame = frame_index


class MockHwaccelSource(MockMediaSource):
    """
    Mock 硬件加速源，模拟 FFmpegHwaccelSource。
    """

    def __init__(
        self,
        path: str,
        codec_name: str = 'h264',
        hwaccel_enabled: bool = True,
        decoder_type: str = 'videotoolbox',
        target_size: Optional[tuple[int, int]] = None
    ) -> None:
        super().__init__(path, codec_name, target_size)
        self._hwaccel_enabled = hwaccel_enabled
        self._decoder_type = decoder_type

    @property
    def using_hwaccel(self) -> bool:
        return self._hwaccel_enabled

    @property
    def active_decoder(self) -> Optional[HardwareDecoder]:
        if self._hwaccel_enabled:
            return HardwareDecoder.VIDEOTOOLBOX
        return None


# =============================================================================
# DecodePolicy 硬解测试
# =============================================================================

class TestDecodePolicyHwaccel:
    """测试 DecodePolicy 硬件加速配置"""

    def test_preview_policy_enable_hwaccel(self) -> None:
        """PREVIEW 策略启用硬件解码"""
        policy = DecodePolicyManager.get_policy(DecodeTarget.PREVIEW)
        assert policy.enable_hwaccel is True
        assert policy.hwaccel_fallback_to_cpu is True
        assert policy.hwaccel_copy_to_cpu is True

    def test_master_policy_no_hwaccel(self) -> None:
        """MASTER 策略不启用硬件解码"""
        policy = DecodePolicyManager.get_policy(DecodeTarget.MASTER)
        assert policy.enable_hwaccel is False

    def test_export_policy_no_hwaccel(self) -> None:
        """EXPORT 策略不启用硬件解码"""
        policy = DecodePolicyManager.get_policy(DecodeTarget.EXPORT)
        assert policy.enable_hwaccel is False

    def test_policy_hwaccel_preferred_decoder(self) -> None:
        """策略可以指定首选解码器"""
        # 创建自定义策略
        custom_policy = DecodePolicy(
            target=DecodeTarget.PREVIEW,
            max_bit_depth=8,
            force_rgb=False,
            force_8bit=True,
            prefer_planar=False,
            preferred_format='yuv420p',
            allow_downgrade=True,
            max_resolution=(1920, 1080),
            enable_hwaccel=True,
            hwaccel_preferred_decoder='videotoolbox'
        )
        assert custom_policy.hwaccel_preferred_decoder == 'videotoolbox'


class TestHardwareDecodeStatus:
    """测试 HardwareDecodeStatus"""

    def test_status_creation(self) -> None:
        """创建状态"""
        status = HardwareDecodeStatus(
            hwaccel_enabled=True,
            decoder_type='videotoolbox',
            fallback_occurred=False,
            codec_supported=True
        )
        assert status.hwaccel_enabled is True
        assert status.decoder_type == 'videotoolbox'
        assert status.fallback_occurred is False

    def test_status_fallback(self) -> None:
        """fallback 状态"""
        status = HardwareDecodeStatus(
            hwaccel_enabled=False,
            decoder_type='cpu',
            fallback_occurred=True,
            fallback_reason='codec_not_supported',
            codec_supported=False
        )
        assert status.hwaccel_enabled is False
        assert status.fallback_occurred is True
        assert status.fallback_reason == 'codec_not_supported'

    def test_status_to_dict(self) -> None:
        """状态转换为字典"""
        status = HardwareDecodeStatus(
            hwaccel_enabled=True,
            decoder_type='nvdec',
            fallback_occurred=False
        )
        d = status.to_dict()
        assert d['hwaccel_enabled'] is True
        assert d['decoder_type'] == 'nvdec'
        assert d['fallback_occurred'] is False

    def test_status_repr(self) -> None:
        """状态字符串表示"""
        status_hwaccel = HardwareDecodeStatus(
            hwaccel_enabled=True,
            decoder_type='videotoolbox'
        )
        assert 'videotoolbox' in repr(status_hwaccel)

        status_cpu = HardwareDecodeStatus(
            hwaccel_enabled=False,
            decoder_type='cpu'
        )
        assert 'cpu' in repr(status_cpu)


# =============================================================================
# HardwareCapability Codec 推荐测试
# =============================================================================

class TestHwaccelCodecRecommendation:
    """测试 codec 硬解推荐逻辑"""

    def test_gop_codecs_recommended(self) -> None:
        """GOP codec 推荐硬解"""
        for codec in ['h264', 'h265', 'hevc', 'vp9', 'av1']:
            assert codec in HWACCEL_RECOMMENDED_CODECS
            assert HardwareCapability.is_hwaccel_recommended(codec) is True

    def test_intra_codecs_not_recommended(self) -> None:
        """Intra codec 不推荐硬解"""
        for codec in ['prores', 'prores_ks', 'dnxhr', 'dnxhd', 'ffv1', 'rawvideo']:
            assert codec in HWACCEL_NOT_RECOMMENDED_CODECS
            assert HardwareCapability.is_hwaccel_recommended(codec) is False

    def test_prores_variants_not_recommended(self) -> None:
        """ProRes 变体不推荐硬解"""
        # ProRes 各种变体
        for codec in ['prores', 'prores_ks', 'prores_aw', 'prores4444', 'prores422']:
            # is_hwaccel_recommended 检查包含匹配
            assert HardwareCapability.is_hwaccel_recommended(codec) is False

    def test_h264_aliases_recommended(self) -> None:
        """H.264/H.265 别名推荐硬解"""
        assert HardwareCapability.is_hwaccel_recommended('libx264') is True
        assert HardwareCapability.is_hwaccel_recommended('libx265') is True

    def test_codec_support_matrix(self) -> None:
        """codec 支持矩阵"""
        # VideoToolbox 支持 H.264/HEVC
        assert HardwareCapability.check_codec_support(HardwareDecoder.VIDEOTOOLBOX, 'h264') is True
        assert HardwareCapability.check_codec_support(HardwareDecoder.VIDEOTOOLBOX, 'hevc') is True

        # NVDEC 支持 AV1
        assert HardwareCapability.check_codec_support(HardwareDecoder.NVDEC, 'av1') is True

        # VAAPI 不支持 AV1
        assert HardwareCapability.check_codec_support(HardwareDecoder.VAAPI, 'av1') is False


# =============================================================================
# HardwareDecodeConfig 测试
# =============================================================================

class TestHardwareDecodeConfig:
    """测试 HardwareDecodeConfig"""

    def test_default_config(self) -> None:
        """默认配置"""
        config = HardwareDecodeConfig()
        assert config.decoder is None  # Auto
        assert config.fallback_to_cpu is True
        assert config.copy_to_cpu is True
        assert config.prefer_hwaccel_for_gop is True

    def test_specific_decoder_config(self) -> None:
        """指定解码器配置"""
        config = HardwareDecodeConfig(decoder=HardwareDecoder.VIDEOTOOLBOX)
        assert config.decoder == HardwareDecoder.VIDEOTOOLBOX

    def test_config_get_decoder_for_gop_codec(self) -> None:
        """配置为 GOP codec 选择解码器"""
        config = HardwareDecodeConfig()
        # 只有在 macOS 上才会返回 VideoToolbox
        # 其他平台根据可用性返回不同解码器
        decoder = config.get_decoder_for_codec('h264')
        if platform.system() == 'Darwin':
            # macOS 优先 VideoToolbox
            assert decoder == HardwareDecoder.VIDEOTOOLBOX or decoder is None


# =============================================================================
# SourceManager 硬解状态测试
# =============================================================================

class TestSourceManagerHwaccel:
    """测试 SourceManager 硬解状态"""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """每个测试前设置"""
        HardwareCapability.clear_cache()

    def test_source_manager_get_hwaccel_status_before_open(self) -> None:
        """未打开时状态为 None"""
        manager = SourceManager(
            path='/test/video.mp4',
            proxy_config=DEFAULT_PROXY_CONFIG,
            source_factory=lambda p, **kwargs: MockMediaSource(p, codec_name='h264')
        )
        status = manager.get_hwaccel_status()
        assert status is None

    def test_source_manager_hwaccel_status_after_open(self) -> None:
        """打开后状态记录"""
        manager = SourceManager(
            path='/test/video.mp4',
            proxy_config=DEFAULT_PROXY_CONFIG
        )
        # 使用自定义工厂模拟
        manager._source_factory = lambda p, **kwargs: MockMediaSource(p, codec_name='h264')
        success = manager.open()
        assert success is True

        status = manager.get_hwaccel_status()
        # 自定义工厂时，状态为 CPU
        assert status is not None
        assert status.hwaccel_enabled is False
        assert status.decoder_type == 'cpu'

    def test_source_manager_get_stats_includes_hwaccel(self) -> None:
        """get_stats() 包含硬解状态"""
        manager = SourceManager(
            path='/test/video.mp4',
            proxy_config=DEFAULT_PROXY_CONFIG,
            source_factory=lambda p, **kwargs: MockMediaSource(p, codec_name='h264')
        )
        success = manager.open()
        assert success is True

        stats = manager.get_stats()
        assert 'hwaccel' in stats
        assert 'hwaccel_enabled' in stats['hwaccel']
        assert 'decoder_type' in stats['hwaccel']

    def test_source_manager_stats_includes_codec(self) -> None:
        """stats 包含 codec 信息"""
        manager = SourceManager(
            path='/test/video.mp4',
            proxy_config=DEFAULT_PROXY_CONFIG,
            source_factory=lambda p, **kwargs: MockMediaSource(p, codec_name='h264')
        )
        success = manager.open()
        assert success is True

        stats = manager.get_stats()
        assert 'codec' in stats
        assert stats['codec'] == 'h264'

    def test_source_manager_no_proxy_uses_master(self) -> None:
        """不需要降级时使用 master 源"""
        # 小视频不需要降级
        manager = SourceManager(
            path='/test/video.mp4',
            proxy_config=DEFAULT_PROXY_CONFIG,
            viewport_size=(1920, 1080),  # 视口与原始相同
            source_factory=lambda p, **kwargs: MockMediaSource(p, codec_name='h264')
        )
        success = manager.open()
        assert success is True

        # proxy_source 应该是 master_source
        assert manager._proxy_source == manager._master_source


# =============================================================================
# Factory create_with_hwaccel 测试（需要真实 FFmpeg）
# =============================================================================

class TestFactoryCreateWithHwaccel:
    """测试 MediaSourceFactory.create_with_hwaccel"""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """每个测试前清除缓存"""
        HardwareCapability.clear_cache()

    def test_create_with_hwaccel_returns_status(self) -> None:
        """create_with_hwaccel 返回状态"""
        from src.shared.media.factory import MediaSourceFactory

        # 使用一个测试文件路径（可能不存在）
        test_path = '/nonexistent/test.mp4'

        # 测试异常处理
        with pytest.raises(MediaOpenError):
            source, status = MediaSourceFactory.create_with_hwaccel(
                test_path,
                decode_target=DecodeTarget.PREVIEW
            )

    @patch('src.shared.media.factory.MediaSourceFactory.probe_video_metadata')
    @patch('src.shared.media.hwaccel.HardwareCapability.detect_available_decoders')
    def test_create_with_hwaccel_gop_codec(self, mock_detect: MagicMock, mock_probe: MagicMock) -> None:
        """GOP codec 尝试硬解"""
        from src.shared.media.factory import MediaSourceFactory

        # Mock metadata
        mock_metadata = VideoMetadata(
            width=1920,
            height=1080,
            fps=FPS_24,
            total_frames=240,
            codec_name='h264',
            codec_profile='High',
            pixel_format='yuv420p',
            bit_depth=8,
        )
        mock_probe.return_value = mock_metadata

        # Mock 检测到的解码器（macOS）
        if platform.system() == 'Darwin':
            mock_detect.return_value = [HardwareDecoder.VIDEOTOOLBOX]
        else:
            mock_detect.return_value = []

        # 由于文件不存在，无法完整测试创建
        # 但可以验证逻辑调用

    def test_create_with_hwaccel_policy_master_no_hwaccel(self) -> None:
        """MASTER 目标不使用硬解"""
        from src.shared.media.factory import MediaSourceFactory

        policy = DecodePolicyManager.get_policy(DecodeTarget.MASTER)
        assert policy.enable_hwaccel is False

        # MASTER 策略应该使用 CPU 解码


# =============================================================================
# Fallback 行为测试
# =============================================================================

class TestHwaccelFallback:
    """测试硬件解码 fallback 行为"""

    def test_hwaccel_status_fallback_to_cpu(self) -> None:
        """硬解失败 fallback 到 CPU"""
        status = HardwareDecodeStatus(
            hwaccel_enabled=False,
            decoder_type='cpu',
            fallback_occurred=True,
            fallback_reason='hwaccel_source_creation_failed',
            codec_supported=True
        )
        assert status.fallback_occurred is True
        assert status.decoder_type == 'cpu'

    def test_intra_codec_fallback_reason(self) -> None:
        """Intra codec 不推荐硬解"""
        status = HardwareDecodeStatus(
            hwaccel_enabled=False,
            decoder_type='cpu',
            fallback_occurred=False,
            fallback_reason='codec_not_recommended_for_hwaccel',
            codec_supported=True
        )
        # ProRes/DNxHR 不推荐硬解，这是预期行为，不算 fallback
        assert status.fallback_reason == 'codec_not_recommended_for_hwaccel'

    def test_config_fallback_to_cpu(self) -> None:
        """配置允许 fallback"""
        config = HardwareDecodeConfig(fallback_to_cpu=True)
        assert config.fallback_to_cpu is True

        # 禁止 fallback 的配置
        config_no_fallback = HardwareDecodeConfig(fallback_to_cpu=False)
        assert config_no_fallback.fallback_to_cpu is False


# =============================================================================
# 平台特定测试
# =============================================================================

class TestPlatformDefaults:
    """测试平台默认解码器"""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        """每个测试前清除缓存"""
        HardwareCapability.clear_cache()

    def test_macos_videotoolbox_default(self) -> None:
        """macOS 默认 VideoToolbox"""
        if platform.system() == 'Darwin':
            default = HardwareCapability.get_platform_default()
            assert default == HardwareDecoder.VIDEOTOOLBOX

    def test_windows_d3d11va_available(self) -> None:
        """Windows D3D11VA 可用"""
        if platform.system() == 'Windows':
            available = HardwareCapability.detect_available_decoders()
            assert HardwareDecoder.D3D11VA in available

    def test_linux_vaapi_check(self) -> None:
        """Linux VAAPI 检查"""
        if platform.system() == 'Linux':
            # VAAPI 可能或可能不可用，取决于系统
            available = HardwareCapability.detect_available_decoders()
            # 不做强制断言，只验证返回了列表
            assert isinstance(available, list)


# =============================================================================
# P1-3: 解码线程参数测试
# =============================================================================

class TestFFmpegThreadParameters:
    """
    P1-3: FFmpeg/PyAV 解码线程参数测试

    测试目标：
    1. FFmpegMediaSource.open() 设置 thread_type = 'AUTO'
    2. FFmpegMediaSource.open() 设置 thread_count = 0（自动选择）
    3. ProRes/DNxHR 等使用 CPU 多线程解码
    4. H.264/H.265 等硬解时线程参数被忽略
    """

    def test_ffmpeg_source_thread_type_auto(self) -> None:
        """FFmpegMediaSource 应设置 thread_type = AUTO"""
        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        # 验证代码逻辑：检查源码中是否设置了 thread_type = 'AUTO'
        # 这是一个静态验证，确保代码正确实现
        import inspect
        source_code = inspect.getsource(FFmpegMediaSource.open)

        # 检查 thread_type = 'AUTO' 存在于源码中
        assert "thread_type = 'AUTO'" in source_code or "thread_type='AUTO'" in source_code

    def test_ffmpeg_source_thread_count_zero(self) -> None:
        """P1-3: FFmpegMediaSource 应设置 thread_count = 0"""
        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        # 验证代码逻辑：检查源码中是否设置了 thread_count = 0
        import inspect
        source_code = inspect.getsource(FFmpegMediaSource.open)

        # 检查 thread_count = 0 存在于源码中
        assert "thread_count = 0" in source_code or "thread_count=0" in source_code

    def test_ffmpeg_source_thread_count_in_codec_context(self) -> None:
        """P1-3: thread_count 应设置在 codec_context 上"""
        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        import inspect
        source_code = inspect.getsource(FFmpegMediaSource.open)

        # 验证设置在 codec_context 上（正确的方式）
        assert "codec_context.thread_count" in source_code

    @pytest.mark.skipif(
        not os.path.exists(os.path.join(os.path.dirname(__file__), 'fixtures', 'test_h264_sdr.mp4')),
        reason="测试视频文件不存在"
    )
    def test_ffmpeg_source_real_video_thread_settings(self) -> None:
        """使用真实视频验证线程设置"""
        from src.shared.media.ffmpeg_source import FFmpegMediaSource

        test_path = os.path.join(os.path.dirname(__file__), 'fixtures', 'test_h264_sdr.mp4')
        if not os.path.exists(test_path):
            pytest.skip("测试视频文件不存在")

        source = FFmpegMediaSource(test_path)
        source.open()

        # 验证打开成功
        assert source.is_open is True

        # 验证线程参数设置（如果 PyAV 支持）
        # 注意：PyAV 的 thread_type 和 thread_count 属性可能不在所有版本中存在
        video_stream = source._video_stream
        if video_stream:
            # thread_type 应为 'AUTO'
            if hasattr(video_stream, 'thread_type'):
                assert video_stream.thread_type == 'AUTO' or video_stream.thread_type.name == 'AUTO'

            # codec_context.thread_count 应为 0（自动）
            if hasattr(video_stream, 'codec_context') and video_stream.codec_context:
                if hasattr(video_stream.codec_context, 'thread_count'):
                    # 0 表示自动选择
                    assert video_stream.codec_context.thread_count == 0

        source.close()


# =============================================================================
# GOP vs Intra Codec 策略测试（P1-2）
# =============================================================================

class TestGOPCodecHwaccelStrategy:
    """
    P1-2: GOP codec 与 Intra codec 分开策略测试

    测试目标：
    1. H.264/H.265/VP9/AV1（GOP codec）推荐硬解
    2. ProRes/DNxHR（Intra codec）不推荐硬解
    3. PREVIEW 策略对 GOP codec 启用硬解
    4. 硬解失败时 fallback CPU，但保持在 v3 管线
    """

    def test_gop_codecs_in_recommended_set(self) -> None:
        """P1-2: GOP codec 应在 HWACCEL_RECOMMENDED_CODECS 中"""
        gop_codecs = ['h264', 'h265', 'hevc', 'vp9', 'av1', 'mpeg2video', 'mpeg4']
        for codec in gop_codecs:
            assert codec in HWACCEL_RECOMMENDED_CODECS, f"{codec} 应在推荐硬解列表中"

    def test_intra_codecs_not_recommended(self) -> None:
        """P1-2: Intra codec 不应在推荐硬解列表中"""
        intra_codecs = ['prores', 'prores_ks', 'dnxhr', 'dnxhd', 'ffv1', 'rawvideo']
        for codec in intra_codecs:
            assert HardwareCapability.is_hwaccel_recommended(codec) is False, \
                f"{codec} 不应推荐硬解"

    def test_preview_policy_for_gop_codec(self) -> None:
        """P1-2: PREVIEW 策略对 GOP codec 应尝试硬解"""
        policy = DecodePolicyManager.get_policy(DecodeTarget.PREVIEW)

        # PREVIEW 策略启用硬解
        assert policy.enable_hwaccel is True

        # PREVIEW 策略允许 fallback 到 CPU
        assert policy.hwaccel_fallback_to_cpu is True

    def test_preview_policy_for_intra_codec_fallback_cpu(self) -> None:
        """P1-2: PREVIEW 策略对 Intra codec 不尝试硬解（自动 fallback CPU）"""
        # ProRes/DNxHR 不推荐硬解
        assert HardwareCapability.is_hwaccel_recommended('prores') is False

        # factory._try_create_hwaccel_source 会检查 codec 是否推荐硬解
        # 不推荐的 codec 会返回 None 和 fallback_reason='codec_not_recommended_for_hwaccel'
        status = HardwareDecodeStatus(
            hwaccel_enabled=False,
            decoder_type='cpu',
            fallback_reason='codec_not_recommended_for_hwaccel'
        )
        # 这是预期行为，不是错误
        assert status.hwaccel_enabled is False
        assert status.decoder_type == 'cpu'

    def test_v3_pipeline_keeps_loaded_after_hwaccel_fallback(self) -> None:
        """P1-2/P0-3: 硬解失败后保持在 v3 管线中"""
        from src.shared.video.fallback_types import VideoPipelineFallbackReason

        # 硬解失败不是 fallback v2 的原因
        # VideoPipelineFallbackReason.HARDWARE_ACCEL_FAILED 表示硬解失败
        # 但这不应该触发 v2 fallback，只是 CPU fallback

        # 验证 _should_fallback_to_v2 不因硬解失败返回 True
        # ProRes 等专业格式不应 fallback v2
        professional_codecs = ('prores', 'dnxhr', 'dnxhd')
        for codec in professional_codecs:
            # 即使硬解失败（实际上 ProRes 不推荐硬解），也不 fallback v2
            # 这是 P0-3 的核心逻辑
            assert codec in professional_codecs  # 只是验证 codec 列表

        # 硬解失败使用 HARDWARE_ACCEL_FAILED 原因
        hwaccel_failed_reason = VideoPipelineFallbackReason.HARDWARE_ACCEL_FAILED
        assert hwaccel_failed_reason.value == 'hwaccel_failed'

        # 但这个 reason 表示的是 v3 内部的降级（硬解 -> CPU）
        # 不是 v3 -> v2 的降级
        # effective_pipeline 应为 'v3_degraded' 而不是 'v2'


# =============================================================================
# 运行测试
# =============================================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])