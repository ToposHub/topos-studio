"""R10：B14 stage-2/4/5 收编——reset / freeze 事务 + 逐项能力诊断。

缺口（复审 R10）：带速度点/动画的分段 stretch、freeze/reverse/rewind/
ramp/reset 此前全部走"not supported yet"的笼统请求——菜单存在不代表
内容能力完成。

本轮契约：

- ``reset_clip`` **不受能力门限制**（重置正是复杂状态的出口）：曲线/
  动画/互链经 ChangeClipSpeedCommand 的 B17/R05/R02 快照语义单步
  还原到 1x，且成功后状态栏回写；
- ``freeze``：整片定格在 source_offset 的权威映射源帧
  （SetClipFreezeCommand；时长保留、曲线参数保留在底层，undo 恢复
  曲线）；已冻结/映射失败给出具体诊断；
- 曲线上的分段 stretch / reverse、rewind：显式不支持 + **逐项**原因
  （Rewind 不是整片倒放；不冒充、零写入）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.commands import SetClipFreezeCommand
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
    TransitionInstance,
)

pytestmark = pytest.mark.edit_ui


def _clip(**kwargs: Any) -> ClipInstance:
    base = dict(
        clip_id="c1", asset_id="asset", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
    )
    base.update(kwargs)
    return ClipInstance(**base)


def _curved() -> ClipInstance:
    """曲线夹具：50 帧 1x + 50 帧 2x → 权威时长 75（几何自洽）。"""
    return _clip(timeline_duration=75, speed=SpeedParams(
        speed=1.0, speed_points=(
            SpeedPoint(source_offset=50, speed=2.0),)))


def _timeline(clip: ClipInstance, transition: bool = False,
              locked: bool = False) -> TimelineSequence:
    transitions = (TransitionInstance(
        transition_id="t1", track_id="v1", left_clip_id="c1",
        center_frame=clip.timeline_end, duration=80,
        alignment="end_on_cut"),) if transition else ()
    return TimelineSequence(
        "r10", "R10", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack(
            "v1", "V1", "video", 0, clips=(clip,),
            transitions=transitions, is_locked=locked),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )


def _view(qtbot: Any, timeline: TimelineSequence):
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)
    return view


# ── reset_clip：复杂状态的出口 ──────────────────────────────────────


def test_r10_reset_clears_curves_and_animation_single_undo(qtbot):
    clip = _curved().with_keyframe_added("opacity", 50, 0.5)
    view = _view(qtbot, _timeline(clip, transition=True))

    view._on_retime_action_requested("c1", "v1", "reset_clip", 0, None)

    changed = view.get_timeline().get_clip_by_id("c1")
    assert changed.speed.speed == 1.0
    assert not changed.speed.speed_points
    assert changed.timeline_duration == 100  # 100 源帧 @1x
    key = changed.animated_params["opacity"].keyframes.keyframes[0]
    assert key.frame == round(50 * 100 / 75)  # F2.4 重锚 75→100
    assert len(view._command_stack._undo_stack) == 1

    view.undo()
    undone = view.get_timeline().get_clip_by_id("c1")
    assert [p.source_offset for p in undone.speed.speed_points] == [50]
    assert undone.timeline_duration == 75
    assert undone.animated_params["opacity"].keyframes.keyframes[0].frame \
        == 50
    # R02：转场一并还原
    assert view.get_timeline().get_track_by_id("v1").transitions[0] \
        .duration == 80


def test_r10_reset_linked_partner_reset_in_lockstep(qtbot):
    from dataclasses import replace as _r
    main = _r(_curved(), linked_clip_id="a")
    partner = _r(_clip(clip_id="a", track_id="a1", speed=SpeedParams(
        speed=1.0, speed_points=(
            SpeedPoint(source_offset=50, speed=2.0),))),
        linked_clip_id="c1")
    timeline = TimelineSequence(
        "r10", "R10", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(main,)),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0, clips=(partner,)),),
    )
    view = _view(qtbot, timeline)

    view._on_retime_action_requested("c1", "v1", "reset_clip", 0, None)

    assert view.get_timeline().get_clip_by_id("a").speed.speed == 1.0
    assert not view.get_timeline().get_clip_by_id("a").speed.speed_points


def test_r10_reset_noop_when_already_plain(qtbot):
    view = _view(qtbot, _timeline(_clip()))
    view._on_retime_action_requested("c1", "v1", "reset_clip", 0, None)
    assert not view._command_stack._undo_stack


# ── freeze：整片定格事务 ────────────────────────────────────────────


def test_r10_freeze_action_freezes_at_mapped_source_frame(qtbot):
    view = _view(qtbot, _timeline(_clip()))

    view._on_retime_action_requested("c1", "v1", "freeze", 30, None)

    changed = view.get_timeline().get_clip_by_id("c1")
    assert changed.freeze_source_frame == 30
    assert changed.timeline_duration == 100  # 时长保留
    assert changed.timeline_to_source_frame(0) == 30
    assert changed.timeline_to_source_frame(99) == 30
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view.get_timeline().get_clip_by_id("c1").freeze_source_frame \
        is None


def test_r10_freeze_keeps_curve_underneath_undo_restores(qtbot):
    view = _view(qtbot, _timeline(_curved()))

    view._on_retime_action_requested("c1", "v1", "freeze", 50, None)

    changed = view.get_timeline().get_clip_by_id("c1")
    assert changed.freeze_source_frame is not None
    # 曲线参数保留在底层（冻结显示覆盖；解冻后恢复）
    assert [p.source_offset for p in changed.speed.speed_points] == [50]
    view.undo()
    undone = view.get_timeline().get_clip_by_id("c1")
    assert undone.freeze_source_frame is None
    assert [p.source_offset for p in undone.speed.speed_points] == [50]


def test_r10_freeze_already_frozen_specific_diagnosis(qtbot):
    view = _view(qtbot, _timeline(_clip().with_freeze_frame(10)))
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    before = view.get_timeline()

    view._on_retime_action_requested("c1", "v1", "freeze", 30, None)

    assert any("frozen" in m.lower() for m in messages)
    assert view.get_timeline() is before
    assert not view._command_stack._undo_stack


def test_r10_freeze_command_semantics_direct():
    timeline = _timeline(_clip())
    cmd = SetClipFreezeCommand.create("c1", "v1", 42)
    changed = cmd.apply(timeline)
    assert changed.get_clip_by_id("c1").freeze_source_frame == 42
    # no-op 同值
    same = SetClipFreezeCommand.create("c1", "v1", 42)
    assert same.apply(changed) is changed
    # 解冻
    unfreeze = SetClipFreezeCommand.create("c1", "v1", None)
    thawed = unfreeze.apply(changed)
    assert thawed.get_clip_by_id("c1").freeze_source_frame is None
    # 负帧拒绝
    with pytest.raises(ValueError):
        SetClipFreezeCommand.create("c1", "v1", -1).apply(timeline)
    # 未执行 undo 拒绝
    with pytest.raises(ValueError):
        SetClipFreezeCommand.create("c1", "v1", 5).undo(timeline)
    # 序列化往返：在已冻结片段上改冻结帧 → 恢复到前值
    frozen_tl = _timeline(_clip().with_freeze_frame(10))
    cmd2 = SetClipFreezeCommand.create("c1", "v1", 42)
    ch2 = cmd2.apply(frozen_tl)
    revived2 = SetClipFreezeCommand.from_dict(cmd2.to_dict())
    assert revived2.undo(ch2).get_clip_by_id("c1").freeze_source_frame == 10
    # cmd 的 roundtrip undo 恢复到它 apply 前的无冻结态
    revived = SetClipFreezeCommand.from_dict(cmd.to_dict())
    assert revived.undo(thawed).get_clip_by_id("c1").freeze_source_frame \
        is None


# ── 逐项能力诊断（显式不支持，零写入） ──────────────────────────────


def test_r10_rewind_diagnosis_not_whole_clip_reverse(qtbot):
    view = _view(qtbot, _timeline(_curved()))
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    requests: list[tuple] = []
    view.clip_speed_settings_requested.connect(
        lambda *args: requests.append(args))
    before = view.get_timeline()

    view._on_retime_action_requested("c1", "v1", "rewind", 50, None)

    assert any("negative segment speeds" in m for m in messages)
    assert any("not whole-clip reverse" in m for m in messages)
    assert view.get_timeline() is before
    assert not view._command_stack._undo_stack
    # 意图仍完整转发（宿主可接管）
    assert requests, "intent must still be forwarded to the host"
    payloads = [args[-1] for args in requests if isinstance(
        args[-1], dict) and args[-1].get("operation") == "retime_controls"]
    assert payloads and payloads[0]["action"] == "rewind"


def test_r10_stretch_on_curves_specific_diagnosis(qtbot):
    view = _view(qtbot, _timeline(_curved().with_keyframe_added(
        "opacity", 10, 0.5)))
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    before = view.get_timeline()

    view._on_retime_action_requested(
        "c1", "v1", "stretch", 0,
        {"original_duration": 100, "duration": 50, "speed": 2.0})

    assert any("speed-point curves" in m for m in messages)
    assert view.get_timeline() is before
    assert not view._command_stack._undo_stack


def test_r10_reverse_on_curves_specific_diagnosis(qtbot):
    view = _view(qtbot, _timeline(_curved()))
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    before = view.get_timeline()

    view._on_retime_action_requested("c1", "v1", "reverse", 0, None)

    assert any("speed-point curves" in m for m in messages)
    assert view.get_timeline() is before
    assert not view._command_stack._undo_stack


def test_r10_plain_reverse_and_pitch_still_work(qtbot):
    """无曲线片段的 reverse/pitch 基础路径不回归。"""
    view = _view(qtbot, _timeline(_clip()))

    view._on_retime_action_requested("c1", "v1", "reverse", 0, None)
    assert view.get_timeline().get_clip_by_id("c1").speed.reverse is True
    view._on_retime_action_requested("c1", "v1", "pitch", 0, True)
    assert view.get_timeline().get_clip_by_id("c1").speed.preserve_pitch \
        is True
    assert len(view._command_stack._undo_stack) == 2
