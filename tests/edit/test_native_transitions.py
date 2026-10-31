"""Regression tests for the native Edit-page transition renderer."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QStyleOptionGraphicsItem

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_render_plan import (
    TimelineRenderPlan,
    calculate_transition_blend,
)
from src.features.edit.core.commands import AddTransitionCommand, UpdateTransitionCommand
from src.features.edit.core.timeline_ops import add_transition
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
    TransitionInstance,
)
from src.features.edit.core.transition_wipe import compute_wipe_mask
from src.features.edit.core.transition_catalog import SPATIAL_TRANSITION_TYPES
from src.features.edit.core.transition_mask import compute_transition_mask
from src.features.edit.ui.timeline_items import TransitionItem
from src.shared.media.metadata import Rational


def _sequence(transition_type: str = "cross_dissolve", params: dict | None = None) -> tuple[
    TimelineSequence, TransitionInstance
]:
    left = ClipInstance(
        clip_id="left",
        asset_id="asset-left",
        track_id="v1",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
    )
    right = ClipInstance(
        clip_id="right",
        asset_id="asset-right",
        track_id="v1",
        timeline_start=100,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
    )
    transition = TransitionInstance(
        transition_id="transition",
        transition_type=transition_type,
        track_id="v1",
        left_clip_id="left",
        right_clip_id="right",
        center_frame=100,
        duration=24,
        params=params or {},
    )
    track = EditTrack(
        track_id="v1",
        name="V1",
        track_type="video",
        index=0,
        clips=(left, right),
        transitions=(transition,),
    )
    return (
        TimelineSequence(
            timeline_id="timeline",
            name="Timeline",
            fps=Rational(24, 1),
            width=8,
            height=4,
            video_tracks=(track,),
        ),
        transition,
    )


def test_transition_progress_reaches_both_endpoints_and_supports_curve() -> None:
    _, transition = _sequence(params={"curve": "ease_in_out"})

    assert calculate_transition_blend(transition, transition.start_frame) == pytest.approx(0.0)
    assert calculate_transition_blend(transition, transition.end_frame - 1) > 0.95
    assert calculate_transition_blend(transition, 100) == pytest.approx(0.5)


@pytest.mark.parametrize(
    "transition_type",
    [
        "cross_dissolve", "dip_to_color", "blur_dissolve",
        "defocus_dissolve", "film_dissolve", "smooth_cut", "wipe",
    ],
)
def test_transition_plan_contains_both_sides_for_a_normal_cut(transition_type: str) -> None:
    sequence, transition = _sequence(transition_type)
    plan = TimelineRenderPlan(sequence)

    frame_plan = plan.get_frame_plan(transition.start_frame)
    assert {layer.clip_id for layer in frame_plan.layers} == {"left", "right"}
    assert frame_plan.transition_count == 1

    frame_plan = plan.get_frame_plan(transition.end_frame - 1)
    assert {layer.clip_id for layer in frame_plan.layers} == {"left", "right"}


def test_transition_plan_uses_keyframed_opacity_for_blend() -> None:
    sequence, transition = _sequence()
    track = sequence.video_tracks[0]
    left = track.get_clip_by_id("left")
    assert left is not None
    left = left.with_keyframe_added("opacity", 100, 0.25)
    sequence = sequence.with_track_updated(
        EditTrack(
            track_id=track.track_id,
            name=track.name,
            track_type=track.track_type,
            index=track.index,
            clips=(left, track.get_clip_by_id("right")),
            transitions=track.transitions,
        )
    )

    info = TimelineRenderPlan(sequence).get_frame_plan(100).transitions[0]
    assert info.left_opacity == pytest.approx(0.25 * (1.0 - 0.5))


def test_wipe_mask_has_full_endpoint_coverage() -> None:
    start = compute_wipe_mask(8, 4, 0.0, {"direction": "left", "softness": 0.2})
    end = compute_wipe_mask(8, 4, 1.0, {"direction": "left", "softness": 0.2})

    assert float(start.max()) == pytest.approx(0.0)
    assert float(end.min()) == pytest.approx(1.0)


@pytest.mark.parametrize("transition_type", sorted(SPATIAL_TRANSITION_TYPES))
def test_extended_transition_masks_have_full_endpoint_coverage(transition_type: str) -> None:
    start = compute_transition_mask(32, 18, 0.0, transition_type)
    middle = compute_transition_mask(32, 18, 0.5, transition_type)
    end = compute_transition_mask(32, 18, 1.0, transition_type)

    assert float(start.max()) == pytest.approx(0.0)
    assert float(end.min()) == pytest.approx(1.0)
    assert 0.0 <= float(middle.min()) <= float(middle.max()) <= 1.0


def test_transition_type_and_position_commands_are_undoable() -> None:
    sequence, transition = _sequence()

    type_command = UpdateTransitionCommand.create_type(
        sequence, transition.transition_id, transition.track_id, "wipe"
    )
    typed = type_command.apply(sequence)
    assert typed.video_tracks[0].transitions[0].transition_type == "wipe"
    restored = type_command.undo(typed)
    assert restored.video_tracks[0].transitions[0].transition_type == "cross_dissolve"

    position_command = UpdateTransitionCommand.create_center_frame(
        sequence, transition.transition_id, transition.track_id, 120
    )
    moved = position_command.apply(sequence)
    assert moved.video_tracks[0].transitions[0].center_frame == 120
    restored = position_command.undo(moved)
    assert restored.video_tracks[0].transitions[0].center_frame == 100


def test_readding_at_the_same_cut_replaces_without_changing_duration() -> None:
    sequence, _ = _sequence()

    replaced = add_transition(sequence, "left", "right", duration=8)

    assert len(replaced.video_tracks[0].transitions) == 1
    transition = replaced.video_tracks[0].transitions[0]
    assert transition.transition_type == "cross_dissolve"
    assert transition.duration == 24


def test_add_transition_command_replaces_and_undoes_existing_cut_transition() -> None:
    sequence, existing = _sequence()
    command = AddTransitionCommand.create(
        track_id="v1",
        left_clip_id="left",
        right_clip_id="right",
        duration=8,
        transition_type="wipe",
        params={"direction": "up"},
    )

    replaced = command.apply(sequence)
    transition = replaced.video_tracks[0].transitions[0]
    assert transition.transition_id == existing.transition_id
    assert transition.transition_type == "wipe"
    assert transition.duration == existing.duration
    assert transition.params == {"direction": "up"}

    restored = command.undo(replaced)
    assert restored.video_tracks[0].transitions[0] == existing


def test_transition_item_paint_supports_translucent_card(qapp) -> None:
    """The visible transition card must paint without Qt overload errors."""
    item = TransitionItem(
        "transition",
        "v1",
        100,
        24,
        5.0,
        0,
        "cross_dissolve",
        "centered",
    )
    item.set_track_height(64)
    assert item.boundingRect().height() == pytest.approx(64.0)
    image = QImage(180, 40, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    item.paint(painter, QStyleOptionGraphicsItem())
    painter.end()


# ── AUD-19211：角度坐标缓存量化键 ────────────────────────────────────────


def test_angle_coord_cache_reuses_neighbouring_angles():
    """拖动角度时相邻读数（<0.1°）复用同一全分辨率坐标数组。

    历史实现以连续 float 为 lru_cache 键——每个中间值都 miss 并重建
    数百万元素的投影数组。
    """
    from src.features.edit.core.transition_wipe import (
        _angle_coord_cached, _angle_coord_quantized)

    _angle_coord_quantized.cache_clear()
    a = _angle_coord_cached(1920, 1080, 37.02)   # → 量化键 370
    b = _angle_coord_cached(1920, 1080, 37.04)   # → 量化键 370（同桶复用）
    c = _angle_coord_cached(1920, 1080, 37.08)   # → 量化键 371（跨界重建）

    assert a is b
    assert c is not a
    # 相邻读数复用同一对象；量化键缓存只应有两个条目
    assert _angle_coord_cached(1920, 1080, 37.03) is a
    assert _angle_coord_quantized.cache_info().currsize == 2
