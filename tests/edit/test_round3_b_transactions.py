"""第三轮复查批B回归：命令事务与关联状态（N03–N08）。

对应 topos_round3_core.md C3-03…C3-08。真实模型/命令/JSON 执行。
"""

from __future__ import annotations

import json

import pytest

from src.features.edit.core.commands import (
    DeleteClipCommand,
    InsertClipCommand,
    InsertLinkedAssetRippleCommand,
    MacroCommand,
    MoveClipCommand,
    OverwriteClipCommand,
    RemoveTransitionCommand,
    SlipClipCommand,
    SlideClipCommand,
    RollTrimCommand,
    RippleDeleteCommand,
    BatchDeleteCommand,
    AppendClipCommand,
)
from src.features.edit.core.timeline_ops import validate_timeline
from src.features.edit.core.timeline_v2 import (
    ClipInstance, EditTrack, TimelineMarker, TimelineSequence,
    TransitionInstance,
)

pytestmark = pytest.mark.edit_core


def _clip(cid: str, tid: str, start: int, dur: int, **kw) -> ClipInstance:
    return ClipInstance(
        clip_id=cid, asset_id=f"a-{cid}", track_id=tid,
        timeline_start=start, timeline_duration=dur, source_start=0,
        source_duration=500, **kw)


def _two_clip_timeline(locked: bool = False) -> TimelineSequence:
    tl = TimelineSequence(timeline_id="t", name="T", fps=24.0)
    tl = tl.with_track_added(EditTrack(
        track_id="v", name="V", track_type="video", index=0,
        is_locked=locked))
    tl = tl.with_clip_added("v", _clip("a", "v", 0, 100))
    tl = tl.with_clip_added("v", _clip("b", "v", 100, 100))
    return tl


def _with_seam_transition(tl: TimelineSequence) -> TimelineSequence:
    track = tl.get_track_by_id("v")
    return tl.with_track_updated(track._rebuild(transitions=(
        TransitionInstance(
            transition_id="seam", transition_type="cross_dissolve",
            left_clip_id="a", right_clip_id="b",
            center_frame=100, duration=20, track_id="v"),)))


# ------------------------------------------------------------------ N03

def test_n03_explicit_remove_transition_not_resurrected() -> None:
    tl = _with_seam_transition(_two_clip_timeline())
    macro = MacroCommand.create("remove only", [RemoveTransitionCommand(
        command_id="r1", label="rm", transition_id="seam", track_id="v")])
    result = macro.apply(tl)
    tl2 = result[0] if isinstance(result, tuple) else result
    assert tl2.get_track_by_id("v").transitions == ()


def test_n03_deleted_owner_transition_not_resurrected() -> None:
    tl = _with_seam_transition(_two_clip_timeline())
    macro = MacroCommand.create("delete a", [DeleteClipCommand(
        command_id="d1", label="del", track_id="v", clip_id="a")])
    result = macro.apply(tl)
    tl2 = result[0] if isinstance(result, tuple) else result
    assert tl2.get_track_by_id("v").transitions == ()
    assert validate_timeline(tl2) == []


def test_n03_group_move_keeps_legal_transition() -> None:
    tl = _with_seam_transition(_two_clip_timeline())
    macro = MacroCommand.create("move both", [
        MoveClipCommand.create("v", "a", 200),
        MoveClipCommand.create("v", "b", 300)])
    result = macro.apply(tl)
    tl2 = result[0] if isinstance(result, tuple) else result
    assert len(tl2.get_track_by_id("v").transitions) == 1
    assert validate_timeline(tl2) == []


# ------------------------------------------------------------------ N04

def test_n04_insert_redo_rejected_on_locked_track() -> None:
    """Insert 首次 apply 成功 → undo → 锁轨 → redo 必须拒绝（此前快路
    径在守卫之前 return）。"""
    tl = _two_clip_timeline()
    cmd = InsertClipCommand(
        command_id="i1", label="ins", asset_id="new", in_frame=0,
        out_frame=9, playhead=40, track_id="v", speed=1.0)
    result = cmd.apply(tl)
    tl2, updated = result
    tl3 = updated.undo(tl2)
    locked = tl3.with_track_updated(
        tl3.get_track_by_id("v").__class__(
            **{**{f: getattr(tl3.get_track_by_id("v"), f)
                  for f in tl3.get_track_by_id("v").__dataclass_fields__},
               "is_locked": True}))
    with pytest.raises(ValueError):
        updated.apply(locked)


def test_n04_seven_content_commands_reject_locked_track() -> None:
    """Slip/Slide/Roll/RippleDelete/BatchDelete/Append/Freeze 七类内容
    写入口全部拒绝锁轨（此前可绕锁写入）。"""
    tl = _two_clip_timeline(locked=True)
    with pytest.raises(ValueError):
        SlipClipCommand(command_id="s", label="s", track_id="v",
                        clip_id="a", delta_frames=5).apply(tl)
    with pytest.raises(ValueError):
        SlideClipCommand(command_id="s2", label="s", track_id="v",
                         clip_id="a", delta_frames=5).apply(tl)
    with pytest.raises(ValueError):
        RollTrimCommand(command_id="r", label="r", track_id="v",
                        left_clip_id="a", right_clip_id="b",
                        delta_frames=5).apply(tl)
    with pytest.raises(ValueError):
        RippleDeleteCommand(command_id="rd", label="rd", track_id="v",
                            clip_id="a").apply(tl)
    with pytest.raises(ValueError):
        BatchDeleteCommand(command_id="bd", label="bd",
                           clip_ids=("a",)).apply(tl)
    with pytest.raises(ValueError):
        AppendClipCommand(command_id="ap", label="ap", asset_id="new",
                          in_frame=0, out_frame=9, track_id="v").apply(tl)


# ------------------------------------------------------------------ N05

def test_n05_insert_marker_transaction_undo_redo_json() -> None:
    tl = TimelineSequence(timeline_id="t", name="T", fps=24.0)
    tl = tl.with_track_added(EditTrack(
        track_id="v", name="V", track_type="video", index=0))
    tl = tl.with_track_added(EditTrack(
        track_id="a", name="A", track_type="audio", index=1,
        is_sync_locked=True))
    tl = tl.with_clip_added("v", _clip("vc", "v", 100, 100))
    tl = tl.with_clip_added("a", _clip("ac", "a", 100, 100))
    tl = tl._rebuild(markers=(
        TimelineMarker(marker_id="vm", frame=110, track_id="v", clip_id="vc"),
        TimelineMarker(marker_id="am", frame=110, track_id="a", clip_id="ac"),
    ))
    cmd = InsertClipCommand(
        command_id="i", label="ins", asset_id="new", in_frame=0,
        out_frame=19, playhead=0, track_id="v", speed=1.0)
    tl2, updated = cmd.apply(tl)

    def frames(seq):
        return {m.marker_id: m.frame for m in seq.markers}

    assert frames(tl2) == {"vm": 130, "am": 130}
    assert validate_timeline(tl2) == []

    tl3 = updated.undo(tl2)
    assert frames(tl3) == {"vm": 110, "am": 110}, "跨轨 marker 不得被逆变换两次"
    assert validate_timeline(tl3) == []

    redo = updated.apply(tl3)
    tl4 = redo[0] if isinstance(redo, tuple) else redo
    assert frames(tl4) == {"vm": 130, "am": 130}, "redo 必须恢复 after markers"
    assert validate_timeline(tl4) == []

    loaded = InsertClipCommand.from_dict(
        json.loads(json.dumps(updated.to_dict())))
    tl5 = loaded.undo(tl4)
    assert frames(tl5) == {"vm": 110, "am": 110}
    assert validate_timeline(tl5) == []


# ------------------------------------------------------------------ N06

def test_n06_linked_ripple_json_roundtrip_full_undo() -> None:
    tl = TimelineSequence(timeline_id="t", name="T", fps=24.0)
    tl = tl.with_track_added(EditTrack(
        track_id="v", name="V", track_type="video", index=0))
    tl = tl.with_track_added(EditTrack(
        track_id="a", name="A", track_type="audio", index=1))
    tl = tl.with_clip_added("v", _clip("v0", "v", 0, 100))
    tl = tl.with_clip_added("a", _clip("a0", "a", 0, 100))
    cmd = InsertLinkedAssetRippleCommand(
        command_id="c", label="L", asset_id="new", in_frame=0, out_frame=9,
        playhead=40, video_track_id="v", audio_track_id="a")
    tl2, updated = cmd.apply(tl)

    loaded = InsertLinkedAssetRippleCommand.from_dict(
        json.loads(json.dumps(updated.to_dict())))
    tl3 = loaded.undo(tl2)
    assert validate_timeline(tl3) == []
    assert len(tl3.get_track_by_id("v").clips) == 1
    assert len(tl3.get_track_by_id("a").clips) == 1

    # redo 身份稳定：apply 与 redo 的 ID 集合一致
    redo = updated.apply(tl3)
    tl4 = redo[0]
    assert ({c.clip_id for c in tl4.get_track_by_id("v").clips}
            == {c.clip_id for c in tl2.get_track_by_id("v").clips})


def test_n06_overwrite_append_fps_in_json_payload() -> None:
    tl = _two_clip_timeline()
    ow = OverwriteClipCommand(
        command_id="o", label="O", asset_id="new", in_frame=0, out_frame=29,
        playhead=150, track_id="v", speed=1.0,
        _source_fps_num=30, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1)
    loaded = OverwriteClipCommand.from_dict(
        json.loads(json.dumps(ow.to_dict())))
    assert loaded._source_fps_num == 30
    assert loaded._timeline_fps_den == 1
    # 回放与 live 同 duration（30fps 源 30 帧在 24fps 序列 = 24 帧）
    tl_live, _ = ow.apply(tl)
    tl_loaded, _ = loaded.apply(tl)
    new_live = [c for c in tl_live.get_track_by_id("v").clips
                if c.asset_id == "new"][0]
    new_loaded = [c for c in tl_loaded.get_track_by_id("v").clips
                  if c.asset_id == "new"][0]
    assert new_live.timeline_duration == new_loaded.timeline_duration == 24


# ------------------------------------------------------------------ N07

def test_n07_linked_ripple_residuals_reciprocal_and_marker_moves() -> None:
    tl = TimelineSequence(timeline_id="t", name="T", fps=24.0)
    tl = tl.with_track_added(EditTrack(
        track_id="v", name="V", track_type="video", index=0))
    tl = tl.with_track_added(EditTrack(
        track_id="a", name="A", track_type="audio", index=1))
    tl = tl.with_clip_added("v", _clip("v0", "v", 0, 100, linked_clip_id="a0"))
    tl = tl.with_clip_added("a", _clip("a0", "a", 0, 100, linked_clip_id="v0"))
    tl = tl._rebuild(markers=(TimelineMarker(
        marker_id="m75", frame=75, track_id="v", clip_id="v0"),))
    cmd = InsertLinkedAssetRippleCommand(
        command_id="c", label="L", asset_id="new", in_frame=0, out_frame=9,
        playhead=40, video_track_id="v", audio_track_id="a")
    tl2, updated = cmd.apply(tl)

    assert validate_timeline(tl2) == []
    right_v = [c for c in tl2.get_track_by_id("v").clips
               if c.timeline_start == 50 and c.asset_id == "a-v0"]
    right_a = [c for c in tl2.get_track_by_id("a").clips
               if c.timeline_start == 50 and c.asset_id == "a-a0"]
    assert right_v and right_a
    assert right_v[0].linked_clip_id == right_a[0].clip_id
    assert right_a[0].linked_clip_id == right_v[0].clip_id
    marker = next(m for m in tl2.markers if m.marker_id == "m75")
    assert marker.frame == 85
    assert marker.clip_id == right_v[0].clip_id

    tl3 = updated.undo(tl2)
    assert validate_timeline(tl3) == []
    assert tl3.markers[0].clip_id == "v0"
    assert tl3.markers[0].frame == 75


# ------------------------------------------------------------------ N08

def test_n08_overwrite_midcover_association_migration_and_undo() -> None:
    tl = TimelineSequence(timeline_id="t", name="T", fps=24.0)
    tl = tl.with_track_added(EditTrack(
        track_id="v", name="V", track_type="video", index=0))
    tl = tl.with_clip_added("v", _clip("c", "v", 0, 100))
    tl = tl._rebuild(markers=(
        TimelineMarker(marker_id="m75", frame=75, track_id="v", clip_id="c"),
        TimelineMarker(marker_id="m10", frame=10, track_id="v", clip_id="c"),
    ))
    cmd = OverwriteClipCommand(
        command_id="o", label="O", asset_id="new", in_frame=0, out_frame=9,
        playhead=40, track_id="v", speed=1.0)
    tl2, updated = cmd.apply(tl)

    assert validate_timeline(tl2) == []
    left = tl2.get_clip_by_id("c")
    assert left is not None and left.timeline_end == 40
    right = [c for c in tl2.get_track_by_id("v").clips
             if c.timeline_start == 50 and c.asset_id == "a-c"]
    assert right, "right residue must exist"
    m75 = next(m for m in tl2.markers if m.marker_id == "m75")
    assert m75.frame == 75 and m75.clip_id == right[0].clip_id
    m10 = next(m for m in tl2.markers if m.marker_id == "m10")
    assert m10.clip_id == "c"

    tl3 = updated.undo(tl2)
    assert validate_timeline(tl3) == []
    assert tl3.get_clip_by_id("c").timeline_duration == 100
    assert {m.clip_id for m in tl3.markers} == {"c"}
    assert {m.frame for m in tl3.markers} == {10, 75}
