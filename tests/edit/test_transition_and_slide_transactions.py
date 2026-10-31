"""批 B2 回归：C11 转场快照 / C12 转场跟随移动 / C14 Slide 完整恢复。

对应 docs/edit_page_full_audit_optimization_plan_2026-10-14.md §9。
"""

from __future__ import annotations

import pytest

from src.features.edit.core.commands import (
    DeleteClipCommand,
    MoveClipCommand,
    SlideClipCommand,
    SplitClipCommand,
)
from src.features.edit.core.timeline_ops import validate_timeline
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
    TransitionInstance,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int, **kw) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"asset-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=kw.pop("source_start", 0),
        source_duration=kw.pop("source_duration", 200),
        **kw,
    )


def _timeline_with_head_transition() -> TimelineSequence:
    """审计场景：A 头部 10 帧单边淡入转场。"""
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(track)
    timeline = timeline.with_clip_added("v1", _clip("a", "v1", 0, 100))
    head_fade = TransitionInstance(
        transition_id="tr-head",
        transition_type="dip_to_color",
        track_id="v1",
        left_clip_id="",
        right_clip_id="a",
        center_frame=0,
        duration=10,
        alignment="start_on_cut",
    )
    return timeline.with_track_updated(
        timeline.get_track_by_id("v1").with_transition_added(head_fade)
    )


# ---------------------------------------------------------------- C11

def test_blade_keeps_and_migrates_head_transition() -> None:
    timeline = _timeline_with_head_transition()
    assert len(timeline.get_track_by_id("v1").transitions) == 1

    cmd = SplitClipCommand.create("v1", "a", 50)
    new_timeline = cmd.apply(timeline)

    track = new_timeline.get_track_by_id("v1")
    # 转场保留并迁移到左段（头转场属于前半）
    assert len(track.transitions) == 1
    trans = track.transitions[0]
    assert trans.right_clip_id == cmd._left_clip_id
    assert validate_timeline(new_timeline) == []


def test_blade_undo_restores_transition_exactly() -> None:
    """审计实测：blade 后转场 1→0→undo 仍 0。"""
    timeline = _timeline_with_head_transition()
    before = timeline.to_dict()

    cmd = SplitClipCommand.create("v1", "a", 50)
    new_timeline = cmd.apply(timeline)
    restored = cmd.undo(new_timeline)

    assert restored.to_dict() == before
    assert len(restored.get_track_by_id("v1").transitions) == 1


def test_delete_undo_restores_transition() -> None:
    timeline = _timeline_with_head_transition()
    before = timeline.to_dict()

    cmd = DeleteClipCommand.create("v1", "a")
    new_timeline, updated = cmd.apply(timeline)
    assert len(new_timeline.get_track_by_id("v1").transitions) == 0

    restored = updated.undo(new_timeline)
    assert restored.to_dict() == before
    assert len(restored.get_track_by_id("v1").transitions) == 1


# ---------------------------------------------------------------- C12

def test_move_clip_transitions_follow() -> None:
    """审计实测：A 带 head 淡入 center=0，A 移到 200 后 trans.start 仍 0。"""
    timeline = _timeline_with_head_transition()

    cmd = MoveClipCommand.create("v1", "a", 200)
    result = cmd.apply(timeline)
    new_timeline = result[0] if isinstance(result, tuple) else result

    trans = new_timeline.get_track_by_id("v1").transitions[0]
    assert trans.center_frame == 200
    assert validate_timeline(new_timeline) == []


def test_move_group_keeps_two_sided_transition() -> None:
    """整组移动保持双边转场相接，validate 零错误。"""
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(track)
    timeline = timeline.with_clip_added("v1", _clip("a", "v1", 0, 100))
    timeline = timeline.with_clip_added("v1", _clip("b", "v1", 100, 100))
    dissolve = TransitionInstance(
        transition_id="tr-seam",
        transition_type="cross_dissolve",
        track_id="v1",
        left_clip_id="a",
        right_clip_id="b",
        center_frame=100,
        duration=20,
        alignment="centered",
    )
    timeline = timeline.with_track_updated(
        timeline.get_track_by_id("v1").with_transition_added(dissolve)
    )

    # 分别移动两片（组移动的等价建模）：a→200 会先分离——按策略删除；
    # 随后 b→200 重新相接时转场已被删除。快照 undo 可完整恢复。
    cmd_a = MoveClipCommand.create("v1", "a", 200)
    timeline_after_a = cmd_a.apply(timeline)
    assert len(timeline_after_a.get_track_by_id("v1").transitions) == 0

    restored = cmd_a.undo(timeline_after_a)
    assert len(restored.get_track_by_id("v1").transitions) == 1
    assert validate_timeline(restored) == []


def test_trim_shortens_transition_duration_to_fit() -> None:
    """片段右裁到比转场更短时 duration 收紧，validate 零错误。"""
    timeline = _timeline_with_head_transition()
    clip = timeline.get_clip_by_id("a")
    trimmed = clip.with_trim_right(6, 6)
    new_timeline = timeline.with_track_updated(
        timeline.get_track_by_id("v1").with_clip_updated(trimmed)
    )
    trans = new_timeline.get_track_by_id("v1").transitions[0]
    assert trans.duration <= 6
    assert validate_timeline(new_timeline) == []


# ---------------------------------------------------------------- C14

def _timeline_for_slide(fade_out: int = 0) -> TimelineSequence:
    """A=(0,90) fade_out, B=(100,90), C=(200,100)，B 前后各 10 帧 gap。"""
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(track)
    timeline = timeline.with_clip_added(
        "v1", _clip("a", "v1", 0, 90, fade_out_duration=fade_out,
                    source_duration=200)
    )
    timeline = timeline.with_clip_added("v1", _clip("b", "v1", 100, 90))
    timeline = timeline.with_clip_added(
        "v1", _clip("c", "v1", 200, 100, source_start=50)
    )
    return timeline


def test_slide_undo_restores_gaps() -> None:
    """审计实测1：slide B+5 后 undo，两个 10 帧 gap 丢失。"""
    timeline = _timeline_for_slide()
    before = timeline.to_dict()

    cmd = SlideClipCommand(
        command_id="cmd", label="slide",
        track_id="v1", clip_id="b", delta_frames=5,
    )
    new_timeline, updated = cmd.apply(timeline)
    # apply 后 B 确实移动
    assert new_timeline.get_clip_by_id("b").timeline_start == 105

    restored = updated.undo(new_timeline)
    assert restored.to_dict() == before
    assert restored.get_clip_by_id("a").timeline_end == 90
    assert restored.get_clip_by_id("b").timeline_start == 100
    assert restored.get_clip_by_id("c").timeline_start == 200


def test_slide_undo_restores_clamped_fade() -> None:
    """审计实测2：紧邻 A100 帧 fade_out=80，slide B-50 后 A 淡出 clamp 50；
    旧 undo 淡出仍 50，应恢复 80。"""
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(track)
    timeline = timeline.with_clip_added(
        "v1", _clip("a", "v1", 0, 100, fade_out_duration=80, source_duration=200)
    )
    timeline = timeline.with_clip_added("v1", _clip("b", "v1", 100, 100))

    cmd = SlideClipCommand(
        command_id="cmd", label="slide",
        track_id="v1", clip_id="b", delta_frames=-50,
    )
    new_timeline, updated = cmd.apply(timeline)
    assert new_timeline.get_clip_by_id("b").timeline_start == 50

    restored = updated.undo(new_timeline)
    a = restored.get_clip_by_id("a")
    assert a.timeline_duration == 100
    assert a.fade_out_duration == 80


def test_slide_redo_stable() -> None:
    timeline = _timeline_for_slide()
    cmd = SlideClipCommand(
        command_id="cmd", label="slide",
        track_id="v1", clip_id="b", delta_frames=5,
    )
    applied, updated = cmd.apply(timeline)
    undone = updated.undo(applied)
    redone, updated2 = updated.apply(undone)
    assert redone.get_clip_by_id("b").timeline_start == 105
    assert redone.to_dict() == applied.to_dict()
