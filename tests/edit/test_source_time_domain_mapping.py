"""批 C1 回归：C04 reverse 帧映射 / C05 方向感知切分 / C06+INT-12 修剪时域
映射 / C07+FE-B04 混合 FPS 时长。

对应 docs/edit_page_full_audit_optimization_plan_2026-10-14.md §9。
验收以逐帧 source 映射不变 / 精确时长为准。
"""

from __future__ import annotations

import dataclasses
import json
from fractions import Fraction

import pytest

from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.commands import (
    ChangeClipSpeedCommand,
    SplitClipCommand,
    TrimClipCommand,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence

pytestmark = pytest.mark.edit_core


def _seq_with(clip: ClipInstance) -> TimelineSequence:
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    return (
        TimelineSequence(timeline_id="t1", name="T", fps=24.0)
        .with_track_added(track)
        .with_clip_added("v1", clip)
    )


def _clip(speed=1.0, **kw) -> ClipInstance:
    params = speed if isinstance(speed, SpeedParams) else SpeedParams(speed=float(speed))
    return ClipInstance(
        clip_id="c1",
        asset_id="a1",
        track_id="v1",
        timeline_start=0,
        timeline_duration=kw.pop("timeline_duration", 100),
        source_start=kw.pop("source_start", 100),
        source_duration=kw.pop("source_duration", 100),
        speed=params,
        **kw,
    )


# ---------------------------------------------------------------- C04

def test_reverse_respected_with_fps_metadata() -> None:
    """审计实测：有 24/24 元数据后倒放前 3 帧变 100/101/102。"""
    clip = _clip(SpeedParams(speed=1.0, reverse=True))
    with_fps = dataclasses.replace(
        clip, _source_fps_num=24, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1,
    )
    frames_no_meta = [clip.timeline_to_source_frame(f) for f in (0, 1, 2)]
    frames_meta = [with_fps.timeline_to_source_frame(f) for f in (0, 1, 2)]
    assert frames_no_meta == frames_meta == [199, 198, 197]


# ---------------------------------------------------------------- C05

def test_split_reverse_frame_sequence_unchanged() -> None:
    """审计实测：倒放 40 切分后左段前 3 帧错乱。切分前后逐帧相同。"""
    seq = _seq_with(_clip(SpeedParams(speed=1.0, reverse=True)))
    frames = list(range(0, 100, 3))
    before = [seq.get_clip_by_id("c1").timeline_to_source_frame(f) for f in frames]

    new = SplitClipCommand.create("v1", "c1", 40).apply(seq)
    left = new.get_clip_by_id("c1")
    right_id = [c.clip_id for c in new.get_track_by_id("v1").clips if c.clip_id != "c1"][0]
    right = new.get_clip_by_id(right_id)
    after = [
        (left if f < 40 else right).timeline_to_source_frame(f) for f in frames
    ]
    assert before == after


def test_split_variable_speed_frame_sequence_unchanged() -> None:
    """审计实测：offset50 后 2x，60 切分后逐帧变化。"""
    seq = _seq_with(
        _clip(
            SpeedParams(speed=1.0, speed_points=(SpeedPoint(source_offset=50, speed=2.0),)),
            timeline_duration=75,
        )
    )
    frames = list(range(0, 75, 2))
    before = [seq.get_clip_by_id("c1").timeline_to_source_frame(f) for f in frames]

    new = SplitClipCommand.create("v1", "c1", 60).apply(seq)
    left = new.get_clip_by_id("c1")
    right_id = [c.clip_id for c in new.get_track_by_id("v1").clips if c.clip_id != "c1"][0]
    right = new.get_clip_by_id(right_id)
    after = [
        (left if f < 60 else right).timeline_to_source_frame(f) for f in frames
    ]
    assert before == after


def test_split_forward_unchanged() -> None:
    seq = _seq_with(_clip(1.0))
    frames = list(range(0, 100, 3))
    before = [seq.get_clip_by_id("c1").timeline_to_source_frame(f) for f in frames]
    new = SplitClipCommand.create("v1", "c1", 40).apply(seq)
    left = new.get_clip_by_id("c1")
    right_id = [c.clip_id for c in new.get_track_by_id("v1").clips if c.clip_id != "c1"][0]
    right = new.get_clip_by_id(right_id)
    after = [(left if f < 40 else right).timeline_to_source_frame(f) for f in frames]
    assert before == after


# ---------------------------------------------------------------- C06/INT-12

def test_trim_left_2x_maps_timeline_to_source() -> None:
    """审计实测：timeline[100,200)/source[100,300)/2x，左裁 10 timeline 帧
    应 source_start 120 / duration 180。"""
    clip = _clip(2.0, timeline_duration=100, source_start=100, source_duration=200)
    clip = clip.with_timeline_start(100)
    ns, nd = clip.with_left_edge_source_values(110)
    assert (ns, nd) == (120, 180)


def test_trim_right_2x_maps_timeline_to_source() -> None:
    clip = _clip(2.0, timeline_duration=100, source_start=100, source_duration=200)
    clip = clip.with_timeline_start(100)
    ns, nd = clip.with_right_edge_source_values(90)
    assert (ns, nd) == (100, 180)


def test_trim_reverse_direction_aware() -> None:
    clip = _clip(SpeedParams(speed=1.0, reverse=True))
    assert clip.with_right_edge_source_values(90) == (110, 90)
    assert clip.with_left_edge_source_values(10) == (100, 90)
    assert clip.with_right_edge_source_values(110) == (90, 110)  # 延伸


def test_trim_mixed_fps_ratio_applied() -> None:
    clip = _clip(1.0, timeline_duration=80, source_start=0, source_duration=100)
    clip = dataclasses.replace(
        clip, _source_fps_num=30, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1,
    )
    ns, nd = clip.with_left_edge_source_values(10)
    # v4 retains the exact fractional sampling clock. The canonical CFR
    # mapper selects 13 at 12.5 (half-up); Python round(12.5)==12 is a
    # different integer-window representation, not the retained-image contract.
    assert ns == clip.timeline_to_source_frame(10) == 13
    assert nd == 100 - ns
    command = TrimClipCommand.create_left_trim(
        "v1", "c1", 10, ns, new_source_duration=nd)
    sequence = _seq_with(clip)
    changed = command.apply(sequence)
    trimmed = changed.get_clip_by_id("c1")
    assert Fraction(trimmed._source_frame_phase_num, trimmed._source_frame_phase_den) == Fraction(-1, 2)
    assert [trimmed.timeline_to_source_frame(f) for f in range(10, 80)] == [
        clip.timeline_to_source_frame(f) for f in range(10, 80)]
    for frame in range(10, 80):
        a, b, weight = trimmed.timeline_to_source_frame_pair(frame)
        original_a, original_b, original_weight = clip.timeline_to_source_frame_pair(frame)
        assert (trimmed.source_start + a, trimmed.source_start + b, weight) == (
            clip.source_start + original_a, clip.source_start + original_b, original_weight)
    loaded = TrimClipCommand.from_dict(json.loads(json.dumps(command.to_dict())))
    assert loaded.undo(changed).to_dict() == sequence.to_dict()


def test_trim_command_accepts_explicit_direction_aware_values() -> None:
    """命令层：显式 (start, duration) 的方向感知修剪正确落地并可 undo。"""
    seq = _seq_with(_clip(SpeedParams(speed=1.0, reverse=True)))
    cmd = TrimClipCommand.create_left_trim(
        track_id="v1", clip_id="c1",
        new_start=10, new_source_start=100,
        old_start=0, old_source_start=100,
        old_duration=100, old_source_duration=100,
        new_source_duration=90,
    )
    result = cmd.apply(seq)
    new = result[0] if isinstance(result, tuple) else result
    clip = new.get_clip_by_id("c1")
    assert (clip.timeline_start, clip.timeline_duration) == (10, 90)
    assert (clip.source_start, clip.source_duration) == (100, 90)
    undone = cmd.undo(new)
    assert undone.get_clip_by_id("c1").source_duration == 100


# ---------------------------------------------------------------- C07/FE-B04

def test_speed_change_mixed_fps_duration() -> None:
    """审计实测：30fps 源 120 帧（timeline 96）在 24fps 序列 2x 应 48。"""
    clip = _clip(1.0, timeline_duration=96, source_start=0, source_duration=120)
    clip = dataclasses.replace(
        clip, _source_fps_num=30, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1,
    )
    seq = _seq_with(clip)
    cmd = ChangeClipSpeedCommand.create("c1", "v1", 2.0)
    result = cmd.apply(seq)
    new = result[0] if isinstance(result, tuple) else result
    assert new.get_clip_by_id("c1").timeline_duration == 48


def test_insert_mixed_fps_duration() -> None:
    """30fps 源 1 秒（30 帧）插入 24fps 序列应 24 帧。"""
    from src.features.edit.core.timeline_ops import insert_clip_at_playhead

    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    seq = (
        TimelineSequence(timeline_id="t1", name="T", fps=24.0)
        .with_track_added(track)
    )
    new = insert_clip_at_playhead(
        seq, "a1", 0, 29, 0, "v1", 1.0,
        source_fps_num=30, source_fps_den=1,
        timeline_fps_num=24, timeline_fps_den=1,
    )
    clip = new.get_track_by_id("v1").clips[0]
    assert clip.timeline_duration == 24


def test_append_mixed_fps_duration() -> None:
    from src.features.edit.core.timeline_ops import append_clip_to_timeline

    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    seq = (
        TimelineSequence(timeline_id="t1", name="T", fps=24.0)
        .with_track_added(track)
    )
    new = append_clip_to_timeline(
        seq, "a1", 0, 29, "v1", 1.0,
        source_fps_num=30, source_fps_den=1,
        timeline_fps_num=24, timeline_fps_den=1,
    )
    clip = new.get_track_by_id("v1").clips[0]
    assert clip.timeline_duration == 24
