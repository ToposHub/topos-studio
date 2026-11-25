"""Exercise Ripple drag geometry, linked offsets, snapping and Auto Select."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest

from src.features.edit.core.timeline_v2 import (
    ClipInstance, EditTrack, Rational, TimelineMarker, TimelineSequence,
    TransitionInstance,
)
from src.features.edit.ui.timeline_items import ClipItem, EditTool
from src.features.edit.ui.timeline_view import TimelineView

pytestmark = pytest.mark.edit_ui


def _allow_source_handles(view: TimelineView) -> None:
    for item in view._items_map.values():
        if isinstance(item, ClipItem):
            item._max_source_frames = 1000


def _view(qtbot: Any) -> TimelineView:
    visual = ClipInstance(
        "video", "video-source", "v1", 100, 100, 100, 200,
        linked_clip_id="audio", _source_fps_num=50, _source_fps_den=1,
        _timeline_fps_num=25, _timeline_fps_den=1,
    )
    audio = ClipInstance(
        "audio", "audio-source", "a1", 104, 100, 100, 100,
        linked_clip_id="video", _source_fps_num=25, _source_fps_den=1,
        _timeline_fps_num=25, _timeline_fps_den=1,
    )
    v1 = EditTrack("v1", "V1", "video", 0, clips=(
        ClipInstance("previous", "other", "v1", 0, 75, 0, 75),
        visual,
        ClipInstance("video-next", "other", "v1", 200, 80, 0, 80),
    ))
    v2 = EditTrack("v2", "V2", "video", 1, is_sync_locked=False, clips=(
        ClipInstance("b-roll", "other", "v2", 200, 80, 0, 80),
    ))
    a1 = EditTrack("a1", "A1", "audio", 0, clips=(
        audio,
        ClipInstance("audio-next", "other", "a1", 204, 80, 0, 80),
    ))
    a2 = EditTrack("a2", "A2", "audio", 1, clips=(
        ClipInstance("music", "other", "a2", 200, 80, 0, 80),
    ))
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1400, 700)
    view.show()
    view.load_timeline(TimelineSequence(
        "ripple", "Ripple", fps=Rational(25, 1),
        video_tracks=(v1, v2), audio_tracks=(a1, a2),
        markers=(TimelineMarker("landmark", 225),),
    ))
    view.set_zoom(3)
    view.set_snap_enabled(False)
    view.set_tool(EditTool.TRIM)
    _allow_source_handles(view)
    QTest.qWait(5)
    return view


def _drag(view: TimelineView, side: str, delta: int) -> QPoint:
    item = view._items_map["video"]
    x = 5 if side == "left" else item._width - 5
    start = view.mapFromScene(item.mapToScene(QPointF(x, 30)))
    stop = start + QPoint(delta * 3, 0)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), stop, 10)
    return stop


def _visual_state(view: TimelineView) -> dict[str, tuple[int, int, int, int]]:
    clips = {clip_id: item for clip_id, item in view._items_map.items()
             if isinstance(item, ClipItem)}
    for item in clips.values():
        assert item.x() == pytest.approx(view._layout_engine.frame_to_x(item._start_frame))
    return {
        clip_id: (item._start_frame, item._duration_frames, item._source_start, item._source_duration)
        for clip_id, item in clips.items()
    }


def _model_state(sequence: TimelineSequence) -> dict[str, tuple[int, int, int, int]]:
    return {
        clip.clip_id: (clip.timeline_start, clip.timeline_duration, clip.source_start, clip.source_duration)
        for track in sequence.all_tracks for clip in track.clips
    }


@pytest.mark.parametrize(
    "side,delta,primary,companion,follower_delta",
    [
        ("right", 10, (100, 110, 100, 220), (104, 110, 100, 110), 10),
        ("right", -10, (100, 90, 100, 180), (104, 90, 100, 90), -10),
        ("left", 10, (100, 90, 120, 180), (104, 90, 110, 90), -10),
        ("left", -10, (90, 110, 80, 220), (94, 110, 90, 110), 0),
    ],
)
def test_ripple_applies_relative_link_and_previews_every_follower(
    qtbot: Any, side: str, delta: int, primary: tuple[int, ...],
    companion: tuple[int, ...], follower_delta: int,
) -> None:
    view = _view(qtbot)
    before = view.get_timeline()
    stop = _drag(view, side, delta)
    preview = _visual_state(view)
    assert view.get_timeline() is before
    assert preview["video"] == primary
    assert preview["audio"] == companion
    assert preview["video-next"][0] == 200 + follower_delta
    assert preview["audio-next"][0] == 204 + follower_delta
    assert preview["music"][0] == 200 + follower_delta
    assert preview["b-roll"][0] == 200

    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=stop)
    assert _model_state(view.get_timeline()) == preview
    assert _visual_state(view) == preview
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view.get_timeline().to_dict() == replace(
        before, revision=view.get_timeline().revision
    ).to_dict()
    assert _visual_state(view) == _model_state(before)


def test_ripple_snap_toggles_in_place_and_commits_displayed_edge(qtbot: Any) -> None:
    view = _view(qtbot)
    view.set_snap_enabled(True)
    stop = _drag(view, "right", 23)
    assert _visual_state(view)["video"][1] == 125
    QTest.keyClick(view, Qt.Key.Key_N)
    assert _visual_state(view)["video"][1] == 123
    QTest.keyClick(view, Qt.Key.Key_N)
    preview = _visual_state(view)
    assert preview["video"][1] == 125
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=stop)
    assert _model_state(view.get_timeline()) == preview


def test_ripple_clamps_to_linked_source_with_tighter_headroom(qtbot: Any) -> None:
    view = _view(qtbot)
    view._items_map["video"]._max_source_frames = 312
    view._items_map["audio"]._max_source_frames = 204
    stop = _drag(view, "right", 30)
    preview = _visual_state(view)
    assert preview["video"] == (100, 104, 100, 208)
    assert preview["audio"] == (104, 104, 100, 104)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=stop)
    assert _model_state(view.get_timeline()) == preview


@pytest.mark.parametrize("cancel", ["escape", "link"])
def test_ripple_cancel_restores_cross_track_preview_and_ignores_release(
    qtbot: Any, cancel: str,
) -> None:
    view = _view(qtbot)
    before = view.get_timeline()
    stop = _drag(view, "right", 15)
    assert _visual_state(view)["music"][0] == 215
    if cancel == "escape":
        QTest.keyClick(view, Qt.Key.Key_Escape)
    else:
        view.set_linked_selection_enabled(False)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=stop)
    assert view.get_timeline() is before
    assert _visual_state(view) == _model_state(before)
    assert not view._command_stack.can_undo()


@pytest.mark.parametrize(
    "side,delta,has_next,expected_delta,expected_audio_end",
    [
        ("left", 5, False, 5, 155),
        ("left", 70, False, 59, 101),
        ("right", -70, False, -59, 101),
        ("left", 55, True, 35, 125),
        ("right", 70, True, 39, 199),
    ],
)
def test_ripple_keeps_a_linked_audio_crossfade_inside_valid_geometry(
    qtbot: Any, side: str, delta: int, has_next: bool,
    expected_delta: int, expected_audio_end: int,
) -> None:
    view = _view(qtbot)
    timeline = view.get_timeline()
    audio = replace(timeline.get_clip_by_id("audio"),
                    timeline_start=80, timeline_duration=80, source_duration=80)
    outgoing = ClipInstance("audio-prev", "other", "a1", 0, 100, 0, 100)
    outgoing_next = ClipInstance("audio-cross-next", "other", "a1", 135, 65, 0, 65)
    clips = (outgoing, audio, outgoing_next) if has_next else (outgoing, audio)
    lane = replace(timeline.audio_tracks[0], clips=clips)
    view.load_timeline(replace(timeline, audio_tracks=(lane, timeline.audio_tracks[1])))
    _allow_source_handles(view)
    original = view.get_timeline()

    stop = _drag(view, side, delta)
    preview = _visual_state(view)
    assert preview["audio"][:2] == (80, expected_audio_end - 80)
    assert preview["video"][1] == 100 + (expected_delta if side == "right" else -expected_delta)
    if has_next:
        assert preview["audio-cross-next"][0] == (100 if side == "left" else 135)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=stop)
    assert _model_state(view.get_timeline()) == preview
    updated_lane = view.get_timeline().audio_tracks[0]
    assert len(updated_lane.transitions) == (2 if has_next else 1)
    assert all(transition.is_overlap_crossfade for transition in updated_lane.transitions)
    view.undo()
    assert view.get_timeline().audio_tracks[0] == original.audio_tracks[0]
    assert _visual_state(view) == _model_state(original)


@pytest.mark.parametrize("stationary_marker", [False, True])
def test_ripple_does_not_snap_to_auto_select_moving_clip_marker_or_transition(
    qtbot: Any, stationary_marker: bool,
) -> None:
    view = _view(qtbot)
    timeline = view.get_timeline()
    music_lane = replace(timeline.audio_tracks[1], transitions=(TransitionInstance(
        transition_id="music-transition", transition_type="audio:equal_power",
        track_id="a2", right_clip_id="music", center_frame=200,
        duration=4, alignment="start_on_cut",
    ),))
    markers = (TimelineMarker("moving-marker", 202, clip_id="music", track_id="a2"),)
    if stationary_marker:
        markers += (TimelineMarker("stationary-marker", 203),)
    view.load_timeline(replace(timeline, video_tracks=(timeline.video_tracks[0],),
                               audio_tracks=(timeline.audio_tracks[0], music_lane),
                               markers=markers))
    _allow_source_handles(view)
    view.set_snap_enabled(True)
    stop = _drag(view, "right", 1)
    preview = _visual_state(view)
    expected_delta = 3 if stationary_marker else 1
    assert preview["video"][1] == 100 + expected_delta
    assert preview["music"][0] == 200 + expected_delta
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=stop)
    assert _model_state(view.get_timeline()) == preview


def test_ripple_snap_keeps_a_gap_clamped_follower_as_stationary(qtbot: Any) -> None:
    view = _view(qtbot)
    timeline = view.get_timeline()
    stationary = ClipInstance("music-prev", "other", "a2", 110, 90, 0, 90)
    music_lane = replace(timeline.audio_tracks[1], clips=(stationary, *timeline.audio_tracks[1].clips))
    view.load_timeline(replace(timeline, video_tracks=(timeline.video_tracks[0],),
                               audio_tracks=(timeline.audio_tracks[0], music_lane),
                               markers=()))
    view.set_snap_enabled(True)
    before = view.get_timeline()
    stop = _drag(view, "right", -1)
    assert _visual_state(view) == _model_state(before)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=stop)
    assert view.get_timeline() is before
    assert not view._command_stack.can_undo()


def test_left_ripple_checks_auto_select_at_the_candidate_cut(qtbot: Any) -> None:
    view = _view(qtbot)
    timeline = view.get_timeline()
    a2 = timeline.audio_tracks[1]
    music = replace(a2.clips[0], timeline_start=101)
    view.load_timeline(replace(timeline,
        audio_tracks=(timeline.audio_tracks[0], replace(a2, clips=(music,))), markers=(),
    ))
    view.set_snap_enabled(True)
    stop = _drag(view, "left", 2)
    preview = _visual_state(view)
    # At frame 101 the clip itself would move. At the requested cut 102
    # it stays still, so snapping must not pull the cut back to its edge.
    assert preview["video"][1] == 98
    assert preview["music"][0] == 101
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=stop)
    assert _model_state(view.get_timeline()) == preview
