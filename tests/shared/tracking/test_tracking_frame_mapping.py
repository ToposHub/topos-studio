"""阶段 0 验收：坐标映射与帧读取基线。

覆盖：
- FrameSampler 按 source frame 读取、代理缩放矩阵、缓存一致性。
- 归一化/像素/帧号三类坐标的往返转换。
- ClipFrameMapper 与 shared TimelineClip 的帧映射语义一致。
"""

import os

import numpy as np
import pytest

from src.shared.tracking.coordinate_map import (
    ClipFrameMapper,
    clamp_region_to_frame,
    norm_point_to_pixel,
    norm_region_to_pixels,
    pixel_point_to_norm,
    pixel_region_to_norm,
    region_contains,
)
from src.shared.tracking.frame_sampler import (
    AnalysisQuality,
    FrameSampler,
    TrackingFrameError,
    media_fingerprint,
)

try:
    from tools.make_tracking_fixtures import FIXTURE_NAMES, generate_all
    TOOLS_AVAILABLE = True
except ImportError:  # pragma: no cover
    TOOLS_AVAILABLE = False


@pytest.fixture(scope="session")
def fixture_dir() -> str:
    return generate_all(force=False)


@pytest.fixture(scope="session")
def translation_video(fixture_dir) -> str:
    return os.path.join(fixture_dir, "translation.mp4")


# ================================================================
# FrameSampler
# ================================================================

class TestFrameSampler:
    def test_probe_info(self, translation_video):
        with FrameSampler(translation_video) as sampler:
            w, h, fps, total = sampler.probe_info()
        assert (w, h) == (1280, 720)
        assert total == 125
        assert abs(fps - 25.0) < 1.0

    def test_full_resolution_read(self, translation_video):
        with FrameSampler(translation_video) as sampler:
            sample = sampler.read_gray(30, AnalysisQuality.HIGH)
        assert sample.gray.shape == (720, 1280)
        assert sample.scale == pytest.approx(1.0)
        assert sample.gray.dtype == np.uint8

    def test_proxy_scale_normal(self, translation_video):
        with FrameSampler(translation_video) as sampler:
            sample = sampler.read_gray(30, AnalysisQuality.NORMAL)
        # 1280x720 -> 长边 1080
        assert sample.width == 1080
        assert sample.height == 608
        assert sample.scale == pytest.approx(1080 / 1280, rel=1e-3)
        assert sample.gray.shape == (sample.height, sample.width)

    def test_proxy_scale_draft(self, translation_video):
        with FrameSampler(translation_video) as sampler:
            sample = sampler.read_gray(30, AnalysisQuality.DRAFT)
        assert sample.width == 540
        assert sample.scale == pytest.approx(540 / 1280, rel=1e-3)

    def test_coordinate_roundtrip_through_scale(self, translation_video):
        with FrameSampler(translation_video) as sampler:
            sample = sampler.read_gray(30, AnalysisQuality.DRAFT)
        x_src, y_src = 640.0, 360.0
        ax, ay = sample.to_analysis_point(x_src, y_src)
        bx, by = sample.to_source_point(ax, ay)
        assert bx == pytest.approx(x_src, abs=1e-6)
        assert by == pytest.approx(y_src, abs=1e-6)
        # 缩放矩阵一致
        mat = sample.scale_matrix()
        mapped = mat @ np.array([ax, ay, 1.0])
        assert mapped[0] == pytest.approx(x_src, abs=1e-6)

    def test_cache_returns_identical_frame(self, translation_video):
        with FrameSampler(translation_video) as sampler:
            a = sampler.read_gray(17, AnalysisQuality.NORMAL)
            b = sampler.read_gray(17, AnalysisQuality.NORMAL)
        assert np.array_equal(a.gray, b.gray)

    def test_different_frames_differ(self, translation_video):
        with FrameSampler(translation_video) as sampler:
            a = sampler.read_gray(10, AnalysisQuality.DRAFT)
            b = sampler.read_gray(40, AnalysisQuality.DRAFT)
        assert not np.array_equal(a.gray, b.gray)

    def test_negative_frame_rejected(self, translation_video):
        with FrameSampler(translation_video) as sampler:
            with pytest.raises(TrackingFrameError):
                sampler.read_gray(-1)

    def test_out_of_range_frame_rejected(self, translation_video):
        with FrameSampler(translation_video) as sampler:
            with pytest.raises(TrackingFrameError):
                sampler.read_gray(10_000, AnalysisQuality.DRAFT)

    def test_missing_file_rejected(self):
        sampler = FrameSampler("/nonexistent/missing_video.mp4")
        with pytest.raises(TrackingFrameError):
            sampler.read_gray(0)


class TestMediaFingerprint:
    def test_stable_and_unique(self, fixture_dir):
        a1 = media_fingerprint(os.path.join(fixture_dir, "translation.mp4"))
        a2 = media_fingerprint(os.path.join(fixture_dir, "translation.mp4"))
        b = media_fingerprint(os.path.join(fixture_dir, "trs.mp4"))
        assert a1 and a1 == a2
        assert a1 != b

    def test_missing_file_returns_empty(self):
        assert media_fingerprint("/nonexistent/no_video.mp4") == ""


# ================================================================
# 坐标转换
# ================================================================

class TestCoordinateConversion:
    def test_point_roundtrip(self):
        px, py = norm_point_to_pixel(0.25, 0.75, 1280, 720)
        assert (px, py) == (320.0, 540.0)
        nx, ny = pixel_point_to_norm(px, py, 1280, 720)
        assert nx == pytest.approx(0.25)
        assert ny == pytest.approx(0.75)

    def test_region_roundtrip(self):
        region = (0.4, 0.4, 0.25, 0.31)
        px = norm_region_to_pixels(region, 1280, 720)
        back = pixel_region_to_norm(px, 1280, 720)
        assert back == pytest.approx(region)

    def test_pixel_to_norm_rejects_bad_size(self):
        with pytest.raises(ValueError):
            pixel_point_to_norm(1.0, 1.0, 0, 720)

    def test_clamp_region(self):
        clamped = clamp_region_to_frame((-10.0, 600.0, 100.0, 300.0), 1280, 720)
        x, y, w, h = clamped
        assert x == 0.0
        assert y == 600.0
        assert y + h <= 720.0
        assert w == pytest.approx(90.0)

    def test_region_contains(self):
        outer = (100.0, 100.0, 400.0, 400.0)
        assert region_contains(outer, (150.0, 150.0, 100.0, 100.0))
        assert not region_contains(outer, (150.0, 150.0, 500.0, 100.0))


# ================================================================
# 帧号映射
# ================================================================

class TestClipFrameMapper:
    def test_identity_speed_roundtrip(self):
        mapper = ClipFrameMapper(source_start=100, source_end=199, timeline_start=40)
        assert mapper.source_to_timeline_frame(100) == 40
        assert mapper.source_to_timeline_frame(150) == 90
        assert mapper.timeline_to_source_frame(90) == 150
        assert mapper.source_to_clip_local_frame(150) == 50
        assert mapper.clip_local_to_source_frame(50) == 150

    def test_double_speed(self):
        mapper = ClipFrameMapper(source_start=0, source_end=199, timeline_start=0, speed=2.0)
        assert mapper.source_to_timeline_frame(10) == 5
        assert mapper.timeline_to_source_frame(5) == 10
        assert mapper.timeline_duration == 100

    def test_out_of_range_rejected(self):
        mapper = ClipFrameMapper(source_start=100, source_end=199, timeline_start=0)
        with pytest.raises(ValueError):
            mapper.source_to_timeline_frame(99)
        with pytest.raises(ValueError):
            mapper.timeline_to_source_frame(200)

    def test_invalid_construction(self):
        with pytest.raises(ValueError):
            ClipFrameMapper(source_start=10, source_end=5, timeline_start=0)
        with pytest.raises(ValueError):
            ClipFrameMapper(source_start=0, source_end=10, timeline_start=0, speed=0.0)

    def test_parity_with_shared_timeline_clip(self):
        """ClipFrameMapper 必须与 shared TimelineClip 的映射语义一致。"""
        from src.shared.media.timeline import TimelineClip

        clip = TimelineClip(
            source_start=100, source_end=199,
            timeline_start=40, timeline_end=140,
            speed=1.0,
        )
        mapper = ClipFrameMapper(source_start=100, source_end=199, timeline_start=40, speed=1.0)
        for sf in range(100, 200):
            assert mapper.source_to_timeline_frame(sf) == clip.source_to_timeline_frame(sf)
