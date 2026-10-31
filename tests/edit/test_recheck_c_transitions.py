"""复查批次 C-1 回归：R04 转场最终态重锚事务。

对应 docs/edit_page_followup_audit_optimization_plan_2026-10-14.md §3.1
（K04），复现：带 seam 转场 Roll 后转场 1→0→undo 仍 0；宏整体 Move 最终
相接却转场 0。
"""

from __future__ import annotations

import pytest

from src.features.edit.core.commands import (
    MacroCommand,
    MoveClipCommand,
    RollTrimCommand,
)
from src.features.edit.core.timeline_ops import validate_timeline
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
    TransitionInstance,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int,
          source_start: int = 0) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"asset-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=source_start,
        source_duration=400,
    )


def _seam_timeline() -> TimelineSequence:
    """A[0,100) + B[100,200)，centered 双边 dissolve seam100 dur20。"""
    t = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    t = t.with_track_added(EditTrack(track_id="v1", name="V1", track_type="video", index=0))
    t = t.with_clip_added("v1", _clip("a", "v1", 0, 100))
    t = t.with_clip_added("v1", _clip("b", "v1", 100, 100, source_start=50))
    trans = TransitionInstance(
        transition_id="tr-seam",
        transition_type="cross_dissolve",
        track_id="v1",
        left_clip_id="a",
        right_clip_id="b",
        center_frame=100,
        duration=20,
        alignment="centered",
    )
    return t.with_track_updated(
        t.get_track_by_id("v1").with_transition_added(trans)
    )


def test_roll_keeps_seam_transition() -> None:
    """K04：Roll ±10 后 seam 转场保留并跟随新切点。"""
    t = _seam_timeline()
    cmd = RollTrimCommand.create("v1", "a", "b", -10)
    new_timeline, updated = cmd.apply(t)
    track = new_timeline.get_track_by_id("v1")
    assert len(track.transitions) == 1
    assert track.transitions[0].center_frame == 90
    assert validate_timeline(new_timeline) == []


def test_roll_undo_restores_transition_exactly() -> None:
    """K04：Roll 后 undo——转场与几何完整回滚（旧实现 undo 后仍 0）。"""
    t = _seam_timeline()
    cmd = RollTrimCommand.create("v1", "a", "b", -10)
    new_timeline, updated = cmd.apply(t)
    restored = updated.undo(new_timeline)
    assert restored.to_dict() == t.to_dict()
    assert len(restored.get_track_by_id("v1").transitions) == 1


def test_roll_three_cycles_stable() -> None:
    t = _seam_timeline()
    cmd = RollTrimCommand.create("v1", "a", "b", 10)
    timeline = t
    for _ in range(3):
        timeline, updated = cmd.apply(timeline)
        timeline = updated.undo(timeline)
    assert timeline.to_dict() == t.to_dict()


def test_macro_group_move_keeps_adjacent_transition() -> None:
    """K04：宏整体 Move A→200、B→300，最终仍相接——转场不丢。"""
    t = _seam_timeline()
    macro = MacroCommand.create(
        "Move group", [
            MoveClipCommand.create("v1", "a", 200),
            MoveClipCommand.create("v1", "b", 300),
        ]
    )
    new_timeline, _ = macro.apply(t)
    track = new_timeline.get_track_by_id("v1")
    a = track.get_clip_by_id("a")
    b = track.get_clip_by_id("b")
    assert (a.timeline_start, a.timeline_end) == (200, 300)
    assert b.timeline_start == 300
    assert len(track.transitions) == 1
    assert track.transitions[0].center_frame == 300
    assert validate_timeline(new_timeline) == []


def test_macro_pure_separation_still_deletes_transition() -> None:
    """分离语义保持：最终真分开的转场仍按策略删除。"""
    t = _seam_timeline()
    macro = MacroCommand.create(
        "Separate group", [
            MoveClipCommand.create("v1", "a", 0),
            MoveClipCommand.create("v1", "b", 500),
        ]
    )
    new_timeline, _ = macro.apply(t)
    track = new_timeline.get_track_by_id("v1")
    assert len(track.transitions) == 0


# ------------------------------------------------------------------ R09

def _json_roundtrip(cmd):
    import json
    return type(cmd).from_dict(json.loads(json.dumps(cmd.to_dict())))


def test_move_transition_snapshot_json_roundtrip() -> None:
    """K09：Move 分离 seam 后 loaded-undo 恢复转场。"""
    t = _seam_timeline()
    cmd = MoveClipCommand.create("v1", "a", 500)
    new_timeline = cmd.apply(t)  # Move 自带回写快照，非 tuple 返回
    assert len(new_timeline.get_track_by_id("v1").transitions) == 0

    loaded = _json_roundtrip(cmd)
    restored = loaded.undo(new_timeline)
    assert len(restored.get_track_by_id("v1").transitions) == 1
    assert restored.to_dict() == t.to_dict()


def test_split_marker_snapshot_json_roundtrip() -> None:
    """K09：Split 后 loaded-undo 恢复单片与 marker 归属。"""
    from src.features.edit.core.commands import SplitClipCommand
    from src.features.edit.core.timeline_v2 import TimelineMarker
    from src.features.edit.core.timeline_ops import validate_timeline

    t = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    t = t.with_track_added(EditTrack(track_id="v1", name="V1", track_type="video", index=0))
    t = t.with_clip_added("v1", _clip("a", "v1", 0, 100))
    t = t._rebuild(
        markers=(TimelineMarker(marker_id="m", frame=75, clip_id="a"),)
    )

    cmd = SplitClipCommand.create("v1", "a", 50)
    new_timeline = cmd.apply(t)
    loaded = _json_roundtrip(cmd)
    restored = loaded.undo(new_timeline)
    assert restored.to_dict() == t.to_dict()
    assert validate_timeline(restored) == []


def test_batch_delete_transition_snapshot_json_roundtrip() -> None:
    """K09：BatchDelete 后 loaded-undo 恢复整轨转场。"""
    from src.features.edit.core.commands import BatchDeleteCommand

    t = _seam_timeline()
    cmd = BatchDeleteCommand(
        command_id="cmd", label="del", clip_ids=("a", "b"), ripple=False
    )
    new_timeline, updated = cmd.apply(t)
    assert len(new_timeline.get_track_by_id("v1").transitions) == 0

    loaded = _json_roundtrip(updated)
    restored = loaded.undo(new_timeline)
    assert len(restored.get_track_by_id("v1").transitions) == 1


def test_slide_snapshot_json_roundtrip() -> None:
    """K09：Slide 快照往返后 undo 几何与 live 一致。"""
    from src.features.edit.core.commands import SlideClipCommand

    t = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    t = t.with_track_added(EditTrack(track_id="v1", name="V1", track_type="video", index=0))
    t = t.with_clip_added("v1", _clip("left", "v1", 0, 100))
    t = t.with_clip_added("v1", _clip("mid", "v1", 100, 90))
    t = t.with_clip_added("v1", _clip("right", "v1", 190, 110))

    cmd = SlideClipCommand.create(t, "v1", "mid", 10)
    new_timeline, updated = cmd.apply(t)
    loaded = _json_roundtrip(updated)
    restored = loaded.undo(new_timeline)
    assert restored.to_dict() == t.to_dict()


# ------------------------------------------------------------------ R12

def test_noop_move_keeps_redo_and_history() -> None:
    """K12：原位 Move 不新增历史、不清 Redo。"""
    from src.features.edit.core.commands import CommandStack

    t = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    t = t.with_track_added(EditTrack(track_id="v1", name="V1", track_type="video", index=0))
    t = t.with_clip_added("v1", _clip("c", "v1", 0, 100))

    stack = CommandStack()
    stack.execute(MoveClipCommand.create("v1", "c", 50), t)
    assert len(stack._undo_stack) == 1
    # undo 一次
    undone = stack.undo(
        _timeline_after_move(t)
    )
    assert stack.can_redo

    timeline_now = undone
    assert timeline_now.get_clip_by_id("c").timeline_start == 0
    same = stack.execute(MoveClipCommand.create("v1", "c", 0), timeline_now)
    # 原位 Move（c 已在 0）：no-op——历史不增、redo 保留
    assert same is timeline_now
    assert len(stack._undo_stack) == 0
    assert stack.can_redo


def _timeline_after_move(t):
    from src.features.edit.core.commands import MoveClipCommand
    return MoveClipCommand.create("v1", "c", 50).apply(t)
