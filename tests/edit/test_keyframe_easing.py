"""Keyframe easing (interpolation curve) tests.

The shared keyframe module (src/shared/keyframe.py) already implements the
easing curves and interpolation math. These tests cover the edit-layer
wiring that was missing before this change:

1. The render plan evaluates animated params per frame (so easing actually
   affects output), and falls back to static values when there are no
   keyframes.
2. SetKeyframeInterpolationCommand switches a keyframe's easing curve and
   is undoable.
3. AnimatableParam.update_keyframe / update_keyframe_interpolation preserve
   values while changing the curve.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.commands import (
    CommandStack,
    SetKeyframeInterpolationCommand,
)
from src.features.edit.core.effect_instance import AnimatableParam
from src.features.edit.core.timeline_render_plan import TimelineRenderPlan
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack
from src.shared.media.metadata import Rational


# ── Fixtures ──


def _opacity_anim(interpolation: InterpolationType = InterpolationType.LINEAR) -> AnimatableParam:
    kt = KeyframeTrack(
        param_name="opacity",
        keyframes=(
            Keyframe(frame=0, value=0.0, interpolation=interpolation),
            Keyframe(frame=10, value=1.0, interpolation=interpolation),
        ),
    )
    return AnimatableParam(base_value=1.0, keyframes=kt)


def _sequence_with_opacity(anim: AnimatableParam) -> TimelineSequence:
    clip = ClipInstance(
        clip_id="c1", asset_id="a1", track_id="t1",
        timeline_start=0, timeline_duration=20, source_start=0, source_duration=20,
        opacity=1.0, animated_params={"opacity": anim},
    )
    track = EditTrack(track_id="t1", name="V1", track_type="video", index=0, clips=(clip,))
    return TimelineSequence(
        timeline_id="s", name="S", fps=Rational(24, 1),
        width=1920, height=1080, video_tracks=(track,),
    )


def _opacity_at(seq: TimelineSequence, frame: int) -> float:
    plan = TimelineRenderPlan(seq)
    return plan.get_frame_plan(frame).layers[0].opacity


@pytest.fixture
def linear_sequence() -> TimelineSequence:
    return _sequence_with_opacity(_opacity_anim(InterpolationType.LINEAR))


# ── Render-plan evaluation ──


class TestEasingRenderEvaluation:
    def test_linear_midpoint(self, linear_sequence: TimelineSequence):
        # linear 0→1 over frames 0..10, midpoint ~0.5
        assert abs(_opacity_at(linear_sequence, 5) - 0.5) < 0.05

    def test_ease_in_slow_start(self, linear_sequence: TimelineSequence):
        """ease_in should produce a value well below linear at the midpoint."""
        cmd = SetKeyframeInterpolationCommand.create("t1", "c1", "opacity", 0, "ease_in")
        seq, _ = cmd.apply(linear_sequence)
        mid = _opacity_at(seq, 5)
        # ease_in (quadratic t²): t=0.5 → 0.25
        assert abs(mid - 0.25) < 0.06
        assert mid < 0.4  # clearly below linear

    def test_ease_out_fast_start(self, linear_sequence: TimelineSequence):
        """ease_out should produce a value well above linear at the midpoint."""
        cmd = SetKeyframeInterpolationCommand.create("t1", "c1", "opacity", 0, "ease_out")
        seq, _ = cmd.apply(linear_sequence)
        mid = _opacity_at(seq, 5)
        # ease_out: t=0.5 → 0.75
        assert abs(mid - 0.75) < 0.06
        assert mid > 0.6

    def test_ease_in_out_symmetric(self, linear_sequence: TimelineSequence):
        """ease_in_out is symmetric, so the midpoint is still ~0.5."""
        cmd = SetKeyframeInterpolationCommand.create("t1", "c1", "opacity", 0, "ease_in_out")
        seq, _ = cmd.apply(linear_sequence)
        assert abs(_opacity_at(seq, 5) - 0.5) < 0.06

    def test_endpoints_exact(self, linear_sequence: TimelineSequence):
        """Regardless of easing, exact keyframe frames give exact values."""
        assert abs(_opacity_at(linear_sequence, 0) - 0.0) < 0.01
        assert abs(_opacity_at(linear_sequence, 10) - 1.0) < 0.01


class TestStaticFallback:
    """Clips without keyframes keep their static values (no regression)."""

    def test_static_opacity_preserved(self):
        clip = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="t1",
            timeline_start=0, timeline_duration=20, source_start=0, source_duration=20,
            opacity=0.7,
        )
        track = EditTrack(track_id="t1", name="V1", track_type="video", index=0, clips=(clip,))
        seq = TimelineSequence(
            timeline_id="s", name="S", fps=Rational(24, 1),
            width=1920, height=1080, video_tracks=(track,),
        )
        # opacity at any frame is the static value (× fade factor 1.0)
        assert abs(_opacity_at(seq, 5) - 0.7) < 0.01

    def test_static_transform_preserved(self):
        clip = ClipInstance(
            clip_id="c1", asset_id="a1", track_id="t1",
            timeline_start=0, timeline_duration=20, source_start=0, source_duration=20,
        )
        track = EditTrack(track_id="t1", name="V1", track_type="video", index=0, clips=(clip,))
        seq = TimelineSequence(
            timeline_id="s", name="S", fps=Rational(24, 1),
            width=1920, height=1080, video_tracks=(track,),
        )
        layer = TimelineRenderPlan(seq).get_frame_plan(5).layers[0]
        assert layer.transform[0] == 0.0  # translate_x
        assert layer.transform[2] == 1.0  # scale_x


# ── Command ──


class TestSetKeyframeInterpolationCommand:
    def test_changes_curve(self, linear_sequence: TimelineSequence):
        cmd = SetKeyframeInterpolationCommand.create("t1", "c1", "opacity", 0, "ease_in")
        seq, updated = cmd.apply(linear_sequence)
        # The keyframe at frame 0 should now be ease_in
        clip = seq.get_track_by_id("t1").get_clip_by_id("c1")
        kf = clip.animated_params["opacity"].keyframes.get_keyframe_at(0)
        assert kf.interpolation == InterpolationType.EASE_IN
        assert updated._old_interpolation is not None

    def test_undo_restores_curve(self, linear_sequence: TimelineSequence):
        stack = CommandStack()
        cmd = SetKeyframeInterpolationCommand.create("t1", "c1", "opacity", 0, "ease_in")
        tl = stack.execute(cmd, linear_sequence)
        mid_after = _opacity_at(tl, 5)
        assert mid_after < 0.4  # ease_in applied
        tl2 = stack.undo(tl)
        assert abs(_opacity_at(tl2, 5) - 0.5) < 0.05  # back to linear

    def test_preserves_value(self, linear_sequence: TimelineSequence):
        """Switching the curve must not change the keyframe's value."""
        cmd = SetKeyframeInterpolationCommand.create("t1", "c1", "opacity", 10, "ease_out")
        seq, _ = cmd.apply(linear_sequence)
        kf = seq.get_track_by_id("t1").get_clip_by_id("c1").animated_params["opacity"].keyframes.get_keyframe_at(10)
        assert kf.value == 1.0
        assert kf.interpolation == InterpolationType.EASE_OUT

    def test_rejects_missing_keyframe(self, linear_sequence: TimelineSequence):
        cmd = SetKeyframeInterpolationCommand.create("t1", "c1", "opacity", 999, "ease_in")
        with pytest.raises(ValueError):
            cmd.apply(linear_sequence)

    def test_serialization(self):
        cmd = SetKeyframeInterpolationCommand.create("t1", "c1", "opacity", 5, "ease_in_out")
        d = cmd.to_dict()
        assert d["command_type"] == "SetKeyframeInterpolationCommand"
        assert d["new_interpolation"] == "ease_in_out"
        restored = SetKeyframeInterpolationCommand.from_dict(d)
        assert restored.new_interpolation == "ease_in_out"


# ── AnimatableParam helpers ──


class TestAnimatableParamInterpolation:
    def test_update_keyframe_interpolation_preserves_value(self):
        anim = _opacity_anim(InterpolationType.LINEAR)
        new_anim = anim.update_keyframe_interpolation(0, InterpolationType.EASE_IN)
        kf = new_anim.keyframes.get_keyframe_at(0)
        assert kf.interpolation == InterpolationType.EASE_IN
        assert kf.value == 0.0  # value unchanged

    def test_update_keyframe_with_interpolation(self):
        anim = _opacity_anim(InterpolationType.LINEAR)
        new_anim = anim.update_keyframe(0, 0.5, InterpolationType.EASE_OUT)
        kf = new_anim.keyframes.get_keyframe_at(0)
        assert kf.value == 0.5
        assert kf.interpolation == InterpolationType.EASE_OUT

    def test_update_keyframe_keeps_interpolation_when_none(self):
        anim = _opacity_anim(InterpolationType.EASE_IN)
        new_anim = anim.update_keyframe(0, 0.3)  # no interpolation arg
        kf = new_anim.keyframes.get_keyframe_at(0)
        assert kf.value == 0.3
        assert kf.interpolation == InterpolationType.EASE_IN  # preserved
