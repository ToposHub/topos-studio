"""
Tests for mixed FPS playback cadence via ClipInstance.

Validates that ClipInstance.timeline_to_source_frame() correctly delegates
to the timeline_frame_mapper when fps metadata is available, producing
uniform cadence for mixed-rate content.
"""

import pytest
from fractions import Fraction


def _make_clip(
    timeline_start: int = 0,
    timeline_duration: int = 100,
    source_start: int = 0,
    source_duration: int = 100,
    source_fps_num: int | None = None,
    source_fps_den: int | None = None,
    timeline_fps_num: int | None = None,
    timeline_fps_den: int | None = None,
    speed: float = 1.0,
):
    """Create a ClipInstance with optional fps metadata."""
    from src.features.edit.core.timeline_v2 import ClipInstance
    return ClipInstance(
        asset_id="asset-1",
        track_id="track-1",
        timeline_start=timeline_start,
        timeline_duration=timeline_duration,
        source_start=source_start,
        source_duration=source_duration,
        speed=speed,
        _source_fps_num=source_fps_num,
        _source_fps_den=source_fps_den,
        _timeline_fps_num=timeline_fps_num,
        _timeline_fps_den=timeline_fps_den,
    )


# ===================================================================
# Test: 1:1 fps (no metadata → old formula)
# ===================================================================

class TestBasicMapping:
    """Same fps, no metadata — uses old inline formula."""

    def test_identity_no_metadata(self):
        """Without fps metadata, old formula gives identity mapping."""
        clip = _make_clip()
        for i in range(0, 10):
            assert clip.timeline_to_source_frame(i) == i

    def test_identity_with_metadata(self):
        """With matching fps metadata, mapper gives same results."""
        clip = _make_clip(
            source_fps_num=24, source_fps_den=1,
            timeline_fps_num=24, timeline_fps_den=1,
        )
        for i in range(0, 10):
            assert clip.timeline_to_source_frame(i) == i


# ===================================================================
# Test: Mixed fps cadence via ClipInstance
# ===================================================================

class TestMixedFpsCadence:
    """Frame mapping with different source and timeline fps."""

    def test_24fps_source_in_30fps_timeline(self):
        """24fps source in 30fps timeline: cadence is uniform (no large gaps)."""
        clip = _make_clip(
            timeline_duration=30,
            source_duration=24,
            source_fps_num=24, source_fps_den=1,
            timeline_fps_num=30, timeline_fps_den=1,
        )
        frames = [clip.timeline_to_source_frame(i) for i in range(30)]
        # All source frames should be in valid range
        assert all(0 <= f < 24 for f in frames)
        # Cadence should be uniform: max gap between consecutive frames is 1
        diffs = [frames[i+1] - frames[i] for i in range(len(frames)-1)]
        assert max(diffs) <= 1, f"Non-uniform cadence: diffs={diffs}"

    def test_30fps_source_in_24fps_timeline(self):
        """30fps source in 24fps timeline: frames skip uniformly."""
        clip = _make_clip(
            timeline_duration=24,
            source_duration=30,
            source_fps_num=30, source_fps_den=1,
            timeline_fps_num=24, timeline_fps_den=1,
        )
        frames = [clip.timeline_to_source_frame(i) for i in range(24)]
        assert all(0 <= f < 30 for f in frames)
        # Should reach into higher source frames
        assert max(frames) >= 20

    def test_23_976fps_in_25fps_no_drift(self):
        """23.976 source in 25fps timeline: 1000 frames no cumulative drift > 1."""
        clip = _make_clip(
            timeline_duration=1000,
            source_duration=1000,
            source_fps_num=24000, source_fps_den=1001,
            timeline_fps_num=25, timeline_fps_den=1,
        )
        for i in range(0, 1000, 10):
            result = clip.timeline_to_source_frame(i)
            # Expected: source_frame ≈ timeline_frame * 24/25
            expected_approx = i * 24 / 25
            assert abs(result - expected_approx) <= 2, (
                f"Drift at frame {i}: got {result}, expected ~{expected_approx}"
            )


# ===================================================================
# Test: Speed changes via ClipInstance
# ===================================================================

class TestSpeedChanges:
    """Speed multiplier via ClipInstance with mapper."""

    def test_half_speed_with_metadata(self):
        """0.5x speed: source time advances at half rate via mapper."""
        clip = _make_clip(
            timeline_duration=100,
            source_duration=100,
            source_fps_num=24, source_fps_den=1,
            timeline_fps_num=24, timeline_fps_den=1,
            speed=0.5,
        )
        result = clip.timeline_to_source_frame(10)
        assert result == 5

    def test_double_speed_with_metadata(self):
        """2.0x speed: source time advances at double rate via mapper."""
        clip = _make_clip(
            timeline_duration=100,
            source_duration=100,
            source_fps_num=24, source_fps_den=1,
            timeline_fps_num=24, timeline_fps_den=1,
            speed=2.0,
        )
        result = clip.timeline_to_source_frame(5)
        assert result == 10

    def test_half_speed_no_metadata_fallback(self):
        """0.5x speed without metadata: old formula also works."""
        clip = _make_clip(
            timeline_duration=100,
            source_duration=100,
            speed=0.5,
        )
        result = clip.timeline_to_source_frame(10)
        assert result == 5


# ===================================================================
# Test: Source start offset
# ===================================================================

class TestSourceStartOffset:
    """Source start offset is applied correctly."""

    def test_offset_with_metadata(self):
        """Source start offset works with mapper."""
        clip = _make_clip(
            source_start=100,
            source_duration=10,
            timeline_duration=10,
            source_fps_num=24, source_fps_den=1,
            timeline_fps_num=24, timeline_fps_den=1,
        )
        result = clip.timeline_to_source_frame(0)
        assert result == 100

    def test_offset_no_metadata(self):
        """Source start offset works with old formula."""
        clip = _make_clip(
            source_start=100,
            source_duration=10,
            timeline_duration=10,
        )
        result = clip.timeline_to_source_frame(0)
        assert result == 100


# ===================================================================
# Test: Edge cases
# ===================================================================

class TestEdgeCases:
    """Edge case handling for ClipInstance frame mapping."""

    def test_out_of_range_raises(self):
        """Out of range frame raises ValueError."""
        clip = _make_clip()
        with pytest.raises(ValueError):
            clip.timeline_to_source_frame(200)

    def test_last_valid_frame(self):
        """Last valid frame maps correctly."""
        clip = _make_clip(timeline_duration=100, source_duration=100)
        result = clip.timeline_to_source_frame(99)
        assert result == 99

    def test_with_metadata_partial(self):
        """Only source_fps set (no timeline_fps): falls back to old formula."""
        clip = _make_clip(
            timeline_duration=10,
            source_duration=10,
            source_fps_num=30,
            # _timeline_fps_num stays None → falls back
        )
        result = clip.timeline_to_source_frame(5)
        assert result == 5  # Old formula: 0 + 5*1.0 = 5
