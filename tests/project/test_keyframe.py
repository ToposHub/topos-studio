"""
Tests for keyframe animation system.

Tests:
- Keyframe creation and serialization
- KeyframeTrack interpolation
- Interpolation functions (linear, bezier, ease)
- ParamDef animation support
- NodeSnapshot keyframe data passing
"""

import pytest
from src.features.composite.utils.keyframe import (
    Keyframe, KeyframeTrack, InterpolationType,
    interpolate, bezier_interpolate, ease_in_interpolate,
    ease_out_interpolate, ease_in_out_interpolate,
    create_simple_track, validate_track
)
from src.features.composite.nodes.base_node import ParamDef
from src.features.composite.core.graph_snapshot import NodeSnapshot, GraphSnapshot

import pytest
pytestmark = pytest.mark.composite_core


class TestKeyframe:
    """Test Keyframe class."""

    def test_keyframe_creation(self):
        """Test basic keyframe creation."""
        kf = Keyframe(frame=10, value=0.5)
        assert kf.frame == 10
        assert kf.value == 0.5
        assert kf.interpolation == InterpolationType.LINEAR

    def test_keyframe_with_bezier(self):
        """Test keyframe with bezier interpolation."""
        kf = Keyframe(
            frame=20,
            value=0.8,
            interpolation=InterpolationType.BEZIER,
            bezier_in=(0.1, 0.0),
            bezier_out=(0.9, 1.0)
        )
        assert kf.interpolation == InterpolationType.BEZIER
        assert kf.bezier_in == (0.1, 0.0)
        assert kf.bezier_out == (0.9, 1.0)

    def test_keyframe_serialization(self):
        """Test keyframe serialization to/from dict."""
        kf = Keyframe(frame=30, value=0.6, interpolation=InterpolationType.EASE_IN)
        data = kf.to_dict()
        assert data["frame"] == 30
        assert data["value"] == 0.6
        assert data["interpolation"] == "ease_in"

        kf2 = Keyframe.from_dict(data)
        assert kf2.frame == 30
        assert kf2.value == 0.6
        assert kf2.interpolation == InterpolationType.EASE_IN

    def test_keyframe_sorting(self):
        """Test keyframe sorting by frame."""
        kf1 = Keyframe(frame=30, value=0.5)
        kf2 = Keyframe(frame=10, value=0.3)
        kf3 = Keyframe(frame=20, value=0.4)
        sorted_kfs = sorted([kf1, kf2, kf3])
        assert sorted_kfs[0].frame == 10
        assert sorted_kfs[1].frame == 20
        assert sorted_kfs[2].frame == 30


class TestKeyframeTrack:
    """Test KeyframeTrack class."""

    def test_track_creation(self):
        """Test basic track creation."""
        track = KeyframeTrack(param_name="opacity", min_val=0.0, max_val=1.0)
        assert track.param_name == "opacity"
        assert track.min_val == 0.0
        assert track.max_val == 1.0

    def test_add_keyframes(self):
        """Test adding keyframes to track."""
        track = KeyframeTrack(param_name="brightness")
        track.add_keyframe(Keyframe(0, 0.0))
        track.add_keyframe(Keyframe(100, 1.0))
        assert len(track.keyframes) == 2
        assert track.keyframes[0].frame == 0
        assert track.keyframes[1].frame == 100

    def test_remove_keyframe(self):
        """Test removing keyframe."""
        track = KeyframeTrack(param_name="contrast")
        track.add_keyframe(Keyframe(0, 0.0))
        track.add_keyframe(Keyframe(50, 0.5))
        track.add_keyframe(Keyframe(100, 1.0))
        result = track.remove_keyframe(50)
        assert result is True
        assert len(track.keyframes) == 2

    def test_get_value_linear(self):
        """Test linear interpolation between keyframes."""
        track = KeyframeTrack(param_name="gain")
        track.add_keyframe(Keyframe(0, 0.0, InterpolationType.LINEAR))
        track.add_keyframe(Keyframe(100, 1.0, InterpolationType.LINEAR))

        # At exact keyframe
        assert track.get_value(0) == 0.0
        assert track.get_value(100) == 1.0

        # Between keyframes (linear interpolation)
        assert track.get_value(50) == 0.5
        assert track.get_value(25) == 0.25
        assert track.get_value(75) == 0.75

    def test_get_value_hold(self):
        """Test hold interpolation."""
        track = KeyframeTrack(param_name="gamma")
        track.add_keyframe(Keyframe(0, 0.0, InterpolationType.HOLD))
        track.add_keyframe(Keyframe(100, 1.0, InterpolationType.HOLD))

        # Hold maintains previous value
        assert track.get_value(0) == 0.0
        assert track.get_value(50) == 0.0  # Held from kf1
        assert track.get_value(99) == 0.0  # Still held
        assert track.get_value(100) == 1.0  # At kf2

    def test_get_value_outside_range(self):
        """Test value before first and after last keyframe."""
        track = KeyframeTrack(param_name="saturation")
        track.add_keyframe(Keyframe(10, 0.5))
        track.add_keyframe(Keyframe(90, 0.8))

        # Before first keyframe - return first value
        assert track.get_value(0) == 0.5
        assert track.get_value(5) == 0.5

        # After last keyframe - return last value
        assert track.get_value(100) == 0.8
        assert track.get_value(200) == 0.8

    def test_linear_extrapolation_uses_end_slopes(self):
        """Spline's linear tails must be evaluated by the renderer too."""
        track = KeyframeTrack(
            param_name="position", min_val=-100.0, max_val=100.0,
            pre_extrap="linear", post_extrap="linear")
        track.add_keyframe(Keyframe(10, 0.0))
        track.add_keyframe(Keyframe(20, 10.0))

        assert track.get_value(0) == pytest.approx(-10.0)
        assert track.get_value(30) == pytest.approx(20.0)

    def test_track_serialization(self):
        """Test track serialization to/from dict."""
        track = KeyframeTrack(param_name="exposure", min_val=0.0, max_val=2.0)
        track.add_keyframe(Keyframe(0, 0.0))
        track.add_keyframe(Keyframe(50, 1.0))
        track.add_keyframe(Keyframe(100, 2.0))

        data = track.to_dict()
        assert data["param_name"] == "exposure"
        assert data["min_val"] == 0.0
        assert data["max_val"] == 2.0
        assert len(data["keyframes"]) == 3

        track2 = KeyframeTrack.from_dict(data)
        assert track2.param_name == "exposure"
        assert len(track2.keyframes) == 3


class TestInterpolationFunctions:
    """Test interpolation functions."""

    def test_linear_interpolation(self):
        """Test linear interpolation."""
        kf1 = Keyframe(0, 0.0, InterpolationType.LINEAR)
        kf2 = Keyframe(100, 1.0, InterpolationType.LINEAR)

        # At exact keyframes
        assert interpolate(0, kf1, kf2) == 0.0
        assert interpolate(100, kf1, kf2) == 1.0

        # Between keyframes
        assert interpolate(50, kf1, kf2) == 0.5
        assert interpolate(25, kf1, kf2) == 0.25

    def test_ease_in_interpolation(self):
        """Test ease in interpolation (quadratic slow start)."""
        kf1 = Keyframe(0, 0.0, InterpolationType.EASE_IN)
        kf2 = Keyframe(100, 1.0, InterpolationType.EASE_IN)

        # At start/end
        assert ease_in_interpolate(0.0, kf1, kf2) == 0.0
        assert ease_in_interpolate(1.0, kf1, kf2) == 1.0

        # At t=0.5, eased_t = 0.25
        result = ease_in_interpolate(0.5, kf1, kf2)
        assert result == 0.25  # t^2 * range

    def test_ease_out_interpolation(self):
        """Test ease out interpolation (quadratic slow end)."""
        kf1 = Keyframe(0, 0.0, InterpolationType.EASE_OUT)
        kf2 = Keyframe(100, 1.0, InterpolationType.EASE_OUT)

        # At start/end
        assert ease_out_interpolate(0.0, kf1, kf2) == 0.0
        assert ease_out_interpolate(1.0, kf1, kf2) == 1.0

        # At t=0.5, eased_t = 0.75
        result = ease_out_interpolate(0.5, kf1, kf2)
        assert result == 0.75  # 1 - (1-t)^2 * range

    def test_clamping(self):
        """Test interpolation clamping to min/max."""
        kf1 = Keyframe(0, 0.0)
        kf2 = Keyframe(100, 100.0)

        # Values should be clamped to 0-1 range
        result = interpolate(50, kf1, kf2, min_val=0.0, max_val=1.0)
        assert result == 1.0  # Clamped to max


class TestParamDefAnimation:
    """Test ParamDef animation support."""

    def test_animatable_check(self):
        """Test is_animatable() method."""
        float_def = ParamDef("gain", ptype="float")
        int_def = ParamDef("radius", ptype="int")
        str_def = ParamDef("name", ptype="str")
        bool_def = ParamDef("enabled", ptype="bool")
        option_def = ParamDef("mode", ptype="option", labels=["a", "b"])
        vec2_def = ParamDef("position", ptype="vec2")

        assert float_def.is_animatable() is True
        assert int_def.is_animatable() is True
        assert str_def.is_animatable() is False
        assert bool_def.is_animatable() is True
        assert option_def.is_animatable() is True
        assert vec2_def.is_animatable() is True
        assert bool_def.is_expression_compatible() is False
        assert float_def.is_expression_compatible() is True

    def test_animated_flag(self):
        """Test animated flag in ParamDef."""
        pdef = ParamDef("opacity", ptype="float", animated=True)
        assert pdef.animated is True

    def test_keyframes_storage(self):
        """Test keyframes storage in ParamDef."""
        keyframes = [
            {"frame": 0, "value": 0.0, "interpolation": "linear"},
            {"frame": 100, "value": 1.0, "interpolation": "linear"},
        ]
        pdef = ParamDef("brightness", ptype="float", animated=True, keyframes=keyframes)
        assert pdef.keyframes == keyframes

    def test_get_keyframe_track(self):
        """Test get_keyframe_track() method."""
        keyframes = [
            {"frame": 0, "value": 0.5, "interpolation": "linear"},
            {"frame": 50, "value": 0.8, "interpolation": "ease_in"},
        ]
        pdef = ParamDef("contrast", ptype="float", animated=True,
                       keyframes=keyframes, min_val=0.0, max_val=1.0)

        track = pdef.get_keyframe_track()
        assert track is not None
        assert track.param_name == "contrast"
        assert len(track.keyframes) == 2


class TestNodeSnapshotKeyframes:
    """Test NodeSnapshot keyframe data passing."""

    def test_node_snapshot_keyframe_tracks(self):
        """Test NodeSnapshot stores keyframe_tracks."""
        ns = NodeSnapshot(
            node_id="grade_01",
            node_type="grade",
            parameters={"gain": 1.0},
            disabled=False,
            input_ports=[("input", "image")],
            output_ports=[("result", "image")],
            keyframe_tracks={
                "gain": {
                    "param_name": "gain",
                    "keyframes": [{"frame": 0, "value": 1.0}],
                    "min_val": 0.0,
                    "max_val": 4.0,
                }
            },
            param_defs_meta={
                "gain": {"ptype": "float", "min_val": 0.0, "max_val": 4.0, "animated": True}
            }
        )
        assert "gain" in ns.keyframe_tracks
        assert ns.keyframe_tracks["gain"]["min_val"] == 0.0


class TestGraphSnapshotFrameNumber:
    """Test GraphSnapshot frame_number field."""

    def test_frame_number_default(self):
        """Test default frame number is 0."""
        gs = GraphSnapshot(generation=1)
        assert gs.frame_number == 0

    def test_frame_number_setting(self):
        """Test setting frame number."""
        gs = GraphSnapshot(generation=1, frame_number=50)
        assert gs.frame_number == 50


class TestUtilityFunctions:
    """Test utility functions."""

    def test_create_simple_track(self):
        """Test create_simple_track helper."""
        track = create_simple_track(
            "opacity", 0, 0.0, 100, 1.0,
            InterpolationType.EASE_IN_OUT
        )
        assert track.param_name == "opacity"
        assert len(track.keyframes) == 2
        assert track.keyframes[0].interpolation == InterpolationType.EASE_IN_OUT

    def test_validate_track_errors(self):
        """Test validate_track error detection.

        P1.8 migration: add_keyframe() now REPLACES a keyframe at an existing
        frame instead of allowing duplicates, so the old "duplicate frame"
        scenario cannot be constructed via the public API any more. This test
        now verifies the out-of-range value branch of validate_track instead.
        """
        # Track with a value outside its declared range
        track = KeyframeTrack("test", min_val=0.0, max_val=1.0)
        track.add_keyframe(Keyframe(10, 5.0))  # 5.0 is outside [0, 1]

        errors = validate_track(track)
        assert len(errors) > 0
        assert "outside range" in errors[0]


# Run tests if executed directly
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
