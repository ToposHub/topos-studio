"""SUB-P1-05 contract: subtitle drag preview coalescing (frame budget).

The bug: handle-drag mouse-move events fired up to ~1000/s on high-DPI mice,
each rebuilding the overlay + re-injecting the clip into the viewer
synchronously, competing with playback/seek on the UI thread.

The fix: ``on_subtitle_interaction_updated`` still applies each delta to the
timeline (so the next delta is correct) but defers the expensive overlay
refresh + viewer re-injection to a coalesced ~16 ms (60 Hz) single-shot
QTimer. These tests verify the coalescing contract directly.
"""

from __future__ import annotations

from typing import List

import pytest

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.ui.subtitle_handle_controller import SubtitleHandleController

pytestmark = pytest.mark.edit_core


class _FakeViewer:
    def __init__(self) -> None:
        self.set_selected_calls: List[object] = []

    def set_selected_subtitle_clip(self, clip, style=None) -> None:
        self.set_selected_calls.append(clip)


class _FakeSubtitlePropertiesPanel:
    """Track whether a repeated timeline press rebuilds subtitle controls."""

    def __init__(self) -> None:
        self._current_subtitle_clip = None
        self.set_calls: List[object] = []

    def set_subtitle_clip(self, clip, _fps: float) -> None:
        self.set_calls.append(clip)
        self._current_subtitle_clip = clip


class _FakeEditPage:
    """Minimal stand-in exposing the attributes the controller touches."""

    def __init__(self, timeline) -> None:
        self._current_timeline = timeline
        self._program_viewer = _FakeViewer()
        self._current_frame = 0
        self.overlay_calls = 0

    def _update_subtitle_overlays(self, frame: int) -> None:
        self.overlay_calls += 1


def _make_controller(seq) -> EditControllerMixin:
    """Build a bare EditControllerMixin instance with a fake edit_page."""
    ctrl = EditControllerMixin.__new__(EditControllerMixin)
    ctrl.edit_page = _FakeEditPage(seq)
    ctrl._subtitle_transform_session = None
    ctrl._subtitle_drag_dirty_clip_id = None
    return ctrl


def _seq_with_subtitle() -> "object":
    from src.features.edit.core.timeline_v2 import (
        EditTrack, SubtitleClip, TimelineSequence,
    )
    sub_track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id="sub-1", track_id="s1",
                timeline_start=0, timeline_duration=72, text="cue",
            ),
        ),
    )
    return TimelineSequence(
        timeline_id="seq-1", name="Seq", width=1920, height=1080, fps=24,
        subtitle_tracks=(sub_track,),
    )


def test_update_does_not_refresh_overlay_synchronously(qapp) -> None:
    """Each delta applies to the timeline, but the overlay is NOT refreshed
    until the coalescing timer fires."""
    seq = _seq_with_subtitle()
    ctrl = _make_controller(seq)

    ctrl.on_subtitle_interaction_updated("sub-1", {"translate_x": 5.0})

    # Timeline was updated...
    assert ctrl.edit_page._current_timeline.get_subtitle_clip_by_id("sub-1").translate_x == 5.0
    # ...but the overlay/viewer were not touched synchronously.
    assert ctrl.edit_page.overlay_calls == 0
    assert ctrl.edit_page._program_viewer.set_selected_calls == []
    # A dirty clip + pending timer exists.
    assert ctrl._subtitle_drag_dirty_clip_id == "sub-1"


def test_coalesced_flush_refreshes_overlay_once(qapp) -> None:
    """Many updates between timer ticks produce a single overlay refresh."""
    seq = _seq_with_subtitle()
    ctrl = _make_controller(seq)

    for x in (1.0, 2.0, 3.0, 4.0, 5.0):
        ctrl.on_subtitle_interaction_updated("sub-1", {"translate_x": x})

    # No synchronous refreshes during the burst.
    assert ctrl.edit_page.overlay_calls == 0

    # Fire the coalesced timer callback.
    ctrl._subtitle_drag_preview_timer.stop()
    ctrl._flush_subtitle_drag_preview()

    assert ctrl.edit_page.overlay_calls == 1
    assert len(ctrl.edit_page._program_viewer.set_selected_calls) == 1
    # Dirty id cleared after flush.
    assert ctrl._subtitle_drag_dirty_clip_id is None


def test_cancel_on_end_clears_pending_flush(qapp) -> None:
    """Pointer-up cancels the pending flush (the commit refreshes instead)."""
    seq = _seq_with_subtitle()
    ctrl = _make_controller(seq)

    ctrl.on_subtitle_interaction_updated("sub-1", {"translate_x": 9.0})
    assert ctrl._subtitle_drag_dirty_clip_id == "sub-1"

    ctrl._cancel_subtitle_drag_preview_flush()
    assert ctrl._subtitle_drag_dirty_clip_id is None
    assert not ctrl._subtitle_drag_preview_timer.isActive()


def test_fast_release_commits_session_delta_before_preview_flush(qapp) -> None:
    """A move followed immediately by release cannot lose the last delta."""
    seq = _seq_with_subtitle()
    ctrl = _make_controller(seq)
    committed: list[object] = []
    ctrl._execute_subtitle_command = lambda command: committed.append(command)

    ctrl.on_subtitle_interaction_started("sub-1")
    ctrl.on_subtitle_interaction_updated("sub-1", {"translate_x": 17.0})
    assert ctrl._subtitle_drag_preview_timer.isActive()

    # Mirrors a stale handle-controller clip: it reports no local final delta
    # because the 16 ms selected-clip refresh has not fired yet.
    ctrl.on_subtitle_interaction_ended("sub-1", {}, {"translate_x": 0.0})

    assert len(committed) == 1
    command = committed[0]
    assert command.updates == {"translate_x": 17.0}
    assert not ctrl._subtitle_drag_preview_timer.isActive()

    transient = ctrl.edit_page._current_timeline
    committed_timeline = command.apply(transient)
    assert committed_timeline.get_subtitle_clip_by_id("sub-1").translate_x == 17.0
    restored = command.undo(committed_timeline)
    assert restored.get_subtitle_clip_by_id("sub-1").translate_x == 0.0


def test_handle_controller_reports_release_even_when_selected_clip_is_stale() -> None:
    """The controller session, not a delayed selected clip, decides no-op."""
    seq = _seq_with_subtitle()
    stale_clip = seq.get_subtitle_clip_by_id("sub-1")
    handle = SubtitleHandleController(object())
    handle.set_clip(stale_clip)
    handle._active_handle = "move"
    handle._original_params = {
        "translate_x": 0.0,
        "translate_y": 0.0,
        "scale_x": 1.0,
        "scale_y": 1.0,
        "rotation": 0.0,
        "pivot_mode": "center",
        "pivot_x": 0.0,
        "pivot_y": 0.0,
    }
    ended: list[tuple[str, dict, dict]] = []
    handle.on_drag_ended = lambda clip_id, delta, original: ended.append(
        (clip_id, delta, original)
    )

    handle.end_drag()

    assert len(ended) == 1
    assert ended[0][0] == "sub-1"
    assert ended[0][1] == {}


def test_repeated_subtitle_click_does_not_rebuild_properties_during_drag() -> None:
    """Each drag press reselects its cue but must not tear down combo widgets."""
    seq = _seq_with_subtitle()
    ctrl = _make_controller(seq)
    panel = _FakeSubtitlePropertiesPanel()
    ctrl.edit_properties_panel = panel

    ctrl.on_subtitle_clip_clicked("sub-1", "s1")
    ctrl.on_subtitle_clip_clicked("sub-1", "s1")

    assert len(panel.set_calls) == 1


def test_drag_back_to_original_is_a_noop_even_with_stale_release_delta(qapp) -> None:
    """The transient clip snapshot wins over cached/intermediate deltas."""
    seq = _seq_with_subtitle()
    ctrl = _make_controller(seq)
    committed: list[object] = []
    ctrl._execute_subtitle_command = lambda command: committed.append(command)

    ctrl.on_subtitle_interaction_started("sub-1")
    ctrl.on_subtitle_interaction_updated("sub-1", {"translate_x": 10.0})
    ctrl.on_subtitle_interaction_updated("sub-1", {"translate_x": 0.0})
    ctrl.on_subtitle_interaction_ended(
        "sub-1", {"translate_x": 10.0}, {"translate_x": 0.0},
    )

    assert committed == []
    assert ctrl.edit_page._current_timeline.get_subtitle_clip_by_id(
        "sub-1"
    ).translate_x == 0.0
