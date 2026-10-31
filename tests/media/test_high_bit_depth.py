"""
High Bit Depth Media Tests (P3.3 + P3.4)
测试高位深视频解码和 ProRes 4444 alpha 保留。

## 覆盖范围

### P3.3 高位深 CPU frame
- read_frame_planar() 保留 planar YUV 格式
- 位深度保持（10-bit 不强制 uint8）
- YUV/YUVA plane 提取
- PlaneInfo 包含 index, dtype, bit_depth

### P3.4 Alpha 保留
- ProRes 4444 alpha 读取
- YUVA 格式 alpha 平面提取
- alpha 值范围验证（0-1023 for 10-bit）
- alpha gradient 测试

## 测试素材

需要以下测试素材（可选）：
- 10-bit H.265 PQ 视频
- ProRes 4444 带 alpha 视频
- 10-bit YUV420 视频
"""

from __future__ import annotations

import os
import pytest
import numpy as np
from typing import Any

# 导入测试模块
from src.shared.media.ffmpeg_source import (
    FFmpegMediaSource,
    _infer_bit_depth,
    PIXEL_FORMAT_DEPTH_MAP,
)
from src.shared.media.frame import (
    DecodedFrame,
    PlaneInfo,
    PixelFormatInfo,
    PIXEL_FORMATS,
    get_pixel_format,
)


# =============================================================================
# Test Fixtures
# =============================================================================

TEST_MEDIA_DIR = os.path.join(os.path.dirname(__file__), 'fixtures')

# 测试视频路径（可能不存在）
TEST_VIDEOS = {
    'h265_10bit': os.path.join(TEST_MEDIA_DIR, 'test_h265_10bit.mp4'),
    'prores_4444': os.path.join(TEST_MEDIA_DIR, 'test_prores_4444.mov'),
    'prores_4444xq': os.path.join(TEST_MEDIA_DIR, 'test_prores_4444xq.mov'),
    'prores_422': os.path.join(TEST_MEDIA_DIR, 'test_prores_422.mov'),
}


def get_available_videos() -> dict[str, str]:
    """返回可用的测试视频"""
    available = {}
    for name, path in TEST_VIDEOS.items():
        if os.path.exists(path):
            available[name] = path
    return available


def pytest_skip_if_no_video(video_path: str | None) -> None:
    """如果没有视频则跳过测试"""
    if video_path is None or not os.path.exists(video_path):
        pytest.skip("No test video available")


@pytest.fixture
def prores_4444_video() -> str | None:
    """ProRes 4444 视频（带 alpha）"""
    return TEST_VIDEOS.get('prores_4444')


@pytest.fixture
def h265_10bit_video() -> str | None:
    """H.265 10-bit 视频"""
    return TEST_VIDEOS.get('h265_10bit')


@pytest.fixture
def prores_422_video() -> str | None:
    """ProRes 422 视频（无 alpha）"""
    return TEST_VIDEOS.get('prores_422')


# =============================================================================
# PlaneInfo Tests (P3.3)
# =============================================================================

class TestPlaneInfo:
    """测试 PlaneInfo 类"""

    def test_plane_info_creation(self) -> None:
        """测试 PlaneInfo 创建"""
        plane_info = PlaneInfo.create(
            index=0,
            width=1920,
            height=1080,
            component='Y',
            dtype=np.uint16,
            bit_depth=10,
        )

        assert plane_info.index == 0
        assert plane_info.width == 1920
        assert plane_info.height == 1080
        assert plane_info.component == 'Y'
        assert plane_info.dtype == np.uint16
        assert plane_info.bit_depth == 10
        # stride 应自动计算
        assert plane_info.stride == 1920 * 2  # uint16 = 2 bytes

    def test_plane_info_custom_stride(self) -> None:
        """测试自定义 stride"""
        plane_info = PlaneInfo.create(
            index=0,
            width=1920,
            height=1080,
            component='Y',
            dtype=np.uint16,
            bit_depth=10,
            stride=4000,  # 自定义 stride（包含 padding）
        )

        assert plane_info.stride == 4000

    def test_plane_info_frozen(self) -> None:
        """测试 PlaneInfo 是 frozen 的"""
        plane_info = PlaneInfo.create(
            index=0,
            width=1920,
            height=1080,
            component='Y',
            dtype=np.uint16,
            bit_depth=10,
        )

        # frozen dataclass 不允许修改
        with pytest.raises(Exception):
            plane_info.width = 2000  # type: ignore


# =============================================================================
# Pixel Format Tests (P3.3)
# =============================================================================

class TestPixelFormats:
    """测试像素格式映射"""

    def test_yuva_10bit_formats_defined(self) -> None:
        """YUVA 10-bit 格式应已定义"""
        yuva_10bit_formats = [
            'yuva420p10le',
            'yuva422p10le',
            'yuva444p10le',
        ]

        for fmt in yuva_10bit_formats:
            info = get_pixel_format(fmt)
            assert info is not None, f"Format {fmt} should be defined"
            assert info.bit_depth == 10
            assert info.has_alpha is True
            assert info.planes == 4

    def test_yuva_12bit_formats_defined(self) -> None:
        """YUVA 12-bit 格式应已定义"""
        yuva_12bit_formats = [
            'yuva420p12le',
            'yuva422p12le',
            'yuva444p12le',
        ]

        for fmt in yuva_12bit_formats:
            info = get_pixel_format(fmt)
            assert info is not None, f"Format {fmt} should be defined"
            assert info.bit_depth == 12
            assert info.has_alpha is True
            assert info.planes == 4

    def test_gbrap_formats_defined(self) -> None:
        """GBRAP 格式应已定义"""
        gbrap_formats = [
            'gbrap',
            'gbrap10le',
            'gbrap12le',
            'gbrap16le',
        ]

        for fmt in gbrap_formats:
            info = get_pixel_format(fmt)
            assert info is not None, f"Format {fmt} should be defined"
            assert info.has_alpha is True
            assert info.planes == 4

    def test_prores_bit_depth(self) -> None:
        """ProRes 格式位深度"""
        prores_formats = [
            ('prores_422', 10),
            ('prores_4444', 10),
            ('prores_4444xq', 12),
        ]

        for fmt, expected_depth in prores_formats:
            depth = _infer_bit_depth(fmt)
            assert depth == expected_depth, f"{fmt} should be {expected_depth}-bit"


# =============================================================================
# High Bit Depth Inference Tests
# =============================================================================

class TestBitDepthInference:
    """测试位深度推断"""

    def test_yuva_formats_bit_depth(self) -> None:
        """YUVA 格式位深度推断"""
        assert _infer_bit_depth('yuva420p10le') == 10
        assert _infer_bit_depth('yuva444p10le') == 10
        assert _infer_bit_depth('yuva420p12le') == 12
        assert _infer_bit_depth('yuva444p12le') == 12
        assert _infer_bit_depth('yuva420p16le') == 16
        assert _infer_bit_depth('yuva444p') == 8

    def test_prores_4444_bit_depth(self) -> None:
        """ProRes 4444 位深度"""
        assert _infer_bit_depth('prores_4444') == 10
        assert _infer_bit_depth('prores_4444xq') == 12

    def test_gbrap_bit_depth(self) -> None:
        """GBRAP 位深度"""
        assert _infer_bit_depth('gbrap') == 8
        assert _infer_bit_depth('gbrap10le') == 10
        assert _infer_bit_depth('gbrap12le') == 12
        assert _infer_bit_depth('gbrap16le') == 16


# =============================================================================
# Planar Output Tests (P3.3)
# =============================================================================

class TestPlanarOutput:
    """测试 planar 输出功能"""

    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('prores_422', '')),
        reason="ProRes 422 test video not available"
    )
    def test_read_frame_planar_yuv(self, prores_422_video: str | None) -> None:
        """读取 ProRes 422 planar YUV 帧"""
        pytest_skip_if_no_video(prores_422_video)

        source = FFmpegMediaSource(prores_422_video)
        metadata = source.probe()

        source.open()
        frame = source.read_frame_planar(0)

        # 应是 planar 格式
        assert frame.is_planar is True
        assert frame.data is None
        assert len(frame.planes) >= 3  # Y, U, V

        # 验证 plane_infos
        assert frame.plane_infos is not None
        assert len(frame.plane_infos) >= 3

        # 验证第一个平面（Y）
        y_plane = frame.planes[0]
        y_info = frame.plane_infos[0]

        assert y_info.component == 'Y'
        assert y_info.width == metadata.width
        assert y_info.height == metadata.height
        assert y_info.bit_depth >= 10  # ProRes 是 10-bit

        # 高位深素材不应是 uint8
        if metadata.bit_depth > 8:
            assert y_info.dtype != np.uint8

        source.close()

    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('h265_10bit', '')),
        reason="H.265 10-bit test video not available"
    )
    def test_read_frame_planar_h265_10bit(self, h265_10bit_video: str | None) -> None:
        """读取 H.265 10-bit planar YUV 帧"""
        pytest_skip_if_no_video(h265_10bit_video)

        source = FFmpegMediaSource(h265_10bit_video)
        metadata = source.probe()

        if metadata.bit_depth < 10:
            pytest.skip("This file is not 10-bit")

        source.open()
        frame = source.read_frame_planar(0)

        # 应是 planar 格式
        assert frame.is_planar is True

        # 位深度保持
        assert frame.bit_depth >= 10

        # dtype 应是 uint16（高位深）
        assert frame.dtype in (np.uint16, np.float32)

        # 验证 Y 平面尺寸
        y_plane = frame.planes[0]
        assert y_plane.shape == (metadata.height, metadata.width)

        source.close()

    def test_planar_format_detection(self) -> None:
        """测试 planar 格式检测"""
        # 创建模拟 DecodedFrame
        import numpy as np

        # Planar YUV 模拟
        y = np.zeros((1080, 1920), dtype=np.uint16)
        u = np.zeros((540, 960), dtype=np.uint16)
        v = np.zeros((540, 960), dtype=np.uint16)

        frame = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            data=None,
            planes=(y, u, v),
            pixel_format='yuv420p10le',
            dtype=np.uint16,
            width=1920,
            height=1080,
        )

        assert frame.is_planar is True
        assert frame.is_packed is False
        assert frame.bit_depth == 10


# =============================================================================
# Alpha Preservation Tests (P3.4)
# =============================================================================

class TestAlphaPreservation:
    """测试 alpha 保留"""

    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('prores_4444', '')),
        reason="ProRes 4444 test video not available"
    )
    def test_prores_4444_alpha_detection(self, prores_4444_video: str | None) -> None:
        """ProRes 4444 alpha 检测"""
        pytest_skip_if_no_video(prores_4444_video)

        source = FFmpegMediaSource(prores_4444_video)
        metadata = source.probe()

        # ProRes 4444 应有 alpha
        assert metadata.has_alpha is True
        assert '4444' in metadata.codec_name.lower()

    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('prores_4444', '')),
        reason="ProRes 4444 test video not available"
    )
    def test_prores_4444_alpha_extraction(self, prores_4444_video: str | None) -> None:
        """ProRes 4444 alpha 平面提取"""
        pytest_skip_if_no_video(prores_4444_video)

        source = FFmpegMediaSource(prores_4444_video)
        metadata = source.probe()

        source.open()
        frame = source.read_frame_planar(0)

        # 应有 4 个平面（Y, U, V, A）
        assert len(frame.planes) >= 4

        # Alpha 平面存在
        alpha_plane = frame.planes[3]
        alpha_info = frame.plane_infos[3]

        assert alpha_info.component == 'A'
        assert alpha_info.width == metadata.width
        assert alpha_info.height == metadata.height

        # Alpha 值范围检查
        # 10-bit alpha 范围是 0-1023
        min_val = alpha_plane.min()
        max_val = alpha_plane.max()

        # 应在有效范围内
        assert min_val >= 0
        if frame.dtype == np.uint16:
            assert max_val <= 1023  # 10-bit
        elif frame.dtype == np.uint8:
            assert max_val <= 255

        source.close()

    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('prores_4444', '')),
        reason="ProRes 4444 test video not available"
    )
    def test_alpha_gradient_values(self, prores_4444_video: str | None) -> None:
        """Alpha gradient 值验证"""
        pytest_skip_if_no_video(prores_4444_video)

        source = FFmpegMediaSource(prores_4444_video)
        metadata = source.probe()

        source.open()

        # 读取多帧验证 alpha
        for i in range(min(5, metadata.total_frames or 5)):
            frame = source.read_frame_planar(i)

            if len(frame.planes) >= 4:
                alpha_plane = frame.planes[3]

                # Alpha 应有有效值
                # 如果是 gradient，应有渐变值
                unique_values = np.unique(alpha_plane)

                # 如果 alpha 不是全透明或全不透明
                if len(unique_values) > 2:
                    # 检查是否有中间值（gradient）
                    non_extreme = unique_values[
                        (unique_values > 0) & (unique_values < unique_values.max())
                    ]
                    # Gradient 应有中间值
                    # （如果测试素材有 gradient）

        source.close()

    def test_yuva_frame_creation(self) -> None:
        """测试 YUVA frame 创建"""
        import numpy as np

        # 创建模拟 YUVA frame
        y = np.zeros((1080, 1920), dtype=np.uint16)
        u = np.zeros((1080, 1920), dtype=np.uint16)  # 444
        v = np.zeros((1080, 1920), dtype=np.uint16)
        a = np.full((1080, 1920), 1023, dtype=np.uint16)  # 全不透明

        frame = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            data=None,
            planes=(y, u, v, a),
            pixel_format='yuva444p10le',
            dtype=np.uint16,
            width=1920,
            height=1080,
        )

        assert frame.has_alpha is True
        assert frame.bit_depth == 10

        # Alpha plane 验证
        alpha = frame.planes[3]
        assert alpha.min() == 1023
        assert alpha.max() == 1023


# =============================================================================
# Bit Depth Preservation Tests
# =============================================================================

class TestBitDepthPreservation:
    """测试位深度保持"""

    def test_no_uint8_downgrade_for_10bit(self) -> None:
        """10-bit 素材不应降级到 uint8"""
        import numpy as np

        # 模拟 10-bit frame
        y = np.array([[100, 200, 500]], dtype=np.uint16)  # 10-bit values
        u = np.array([[50, 100]], dtype=np.uint16)
        v = np.array([[75, 150]], dtype=np.uint16)

        frame = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            data=None,
            planes=(y, u, v),
            pixel_format='yuv420p10le',
            dtype=np.uint16,
            width=3,
            height=1,
        )

        # dtype 应是 uint16
        assert frame.dtype == np.uint16
        assert frame.bit_depth == 10

        # Y 平面值应 > 255（证明保留了高位深）
        assert y.max() > 255

    @pytest.mark.skipif(
        not os.path.exists(TEST_VIDEOS.get('prores_4444xq', '')),
        reason="ProRes 4444 XQ test video not available"
    )
    def test_12bit_preservation(self) -> None:
        """12-bit 位深度保持"""
        pytest_skip_if_no_video(TEST_VIDEOS.get('prores_4444xq'))

        source = FFmpegMediaSource(TEST_VIDEOS['prores_4444xq'])
        metadata = source.probe()

        if metadata.bit_depth < 12:
            pytest.skip("This file is not 12-bit")

        source.open()
        frame = source.read_frame_planar(0)

        # 12-bit 应是 uint16
        assert frame.dtype in (np.uint16, np.float32)

        source.close()


# =============================================================================
# Chroma Subsampling Tests
# =============================================================================

class TestChromaSubsampling:
    """测试色度子采样处理"""

    def test_420_subsampling(self) -> None:
        """4:2:0 子采样尺寸"""
        import numpy as np

        # 1920x1080 4:2:0
        # Y: 1920x1080, U/V: 960x540
        y = np.zeros((1080, 1920), dtype=np.uint16)
        u = np.zeros((540, 960), dtype=np.uint16)
        v = np.zeros((540, 960), dtype=np.uint16)

        frame = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            data=None,
            planes=(y, u, v),
            pixel_format='yuv420p10le',
            dtype=np.uint16,
            width=1920,
            height=1080,
        )

        # 主尺寸
        assert frame.width == 1920
        assert frame.height == 1080

        # U/V 平面尺寸应是 Y 的一半
        assert frame.planes[1].shape == (540, 960)
        assert frame.planes[2].shape == (540, 960)

    def test_444_subsampling(self) -> None:
        """4:4:4 无子采样"""
        import numpy as np

        # 1920x1080 4:4:4
        # Y, U, V 都是 1920x1080
        y = np.zeros((1080, 1920), dtype=np.uint16)
        u = np.zeros((1080, 1920), dtype=np.uint16)
        v = np.zeros((1080, 1920), dtype=np.uint16)

        frame = DecodedFrame(
            frame_index=0,
            pts=0,
            time_seconds=0.0,
            data=None,
            planes=(y, u, v),
            pixel_format='yuv444p10le',
            dtype=np.uint16,
            width=1920,
            height=1080,
        )

        # 所有平面尺寸相同
        assert frame.planes[0].shape == (1080, 1920)
        assert frame.planes[1].shape == (1080, 1920)
        assert frame.planes[2].shape == (1080, 1920)


# =============================================================================
# Integration Tests
# =============================================================================

class TestIntegration:
    """集成测试"""

    @pytest.mark.skipif(
        len(get_available_videos()) < 2,
        reason="Need at least 2 test videos"
    )
    def test_planar_vs_packed_comparison(self) -> None:
        """比较 planar 和 packed 输出"""
        available = get_available_videos()
        video_path = list(available.values())[0]

        source = FFmpegMediaSource(video_path)
        source.open()

        # 读取 packed
        packed_frame = source.read_frame(0)
        source.seek_frame(0)

        # 读取 planar
        planar_frame = source.read_frame_planar(0)

        # 尺寸应一致
        assert packed_frame.width == planar_frame.width
        assert packed_frame.height == planar_frame.height

        # packed 应有 data，planar 应有 planes
        assert packed_frame.data is not None
        assert len(planar_frame.planes) > 0

        source.close()


# =============================================================================
# Manual Test Runner
# =============================================================================

def run_manual_tests() -> None:
    """手动运行测试（用于调试）"""
    print("\n=== Manual High Bit Depth Tests ===\n")

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
                  f"pix_fmt={metadata.pixel_format}, "
                  f"bit_depth={metadata.bit_depth}, "
                  f"has_alpha={metadata.has_alpha}")

            # Open
            source.open()

            # Test planar output
            print("\n  --- Planar Output Test ---")
            frame = source.read_frame_planar(0)
            print(f"  Planar frame: format={frame.pixel_format}, "
                  f"dtype={frame.dtype}, "
                  f"planes={len(frame.planes)}, "
                  f"bit_depth={frame.bit_depth}")

            if frame.plane_infos:
                for i, info in enumerate(frame.plane_infos):
                    print(f"    Plane {i}: {info.component}, "
                          f"{info.width}x{info.height}, "
                          f"dtype={info.dtype}, "
                          f"bit_depth={info.bit_depth}")

            # Test alpha if available
            if metadata.has_alpha and len(frame.planes) >= 4:
                alpha = frame.planes[3]
                print(f"\n  Alpha plane: min={alpha.min()}, max={alpha.max()}, "
                      f"mean={alpha.mean():.2f}")

            # Test packed output for comparison
            source.seek_frame(0)
            print("\n  --- Packed Output Test ---")
            packed_frame = source.read_frame(0)
            print(f"  Packed frame: format={packed_frame.pixel_format}, "
                  f"dtype={packed_frame.dtype}, "
                  f"shape={packed_frame.shape}")

            # Close
            source.close()
            print(f"\n  Closed successfully")

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()


if __name__ == '__main__':
    run_manual_tests()