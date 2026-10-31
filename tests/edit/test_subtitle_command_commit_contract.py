"""SUB-P0-01 contract: subtitle commands commit through the unified path.

These tests verify the guarantee that adding / updating / deleting a subtitle
cue no longer mutates ``edit_page._current_timeline`` through a private bypass.
Instead every subtitle command must travel the same commit entry as regular
clip edits (``TimelineView.execute_command``), so that:

* the shared undo/redo stack receives the command,
* ``TimelineView._timeline`` and the emitted ``timeline_changed`` signal carry
  the same sequence revision,
* the timeline view rebuilds/updates its SubtitleClipItems from that revision.

The tests exercise ``TimelineView`` directly (the real commit surface) so they
stay headless and do not require a full MainWindow.
"""

from __future__ import annotations

from typing import List

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QKeyEvent

from src.features.edit.core.commands import (
    AddSubtitleClipCommand,
    DeleteSubtitleClipCommand,
    UpdateSubtitleClipCommand,
)
from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip, TimelineSequence
from src.features.edit.core.selection import SelectionItem, SelectionType, TimelineSelection
from src.features.edit.ui.subtitle_clip_item import SubtitleClipItem
from src.features.edit.ui.timeline_view import TimelineView

pytestmark = pytest.mark.edit_core


def _sequence_with_subtitle_track() -> TimelineSequence:
    subtitle_track = EditTrack(
        track_id="s1",
        name="S1",
        track_type="subtitle",
        index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id="sub-existing",
                track_id="s1",
                timeline_start=0,
                timeline_duration=72,
                text="existing cue",
            ),
        ),
    )
    return TimelineSequence(
        timeline_id="seq-1",
        name="Seq",
        width=1920,
        height=1080,
        fps=24,
        video_tracks=(),
        audio_tracks=(),
        subtitle_tracks=(subtitle_track,),
    )


def _sequence_with_two_subtitle_clips() -> TimelineSequence:
    track = EditTrack(
        track_id="s1",
        name="S1",
        track_type="subtitle",
        index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id="sub-1",
                track_id="s1",
                timeline_start=0,
                timeline_duration=10,
                text="one",
            ),
            SubtitleClip(
                clip_id="sub-2",
                track_id="s1",
                timeline_start=20,
                timeline_duration=10,
                text="two",
            ),
        ),
    )
    return TimelineSequence(
        timeline_id="seq-1",
        name="Seq",
        width=1920,
        height=1080,
        fps=24,
        video_tracks=(),
        audio_tracks=(),
        subtitle_tracks=(track,),
    )


class _TimelineChangedRecorder:
    """Capture every timeline emitted by TimelineView.timeline_changed."""

    def __init__(self, view: TimelineView) -> None:
        self._view = view
        self.emitted: List[TimelineSequence] = []
        view.timeline_changed.connect(self._record)

    def _record(self, timeline: TimelineSequence) -> None:
        self.emitted.append(timeline)


def test_add_subtitle_commit_updates_view_and_signal(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_subtitle_track())
    recorder = _TimelineChangedRecorder(view)

    command = AddSubtitleClipCommand.create(track_id="s1", timeline_start=100, text="new cue")
    view.execute_command(command)

    # A single revision is emitted and the view adopts it.
    assert len(recorder.emitted) == 1
    committed = recorder.emitted[0]
    assert view._timeline is committed

    # The command stack now owns exactly one undoable entry.
    assert view.can_undo() is True

    # The new cue is reflected in both the model and the rebuilt view item.
    new_clip = committed.get_subtitle_clip_by_id(command._created_clip_id)
    assert new_clip is not None
    item = view._items_map.get(new_clip.clip_id)
    assert isinstance(item, SubtitleClipItem)


def test_update_subtitle_commit_reflects_new_value_in_view(qapp) -> None:
    view = TimelineView()
    seq = _sequence_with_subtitle_track()
    view.load_timeline(seq)
    recorder = _TimelineChangedRecorder(view)

    command = UpdateSubtitleClipCommand.create(
        clip_id="sub-existing",
        updates={"text": "edited text", "timeline_start": 10},
        label="Edit cue",
    )
    view.execute_command(command)

    assert len(recorder.emitted) == 1
    committed = recorder.emitted[0]
    assert view._timeline is committed

    updated = committed.get_subtitle_clip_by_id("sub-existing")
    assert updated is not None
    assert updated.text == "edited text"
    assert updated.timeline_start == 10

    # The view item is rebuilt from the committed revision.
    item = view._items_map.get("sub-existing")
    assert isinstance(item, SubtitleClipItem)


def test_delete_subtitle_commit_removes_item_from_view(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_subtitle_track())
    assert isinstance(view._items_map.get("sub-existing"), SubtitleClipItem)

    recorder = _TimelineChangedRecorder(view)
    command = DeleteSubtitleClipCommand.create(clip_id="sub-existing")
    view.execute_command(command)

    assert len(recorder.emitted) == 1
    committed = recorder.emitted[0]
    assert view._timeline is committed
    assert committed.get_subtitle_clip_by_id("sub-existing") is None
    assert "sub-existing" not in view._items_map


def test_subtitle_undo_redo_round_trips_through_unified_stack(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_subtitle_track())

    view.execute_command(
        UpdateSubtitleClipCommand.create(
            clip_id="sub-existing",
            updates={"text": "after"},
            label="Edit text",
        )
    )
    assert view._timeline.get_subtitle_clip_by_id("sub-existing").text == "after"

    view.undo()
    assert view._timeline.get_subtitle_clip_by_id("sub-existing").text == "existing cue"

    view.redo()
    assert view._timeline.get_subtitle_clip_by_id("sub-existing").text == "after"

    # After redo the view item must still be a SubtitleClipItem tracking the cue.
    assert isinstance(view._items_map.get("sub-existing"), SubtitleClipItem)


def test_delete_key_removes_selected_subtitle_clip_and_undo_restores(qapp) -> None:
    """Delete in the timeline must dispatch subtitle IDs to subtitle commands."""
    view = TimelineView()
    view.load_timeline(_sequence_with_subtitle_track())
    view._on_subtitle_clip_clicked("sub-existing", "s1")

    view.keyPressEvent(QKeyEvent(
        QKeyEvent.Type.KeyPress,
        Qt.Key.Key_Delete,
        Qt.KeyboardModifier.NoModifier,
    ))

    assert view._timeline.get_subtitle_clip_by_id("sub-existing") is None
    assert "sub-existing" not in view._items_map

    view.undo()
    assert view._timeline.get_subtitle_clip_by_id("sub-existing") is not None
    assert isinstance(view._items_map.get("sub-existing"), SubtitleClipItem)


def test_subtitle_clip_item_is_focusable_for_delete_key(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_subtitle_track())
    item = view._items_map["sub-existing"]

    assert isinstance(item, SubtitleClipItem)
    assert item.flags() & item.GraphicsItemFlag.ItemIsFocusable


def test_subtitle_context_menu_emits_delete_action(monkeypatch, qapp) -> None:
    """The subtitle right-click menu exposes deletion through the normal signal."""
    class _Action:
        def setEnabled(self, enabled: bool) -> None:
            self.enabled = enabled

    class _Menu:
        def __init__(self, parent) -> None:
            self.action = None

        def addAction(self, _label: str):
            self.action = _Action()
            return self.action

        def exec(self, _screen_pos):
            return self.action

    monkeypatch.setattr("PyQt6.QtWidgets.QMenu", _Menu)

    view = TimelineView()
    view.load_timeline(_sequence_with_subtitle_track())
    requested = []
    view.subtitle_clip_delete.connect(
        lambda clip_id, track_id: requested.append((clip_id, track_id))
    )

    view._on_subtitle_clip_context_menu_requested(
        "sub-existing", "s1", QPoint(10, 20)
    )

    assert requested == [("sub-existing", "s1")]


def test_rubber_band_selection_includes_subtitle_items(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_subtitle_track())
    view._rubber_band = type("_RubberBand", (), {"hide": lambda self: None})()
    view._rubber_band_start_scene = QPointF(0, 0)
    view._rubber_band_active = True

    item = view._items_map["sub-existing"]
    end = item.sceneBoundingRect().bottomRight() + QPointF(10, 10)

    class _ReleaseEvent:
        def pos(self):
            return view.mapFromScene(end)

        def button(self):
            return Qt.MouseButton.LeftButton

        def modifiers(self):
            return Qt.KeyboardModifier.NoModifier

    view._finish_rubber_band_selection(_ReleaseEvent())

    assert view._selection.selected_clip_ids == ("sub-existing",)


def test_selected_subtitles_move_as_one_undoable_group(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_two_subtitle_clips())
    view.set_selection(TimelineSelection(
        items=(
            SelectionItem(SelectionType.CLIP, "sub-1", "s1", "seq-1"),
            SelectionItem(SelectionType.CLIP, "sub-2", "s1", "seq-1"),
        ),
        primary_item=SelectionItem(SelectionType.CLIP, "sub-1", "s1", "seq-1"),
    ))

    view._on_subtitle_clip_drag_preview("sub-1", "s1", 10, 10.0)
    assert view._items_map["sub-1"].start_frame == 10
    assert view._items_map["sub-2"].start_frame == 30

    view._commit_subtitle_group_drag(
        {"sub-1": 0, "sub-2": 20}, 10, "s1"
    )

    assert view._timeline.get_subtitle_clip_by_id("sub-1").timeline_start == 10
    assert view._timeline.get_subtitle_clip_by_id("sub-2").timeline_start == 30
    view.undo()
    assert view._timeline.get_subtitle_clip_by_id("sub-1").timeline_start == 0
    assert view._timeline.get_subtitle_clip_by_id("sub-2").timeline_start == 20


def test_copy_and_paste_selected_subtitles_preserves_relative_timing(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_two_subtitle_clips())
    view.set_selection(TimelineSelection(
        items=(
            SelectionItem(SelectionType.CLIP, "sub-1", "s1", "seq-1"),
            SelectionItem(SelectionType.CLIP, "sub-2", "s1", "seq-1"),
        ),
    ))

    view._copy_selected_clips()
    view._paste_clips_at_frame(100)

    starts = sorted(
        clip.timeline_start
        for clip in view._timeline.subtitle_tracks[0].subtitle_clips
    )
    assert starts == [0, 20, 100, 120]


def test_delete_key_removes_all_selected_subtitles(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_sequence_with_two_subtitle_clips())
    view.set_selection(TimelineSelection(
        items=(
            SelectionItem(SelectionType.CLIP, "sub-1", "s1", "seq-1"),
            SelectionItem(SelectionType.CLIP, "sub-2", "s1", "seq-1"),
        ),
    ))

    view._delete_selected_clips()

    assert view._timeline.get_subtitle_clip_by_id("sub-1") is None
    assert view._timeline.get_subtitle_clip_by_id("sub-2") is None
