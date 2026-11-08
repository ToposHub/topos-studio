"""B15：MoveSpeedPointCommand——速度点沿源坐标移动的领域操作。

覆盖：边界/碰撞拒绝、移动点前采样逐帧不变、积分时长重算（混 fps、
8x、smooth）、反放、切片 curve_offset 窗口、互链伙伴同步、快照式
undo 完整还原（含动画）、序列化往返。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from src.features.edit.core.commands import (
    CommandStack,
    MoveSpeedPointCommand,
)
from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(speed: SpeedParams, *, duration: int = 80,
          source_duration: int = 80, fps_meta: tuple | None = None,
          animated: bool = False) -> ClipInstance:
    extras: dict[str, Any] = {}
    if fps_meta:
        extras = {
            "_source_fps_num": fps_meta[0], "_source_fps_den": fps_meta[1],
            "_timeline_fps_num": fps_meta[2], "_timeline_fps_den": fps_meta[3],
        }
    clip = ClipInstance(
        "v", "asset", "v1", 0, duration, 0, source_duration,
        speed=speed, **extras)
    if animated:
        clip = clip.with_keyframe_added("opacity", 5, 0.2)
    return clip


def _timeline(clip: ClipInstance, partner: ClipInstance | None = None,
              ) -> TimelineSequence:
    audio_clips = (partner,) if partner is not None else ()
    return TimelineSequence(
        "move-point", "MovePoint", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack(
            "v1", "V1", "video", 0, clips=(clip,)),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0, clips=audio_clips),),
    )


def _sampled(clip: ClipInstance, frames: list[int]) -> list[int]:
    return [clip.timeline_to_source_frame(f) for f in frames]


def test_b15_move_keeps_mapping_before_point_and_reintegrates_after():
    """点前逐帧不变；点后按新分界重积分；时长随积分重算。"""
    params = SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=16, speed=2.0),))
    clip = _clip(params, duration=80, source_duration=80)
    timeline = _timeline(clip)
    before = _sampled(clip, [0, 8, 15, 16, 40, 79])

    cmd = MoveSpeedPointCommand.create("v", "v1", 16, 32)
    changed = cmd.apply(timeline)

    moved_clip = changed.get_clip_by_id("v")
    # 点前（timeline 帧 0..15 采样源 0..15）逐帧不变
    assert _sampled(moved_clip, [0, 8, 15]) == before[:3]
    # 新分界 32：1x 段变长（0..32 源），点后 2x
    assert moved_clip.timeline_to_source_frame(31) == 31
    assert moved_clip.timeline_to_source_frame(32) == 32
    assert moved_clip.speed.speed_points == (
        SpeedPoint(source_offset=32, speed=2.0),)
    # 时长重算：32/1 + 48/2 = 56
    assert moved_clip.timeline_duration == 56


def test_b15_same_position_is_noop_identity():
    params = SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=16, speed=2.0),))
    timeline = _timeline(_clip(params))
    cmd = MoveSpeedPointCommand.create("v", "v1", 16, 16)
    assert cmd.apply(timeline) is timeline


def test_b15_collision_and_boundary_rejected():
    params = SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=16, speed=2.0),
        SpeedPoint(source_offset=48, speed=4.0),
    ))
    timeline = _timeline(_clip(params, source_duration=80))
    # 与相邻点重合
    with pytest.raises(ValueError):
        MoveSpeedPointCommand.create("v", "v1", 16, 48).apply(timeline)
    # 越过相邻点（乱序）
    with pytest.raises(ValueError):
        MoveSpeedPointCommand.create("v", "v1", 16, 60).apply(timeline)
    # 出域（0 或域长之外）
    with pytest.raises(ValueError):
        MoveSpeedPointCommand.create("v", "v1", 16, 0).apply(timeline)
    with pytest.raises(ValueError):
        MoveSpeedPointCommand.create("v", "v1", 48, 90).apply(timeline)
    # 点不存在
    with pytest.raises(ValueError):
        MoveSpeedPointCommand.create("v", "v1", 20, 30).apply(timeline)


def test_b15_8x_and_mixed_fps_recompute_duration():
    """8x 段 + 48fps 源/24fps 时间线：时长按完整积分与 fps 比重算。"""
    params = SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=16, speed=8.0),))
    clip = _clip(
        params,
        duration=round(16 + 64 / 8 * 2),  # 1x 段 16×2 + 8x 段 64/8×2 = 48
        source_duration=80,
        fps_meta=(48, 1, 24, 1))
    timeline = _timeline(clip)

    changed = MoveSpeedPointCommand.create("v", "v1", 16, 40).apply(timeline)

    moved = changed.get_clip_by_id("v")
    # 新积分：40 源帧 1x + 40 源帧 8x，时间线域 = (40 + 5) × (24/48) = 22.5 → 23
    assert moved.timeline_duration in (22, 23)


def test_b15_smooth_easing_point_moves_with_easing_kept():
    params = SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=16, speed=2.0, easing="smooth"),))
    timeline = _timeline(_clip(params))
    changed = MoveSpeedPointCommand.create("v", "v1", 16, 32).apply(timeline)
    (point,) = changed.get_clip_by_id("v").speed.speed_points
    assert point.source_offset == 32
    assert point.easing == "smooth"
    assert point.speed == 2.0


def test_b15_reverse_clip_moves_in_source_coordinates():
    """反放片：点位仍按源序存储/移动，reverse 只在输出端翻转。"""
    params = SpeedParams(speed=1.0, reverse=True, speed_points=(
        SpeedPoint(source_offset=16, speed=2.0),))
    timeline = _timeline(_clip(params))
    changed = MoveSpeedPointCommand.create("v", "v1", 16, 32).apply(timeline)
    moved = changed.get_clip_by_id("v")
    assert moved.speed.reverse is True
    assert moved.speed.speed_points[0].source_offset == 32


def test_b15_sliced_window_moves_in_curve_coordinates():
    """切片 clip（curve_offset>0）：窗口坐标输入换算到曲线坐标。"""
    params = SpeedParams(
        speed=1.0,
        speed_points=(SpeedPoint(source_offset=40, speed=2.0),),
        curve_offset=24, curve_length=104)  # 窗口 [24, 104)
    clip = _clip(params, duration=40, source_duration=80)
    timeline = _timeline(clip)

    # 窗口 offset 16（曲线 40）→ 窗口 offset 40（曲线 64）
    changed = MoveSpeedPointCommand.create("v", "v1", 16, 40).apply(timeline)

    moved = changed.get_clip_by_id("v")
    assert moved.speed.curve_offset == 24
    assert moved.speed.speed_points[0].source_offset == 64


def test_b15_linked_partner_gets_same_moved_curve_and_undo_restores():
    params = SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=16, speed=2.0),))
    clip = _clip(params, animated=True)
    partner = replace(
        _clip(params, animated=True),
        clip_id="a", track_id="a1", linked_clip_id="v",
        timeline_duration=80, source_duration=80)
    clip = replace(clip, linked_clip_id="a")
    timeline = _timeline(clip, partner)

    stack = CommandStack()
    cmd = MoveSpeedPointCommand.create("v", "v1", 16, 32)
    changed = stack.execute(cmd, timeline)

    assert changed.get_clip_by_id("a").speed.speed_points[0].source_offset == 32
    assert changed.get_clip_by_id("a").timeline_duration == \
        changed.get_clip_by_id("v").timeline_duration
    assert len(stack._undo_stack) == 1

    undone = stack.undo(changed)
    for cid in ("v", "a"):
        restored = undone.get_clip_by_id(cid)
        assert restored.speed.speed_points[0].source_offset == 16
        assert restored.timeline_duration == 80
        assert restored.fade_in_duration == clip.fade_in_duration
        keys = restored.animated_params["opacity"].keyframes.keyframes
        assert [k.frame for k in keys] == [5]


def test_b15_serialization_roundtrip():
    """to_dict 不携带快照：from_dict 恢复后走反向移动降级 undo。"""
    # 积分一致的基线：16/1x + 64/2x = 48
    timeline = _timeline(_clip(
        SpeedParams(speed=1.0, speed_points=(
            SpeedPoint(source_offset=16, speed=2.0),)),
        duration=48))
    cmd = MoveSpeedPointCommand.create("v", "v1", 16, 32)
    changed = cmd.apply(timeline)
    revived = MoveSpeedPointCommand.from_dict(cmd.to_dict())
    assert revived.source_offset == 16 and revived.new_source_offset == 32
    undone = revived.undo(changed)
    assert undone.get_clip_by_id("v").speed.speed_points[0].source_offset == 16
    # 降级路径按积分重算（与原曲线一致的真实时长）
    assert undone.get_clip_by_id("v").timeline_duration == 48
