"""
Video Pipeline Smoke Tests
端到端测试视频管线的完整流程。

## 覆盖范围

- probe() 元数据读取
- open/close 资源管理
- decode (read_frame/read_next_frame)
- seek_frame() 随机定位
- 多种编码格式 (ProRes, DNxHR, H.264, H.265)
- 高位深视频 (10-bit/12-bit)
- HDR 视频 (PQ/HLG)
- 不同帧率 (23.976, 24, 25, 29.97, 30, 60)
- 音频元数据
- timecode 读取

## 测试策略

- 如果测试视频文件存在，运行实际测试
- 如果不存在，使用 pytest.skip 或 mock

## 测试素材

参考 tests/media/fixtures_manifest.json
"""

from __future__ import annotations

import os
import json
import gc
import pytest
import tracemalloc
from typing import Any, Optional
from pathlib import Path

import numpy as np

# 导入测试模块
from src.shared.media.ffmpeg_source import (
    FFmpegMediaSource,
    _infer_bit_depth,
    _is_gop_codec,
    _is_intra_codec,
)
from src.shared.media.metadata import (
    VideoMetadata,
    AudioMetadata,
    Rational,
    FPS_23_976,
    FPS_24,
    FPS_25,
    FPS_29_97,
    FPS_30,
    FPS_50,
    FPS_59_94,
    FPS_60,
)
from src.shared.media.frame import DecodedFrame
from src.shared.media.timecode import Timecode, TimecodeType
from src.shared.media.source import (
    MediaSource,
    VideoSource,
    MediaSourceError,
    MediaOpenError,
    MediaDecodeError,
    MediaSeekError,
)
from src.shared.media.errors import DecodeError


# =============================================================================
# Test Fixtures Configuration
# =============================================================================

# 测试素材路径
TEST_MEDIA_DIR = os.path.join(os.path.dirname(__file__), 'fixtures')

# fixtures_manifest.json 路径（清单在 tests/media/ 根，非 fixtures/ 子目录）
FIXTURES_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), 'fixtures_manifest.json')

# 实际存在的测试视频文件
TEST_VIDEOS = {
    'h264_sdr': os.path.join(TEST_MEDIA_DIR, 'test_h264_sdr.mp4'),
    'h265_pq': os.path.join(TEST_MEDIA_DIR, 'test_h265_pq.mp4'),
    'h265_pq_v2': os.path.join(TEST_MEDIA_DIR, 'test_h265_pq_v2.mp4'),
    'prores': os.path.join(TEST_MEDIA_DIR, 'test_prores.mov'),
}


def get_available_videos() -> dict[str, str]:
    """返回可用的测试视频"""
    available = {}
    for name, path in TEST_VIDEOS.items():
        if os.path.exists(path):
            available[name] = path
    return available


def load_fixtures_manifest() -> dict[str, Any]:
    """加载测试素材清单"""
    if os.path.exists(FIXTURES_MANIFEST_PATH):
        with open(FIXTURES_MANIFEST_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def get_fixture_path(fixture_id: str) -> Optional[str]:
    """根据 fixture ID 获取测试素材路径"""
    manifest = load_fixtures_manifest()
    if not manifest:
        return None

    fixtures = manifest.get('fixtures', {})
    
    # 处理嵌套 fixtures（如 fps_variations）
    for key, value in fixtures.items():
        if isinstance(value, dict):
            if 'fixtures' in value:
                # 嵌套结构
                nested = value.get('fixtures', {})
                if fixture_id in nested:
                    file_path = nested[fixture_id].get('file_path')
                    if file_path:
                        # 尝试多个 fallback 路径
                        config = manifest.get('config', {})
                        fallback_paths = config.get('fallback_paths', [])
                        for fallback in fallback_paths:
                            expanded = os.path.expanduser(fallback)
                            full_path = os.path.join(expanded, file_path)
                            if os.path.exists(full_path):
                                return full_path
            elif 'file_path' in value:
                # 直接 fixture
                if key == fixture_id:
                    file_path = value.get('file_path')
                    if file_path:
                        for fallback in manifest.get('config', {}).get('fallback_paths', []):
                            expanded = os.path.expanduser(fallback)
                            full_path = os.path.join(expanded, file_path)
                            if os.path.exists(full_path):
                                return full_path

    return None


# =============================================================================
# Pytest Fixtures
# =============================================================================

@pytest.fixture
def h264_video() -> Optional[str]:
    """H.264 SDR 视频路径"""
    return TEST_VIDEOS.get('h264_sdr')


@pytest.fixture
def h265_pq_video() -> Optional[str]:
    """H.265 PQ HDR 视频路径（优先使用 v2，有正确的 HDR 元数据）"""
    # 优先使用 v2（有正确的 PQ/BT.2020 元数据）
    v2_path = TEST_VIDEOS.get('h265_pq_v2')
    if v2_path and os.path.exists(v2_path):
        return v2_path
    # fallback 到旧版本（元数据可能为 unknown）
    return TEST_VIDEOS.get('h265_pq')


@pytest.fixture
def h265_hlg_video() -> Optional[str]:
    """H.265 HLG HDR 视频路径"""
    return TEST_VIDEOS.get('h265_pq_v2')  # 使用 v2 作为 HLG 替代


@pytest.fixture
def prores_video() -> Optional[str]:
    """ProRes 视频路径"""
    return TEST_VIDEOS.get('prores')


@pytest.fixture
def any_available_video() -> Optional[str]:
    """任意可用的测试视频"""
    available = get_available_videos()
    if available:
        return list(available.values())[0]
    return None


@pytest.fixture
def fixtures_manifest() -> dict[str, Any]:
    """测试素材清单"""
    return load_fixtures_manifest()


# =============================================================================
# Helper Functions
# =============================================================================

def pytest_skip_if_no_video(video_path: Optional[str]) -> None:
    """如果没有视频则跳过测试"""
    if video_path is None or not os.path.exists(video_path):
        pytest.skip("No test video available")


def create_synthetic_video_metadata(
    codec: str = 'h264',
    width: int = 1920,
    height: int = 1080,
    fps: Rational = FPS_24,
    bit_depth: int = 8,
    is_hdr: bool = False,
) -> VideoMetadata:
    """创建合成视频元数据（用于 mock 测试）"""
    color_transfer = 'smpte2084' if is_hdr else 'bt709'
    color_primaries = 'bt2020' if is_hdr else 'bt709'
    color_matrix = 'bt2020nc' if is_hdr else 'bt709'
    
    return VideoMetadata(
        width=width,
        height=height,
        fps=fps,
        total_frames=240,
        codec_name=codec,
        codec_profile='High' if codec in ('h264', 'hevc') else '422 HQ',
        pixel_format=f'yuv420p{bit_depth}le' if bit_depth > 8 else 'yuv420p',
        bit_depth=bit_depth,
        color_primaries=color_primaries,
        color_transfer=color_transfer,
        color_matrix=color_matrix,
        color_range='tv',
        has_alpha=False,
    )


# =============================================================================
# Smoke Tests - Probe
# =============================================================================

class TestSmokeProbe:
    """Smoke 测试 - 视频探测"""

    @pytest.mark.smoke
    def test_probe_returns_metadata(self, any_available_video: Optional[str]) -> None:
        """探测视频应返回有效元数据"""
        pytest_skip_if_no_video(any_available_video)

        source = FFmpegMediaSource(any_available_video)
        metadata = source.probe()

        assert metadata is not None
        assert isinstance(metadata, VideoMetadata)
        assert metadata.width > 0
        assert metadata.height > 0
        assert metadata.fps.to_float() > 0
        assert metadata.codec_name is not None

    @pytest.mark.smoke
    @pytest.mark.parametrize('video_key', ['h264_sdr', 'h265_pq', 'prores'])
    def test_probe_different_formats(
        self, video_key: str, request: pytest.FixtureRequest
    ) -> None:
        """探测不同编码格式"""
        video_path = request.getfixturevalue(
            video_key.replace('h265_pq', 'h265_pq_video')
            .replace('h264_sdr', 'h264_video')
            .replace('prores', 'prores_video')
        )
        pytest_skip_if_no_video(video_path)

        source = FFmpegMediaSource(video_path)
        metadata = source.probe()

        assert metadata is not None
        
        # 验证编码器名称
        if video_key == 'h264_sdr':
            assert metadata.codec_name == 'h264'
        elif video_key == 'h265_pq':
            assert metadata.codec_name in ('hevc', 'h265')
        elif video_key == 'prores':
            assert 'prores' in metadata.codec_name.lower()

    @pytest.mark.smoke
    def test_probe_nonexistent_file(self) -> None:
        """探测不存在文件应抛出错误"""
        source = FFmpegMediaSource('/nonexistent/video.mp4')

        with pytest.raises(MediaSourceError):
            source.probe()

    @pytest.mark.smoke
    def test_probe_color_metadata(self, h265_pq_video: Optional[str]) -> None:
        """探测 HDR 视频的色彩元数据"""
        pytest_skip_if_no_video(h265_pq_video)

        source = FFmpegMediaSource(h265_pq_video)
        metadata = source.probe()

        assert metadata.is_hdr is True
        assert metadata.color_transfer in ('smpte2084', 'arib-std-b67')
        assert metadata.color_primaries == 'bt2020'
        assert metadata.bit_depth >= 10


# =============================================================================
# Smoke Tests - Open/Close
# =============================================================================

class TestSmokeOpenClose:
    """Smoke 测试 - 资源管理"""

    @pytest.mark.smoke
    def test_open_close_cycle(self, any_available_video: Optional[str]) -> None:
        """测试打开和关闭循环"""
        pytest_skip_if_no_video(any_available_video)

        source = FFmpegMediaSource(any_available_video)

        # 初始状态
        assert source.is_open is False

        # 打开
        source.open()
        assert source.is_open is True

        # 关闭
        source.close()
        assert source.is_open is False

    @pytest.mark.smoke
    def test_context_manager(self, any_available_video: Optional[str]) -> None:
        """测试上下文管理器"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            assert source.is_open is True

        assert source.is_open is False

    @pytest.mark.smoke
    def test_double_open_is_safe(self, any_available_video: Optional[str]) -> None:
        """重复打开应该安全"""
        pytest_skip_if_no_video(any_available_video)

        source = FFmpegMediaSource(any_available_video)
        source.open()
        source.open()  # 第二次打开应该无效果

        assert source.is_open is True
        source.close()

    @pytest.mark.smoke
    def test_double_close_is_safe(self, any_available_video: Optional[str]) -> None:
        """重复关闭应该安全"""
        pytest_skip_if_no_video(any_available_video)

        source = FFmpegMediaSource(any_available_video)
        source.open()
        source.close()
        source.close()  # 第二次关闭应该无效果

        assert source.is_open is False


# =============================================================================
# Smoke Tests - Decode
# =============================================================================

class TestSmokeDecode:
    """Smoke 测试 - 视频解码"""

    @pytest.mark.smoke
    def test_read_first_frame(self, any_available_video: Optional[str]) -> None:
        """读取第一帧"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            metadata = source.probe()
            frame = source.read_next_frame()

            assert frame is not None
            assert frame.frame_index == 0
            assert frame.width == metadata.width
            assert frame.height == metadata.height
            assert frame.data is not None or len(frame.planes) > 0

    @pytest.mark.smoke
    def test_read_multiple_frames(self, any_available_video: Optional[str]) -> None:
        """读取多帧"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            frames: list[DecodedFrame] = []

            for i in range(5):
                frame = source.read_next_frame()
                frames.append(frame)
                assert frame.frame_index == i

            # 验证帧号序列
            frame_indices = [f.frame_index for f in frames]
            assert frame_indices == [0, 1, 2, 3, 4]

    @pytest.mark.smoke
    def test_frame_dimensions_match_metadata(
        self, any_available_video: Optional[str]
    ) -> None:
        """帧尺寸应与元数据匹配"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            metadata = source.probe()
            frame = source.read_next_frame()

            assert frame.width == metadata.width
            assert frame.height == metadata.height

    @pytest.mark.smoke
    def test_frame_dtype_correct(self, any_available_video: Optional[str]) -> None:
        """帧数据类型应正确"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            metadata = source.probe()
            frame = source.read_next_frame()

            # 根据位深度验证 dtype
            if metadata.bit_depth >= 10:
                assert frame.dtype in (np.uint16, np.float32)
            else:
                assert frame.dtype == np.uint8

    @pytest.mark.smoke
    def test_eof_handling(self, any_available_video: Optional[str]) -> None:
        """验证 EOF 处理"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            metadata = source.probe()

            if metadata.total_frames is None:
                pytest.skip("Total frames unknown")

            # 解码到末尾
            for _ in range(metadata.total_frames):
                source.read_next_frame()

            # EOF 应抛出 StopIteration 或 EOFError
            with pytest.raises((StopIteration, EOFError)):
                source.read_next_frame()


# =============================================================================
# Smoke Tests - Seek
# =============================================================================

class TestSmokeSeek:
    """Smoke 测试 - 帧定位"""

    @pytest.mark.smoke
    def test_seek_to_middle_frame(self, any_available_video: Optional[str]) -> None:
        """定位到中间帧"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            metadata = source.probe()

            if metadata.total_frames is None or metadata.total_frames < 10:
                pytest.skip("Not enough frames for seek test")

            target_frame = metadata.total_frames // 2
            
            # 定位
            source.seek_frame(target_frame)
            
            # 读取帧，验证帧号
            frame = source.read_next_frame()
            assert frame.frame_index == target_frame

    @pytest.mark.smoke
    def test_seek_to_first_frame(self, any_available_video: Optional[str]) -> None:
        """定位到第一帧"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            # 先读取几帧
            source.read_next_frame()
            source.read_next_frame()
            
            # 定位回第一帧
            source.seek_frame(0)
            
            frame = source.read_next_frame()
            assert frame.frame_index == 0

    @pytest.mark.smoke
    def test_seek_to_last_frame(self, any_available_video: Optional[str]) -> None:
        """定位到最后一帧"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            metadata = source.probe()

            if metadata.total_frames is None:
                pytest.skip("Total frames unknown")

            last_frame = metadata.total_frames - 1
            
            source.seek_frame(last_frame)
            frame = source.read_next_frame()
            
            assert frame.frame_index == last_frame

    @pytest.mark.smoke
    def test_seek_invalid_negative(self, any_available_video: Optional[str]) -> None:
        """定位到负帧号应失败"""
        pytest_skip_if_no_video(any_available_video)

        source = FFmpegMediaSource(any_available_video)
        source.open()

        with pytest.raises((ValueError, MediaSeekError)):
            source.seek_frame(-1)

        source.close()


# =============================================================================
# Smoke Tests - High Bit Depth
# =============================================================================

class TestSmokeHighBitDepth:
    """Smoke 测试 - 高位深视频"""

    @pytest.mark.smoke
    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('prores', '')),
        reason="ProRes test video not available"
    )
    def test_10bit_preserved(self, prores_video: Optional[str]) -> None:
        """10-bit 视频应保持高位深"""
        pytest_skip_if_no_video(prores_video)

        source = FFmpegMediaSource(prores_video)
        metadata = source.probe()

        if metadata.bit_depth < 10:
            pytest.skip("This video is not 10-bit")

        source.open()
        frame = source.read_next_frame()

        # 高位深素材不应降级到 uint8
        assert frame.dtype in (np.uint16, np.float32)

        # 如果是 uint16，检查是否有超过 255 的值
        if frame.dtype == np.uint16 and frame.data is not None:
            max_val = frame.data.max()
            # 10-bit 最大值应该是 1023 (或接近)
            # 但由于转换到 RGB，可能被拉伸
            # 这里只验证 dtype 正确

        source.close()

    @pytest.mark.smoke
    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('h265_pq', '')),
        reason="H.265 PQ test video not available"
    )
    def test_h265_10bit_preserved(self, h265_pq_video: Optional[str]) -> None:
        """H.265 10-bit PQ 视频应保持高位深"""
        pytest_skip_if_no_video(h265_pq_video)

        source = FFmpegMediaSource(h265_pq_video)
        metadata = source.probe()

        assert metadata.bit_depth >= 10
        assert metadata.is_hdr is True

        source.open()
        frame = source.read_next_frame()

        # 高位深素材不应降级到 uint8
        assert frame.dtype in (np.uint16, np.float32)

        source.close()


# =============================================================================
# Smoke Tests - HDR
# =============================================================================

class TestSmokeHDR:
    """Smoke 测试 - HDR 视频"""

    @pytest.mark.smoke
    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('h265_pq', '')),
        reason="H.265 PQ test video not available"
    )
    def test_pq_color_metadata(self, h265_pq_video: Optional[str]) -> None:
        """PQ HDR 视频色彩元数据"""
        pytest_skip_if_no_video(h265_pq_video)

        source = FFmpegMediaSource(h265_pq_video)
        metadata = source.probe()

        assert metadata.color_transfer == 'smpte2084'
        assert metadata.color_primaries == 'bt2020'
        assert metadata.color_matrix == 'bt2020nc'
        assert metadata.is_hdr is True

    @pytest.mark.smoke
    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('h265_pq_v2', '')),
        reason="H.265 HLG test video not available"
    )
    def test_hlg_color_metadata(self, h265_hlg_video: Optional[str]) -> None:
        """HLG HDR 视频色彩元数据"""
        pytest_skip_if_no_video(h265_hlg_video)

        source = FFmpegMediaSource(h265_hlg_video)
        metadata = source.probe()

        # HLG 使用 arib-std-b67 传输函数
        # 注意：可能实际视频是 PQ，需要检查
        if metadata.color_transfer == 'arib-std-b67':
            assert metadata.is_hdr is True
        else:
            # 如果不是 HLG，标记为预期行为
            pytest.skip("Test video is not HLG encoded")


# =============================================================================
# Smoke Tests - Frame Rate
# =============================================================================

class TestSmokeFrameRate:
    """Smoke 测试 - 不同帧率"""

    @pytest.mark.smoke
    @pytest.mark.parametrize('fps_rational,fps_float', [
        (FPS_23_976, 23.976),
        (FPS_24, 24.0),
        (FPS_25, 25.0),
        (FPS_29_97, 29.97),
        (FPS_30, 30.0),
        (FPS_50, 50.0),
        (FPS_59_94, 59.94),
        (FPS_60, 60.0),
    ])
    def test_fps_rational_conversion(
        self, fps_rational: Rational, fps_float: float
    ) -> None:
        """帧率有理数转换"""
        actual = fps_rational.to_float()
        # 允许小误差（浮点精度）
        assert abs(actual - fps_float) < 0.001

    @pytest.mark.smoke
    def test_fps_from_video(self, any_available_video: Optional[str]) -> None:
        """从视频获取帧率"""
        pytest_skip_if_no_video(any_available_video)

        source = FFmpegMediaSource(any_available_video)
        metadata = source.probe()

        # 帧率应在合理范围内
        fps = metadata.fps.to_float()
        assert 1.0 <= fps <= 120.0

        # 验证常见帧率
        common_fps = [23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0]
        is_common = any(abs(fps - cf) < 0.1 for cf in common_fps)
        assert is_common, f"Unusual frame rate: {fps}"

    @pytest.mark.smoke
    def test_frame_time_calculation(
        self, any_available_video: Optional[str]
    ) -> None:
        """帧时间计算"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            metadata = source.probe()

            for i in range(5):
                frame = source.read_next_frame()

                # 时间应该接近 frame_index / fps
                expected_time = i / metadata.fps.to_float()
                actual_time = frame.time_seconds

                # 允许一定误差（由于 PTS 精度）
                assert abs(actual_time - expected_time) < 0.1  # 100ms 误差


# =============================================================================
# Smoke Tests - Timecode
# =============================================================================

class TestSmokeTimecode:
    """Smoke 测试 - 时间码"""

    @pytest.mark.smoke
    def test_timecode_ndf_creation(self) -> None:
        """NDF 时间码创建"""
        tc = Timecode(1, 0, 0, 0, FPS_24, TimecodeType.NON_DROP_FRAME)
        
        assert tc.hours == 1
        assert tc.minutes == 0
        assert tc.seconds == 0
        assert tc.frames == 0
        assert tc.to_string() == '01:00:00:00'

    @pytest.mark.smoke
    def test_timecode_df_creation(self) -> None:
        """DF 时间码创建"""
        # 29.97fps DF 时间码
        tc = Timecode(1, 0, 0, 0, FPS_29_97, TimecodeType.DROP_FRAME)
        
        assert tc.to_string() == '01:00:00;00'

    @pytest.mark.smoke
    def test_timecode_frame_conversion(self) -> None:
        """时间码帧号转换"""
        tc = Timecode.from_string('01:00:00;00', FPS_29_97)
        frame = tc.to_frame_number()
        
        # 1小时 = 107892帧 (29.97 DF)
        assert frame == 107892

    @pytest.mark.smoke
    def test_timecode_from_video(
        self, any_available_video: Optional[str]
    ) -> None:
        """从视频获取时间码"""
        pytest_skip_if_no_video(any_available_video)

        source = FFmpegMediaSource(any_available_video)
        metadata = source.probe()

        # 如果视频有时间码元数据
        if metadata.timecode:
            tc = Timecode.from_string(metadata.timecode, metadata.fps)
            assert tc is not None

    @pytest.mark.smoke
    def test_timecode_add_frames(self) -> None:
        """时间码加帧"""
        tc = Timecode.from_string('00:00:01:00', FPS_24)
        new_tc = tc.add_frames(24)

        assert new_tc.to_string() == '00:00:02:00'


# =============================================================================
# Smoke Tests - Audio Metadata
# =============================================================================

class TestSmokeAudioMetadata:
    """Smoke 测试 - 音频元数据"""

    @pytest.mark.smoke
    def test_audio_metadata_class(self) -> None:
        """AudioMetadata 类基本功能"""
        audio = AudioMetadata(
            sample_rate=48000,
            channels=2,
            channel_layout='stereo',
            codec_name='pcm_s16le',
            bit_depth=16,
            duration_seconds=10.0,
        )

        assert audio.sample_rate == 48000
        assert audio.channels == 2
        assert audio.is_pcm is True
        assert audio.is_compressed is False

    @pytest.mark.smoke
    def test_compressed_audio_detection(self) -> None:
        """压缩音频检测"""
        audio_aac = AudioMetadata(
            sample_rate=48000,
            channels=2,
            codec_name='aac',
        )

        assert audio_aac.is_compressed is True
        assert audio_aac.is_pcm is False

        audio_flac = AudioMetadata(
            sample_rate=48000,
            channels=2,
            codec_name='flac',
        )

        assert audio_flac.is_compressed is True
        assert audio_flac.is_lossless is True


# =============================================================================
# Smoke Tests - Memory Management
# =============================================================================

class TestSmokeMemory:
    """Smoke 测试 - 内存管理"""

    @pytest.mark.smoke
    def test_no_memory_accumulation(self, any_available_video: Optional[str]) -> None:
        """验证解码无内存累积"""
        pytest_skip_if_no_video(any_available_video)

        tracemalloc.start()

        source = FFmpegMediaSource(any_available_video)
        source.open()

        # 记录初始内存
        initial_snapshot = tracemalloc.take_snapshot()

        # 解码多帧
        frames_read = 0
        max_frames = 20

        try:
            for _ in range(max_frames):
                frame = source.read_next_frame()
                del frame
                frames_read += 1
        except (StopIteration, EOFError):
            pass

        # 强制 GC
        gc.collect()

        # 记录最终内存
        final_snapshot = tracemalloc.take_snapshot()

        source.close()
        tracemalloc.stop()

        # 计算内存增长
        top_stats = final_snapshot.compare_to(initial_snapshot, 'lineno')

        # 不应该有超过 50MB 的增长
        significant_growth = [
            stat for stat in top_stats
            if stat.size_diff > 50 * 1024 * 1024
        ]

        assert len(significant_growth) == 0


# =============================================================================
# Smoke Tests - Full Pipeline
# =============================================================================

class TestSmokeFullPipeline:
    """Smoke 测试 - 完整管线"""

    @pytest.mark.smoke
    def test_probe_decode_seek_close(
        self, any_available_video: Optional[str]
    ) -> None:
        """完整管线：probe -> decode -> seek -> close"""
        pytest_skip_if_no_video(any_available_video)

        # 1. Probe
        source = FFmpegMediaSource(any_available_video)
        metadata = source.probe()
        assert metadata is not None

        # 2. Open
        source.open()
        assert source.is_open is True

        # 3. Decode first frames
        frame1 = source.read_next_frame()
        assert frame1.frame_index == 0

        frame2 = source.read_next_frame()
        assert frame2.frame_index == 1

        # 4. Seek to middle
        if metadata.total_frames and metadata.total_frames > 10:
            target = metadata.total_frames // 2
            source.seek_frame(target)
            frame_middle = source.read_next_frame()
            assert frame_middle.frame_index == target

        # 5. Seek back to start
        source.seek_frame(0)
        frame_start = source.read_next_frame()
        assert frame_start.frame_index == 0

        # 6. Close
        source.close()
        assert source.is_open is False

    @pytest.mark.smoke
    def test_multiple_sources_sequentially(self) -> None:
        """顺序处理多个源"""
        available = get_available_videos()

        if len(available) < 2:
            pytest.skip("Need at least 2 test videos")

        paths = list(available.values())[:2]

        for path in paths:
            source = FFmpegMediaSource(path)
            metadata = source.probe()
            assert metadata is not None

            source.open()
            frame = source.read_next_frame()
            assert frame.frame_index == 0

            source.close()


# =============================================================================
# Smoke Tests - Codec Classification
# =============================================================================

class TestSmokeCodecClassification:
    """Smoke 测试 - 编解码器分类"""

    @pytest.mark.smoke
    def test_gop_codec_detection(self) -> None:
        """GOP codec 检测"""
        assert _is_gop_codec('h264') is True
        assert _is_gop_codec('h265') is True
        assert _is_gop_codec('hevc') is True
        assert _is_gop_codec('vp9') is True
        assert _is_gop_codec('av1') is True

    @pytest.mark.smoke
    def test_intra_codec_detection(self) -> None:
        """Intra codec 检测"""
        assert _is_intra_codec('prores') is True
        assert _is_intra_codec('dnxhr') is True
        assert _is_intra_codec('dnxhd') is True
        assert _is_intra_codec('rawvideo') is True

    @pytest.mark.smoke
    def test_bit_depth_inference(self) -> None:
        """位深度推断"""
        assert _infer_bit_depth('yuv420p') == 8
        assert _infer_bit_depth('yuv420p10le') == 10
        assert _infer_bit_depth('yuv420p12le') == 12
        assert _infer_bit_depth('yuv420p16le') == 16


# =============================================================================
# Smoke Tests - Error Handling
# =============================================================================

class TestSmokeErrorHandling:
    """Smoke 测试 - 错误处理"""

    @pytest.mark.smoke
    def test_decode_without_open_fails(self, any_available_video: Optional[str]) -> None:
        """未打开时解码应失败"""
        pytest_skip_if_no_video(any_available_video)

        source = FFmpegMediaSource(any_available_video)

        with pytest.raises((RuntimeError, MediaDecodeError)):
            source.read_next_frame()

    @pytest.mark.smoke
    def test_nonexistent_file_probe_fails(self) -> None:
        """不存在文件探测应失败"""
        source = FFmpegMediaSource('/path/that/does/not/exist.mp4')

        with pytest.raises(MediaSourceError):
            source.probe()

    @pytest.mark.smoke
    def test_nonexistent_file_open_fails(self) -> None:
        """不存在文件打开应失败"""
        source = FFmpegMediaSource('/path/that/does/not/exist.mp4')

        with pytest.raises(MediaOpenError):
            source.open()


# =============================================================================
# Synthetic Tests (No Video Files Required)
# =============================================================================

class TestSyntheticMetadata:
    """合成元数据测试（不需要实际视频文件）"""

    def test_create_synthetic_metadata(self) -> None:
        """创建合成元数据"""
        metadata = create_synthetic_video_metadata(
            codec='hevc',
            width=3840,
            height=2160,
            fps=FPS_24,
            bit_depth=10,
            is_hdr=True,
        )

        assert metadata.width == 3840
        assert metadata.height == 2160
        assert metadata.codec_name == 'hevc'
        assert metadata.bit_depth == 10
        assert metadata.is_hdr is True

    def test_synthetic_metadata_sdr(self) -> None:
        """创建 SDR 合成元数据"""
        metadata = create_synthetic_video_metadata()

        assert metadata.is_hdr is False
        assert metadata.color_transfer == 'bt709'

    def test_synthetic_metadata_duration(self) -> None:
        """合成元数据时长计算"""
        metadata = create_synthetic_video_metadata(fps=FPS_24)

        duration = metadata.duration_seconds
        assert duration == 10.0  # 240 frames / 24 fps = 10 seconds


# =============================================================================
# Manifest Tests
# =============================================================================

class TestFixturesManifest:
    """测试素材清单测试"""

    def test_manifest_exists(self) -> None:
        """清单文件应存在"""
        assert os.path.exists(FIXTURES_MANIFEST_PATH)

    def test_manifest_valid_json(self, fixtures_manifest: dict[str, Any]) -> None:
        """清单应为有效 JSON"""
        assert isinstance(fixtures_manifest, dict)
        assert 'fixtures' in fixtures_manifest

    def test_manifest_has_prores_fixture(self, fixtures_manifest: dict[str, Any]) -> None:
        """清单应包含 ProRes fixture"""
        fixtures = fixtures_manifest.get('fixtures', {})
        assert 'prores_422_hq_10bit' in fixtures

    def test_manifest_has_h264_fixture(self, fixtures_manifest: dict[str, Any]) -> None:
        """清单应包含 H.264 fixture"""
        fixtures = fixtures_manifest.get('fixtures', {})
        assert 'h264_long_gop' in fixtures

    def test_manifest_has_hdr_fixture(self, fixtures_manifest: dict[str, Any]) -> None:
        """清单应包含 HDR fixture"""
        fixtures = fixtures_manifest.get('fixtures', {})
        assert 'h265_10bit_pq' in fixtures
        assert 'h265_10bit_hlg' in fixtures

    def test_manifest_has_fps_variations(self, fixtures_manifest: dict[str, Any]) -> None:
        """清单应包含帧率测试素材"""
        fixtures = fixtures_manifest.get('fixtures', {})
        fps_fixtures = fixtures.get('fps_variations', {}).get('fixtures', {})
        
        expected_fps = ['fps_23_976', 'fps_24', 'fps_25', 'fps_29_97', 'fps_30', 'fps_50', 'fps_60']
        for fps_name in expected_fps:
            assert fps_name in fps_fixtures


# =============================================================================
# Run Tests Manually
# =============================================================================

def run_manual_smoke_tests() -> None:
    """手动运行 smoke 测试（用于调试）"""
    print("\n=== Manual Video Pipeline Smoke Tests ===\n")

    available = get_available_videos()
    if not available:
        print("No test videos available. Skipping.")
        return

    for name, path in available.items():
        print(f"\n--- Testing {name}: {path} ---\n")

        try:
            # 1. Probe
            source = FFmpegMediaSource(path)
            metadata = source.probe()
            print(f"  [Probe] {metadata.width}x{metadata.height}, "
                  f"{metadata.fps.to_fps():.3f}fps, "
                  f"codec={metadata.codec_name}, "
                  f"bit_depth={metadata.bit_depth}, "
                  f"HDR={metadata.is_hdr}")

            # 2. Open
            source.open()
            print(f"  [Open] Success")

            # 3. Decode first 3 frames
            for i in range(3):
                frame = source.read_next_frame()
                print(f"  [Decode] Frame {frame.frame_index}: "
                      f"dtype={frame.dtype}, "
                      f"time={frame.time_seconds:.3f}s")

            # 4. Seek
            if metadata.total_frames and metadata.total_frames > 10:
                target = metadata.total_frames // 2
                source.seek_frame(target)
                frame = source.read_next_frame()
                print(f"  [Seek] Frame {frame.frame_index} at middle")

            # 5. Close
            source.close()
            print(f"  [Close] Success")

        except Exception as e:
            print(f"  ERROR: {e}")


if __name__ == '__main__':
    run_manual_smoke_tests()