"""P1-06: double-clicking a transition in the effects panel must apply it.

Contract (from the professional-gap audit):
- Double-click and drag-drop use the SAME command path
  (AddTransitionCommand / UpdateTransitionCommand via
  TimelineView.add_transition_at_cut_point / add_transition_at_clip_edge).
- Targeting order with a selection: the clip's out-cut, then its in-cut,
  then a one-sided edge transition for isolated clips.
- Without a selection: nearest cut to the playhead on an unlocked track of
  the matching kind.
- Failures (locked track, track-kind mismatch, no cut near the playhead,
  missing clip) return a human-readable reason instead of a silent no-op.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)
from src.features.edit.ui.edit_effects_panel import EditEffectsPanel
from src.features.edit.ui.timeline_view import TimelineView


def _clip(clip_id: str, track_id: str, start: int, duration: int = 100) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id="asset-1",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=duration,
    )


def _sequence(
    video_clips: tuple[ClipInstance, ...] = (),
    audio_clips: tuple[ClipInstance, ...] = (),
    lock_v1: bool = False,
) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="seq-transitions",
        name="Transitions",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(
            EditTrack(
                track_id="v1",
                name="V1",
                track_type="video",
                index=0,
                clips=video_clips,
                is_locked=lock_v1,
            ),
        ),
        audio_tracks=(
            EditTrack(
                track_id="a1",
                name="A1",
                track_type="audio",
                index=0,
                clips=audio_clips,
            ),
        ),
    )


def _view(qapp, sequence: TimelineSequence) -> TimelineView:
    view = TimelineView()
    view.load_timeline(sequence)
    return view


def _track_transitions(view: TimelineView, track_id: str = "v1"):
    track = view._timeline.get_track_by_id(track_id)
    assert track is not None
    return track.transitions


class TestApplyFromPanelWithSelection:
    def test_selected_clip_with_right_neighbor_adds_cut_transition(self, qapp):
        seq = _sequence(video_clips=(_clip("c1", "v1", 0), _clip("c2", "v1", 100)))
        view = _view(qapp, seq)

        reason = view.apply_transition_from_panel("cross_dissolve", "c1")

        assert reason == ""
        transitions = _track_transitions(view)
        assert len(transitions) == 1
        t = transitions[0]
        assert t.transition_type == "cross_dissolve"
        assert (t.left_clip_id, t.right_clip_id) == ("c1", "c2")
        assert t.center_frame == 100  # the cut
        assert t.alignment == "centered"

    def test_rightmost_clip_falls_back_to_in_cut(self, qapp):
        seq = _sequence(video_clips=(_clip("c1", "v1", 0), _clip("c2", "v1", 100)))
        view = _view(qapp, seq)

        reason = view.apply_transition_from_panel("dip_to_color", "c2")

        assert reason == ""
        transitions = _track_transitions(view)
        assert len(transitions) == 1
        assert (transitions[0].left_clip_id, transitions[0].right_clip_id) == ("c1", "c2")

    def test_isolated_clip_gets_one_sided_edge_transition(self, qapp):
        seq = _sequence(video_clips=(_clip("solo", "v1", 50),))
        view = _view(qapp, seq)

        reason = view.apply_transition_from_panel("cross_dissolve", "solo")

        assert reason == ""
        transitions = _track_transitions(view)
        assert len(transitions) == 1
        t = transitions[0]
        assert t.is_one_sided
        assert t.left_clip_id == "solo" and t.right_clip_id == ""
        assert t.alignment == "end_on_cut"  # tail edge

    def test_locked_track_returns_reason_and_adds_nothing(self, qapp):
        seq = _sequence(
            video_clips=(_clip("c1", "v1", 0), _clip("c2", "v1", 100)),
            lock_v1=True,
        )
        view = _view(qapp, seq)

        reason = view.apply_transition_from_panel("cross_dissolve", "c1")

        assert reason != ""
        assert "locked" in reason.lower()
        assert _track_transitions(view) == ()

    def test_audio_transition_on_video_clip_returns_reason(self, qapp):
        seq = _sequence(video_clips=(_clip("c1", "v1", 0), _clip("c2", "v1", 100)))
        view = _view(qapp, seq)

        reason = view.apply_transition_from_panel("audio:constant_power", "c1")

        assert reason != ""
        assert _track_transitions(view, "v1") == ()

    def test_audio_transition_on_audio_clip_succeeds(self, qapp):
        seq = _sequence(audio_clips=(_clip("a-c1", "a1", 0), _clip("a-c2", "a1", 100)))
        view = _view(qapp, seq)

        reason = view.apply_transition_from_panel("audio:constant_power", "a-c1")

        assert reason == ""
        transitions = _track_transitions(view, "a1")
        assert len(transitions) == 1
        assert transitions[0].transition_type == "audio:constant_power"

    def test_wipe_payload_splits_direction_param(self, qapp):
        seq = _sequence(video_clips=(_clip("c1", "v1", 0), _clip("c2", "v1", 100)))
        view = _view(qapp, seq)

        reason = view.apply_transition_from_panel("wipe:left", "c1")

        assert reason == ""
        t = _track_transitions(view)[0]
        assert t.transition_type == "wipe"
        assert t.params.get("direction") == "left"

    def test_missing_clip_id_returns_reason(self, qapp):
        seq = _sequence(video_clips=(_clip("c1", "v1", 0),))
        view = _view(qapp, seq)

        reason = view.apply_transition_from_panel("cross_dissolve", "ghost")

        assert reason != ""

    def test_result_is_undoable(self, qapp):
        seq = _sequence(video_clips=(_clip("c1", "v1", 0), _clip("c2", "v1", 100)))
        view = _view(qapp, seq)

        view.apply_transition_from_panel("cross_dissolve", "c1")
        assert len(_track_transitions(view)) == 1

        view.undo()
        assert _track_transitions(view) == ()

        view.redo()
        assert len(_track_transitions(view)) == 1


class TestApplyFromPanelPlayheadFallback:
    def test_no_selection_uses_nearest_cut_to_playhead(self, qapp):
        seq = _sequence(video_clips=(_clip("c1", "v1", 0), _clip("c2", "v1", 100)))
        view = _view(qapp, seq)

        reason = view.apply_transition_from_panel("cross_dissolve", "", playhead_frame=97)

        assert reason == ""
        transitions = _track_transitions(view)
        assert (transitions[0].left_clip_id, transitions[0].right_clip_id) == ("c1", "c2")

    def test_playhead_far_from_any_cut_returns_reason(self, qapp):
        seq = _sequence(video_clips=(_clip("c1", "v1", 0), _clip("c2", "v1", 100)))
        view = _view(qapp, seq)

        reason = view.apply_transition_from_panel("cross_dissolve", "", playhead_frame=55)

        assert reason != ""
        assert _track_transitions(view) == ()

    def test_empty_timeline_returns_reason(self, qapp):
        view = _view(qapp, _sequence())

        reason = view.apply_transition_from_panel("cross_dissolve", "", playhead_frame=0)

        assert reason != ""

    def test_no_timeline_returns_reason(self, qapp):
        view = TimelineView()

        reason = view.apply_transition_from_panel("cross_dissolve", "")

        assert reason != ""


class TestPanelDoubleClickSignal:
    def test_double_click_emits_payload_and_selected_clip(self, qapp):
        from PyQt6.QtWidgets import QTreeWidgetItem

        panel = EditEffectsPanel()
        panel.set_selected_clip("clip-abc")

        item = QTreeWidgetItem()
        item.setData(0, 0, "Cross Dissolve")
        item.setData(0, 0x0100, "transition")  # Qt.ItemDataRole.UserRole
        item.setData(0, 0x0101, "cross_dissolve")  # UserRole + 1

        emitted = []
        panel.transition_apply_requested.connect(
            lambda payload, clip_id: emitted.append((payload, clip_id))
        )

        panel._on_transition_double_clicked(item, 0)

        assert emitted == [("cross_dissolve", "clip-abc")]

    def test_double_click_without_selection_emits_empty_clip(self, qapp):
        from PyQt6.QtWidgets import QTreeWidgetItem

        panel = EditEffectsPanel()
        panel.set_selected_clip(None)

        item = QTreeWidgetItem()
        item.setData(0, 0x0100, "transition")
        item.setData(0, 0x0101, "wipe:left")

        emitted = []
        panel.transition_apply_requested.connect(
            lambda payload, clip_id: emitted.append((payload, clip_id))
        )

        panel._on_transition_double_clicked(item, 0)

        assert emitted == [("wipe:left", "")]

    def test_category_item_does_not_emit(self, qapp):
        from PyQt6.QtWidgets import QTreeWidgetItem

        panel = EditEffectsPanel()

        item = QTreeWidgetItem()
        item.setData(0, 0x0100, "category")

        emitted = []
        panel.transition_apply_requested.connect(
            lambda payload, clip_id: emitted.append((payload, clip_id))
        )

        panel._on_transition_double_clicked(item, 0)

        assert emitted == []

    def test_show_transition_feedback_sets_info_label(self, qapp):
        panel = EditEffectsPanel()
        panel.show_transition_feedback("Track 'V1' is locked")
        assert "locked" in panel.info_label.text()
