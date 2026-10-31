"""Tests for track-global subtitle styling and per-cue custom overrides."""

from __future__ import annotations

from src.features.edit.core.captions.commands import (
    UpdateSubtitleTrackMetadataCommand,
    UpdateSubtitleTrackStyleCommand,
)
from src.features.edit.core.captions.model import (
    CaptionCueMetadata,
    CaptionStyle,
    CaptionTrackMetadata,
    EMPTY_STYLE,
    clip_style_base,
    resolve_style,
)
from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip, TimelineSequence


def _sequence() -> TimelineSequence:
    track_id = "sub-track"
    first = SubtitleClip(
        clip_id="cue-1", track_id=track_id, timeline_start=0,
        timeline_duration=24, text="One",
    )
    second = SubtitleClip(
        clip_id="cue-2", track_id=track_id, timeline_start=30,
        timeline_duration=24, text="Two",
    )
    track = EditTrack(
        track_id=track_id, track_type="subtitle", subtitle_clips=(first, second),
    )
    return TimelineSequence(timeline_id="timeline", subtitle_tracks=(track,))


def test_track_style_resolves_for_every_inherited_cue() -> None:
    sequence = _sequence()
    track = sequence.subtitle_tracks[0]
    default = CaptionStyle(
        halign="center", valign="middle", translate_x=120.0,
        translate_y=-80.0, scale_x=1.1,
    )
    track = track.with_caption_metadata(CaptionTrackMetadata(default_style=default))
    sequence = sequence.with_track_updated(track)

    for cue in track.subtitle_clips:
        style = resolve_style(default, cue.style_override, base=clip_style_base(cue))
        assert style["halign"] == "center"
        assert style["valign"] == "middle"
        assert style["translate_x"] == 120.0
        assert style["translate_y"] == -80.0
        assert style["scale_x"] == 1.1


def test_custom_override_stops_following_track_style() -> None:
    sequence = _sequence()
    track = sequence.subtitle_tracks[0]
    custom = CaptionStyle(halign="left", translate_x=-240.0)
    cue = track.subtitle_clips[0].with_style_override(custom)
    track = track.with_subtitle_clip_updated(cue)
    track = track.with_caption_metadata(
        CaptionTrackMetadata(default_style=CaptionStyle(halign="center", translate_x=100.0))
    )

    resolved = resolve_style(
        track.caption_metadata.default_style,
        cue.style_override,
        base=clip_style_base(cue),
    )

    assert resolved["halign"] == "left"
    assert resolved["translate_x"] == -240.0


def test_global_style_command_is_undoable() -> None:
    sequence = _sequence()
    command = UpdateSubtitleTrackStyleCommand.create(
        "sub-track", CaptionStyle(halign="right", translate_x=300.0),
    )

    updated, applied = command.apply(sequence)
    assert updated.subtitle_tracks[0].caption_metadata.default_style.halign == "right"
    assert updated.subtitle_tracks[0].caption_metadata.default_style.translate_x == 300.0

    restored = applied.undo(updated)
    assert restored.subtitle_tracks[0].caption_metadata is None


def test_empty_style_keeps_legacy_defaults() -> None:
    resolved = resolve_style(EMPTY_STYLE, None)
    assert resolved["translate_x"] == 0.0
    assert resolved["scale_x"] == 1.0


def test_global_style_does_not_bypass_locked_inherited_cue() -> None:
    sequence = _sequence()
    track = sequence.subtitle_tracks[0]
    locked = track.subtitle_clips[0].with_caption_metadata(
        CaptionCueMetadata(locked=True)
    )
    sequence = sequence.with_track_updated(track.with_subtitle_clip_updated(locked))

    command = UpdateSubtitleTrackStyleCommand.create(
        "sub-track", CaptionStyle(translate_x=100.0),
    )

    import pytest
    with pytest.raises(ValueError, match="Locked subtitle cue"):
        command.apply(sequence)


def test_track_language_metadata_is_undoable() -> None:
    sequence = _sequence()
    command = UpdateSubtitleTrackMetadataCommand.create(
        "sub-track", {"language": "en-US", "role": "secondary"},
    )

    updated, applied = command.apply(sequence)
    assert updated.subtitle_tracks[0].caption_metadata.language == "en-US"
    assert updated.subtitle_tracks[0].caption_metadata.role == "secondary"

    restored = applied.undo(updated)
    assert restored.subtitle_tracks[0].caption_metadata is None
