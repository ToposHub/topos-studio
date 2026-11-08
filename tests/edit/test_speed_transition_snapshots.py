"""R02：速度事务撤销必须还原转场（快照含整轨 transitions）。

反例（复审 S02a/b）：

- ``SetClipSpeedSettingsCommand``：clip 100 帧、尾部转场 80 → 2x 后
  clip 50、转场被钳 50；undo 恢复 clip 100 但**转场仍 50**（80 帧
  的转场定义永久丢失）；
- ``MoveSpeedPointCommand``：曲线 clip 缩短后转场被钳，undo 同样
  只还原 clip 几何，转场时长/中心停留在钳制值。

根因：``with_clip_updated``/``_rebuild(clips=...)`` 会触发
``reanchor_transitions`` 钳制转场，undo 重建 clip 后再次 reanchor
只会从**已丢失的时长**继续推导。修复 = apply 前快照受影响轨的完整
transitions，undo 在 clip 还原后显式恢复（ChangeClipSpeedCommand 的
既有模式）。断言逐字段（duration/center/alignment/id），不只比时长。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.commands import (
    CommandStack,
    MacroCommand,
    MoveSpeedPointCommand,
    SetClipSpeedSettingsCommand,
)
from src.features.edit.core.speed_settings import plan_speed_settings_edit
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
    TransitionInstance,
)

pytestmark = pytest.mark.edit_core


def _transition(clip_id: str, track_id: str, *, duration: int,
                end_frame: int) -> TransitionInstance:
    """clip 尾部单边淡出转场（left-only，end_on_cut 锚在片尾）。"""
    return TransitionInstance(
        transition_id=f"t-{clip_id}",
        transition_type="cross_dissolve",
        track_id=track_id,
        left_clip_id=clip_id,
        center_frame=end_frame,
        duration=duration,
        alignment="end_on_cut",
        params={"softness": 0.3},
    )


def _timeline(
    clip: ClipInstance,
    transition: TransitionInstance | None = None,
    *, extra_clips: tuple[ClipInstance, ...] = (),
    extra_transitions: tuple[TransitionInstance, ...] = (),
    partner: ClipInstance | None = None,
    partner_transition: TransitionInstance | None = None,
) -> TimelineSequence:
    video_clips = (clip,) + extra_clips
    transitions = (
        (transition,) if transition is not None else ()) + extra_transitions
    audio_clips = (partner,) if partner is not None else ()
    audio_transitions = (
        (partner_transition,) if partner_transition is not None else ())
    return TimelineSequence(
        "r02", "R02", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack(
            "v1", "V1", "video", 0, clips=video_clips,
            transitions=transitions),),
        audio_tracks=(EditTrack(
            "a1", "A1", "audio", 0, clips=audio_clips,
            transitions=audio_transitions),),
    )


def _settings(**overrides: Any) -> dict[str, Any]:
    base = {
        "speed": 2.0, "reverse": False, "freeze": False,
        "preserve_pitch": False, "ripple": False,
        "keyframe_timing": "stretch", "duration_frames": 50,
    }
    base.update(overrides)
    return base


def _fields(t: TransitionInstance) -> tuple:
    return (t.transition_id, t.transition_type, t.track_id,
            t.left_clip_id, t.right_clip_id, t.center_frame,
            t.duration, t.alignment, t.params)


# ── S02a：SetClipSpeedSettingsCommand ────────────────────────────────


def test_r02_speed_settings_undo_restores_transition():
    clip = ClipInstance("v", "asset", "v1", 0, 100, 0, 100)
    transition = _transition("v", "v1", duration=80, end_frame=100)
    timeline = _timeline(clip, transition)

    plan = plan_speed_settings_edit(timeline, "v1", "v", _settings())
    changed = plan.command.apply(timeline)

    # apply：clip 100→50，转场按新片长钳制（50）——这是 reanchor 的
    # 合法行为；丢失点在 undo。
    assert changed.get_clip_by_id("v").timeline_duration == 50
    clamped = changed.get_track_by_id("v1").transitions[0]
    assert clamped.duration == 50

    undone = plan.command.undo(changed)
    restored = undone.get_track_by_id("v1").transitions
    assert len(restored) == 1
    assert _fields(restored[0]) == _fields(transition)
    assert undone.get_clip_by_id("v").timeline_duration == 100


def test_r02_speed_settings_undo_restores_linked_transition():
    clip = replace(
        ClipInstance("v", "asset", "v1", 0, 100, 0, 100),
        linked_clip_id="a")
    partner = replace(
        ClipInstance("a", "asset", "a1", 0, 100, 0, 100),
        linked_clip_id="v")
    main_t = _transition("v", "v1", duration=80, end_frame=100)
    partner_t = _transition("a", "a1", duration=60, end_frame=100)
    timeline = _timeline(clip, main_t, partner=partner,
                         partner_transition=partner_t)

    plan = plan_speed_settings_edit(timeline, "v1", "v", _settings())
    changed = plan.command.apply(timeline)
    assert changed.get_track_by_id("a1").transitions[0].duration == 50

    undone = plan.command.undo(changed)
    assert _fields(undone.get_track_by_id("v1").transitions[0]) == \
        _fields(main_t)
    assert _fields(undone.get_track_by_id("a1").transitions[0]) == \
        _fields(partner_t)


def test_r02_speed_settings_ripple_transition_restored():
    """ripple 后继平移 + 非居中对齐的长转场被钳，undo 全量还原。"""
    clip = ClipInstance("v", "asset", "v1", 0, 100, 0, 100)
    follower = ClipInstance("f", "asset", "v1", 100, 40, 0, 40)
    cut = TransitionInstance(
        transition_id="t-cut", track_id="v1",
        left_clip_id="v", right_clip_id="f",
        center_frame=100, duration=120, alignment="start_on_cut")
    timeline = _timeline(clip, extra_clips=(follower,),
                         extra_transitions=(cut,))

    plan = plan_speed_settings_edit(
        timeline, "v1", "v", _settings(ripple=True))
    changed = plan.command.apply(timeline)
    assert changed.get_clip_by_id("v").timeline_duration == 50
    assert changed.get_clip_by_id("f").timeline_start == 50
    # 非居中：max = 50 + 40 = 90 → 120 被钳
    assert changed.get_track_by_id("v1").transitions[0].duration == 90

    undone = plan.command.undo(changed)
    assert undone.get_clip_by_id("f").timeline_start == 100
    assert _fields(undone.get_track_by_id("v1").transitions[0]) == \
        _fields(cut)


def test_r02_speed_settings_macro_single_undo_restores():
    clip = ClipInstance("v", "asset", "v1", 0, 100, 0, 100)
    transition = _transition("v", "v1", duration=80, end_frame=100)
    timeline = _timeline(clip, transition)

    cmd = SetClipSpeedSettingsCommand.create("v", "v1", _settings())
    macro = MacroCommand(command_id="m1", label="macro",
                         sub_commands=(cmd,))
    stack = CommandStack()
    changed = stack.execute(macro, timeline)
    assert changed.get_track_by_id("v1").transitions[0].duration == 50
    assert len(stack._undo_stack) == 1

    undone = stack.undo(changed)
    assert _fields(undone.get_track_by_id("v1").transitions[0]) == \
        _fields(transition)


def test_r02_speed_settings_redo_undo_cycle_restores_twice():
    clip = ClipInstance("v", "asset", "v1", 0, 100, 0, 100)
    transition = _transition("v", "v1", duration=80, end_frame=100)
    timeline = _timeline(clip, transition)
    cmd = SetClipSpeedSettingsCommand.create("v", "v1", _settings())

    first = cmd.apply(timeline)
    back = cmd.undo(first)
    assert _fields(back.get_track_by_id("v1").transitions[0]) == \
        _fields(transition)
    # redo（同一命令实例重放）→ 再次钳制 → 再次 undo 仍全量还原
    second = cmd.apply(back)
    assert second.get_track_by_id("v1").transitions[0].duration == 50
    back2 = cmd.undo(second)
    assert _fields(back2.get_track_by_id("v1").transitions[0]) == \
        _fields(transition)


def test_r02_speed_settings_noop_and_locked_leave_transitions():
    clip = ClipInstance("v", "asset", "v1", 0, 100, 0, 100)
    transition = _transition("v", "v1", duration=80, end_frame=100)
    timeline = _timeline(clip, transition)

    # no-op（1x 同值）：同一实例、转场原样
    noop = SetClipSpeedSettingsCommand.create(
        "v", "v1", _settings(speed=1.0, duration_frames=100))
    assert noop.apply(timeline) is timeline

    # 锁轨拒绝：转场与 clip 均零写入
    locked = _timeline(
        clip, transition)  # 同构，轨锁在下面构造
    locked = TimelineSequence(
        "r02", "R02", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack(
            "v1", "V1", "video", 0, clips=(clip,),
            transitions=(transition,), is_locked=True),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )
    with pytest.raises(ValueError):
        cmd = SetClipSpeedSettingsCommand.create("v", "v1", _settings())
        cmd.apply(locked)
    assert _fields(locked.get_track_by_id("v1").transitions[0]) == \
        _fields(transition)


# ── S02b：MoveSpeedPointCommand ──────────────────────────────────────


def _curve_clip() -> ClipInstance:
    """恒速 1.0 至源 20，之后 4x：时长 = 20 + 80/4 = 40 帧。"""
    return ClipInstance(
        "v", "asset", "v1", 0, 40, 0, 100,
        speed=SpeedParams(speed=1.0, speed_points=(
            SpeedPoint(source_offset=20, speed=4.0),)),
    )


def test_r02_move_speed_point_undo_restores_transition():
    clip = _curve_clip()
    transition = _transition("v", "v1", duration=36, end_frame=40)
    timeline = _timeline(clip, transition)

    cmd = MoveSpeedPointCommand.create("v", "v1", 20, 5)
    changed = cmd.apply(timeline)

    # 点移到 5：时长 = 5 + 95/4 = 28.75 → 29，转场钳 29
    assert changed.get_clip_by_id("v").timeline_duration == 29
    assert changed.get_track_by_id("v1").transitions[0].duration == 29

    undone = cmd.undo(changed)
    assert undone.get_clip_by_id("v").timeline_duration == 40
    assert _fields(undone.get_track_by_id("v1").transitions[0]) == \
        _fields(transition)


def test_r02_move_speed_point_linked_transition_restored():
    clip = replace(_curve_clip(), linked_clip_id="a")
    partner = replace(
        ClipInstance(
            "a", "asset", "a1", 0, 40, 0, 100,
            speed=SpeedParams(speed=1.0, speed_points=(
                SpeedPoint(source_offset=20, speed=4.0),)),
        ), linked_clip_id="v")
    main_t = _transition("v", "v1", duration=36, end_frame=40)
    partner_t = _transition("a", "a1", duration=30, end_frame=40)
    timeline = _timeline(clip, main_t, partner=partner,
                         partner_transition=partner_t)

    cmd = MoveSpeedPointCommand.create("v", "v1", 20, 5)
    changed = cmd.apply(timeline)
    assert changed.get_track_by_id("a1").transitions[0].duration == 29

    undone = cmd.undo(changed)
    assert _fields(undone.get_track_by_id("v1").transitions[0]) == \
        _fields(main_t)
    assert _fields(undone.get_track_by_id("a1").transitions[0]) == \
        _fields(partner_t)
