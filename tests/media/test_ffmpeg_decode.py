"""
FFmpeg Media Source Decode Tests
测试 FFmpegMediaSource 的顺序解码功能。

## 覆盖范围

- probe() 元数据读取
- open/close 资源管理
- read_next_frame() 顺序解码
- 帧号递增验证
- 高位深保持（不强制 uint8）
- EOF 处理
- 内存管理（无累积）

## 测试素材

- tests/media/fixtures/test_h264_sdr.mp4
- tests/media/fixtures/test_h265_pq.mp4
- tests/media/fixtures/test_prores.mov
"""

from __future__ import annotations

import os
import pytest
import gc
import tracemalloc
from typing import Any

import numpy as np

# 导入测试模块
from src.shared.media.ffmpeg_source import (
    FFmpegMediaSource,
    _infer_bit_depth,
    _is_gop_codec,
    _is_intra_codec,
)
from src.shared.media.metadata import Rational
from src.shared.media.frame import DecodedFrame
from src.shared.media.source import MediaSourceError, MediaOpenError, MediaDecodeError
from src.shared.media.errors import DecodeError


# =============================================================================
# Test Fixtures
# =============================================================================

# 测试素材路径
TEST_MEDIA_DIR = os.path.join(os.path.dirname(__file__), 'fixtures')

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


@pytest.fixture
def h264_video() -> str | None:
    """H.264 SDR 视频路径"""
    return TEST_VIDEOS.get('h264_sdr')


@pytest.fixture
def h265_pq_video() -> str | None:
    """H.265 PQ HDR 视频路径（优先使用 v2，有正确的 HDR 元数据）"""
    # 优先使用 v2（有正确的 PQ/BT.2020 元数据）
    v2_path = TEST_VIDEOS.get('h265_pq_v2')
    if v2_path and os.path.exists(v2_path):
        return v2_path
    # fallback 到旧版本（元数据可能为 unknown）
    return TEST_VIDEOS.get('h265_pq')


@pytest.fixture
def prores_video() -> str | None:
    """ProRes 视频路径"""
    return TEST_VIDEOS.get('prores')


@pytest.fixture
def any_available_video() -> str | None:
    """任意可用的测试视频"""
    available = get_available_videos()
    if available:
        return list(available.values())[0]
    return None


# =============================================================================
# Helper Functions
# =============================================================================

def pytest_skip_if_no_video(video_path: str | None) -> None:
    """如果没有视频则跳过测试"""
    if video_path is None or not os.path.exists(video_path):
        pytest.skip("No test video available")


# =============================================================================
# Bit Depth Tests
# =============================================================================

class TestBitDepthInference:
    """测试位深度推断"""

    def test_8bit_formats(self) -> None:
        """测试 8-bit 格式推断"""
        assert _infer_bit_depth('yuv420p') == 8
        assert _infer_bit_depth('yuv422p') == 8
        assert _infer_bit_depth('yuv444p') == 8
        assert _infer_bit_depth('rgb24') == 8
        assert _infer_bit_depth('bgra') == 8
        assert _infer_bit_depth('gbrp') == 8

    def test_10bit_formats(self) -> None:
        """测试 10-bit 格式推断"""
        assert _infer_bit_depth('yuv420p10le') == 10
        assert _infer_bit_depth('yuv422p10le') == 10
        assert _infer_bit_depth('yuv444p10le') == 10
        assert _infer_bit_depth('gbrp10le') == 10
        assert _infer_bit_depth('yuv420p10be') == 10

    def test_12bit_formats(self) -> None:
        """测试 12-bit 格式推断"""
        assert _infer_bit_depth('yuv420p12le') == 12
        assert _infer_bit_depth('yuv422p12le') == 12
        assert _infer_bit_depth('yuv444p12le') == 12
        assert _infer_bit_depth('gbrp12le') == 12

    def test_16bit_formats(self) -> None:
        """测试 16-bit 格式推断"""
        assert _infer_bit_depth('yuv420p16le') == 16
        assert _infer_bit_depth('rgb48le') == 16
        assert _infer_bit_depth('rgba64le') == 16

    def test_unknown_format_defaults_to_8(self) -> None:
        """未知格式默认为 8-bit"""
        assert _infer_bit_depth('unknown_format') == 8
        assert _infer_bit_depth('') == 8


# =============================================================================
# Codec Classification Tests
# =============================================================================

class TestCodecClassification:
    """测试编解码器分类"""

    def test_gop_codecs(self) -> None:
        """测试 GOP codec 检测"""
        assert _is_gop_codec('h264') is True
        assert _is_gop_codec('h265') is True
        assert _is_gop_codec('hevc') is True
        assert _is_gop_codec('vp9') is True
        assert _is_gop_codec('av1') is True
        assert _is_gop_codec('mpeg4') is True

    def test_intra_codecs(self) -> None:
        """测试 Intra codec 检测"""
        assert _is_intra_codec('prores') is True
        assert _is_intra_codec('prores_ks') is True
        assert _is_intra_codec('dnxhr') is True
        assert _is_intra_codec('dnxhd') is True
        assert _is_intra_codec('ffv1') is True
        assert _is_intra_codec('rawvideo') is True

    def test_codec_aliases(self) -> None:
        """测试 codec 别名"""
        assert _is_gop_codec('libx264') is True
        assert _is_gop_codec('libx265') is True
        assert _is_intra_codec('prores_aw') is True


# =============================================================================
# Probe Tests
# =============================================================================

class TestProbe:
    """测试视频探测功能"""

    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('h264_sdr', '')),
        reason="H.264 test video not available"
    )
    def test_probe_h264(self, h264_video: str | None) -> None:
        """探测 H.264 视频"""
        pytest_skip_if_no_video(h264_video)

        source = FFmpegMediaSource(h264_video)
        metadata = source.probe()

        assert metadata is not None
        assert metadata.width > 0
        assert metadata.height > 0
        assert metadata.codec_name == 'h264'
        assert metadata.fps.to_float() > 0
        assert metadata.bit_depth >= 8

    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('h265_pq', '')),
        reason="H.265 PQ test video not available"
    )
    def test_probe_h265_hdr(self, h265_pq_video: str | None) -> None:
        """探测 H.265 HDR 视频"""
        pytest_skip_if_no_video(h265_pq_video)

        source = FFmpegMediaSource(h265_pq_video)
        metadata = source.probe()

        assert metadata is not None
        assert metadata.codec_name in ('hevc', 'h265')
        assert metadata.bit_depth >= 10  # HDR 通常是 10-bit

        # 检查 HDR 元数据
        assert metadata.is_hdr is True
        assert metadata.color_transfer in ('smpte2084', 'arib-std-b67')

    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('prores', '')),
        reason="ProRes test video not available"
    )
    def test_probe_prores(self, prores_video: str | None) -> None:
        """探测 ProRes 视频"""
        pytest_skip_if_no_video(prores_video)

        source = FFmpegMediaSource(prores_video)
        metadata = source.probe()

        assert metadata is not None
        assert 'prores' in metadata.codec_name.lower()
        assert metadata.bit_depth >= 10  # ProRes 通常是 10-bit

    def test_probe_nonexistent_file(self) -> None:
        """探测不存在文件应抛出错误"""
        source = FFmpegMediaSource('/nonexistent/video.mp4')

        with pytest.raises(MediaSourceError):
            source.probe()


# =============================================================================
# Open/Close Tests
# =============================================================================

class TestOpenClose:
    """测试资源管理"""

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_open_close_cycle(self, any_available_video: str | None) -> None:
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

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_context_manager(self, any_available_video: str | None) -> None:
        """测试上下文管理器"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            assert source.is_open is True

        assert source.is_open is False

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_double_open(self, any_available_video: str | None) -> None:
        """测试重复打开"""
        pytest_skip_if_no_video(any_available_video)

        source = FFmpegMediaSource(any_available_video)
        source.open()
        source.open()  # 第二次打开应该无效果

        assert source.is_open is True
        source.close()

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_double_close(self, any_available_video: str | None) -> None:
        """测试重复关闭"""
        pytest_skip_if_no_video(any_available_video)

        source = FFmpegMediaSource(any_available_video)
        source.open()
        source.close()
        source.close()  # 第二次关闭应该无效果

        assert source.is_open is False


# =============================================================================
# Sequential Decode Tests
# =============================================================================

class TestSequentialDecode:
    """测试顺序解码功能"""

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_read_first_frame(self, any_available_video: str | None) -> None:
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

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_read_three_frames(self, any_available_video: str | None) -> None:
        """读取前三帧"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            frames: list[DecodedFrame] = []

            for i in range(3):
                frame = source.read_next_frame()
                frames.append(frame)

                # 验证帧号递增
                assert frame.frame_index == i

            # 验证帧号序列
            frame_indices = [f.frame_index for f in frames]
            assert frame_indices == [0, 1, 2]

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_frame_dtype_preserved(self, any_available_video: str | None) -> None:
        """验证帧数据类型"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            metadata = source.probe()
            frame = source.read_next_frame()

            # 根据位深度验证 dtype
            if metadata.bit_depth >= 10:
                # 高位深素材应为 uint16 或 float32
                assert frame.dtype in (np.uint16, np.float32)
            else:
                # 8-bit 素材应为 uint8
                assert frame.dtype == np.uint8

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_current_frame_counter(self, any_available_video: str | None) -> None:
        """验证帧计数器"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            assert source.current_frame == 0

            for i in range(5):
                source.read_next_frame()
                # read_next_frame 后 current_frame 应递增
                # 注意：frame_index 是返回帧的索引（从 0 开始）

            # 检查 current_frame 属性
            assert source.current_frame >= 5

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_eof_handling(self, any_available_video: str | None) -> None:
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
# High Bit Depth Tests
# =============================================================================

class TestHighBitDepth:
    """测试高位深解码"""

    def test_planar_10bit_planes_decode_samples_not_bytes(self) -> None:
        """10-bit planar PyAV planes should be cropped uint16 samples, not raw bytes."""
        av = pytest.importorskip("av")

        frame = av.VideoFrame(4, 2, "yuv422p10le")
        visible_values = [
            np.array([[64, 128, 512, 940], [65, 129, 513, 941]], dtype=np.uint16),
            np.array([[512, 600], [513, 601]], dtype=np.uint16),
            np.array([[500, 700], [501, 701]], dtype=np.uint16),
        ]

        for plane, visible in zip(frame.planes, visible_values):
            stride_samples = plane.line_size // np.dtype(np.uint16).itemsize
            padded = np.full((plane.height, stride_samples), 999, dtype=np.uint16)
            padded[:visible.shape[0], :visible.shape[1]] = visible
            plane.update(padded.tobytes())

        source = FFmpegMediaSource("/fake/path.mov", preserve_planes=True)
        planes, plane_infos = source._extract_planes_from_frame(frame, "yuv422p10le")

        assert [plane.shape for plane in planes] == [(2, 4), (2, 2), (2, 2)]
        assert all(plane.dtype == np.uint16 for plane in planes)
        assert np.array_equal(planes[0], visible_values[0])
        assert np.array_equal(planes[1], visible_values[1])
        assert np.array_equal(planes[2], visible_values[2])
        assert [info.width for info in plane_infos] == [4, 2, 2]
        assert [info.height for info in plane_infos] == [2, 2, 2]
        assert [info.bit_depth for info in plane_infos] == [10, 10, 10]

    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('prores', '')),
        reason="ProRes test video not available"
    )
    def test_prores_10bit_preserved(self, prores_video: str | None) -> None:
        """ProRes 10-bit 素材保持高位深"""
        pytest_skip_if_no_video(prores_video)

        source = FFmpegMediaSource(prores_video)
        metadata = source.probe()

        if metadata.bit_depth < 10:
            pytest.skip("This ProRes file is not 10-bit")

        source.open()
        frame = source.read_next_frame()

        # 高位深素材不应降级到 uint8
        assert frame.dtype != np.uint8 or metadata.bit_depth == 8

        # 应为 uint16 或 float32
        if frame.data is not None:
            assert frame.dtype in (np.uint16, np.float32)
            # 检查是否有超过 255 的值（证明保留了高位深）
            if frame.dtype == np.uint16:
                max_val = frame.data.max()
                # 如果最大值 > 255，说明保留了 10-bit 数据
                # 但由于转换到 RGB，可能被拉伸，所以只需检查 dtype

        source.close()

    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('h265_pq', '')),
        reason="H.265 PQ test video not available"
    )
    def test_h265_10bit_preserved(self, h265_pq_video: str | None) -> None:
        """H.265 10-bit PQ 素材保持高位深"""
        pytest_skip_if_no_video(h265_pq_video)

        source = FFmpegMediaSource(h265_pq_video)
        metadata = source.probe()

        if metadata.bit_depth < 10:
            pytest.skip("This file is not 10-bit")

        source.open()
        frame = source.read_next_frame()

        # 高位深素材不应降级到 uint8
        assert frame.dtype in (np.uint16, np.float32)

        source.close()


# =============================================================================
# Memory Management Tests
# =============================================================================

class TestMemoryManagement:
    """测试内存管理"""

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_no_memory_accumulation(self, any_available_video: str | None) -> None:
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
                # 显式删除帧引用
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

        # 检查是否有显著内存增长（> 10MB）
        significant_growth = [
            stat for stat in top_stats
            if stat.size_diff > 10 * 1024 * 1024
        ]

        # 如果有显著增长，打印并警告
        if significant_growth:
            print("\nSignificant memory growth detected:")
            for stat in significant_growth[:5]:
                print(f"  {stat}")

        # 不应该有超过 50MB 的增长（允许一定缓冲）
        assert len(significant_growth) == 0 or all(
            stat.size_diff < 50 * 1024 * 1024
            for stat in significant_growth
        )

    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('prores', '')),
        reason="ProRes test video not available"
    )
    def test_4k_prores_memory(self, prores_video: str | None) -> None:
        """4K ProRes 连续解码无内存增长"""
        pytest_skip_if_no_video(prores_video)

        source = FFmpegMediaSource(prores_video)
        metadata = source.probe()

        # 只对 4K 或接近 4K 的文件测试
        if metadata.width < 1920 or metadata.height < 1080:
            pytest.skip("Not a high resolution video")

        tracemalloc.start()
        source.open()

        initial_mem = tracemalloc.get_traced_memory()[0]

        # 解码 50 帧
        frames_read = 0
        try:
            for _ in range(50):
                frame = source.read_next_frame()
                del frame
                frames_read += 1
        except (StopIteration, EOFError):
            pass

        gc.collect()
        final_mem = tracemalloc.get_traced_memory()[0]

        source.close()
        tracemalloc.stop()

        # 内存增长不应超过每帧 10MB * 50 帧 = 500MB
        # 但正常情况下应该远小于此（帧数据应该被释放）
        mem_growth_mb = (final_mem - initial_mem) / (1024 * 1024)

        print(f"\nMemory growth after {frames_read} frames: {mem_growth_mb:.2f} MB")

        # 允许一定缓冲，但不应该线性增长
        # 对于 4K 帧，每帧约 24MB (RGB) 或 12MB (YUV)
        # 内存增长应该稳定（帧被释放）
        # 这里设置一个宽松的阈值：100MB
        assert mem_growth_mb < 100, f"Memory grew by {mem_growth_mb:.2f} MB"


# =============================================================================
# PTS and Time Tests
# =============================================================================

class TestPTSAndTime:
    """测试 PTS 和时间计算"""

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_pts_increasing(self, any_available_video: str | None) -> None:
        """验证 PTS 递增"""
        pytest_skip_if_no_video(any_available_video)

        with FFmpegMediaSource(any_available_video) as source:
            pts_values: list[int | None] = []

            for _ in range(10):
                frame = source.read_next_frame()
                pts_values.append(frame.pts)

            # 过滤掉 None 值
            valid_pts = [p for p in pts_values if p is not None]

            if valid_pts:
                # PTS 应递增（或至少不递减）
                for i in range(1, len(valid_pts)):
                    assert valid_pts[i] >= valid_pts[i - 1]

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_time_seconds_reasonable(self, any_available_video: str | None) -> None:
        """验证时间计算合理"""
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
# Integration Tests
# =============================================================================

class TestIntegration:
    """集成测试"""

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_full_workflow(self, any_available_video: str | None) -> None:
        """完整工作流测试"""
        pytest_skip_if_no_video(any_available_video)

        # 1. 创建 source
        source = FFmpegMediaSource(any_available_video)

        # 2. 探测元数据
        metadata = source.probe()
        assert metadata is not None

        # 3. 打开
        source.open()
        assert source.is_open is True

        # 4. 解码几帧
        frames: list[DecodedFrame] = []
        for _ in range(5):
            frame = source.read_next_frame()
            frames.append(frame)

        # 5. 验证帧
        assert len(frames) == 5
        for i, frame in enumerate(frames):
            assert frame.frame_index == i
            assert frame.width == metadata.width
            assert frame.height == metadata.height

        # 6. 关闭
        source.close()
        assert source.is_open is False

    @pytest.mark.skipif(
        len(get_available_videos()) < 2,
        reason="Need at least 2 test videos"
    )
    def test_multiple_sources(self) -> None:
        """多源并发测试"""
        available = get_available_videos()

        if len(available) < 2:
            pytest.skip("Need at least 2 test videos")

        paths = list(available.values())[:2]
        sources = [FFmpegMediaSource(p) for p in paths]

        # 探测所有源
        for source in sources:
            metadata = source.probe()
            assert metadata is not None

        # 打开所有源
        for source in sources:
            source.open()
            assert source.is_open is True

        # 解码第一帧
        for source in sources:
            frame = source.read_next_frame()
            assert frame.frame_index == 0

        # 关闭所有源
        for source in sources:
            source.close()
            assert source.is_open is False


# =============================================================================
# Error Handling Tests
# =============================================================================

class TestErrorHandling:
    """测试错误处理"""

    def test_decode_without_open(self, any_available_video: str | None) -> None:
        """未打开时解码应失败"""
        if any_available_video is None:
            pytest.skip("No test video available")

        source = FFmpegMediaSource(any_available_video)

        with pytest.raises((RuntimeError, MediaDecodeError)):
            source.read_next_frame()

    def test_nonexistent_file(self) -> None:
        """不存在文件应抛出错误"""
        source = FFmpegMediaSource('/path/that/does/not/exist.mp4')

        with pytest.raises(MediaSourceError):
            source.probe()

        with pytest.raises(MediaOpenError):
            source.open()


# =============================================================================
# P3.B: Target Size / Proxy Tests
# =============================================================================

class TestTargetSizeProxy:
    """测试 target_size proxy 降分辨率功能 (P3.B)

    验收标准：
    - proxy frame width/height 小于原始 master 尺寸
    - master frame 保持原始分辨率
    - is_proxy 标记正确
    """

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_target_size_reduces_resolution(
        self, any_available_video: str | None
    ) -> None:
        """target_size 参数应正确降低帧分辨率"""
        pytest_skip_if_no_video(any_available_video)

        # 先探测原始尺寸
        master_source = FFmpegMediaSource(any_available_video)
        metadata = master_source.probe()
        original_width = metadata.width
        original_height = metadata.height

        # 计算 proxy 尺寸（缩小到一半）
        proxy_width = original_width // 2
        proxy_height = original_height // 2

        # 创建 proxy 源（带 target_size）
        proxy_source = FFmpegMediaSource(
            any_available_video,
            target_size=(proxy_width, proxy_height)
        )

        with proxy_source:
            proxy_frame = proxy_source.read_next_frame()

            # 验收：proxy frame 尺寸应小于原始
            assert proxy_frame.width == proxy_width
            assert proxy_frame.height == proxy_height
            assert proxy_frame.width < original_width
            assert proxy_frame.height < original_height

            # 验收：is_proxy 标记应为 True
            assert proxy_frame.is_proxy is True

            print(
                f"  Original: {original_width}x{original_height}, "
                f"Proxy: {proxy_frame.width}x{proxy_frame.height}"
            )

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_master_without_target_size_keeps_original(
        self, any_available_video: str | None
    ) -> None:
        """master 源（无 target_size）应保持原始分辨率"""
        pytest_skip_if_no_video(any_available_video)

        source = FFmpegMediaSource(any_available_video)  # 无 target_size
        metadata = source.probe()

        with source:
            frame = source.read_next_frame()

            # 验收：master frame 应保持原始分辨率
            assert frame.width == metadata.width
            assert frame.height == metadata.height

            # 验收：is_proxy 标记应为 False
            assert frame.is_proxy is False

            print(f"  Master: {frame.width}x{frame.height}")

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_multiple_frames_with_target_size(
        self, any_available_video: str | None
    ) -> None:
        """连续解码多帧时 target_size 应持续生效"""
        pytest_skip_if_no_video(any_available_video)

        metadata = FFmpegMediaSource(any_available_video).probe()
        proxy_width = metadata.width // 2
        proxy_height = metadata.height // 2

        proxy_source = FFmpegMediaSource(
            any_available_video,
            target_size=(proxy_width, proxy_height)
        )

        with proxy_source:
            for i in range(5):
                frame = proxy_source.read_next_frame()
                assert frame.width == proxy_width
                assert frame.height == proxy_height
                assert frame.is_proxy is True
                print(f"  Frame {i}: {frame.width}x{frame.height}, proxy={frame.is_proxy}")

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_original_size_preserved_in_metadata(
        self, any_available_video: str | None
    ) -> None:
        """proxy 源应保留 original_size 信息"""
        pytest_skip_if_no_video(any_available_video)

        master_source = FFmpegMediaSource(any_available_video)
        master_metadata = master_source.probe()
        original_width = master_metadata.width
        original_height = master_metadata.height

        proxy_width = original_width // 2
        proxy_height = original_height // 2

        proxy_source = FFmpegMediaSource(
            any_available_video,
            target_size=(proxy_width, proxy_height)
        )

        with proxy_source:
            # 探测后应保存 original_size
            assert proxy_source.original_size == (original_width, original_height)

            # target_size 属性应正确返回
            assert proxy_source.target_size == (proxy_width, proxy_height)

            print(
                f"  original_size={proxy_source.original_size}, "
                f"target_size={proxy_source.target_size}"
            )

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_seek_and_decode_with_target_size(
        self, any_available_video: str | None
    ) -> None:
        """seek 后解码也应正确使用 target_size"""
        pytest_skip_if_no_video(any_available_video)

        metadata = FFmpegMediaSource(any_available_video).probe()

        if metadata.total_frames is None or metadata.total_frames < 20:
            pytest.skip("Video too short for seek test")

        proxy_width = metadata.width // 2
        proxy_height = metadata.height // 2

        proxy_source = FFmpegMediaSource(
            any_available_video,
            target_size=(proxy_width, proxy_height)
        )

        with proxy_source:
            # 先读几帧
            for _ in range(3):
                proxy_source.read_next_frame()

            # Seek 到中间帧
            target_frame = metadata.total_frames // 2
            proxy_source.seek_frame(target_frame)
            frame = proxy_source.read_next_frame()

            # seek 后的帧尺寸仍应为 proxy 尺寸
            assert frame.width == proxy_width
            assert frame.height == proxy_height
            assert frame.is_proxy is True

            print(f"  Seek to {target_frame}: {frame.width}x{frame.height}")

    @pytest.mark.skipif(
        not get_available_videos(),
        reason="No test videos available"
    )
    def test_high_bit_depth_proxy(
        self, prores_video: str | None
    ) -> None:
        """高位深视频 proxy 应保持正确 dtype"""
        pytest_skip_if_no_video(prores_video)

        master_source = FFmpegMediaSource(prores_video)
        metadata = master_source.probe()

        if metadata.bit_depth < 10:
            pytest.skip("This video is not 10-bit")

        proxy_width = metadata.width // 2
        proxy_height = metadata.height // 2

        proxy_source = FFmpegMediaSource(
            prores_video,
            target_size=(proxy_width, proxy_height)
        )

        with proxy_source:
            frame = proxy_source.read_next_frame()

            # proxy 帧仍应保持高位深 dtype
            assert frame.dtype in (np.uint16, np.float32)

            # 尺寸正确
            assert frame.width == proxy_width
            assert frame.height == proxy_height

            print(f"  High bit depth proxy: dtype={frame.dtype}")


# =============================================================================
# Run Tests Manually
# =============================================================================

def run_manual_tests() -> None:
    """手动运行测试（用于调试）"""
    print("\n=== Manual Decode Tests ===\n")

    available = get_available_videos()
    if not available:
        print("No test videos available. Skipping.")
        return

    for name, path in available.items():
        print(f"\n--- Testing {name}: {path} ---\n")

        try:
            source = FFmpegMediaSource(path)

            # Probe
            metadata = source.probe()
            print(f"  Metadata: {metadata.width}x{metadata.height}, "
                  f"{metadata.fps.to_fps():.3f}fps, "
                  f"codec={metadata.codec_name}, "
                  f"bit_depth={metadata.bit_depth}")

            # Open
            source.open()
            print(f"  Opened successfully")

            # Read first 3 frames
            for i in range(3):
                frame = source.read_next_frame()
                print(f"  Frame {frame.frame_index}: "
                      f"dtype={frame.dtype}, "
                      f"format={frame.pixel_format}, "
                      f"pts={frame.pts}, "
                      f"time={frame.time_seconds:.3f}s")

            # Close
            source.close()
            print(f"  Closed successfully")

        except Exception as e:
            print(f"  ERROR: {e}")


if __name__ == '__main__':
    run_manual_tests()


# =============================================================================
# Reformat Fallback Chain & HW Frame Transfer Tests
# =============================================================================

class TestReformatFallbackChain:
    """测试 _get_reformat_fallback_chain 生成正确的中间格式链"""

    def test_yuv444p12le_16bit_target(self) -> None:
        chain = FFmpegMediaSource._get_reformat_fallback_chain('yuv444p12le', 'rgb48le')
        assert len(chain) == 3
        assert chain[0] == ('yuv444p', 'rgb48le')
        assert chain[1] == ('yuv444p16le', 'rgb48le')
        assert chain[2] == ('yuv420p', 'rgb24')

    def test_yuv444p12le_8bit_target(self) -> None:
        chain = FFmpegMediaSource._get_reformat_fallback_chain('yuv444p12le', 'rgb24')
        assert len(chain) == 2
        assert chain[0] == ('yuv444p', 'rgb24')
        assert chain[1] == ('yuv420p', 'rgb24')

    def test_yuv420p10le_16bit_target(self) -> None:
        chain = FFmpegMediaSource._get_reformat_fallback_chain('yuv420p10le', 'rgb48le')
        assert len(chain) == 3
        assert chain[0] == ('yuv420p', 'rgb48le')
        assert chain[1] == ('yuv420p16le', 'rgb48le')
        assert chain[2] == ('yuv420p', 'rgb24')

    def test_yuv422p12le(self) -> None:
        chain = FFmpegMediaSource._get_reformat_fallback_chain('yuv422p12le', 'rgb48le')
        assert chain[0] == ('yuv422p', 'rgb48le')

    def test_p010le(self) -> None:
        chain = FFmpegMediaSource._get_reformat_fallback_chain('p010le', 'rgb48le')
        assert chain[0] == ('yuv420p', 'rgb48le')

    def test_unknown_format_falls_to_yuv420p(self) -> None:
        chain = FFmpegMediaSource._get_reformat_fallback_chain('gbrp12le', 'rgb48le')
        assert len(chain) == 1
        assert chain[0] == ('yuv420p', 'rgb24')

    def test_yuva444p12le(self) -> None:
        chain = FFmpegMediaSource._get_reformat_fallback_chain('yuva444p12le', 'rgba64le')
        assert chain[0] == ('yuva444p', 'rgba64le')

    def test_regular_yuv444p_no_extra_chain(self) -> None:
        chain = FFmpegMediaSource._get_reformat_fallback_chain('yuv444p', 'rgb24')
        assert len(chain) == 1
        assert chain[0] == ('yuv420p', 'rgb24')


class TestHwFrameTransfer:
    """测试 _transfer_hw_frame_if_needed 硬件帧检测"""

    def test_regular_frame_not_transferred(self) -> None:
        import av
        frame = av.VideoFrame(32, 32, 'yuv420p')
        result = FFmpegMediaSource._transfer_hw_frame_if_needed(frame)
        assert result is frame

    def test_non_hw_frame_attribute(self) -> None:
        import av
        frame = av.VideoFrame(16, 16, 'rgb24')
        assert not hasattr(frame, 'is_hw')
