"""复查批次 A 回归（领域层）：R01 锁轨守卫 / R02 反向右修剪 / R05 fade 钳制。

对应 docs/edit_page_followup_audit_optimization_plan_2026-10-14.md §3.1
（K01/K02/K05）与 §3.2 RI-03 的领域层部分。复现输入取自
docs/audit/2026-10-14-edit-recheck/reports/topos_recheck_core.md。
"""

from __future__ import annotations

import pytest

from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.commands import (
    BatchDeleteCommand,
    ChangeClipSpeedCommand,
    DeleteClipCommand,
    InsertClipCommand,
    MoveClipCommand,
    OverwriteClipCommand,
    RippleTrimCommand,
    TrimClipCommand,
    UpdateClipParamCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
    TransitionInstance,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int = 0, duration: int = 100,
          **kw) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"asset-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=kw.pop("source_start", 0),
        source_duration=kw.pop("source_duration", duration),
        **kw,
    )


def _reverse_clip(clip_id: str, track_id: str, source_start: int,
                  source_duration: int, timeline_start: int = 0) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"asset-{clip_id}",
        track_id=track_id,
        timeline_start=timeline_start,
        timeline_duration=source_duration,
        source_start=source_start,
        source_duration=source_duration,
        speed=SpeedParams(speed=1.0, reverse=True),
    )


def _timeline(v_locked: bool = False, a_locked: bool = False) -> TimelineSequence:
    video = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        is_locked=v_locked,
    )
    audio = EditTrack(
        track_id="a1", name="A1", track_type="audio", index=0,
        is_locked=a_locked,
    )
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    return timeline.with_track_added(video).with_track_added(audio)


# ------------------------------------------------------------------ R01

def test_batch_ripple_delete_keeps_locked_sync_follower() -> None:
    """K01：累计波纹删除不得移动锁定音轨的 follower（100→80 回归）。"""
    timeline = _timeline()
    timeline = timeline.with_clip_added("v1", _clip("d1", "v1", 0, 10))
    timeline = timeline.with_clip_added("v1", _clip("d2", "v1", 20, 10))
    locked_audio = EditTrack(
        track_id="a1", name="A1", track_type="audio", index=0,
        is_locked=True, is_sync_locked=True,
    )
    timeline = timeline.with_track_updated(locked_audio)
    timeline = timeline.with_clip_added("a1", _clip("af", "a1", 100, 100))

    cmd = BatchDeleteCommand(
        command_id="cmd", label="del", clip_ids=("d1", "d2"), ripple=True
    )
    new_timeline, _updated = cmd.apply(timeline)
    assert new_timeline.get_clip_by_id("af").timeline_start == 100


def _locked_single_clip_timeline() -> TimelineSequence:
    timeline = _timeline(v_locked=True)
    return timeline.with_clip_added(
        "v1", _clip("c", "v1", 0, 100, source_duration=200)
    )


def test_move_rejected_on_locked_track() -> None:
    timeline = _locked_single_clip_timeline()
    cmd = MoveClipCommand(
        command_id="cmd", label="m", track_id="v1", clip_id="c",
        new_timeline_start=50,
    )
    with pytest.raises(ValueError, match="locked"):
        cmd.apply(timeline)


def test_trim_rejected_on_locked_track() -> None:
    timeline = _locked_single_clip_timeline()
    cmd = TrimClipCommand.create_right_trim("v1", "c", 50, 50)
    with pytest.raises(ValueError, match="locked"):
        cmd.apply(timeline)


def test_delete_rejected_on_locked_track() -> None:
    timeline = _locked_single_clip_timeline()
    cmd = DeleteClipCommand(
        command_id="cmd", label="d", track_id="v1", clip_id="c"
    )
    with pytest.raises(ValueError, match="locked"):
        cmd.apply(timeline)


def test_insert_rejected_on_locked_track() -> None:
    timeline = _locked_single_clip_timeline()
    cmd = InsertClipCommand(
        command_id="cmd", label="i", asset_id="new-asset",
        in_frame=0, out_frame=9, playhead=0, track_id="v1",
    )
    with pytest.raises(ValueError, match="locked"):
        cmd.apply(timeline)


def test_overwrite_rejected_on_locked_track() -> None:
    timeline = _locked_single_clip_timeline()
    cmd = OverwriteClipCommand(
        command_id="cmd", label="o", asset_id="new-asset",
        in_frame=0, out_frame=9, playhead=0, track_id="v1",
    )
    with pytest.raises(ValueError, match="locked"):
        cmd.apply(timeline)


def test_ripple_trim_rejected_on_locked_track() -> None:
    timeline = _locked_single_clip_timeline()
    cmd = RippleTrimCommand(
        command_id="cmd", label="rt", track_id="v1", clip_id="c",
        trim_side="right", new_timeline_end=50,
    )
    with pytest.raises(ValueError, match="locked"):
        cmd.apply(timeline)


def test_update_clip_param_rejected_on_locked_track() -> None:
    """RI-03：Properties Transform 直写锁轨（X 55→77 审计实测）。"""
    from src.features.edit.core.clip_params import TransformParams

    timeline = _locked_single_clip_timeline()
    clip = timeline.get_clip_by_id("c")
    cmd = UpdateClipParamCommand(
        command_id="cmd", label="p", track_id="v1", clip_id="c",
        param_type="transform",
        new_value=TransformParams(translate_x=99.0),
        old_value=clip.transform,
    )
    with pytest.raises(ValueError, match="locked"):
        cmd.apply(timeline)


# ------------------------------------------------------------------ R02

def test_reverse_right_trim_factory_applies_new_source_start() -> None:
    """K02：真实 UI 工厂只传 new_source_start，反向右修必须生效。"""
    timeline = _timeline()
    timeline = timeline.with_clip_added(
        "v1",
        _reverse_clip("c", "v1", source_start=100, source_duration=100),
    )
    cmd = TrimClipCommand.create_right_trim(
        "v1", "c", new_duration=90, new_source_duration=90,
        new_source_start=110,
    )
    new_timeline = cmd.apply(timeline)
    clip = new_timeline.get_clip_by_id("c")
    assert (clip.source_start, clip.source_duration) == (110, 90)
    # 反向保留帧不变：首 3 帧仍是 199/198/197（低端被裁的是 100–109）
    assert [clip.timeline_to_source_frame(i) for i in range(3)] == [199, 198, 197]


def test_reverse_right_trim_undo_restores_source_start() -> None:
    """K02：显式成功路径 undo 也要回 source_start（审计实测停在 110）。"""
    timeline = _timeline()
    timeline = timeline.with_clip_added(
        "v1",
        _reverse_clip("c", "v1", source_start=100, source_duration=100),
    )
    cmd = TrimClipCommand.create_right_trim(
        "v1", "c", new_duration=90, new_source_duration=90,
        new_source_start=110,
    )
    new_timeline = cmd.apply(timeline)
    restored = cmd.undo(new_timeline)
    clip = restored.get_clip_by_id("c")
    assert (clip.source_start, clip.source_duration) == (100, 100)
    assert restored.to_dict() == timeline.to_dict()


# ------------------------------------------------------------------ R05

def _fade_timeline() -> TimelineSequence:
    timeline = _timeline()
    return timeline.with_clip_added(
        "v1",
        _clip("c", "v1", 0, 100, fade_out_duration=40),
    )


def test_explicit_left_trim_with_fade_does_not_crash() -> None:
    """K05 复现1：显式左修 fade40 缩到 20 曾抛 ValueError。"""
    timeline = _fade_timeline()
    cmd = TrimClipCommand.create_left_trim(
        "v1", "c", new_start=80, new_source_start=80, new_source_duration=20,
    )
    new_timeline = cmd.apply(timeline)
    clip = new_timeline.get_clip_by_id("c")
    assert clip.timeline_duration == 20
    assert clip.fade_out_duration == 20  # 钳制而非崩溃


def test_speed_5x_with_fade_does_not_crash_and_undo_restores() -> None:
    """K05 复现2：fade40 片段 5x 变速曾抛 ValueError；undo 回 fade40。"""
    timeline = _fade_timeline()
    cmd = ChangeClipSpeedCommand.create("c", "v1", 5.0)
    new_timeline, updated = cmd.apply(timeline)
    clip = new_timeline.get_clip_by_id("c")
    assert clip.timeline_duration == 20
    assert clip.fade_out_duration == 20

    restored = updated.undo(new_timeline)
    clip = restored.get_clip_by_id("c")
    assert clip.timeline_duration == 100
    assert clip.fade_out_duration == 40


def _head_transition_timeline() -> TimelineSequence:
    """K05 复现3：clip100 fade40 + 头部 40 帧单边转场。"""
    timeline = _fade_timeline()
    head = TransitionInstance(
        transition_id="tr-head",
        transition_type="dip_to_color",
        track_id="v1",
        left_clip_id="",
        right_clip_id="c",
        center_frame=0,
        duration=40,
        alignment="start_on_cut",
    )
    return timeline.with_track_updated(
        timeline.get_track_by_id("v1").with_transition_added(head)
    )


def test_right_trim_undo_restores_fade_and_transition() -> None:
    """K05 复现3：默认右修到 20 后 undo，fade 与转场都要回 40。"""
    timeline = _head_transition_timeline()
    cmd = TrimClipCommand.create_right_trim("v1", "c", 20, 20)
    new_timeline = cmd.apply(timeline)
    assert new_timeline.get_clip_by_id("c").fade_out_duration == 20
    assert new_timeline.get_track_by_id("v1").transitions[0].duration == 20

    restored = cmd.undo(new_timeline)
    assert restored.get_clip_by_id("c").fade_out_duration == 40
    assert restored.get_track_by_id("v1").transitions[0].duration == 40
    assert restored.to_dict() == timeline.to_dict()


def test_trim_command_json_roundtrip_keeps_new_snapshots() -> None:
    """R09 方向：新增 fade/转场快照随 to_dict/from_dict 往返。"""
    import json

    timeline = _head_transition_timeline()
    cmd = TrimClipCommand.create_right_trim("v1", "c", 20, 20)
    new_timeline = cmd.apply(timeline)
    payload = json.loads(json.dumps(cmd.to_dict()))
    loaded = TrimClipCommand.from_dict(payload)
    restored = loaded.undo(new_timeline)
    assert restored.get_clip_by_id("c").fade_out_duration == 40
    assert restored.get_track_by_id("v1").transitions[0].duration == 40
