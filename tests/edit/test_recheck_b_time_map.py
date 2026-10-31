"""复查批次 B-2 回归：R06 变速点 mixedFPS/smooth 积分 + R11 逆映射 FPS。

对应 docs/edit_page_followup_audit_optimization_plan_2026-10-14.md §3.1
（K06/K11），复现值取自 docs/audit/2026-10-14-edit-recheck/evidence/probes。
"""

from __future__ import annotations

import pytest

from src.features.edit.core.clip_params import SpeedPoint, SpeedParams
from src.features.edit.core.timeline_ops import timeline_duration_for_source
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence

pytestmark = pytest.mark.edit_core


def _speed_clip(speed: SpeedParams, source_duration: int, src_fps, tl_fps) -> ClipInstance:
    return ClipInstance(
        clip_id="c",
        asset_id="a",
        track_id="v1",
        timeline_start=0,
        timeline_duration=speed.timeline_duration_for_source_duration(source_duration),
        source_start=0,
        source_duration=source_duration,
        speed=speed,
        _source_fps_num=src_fps,
        _source_fps_den=1,
        _timeline_fps_num=tl_fps,
        _timeline_fps_den=1,
    )


def _seq(clip: ClipInstance) -> TimelineSequence:
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(
        EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    )
    return timeline.with_clip_added("v1", clip)


# ------------------------------------------------------------------ R06-1

def test_speed_point_mixed_fps_forward_mapping() -> None:
    """K06 复现1：120 源帧/30fps，base1 + offset60→2x，24fps 序列。
    时长 72；第 71 帧应播到源 ~117-118（旧实现 82）。"""
    speed = SpeedParams(
        speed=1.0,
        speed_points=(SpeedPoint(source_offset=60, speed=2.0),),
    )
    dur = timeline_duration_for_source(120, speed, 30, 1, 24, 1)
    assert dur == 72

    clip = ClipInstance(
        clip_id="c", asset_id="a", track_id="v1",
        timeline_start=0, timeline_duration=dur,
        source_start=0, source_duration=120, speed=speed,
        _source_fps_num=30, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1,
    )
    seq = _seq(clip)
    got = seq.get_clip_by_id("c").timeline_to_source_frame(71)
    assert 116 <= got <= 118
    # 最后一帧逼近源尾（旧实现只播到 82 就 clamp）
    last = seq.get_clip_by_id("c").timeline_to_source_frame(dur - 1)
    assert last >= 115


def test_speed_point_mixed_fps_covers_source_tail() -> None:
    """映射必须覆盖到源区间末端附近，不提前 clamp。"""
    speed = SpeedParams(
        speed=1.0,
        speed_points=(SpeedPoint(source_offset=60, speed=2.0),),
    )
    clip = ClipInstance(
        clip_id="c", asset_id="a", track_id="v1",
        timeline_start=0, timeline_duration=72,
        source_start=0, source_duration=120, speed=speed,
        _source_fps_num=30, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1,
    )
    seq = _seq(clip)
    mapped = [
        seq.get_clip_by_id("c").timeline_to_source_frame(f) for f in range(72)
    ]
    assert max(mapped) >= 117  # 源尾 [117..119]
    assert mapped == sorted(mapped)  # 单调不回退


# ------------------------------------------------------------------ R06-2

def test_smooth_ramp_duration_uses_curve_integration() -> None:
    """K06 复现2：base1 + offset60→2x smooth，source120，24/24——
    权威积分 102（旧 constant 段冒充 90，错裁曲线）。"""
    speed = SpeedParams(
        speed=1.0,
        speed_points=(
            SpeedPoint(source_offset=60, speed=2.0, easing="smooth"),
        ),
    )
    no_fps = speed.timeline_duration_for_source_duration(120)
    assert no_fps == 102  # 权威曲线积分

    got = timeline_duration_for_source(120, speed, 24, 1, 24, 1)
    assert got == 102  # 同 fps 不缩放，不得回退 90


def test_smooth_ramp_mixed_fps_scales_curve_total() -> None:
    """smooth 102 帧曲线 @24fps 源 → 30fps 序列 = 102×30/24 = 128。"""
    speed = SpeedParams(
        speed=1.0,
        speed_points=(
            SpeedPoint(source_offset=60, speed=2.0, easing="smooth"),
        ),
    )
    got = timeline_duration_for_source(120, speed, 24, 1, 30, 1)
    assert got == 128


# ------------------------------------------------------------------ R11

def test_inverse_mapping_accounts_for_fps() -> None:
    """K11：source[100,200)，30fps 源/24fps 序列——source 160 → timeline
    48（旧 60）；正反映射可逆。"""
    clip = ClipInstance(
        clip_id="c", asset_id="a", track_id="v1",
        timeline_start=0, timeline_duration=80,
        source_start=100, source_duration=100,
        _source_fps_num=30, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1,
    )
    seq = _seq(clip)
    assert seq.get_clip_by_id("c").source_to_timeline_frame(160) == 48
    # 正映射回到同一源帧（可逆）
    assert seq.get_clip_by_id("c").timeline_to_source_frame(48) == 160


def test_inverse_mapping_legacy_no_fps_unchanged() -> None:
    clip = ClipInstance(
        clip_id="c", asset_id="a", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100, speed=2.0,
    )
    seq = _seq(clip)
    # legacy：无 fps 元数据，source 60 → timeline 30
    assert seq.get_clip_by_id("c").source_to_timeline_frame(60) == 30


def test_inverse_mapping_constant_with_fps() -> None:
    clip = ClipInstance(
        clip_id="c", asset_id="a", track_id="v1",
        timeline_start=0, timeline_duration=80,
        source_start=100, source_duration=100, speed=2.0,
        _source_fps_num=24, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1,
    )
    seq = _seq(clip)
    c = seq.get_clip_by_id("c")
    # 2x：source 160（rel 60）→ timeline 30
    assert c.source_to_timeline_frame(160) == 30
