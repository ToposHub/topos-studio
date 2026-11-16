"""Track identity colors survive editing and project round trips."""

from __future__ import annotations

import json

import pytest

from src.features.edit.core.commands import (
    AddBusCommand, AddTrackCommand, CommandStack, CopyClipsToNewTrackCommand,
    DeleteTrackCommand, InsertLinkedAssetCommand, InsertLinkedAssetRippleCommand,
    MoveClipToTrackCommand, MoveTrackCommand,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.track_colors import TRACK_COLORS, next_track_color, resolve_track_color


pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int = 0) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id="asset", track_id=track_id,
        timeline_start=start, timeline_duration=24,
        source_start=0, source_duration=24,
    )


@pytest.mark.parametrize("track_type", ["video", "audio", "subtitle"])
def test_new_tracks_keep_distinct_colors_across_insert_delete_move_and_save(track_type: str) -> None:
    timeline = TimelineSequence()
    for index in range(4):
        timeline, _ = AddTrackCommand.create(track_type, track_id=f"track-{index}").apply(timeline)
    original = {track.track_id: track.color for track in timeline.all_tracks}
    assert len(set(original.values())) == 4
    timeline, _ = AddTrackCommand.create(track_type, position=0, track_id="inserted").apply(timeline)
    assert len({track.color for track in timeline.all_tracks}) == 5
    timeline, deletion = DeleteTrackCommand.create("track-1").apply(timeline)
    timeline = MoveTrackCommand.create("track-2", "up").apply(timeline)
    timeline = deletion.undo(timeline)
    for track_id, color in original.items():
        assert timeline.get_track_by_id(track_id).color == color
    restored = TimelineSequence.from_dict(json.loads(json.dumps(timeline.to_dict())))
    assert {track.track_id: track.color for track in restored.all_tracks} == {
        track.track_id: track.color for track in timeline.all_tracks
    }


def test_legacy_and_custom_colors_are_not_rewritten_or_reused() -> None:
    legacy = EditTrack(track_id="legacy", track_type="audio")
    custom = EditTrack(track_id="custom", track_type="audio", index=1, color=TRACK_COLORS[0].upper())
    timeline = TimelineSequence(audio_tracks=(legacy, custom))
    updated, _ = AddTrackCommand.create("audio").apply(timeline)
    created = updated.audio_tracks[-1]
    assert updated.audio_tracks[0].color is None
    assert updated.audio_tracks[1].color == custom.color
    assert created.color.casefold() != custom.color.casefold()
    assert created.color != resolve_track_color(legacy.track_id, None)
    deleted, command = DeleteTrackCommand.create("legacy").apply(updated)
    assert command.undo(deleted).get_track_by_id("legacy").color is None
    assert resolve_track_color("custom", custom.color) == custom.color
    ensured, _ = AddTrackCommand.create("audio", track_id="legacy").apply(timeline)
    assert ensured is timeline
    assert ensured.get_track_by_id("legacy").color is None


@pytest.mark.parametrize("kind", ["menu", "bus", "copy", "move", "insert", "insert_missing", "ripple"])
def test_new_track_creation_paths_keep_color_on_undo_redo(kind: str) -> None:
    audio = EditTrack(
        track_id="a1", track_type="audio", color=TRACK_COLORS[0],
        clips=(_clip("audio-original", "a1"),),
    )
    video = EditTrack(track_id="v1", track_type="video")
    timeline = TimelineSequence(video_tracks=(video,), audio_tracks=(audio,))
    if kind == "menu":
        command = AddTrackCommand.create("audio")
    elif kind == "bus":
        command = AddBusCommand.create()
    elif kind == "copy":
        command = CopyClipsToNewTrackCommand.create("audio", "below", [_clip("copy", "a1")])
    elif kind == "move":
        command = MoveClipToTrackCommand.create_for_new_track(
            "a1", "audio-original", "audio", "below", 0, 0,
        )
    elif kind in ("insert", "insert_missing"):
        command = InsertLinkedAssetCommand.create(
            "v1", _clip("video-new", "v1"),
            "a1" if kind == "insert" else None, _clip("audio-new", "a1"),
        )
    else:
        command = InsertLinkedAssetRippleCommand(
            command_id="ripple", label="Insert A/V", asset_id="asset",
            in_frame=0, out_frame=24, playhead=0, video_track_id="v1",
        )
    stack = CommandStack()
    added = stack.execute(command, timeline)
    new_track = added.audio_tracks[-1]
    assert new_track.track_id != audio.track_id
    assert new_track.color is not None
    assert new_track.color != audio.color
    assert new_track.color != resolve_track_color(video.track_id, video.color)
    undone = stack.undo(added)
    assert undone is not None
    assert len(undone.audio_tracks) == 1
    assert undone.audio_tracks[0].color == audio.color
    redone = stack.redo(undone)
    assert redone is not None
    assert redone.audio_tracks[-1].color == new_track.color
    assert redone.audio_tracks[0].color == audio.color


def test_palette_extends_without_repeating_saved_colors() -> None:
    colors: list[str] = []
    for _ in range(40):
        colors.append(next_track_color(colors))
    assert len(set(colors)) == 40
    assert tuple(colors[:len(TRACK_COLORS)]) == TRACK_COLORS


def test_interleaved_video_audio_subtitle_and_bus_tracks_use_different_colors() -> None:
    timeline = TimelineSequence()
    for track_type in ("video", "audio", "subtitle", "audio", "video", "bus", "subtitle", "bus"):
        command = AddBusCommand.create() if track_type == "bus" else AddTrackCommand.create(track_type)
        timeline, _ = command.apply(timeline)
        colors = [track.color for track in timeline.all_tracks]
        assert None not in colors
        assert len(set(colors)) == len(colors)
