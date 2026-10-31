"""Rolling a linked cut must keep A/V edges aligned in preview and history."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest

from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.linked_roll import build_linked_roll_command
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_items import EditTool
from src.features.edit.ui.timeline_view import TimelineView


def _sequence(speed: float = 1.0, reverse: bool = False, locked: bool = False) -> TimelineSequence:
    clips = []
    for prefix, track, links in (("v", "video", "a"), ("a", "audio", "v")):
        clips.append(EditTrack(
            track_id=prefix, track_type=track, name=prefix, index=0,
            is_locked=locked and prefix == "a",
            clips=tuple(ClipInstance(
                clip_id=f"{prefix}{i+1}", asset_id="missing", track_id=prefix,
                timeline_start=i*100, timeline_duration=100,
                source_start=100+i*300, source_duration=int(100*speed),
                speed=SpeedParams(speed=speed, reverse=reverse),
                linked_clip_id=f"{links}{i+1}",
            ) for i in range(2)),
        ))
    return TimelineSequence(timeline_id="t", name="AV", fps=24,
                            video_tracks=(clips[0],), audio_tracks=(clips[1],))


def _view(qtbot: Any, sequence: TimelineSequence) -> TimelineView:
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1400, 650)
    view.show()
    view.load_timeline(sequence)
    view.set_zoom(3)
    view.set_snap_enabled(False)
    view.set_tool(EditTool.ROLL)
    for item in view._items_map.values():
        item._max_source_frames = 1000
    QTest.qWait(5)
    return view


def _drag(view: TimelineView, release: bool = True) -> None:
    handle = next(h for h in view._roll_handles.values() if h.left_clip_id == "v1")
    point = view.mapFromScene(handle.mapToScene(handle.boundingRect().center()))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(view.viewport(), point+QPoint(30, 0), 10)
    if release:
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point+QPoint(30, 0))


@pytest.mark.parametrize("speed,reverse", [(1.0, False), (2.0, False), (0.5, True)])
def test_linked_roll_preview_release_and_undo_share_all_four_clips(qtbot: Any, speed: float, reverse: bool) -> None:
    original = _sequence(speed, reverse)
    view = _view(qtbot, original)
    _drag(view, release=False)
    assert view._items_map['v1']._duration_frames == view._items_map['a1']._duration_frames == 110
    preview = {cid: (i._start_frame, i._duration_frames, i._source_start, i._source_duration)
               for cid, i in view._items_map.items()}
    handle = next(h for h in view._roll_handles.values() if h.left_clip_id == "v1")
    point = view.mapFromScene(handle.mapToScene(handle.boundingRect().center()))
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    for cid, values in preview.items():
        clip = view._timeline.get_clip_by_id(cid)
        assert (clip.timeline_start, clip.timeline_duration, clip.source_start, clip.source_duration) == values
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    for track in original.all_tracks:
        for clip in track.clips:
            assert view._timeline.get_clip_by_id(clip.clip_id).to_dict() == clip.to_dict()
    view.redo()
    assert view._timeline.get_clip_by_id('v1').timeline_end == view._timeline.get_clip_by_id('a1').timeline_end == 110


def test_linked_roll_escape_restores_every_visual_without_history(qtbot: Any) -> None:
    view = _view(qtbot, _sequence())
    _drag(view, release=False)
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert view._interaction.is_idle
    for cid, item in view._items_map.items():
        clip = view._timeline.get_clip_by_id(cid)
        assert (item._start_frame, item._duration_frames) == (clip.timeline_start, clip.timeline_duration)
    assert not view._command_stack.can_undo()


def test_locked_audio_rejects_video_roll_before_preview(qtbot: Any) -> None:
    view = _view(qtbot, _sequence(locked=True))
    _drag(view)
    assert view._timeline.revision == 0
    assert not view._command_stack.can_undo()
    assert view._roll_preview_state is None


def test_independent_roll_is_available_when_linked_selection_is_disabled(qtbot: Any) -> None:
    view = _view(qtbot, _sequence())
    view._linked_selection_enabled = False
    _drag(view)
    assert view._timeline.get_clip_by_id('v1').timeline_end == 110
    assert view._timeline.get_clip_by_id('a1').timeline_end == 100


def test_numeric_roll_uses_common_headroom_and_rejects_misaligned_links() -> None:
    sequence = _sequence()
    command, delta = build_linked_roll_command(
        sequence, 'v1', 'v2', 10,
        lambda clip: 205 if clip.clip_id == 'a1' else 1000)
    assert delta == 5
    result = command.apply(sequence)
    if isinstance(result, tuple):
        result = result[0]
    assert result.get_clip_by_id('v1').timeline_end == result.get_clip_by_id('a1').timeline_end == 105
    audio = sequence.audio_tracks[0]
    changed = sequence.with_track_updated(replace(audio, clips=(
        replace(audio.clips[0], timeline_duration=99), audio.clips[1])))
    with pytest.raises(ValueError, match="boundaries differ"):
        build_linked_roll_command(changed, 'v1', 'v2', 1, lambda clip: 1000)


@pytest.mark.parametrize("speed", [0.5, 1.0, 2.0])
def test_numeric_dialog_uses_actual_reverse_handles_without_preclamping(qtbot: Any, monkeypatch: Any, speed: float) -> None:
    from src.features.edit.ui.trim_numeric_dialog import TrimNumericDialog

    view = _view(qtbot, _sequence(speed=speed, reverse=True))
    monkeypatch.setattr(TrimNumericDialog, 'exec', lambda self: TrimNumericDialog.DialogCode.Accepted)
    monkeypatch.setattr(TrimNumericDialog, 'values', lambda self: ('out', 'roll', -10, False))
    view._on_clip_numeric_trim_requested('v1', 'v')
    assert view._timeline.get_clip_by_id('v1').timeline_end == 90
    assert view._timeline.get_clip_by_id('a1').timeline_end == 90
    assert len(view._command_stack._undo_stack) == 1
