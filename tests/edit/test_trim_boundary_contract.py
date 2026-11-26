"""Source-frame mapping used by the timeline freeze action."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_ui


def _timeline(*clips: ClipInstance, fps: Rational = Rational(24, 1),
              ) -> TimelineSequence:
    return TimelineSequence(
        "trim-r07", "Trim", width=64, height=48, fps=fps,
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=clips),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )


def _view(qtbot: Any, timeline: TimelineSequence) -> Any:
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)
    return view


def _clip(**kwargs: Any) -> ClipInstance:
    base = dict(
        clip_id="c1", asset_id="asset", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
    )
    base.update(kwargs)
    return ClipInstance(**base)


def test_r07_authoritative_boundary_matches_export_mapping(qtbot):
    """恒速：offset k 与导出映射逐点一致（回归保护）。"""
    view = _view(qtbot, _timeline(_clip()))
    clip = view.get_timeline().get_clip_by_id("c1")
    for offset in (0, 1, 50, 99):
        assert view._authoritative_boundary(clip, offset) == \
            clip.timeline_to_source_frame(clip.timeline_start + offset)


def test_r07_authoritative_boundary_reverse_and_curve(qtbot):
    """reverse/速度点曲线：权威映射给真实显示帧（旧比例法退 None）。"""
    view = _view(qtbot, _timeline(
        _clip(speed=SpeedParams(speed=1.0, reverse=True))))
    reversed_clip = view.get_timeline().get_clip_by_id("c1")
    frame = view._authoritative_boundary(reversed_clip, 10)
    assert frame is not None
    assert frame == reversed_clip.timeline_to_source_frame(
        reversed_clip.timeline_start + 10)

    curved = _clip(speed=SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=30, speed=2.0),)))
    view.load_timeline(_timeline(curved))
    curved_clip = view.get_timeline().get_clip_by_id("c1")
    frame = view._authoritative_boundary(curved_clip, 40)
    assert frame is not None
    assert frame == curved_clip.timeline_to_source_frame(
        curved_clip.timeline_start + 40)


def test_r07_authoritative_boundary_freeze_and_mixed_fps(qtbot):
    frozen = _clip().with_freeze_frame(7)
    view = _view(qtbot, _timeline(frozen))
    frozen_clip = view.get_timeline().get_clip_by_id("c1")
    assert view._authoritative_boundary(frozen_clip, 42) == 7

    mixed = _clip(
        _source_fps_num=48, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1)
    view.load_timeline(_timeline(mixed))
    mixed_clip = view.get_timeline().get_clip_by_id("c1")
    # 24fps 时间线第 1 tick = 源 2 帧（48fps 源）
    assert view._authoritative_boundary(mixed_clip, 1) == 2


def test_r07_authoritative_boundary_beyond_committed_geometry(qtbot):
    """roll 拖拽查询越过提交几何（offset > duration）仍可映射。"""
    view = _view(qtbot, _timeline(_clip(source_duration=300)))
    clip = view.get_timeline().get_clip_by_id("c1")
    # duration=100、offset 130（roll 右移 31）：仍应给出权威帧
    frame = view._authoritative_boundary(clip, 130)
    assert frame == 130
