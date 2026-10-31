"""SUB-P0-04 contract (gap close): live drag clamping during the drag.

Phase 0.4 first shipped only release-time restore. This test pins the missing
half: a drag proposal is clamped to the legal range IN REAL TIME via the
injected ``_apply_subtitle_drag_clamp`` callback, so a cue can never be shown
in an overlapping position mid-drag (not only snapped back after a failed
commit).
"""

from __future__ import annotations

import pytest

from src.features.edit.core.timeline_v2 import (
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)
from src.features.edit.ui.subtitle_clip_item import SubtitleClipItem
from src.features.edit.ui.timeline_view import TimelineView

pytestmark = pytest.mark.edit_core


def _two_cue_sequence() -> TimelineSequence:
    """cue-a [0,72), cue-b [72,144) on the same track."""
    track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id="cue-a", track_id="s1",
                timeline_start=0, timeline_duration=72, text="A",
            ),
            SubtitleClip(
                clip_id="cue-b", track_id="s1",
                timeline_start=72, timeline_duration=72, text="B",
            ),
        ),
    )
    return TimelineSequence(
        timeline_id="seq-1", name="Seq", width=1920, height=1080, fps=24,
        subtitle_tracks=(track,),
    )


def test_move_clamp_callback_is_injected(qapp) -> None:
    view = TimelineView()
    view.load_timeline(_two_cue_sequence())
    item = view._items_map.get("cue-a")
    assert isinstance(item, SubtitleClipItem)
    # The clamp callback must be wired during load.
    assert item._drag_clamp_callback is not None


def test_move_preview_is_not_clamped_to_legal_range(qapp) -> None:
    """A move previews at the pointer; release uses overwrite semantics."""
    view = TimelineView()
    view.load_timeline(_two_cue_sequence())

    # Proposal: move cue-a to start=30 (would overlap cue-b since [30,102)).
    start, dur = view._apply_subtitle_drag_clamp(
        track_id="s1", clip_id="cue-a", mode="move",
        orig_start=0, orig_dur=72, proposed_start=30, proposed_dur=72,
    )
    assert start == 30
    assert dur == 72


def test_trim_right_proposal_is_clamped(qapp) -> None:
    """cue-a right-trim cannot extend into cue-b [72,144)."""
    view = TimelineView()
    view.load_timeline(_two_cue_sequence())

    # Proposal: extend cue-a duration from 72 to 100 (end 100 > cue-b start 72).
    start, dur = view._apply_subtitle_drag_clamp(
        track_id="s1", clip_id="cue-a", mode="trim_right",
        orig_start=0, orig_dur=72, proposed_start=0, proposed_dur=100,
    )
    # max_duration = right_neighbour_start (72) - current_start (0) = 72.
    assert dur == 72
    assert start == 0


def test_locked_track_move_returns_original(qapp) -> None:
    """A locked track forbids the edit → returns the original geometry."""
    seq = _two_cue_sequence()
    locked_track = seq.subtitle_tracks[0].with_visibility(True)
    # Build a sequence whose subtitle track is locked.
    locked = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        is_locked=True,
        subtitle_clips=seq.subtitle_tracks[0].subtitle_clips,
    )
    locked_seq = TimelineSequence(
        timeline_id="seq-1", name="Seq", width=1920, height=1080, fps=24,
        subtitle_tracks=(locked,),
    )
    view = TimelineView()
    view.load_timeline(locked_seq)

    start, dur = view._apply_subtitle_drag_clamp(
        track_id="s1", clip_id="cue-a", mode="move",
        orig_start=0, orig_dur=72, proposed_start=30, proposed_dur=72,
    )
    # Locked → proposal rejected, original returned unchanged.
    assert start == 0
    assert dur == 72


def test_move_preview_ignores_live_neighbours(qapp) -> None:
    """Overwrite moves may enter an occupied span before release."""
    view = TimelineView()
    view.load_timeline(_two_cue_sequence())

    # cue-a may preview over cue-b; the command resolves it by overwrite.
    s1, _ = view._apply_subtitle_drag_clamp(
        "s1", "cue-a", "move", 0, 72, 30, 72)
    assert s1 == 30

    # Delete cue-b so cue-a is now free to move forward.
    from src.features.edit.core.commands import DeleteSubtitleClipCommand
    view.execute_command(DeleteSubtitleClipCommand.create(clip_id="cue-b"))

    # Removing cue-b does not affect the allowed pointer position.
    s2, _ = view._apply_subtitle_drag_clamp(
        "s1", "cue-a", "move", 0, 72, 30, 72)
    assert s2 == 30
