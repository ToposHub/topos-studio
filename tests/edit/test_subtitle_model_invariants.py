"""SUB-P0-03 contract: subtitle model invariants & Update patch whitelist.

These tests enforce the data-integrity guarantees that previously were only
conventions:

* ``UpdateSubtitleClipCommand`` rejects identity fields (``clip_id``/
  ``track_id``); a cue can never be orphaned from its owner track by an edit.
* ``EditTrack`` rejects subtitle clips on non-subtitle tracks, clips whose
  ``track_id`` disagrees with the owner, and duplicate clip ids.
* ``TimelineSequence`` rejects duplicate subtitle clip ids across the whole
  sequence (the cue id is the global lookup key).

Every illegal construction is expected to fail fast with a ``ValueError``.
"""

from __future__ import annotations

import pytest

from src.features.edit.core.commands import (
    CommandStack,
    CopySubtitleClipCommand,
    MoveSubtitleClipCommand,
    UpdateSubtitleClipCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _sub(clip_id: str = "sub-1", track_id: str = "s1", start: int = 0,
         duration: int = 72) -> SubtitleClip:
    return SubtitleClip(
        clip_id=clip_id,
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        text="x",
    )


# ── Update patch whitelist ─────────────────────────────────────────────────


@pytest.mark.parametrize("forbidden", ["clip_id", "track_id"])
def test_update_rejects_identity_fields(forbidden: str) -> None:
    with pytest.raises(ValueError, match="identity fields"):
        UpdateSubtitleClipCommand.create(
            clip_id="sub-1",
            updates={forbidden: "something-else"},
        )


def test_update_create_with_previous_also_rejects_identity_fields() -> None:
    with pytest.raises(ValueError, match="identity fields"):
        UpdateSubtitleClipCommand.create_with_previous(
            clip_id="sub-1",
            updates={"track_id": "s2"},
            previous={"track_id": "s1"},
        )


def test_update_allows_timing_and_style_fields() -> None:
    # These are the legitimate edit surfaces: timing, text, transform, style.
    cmd = UpdateSubtitleClipCommand.create(
        clip_id="sub-1",
        updates={
            "timeline_start": 10,
            "timeline_duration": 100,
            "text": "edited",
            "translate_x": 5.0,
            "rotation": 12.0,
            "fill_r": 0.5,
            "is_enabled": False,
        },
    )
    assert set(cmd.updates) <= set(cmd.updates)  # constructed without error


def test_update_apply_propagates_invariant_failure() -> None:
    """A timing update that creates an overlap must fail at apply (model-level)."""
    track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(
            _sub("sub-1", "s1", start=0, duration=72),
            _sub("sub-2", "s1", start=72, duration=72),
        ),
    )
    seq = TimelineSequence(
        timeline_id="seq", name="S", width=1920, height=1080, fps=24,
        subtitle_tracks=(track,),
    )
    # Moving sub-2 to start=10 overlaps sub-1 [0,72).
    cmd = UpdateSubtitleClipCommand.create(
        clip_id="sub-2", updates={"timeline_start": 10}
    )
    with pytest.raises(ValueError):
        cmd.apply(seq)


# ── EditTrack invariants ───────────────────────────────────────────────────


def test_edit_track_rejects_subtitle_clips_on_video_track() -> None:
    with pytest.raises(ValueError, match="cannot carry subtitle_clips"):
        EditTrack(
            track_id="v1", name="V1", track_type="video", index=0,
            subtitle_clips=(_sub("sub-1", "v1"),),
        )


def test_edit_track_rejects_regular_clips_on_subtitle_track() -> None:
    media_clip = ClipInstance(
        clip_id="media-1",
        asset_id="asset-1",
        track_id="s1",
        timeline_start=0,
        timeline_duration=24,
        source_start=0,
        source_duration=24,
    )
    with pytest.raises(ValueError, match="cannot carry regular media clips"):
        EditTrack(
            track_id="s1", name="S1", track_type="subtitle", index=0,
            clips=(media_clip,),
        )


def test_edit_track_rejects_mismatched_clip_track_id() -> None:
    with pytest.raises(ValueError, match="does not match owner track"):
        EditTrack(
            track_id="s1", name="S1", track_type="subtitle", index=0,
            subtitle_clips=(_sub("sub-1", track_id="s2"),),
        )


def test_edit_track_rejects_duplicate_clip_id_within_track() -> None:
    with pytest.raises(ValueError, match="Duplicate subtitle clip_id within track"):
        EditTrack(
            track_id="s1", name="S1", track_type="subtitle", index=0,
            subtitle_clips=(
                _sub("dup", "s1", start=0, duration=10),
                _sub("dup", "s1", start=20, duration=10),
            ),
        )


def test_edit_track_accepts_well_formed_subtitle_track() -> None:
    track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(
            _sub("sub-1", "s1", start=0, duration=10),
            _sub("sub-2", "s1", start=20, duration=10),
        ),
    )
    assert len(track.subtitle_clips) == 2


# ── TimelineSequence global cue-id uniqueness ──────────────────────────────


def test_sequence_rejects_duplicate_clip_id_across_tracks() -> None:
    t1 = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(_sub("shared", "s1", start=0, duration=10),),
    )
    t2 = EditTrack(
        track_id="s2", name="S2", track_type="subtitle", index=1,
        subtitle_clips=(_sub("shared", "s2", start=0, duration=10),),
    )
    with pytest.raises(ValueError, match="Duplicate subtitle clip_id across sequence"):
        TimelineSequence(
            timeline_id="seq", name="S", width=1920, height=1080, fps=24,
            subtitle_tracks=(t1, t2),
        )


def test_sequence_accepts_distinct_clip_ids_across_tracks() -> None:
    t1 = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(_sub("a", "s1", start=0, duration=10),),
    )
    t2 = EditTrack(
        track_id="s2", name="S2", track_type="subtitle", index=1,
        subtitle_clips=(_sub("b", "s2", start=0, duration=10),),
    )
    seq = TimelineSequence(
        timeline_id="seq", name="S", width=1920, height=1080, fps=24,
        subtitle_tracks=(t1, t2),
    )
    assert seq.get_subtitle_clip_by_id("a") is not None
    assert seq.get_subtitle_clip_by_id("b") is not None


# ── Cross-track move / Alt-drag copy ──────────────────────────────────────


def _two_subtitle_track_sequence(*, target_locked: bool = False) -> TimelineSequence:
    source = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(_sub("source", "s1", start=0, duration=10),),
    )
    target = EditTrack(
        track_id="s2", name="S2", track_type="subtitle", index=1,
        is_locked=target_locked,
        subtitle_clips=(_sub("target-existing", "s2", start=40, duration=10),),
    )
    return TimelineSequence(
        timeline_id="seq", name="S", width=1920, height=1080, fps=24,
        subtitle_tracks=(source, target),
    )


def test_move_subtitle_clip_cross_track_round_trips() -> None:
    sequence = _two_subtitle_track_sequence()
    command = MoveSubtitleClipCommand.create("source", "s2", 20)

    moved = command.apply(sequence)
    cue = moved.get_subtitle_clip_by_id("source")
    assert cue is not None
    assert cue.track_id == "s2"
    assert cue.timeline_start == 20
    assert moved.get_track_by_id("s1").subtitle_clips == ()

    restored = command.undo(moved)
    cue = restored.get_subtitle_clip_by_id("source")
    assert cue is not None
    assert cue.track_id == "s1"
    assert cue.timeline_start == 0


def test_copy_subtitle_clip_reuses_identity_on_redo() -> None:
    sequence = _two_subtitle_track_sequence()
    command = CopySubtitleClipCommand.create("source", "s2", 20)
    stack = CommandStack()

    copied = stack.execute(command, sequence)
    copied_id = command._copied_clip_id
    assert copied_id is not None
    copied_cue = copied.get_subtitle_clip_by_id(copied_id)
    assert copied_cue is not None
    assert copied_cue.track_id == "s2"
    assert copied_cue.timeline_start == 20
    assert copied.get_subtitle_clip_by_id("source") is not None

    undone = stack.undo(copied)
    assert undone is not None
    assert undone.get_subtitle_clip_by_id(copied_id) is None

    redone = stack.redo(undone)
    assert redone is not None
    assert command._copied_clip_id == copied_id
    assert redone.get_subtitle_clip_by_id(copied_id) is not None


def test_cross_track_subtitle_move_and_copy_reject_locked_target() -> None:
    sequence = _two_subtitle_track_sequence(target_locked=True)
    with pytest.raises(ValueError, match="locked"):
        MoveSubtitleClipCommand.create("source", "s2", 20).apply(sequence)
    with pytest.raises(ValueError, match="locked"):
        CopySubtitleClipCommand.create("source", "s2", 20).apply(sequence)


def test_cross_track_subtitle_move_rejects_target_overlap() -> None:
    sequence = _two_subtitle_track_sequence()
    with pytest.raises(ValueError, match="overlaps"):
        MoveSubtitleClipCommand.create("source", "s2", 45).apply(sequence)


def test_overwrite_move_eats_overlapped_cue_and_undo_restores_it() -> None:
    """Timeline drag moves overwrite occupied subtitle spans in one undo step."""
    moving = _sub("moving", "s1", start=0, duration=20)
    existing = _sub("existing", "s1", start=20, duration=20)
    track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(moving, existing),
    )
    sequence = TimelineSequence(
        timeline_id="seq", name="S", width=1920, height=1080, fps=24,
        subtitle_tracks=(track,),
    )
    command = MoveSubtitleClipCommand.create(
        "moving", "s1", 30, overwrite_overlaps=True,
    )

    moved = command.apply(sequence)
    assert moved.get_subtitle_clip_by_id("moving").timeline_start == 30
    survivor = moved.get_subtitle_clip_by_id("existing")
    assert survivor is not None
    assert (survivor.timeline_start, survivor.timeline_duration) == (20, 10)

    restored = command.undo(moved)
    restored_existing = restored.get_subtitle_clip_by_id("existing")
    assert restored_existing is not None
    assert (restored_existing.timeline_start, restored_existing.timeline_duration) == (20, 20)
    assert restored.get_subtitle_clip_by_id("moving").timeline_start == 0


def test_overwrite_move_removes_fully_covered_cue() -> None:
    moving = _sub("moving", "s1", start=0, duration=20)
    covered = _sub("covered", "s1", start=35, duration=10)
    track = EditTrack(
        track_id="s1", name="S1", track_type="subtitle", index=0,
        subtitle_clips=(moving, covered),
    )
    sequence = TimelineSequence(
        timeline_id="seq", name="S", width=1920, height=1080, fps=24,
        subtitle_tracks=(track,),
    )

    moved = MoveSubtitleClipCommand.create(
        "moving", "s1", 30, overwrite_overlaps=True,
    ).apply(sequence)

    assert moved.get_subtitle_clip_by_id("covered") is None
    assert moved.get_subtitle_clip_by_id("moving").timeline_start == 30


@pytest.mark.parametrize(
    ("collection_name", "track_type"),
    [
        ("video_tracks", "subtitle"),
        ("audio_tracks", "subtitle"),
        ("subtitle_tracks", "video"),
        ("subtitle_tracks", "audio"),
    ],
)
def test_sequence_rejects_track_in_wrong_collection(
    collection_name: str, track_type: str,
) -> None:
    track = EditTrack(
        track_id="wrong-1", name="Wrong", track_type=track_type, index=0,
    )
    kwargs = {collection_name: (track,)}
    with pytest.raises(ValueError, match=f"{collection_name} can only contain"):
        TimelineSequence(
            timeline_id="seq", name="S", width=1920, height=1080, fps=24,
            **kwargs,
        )
