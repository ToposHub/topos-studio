"""B11：Alt-drag 组复制共同落位——互链/多选成员不再逐片独立避让。

反例（真实 Qt 探针复现）：A/V 互链对 100..200，Alt 拖到 220；A1 已有
200..300 blocker。旧提交路径把视频副本放 220、音频副本独立避让到 300，
两副本仍互链却错位 80 帧。修复后整组共用共同位移（都落 300）或整次
拒绝；锁轨成员使整组拒绝；正常路径 delta 保持不变。
"""

from __future__ import annotations

from dataclasses import replace
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


def _asset(asset_id: str, *, frames: int = 1000) -> MediaAsset:
    return MediaAsset(
        asset_id=asset_id, path=f"/tmp/{asset_id}.mp4", media_type="video",
        display_name=asset_id,
        metadata={"fps": 24.0, "total_frames": frames},
    )


def _linked_pair_timeline(*, audio_blocker: bool = False,
                          locked_audio: bool = False) -> TimelineSequence:
    video = ClipInstance(
        clip_id="vc", asset_id="a", track_id="v1",
        timeline_start=100, timeline_duration=100,
        source_start=0, source_duration=100, linked_clip_id="ac")
    audio = ClipInstance(
        clip_id="ac", asset_id="a", track_id="a1",
        timeline_start=100, timeline_duration=100,
        source_start=0, source_duration=100, linked_clip_id="vc")
    audio_clips: tuple[ClipInstance, ...] = (audio,)
    if audio_blocker:
        audio_clips += (ClipInstance(
            clip_id="blocker", asset_id="a", track_id="a1",
            timeline_start=200, timeline_duration=100,
            source_start=0, source_duration=100),)
    return TimelineSequence(
        timeline_id="copy-b11", name="Copy", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(video,)),),
        audio_tracks=(EditTrack(
            "a1", "A1", "audio", 0, clips=audio_clips,
            is_locked=locked_audio),),
    )


def _view(qtbot: Any, timeline: TimelineSequence) -> Any:
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)
    view.set_project(EditProject("p", "P", assets={"a": _asset("a")}))
    return view


def _copy_session(timeline: TimelineSequence, *, copy: bool = True,
                  track_map: dict[str, str] | None = None) -> ClipDragSession:
    originals = {
        "vc": 100, "ac": 100,
    }
    current = dict(track_map or {})
    return ClipDragSession(
        primary_clip_id="vc",
        linked_clip_ids=["ac"],
        original_positions=originals,
        original_track_ids={"vc": "v1", "ac": "a1"},
        current_track_ids={"vc": current.get("vc", "v1"),
                           "ac": current.get("ac", "a1")},
        copy_mode=copy,
    )


def test_b11_group_moves_together_past_blocker(qtbot: Any) -> None:
    """反例：A1 有 blocker 时整组共同移动，互链副本同起点。"""
    view = _view(qtbot, _linked_pair_timeline(audio_blocker=True))
    view._current_drag_session = _copy_session(view.get_timeline())

    view._handle_copy_drag_finished("vc", "v1", 100, 220)  # delta=+120

    timeline = view.get_timeline()
    video_copy = next(
        c for c in timeline.video_tracks[0].clips if c.clip_id != "vc")
    audio_copy = next(
        c for c in timeline.audio_tracks[0].clips
        if c.clip_id not in ("ac", "blocker"))
    # 整组共同位移：音频需避开 blocker(200..300) → 共同落 300
    assert video_copy.timeline_start == 300
    assert audio_copy.timeline_start == 300
    assert video_copy.linked_clip_id == audio_copy.clip_id
    assert audio_copy.linked_clip_id == video_copy.clip_id
    # 单步 undo
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    restored = view.get_timeline()
    assert [c.clip_id for c in restored.video_tracks[0].clips] == ["vc"]
    assert {c.clip_id for c in restored.audio_tracks[0].clips} == {
        "ac", "blocker"}


def test_b11_group_copies_without_blocker_keep_delta(qtbot: Any) -> None:
    """无 blocker 的正常组复制保持共同 delta（回归保护）。"""
    view = _view(qtbot, _linked_pair_timeline())
    view._current_drag_session = _copy_session(view.get_timeline())

    view._handle_copy_drag_finished("vc", "v1", 100, 220)

    timeline = view.get_timeline()
    video_copy = next(
        c for c in timeline.video_tracks[0].clips if c.clip_id != "vc")
    audio_copy = next(
        c for c in timeline.audio_tracks[0].clips if c.clip_id != "ac")
    assert video_copy.timeline_start == 220
    assert audio_copy.timeline_start == 220
    assert video_copy.linked_clip_id == audio_copy.clip_id


def test_b11_locked_member_track_rejects_whole_group(qtbot: Any) -> None:
    """锁轨成员 → 整组拒绝、零写入（不再静默丢成员）。"""
    view = _view(qtbot, _linked_pair_timeline(locked_audio=True))
    before = view.get_timeline()
    view._current_drag_session = _copy_session(view.get_timeline())

    view._handle_copy_drag_finished("vc", "v1", 100, 220)

    assert view.get_timeline() is before
    assert not view._command_stack._undo_stack


def test_b11_cross_track_group_copy_common_delta(qtbot: Any) -> None:
    """跨轨组复制（V1→V2、A1→A2）保持共同 delta 与互链。"""
    timeline = _linked_pair_timeline()
    timeline = timeline.with_track_added(EditTrack(
        "v2", "V2", "video", 1))
    timeline = timeline.with_track_added(EditTrack(
        "a2", "A2", "audio", 1))
    view = _view(qtbot, timeline)
    view._current_drag_session = _copy_session(
        view.get_timeline(), track_map={"vc": "v2", "ac": "a2"})

    view._handle_copy_drag_finished("vc", "v1", 100, 150)  # delta=+50

    new_timeline = view.get_timeline()
    v2 = new_timeline.get_track_by_id("v2")
    a2 = new_timeline.get_track_by_id("a2")
    assert len(v2.clips) == 1 and v2.clips[0].timeline_start == 150
    assert len(a2.clips) == 1 and a2.clips[0].timeline_start == 150
    assert v2.clips[0].linked_clip_id == a2.clips[0].clip_id
    assert a2.clips[0].linked_clip_id == v2.clips[0].clip_id


def test_b11_multi_selection_same_track_no_batch_collision(qtbot: Any) -> None:
    """同轨多选复制保持相对几何，不互相避让。"""
    timeline = _linked_pair_timeline()
    second = ClipInstance(
        clip_id="vc2", asset_id="a", track_id="v1",
        timeline_start=250, timeline_duration=50,
        source_start=0, source_duration=50)
    timeline = timeline.with_track_updated(
        replace(timeline.video_tracks[0],
                clips=timeline.video_tracks[0].clips + (second,)))
    view = _view(qtbot, timeline)
    session = ClipDragSession(
        primary_clip_id="vc",
        selected_clip_ids=["vc2"],
        original_positions={"vc": 100, "vc2": 250},
        original_track_ids={"vc": "v1", "vc2": "v1"},
        current_track_ids={"vc": "v1", "vc2": "v1"},
        copy_mode=True,
    )
    view._current_drag_session = session

    # delta=+250：副本 350..450 / 500..550 不与原片重叠，也不互相重叠
    view._handle_copy_drag_finished("vc", "v1", 100, 350)

    v1 = view.get_timeline().video_tracks[0]
    starts = sorted(
        c.timeline_start for c in v1.clips
        if c.clip_id not in ("vc", "vc2"))
    # 保持相对几何：不互相避让、无共同位移（shift=0）
    assert starts == [350, 500]
