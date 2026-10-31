"""Real linked Slip/Slide gestures must preview and commit one A/V edit."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_items import EditTool
from src.features.edit.ui.timeline_view import TimelineView


def _sequence(locked: bool = False) -> TimelineSequence:
    tracks = []
    for prefix, kind, partner in (("v", "video", "a"), ("a", "audio", "v")):
        tracks.append(EditTrack(
            track_id=prefix, name=prefix, track_type=kind, index=0,
            is_locked=locked and prefix == "a",
            clips=tuple(ClipInstance(
                clip_id=f"{prefix}{i}", asset_id="missing", track_id=prefix,
                timeline_start=i*100, timeline_duration=100,
                source_start=100+i*200, source_duration=100,
                linked_clip_id=f"{partner}{i}",
            ) for i in range(3))))
    return TimelineSequence(timeline_id="t", name="AV", video_tracks=(tracks[0],), audio_tracks=(tracks[1],))


def _view(qtbot: Any, sequence: TimelineSequence, tool: EditTool) -> TimelineView:
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1400, 650)
    view.show()
    view.load_timeline(sequence)
    view.set_zoom(3)
    view.set_snap_enabled(False)
    view.set_tool(tool)
    for item in view._items_map.values():
        item._max_source_frames = 1000
    QTest.qWait(5)
    return view


def _drag(view: TimelineView, frames: int = 10, release: bool = True) -> tuple[QPoint, QPoint]:
    item = view._items_map["v1"]
    point = view.mapFromScene(item.mapToScene(QPointF(item.boundingRect().width()/2, item.boundingRect().height()/2)))
    target = point+QPoint(frames*3, 0)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(view.viewport(), target, 10)
    if release:
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=target)
    return point, target


@pytest.mark.parametrize("tool", [EditTool.SLIP, EditTool.SLIDE])
def test_real_linked_gesture_preview_commit_and_one_undo(qtbot: Any, tool: EditTool) -> None:
    original = _sequence()
    view = _view(qtbot, original, tool)
    _, target = _drag(view, release=False)
    if tool == EditTool.SLIP:
        assert view._items_map["v1"]._source_start == view._items_map["a1"]._source_start == 310
    else:
        assert view._items_map["v1"]._start_frame == view._items_map["a1"]._start_frame == 110
    preview = {cid: (item._start_frame, item._duration_frames, item._source_start, item._source_duration)
               for cid, item in view._items_map.items()}
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=target)
    for cid, values in preview.items():
        clip = view._timeline.get_clip_by_id(cid)
        assert (clip.timeline_start, clip.timeline_duration, clip.source_start, clip.source_duration) == values
        item = view._items_map[cid]
        assert (item._start_frame, item._duration_frames, item._source_start, item._source_duration) == values
    assert len(view._command_stack._undo_stack) == 1
    assert not view._preview_affected_item_ids
    view.undo()
    for track in original.all_tracks:
        for clip in track.clips:
            assert view._timeline.get_clip_by_id(clip.clip_id).to_dict() == clip.to_dict()


def _assert_projected(view: TimelineView) -> None:
    for cid, item in view._items_map.items():
        clip = view._timeline.get_clip_by_id(cid)
        assert (item._start_frame, item._duration_frames, item._source_start, item._source_duration) == (
            clip.timeline_start, clip.timeline_duration, clip.source_start, clip.source_duration)
    assert not view._preview_affected_item_ids


@pytest.mark.parametrize("tool", [EditTool.SLIP, EditTool.SLIDE])
@pytest.mark.parametrize("reason", ["escape", "tool", "linked", "focus"])
def test_cancel_restores_entire_preview_and_ignores_late_release(
    qtbot: Any, tool: EditTool, reason: str,
) -> None:
    sequence = _sequence()
    view = _view(qtbot, sequence, tool)
    _, target = _drag(view, release=False)
    if reason == "escape":
        QTest.keyClick(view, Qt.Key.Key_Escape)
    elif reason == "tool":
        view.set_tool(EditTool.SELECT)
    elif reason == "linked":
        view.set_linked_selection_enabled(False)
    else:
        from PyQt6.QtGui import QFocusEvent
        view.focusOutEvent(QFocusEvent(QFocusEvent.Type.FocusOut))
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=target)
    _assert_projected(view)
    assert not view._command_stack._undo_stack
    assert view._timeline.to_dict() == sequence.to_dict()


@pytest.mark.parametrize("tool", [EditTool.SLIP, EditTool.SLIDE])
@pytest.mark.parametrize("lock_during_drag", [False, True])
def test_partner_lock_rejects_group_without_qt_exception(
    qtbot: Any, tool: EditTool, lock_during_drag: bool,
) -> None:
    sequence = _sequence(locked=not lock_during_drag)
    view = _view(qtbot, sequence, tool)
    _, target = _drag(view, release=False)
    if lock_during_drag:
        audio = view._timeline.audio_tracks[0]
        view._timeline = view._timeline.with_track_updated(audio._rebuild(is_locked=True))
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=target)
    _assert_projected(view)
    assert not view._command_stack._undo_stack
    assert [c.to_dict() for t in view._timeline.all_tracks for c in t.clips] == [
        c.to_dict() for t in sequence.all_tracks for c in t.clips]


@pytest.mark.parametrize("tool", [EditTool.SLIP, EditTool.SLIDE])
def test_linked_disabled_edits_independently_even_with_locked_partner(qtbot: Any, tool: EditTool) -> None:
    original = _sequence(locked=True)
    view = _view(qtbot, original, tool)
    view.set_linked_selection_enabled(False)
    _drag(view)
    _assert_projected(view)
    assert len(view._command_stack._undo_stack) == 1
    assert view._timeline.audio_tracks[0].to_dict() == original.audio_tracks[0].to_dict()
    assert view._timeline.video_tracks[0].to_dict() != original.video_tracks[0].to_dict()


@pytest.mark.parametrize("tool", [EditTool.SLIP, EditTool.SLIDE])
def test_returning_to_origin_and_click_have_no_history(qtbot: Any, tool: EditTool) -> None:
    view = _view(qtbot, _sequence(), tool)
    point, _ = _drag(view, release=False)
    QTest.mouseMove(view.viewport(), point, 10)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    _assert_projected(view)
    assert not view._command_stack._undo_stack


@pytest.mark.parametrize("tool", [EditTool.SLIP, EditTool.SLIDE])
def test_mismatched_linked_boundaries_are_rejected(qtbot: Any, tool: EditTool) -> None:
    original = _sequence()
    audio = original.audio_tracks[0]
    original = original.with_track_updated(audio._rebuild(clips=tuple(
        replace(c, timeline_start=c.timeline_start+1, timeline_duration=c.timeline_duration-1)
        if c.clip_id == "a1" else c for c in audio.clips)))
    view = _view(qtbot, original, tool)
    _drag(view)
    _assert_projected(view)
    assert not view._command_stack._undo_stack


@pytest.mark.parametrize("tool", [EditTool.SLIP, EditTool.SLIDE])
@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("reverse", [False, True])
def test_common_limits_with_different_speeds_and_source_windows(
    qtbot: Any, tool: EditTool, direction: int, reverse: bool,
) -> None:
    from src.features.edit.core.clip_params import SpeedParams
    original = _sequence()
    for track in original.all_tracks:
        speed = 1.25 if track.track_id == "v" else 2.0
        original = original.with_track_updated(track._rebuild(clips=tuple(
            replace(c, source_start=250 if c.clip_id.endswith("1") else c.source_start,
                    source_duration=int(c.timeline_duration*speed),
                    speed=SpeedParams(speed=speed, reverse=reverse)) for c in track.clips)))
    if tool == EditTool.SLIP and direction < 0:
        audio = original.audio_tracks[0]
        original = original.with_track_updated(audio._rebuild(clips=tuple(
            replace(c, source_start=4) if c.clip_id == "a1" else c for c in audio.clips)))
    view = _view(qtbot, original, tool)
    if tool == EditTool.SLIP and direction > 0:
        limiting = view._items_map["a1"]
        limiting._max_source_frames = limiting._source_start+limiting._source_duration+4
    _, target = _drag(view, frames=direction*10, release=False)
    expected = {cid: (item._start_frame, item._duration_frames, item._source_start, item._source_duration)
                for cid, item in view._items_map.items()}
    if tool == EditTool.SLIP:
        assert view._items_map["v1"]._source_start-original.get_clip_by_id("v1").source_start == direction*4
        assert view._items_map["a1"]._source_start-original.get_clip_by_id("a1").source_start == direction*4
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=target)
    _assert_projected(view)
    for cid, values in expected.items():
        clip = view._timeline.get_clip_by_id(cid)
        assert (clip.timeline_start, clip.timeline_duration, clip.source_start, clip.source_duration) == values
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    for track in original.all_tracks:
        for clip in track.clips:
            assert view._timeline.get_clip_by_id(clip.clip_id).to_dict() == clip.to_dict()


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("direction", [-1, 1])
def test_slide_clamps_all_six_items_to_partner_source_limit(
    qtbot: Any, reverse: bool, direction: int,
) -> None:
    from src.features.edit.core.clip_params import SpeedParams
    original = _sequence()
    for track in original.all_tracks:
        speed = 1.25 if track.track_id == "v" else 2.0
        original = original.with_track_updated(track._rebuild(clips=tuple(
            replace(c, speed=SpeedParams(speed=speed, reverse=reverse),
                    source_duration=int(c.timeline_duration*speed),
                    source_start=8 if (track.track_id == "a" and (
                        (direction > 0 and reverse and c.clip_id == "a0")
                        or (direction < 0 and not reverse and c.clip_id == "a2"))) else c.source_start)
            for c in track.clips)))
    view = _view(qtbot, original, EditTool.SLIDE)
    limiting_id = "a0" if direction > 0 else "a2"
    limiting = view._items_map[limiting_id]
    if (direction > 0 and not reverse) or (direction < 0 and reverse):
        limiting._max_source_frames = limiting._source_start+limiting._source_duration+8
    _, target = _drag(view, frames=direction*10, release=False)
    assert view._items_map["v1"]._start_frame == view._items_map["a1"]._start_frame == 100+direction*4
    preview = {cid: (item._start_frame, item._duration_frames, item._source_start, item._source_duration)
               for cid, item in view._items_map.items()}
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=target)
    _assert_projected(view)
    for cid, values in preview.items():
        clip = view._timeline.get_clip_by_id(cid)
        assert (clip.timeline_start, clip.timeline_duration, clip.source_start, clip.source_duration) == values
    assert len(view._command_stack._undo_stack) == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_slip_keeps_retime_control_domain_phase_and_exact_source_offset(qtbot: Any, reverse: bool) -> None:
    from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
    original = _sequence()
    for track in original.all_tracks:
        original = original.with_track_updated(track._rebuild(clips=tuple(
            replace(c, speed=SpeedParams(speed=1.25, reverse=reverse,
                                        speed_points=(SpeedPoint(60, 2.0, "smooth"),),
                                        curve_offset=17, curve_length=300),
                    source_duration=150, _source_frame_phase_num=1, _source_frame_phase_den=3)
            for c in track.clips)))
    view = _view(qtbot, original, EditTool.SLIP)
    _drag(view)
    _assert_projected(view)
    for cid in ("v1", "a1"):
        before, after = original.get_clip_by_id(cid), view._timeline.get_clip_by_id(cid)
        assert after.speed == before.speed
        assert after._source_frame_phase_num == before._source_frame_phase_num
        assert after._source_frame_phase_den == before._source_frame_phase_den
        assert [after.timeline_to_source_frame(f)-before.timeline_to_source_frame(f)
                for f in range(before.timeline_start, before.timeline_end)] == [10]*100
    view.undo()
    assert view._timeline.get_clip_by_id("v1").to_dict() == original.get_clip_by_id("v1").to_dict()


def test_linked_slip_rejects_different_source_frame_rates(qtbot: Any) -> None:
    original = _sequence()
    audio = original.audio_tracks[0]
    original = original.with_track_updated(audio._rebuild(clips=tuple(
        replace(c, _source_fps_num=30, _source_fps_den=1) if c.clip_id == "a1" else c
        for c in audio.clips)))
    view = _view(qtbot, original, EditTool.SLIP)
    _drag(view)
    _assert_projected(view)
    assert not view._command_stack._undo_stack


def test_linked_slide_rejects_mismatched_neighbor_links(qtbot: Any) -> None:
    original = _sequence()
    audio = original.audio_tracks[0]
    original = original.with_track_updated(audio._rebuild(clips=tuple(
        replace(c, linked_clip_id=None) if c.clip_id == "a0" else c for c in audio.clips)))
    view = _view(qtbot, original, EditTool.SLIDE)
    _drag(view)
    _assert_projected(view)
    assert not view._command_stack._undo_stack
