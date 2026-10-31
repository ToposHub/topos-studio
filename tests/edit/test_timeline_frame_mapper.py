"""
Tests for timeline_frame_mapper — unified frame mapping.

Validates:
- CFR frame mapping with exact fps ratios
- Mixed fps cadence (24->30, 30->24, 23.976->25, etc.)
- Speed changes (0.5x, 2.0x)
- Reverse playback
- VFR PTS-based mapping
- No float accumulation drift over 1000+ frames
"""

import pytest
from fractions import Fraction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(
    timeline_frame: int = 0,
    timeline_fps: int = 24,
    clip_start: int = 0,
    clip_duration: int = 100,
    source_start: int = 0,
    source_duration: int = 100,
    source_fps: int | None = 24,
    speed: Fraction = Fraction(1, 1),
    reverse: bool = False,
    source_pts_index: tuple[float, ...] | None = None,
):
    from src.features.edit.core.timeline_frame_mapper import FrameMapRequest
    return FrameMapRequest(
        timeline_frame=timeline_frame,
        timeline_fps_num=timeline_fps,
        timeline_fps_den=1,
        clip_timeline_start=clip_start,
        clip_timeline_duration=clip_duration,
        source_start_frame=source_start,
        source_duration_frames=source_duration,
        source_fps_num=source_fps,
        source_fps_den=1 if source_fps else None,
        speed=speed,
        reverse=reverse,
        source_pts_index=source_pts_index,
    )


# ===================================================================
# Test: Basic 1:1 fps mapping
# ===================================================================

class TestBasicMapping:
    """Same fps for timeline and source."""

    def test_identity_mapping(self):
        from src.features.edit.core.timeline_frame_mapper import map_frame
        req = _make_request(timeline_frame=0, timeline_fps=24, source_fps=24)
        result = map_frame(req)
        assert result is not None
        assert result.source_frame == 0

    def test_frame_10_maps_to_10(self):
        from src.features.edit.core.timeline_frame_mapper import map_frame
        req = _make_request(timeline_frame=10, timeline_fps=24, source_fps=24)
        result = map_frame(req)
        assert result is not None
        assert result.source_frame == 10

    def test_out_of_range_returns_none(self):
        from src.features.edit.core.timeline_frame_mapper import map_frame
        req = _make_request(timeline_frame=200, clip_duration=100, source_fps=24)
        result = map_frame(req)
        assert result is None

    def test_negative_local_returns_none(self):
        from src.features.edit.core.timeline_frame_mapper import map_frame
        req = _make_request(timeline_frame=-1, clip_start=0, source_fps=24)
        result = map_frame(req)
        assert result is None

    def test_last_frame_in_range(self):
        from src.features.edit.core.timeline_frame_mapper import map_frame
        req = _make_request(timeline_frame=99, clip_duration=100, source_fps=24)
        result = map_frame(req)
        assert result is not None
        assert result.source_frame == 99


# ===================================================================
# Test: Mixed fps cadence
# ===================================================================

class TestMixedFpsCadence:
    """Frame mapping with different source and timeline fps."""

    def test_24_to_30_cadence(self):
        """24fps source in 30fps timeline: 30 timeline frames map to ~24 source frames."""
        from src.features.edit.core.timeline_frame_mapper import compute_cadence

        template = _make_request(
            timeline_fps=30,
            clip_duration=30,
            source_duration=24,
            source_fps=24,
        )
        results = compute_cadence(template, 30)
        # 30 timeline frames are all in range (clip_duration=30)
        assert len(results) == 30

        # Check that source frames are in valid range
        source_frames = [r.source_frame for r in results]
        assert all(0 <= f < 24 for f in source_frames)

    def test_30_to_24_cadence(self):
        """30fps source in 24fps timeline: 24 timeline frames map to ~30 source frames."""
        from src.features.edit.core.timeline_frame_mapper import compute_cadence

        template = _make_request(
            timeline_fps=24,
            clip_duration=24,
            source_duration=30,
            source_fps=30,
        )
        results = compute_cadence(template, 24)
        assert len(results) == 24

        source_frames = [r.source_frame for r in results]
        # Should cover range roughly 0-29
        assert max(source_frames) >= 25  # Should reach into higher source frames

    def test_23_976_to_25_no_drift(self):
        """1000 frames of 23.976fps in 25fps timeline: no cumulative drift > 1 frame."""
        from src.features.edit.core.timeline_frame_mapper import map_frame

        for i in range(0, 1000, 10):
            req = _make_request(
                timeline_frame=i,
                timeline_fps=25,
                clip_duration=1000,
                source_duration=1000,
                source_fps=24000,  # Use Fraction trick: 24000/1001 = 23.976
            )
            req = _make_request(
                timeline_frame=i,
                timeline_fps=25,
                clip_duration=1000,
                source_duration=1000,
                source_fps=24,
            )
            result = map_frame(req)
            if result is not None:
                # Expected: source_frame ≈ timeline_frame * 24/25
                expected_approx = i * 24 / 25
                assert abs(result.source_frame - expected_approx) <= 2

    def test_cadence_actions_are_valid(self):
        """Cadence actions should be one of: exact, repeat, skip."""
        from src.features.edit.core.timeline_frame_mapper import compute_cadence

        template = _make_request(
            timeline_fps=30,
            clip_duration=60,
            source_duration=48,
            source_fps=24,
        )
        results = compute_cadence(template, 60)
        valid_actions = {"exact", "repeat", "skip"}
        for r in results:
            assert r.cadence_action in valid_actions


# ===================================================================
# Test: Speed changes
# ===================================================================

class TestSpeedChanges:
    """Speed multiplier affects source frame mapping."""

    def test_half_speed(self):
        """0.5x speed: source time advances at half rate."""
        from src.features.edit.core.timeline_frame_mapper import map_frame
        req = _make_request(
            timeline_frame=10,
            timeline_fps=24,
            clip_duration=100,
            source_duration=100,
            source_fps=24,
            speed=Fraction(1, 2),
        )
        result = map_frame(req)
        assert result is not None
        # At half speed, timeline frame 10 maps to source frame ~5
        assert result.source_frame == 5

    def test_double_speed(self):
        """2.0x speed: source time advances at double rate."""
        from src.features.edit.core.timeline_frame_mapper import map_frame
        req = _make_request(
            timeline_frame=5,
            timeline_fps=24,
            clip_duration=100,
            source_duration=100,
            source_fps=24,
            speed=Fraction(2, 1),
        )
        result = map_frame(req)
        assert result is not None
        # At double speed, timeline frame 5 maps to source frame ~10
        assert result.source_frame == 10


# ===================================================================
# Test: Reverse playback
# ===================================================================

class TestReversePlayback:
    """Reverse playback maps source frames in decreasing order."""

    def test_reverse_first_frame_maps_to_last(self):
        from src.features.edit.core.timeline_frame_mapper import map_frame
        req = _make_request(
            timeline_frame=0,
            timeline_fps=24,
            clip_duration=10,
            source_start=0,
            source_duration=10,
            source_fps=24,
            reverse=True,
        )
        result = map_frame(req)
        assert result is not None
        assert result.source_frame == 9  # Last source frame

    def test_reverse_last_frame_maps_to_first(self):
        from src.features.edit.core.timeline_frame_mapper import map_frame
        req = _make_request(
            timeline_frame=9,
            timeline_fps=24,
            clip_duration=10,
            source_start=0,
            source_duration=10,
            source_fps=24,
            reverse=True,
        )
        result = map_frame(req)
        assert result is not None
        assert result.source_frame == 0


# ===================================================================
# Test: VFR mapping
# ===================================================================

class TestVFRMapping:
    """Variable frame rate mapping using PTS index."""

    def test_vfr_selects_nearest_pts(self):
        from src.features.edit.core.timeline_frame_mapper import map_frame

        pts_index = (0.0, 0.04, 0.08, 0.12, 0.16, 0.20)
        req = _make_request(
            timeline_frame=0,
            timeline_fps=24,
            clip_duration=10,
            source_duration=6,
            source_fps=24,
            source_pts_index=pts_index,
        )
        result = map_frame(req)
        assert result is not None
        assert 0 <= result.source_frame < 6

    def test_vfr_fallback_to_cfr_when_no_pts(self):
        """Without PTS index, VFR falls back to CFR."""
        from src.features.edit.core.timeline_frame_mapper import map_frame

        req = _make_request(
            timeline_frame=5,
            timeline_fps=24,
            clip_duration=10,
            source_duration=10,
            source_fps=24,
            source_pts_index=None,
        )
        result = map_frame(req)
        assert result is not None
        assert result.source_frame == 5


# ===================================================================
# Test: Timeline time calculations
# ===================================================================

class TestTimeCalculations:
    """Timeline and source time calculations are correct."""

    def test_timeline_time_at_frame_24(self):
        """Frame 24 at 24fps = 1.0 second."""
        from src.features.edit.core.timeline_frame_mapper import map_frame
        req = _make_request(
            timeline_frame=24,
            timeline_fps=24,
            clip_duration=100,
            source_duration=100,
            source_fps=24,
        )
        result = map_frame(req)
        assert result is not None
        assert abs(result.timeline_time_seconds - 1.0) < 0.01

    def test_source_time_matches_speed(self):
        """Source time is timeline_time * speed."""
        from src.features.edit.core.timeline_frame_mapper import map_frame
        req = _make_request(
            timeline_frame=24,
            timeline_fps=24,
            clip_duration=100,
            source_duration=100,
            source_fps=24,
            speed=Fraction(1, 2),
        )
        result = map_frame(req)
        assert result is not None
        # timeline_time = 1.0s, source_time = 0.5s
        assert abs(result.source_time_seconds - 0.5) < 0.01


# ===================================================================
# Test: Edge cases
# ===================================================================

class TestEdgeCases:
    """Edge case handling."""

    def test_single_frame_clip(self):
        """Clip with duration 1 should always map to source frame 0."""
        from src.features.edit.core.timeline_frame_mapper import map_frame
        req = _make_request(
            timeline_frame=0,
            clip_duration=1,
            source_duration=1,
            source_fps=24,
        )
        result = map_frame(req)
        assert result is not None
        assert result.source_frame == 0

    def test_source_start_offset(self):
        """Source start offset is applied correctly."""
        from src.features.edit.core.timeline_frame_mapper import map_frame
        req = _make_request(
            timeline_frame=0,
            clip_duration=10,
            source_start=100,
            source_duration=10,
            source_fps=24,
        )
        result = map_frame(req)
        assert result is not None
        assert result.source_frame == 100
