"""视频降噪参数单元测试（纯 CPU，无 GL）。"""

import pytest

from src.shared.video.denoise.params import (
    SpatialDenoiseParams,
    TemporalDenoiseParams,
    VideoDenoiseParams,
)

pytestmark = pytest.mark.media_core


class TestSpatialParams:
    def test_defaults_are_inactive(self):
        p = SpatialDenoiseParams()
        assert not p.enabled
        assert not p.is_active()

    def test_active_requires_strength(self):
        assert SpatialDenoiseParams(enabled=True, luma=1.0).is_active()
        assert not SpatialDenoiseParams(enabled=True, luma=0.0, chroma=0.0).is_active()

    def test_sanitize_clamps(self):
        p = SpatialDenoiseParams(
            enabled=1, mode="bogus", radius=99, luma=150.0,
            chroma=-5.0, threshold=2.0, detail_recovery=-1.0,
        ).sanitized()
        assert p.enabled is True
        assert p.mode == "fast"
        assert p.radius == 10
        assert p.luma == 100.0
        assert p.chroma == 0.0
        assert p.threshold == 1.0
        assert p.detail_recovery == 0.0

    def test_roundtrip(self):
        p = SpatialDenoiseParams(enabled=True, mode="better", radius=5,
                                 luma=42.0, chroma=17.0, threshold=0.3,
                                 detail_recovery=0.8)
        q = SpatialDenoiseParams.from_dict(p.to_dict())
        assert q == p


class TestTemporalParams:
    def test_defaults_are_inactive(self):
        assert not TemporalDenoiseParams().is_active()

    def test_sanitize_clamps_frames(self):
        assert TemporalDenoiseParams(frames=99).sanitized().frames == 3
        assert TemporalDenoiseParams(frames=0).sanitized().frames == 1
        assert TemporalDenoiseParams(motion="warp").sanitized().motion == "fast"

    def test_roundtrip(self):
        p = TemporalDenoiseParams(enabled=True, frames=3, luma=70, chroma=35,
                                  motion="accurate", threshold=0.4, blend=0.8,
                                  scene_cut_sensitivity=0.9)
        assert TemporalDenoiseParams.from_dict(p.to_dict()) == p


class TestQualityModes:
    def test_preview_downgrades(self):
        v = VideoDenoiseParams(
            temporal=TemporalDenoiseParams(enabled=True, frames=3, motion="accurate"),
            spatial=SpatialDenoiseParams(enabled=True, mode="enhanced", radius=8),
        )
        p = v.for_quality("preview")
        assert p.temporal.frames == 2
        assert p.temporal.motion == "fast"
        assert p.spatial.mode == "fast"
        assert p.spatial.radius <= 4

    def test_paused_upgrades_minimally(self):
        v = VideoDenoiseParams(
            temporal=TemporalDenoiseParams(enabled=True, frames=1, motion="fast"),
            spatial=SpatialDenoiseParams(enabled=True, mode="fast"),
        )
        p = v.for_quality("paused")
        assert p.temporal.motion == "balanced"
        assert p.spatial.mode == "better"
        assert p.temporal.frames == 1  # 用户显式选择不被上调

    def test_export_max_quality(self):
        v = VideoDenoiseParams(
            temporal=TemporalDenoiseParams(enabled=True, frames=1, motion="fast"),
            spatial=SpatialDenoiseParams(enabled=True, mode="fast"),
        )
        p = v.for_quality("export")
        assert p.temporal.frames == 3
        assert p.temporal.motion == "accurate"
        assert p.spatial.mode == "enhanced"

    def test_parameters_key_changes_on_param_change(self):
        v1 = VideoDenoiseParams(
            temporal=TemporalDenoiseParams(enabled=True, luma=50))
        v2 = VideoDenoiseParams(
            temporal=TemporalDenoiseParams(enabled=True, luma=51))
        assert v1.parameters_key() != v2.parameters_key()
        assert v1.parameters_key() == v1.sanitized().parameters_key()

    def test_toplevel_roundtrip(self):
        v = VideoDenoiseParams(
            temporal=TemporalDenoiseParams(enabled=True, luma=60),
            spatial=SpatialDenoiseParams(enabled=True, mode="better", chroma=30),
        )
        assert VideoDenoiseParams.from_dict(v.to_dict()) == v
        # 未知/空数据安全
        assert VideoDenoiseParams.from_dict({}) == VideoDenoiseParams()
        assert VideoDenoiseParams.from_dict(None) == VideoDenoiseParams()
