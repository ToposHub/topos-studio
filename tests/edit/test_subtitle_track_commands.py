"""Regression tests for subtitle track reordering and metadata retention."""

from __future__ import annotations

import pytest

from src.features.edit.core.captions.model import CaptionStyle, CaptionTrackMetadata
from src.features.edit.core.commands import (
    AddTrackCommand,
    DeleteTrackCommand,
    MoveTrackCommand,
)
from src.features.edit.core.timeline_v2 import EditTrack, SubtitleClip, TimelineSequence

pytestmark = pytest.mark.edit_core


def _timeline() -> TimelineSequence:
    def track(track_id: str, index: int, text: str) -> EditTrack:
        cue = SubtitleClip(
            clip_id=f"cue-{track_id}", track_id=track_id,
            timeline_start=0, timeline_duration=24, text=text,
        )
        return EditTrack(
            track_id=track_id, name=track_id, track_type="subtitle", index=index,
            subtitle_clips=(cue,),
            caption_metadata=CaptionTrackMetadata(
                language="zh-CN", default_style=CaptionStyle(translate_x=index * 10.0)
            ),
        )

    return TimelineSequence(
        timeline_id="timeline", subtitle_tracks=(track("s1", 0, "one"), track("s2", 1, "two"))
    )


def test_move_subtitle_track_preserves_cues_and_global_style() -> None:
    timeline = _timeline()

    moved = MoveTrackCommand.create("s1", "down").apply(timeline)

    assert [track.track_id for track in moved.subtitle_tracks] == ["s2", "s1"]
    restored_track = moved.subtitle_tracks[1]
    assert restored_track.subtitle_clips[0].text == "one"
    assert restored_track.caption_metadata.language == "zh-CN"
    assert restored_track.caption_metadata.default_style.translate_x == 0.0
    assert [track.index for track in moved.subtitle_tracks] == [0, 1]


@pytest.mark.parametrize("command_factory", [
    lambda: AddTrackCommand.create("subtitle", "new", position=0),
    lambda: DeleteTrackCommand.create("s1"),
])
def test_add_delete_reindex_preserves_subtitle_metadata(command_factory) -> None:
    timeline = _timeline()
    command = command_factory()
    updated = command.apply(timeline)
    updated_timeline = updated[0] if isinstance(updated, tuple) else updated

    survivors = {
        track.track_id: track for track in updated_timeline.subtitle_tracks
    }
    if "s1" in survivors:
        assert survivors["s1"].caption_metadata.language == "zh-CN"
        assert survivors["s1"].subtitle_clips[0].text == "one"
    if "s2" in survivors:
        assert survivors["s2"].caption_metadata.language == "zh-CN"
        assert survivors["s2"].subtitle_clips[0].text == "two"
