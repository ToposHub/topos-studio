"""Caption review locks are enforced at the command boundary."""

from __future__ import annotations

import pytest

from src.features.edit.core.captions.commands import (
    MergeSubtitleClipsCommand,
    SplitSubtitleClipCommand,
    UpdateCaptionStyleOverrideCommand,
)
from src.features.edit.core.captions.model import (
    CaptionCueMetadata,
    CaptionStatus,
    CaptionStyle,
)
from src.features.edit.core.commands import (
    AddSubtitleClipCommand,
    DeleteSubtitleClipCommand,
    UpdateSubtitleClipCommand,
)
from src.features.edit.core.timeline_v2 import (
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _sequence(metadata: CaptionCueMetadata) -> TimelineSequence:
    track = EditTrack(
        track_id="s1",
        name="S1",
        track_type="subtitle",
        index=0,
        subtitle_clips=(
            SubtitleClip(
                clip_id="locked",
                track_id="s1",
                timeline_start=0,
                timeline_duration=24,
                text="locked cue",
                caption_metadata=metadata,
            ),
            SubtitleClip(
                clip_id="open",
                track_id="s1",
                timeline_start=24,
                timeline_duration=24,
                text="open cue",
            ),
        ),
    )
    return TimelineSequence(
        timeline_id="seq",
        name="Sequence",
        width=1920,
        height=1080,
        fps=24,
        subtitle_tracks=(track,),
    )


@pytest.mark.parametrize(
    "metadata",
    [
        CaptionCueMetadata(locked=True, speaker="A"),
        CaptionCueMetadata(status=CaptionStatus.LOCKED, speaker="A"),
    ],
)
def test_locked_cue_rejects_update_delete_split_style_and_merge(
    metadata: CaptionCueMetadata,
) -> None:
    sequence = _sequence(metadata)
    commands = (
        UpdateSubtitleClipCommand.create("locked", {"text": "changed"}),
        DeleteSubtitleClipCommand.create("locked"),
        SplitSubtitleClipCommand.create("locked", 12),
        UpdateCaptionStyleOverrideCommand.create(
            "locked", CaptionStyle(font_size=80),
        ),
        MergeSubtitleClipsCommand.create("locked", "open"),
    )
    for command in commands:
        with pytest.raises(ValueError, match="locked|Locked"):
            command.apply(sequence)


@pytest.mark.parametrize(
    "metadata",
    [
        CaptionCueMetadata(locked=True, speaker="A", note="keep"),
        CaptionCueMetadata(
            status=CaptionStatus.LOCKED,
            speaker="A",
            note="keep",
        ),
    ],
)
def test_pure_metadata_unlock_is_allowed_and_undo_restores_lock(
    metadata: CaptionCueMetadata,
) -> None:
    sequence = _sequence(metadata)
    unlocked = metadata.replace(locked=False, status=CaptionStatus.DRAFT)
    command = UpdateSubtitleClipCommand.create(
        "locked", {"caption_metadata": unlocked}, label="Unlock Caption",
    )

    updated = command.apply(sequence)
    assert not updated.get_subtitle_clip_by_id("locked").is_edit_locked()

    restored = command.undo(updated)
    assert restored.get_subtitle_clip_by_id("locked").is_edit_locked()


def test_unlock_cannot_change_other_metadata_or_text_atomically() -> None:
    metadata = CaptionCueMetadata(locked=True, speaker="A", note="keep")
    sequence = _sequence(metadata)

    changed_note = metadata.replace(locked=False, note="changed")
    with pytest.raises(ValueError, match="locked"):
        UpdateSubtitleClipCommand.create(
            "locked", {"caption_metadata": changed_note},
        ).apply(sequence)

    unlocked = metadata.replace(locked=False)
    with pytest.raises(ValueError, match="locked"):
        UpdateSubtitleClipCommand.create(
            "locked", {"caption_metadata": unlocked, "text": "changed"},
        ).apply(sequence)


def test_unlocked_cue_remains_editable() -> None:
    sequence = _sequence(CaptionCueMetadata())
    updated = UpdateSubtitleClipCommand.create(
        "locked", {"text": "changed"},
    ).apply(sequence)
    assert updated.get_subtitle_clip_by_id("locked").text == "changed"


def test_locked_track_rejects_all_subtitle_mutations() -> None:
    sequence = _sequence(CaptionCueMetadata())
    locked_track = sequence.subtitle_tracks[0]._rebuild(is_locked=True)
    sequence = sequence._rebuild(subtitle_tracks=(locked_track,))

    commands = (
        AddSubtitleClipCommand.create("s1", 48, duration=24),
        UpdateSubtitleClipCommand.create("locked", {"text": "changed"}),
        DeleteSubtitleClipCommand.create("locked"),
        SplitSubtitleClipCommand.create("locked", 12),
        MergeSubtitleClipsCommand.create("locked", "open"),
    )
    for command in commands:
        with pytest.raises(ValueError, match="track|tracks"):
            command.apply(sequence)


@pytest.mark.parametrize(
    "metadata",
    [
        CaptionCueMetadata(locked=True),
        CaptionCueMetadata(status=CaptionStatus.LOCKED),
    ],
)
@pytest.mark.parametrize("mode", ["overwrite", "ripple"])
def test_add_cannot_indirectly_displace_locked_cue(
    metadata: CaptionCueMetadata,
    mode: str,
) -> None:
    sequence = _sequence(metadata)
    command = AddSubtitleClipCommand.create(
        "s1", 0, duration=12, mode=mode,
    )
    with pytest.raises(ValueError, match="Locked subtitle clips"):
        command.apply(sequence)


def test_insert_may_advance_past_locked_cue_without_mutating_it() -> None:
    sequence = _sequence(CaptionCueMetadata(locked=True))
    command = AddSubtitleClipCommand.create(
        "s1", 0, duration=12, mode="insert",
    )
    result = command.apply(sequence)
    updated = result[0] if isinstance(result, tuple) else result

    locked = updated.get_subtitle_clip_by_id("locked")
    assert locked.timeline_start == 0
    assert locked.timeline_duration == 24
    assert len(updated.subtitle_tracks[0].subtitle_clips) == 3


def test_add_rejects_unknown_edit_mode_before_dispatch() -> None:
    with pytest.raises(ValueError, match="Invalid caption edit mode"):
        AddSubtitleClipCommand.create(
            "s1", 0, duration=12, mode="bogus",
        )
