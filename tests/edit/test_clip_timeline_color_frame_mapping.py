"""
Test ClipInstance frame mapping methods.

Validates:
- timeline_to_source_frame
- source_to_timeline_frame
- clip_local_to_source_frame
- source_to_clip_local_frame
- SpeedParams integration
- Boundary and error cases
"""

import pytest

from src.features.edit.core.timeline_v2 import ClipInstance
from src.features.edit.core.clip_params import SpeedParams


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_clip(
    timeline_start: int = 0,
    timeline_duration: int = 100,
    source_start: int = 0,
    source_duration: int = 100,
    speed: float = 1.0,
) -> ClipInstance:
    """Create a ClipInstance with sensible defaults for testing."""
    return ClipInstance(
        clip_id="test-clip",
        asset_id="test-asset",
        track_id="test-track",
        timeline_start=timeline_start,
        timeline_duration=timeline_duration,
        source_start=source_start,
        source_duration=source_duration,
        speed=speed,
    )


def _make_clip_speedparams(
    timeline_start: int = 0,
    timeline_duration: int = 100,
    source_start: int = 0,
    source_duration: int = 100,
    speed: float = 1.0,
) -> ClipInstance:
    """Create a ClipInstance using SpeedParams instead of raw float."""
    return ClipInstance(
        clip_id="test-clip",
        asset_id="test-asset",
        track_id="test-track",
        timeline_start=timeline_start,
        timeline_duration=timeline_duration,
        source_start=source_start,
        source_duration=source_duration,
        speed=SpeedParams(speed=speed),
    )


# ===================================================================
# Test: timeline_to_source_frame — normal speed (1.0x)
# ===================================================================

class TestTimelineToSourceFrameNormalSpeed:
    """timeline_to_source_frame at speed 1.0."""

    def test_basic_mapping(self):
        """timeline_start=100, source_start=50: timeline 110 -> source 60."""
        clip = _make_clip(timeline_start=100, source_start=50,
                          timeline_duration=50, source_duration=50)
        assert clip.timeline_to_source_frame(110) == 60

    def test_first_frame(self):
        """First frame of clip maps to source_start."""
        clip = _make_clip(timeline_start=100, source_start=50,
                          timeline_duration=50, source_duration=50)
        assert clip.timeline_to_source_frame(100) == 50

    def test_last_frame(self):
        """Last frame (timeline_end - 1) maps correctly."""
        clip = _make_clip(timeline_start=100, source_start=50,
                          timeline_duration=50, source_duration=50)
        # timeline_end = 150, last valid = 149
        # relative = 149 - 100 = 49, source = 50 + 49 = 99
        assert clip.timeline_to_source_frame(149) == 99

    def test_zero_offset(self):
        """timeline_start=0, source_start=0: identity mapping."""
        clip = _make_clip(timeline_start=0, source_start=0,
                          timeline_duration=100, source_duration=100)
        assert clip.timeline_to_source_frame(0) == 0
        assert clip.timeline_to_source_frame(50) == 50
        assert clip.timeline_to_source_frame(99) == 99


# ===================================================================
# Test: timeline_to_source_frame — trimmed clips
# ===================================================================

class TestTimelineToSourceFrameTrimmed:
    """Trimmed clips where source_start != 0."""

    def test_trimmed_source_start(self):
        """source_start=200, timeline start -> source 200."""
        clip = _make_clip(timeline_start=0, source_start=200,
                          timeline_duration=50, source_duration=50)
        assert clip.timeline_to_source_frame(0) == 200

    def test_trimmed_offset(self):
        """Trimmed clip with offset."""
        clip = _make_clip(timeline_start=50, source_start=200,
                          timeline_duration=30, source_duration=30)
        # timeline frame 60 -> relative 10 -> source 200 + 10 = 210
        assert clip.timeline_to_source_frame(60) == 210

    def test_trimmed_with_float_speed(self):
        """Trimmed clip at speed 1.0 (float)."""
        clip = ClipInstance(
            clip_id="t", asset_id="a", track_id="trk",
            timeline_start=10, timeline_duration=20,
            source_start=500, source_duration=20,
            speed=1.0,
        )
        assert clip.timeline_to_source_frame(10) == 500
        assert clip.timeline_to_source_frame(20) == 510


# ===================================================================
# Test: timeline_to_source_frame — speed 2.0x
# ===================================================================

class TestTimelineToSourceFrameSpeed2x:
    """Speed 2.0: clip plays at 2x, so source advances twice as fast."""

    def test_float_speed(self):
        """With float speed=2.0: timeline frame advances 1 -> source advances 2."""
        # source_duration=200, speed=2.0 -> timeline_duration should be 100
        clip = ClipInstance(
            clip_id="t", asset_id="a", track_id="trk",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=200,
            speed=2.0,
        )
        # timeline 0 -> source 0
        assert clip.timeline_to_source_frame(0) == 0
        # timeline 10 -> source 10 * 2 = 20
        assert clip.timeline_to_source_frame(10) == 20
        # timeline 50 -> source 100
        assert clip.timeline_to_source_frame(50) == 100

    def test_speed_params(self):
        """With SpeedParams(speed=2.0)."""
        clip = _make_clip_speedparams(
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=200,
            speed=2.0,
        )
        # SpeedParams.timeline_to_source_frames(10, 200) -> (20, 20)
        # source_start + 20 = 20
        assert clip.timeline_to_source_frame(10) == 20
        assert clip.timeline_to_source_frame(0) == 0
        assert clip.timeline_to_source_frame(50) == 100


# ===================================================================
# Test: timeline_to_source_frame — speed 0.5x
# ===================================================================

class TestTimelineToSourceFrameSpeedHalf:
    """Speed 0.5: slow motion, source advances half as fast."""

    def test_float_speed(self):
        """With float speed=0.5."""
        # source_duration=50, speed=0.5 -> timeline_duration should be 100
        clip = ClipInstance(
            clip_id="t", asset_id="a", track_id="trk",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=50,
            speed=0.5,
        )
        # timeline 0 -> source 0
        assert clip.timeline_to_source_frame(0) == 0
        # timeline 10 -> source int(10 * 0.5) = 5
        assert clip.timeline_to_source_frame(10) == 5
        # timeline 20 -> source int(20 * 0.5) = 10
        assert clip.timeline_to_source_frame(20) == 10

    def test_speed_params(self):
        """With SpeedParams(speed=0.5)."""
        clip = _make_clip_speedparams(
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=50,
            speed=0.5,
        )
        # SpeedParams.timeline_to_source_frames(10, 50) -> int(10 * 0.5) = 5
        assert clip.timeline_to_source_frame(10) == 5


# ===================================================================
# Test: clip_local_to_source_frame
# ===================================================================

class TestClipLocalToSourceFrame:
    """clip_local_to_source_frame: clip-local frame (0-based) -> source frame."""

    def test_normal_speed(self):
        """Normal speed, offset clip."""
        clip = _make_clip(timeline_start=100, source_start=50,
                          timeline_duration=50, source_duration=50)
        # clip_local 0 -> timeline 100 -> source 50
        assert clip.clip_local_to_source_frame(0) == 50
        # clip_local 10 -> timeline 110 -> source 60
        assert clip.clip_local_to_source_frame(10) == 60

    def test_speed_2x(self):
        """Speed 2.0: clip local 10 -> source offset 20."""
        clip = ClipInstance(
            clip_id="t", asset_id="a", track_id="trk",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=200,
            speed=2.0,
        )
        # clip_local 10 -> timeline 10 -> source 20
        assert clip.clip_local_to_source_frame(10) == 20

    def test_speed_2x_speedparams(self):
        """Speed 2.0 with SpeedParams."""
        clip = _make_clip_speedparams(
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=200,
            speed=2.0,
        )
        assert clip.clip_local_to_source_frame(10) == 20

    def test_speed_half(self):
        """Speed 0.5: clip local 10 -> source offset 5."""
        clip = ClipInstance(
            clip_id="t", asset_id="a", track_id="trk",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=50,
            speed=0.5,
        )
        # clip_local 10 -> timeline 10 -> source int(10 * 0.5) = 5
        assert clip.clip_local_to_source_frame(10) == 5

    def test_speed_half_speedparams(self):
        """Speed 0.5 with SpeedParams."""
        clip = _make_clip_speedparams(
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=50,
            speed=0.5,
        )
        assert clip.clip_local_to_source_frame(10) == 5


# ===================================================================
# Test: source_to_clip_local_frame
# ===================================================================

class TestSourceToClipLocalFrame:
    """source_to_clip_local_frame: source frame -> clip-local frame (0-based)."""

    def test_normal_speed(self):
        """Normal speed round-trip."""
        clip = _make_clip(timeline_start=100, source_start=50,
                          timeline_duration=50, source_duration=50)
        # source 60 -> timeline 110 -> clip_local 10
        assert clip.source_to_clip_local_frame(60) == 10

    def test_first_source_frame(self):
        """source_start maps to clip_local 0."""
        clip = _make_clip(timeline_start=100, source_start=50,
                          timeline_duration=50, source_duration=50)
        assert clip.source_to_clip_local_frame(50) == 0

    def test_speed_2x(self):
        """Speed 2.0: source 20 -> clip_local 10."""
        clip = ClipInstance(
            clip_id="t", asset_id="a", track_id="trk",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=200,
            speed=2.0,
        )
        # source 20 -> timeline int(20/2.0)=10 -> clip_local 10
        assert clip.source_to_clip_local_frame(20) == 10

    def test_speed_half(self):
        """Speed 0.5: source 5 -> clip_local 10."""
        clip = ClipInstance(
            clip_id="t", asset_id="a", track_id="trk",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=50,
            speed=0.5,
        )
        # source 5 -> timeline int(5/0.5)=10 -> clip_local 10
        assert clip.source_to_clip_local_frame(5) == 10


# ===================================================================
# Test: source_to_timeline_frame
# ===================================================================

class TestSourceToTimelineFrame:
    """source_to_timeline_frame: source frame -> absolute timeline frame."""

    def test_normal_speed(self):
        clip = _make_clip(timeline_start=100, source_start=50,
                          timeline_duration=50, source_duration=50)
        assert clip.source_to_timeline_frame(50) == 100
        assert clip.source_to_timeline_frame(60) == 110

    def test_speed_2x_float(self):
        clip = ClipInstance(
            clip_id="t", asset_id="a", track_id="trk",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=200,
            speed=2.0,
        )
        # source 20 -> timeline int(20/2.0) = 10
        assert clip.source_to_timeline_frame(20) == 10

    def test_speed_2x_speedparams(self):
        clip = _make_clip_speedparams(
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=200,
            speed=2.0,
        )
        assert clip.source_to_timeline_frame(20) == 10

    def test_speed_half(self):
        clip = ClipInstance(
            clip_id="t", asset_id="a", track_id="trk",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=50,
            speed=0.5,
        )
        # source 5 -> timeline int(5/0.5) = 10
        assert clip.source_to_timeline_frame(5) == 10


# ===================================================================
# Test: Boundary / error cases
# ===================================================================

class TestFrameMappingBoundaryErrors:
    """Frame outside clip range raises ValueError."""

    def test_timeline_frame_before_clip(self):
        clip = _make_clip(timeline_start=100, source_start=0,
                          timeline_duration=50, source_duration=50)
        with pytest.raises(ValueError, match="outside clip range"):
            clip.timeline_to_source_frame(99)

    def test_timeline_frame_at_end(self):
        """timeline_end is exclusive; accessing it should raise."""
        clip = _make_clip(timeline_start=100, source_start=0,
                          timeline_duration=50, source_duration=50)
        with pytest.raises(ValueError, match="outside clip range"):
            clip.timeline_to_source_frame(150)

    def test_timeline_frame_far_after(self):
        clip = _make_clip(timeline_start=0, source_start=0,
                          timeline_duration=10, source_duration=10)
        with pytest.raises(ValueError, match="outside clip range"):
            clip.timeline_to_source_frame(999)

    def test_source_frame_before_range(self):
        clip = _make_clip(timeline_start=0, source_start=50,
                          timeline_duration=50, source_duration=50)
        with pytest.raises(ValueError, match="outside clip range"):
            clip.source_to_timeline_frame(49)

    def test_source_frame_after_range(self):
        clip = _make_clip(timeline_start=0, source_start=50,
                          timeline_duration=50, source_duration=50)
        # source_end = 50 + 50 - 1 = 99; 100 is outside
        with pytest.raises(ValueError, match="outside clip range"):
            clip.source_to_timeline_frame(100)

    def test_clip_local_frame_at_duration(self):
        """clip_local_frame == timeline_duration is out of range."""
        clip = _make_clip(timeline_start=0, source_start=0,
                          timeline_duration=10, source_duration=10)
        with pytest.raises(ValueError, match="outside clip range"):
            clip.clip_local_to_source_frame(10)

    def test_clip_local_frame_negative(self):
        """Negative clip_local_frame is out of range."""
        clip = _make_clip(timeline_start=5, source_start=0,
                          timeline_duration=10, source_duration=10)
        with pytest.raises(ValueError, match="outside clip range"):
            clip.clip_local_to_source_frame(-1)


# ===================================================================
# Test: effective_speed
# ===================================================================

class TestEffectiveSpeed:
    """effective_speed() returns correct float in all cases."""

    def test_float_speed(self):
        clip = _make_clip(speed=2.0)
        assert clip.effective_speed() == 2.0

    def test_speed_params(self):
        clip = _make_clip_speedparams(speed=0.5)
        assert clip.effective_speed() == 0.5

    def test_default_speed(self):
        clip = _make_clip()
        assert clip.effective_speed() == 1.0


# ===================================================================
# Test: Properties
# ===================================================================

class TestProperties:
    """timeline_end and source_end."""

    def test_timeline_end(self):
        clip = _make_clip(timeline_start=100, timeline_duration=50)
        assert clip.timeline_end == 150

    def test_source_end(self):
        clip = _make_clip(source_start=200, source_duration=50)
        assert clip.source_end == 249  # inclusive: 200 + 50 - 1
