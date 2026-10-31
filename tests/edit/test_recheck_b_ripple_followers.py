"""复查批次 B-1 回归：R03 跨轨波纹 delta 统一 + R08 目标轨动画/marker
共同位移。

对应 docs/edit_page_followup_audit_optimization_plan_2026-10-14.md §3.1
（K03/K08），复现见 docs/audit/2026-10-14-edit-recheck/reports/
topos_recheck_core.md。
"""

from __future__ import annotations

import pytest

from src.features.edit.core.commands import (
    BatchDeleteCommand,
    InsertClipCommand,
    RippleTrimCommand,
)
from src.features.edit.core.timeline_ops import (
    delete_clip,
    insert_clip_at_playhead,
    move_clip,
    ripple_trim_clip_right,
    validate_timeline,
)
from src.features.edit.core.timeline_v2 import (
    AnimatableParam,
    ClipInstance,
    EditTrack,
    Keyframe,
    KeyframeTrack,
    TimelineMarker,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int, **kw) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=kw.pop("asset_id", f"asset-{clip_id}"),
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=kw.pop("source_start", 0),
        source_duration=kw.pop("source_duration", 400),
        **kw,
    )


def _opacity_track(start: int = 0, duration: int = 100) -> AnimatableParam:
    return AnimatableParam(
        base_value=1.0,
        keyframes=KeyframeTrack(
            param_name="opacity",
            keyframes=(
                Keyframe(frame=start, value=0.0),
                Keyframe(frame=start + duration - 1, value=1.0),
            ),
        ),
    )


def _timeline() -> TimelineSequence:
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(
        EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    )
    timeline = timeline.with_track_added(
        EditTrack(
            track_id="a1", name="A1", track_type="audio", index=0,
            is_sync_locked=True,
        )
    )
    return timeline


# ------------------------------------------------------------------ R03

def test_insert_mixed_fps_cross_track_delta_matches_duration() -> None:
    """K03：30fps 源 30 帧插 24fps 序列——目标轨与同步轨 follower 同移
    24（旧 ripple_offset=30 使音轨 130 错位）。"""
    timeline = _timeline()
    timeline = timeline.with_clip_added("v1", _clip("vf", "v1", 100, 50))
    timeline = timeline.with_clip_added("a1", _clip("af", "a1", 100, 50))

    cmd = InsertClipCommand.create(
        asset_id="new",
        in_frame=0,
        out_frame=29,  # 30 帧素材
        playhead=0,
        track_id="v1",
        speed=1.0,
        source_fps_num=30,
        source_fps_den=1,
        timeline_fps_num=24,
        timeline_fps_den=1,
    )
    new_timeline, _ = cmd.apply(timeline)
    assert new_timeline.get_clip_by_id("vf").timeline_start == 124
    assert new_timeline.get_clip_by_id("af").timeline_start == 124


# ------------------------------------------------------------------ R08

def _marker(clip_id: str, frame: int) -> TimelineMarker:
    return TimelineMarker(marker_id=f"m-{clip_id}", frame=frame, clip_id=clip_id)


def test_insert_shifts_target_track_keys_and_markers() -> None:
    """K08 复现1：目标轨 follower [100,200) keys/marker——插 20 后
    start/keys/marker 同移 +20，局部曲线不变，validator 空。"""
    timeline = _timeline()
    timeline = timeline.with_clip_added(
        "v1",
        _clip(
            "follower", "v1", 100, 100,
            animated_params={"opacity": _opacity_track(100, 100)},
        ),
    )
    timeline = timeline._rebuild(markers=(_marker("follower", 110),))

    new_timeline = insert_clip_at_playhead(
        timeline, "new", 0, 19, 0, "v1", 1.0,
    )
    moved = new_timeline.get_clip_by_id("follower")
    assert moved.timeline_start == 120
    frames = [
        k.frame for k in moved.animated_params["opacity"].keyframes.keyframes
    ]
    assert frames == [120, 219]
    assert new_timeline.markers[0].frame == 130
    # 局部曲线：新起点 opacity 仍 0
    param = moved.animated_params["opacity"]
    assert param.get_value_at_frame(120) == pytest.approx(0.0)
    assert validate_timeline(new_timeline) == []


def test_insert_undo_restores_target_track_markers() -> None:
    timeline = _timeline()
    timeline = timeline.with_clip_added("v1", _clip("follower", "v1", 100, 100))
    timeline = timeline._rebuild(markers=(_marker("follower", 110),))

    cmd = InsertClipCommand.create(
        asset_id="new", in_frame=0, out_frame=19, playhead=0, track_id="v1",
    )
    new_timeline, updated = cmd.apply(timeline)
    assert new_timeline.markers[0].frame == 130

    restored = updated.undo(new_timeline)
    assert restored.markers[0].frame == 110


def test_ripple_delete_shifts_target_track_keys() -> None:
    """K08 复现2：删目标轨 [0,20) 波纹——follower 100→80，keys 100/199
    →80/179，marker 110→90，局部 10 帧 opacity 保持原值。"""
    timeline = _timeline()
    timeline = timeline.with_clip_added("v1", _clip("gone", "v1", 0, 20))
    timeline = timeline.with_clip_added(
        "v1",
        _clip(
            "follower", "v1", 100, 100,
            animated_params={"opacity": _opacity_track(100, 100)},
        ),
    )
    timeline = timeline._rebuild(markers=(_marker("follower", 110),))

    new_timeline = delete_clip(timeline, "gone", ripple=True)
    moved = new_timeline.get_clip_by_id("follower")
    assert moved.timeline_start == 80
    frames = [
        k.frame for k in moved.animated_params["opacity"].keyframes.keyframes
    ]
    assert frames == [80, 179]
    assert new_timeline.markers[0].frame == 90
    param = moved.animated_params["opacity"]
    # clip-local 10 帧处（原 110）opacity 不变
    before_param = timeline.get_clip_by_id("follower").animated_params["opacity"]
    assert param.get_value_at_frame(90) == pytest.approx(
        before_param.get_value_at_frame(110)
    )


def test_batch_ripple_delete_shifts_target_keys_via_helper() -> None:
    """BatchDelete 走 merged helper 的目标轨路径同样覆盖 keys。"""
    timeline = _timeline()
    timeline = timeline.with_clip_added("v1", _clip("d1", "v1", 0, 10))
    timeline = timeline.with_clip_added("v1", _clip("d2", "v1", 20, 10))
    timeline = timeline.with_clip_added(
        "v1",
        _clip(
            "follower", "v1", 100, 100,
            animated_params={"opacity": _opacity_track(100, 100)},
        ),
    )

    cmd = BatchDeleteCommand(
        command_id="cmd", label="del", clip_ids=("d1", "d2"), ripple=True
    )
    new_timeline, _ = cmd.apply(timeline)
    moved = new_timeline.get_clip_by_id("follower")
    assert moved.timeline_start == 80
    frames = [
        k.frame for k in moved.animated_params["opacity"].keyframes.keyframes
    ]
    assert frames == [80, 179]


def test_move_clip_ripple_shifts_keys_and_markers() -> None:
    """R08：move_clip(ripple=True) 跟随片段 keys/marker 共同位移。"""
    timeline = _timeline()
    timeline = timeline.with_clip_added("v1", _clip("m", "v1", 0, 10))
    timeline = timeline.with_clip_added(
        "v1",
        _clip(
            "follower", "v1", 100, 100,
            animated_params={"opacity": _opacity_track(100, 100)},
        ),
    )
    timeline = timeline._rebuild(markers=(_marker("follower", 110),))

    new_timeline = move_clip(timeline, "m", "v1", 20, ripple=True)
    moved = new_timeline.get_clip_by_id("follower")
    assert moved.timeline_start == 120
    frames = [
        k.frame for k in moved.animated_params["opacity"].keyframes.keyframes
    ]
    assert frames == [120, 219]
    assert new_timeline.markers[0].frame == 130


def test_ripple_trim_right_shifts_followers_keys_and_markers() -> None:
    """R08：波纹右修缩短 20——后续片段左移 20，keys/marker 同移。"""
    timeline = _timeline()
    timeline = timeline.with_clip_added("v1", _clip("c1", "v1", 0, 100))
    timeline = timeline.with_clip_added(
        "v1",
        _clip(
            "follower", "v1", 100, 100,
            animated_params={"opacity": _opacity_track(100, 100)},
        ),
    )
    timeline = timeline._rebuild(markers=(_marker("follower", 110),))

    new_timeline = ripple_trim_clip_right(timeline, "c1", 80)
    moved = new_timeline.get_clip_by_id("follower")
    assert moved.timeline_start == 80
    frames = [
        k.frame for k in moved.animated_params["opacity"].keyframes.keyframes
    ]
    assert frames == [80, 179]
    assert new_timeline.markers[0].frame == 90
    assert validate_timeline(new_timeline) == []


def test_ripple_trim_command_undo_restores_followers() -> None:
    """R08：RippleTrimCommand undo 后 follower keys/marker 回原位。"""
    timeline = _timeline()
    timeline = timeline.with_clip_added("v1", _clip("c1", "v1", 0, 100))
    timeline = timeline.with_clip_added(
        "v1",
        _clip(
            "follower", "v1", 100, 100,
            animated_params={"opacity": _opacity_track(100, 100)},
        ),
    )
    timeline = timeline._rebuild(markers=(_marker("follower", 110),))

    cmd = RippleTrimCommand(
        command_id="cmd", label="rt", track_id="v1", clip_id="c1",
        trim_side="right", new_timeline_end=80,
    )
    new_timeline, updated = cmd.apply(timeline)
    assert new_timeline.get_clip_by_id("follower").timeline_start == 80

    restored = updated.undo(new_timeline)
    moved = restored.get_clip_by_id("follower")
    assert moved.timeline_start == 100
    frames = [
        k.frame for k in moved.animated_params["opacity"].keyframes.keyframes
    ]
    assert frames == [100, 199]
    assert restored.markers[0].frame == 110
