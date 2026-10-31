"""Regression coverage for linked audio/video timeline invariants."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.commands import (
    BatchDeleteCommand,
    ChangeClipSpeedCommand,
    DeleteClipCommand,
    InsertLinkedAssetCommand,
    MacroCommand,
    SetClipLinkedCommand,
    SplitClipCommand,
)
from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


def _clip(
    clip_id: str,
    track_id: str,
    linked_id: str | None = None,
    duration: int = 100,
) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id="asset_av",
        track_id=track_id,
        timeline_start=0,
        timeline_duration=duration,
        source_start=0,
        source_duration=duration,
        speed=1.0,
        linked_clip_id=linked_id,
    )


def _linked_timeline() -> TimelineSequence:
    video = EditTrack(
        track_id="v1",
        name="V1",
        track_type="video",
        index=0,
        clips=(_clip("v0", "v1", "a0"),),
    )
    audio = EditTrack(
        track_id="a1",
        name="A1",
        track_type="audio",
        index=0,
        clips=(_clip("a0", "a1", "v0"),),
    )
    return TimelineSequence(
        timeline_id="tl1",
        name="Linked",
        fps=Rational(24, 1),
        video_tracks=(video,),
        audio_tracks=(audio,),
    )


def test_one_sided_split_keeps_left_link_valid() -> None:
    timeline = _linked_timeline()
    command = SplitClipCommand.create("v1", "v0", 50)

    result = command.apply(timeline)
    assert result.get_clip_by_id("v0").linked_clip_id == "a0"
    assert result.get_reciprocal_linked_clip("v0").clip_id == "a0"
    assert result.get_clip_by_id("a0").linked_clip_id == "v0"
    assert result.get_track_by_id("v1").get_clip_at(50) is not None


def test_macro_split_relinks_right_audio_video_segments() -> None:
    timeline = _linked_timeline()
    macro = MacroCommand.create(
        "Split linked A/V",
        [
            SplitClipCommand.create("v1", "v0", 50),
            SplitClipCommand.create("a1", "a0", 50),
        ],
    )

    split_timeline, updated_macro = macro.apply(timeline)
    right_video = split_timeline.get_track_by_id("v1").get_clip_at(50)
    right_audio = split_timeline.get_track_by_id("a1").get_clip_at(50)
    assert right_video is not None and right_audio is not None
    assert right_video.linked_clip_id == right_audio.clip_id
    assert right_audio.linked_clip_id == right_video.clip_id

    restored = updated_macro.undo(split_timeline)
    assert restored.get_clip_by_id("v0").linked_clip_id == "a0"
    assert restored.get_clip_by_id("a0").linked_clip_id == "v0"


def test_delete_clears_partner_and_undo_restores_pair() -> None:
    timeline = _linked_timeline()
    command = DeleteClipCommand.create("v1", "v0")

    deleted, updated = command.apply(timeline)
    assert deleted.get_clip_by_id("v0") is None
    assert deleted.get_clip_by_id("a0").linked_clip_id is None

    restored = updated.undo(deleted)
    assert restored.get_clip_by_id("v0").linked_clip_id == "a0"
    assert restored.get_clip_by_id("a0").linked_clip_id == "v0"


def test_batch_delete_clears_surviving_partner_and_undo_restores_pair() -> None:
    timeline = _linked_timeline()
    command = BatchDeleteCommand.create(timeline, ["v0"])

    deleted, updated = command.apply(timeline)
    assert deleted.get_clip_by_id("a0").linked_clip_id is None

    restored = updated.undo(deleted)
    assert restored.get_clip_by_id("v0").linked_clip_id == "a0"
    assert restored.get_clip_by_id("a0").linked_clip_id == "v0"


def test_relink_undo_restores_displaced_pair() -> None:
    timeline = _linked_timeline()
    extra_audio = EditTrack(
        track_id="a2",
        name="A2",
        track_type="audio",
        index=1,
        clips=(_clip("a1", "a2"),),
    )
    timeline = timeline.with_track_added(extra_audio)
    command = SetClipLinkedCommand.create_link("v0", "v1", "a1", "a2")

    relinked = command.apply(timeline)
    restored = command.undo(relinked)
    assert restored.get_clip_by_id("v0").linked_clip_id == "a0"
    assert restored.get_clip_by_id("a0").linked_clip_id == "v0"
    assert restored.get_clip_by_id("a1").linked_clip_id is None


def test_speed_change_updates_and_undoes_both_members() -> None:
    timeline = _linked_timeline()
    command = ChangeClipSpeedCommand.create("v0", "v1", 2.0)

    changed, updated = command.apply(timeline)
    assert changed.get_clip_by_id("v0").timeline_duration == 50
    assert changed.get_clip_by_id("a0").timeline_duration == 50
    assert changed.get_clip_by_id("a0").speed.speed == 2.0

    restored = updated.undo(changed)
    assert restored.get_clip_by_id("v0").timeline_duration == 100
    assert restored.get_clip_by_id("a0").timeline_duration == 100
    assert restored.get_clip_by_id("a0").speed.speed == 1.0


def test_variable_speed_change_keeps_linked_members_in_sync() -> None:
    timeline = _linked_timeline()
    speed = (
        SpeedParams(speed=1.0)
        .with_speed_point(50, 100)
        .with_speed_point_speed(50, 2.0)
    )
    command = ChangeClipSpeedCommand.create("v0", "v1", speed)

    changed, updated = command.apply(timeline)
    assert changed.get_clip_by_id("v0").timeline_duration == 75
    assert changed.get_clip_by_id("a0").timeline_duration == 75
    assert changed.get_clip_by_id("v0").speed == speed
    assert changed.get_clip_by_id("a0").speed == speed

    restored = updated.undo(changed)
    assert restored.get_clip_by_id("v0").timeline_duration == 100
    assert restored.get_clip_by_id("a0").timeline_duration == 100


def test_loading_repairs_stale_one_way_link() -> None:
    timeline = _linked_timeline()
    data = timeline.to_dict()
    data["video_tracks"][0]["clips"][0]["linked_clip_id"] = "missing"

    loaded = TimelineSequence.from_dict(data)
    assert loaded.get_clip_by_id("v0").linked_clip_id is None
    assert loaded.get_clip_by_id("a0").linked_clip_id is None


def test_insert_linked_asset_creates_missing_audio_track_atomically() -> None:
    video_track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = TimelineSequence(timeline_id="tl1", video_tracks=(video_track,))
    video = _clip("v_new", "v1", "a_new", duration=40)
    audio = _clip("a_new", "pending", "v_new", duration=40)
    command = InsertLinkedAssetCommand.create(
        video_track_id="v1",
        video_clip=video,
        audio_track_id=None,
        audio_clip=audio,
    )

    inserted, _ = command.apply(timeline)
    assert len(inserted.audio_tracks) == 1
    assert inserted.get_reciprocal_linked_clip("v_new").clip_id == "a_new"
