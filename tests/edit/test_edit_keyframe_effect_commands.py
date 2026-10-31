"""Tests for effect-scoped keyframe commands (Phase 3 / round 2).

These verify that ``AddKeyframeCommand`` and ``RemoveKeyframeCommand``
write keyframes to ``EffectInstance.animated_params`` (not the clip's
``animated_params``) when an ``effect_id`` is supplied — the data path
that makes per-effect parameter animation work.

The clip-level keyframe path (opacity / transform.*) is already covered
by other suites; here we focus on the effect branch added in round 2.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    EditTrack,
    ClipInstance,
)
from src.features.edit.core.clip_params import TransformParams, CropParams, SpeedParams
from src.features.edit.core.commands import (
    AddKeyframeCommand,
    RemoveKeyframeCommand,
)
from src.features.edit.core.effect_instance import EffectInstance


# ================================================================
# Fixtures
# ================================================================

@pytest.fixture
def timeline_with_effect_clip():
    """A timeline → track → clip with one blur effect attached.

    The effect's ``effect_type`` is ``"blur"`` so the catalog lookup in
    ``AddKeyframeCommand._effect_param_range`` resolves a real ParamDef
    (radius: 0.0–50.0).
    """
    timeline = TimelineSequence(
        timeline_id=str(uuid.uuid4()), name="T", fps=24,
        width=1920, height=1080, sample_rate=48000,
    )
    track = EditTrack(
        track_id="track_1", name="V1", track_type="video", index=0,
    )
    timeline = timeline.with_track_added(track)

    clip = ClipInstance(
        clip_id="clip_1", asset_id="asset_1", track_id="track_1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
        speed=SpeedParams(speed=1.0),
        transform=TransformParams(), crop=CropParams(), opacity=1.0,
    )
    timeline = timeline.with_clip_added("track_1", clip)

    effect = EffectInstance(
        effect_id="eff_blur_1",
        effect_type="blur",
        params={"radius": 5.0, "blur_type": "gaussian", "mix": 1.0},
    )
    clip = timeline.get_track_by_id("track_1").get_clip_by_id("clip_1")
    clip = clip.with_effect_added(effect)
    timeline = timeline.with_track_updated(
        timeline.get_track_by_id("track_1").with_clip_updated(clip)
    )
    return timeline


def _get_effect(timeline: TimelineSequence, effect_id: str = "eff_blur_1"):
    clip = timeline.get_track_by_id("track_1").get_clip_by_id("clip_1")
    return clip.get_effect_by_id(effect_id)


# ================================================================
# AddKeyframeCommand — effect branch
# ================================================================

class TestAddKeyframeEffect:
    def test_adds_keyframe_to_effect_animated_params(self, timeline_with_effect_clip):
        """The keyframe lands in EffectInstance.animated_params, not
        ClipInstance.animated_params."""
        cmd = AddKeyframeCommand.create(
            track_id="track_1", clip_id="clip_1",
            param_name="radius", frame=10, value=8.0,
            effect_id="eff_blur_1",
        )
        new_tl = cmd.apply(timeline_with_effect_clip)

        effect = _get_effect(new_tl)
        assert "radius" in effect.animated_params
        assert effect.is_param_animated("radius") is True
        assert effect.has_keyframe_at("radius", 10) is True

        # And the clip's own animated_params must NOT be touched.
        clip = new_tl.get_track_by_id("track_1").get_clip_by_id("clip_1")
        assert "radius" not in clip.animated_params

    def test_min_max_from_catalog_param_def(self, timeline_with_effect_clip):
        """The AnimatableParam's min/max come from the catalog's ParamDef
        (blur.radius → 0.0–100.0), not the hardcoded (0, 100) default."""
        cmd = AddKeyframeCommand.create(
            track_id="track_1", clip_id="clip_1",
            param_name="radius", frame=0, value=5.0,
            effect_id="eff_blur_1",
        )
        new_tl = cmd.apply(timeline_with_effect_clip)
        anim = _get_effect(new_tl).animated_params["radius"]
        # blur.radius ParamDef declares min_val=0.0, max_val=100.0.
        # We assert max is the catalog value (100.0) to prove the lookup
        # ran — not the generic fallback which also happens to be 100.0.
        # Use a different param to make the assertion meaningful: mix has
        # max 1.0, which the (0,100) fallback would never produce.
        cmd2 = AddKeyframeCommand.create(
            track_id="track_1", clip_id="clip_1",
            param_name="mix", frame=0, value=0.5,
            effect_id="eff_blur_1",
        )
        new_tl2 = cmd2.apply(new_tl)
        anim_mix = _get_effect(new_tl2).animated_params["mix"]
        assert anim_mix.min_value == pytest.approx(0.0)
        assert anim_mix.max_value == pytest.approx(1.0)  # NOT the 100 fallback

    def test_clip_path_unchanged_when_no_effect_id(self, timeline_with_effect_clip):
        """Without effect_id the command still writes clip.animated_params
        (opacity path) — regression guard for the branch logic."""
        cmd = AddKeyframeCommand.create(
            track_id="track_1", clip_id="clip_1",
            param_name="opacity", frame=0, value=0.5,
        )
        new_tl = cmd.apply(timeline_with_effect_clip)
        clip = new_tl.get_track_by_id("track_1").get_clip_by_id("clip_1")
        assert "opacity" in clip.animated_params
        # Effect untouched.
        assert _get_effect(new_tl).animated_params == {}

    def test_undo_removes_the_keyframe(self, timeline_with_effect_clip):
        cmd = AddKeyframeCommand.create(
            track_id="track_1", clip_id="clip_1",
            param_name="radius", frame=10, value=8.0,
            effect_id="eff_blur_1",
        )
        new_tl = cmd.apply(timeline_with_effect_clip)
        assert _get_effect(new_tl).has_keyframe_at("radius", 10)

        restored = cmd.undo(new_tl)
        assert _get_effect(restored).has_keyframe_at("radius", 10) is False

    def test_serialization_round_trips_effect_id(self, timeline_with_effect_clip):
        cmd = AddKeyframeCommand.create(
            track_id="track_1", clip_id="clip_1",
            param_name="radius", frame=5, value=3.0,
            effect_id="eff_blur_1",
        )
        data = cmd.to_dict()
        assert data["effect_id"] == "eff_blur_1"

        restored = AddKeyframeCommand.from_dict(data)
        assert restored.effect_id == "eff_blur_1"

    def test_unknown_effect_id_raises(self, timeline_with_effect_clip):
        """A bogus effect_id should raise (not silently no-op) so the
        caller knows the command couldn't run."""
        cmd = AddKeyframeCommand.create(
            track_id="track_1", clip_id="clip_1",
            param_name="radius", frame=0, value=1.0,
            effect_id="does_not_exist",
        )
        with pytest.raises(ValueError, match="Effect not found"):
            cmd.apply(timeline_with_effect_clip)


# ================================================================
# RemoveKeyframeCommand — effect branch
# ================================================================

class TestRemoveKeyframeEffect:
    def test_removes_keyframe_from_effect(self, timeline_with_effect_clip):
        """Add then remove → effect.animated_params cleared for that param."""
        add = AddKeyframeCommand.create(
            track_id="track_1", clip_id="clip_1",
            param_name="radius", frame=10, value=8.0,
            effect_id="eff_blur_1",
        )
        tl_with_kf = add.apply(timeline_with_effect_clip)
        assert _get_effect(tl_with_kf).has_keyframe_at("radius", 10)

        rm = RemoveKeyframeCommand.create(
            track_id="track_1", clip_id="clip_1",
            param_name="radius", frame=10,
            effect_id="eff_blur_1",
        )
        tl_removed = rm.apply(tl_with_kf)
        assert _get_effect(tl_removed).has_keyframe_at("radius", 10) is False

    def test_clip_path_unchanged_when_no_effect_id(self, timeline_with_effect_clip):
        """Without effect_id, RemoveKeyframeCommand still targets the
        clip's animated_params — regression guard."""
        # Seed a clip-level keyframe first.
        add_clip_kf = AddKeyframeCommand.create(
            track_id="track_1", clip_id="clip_1",
            param_name="opacity", frame=0, value=0.5,
        )
        tl = add_clip_kf.apply(timeline_with_effect_clip)
        clip = tl.get_track_by_id("track_1").get_clip_by_id("clip_1")
        assert clip.has_keyframe_at("opacity", 0)

        rm = RemoveKeyframeCommand.create(
            track_id="track_1", clip_id="clip_1",
            param_name="opacity", frame=0,
        )
        tl2 = rm.apply(tl)
        clip2 = tl2.get_track_by_id("track_1").get_clip_by_id("clip_1")
        assert clip2.has_keyframe_at("opacity", 0) is False

    def test_serialization_round_trips_effect_id(self):
        rm = RemoveKeyframeCommand.create(
            track_id="track_1", clip_id="clip_1",
            param_name="radius", frame=10,
            effect_id="eff_blur_1",
        )
        data = rm.to_dict()
        assert data["effect_id"] == "eff_blur_1"
        restored = RemoveKeyframeCommand.from_dict(data)
        assert restored.effect_id == "eff_blur_1"

    def test_remove_without_effect_id_default_is_none(self):
        """The new effect_id field defaults to None so legacy callers
        (which don't pass it) keep hitting the clip path."""
        rm = RemoveKeyframeCommand.create(
            track_id="t", clip_id="c", param_name="opacity", frame=0,
        )
        assert rm.effect_id is None
