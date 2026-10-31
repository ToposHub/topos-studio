"""FX-C01 regression: effect edit must not drop Clip fields.

Any effect operation (Add / UpdateParam / SetEnabled / Remove) routes
through ``ClipInstance.with_effects()``. The old hand-written
constructor only carried a subset of fields and silently dropped
``label_color`` / fade / ColorStack / Composite / render-cache / FPS-PTS
metadata. That could make a blur cancel the fade, detach the color
stack, or lose the source-PTS map mid-edit.

This test pins the contract: after *any* effect operation, every non-
``effects`` field must be byte-for-byte equal to the original clip.

The file is written to FAIL before the fix (``with_effects`` rebuild),
so it guards the regression permanently.
"""

from __future__ import annotations

from src.features.edit.core.timeline_v2 import ClipInstance
from src.features.edit.core.effect_instance import EffectInstance


def _full_clip() -> ClipInstance:
    """Build a ClipInstance carrying every field that FX-C01 lists."""
    return ClipInstance(
        clip_id="clip-c01",
        asset_id="asset-1",
        track_id="track-1",
        timeline_start=10,
        timeline_duration=100,
        source_start=5,
        source_duration=100,
        is_enabled=True,
        label_color="#ff8800",
        fade_in_duration=4,
        fade_out_duration=6,
        color_stack_id="stack-42",
        color_shared=True,
        composite_id="comp-7",
        composite_bypassed=False,
        render_cache_id="cache-99",
        _source_fps_num=24000,
        _source_fps_den=1001,
        _timeline_fps_num=24,
        _timeline_fps_den=1,
        _source_pts_index=(0.0, 41.7, 83.4, 125.1),
    )


def _assert_non_effect_fields_preserved(original: ClipInstance, result: ClipInstance) -> None:
    """Every field except ``effects`` must survive unchanged."""
    # Core identity / timing
    assert result.clip_id == original.clip_id
    assert result.asset_id == original.asset_id
    assert result.track_id == original.track_id
    assert result.timeline_start == original.timeline_start
    assert result.timeline_duration == original.timeline_duration
    assert result.source_start == original.source_start
    assert result.source_duration == original.source_duration
    assert result.speed == original.speed
    assert result.linked_clip_id == original.linked_clip_id
    # Transform / crop / opacity
    assert result.transform == original.transform
    assert result.crop == original.crop
    assert result.opacity == original.opacity
    assert result.animated_params == original.animated_params
    assert result.is_enabled == original.is_enabled
    # FX-C01 specifically-targeted fields
    assert result.label_color == original.label_color, "label_color dropped"
    assert result.fade_in_duration == original.fade_in_duration, "fade_in dropped"
    assert result.fade_out_duration == original.fade_out_duration, "fade_out dropped"
    assert result.color_stack_id == original.color_stack_id, "color_stack_id dropped"
    assert result.color_shared == original.color_shared, "color_shared dropped"
    assert result.composite_id == original.composite_id, "composite_id dropped"
    assert result.composite_bypassed == original.composite_bypassed, "composite_bypassed dropped"
    assert result.render_cache_id == original.render_cache_id, "render_cache_id dropped"
    # FPS / PTS mapping
    assert result._source_fps_num == original._source_fps_num, "_source_fps_num dropped"
    assert result._source_fps_den == original._source_fps_den, "_source_fps_den dropped"
    assert result._timeline_fps_num == original._timeline_fps_num, "_timeline_fps_num dropped"
    assert result._timeline_fps_den == original._timeline_fps_den, "_timeline_fps_den dropped"
    assert result._source_pts_index == original._source_pts_index, "_source_pts_index dropped"


def test_with_effects_preserves_all_fields() -> None:
    """``with_effects`` is the lowest-level primitive; it must be lossless."""
    clip = _full_clip()
    new_effects = (EffectInstance(effect_id="e0", effect_type="blur", params={"radius": 2.0}),)
    result = clip.with_effects(new_effects)
    _assert_non_effect_fields_preserved(clip, result)
    assert result.effects == new_effects


def test_add_effect_preserves_all_fields() -> None:
    clip = _full_clip()
    result = clip.with_effect_added(
        EffectInstance(effect_type="blur", params={"radius": 2.0})
    )
    _assert_non_effect_fields_preserved(clip, result)
    assert len(result.effects) == len(clip.effects) + 1


def test_update_effect_preserves_all_fields() -> None:
    clip = _full_clip().with_effect_added(
        EffectInstance(effect_id="e-update", effect_type="blur", params={"radius": 1.0})
    )
    updated = EffectInstance(
        effect_id="e-update", effect_type="blur", params={"radius": 9.0}
    )
    result = clip.with_effect_updated("e-update", updated)
    _assert_non_effect_fields_preserved(clip, result)


def test_remove_effect_preserves_all_fields() -> None:
    clip = _full_clip().with_effect_added(
        EffectInstance(effect_id="e-rm", effect_type="blur", params={"radius": 1.0})
    )
    result = clip.with_effect_removed("e-rm")
    _assert_non_effect_fields_preserved(clip, result)
    assert len(result.effects) == len(clip.effects) - 1
