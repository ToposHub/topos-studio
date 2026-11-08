"""R06：组复制同时自动创建 V/A 双轨。

反例（LINKED_COPY_TWO_AUTO_TRACKS，基线已有）：互链 A/V 对 Alt 拖到
新增轨区（视频→``__new_video_track_above__``、音频→
``__new_audio_track_below__``），planner 两副本都 220 帧可行，提交却
把所有副本装进**单一** ``new_track_clips`` 列表、伪轨 ID 取最后一个
（audio）→ 两条 220..320 的副本挤进同一条新音频轨 → EditTrack 重叠
拒绝 → 静默失败无诊断。

修复：按目标伪轨 ID 分桶；同一宏为每个伪轨建一条 CopyClipsToNewTrack
Command（V/A 各一条）；类型不匹配整次拒绝并诊断；单步 undo 两轨
与副本一起恢复。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)
from src.features.edit.ui.timeline_interaction import ClipDragSession

pytestmark = pytest.mark.edit_ui


def _asset() -> MediaAsset:
    return MediaAsset(
        asset_id="a", path="/tmp/a.mp4", media_type="video",
        display_name="a", metadata={"fps": 24.0, "total_frames": 1000},
    )


def _pair_timeline(*, locked_audio: bool = False) -> TimelineSequence:
    video = ClipInstance(
        clip_id="vc", asset_id="a", track_id="v1",
        timeline_start=100, timeline_duration=100,
        source_start=0, source_duration=100, linked_clip_id="ac")
    audio = ClipInstance(
        clip_id="ac", asset_id="a", track_id="a1",
        timeline_start=100, timeline_duration=100,
        source_start=0, source_duration=100, linked_clip_id="vc")
    return TimelineSequence(
        timeline_id="copy-r06", name="Copy", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(video,)),),
        audio_tracks=(EditTrack(
            "a1", "A1", "audio", 0, clips=(audio,),
            is_locked=locked_audio),),
    )


def _view(qtbot: Any, timeline: TimelineSequence) -> Any:
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)
    view.set_project(EditProject("p", "P", assets={"a": _asset()}))
    return view


def _copy_session(track_map: dict[str, str]) -> ClipDragSession:
    return ClipDragSession(
        primary_clip_id="vc",
        linked_clip_ids=["ac"],
        original_positions={"vc": 100, "ac": 100},
        original_track_ids={"vc": "v1", "ac": "a1"},
        current_track_ids={
            "vc": track_map.get("vc", "v1"),
            "ac": track_map.get("ac", "a1"),
        },
        copy_mode=True,
    )


def test_r06_two_auto_tracks_created_bucketed(qtbot: Any) -> None:
    """1V/1A/2clip → 2V/2A/4clip，history 1，A/V 副本同帧互链。"""
    view = _view(qtbot, _pair_timeline())
    view._current_drag_session = _copy_session({
        "vc": "__new_video_track_above__",
        "ac": "__new_audio_track_below__",
    })

    view._handle_copy_drag_finished("vc", "v1", 100, 220)  # delta=+120

    timeline = view.get_timeline()
    assert len(timeline.video_tracks) == 2
    assert len(timeline.audio_tracks) == 2
    new_v = timeline.video_tracks[1]
    new_a = timeline.audio_tracks[1]
    assert new_v.track_type == "video"
    assert new_a.track_type == "audio"
    assert len(new_v.clips) == 1 and len(new_a.clips) == 1
    v_copy, a_copy = new_v.clips[0], new_a.clips[0]
    # 同帧 + 互链
    assert v_copy.timeline_start == 220
    assert a_copy.timeline_start == 220
    assert v_copy.linked_clip_id == a_copy.clip_id
    assert a_copy.linked_clip_id == v_copy.clip_id
    # 原件未动
    assert timeline.get_clip_by_id("vc").timeline_start == 100
    assert timeline.get_clip_by_id("ac").timeline_start == 100
    # 单步 undo：两轨与副本一起恢复
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    restored = view.get_timeline()
    assert len(restored.video_tracks) == 1
    assert len(restored.audio_tracks) == 1
    assert [c.clip_id for c in restored.video_tracks[0].clips] == ["vc"]
    assert [c.clip_id for c in restored.audio_tracks[0].clips] == ["ac"]


def test_r06_single_auto_track_regression(qtbot: Any) -> None:
    """仅一个伪轨目标（两视频多选）仍是一条新轨 + 副本不重叠。"""
    timeline = _pair_timeline()
    extra = ClipInstance(
        clip_id="vc2", asset_id="a", track_id="v1",
        timeline_start=300, timeline_duration=50,
        source_start=0, source_duration=50)
    timeline = timeline.with_track_updated(
        timeline.video_tracks[0]._rebuild(
            clips=timeline.video_tracks[0].clips + (extra,)))
    view = _view(qtbot, timeline)
    view._current_drag_session = ClipDragSession(
        primary_clip_id="vc",
        linked_clip_ids=["vc2"],
        original_positions={"vc": 100, "vc2": 300},
        original_track_ids={"vc": "v1", "vc2": "v1"},
        current_track_ids={"vc": "__new_video_track_above__",
                           "vc2": "__new_video_track_above__"},
        copy_mode=True,
    )
    view._handle_copy_drag_finished("vc", "v1", 100, 220)

    changed = view.get_timeline()
    assert len(changed.video_tracks) == 2
    new_v = changed.video_tracks[1]
    assert new_v.track_type == "video"
    assert len(new_v.clips) == 2
    starts = sorted(c.timeline_start for c in new_v.clips)
    assert starts == [220, 420]  # 共同 delta，保持相对几何


def test_r06_type_mismatch_whole_reject_with_status(qtbot: Any) -> None:
    """音频成员被规划到视频伪轨（几何不重叠、类型错）→ 整次拒绝 + 诊断。

    两成员错开（100..200 / 300..400）避开 planner 的同轨冲突检查，
    类型错必须在**提交前**被拒——不能把音频副本放进新建视频轨。
    """
    timeline = _pair_timeline()
    audio = timeline.get_clip_by_id("ac")
    from dataclasses import replace as _dc_replace
    timeline = timeline.with_track_updated(
        timeline.get_track_by_id("a1")._rebuild(
            clips=(_dc_replace(audio, timeline_start=300),)))
    # 会话的原始几何与改后时间线一致
    view = _view(qtbot, timeline)
    status: list[str] = []
    view.status_message_requested.connect(status.append)
    view._current_drag_session = ClipDragSession(
        primary_clip_id="vc",
        linked_clip_ids=["ac"],
        original_positions={"vc": 100, "ac": 300},
        original_track_ids={"vc": "v1", "ac": "a1"},
        current_track_ids={"vc": "__new_video_track_above__",
                           "ac": "__new_video_track_above__"},
        copy_mode=True,
    )
    view._handle_copy_drag_finished("vc", "v1", 100, 220)

    assert status, "rejection must emit a UI diagnostic"
    changed = view.get_timeline()
    assert len(changed.video_tracks) == 1
    assert len(changed.audio_tracks) == 1
    assert not view._command_stack._undo_stack


def test_r06_locked_real_track_rejects_whole_group(qtbot: Any) -> None:
    """混合目标（成员去锁定的真实轨）→ 整次拒绝，新轨不创建。"""
    view = _view(qtbot, _pair_timeline(locked_audio=True))
    status: list[str] = []
    view.status_message_requested.connect(status.append)
    view._current_drag_session = _copy_session({
        "vc": "__new_video_track_above__",
        "ac": "a1",  # 真实轨且被锁
    })
    view._handle_copy_drag_finished("vc", "v1", 100, 220)

    assert status
    timeline = view.get_timeline()
    assert len(timeline.video_tracks) == 1
    assert len(timeline.audio_tracks) == 1
    assert not view._command_stack._undo_stack
