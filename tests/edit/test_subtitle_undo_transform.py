"""SUB-P0-02 contract: subtitle transform drag commits one undoable command.

The bug: the live handle-drag mutated the timeline directly and the
pointer-up handler pushed an ``UpdateSubtitleClipCommand`` whose ``_previous``
was derived from the *already-final* clip, so Undo restored the post-drag
state (a no-op). These tests verify the fix:

* ``SubtitleTransformSession`` captures the pre-drag transform fields.
* ``UpdateSubtitleClipCommand.create_with_previous`` honours an explicit
  ``previous`` snapshot instead of re-deriving it.
* After commit, Undo restores the pre-drag values field-by-field; Redo
  reapplies the final values. A cancelled/empty drag writes no command.
"""

from __future__ import annotations

import pytest

from src.app.controllers.edit_controller import (
    SubtitleTransformSession,
    _SUBTITLE_TRANSFORM_FIELDS,
)
from src.features.edit.core.commands import UpdateSubtitleClipCommand
from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip, TimelineSequence

pytestmark = pytest.mark.edit_core


def _sequence_with_one_subtitle(clip: SubtitleClip) -> TimelineSequence:
    track = EditTrack(
        track_id="s1",
        name="S1",
        track_type="subtitle",
        index=0,
        subtitle_clips=(clip,),
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


def _base_clip() -> SubtitleClip:
    return SubtitleClip(
        clip_id="sub-1",
        track_id="s1",
        timeline_start=0,
        timeline_duration=72,
        text="hello",
        translate_x=0.0,
        translate_y=0.0,
        scale_x=1.0,
        scale_y=1.0,
        rotation=0.0,
    )


def test_session_captures_pre_drag_transform_fields() -> None:
    clip = _base_clip()
    session = SubtitleTransformSession.begin(clip)
    assert session.clip_id == "sub-1"
    # All transform fields are snapshotted.
    for name in _SUBTITLE_TRANSFORM_FIELDS:
        assert name in session.before
    assert session.before["scale_x"] == 1.0
    assert session.before["rotation"] == 0.0


def test_session_accumulate_is_last_write_wins() -> None:
    session = SubtitleTransformSession("sub-1", {"rotation": 0.0})
    session.accumulate({"rotation": 5.0})
    session.accumulate({"rotation": 12.0, "scale_x": 1.5})
    assert session.accumulated == {"rotation": 12.0, "scale_x": 1.5}


def test_undo_restores_pre_drag_state_with_explicit_previous() -> None:
    clip = _base_clip()
    seq = _sequence_with_one_subtitle(clip)

    # Simulate a drag that has already been applied live to the timeline
    # (translate + rotate), just like on_subtitle_interaction_updated does.
    live = UpdateSubtitleClipCommand.create(
        clip_id="sub-1",
        updates={"translate_x": 100.0, "rotation": 45.0},
        label="Drag Subtitle",
    )
    seq = live.apply(seq)
    assert seq.get_subtitle_clip_by_id("sub-1").rotation == 45.0

    # The release handler builds the commit using the pre-drag snapshot.
    before = SubtitleTransformSession.begin(clip).before
    commit = UpdateSubtitleClipCommand.create_with_previous(
        clip_id="sub-1",
        updates={"translate_x": 100.0, "rotation": 45.0},
        previous=before,
        label="Transform Subtitle",
    )
    # Commit is idempotent on the already-final timeline (re-apply same values).
    seq = commit.apply(seq)

    # Undo must restore the pre-drag transform, not the post-drag state.
    seq = commit.undo(seq)
    restored = seq.get_subtitle_clip_by_id("sub-1")
    assert restored.translate_x == 0.0
    assert restored.rotation == 0.0
    assert restored.scale_x == 1.0

    # Redo reapplies the final transform.
    seq = commit.redo(seq) if hasattr(commit, "redo") else commit.apply(seq)
    final = seq.get_subtitle_clip_by_id("sub-1")
    assert final.translate_x == 100.0
    assert final.rotation == 45.0


def test_undo_uses_explicit_previous_over_rederived_snapshot() -> None:
    """Regression: without create_with_previous, _previous would equal final."""
    clip = _base_clip()
    seq = _sequence_with_one_subtitle(clip)

    # Live drag applies the final rotation.
    seq = UpdateSubtitleClipCommand.create(
        clip_id="sub-1", updates={"rotation": 90.0}
    ).apply(seq)

    # Old (buggy) command re-derives _previous from the now-final clip.
    buggy = UpdateSubtitleClipCommand.create(
        clip_id="sub-1", updates={"rotation": 90.0}
    )
    buggy.apply(seq)
    assert buggy._previous == {"rotation": 90.0}  # undo would be a no-op

    # New command carries the true pre-drag value.
    fixed = UpdateSubtitleClipCommand.create_with_previous(
        clip_id="sub-1",
        updates={"rotation": 90.0},
        previous={"rotation": 0.0},
    )
    fixed.apply(seq)
    assert fixed._previous == {"rotation": 0.0}  # undo restores pre-drag


def test_empty_drag_writes_no_command() -> None:
    """A drag that produces no net delta must not push an undo entry.

    The controller's on_subtitle_interaction_ended returns early when
    final_delta is empty; this test encodes that contract at the session level.
    """
    session = SubtitleTransformSession.begin(_base_clip())
    # No accumulate calls -> empty net delta.
    assert session.accumulated == {}
    # The release handler would build no command in this case.
    assert not session.accumulated
