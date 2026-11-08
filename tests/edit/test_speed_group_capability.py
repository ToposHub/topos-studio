"""R01：链接组能力与参数预检——speed settings 的全组成员检查。

反例（复审 S01）：主片恒速、互链伙伴带速度点 (50, 2x) 时，
``plan_speed_settings_edit`` 仍 executable，``SetClipSpeedSettingsCommand``
提交后伙伴速度点 1→0 被静默清除。本文件锁定：

- 组级能力（主片 + 互链伙伴的曲线/冻结/轨锁）在任何 apply 前拒绝；
- 单向坏链不当作组（互链编辑以双向有效链接为准）；
- 两种主片方向（视频主/音频主）支持组 apply + undo 字段逐一相等；
- 逐成员保留非目标字段（伙伴自己的曲线窗口/淡变/动画各自保留与还原）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.commands import SetClipSpeedSettingsCommand
from src.features.edit.core.speed_settings import (
    check_group_capability,
    plan_speed_settings_edit,
    resolve_edit_group,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, **kwargs: Any) -> ClipInstance:
    speed = kwargs.pop("speed", SpeedParams(speed=1.0))
    animated = kwargs.pop("animated", False)
    clip = ClipInstance(
        clip_id, kwargs.pop("asset_id", "asset"), track_id,
        kwargs.pop("timeline_start", 0),
        kwargs.pop("duration", 100),
        kwargs.pop("source_start", 0),
        kwargs.pop("source_duration", 100),
        fade_in_duration=kwargs.pop("fade_in", 10),
        fade_out_duration=kwargs.pop("fade_out", 12),
        speed=speed,
        _source_fps_num=kwargs.pop("source_fps_num", 24),
        _source_fps_den=kwargs.pop("source_fps_den", 1),
        **kwargs,
    )
    if animated:
        clip = clip.with_keyframe_added("opacity", 50, 0.5)
    return clip


def _pair_timeline(
    main: ClipInstance, partner: ClipInstance | None = None,
    *, lock_audio: bool = False, lock_video: bool = False,
) -> TimelineSequence:
    """互链 A/V 对：main 在 v1，partner 在 a1（双向 linked_clip_id）。"""
    if partner is not None:
        main = replace(main, linked_clip_id=partner.clip_id)
        partner = replace(partner, linked_clip_id=main.clip_id)
    return TimelineSequence(
        "r01", "R01", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack(
            "v1", "V1", "video", 0, clips=(main,),
            is_locked=lock_video),),
        audio_tracks=(EditTrack(
            "a1", "A1", "audio", 0,
            clips=(partner,) if partner is not None else (),
            is_locked=lock_audio),),
    )


def _settings(**overrides: Any) -> dict[str, Any]:
    base = {
        "speed": 2.0, "reverse": False, "freeze": False,
        "preserve_pitch": False, "ripple": False,
        "keyframe_timing": "stretch", "duration_frames": 50,
    }
    base.update(overrides)
    return base


# ── 组级能力预检：伙伴状态必须在任何 apply 前拒绝 ─────────────────────


def test_r01_partner_speed_point_rejected_zero_write():
    """S01 反例：主片恒速 + 伙伴带点 → 拒绝；命令直连也零写入。"""
    main = _clip("v", "v1")
    partner = _clip("a", "a1", speed=SpeedParams(
        speed=1.0, speed_points=(SpeedPoint(source_offset=50, speed=2.0),)))
    timeline = _pair_timeline(main, partner)

    plan = plan_speed_settings_edit(timeline, "v1", "v", _settings())
    assert not plan.executable
    assert plan.unsupported and "linked" in plan.unsupported.lower()

    # 绕过接收层直接构造命令：apply 必须在任何轨道重建前抛错
    cmd = SetClipSpeedSettingsCommand.create("v", "v1", _settings())
    with pytest.raises(ValueError):
        cmd.apply(timeline)
    # 伙伴速度点原样保留（零写入）
    params = timeline.get_clip_by_id("a").speed
    assert len(params.speed_points) == 1
    assert params.speed_points[0].source_offset == 50


def test_r01_partner_frozen_rejected():
    main = _clip("v", "v1")
    partner = _clip("a", "a1").with_freeze_frame(5)
    timeline = _pair_timeline(main, partner)

    plan = plan_speed_settings_edit(timeline, "v1", "v", _settings())
    assert plan.unsupported and "frozen" in plan.unsupported.lower()
    assert timeline.get_clip_by_id("a").freeze_source_frame == 5


def test_r01_partner_track_locked_rejected_zero_history():
    main = _clip("v", "v1")
    partner = _clip("a", "a1")
    timeline = _pair_timeline(main, partner, lock_audio=True)

    reason = check_group_capability(timeline, timeline.get_clip_by_id("v"))
    assert reason is not None and "locked" in reason.lower()
    plan = plan_speed_settings_edit(timeline, "v1", "v", _settings())
    assert not plan.executable
    assert timeline.get_clip_by_id("v").timeline_duration == 100


def test_r01_main_track_locked_rejected():
    main = _clip("v", "v1")
    timeline = _pair_timeline(main, lock_video=True)
    plan = plan_speed_settings_edit(timeline, "v1", "v", _settings())
    assert not plan.executable
    assert (plan.unsupported or plan.error) and \
        "locked" in (plan.unsupported or plan.error).lower()


def test_r01_one_way_stale_link_is_solo():
    """单向坏链（目标不存在/不回指）不是组：按单片处理。"""
    stale = replace(_clip("v", "v1"), linked_clip_id="ghost")
    partner = _clip("a", "a1")  # 未回指 → 坏链
    timeline = TimelineSequence(
        "r01", "R01", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(stale,)),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0, clips=(partner,)),),
    )
    # 双向校验：ghost 不存在 → 组只有主片
    group = resolve_edit_group(timeline, timeline.get_clip_by_id("v"))
    assert [c.clip_id for c in group] == ["v"]

    plan = plan_speed_settings_edit(timeline, "v1", "v", _settings())
    assert plan.executable
    changed = plan.command.apply(timeline)
    assert changed.get_clip_by_id("v").timeline_duration == 50
    # 伙伴不被组事务触碰
    assert changed.get_clip_by_id("a").timeline_duration == 100


# ── 支持组：两种主片方向 + 逐成员非目标字段保留 ───────────────────────


def test_r01_supported_group_apply_undo_fieldwise(video_main: bool = True):
    """视频主方向与音频主方向：apply 同步全组，undo 字段逐一相等。"""
    for video_main in (True, False):
        main = _clip("v", "v1", animated=True)
        partner = _clip("a", "a1", animated=True)
        timeline = _pair_timeline(main, partner)
        before = {
            cid: timeline.get_clip_by_id(cid).to_dict()
            for cid in ("v", "a")
        }

        main_track = "v1" if video_main else "a1"
        main_id = "v" if video_main else "a"
        plan = plan_speed_settings_edit(
            timeline, main_track, main_id, _settings())
        assert plan.executable, plan.unsupported or plan.error

        changed = plan.command.apply(timeline)
        for cid in ("v", "a"):
            clip = changed.get_clip_by_id(cid)
            assert clip.speed.speed == 2.0
            assert clip.timeline_duration == 50
            assert clip.fade_in_duration == 10
            assert clip.fade_out_duration == 12
            key = clip.animated_params["opacity"].keyframes.keyframes[0]
            assert key.frame == 25  # stretch：50/100 → 25/50

        undone = plan.command.undo(changed)
        for cid in ("v", "a"):
            assert undone.get_clip_by_id(cid).to_dict() == before[cid], (
                f"undo must restore member {cid!r} field-by-field "
                f"(direction video_main={video_main})")


def test_r01_partner_curve_window_preserved():
    """伙伴自己的恒速切片窗口（曲线坐标）是**非目标字段**，不被主片
    base 覆盖——此前 new_params 源自主片，伙伴 curve_offset 被静默清 0。"""
    main = _clip("v", "v1")
    partner = _clip("a", "a1", speed=SpeedParams(
        speed=1.0, curve_offset=16, curve_length=100))
    timeline = _pair_timeline(main, partner)

    plan = plan_speed_settings_edit(timeline, "v1", "v", _settings())
    assert plan.executable
    changed = plan.command.apply(timeline)

    params = changed.get_clip_by_id("a").speed
    assert params.speed == 2.0  # 目标字段：组速度
    assert params.curve_offset == 16  # 非目标字段：伙伴窗口保留
    assert params.curve_length == 100


def test_r01_different_span_and_fps_members_proportional():
    """不同源 span / 源 fps 的成员：逐成员按自己的源时基换算时长。"""
    main = _clip("v", "v1")  # 源 100 帧 @24fps → 2x = 50
    partner = _clip(
        "a", "a1", duration=160, source_duration=80,
        source_fps_num=12)  # 源 80 帧 @12fps → 1x=160 → 2x=80
    timeline = _pair_timeline(main, partner)

    plan = plan_speed_settings_edit(timeline, "v1", "v", _settings(
        duration_frames=50))
    assert plan.executable, plan.unsupported or plan.error
    changed = plan.command.apply(timeline)
    assert changed.get_clip_by_id("v").timeline_duration == 50
    assert changed.get_clip_by_id("a").timeline_duration == 80

    undone = plan.command.undo(changed)
    assert undone.get_clip_by_id("v").timeline_duration == 100
    assert undone.get_clip_by_id("a").timeline_duration == 160


def test_r01_command_validates_partner_before_any_track_write():
    """命令层独立防御：第一成员已改、第二成员非法时不得半提交。

    命令 apply 的成员校验必须先于任何 with_track_updated 完成——
    通过冻结伙伴验证：抛 ValueError 后主片也未被改写。
    """
    main = _clip("v", "v1")
    partner = _clip("a", "a1").with_freeze_frame(0)
    timeline = _pair_timeline(main, partner)

    cmd = SetClipSpeedSettingsCommand.create("v", "v1", _settings())
    with pytest.raises(ValueError):
        cmd.apply(timeline)
    assert timeline.get_clip_by_id("v").timeline_duration == 100
    assert timeline.get_clip_by_id("v").speed.speed == 1.0
