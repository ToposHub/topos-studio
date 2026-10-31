"""
Regression tests for timeline command undo/redo bugs.

Tests for:
- SetTrackMuteCommand: apply + undo restores original mute state
- SetTrackSoloCommand: apply + undo restores original solo state
- SetTrackLockCommand: apply + undo restores original lock state
- SetTrackVolumeCommand: apply + undo restores original volume
- RenameTrackCommand: apply + undo restores original name and volume preserved
- AddTrackCommand: undo removes track, volume preserved on remaining tracks
- DeleteTrackCommand: undo restores track with all fields including volume
- MacroCommand with track commands: undo restores all sub-operations
- MoveClipToTrackCommand: undo restores clip and all track volumes preserved
- MoveClipToExistingTrackCommand: volumes preserved on both source and target tracks
- SlipClipCommand: apply + undo restores original source_start
- RippleDeleteCommand: undo restores deleted clip and moved clips
- SplitClipCommand: works with both float speed and SpeedParams
"""

from __future__ import annotations

import pytest
import uuid

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    EditTrack,
    ClipInstance,
    SubtitleClip,
)
from src.features.edit.core.clip_params import TransformParams, CropParams, SpeedParams
from src.features.edit.core.commands import (
    AddClipCommand,
    DeleteClipCommand,
    SetTrackMuteCommand,
    SetTrackSoloCommand,
    SetTrackLockCommand,
    SetTrackVolumeCommand,
    SetTrackPanCommand,
    RenameTrackCommand,
    AddTrackCommand,
    DeleteTrackCommand,
    MacroCommand,
    MoveClipsOverwriteCommand,
    MoveClipToTrackCommand,
    MoveClipToExistingTrackCommand,
    SlipClipCommand,
    RippleDeleteCommand,
    SplitClipCommand,
    CommandStack,
)


# ================================================================
# MoveClipsOverwriteCommand Tests
# ================================================================

class TestMoveClipsOverwriteCommand:
    """Dragging onto occupied existing tracks overwrites target material."""

    def test_cross_track_move_fully_overwrites_target_clip(
        self,
        timeline_with_two_tracks: TimelineSequence,
        sample_clip: ClipInstance,
    ):
        blocker = ClipInstance(
            clip_id="blocker",
            asset_id="asset_blocker",
            track_id="track_2",
            timeline_start=20,
            timeline_duration=30,
            source_start=0,
            source_duration=30,
            speed=SpeedParams(speed=1.0),
        )
        tl = timeline_with_two_tracks.with_clip_added("track_1", sample_clip)
        tl = tl.with_clip_added("track_2", blocker)

        cmd = MoveClipsOverwriteCommand.create([
            {
                "clip_id": "clip_1",
                "source_track_id": "track_1",
                "target_track_id": "track_2",
                "new_start": 10,
                "old_start": 0,
            }
        ])
        stack = CommandStack()
        result = stack.execute(cmd, tl)

        assert result.get_track_by_id("track_1").get_clip_by_id("clip_1") is None
        moved = result.get_track_by_id("track_2").get_clip_by_id("clip_1")
        assert moved is not None
        assert moved.timeline_start == 10
        assert result.get_clip_by_id("blocker") is None

        restored = stack.undo(result)
        assert restored is not None
        assert restored.get_track_by_id("track_1").get_clip_by_id("clip_1") is not None
        assert restored.get_track_by_id("track_2").get_clip_by_id("blocker") is not None

    def test_overwrite_middle_splits_existing_target_clip(
        self,
        timeline_with_two_tracks: TimelineSequence,
        sample_clip: ClipInstance,
    ):
        short_clip = sample_clip.with_timeline_duration(20)
        long_target = ClipInstance(
            clip_id="target",
            asset_id="asset_target",
            track_id="track_2",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            speed=SpeedParams(speed=1.0),
            linked_clip_id="target-audio",
        )
        tl = timeline_with_two_tracks.with_clip_added("track_1", short_clip)
        tl = tl.with_clip_added("track_2", long_target)

        cmd = MoveClipsOverwriteCommand.create([
            {
                "clip_id": "clip_1",
                "source_track_id": "track_1",
                "target_track_id": "track_2",
                "new_start": 40,
                "old_start": 0,
            }
        ])
        result = CommandStack().execute(cmd, tl)
        clips = result.get_track_by_id("track_2").clips

        assert [(c.clip_id, c.timeline_start, c.timeline_duration) for c in clips] == [
            ("target", 0, 40),
            ("clip_1", 40, 20),
            (clips[2].clip_id, 60, 40),
        ]
        assert clips[0].linked_clip_id is None
        assert clips[2].linked_clip_id is None

    def test_partial_overwrite_clears_surviving_link_partner_and_undo_restores_it(
        self,
        timeline_with_two_tracks: TimelineSequence,
        sample_clip: ClipInstance,
    ) -> None:
        moving = sample_clip.with_timeline_duration(20)
        target = ClipInstance(
            clip_id="target",
            asset_id="asset_target",
            track_id="track_2",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            speed=SpeedParams(speed=1.0),
            linked_clip_id="target_audio",
        )
        audio = EditTrack(
            track_id="audio_1",
            name="Audio 1",
            track_type="audio",
            index=0,
            clips=(
                ClipInstance(
                    clip_id="target_audio",
                    asset_id="asset_target",
                    track_id="audio_1",
                    timeline_start=0,
                    timeline_duration=100,
                    source_start=0,
                    source_duration=100,
                    speed=SpeedParams(speed=1.0),
                    linked_clip_id="target",
                ),
            ),
        )
        timeline = timeline_with_two_tracks.with_clip_added("track_1", moving)
        timeline = timeline.with_clip_added("track_2", target).with_track_added(audio)
        command = MoveClipsOverwriteCommand.create([
            {
                "clip_id": "clip_1",
                "source_track_id": "track_1",
                "target_track_id": "track_2",
                "new_start": 0,
                "old_start": 0,
            }
        ])

        stack = CommandStack()
        changed = stack.execute(command, timeline)
        surviving_target = changed.get_clip_by_id("target")
        assert surviving_target is not None
        assert surviving_target.timeline_start == 20
        assert surviving_target.linked_clip_id is None
        assert changed.get_clip_by_id("target_audio").linked_clip_id is None

        restored = stack.undo(changed)
        assert restored.get_clip_by_id("target").linked_clip_id == "target_audio"
        assert restored.get_clip_by_id("target_audio").linked_clip_id == "target"


# ================================================================
# Test Fixtures
# ================================================================

@pytest.fixture
def empty_timeline() -> TimelineSequence:
    """Create an empty timeline for testing."""
    return TimelineSequence(
        timeline_id=str(uuid.uuid4()),
        name="Test Timeline",
        fps=24,
        width=1920,
        height=1080,
        sample_rate=48000,
    )


@pytest.fixture
def timeline_with_track(empty_timeline: TimelineSequence) -> TimelineSequence:
    """Create a timeline with one video track."""
    track = EditTrack(
        track_id="track_1",
        name="Video 1",
        track_type="video",
        index=0,
    )
    return empty_timeline.with_track_added(track)


@pytest.fixture
def timeline_with_two_tracks(empty_timeline: TimelineSequence) -> TimelineSequence:
    """Create a timeline with two video tracks, each with non-default volume."""
    track1 = EditTrack(
        track_id="track_1",
        name="Video 1",
        track_type="video",
        index=0,
        volume=1.5,
    )
    track2 = EditTrack(
        track_id="track_2",
        name="Video 2",
        track_type="video",
        index=1,
        volume=0.8,
    )
    tl = empty_timeline.with_track_added(track1)
    return tl.with_track_added(track2)


@pytest.fixture
def sample_clip() -> ClipInstance:
    """Create a sample clip for testing."""
    return ClipInstance(
        clip_id="clip_1",
        asset_id="asset_1",
        track_id="track_1",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        speed=SpeedParams(speed=1.0),
        transform=TransformParams(),
        crop=CropParams(),
        opacity=1.0,
    )


@pytest.fixture
def second_clip() -> ClipInstance:
    """Create a second clip for testing."""
    return ClipInstance(
        clip_id="clip_2",
        asset_id="asset_2",
        track_id="track_1",
        timeline_start=100,
        timeline_duration=50,
        source_start=0,
        source_duration=50,
        speed=SpeedParams(speed=1.0),
        transform=TransformParams(),
        crop=CropParams(),
        opacity=1.0,
    )


@pytest.fixture
def third_clip() -> ClipInstance:
    """Create a third clip for testing."""
    return ClipInstance(
        clip_id="clip_3",
        asset_id="asset_3",
        track_id="track_1",
        timeline_start=150,
        timeline_duration=50,
        source_start=0,
        source_duration=50,
        speed=SpeedParams(speed=1.0),
        transform=TransformParams(),
        crop=CropParams(),
        opacity=1.0,
    )


@pytest.fixture
def timeline_with_clip(timeline_with_track: TimelineSequence, sample_clip: ClipInstance) -> TimelineSequence:
    """Create a timeline with one clip."""
    return timeline_with_track.with_clip_added("track_1", sample_clip)


@pytest.fixture
def timeline_with_two_clips(
    timeline_with_track: TimelineSequence,
    sample_clip: ClipInstance,
    second_clip: ClipInstance
) -> TimelineSequence:
    """Create a timeline with two clips."""
    tl = timeline_with_track.with_clip_added("track_1", sample_clip)
    return tl.with_clip_added("track_1", second_clip)


@pytest.fixture
def timeline_with_three_clips(
    timeline_with_track: TimelineSequence,
    sample_clip: ClipInstance,
    second_clip: ClipInstance,
    third_clip: ClipInstance
) -> TimelineSequence:
    """Create a timeline with three clips."""
    tl = timeline_with_track.with_clip_added("track_1", sample_clip)
    tl = tl.with_clip_added("track_1", second_clip)
    return tl.with_clip_added("track_1", third_clip)


# ================================================================
# Bug 1: Track commands return updated_command
# ================================================================

class TestSetTrackMuteCommand:
    """Tests for SetTrackMuteCommand undo/redo regression."""

    def test_apply_returns_tuple(self, timeline_with_track: TimelineSequence) -> None:
        """apply() should return (timeline, updated_cmd) tuple."""
        cmd = SetTrackMuteCommand.create("track_1", True)
        result = cmd.apply(timeline_with_track)
        assert isinstance(result, tuple)
        new_timeline, updated_cmd = result
        assert updated_cmd._old_mute is False

    def test_apply_undo_restores_mute(self, timeline_with_track: TimelineSequence) -> None:
        """After apply + undo, original mute state should be restored."""
        cmd = SetTrackMuteCommand.create("track_1", True)
        result = cmd.apply(timeline_with_track)
        new_timeline, updated_cmd = result

        # Verify mute is now True
        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        assert track.is_muted is True

        # Undo
        restored = updated_cmd.undo(new_timeline)
        track = restored.get_track_by_id("track_1")
        assert track is not None
        assert track.is_muted is False

    def test_command_stack_undo_restores_mute(self, timeline_with_track: TimelineSequence) -> None:
        """CommandStack undo should restore original mute state."""
        stack = CommandStack()
        cmd = SetTrackMuteCommand.create("track_1", True)
        tl = stack.execute(cmd, timeline_with_track)

        track = tl.get_track_by_id("track_1")
        assert track is not None
        assert track.is_muted is True

        tl = stack.undo(tl)
        assert tl is not None
        track = tl.get_track_by_id("track_1")
        assert track is not None
        assert track.is_muted is False


class TestSetTrackSoloCommand:
    """Tests for SetTrackSoloCommand undo/redo regression."""

    def test_apply_returns_tuple(self, timeline_with_track: TimelineSequence) -> None:
        """apply() should return (timeline, updated_cmd) tuple."""
        cmd = SetTrackSoloCommand.create("track_1", True)
        result = cmd.apply(timeline_with_track)
        assert isinstance(result, tuple)
        new_timeline, updated_cmd = result
        assert updated_cmd._old_solo is False

    def test_apply_undo_restores_solo(self, timeline_with_track: TimelineSequence) -> None:
        """After apply + undo, original solo state should be restored."""
        cmd = SetTrackSoloCommand.create("track_1", True)
        result = cmd.apply(timeline_with_track)
        new_timeline, updated_cmd = result

        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        assert track.is_solo is True

        restored = updated_cmd.undo(new_timeline)
        track = restored.get_track_by_id("track_1")
        assert track is not None
        assert track.is_solo is False

    def test_command_stack_undo_restores_solo(self, timeline_with_track: TimelineSequence) -> None:
        """CommandStack undo should restore original solo state."""
        stack = CommandStack()
        cmd = SetTrackSoloCommand.create("track_1", True)
        tl = stack.execute(cmd, timeline_with_track)

        track = tl.get_track_by_id("track_1")
        assert track.is_solo is True

        tl = stack.undo(tl)
        assert tl is not None
        track = tl.get_track_by_id("track_1")
        assert track.is_solo is False


class TestSetTrackLockCommand:
    """Tests for SetTrackLockCommand undo/redo regression."""

    def test_apply_returns_tuple(self, timeline_with_track: TimelineSequence) -> None:
        """apply() should return (timeline, updated_cmd) tuple."""
        cmd = SetTrackLockCommand.create("track_1", True)
        result = cmd.apply(timeline_with_track)
        assert isinstance(result, tuple)
        new_timeline, updated_cmd = result
        assert updated_cmd._old_lock is False

    def test_apply_undo_restores_lock(self, timeline_with_track: TimelineSequence) -> None:
        """After apply + undo, original lock state should be restored."""
        cmd = SetTrackLockCommand.create("track_1", True)
        result = cmd.apply(timeline_with_track)
        new_timeline, updated_cmd = result

        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        assert track.is_locked is True

        restored = updated_cmd.undo(new_timeline)
        track = restored.get_track_by_id("track_1")
        assert track is not None
        assert track.is_locked is False

    def test_command_stack_undo_restores_lock(self, timeline_with_track: TimelineSequence) -> None:
        """CommandStack undo should restore original lock state."""
        stack = CommandStack()
        cmd = SetTrackLockCommand.create("track_1", True)
        tl = stack.execute(cmd, timeline_with_track)

        track = tl.get_track_by_id("track_1")
        assert track.is_locked is True

        tl = stack.undo(tl)
        assert tl is not None
        track = tl.get_track_by_id("track_1")
        assert track.is_locked is False


class TestSetTrackVolumeCommand:
    """Tests for SetTrackVolumeCommand undo/redo regression."""

    def test_apply_returns_tuple(self, timeline_with_track: TimelineSequence) -> None:
        """apply() should return (timeline, updated_cmd) tuple."""
        cmd = SetTrackVolumeCommand.create("track_1", 1.5)
        result = cmd.apply(timeline_with_track)
        assert isinstance(result, tuple)
        new_timeline, updated_cmd = result
        assert updated_cmd._old_volume == 1.0

    def test_apply_undo_restores_volume(self, timeline_with_track: TimelineSequence) -> None:
        """After apply + undo, original volume should be restored."""
        cmd = SetTrackVolumeCommand.create("track_1", 1.5)
        result = cmd.apply(timeline_with_track)
        new_timeline, updated_cmd = result

        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        assert track.volume == 1.5

        restored = updated_cmd.undo(new_timeline)
        track = restored.get_track_by_id("track_1")
        assert track is not None
        assert track.volume == 1.0

    def test_command_stack_undo_restores_volume(self, timeline_with_track: TimelineSequence) -> None:
        """CommandStack undo should restore original volume."""
        stack = CommandStack()
        cmd = SetTrackVolumeCommand.create("track_1", 1.5)
        tl = stack.execute(cmd, timeline_with_track)

        track = tl.get_track_by_id("track_1")
        assert track.volume == 1.5

        tl = stack.undo(tl)
        assert tl is not None
        track = tl.get_track_by_id("track_1")
        assert track.volume == 1.0


class TestSetTrackPanCommand:
    """Tests for SetTrackPanCommand undo/redo regression."""

    def test_apply_returns_tuple(self, timeline_with_track: TimelineSequence) -> None:
        """apply() should return (timeline, updated_cmd) tuple with old_pan stored."""
        cmd = SetTrackPanCommand.create("track_1", 0.5)
        result = cmd.apply(timeline_with_track)
        assert isinstance(result, tuple)
        new_timeline, updated_cmd = result
        assert updated_cmd._old_pan == 0.0  # default pan

    def test_apply_undo_restores_pan(self, timeline_with_track: TimelineSequence) -> None:
        """After apply + undo, original pan should be restored."""
        cmd = SetTrackPanCommand.create("track_1", -0.7)
        result = cmd.apply(timeline_with_track)
        new_timeline, updated_cmd = result

        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        assert track.pan == -0.7

        restored = updated_cmd.undo(new_timeline)
        track = restored.get_track_by_id("track_1")
        assert track is not None
        assert track.pan == 0.0

    def test_command_stack_undo_restores_pan(self, timeline_with_track: TimelineSequence) -> None:
        """CommandStack undo should restore original pan."""
        stack = CommandStack()
        cmd = SetTrackPanCommand.create("track_1", 0.3)
        tl = stack.execute(cmd, timeline_with_track)

        track = tl.get_track_by_id("track_1")
        assert track.pan == 0.3

        tl = stack.undo(tl)
        assert tl is not None
        track = tl.get_track_by_id("track_1")
        assert track.pan == 0.0

    def test_pan_clamped_to_range(self, timeline_with_track: TimelineSequence) -> None:
        """Pan values outside [-1.0, 1.0] should be clamped on apply."""
        cmd = SetTrackPanCommand.create("track_1", 5.0)  # over-range
        new_timeline, _ = cmd.apply(timeline_with_track)
        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        assert track.pan == 1.0

    def test_pan_serialization_roundtrip(self) -> None:
        """SetTrackPanCommand to_dict/from_dict roundtrip."""
        cmd = SetTrackPanCommand.create("track_1", 0.25)
        cmd_with_old = SetTrackPanCommand(
            command_id=cmd.command_id, label=cmd.label, track_id="track_1",
            pan=0.25, _old_pan=0.0,
        )
        d = cmd_with_old.to_dict()
        assert d["command_type"] == "SetTrackPanCommand"
        assert d["pan"] == 0.25
        assert d["old_pan"] == 0.0
        restored = SetTrackPanCommand.from_dict(d)
        assert restored.pan == 0.25
        assert restored._old_pan == 0.0

    def test_volume_preserved_when_setting_pan(self, timeline_with_two_tracks: TimelineSequence) -> None:
        """Setting pan must not reset volume (regression guard for field copy)."""
        # track_1 has volume=1.5
        cmd = SetTrackPanCommand.create("track_1", -0.4)
        new_timeline, _ = cmd.apply(timeline_with_two_tracks)
        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        assert track.pan == -0.4
        assert track.volume == 1.5  # preserved


class TestRenameTrackCommand:
    """Tests for RenameTrackCommand undo/redo regression."""

    def test_apply_returns_tuple(self, timeline_with_track: TimelineSequence) -> None:
        """apply() should return (timeline, updated_cmd) tuple."""
        cmd = RenameTrackCommand.create("track_1", "New Name")
        result = cmd.apply(timeline_with_track)
        assert isinstance(result, tuple)
        new_timeline, updated_cmd = result
        assert updated_cmd._old_name == "Video 1"

    def test_apply_undo_restores_name(self, timeline_with_track: TimelineSequence) -> None:
        """After apply + undo, original name and volume should be restored."""
        cmd = RenameTrackCommand.create("track_1", "New Name")
        result = cmd.apply(timeline_with_track)
        new_timeline, updated_cmd = result

        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        assert track.name == "New Name"
        # volume should be preserved
        assert track.volume == 1.0

        restored = updated_cmd.undo(new_timeline)
        track = restored.get_track_by_id("track_1")
        assert track is not None
        assert track.name == "Video 1"
        assert track.volume == 1.0

    def test_command_stack_undo_restores_name(self, timeline_with_track: TimelineSequence) -> None:
        """CommandStack undo should restore original name."""
        stack = CommandStack()
        cmd = RenameTrackCommand.create("track_1", "New Name")
        tl = stack.execute(cmd, timeline_with_track)

        track = tl.get_track_by_id("track_1")
        assert track.name == "New Name"

        tl = stack.undo(tl)
        assert tl is not None
        track = tl.get_track_by_id("track_1")
        assert track.name == "Video 1"

    def test_volume_preserved_after_rename(self, timeline_with_two_tracks: TimelineSequence) -> None:
        """Volume should not be lost when renaming a track."""
        # track_1 has volume=1.5
        cmd = RenameTrackCommand.create("track_1", "Renamed")
        result = cmd.apply(timeline_with_two_tracks)
        new_timeline, updated_cmd = result

        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        assert track.name == "Renamed"
        assert track.volume == 1.5  # NOT reset to 1.0

        # Undo should also preserve volume
        restored = updated_cmd.undo(new_timeline)
        track = restored.get_track_by_id("track_1")
        assert track is not None
        assert track.volume == 1.5


# ================================================================
# Bug 2/3: Volume preserved in EditTrack reconstruction
# ================================================================

class TestAddTrackCommand:
    """Tests for AddTrackCommand undo/redo regression."""

    def test_undo_removes_track(self, empty_timeline: TimelineSequence) -> None:
        """Undo should remove the added track."""
        cmd = AddTrackCommand.create("video", "Test Track")
        result = cmd.apply(empty_timeline)
        assert isinstance(result, tuple)
        new_timeline, updated_cmd = result

        assert len(new_timeline.video_tracks) == 1
        assert new_timeline.video_tracks[0].name == "Test Track"

        restored = updated_cmd.undo(new_timeline)
        assert len(restored.video_tracks) == 0

    def test_new_track_has_default_volume(self, empty_timeline: TimelineSequence) -> None:
        """New track should have volume=1.0."""
        cmd = AddTrackCommand.create("video", "Test Track")
        result = cmd.apply(empty_timeline)
        new_timeline = result[0]

        assert len(new_timeline.video_tracks) == 1
        assert new_timeline.video_tracks[0].volume == 1.0

    def test_existing_track_volume_preserved_on_add(self, timeline_with_two_tracks: TimelineSequence) -> None:
        """Adding a track should not reset existing track volumes."""
        # track_1 has volume=1.5, track_2 has volume=0.8
        cmd = AddTrackCommand.create("video", "V3", position=2)
        new_timeline = cmd.apply(timeline_with_two_tracks)[0]

        # Original tracks should keep their volumes
        t1 = new_timeline.get_track_by_id("track_1")
        t2 = new_timeline.get_track_by_id("track_2")
        assert t1 is not None and t1.volume == 1.5
        assert t2 is not None and t2.volume == 0.8

        # New track should have default volume
        assert len(new_timeline.video_tracks) == 3
        assert new_timeline.video_tracks[2].volume == 1.0

    def test_undo_reindex_preserves_volume(self, timeline_with_two_tracks: TimelineSequence) -> None:
        """Undo add track should preserve volumes when reindexing."""
        cmd = AddTrackCommand.create("video", "V3", position=2)
        new_timeline, updated_cmd = cmd.apply(timeline_with_two_tracks)
        assert len(new_timeline.video_tracks) == 3

        restored = updated_cmd.undo(new_timeline)
        assert len(restored.video_tracks) == 2

        t1 = restored.get_track_by_id("track_1")
        t2 = restored.get_track_by_id("track_2")
        assert t1 is not None and t1.volume == 1.5
        assert t2 is not None and t2.volume == 0.8


class TestDeleteTrackCommand:
    """Tests for DeleteTrackCommand undo/redo regression."""

    def test_undo_restores_track_with_all_fields(self, timeline_with_two_tracks: TimelineSequence) -> None:
        """Undo should restore deleted track with all fields including volume."""
        # track_2 has volume=0.8
        cmd = DeleteTrackCommand.create("track_2")
        result = cmd.apply(timeline_with_two_tracks)

        # Command now returns (timeline, updated_cmd) tuple
        assert isinstance(result, tuple)
        new_timeline, updated_cmd = result
        assert len(new_timeline.video_tracks) == 1

        # Undo using the updated command
        restored = updated_cmd.undo(new_timeline)
        assert len(restored.video_tracks) == 2

        # Find restored track
        restored_track = restored.get_track_by_id("track_2")
        assert restored_track is not None
        assert restored_track.volume == 0.8
        assert restored_track.name == "Video 2"

    def test_remaining_track_volume_preserved_after_delete(self, timeline_with_two_tracks: TimelineSequence) -> None:
        """Deleting a track should not reset remaining track volumes."""
        cmd = DeleteTrackCommand.create("track_2")
        new_timeline = cmd.apply(timeline_with_two_tracks)[0]

        t1 = new_timeline.get_track_by_id("track_1")
        assert t1 is not None
        assert t1.volume == 1.5


# ================================================================
# Bug 4: MacroCommand propagates updated sub-commands
# ================================================================

class TestMacroCommand:
    """Tests for MacroCommand undo/redo regression."""

    def test_macro_with_track_commands_undo_restores_all(self, timeline_with_track: TimelineSequence) -> None:
        """MacroCommand should propagate updated sub-commands for proper undo."""
        mute_cmd = SetTrackMuteCommand.create("track_1", True)
        solo_cmd = SetTrackSoloCommand.create("track_1", True)

        macro = MacroCommand.create(
            "Mute and Solo",
            [mute_cmd, solo_cmd],
        )

        # Apply through CommandStack
        stack = CommandStack()
        tl = stack.execute(macro, timeline_with_track)

        track = tl.get_track_by_id("track_1")
        assert track is not None
        assert track.is_muted is True
        assert track.is_solo is True

        # Undo
        tl = stack.undo(tl)
        assert tl is not None
        track = tl.get_track_by_id("track_1")
        assert track is not None
        assert track.is_muted is False
        assert track.is_solo is False

    def test_macro_apply_returns_tuple(self, timeline_with_track: TimelineSequence) -> None:
        """MacroCommand.apply() should return (timeline, updated_macro)."""
        mute_cmd = SetTrackMuteCommand.create("track_1", True)
        macro = MacroCommand.create("Mute", [mute_cmd])

        result = macro.apply(timeline_with_track)
        assert isinstance(result, tuple)
        new_timeline, updated_macro = result

        # The updated macro should have updated sub-commands
        assert len(updated_macro.sub_commands) == 1
        assert updated_macro.sub_commands[0]._old_mute is False

    def test_macro_with_volume_and_rename_undo(self, timeline_with_two_tracks: TimelineSequence) -> None:
        """MacroCommand should undo volume and rename correctly."""
        vol_cmd = SetTrackVolumeCommand.create("track_1", 0.5)
        rename_cmd = RenameTrackCommand.create("track_1", "Renamed")

        macro = MacroCommand.create(
            "Volume + Rename",
            [vol_cmd, rename_cmd],
        )

        stack = CommandStack()
        tl = stack.execute(macro, timeline_with_two_tracks)

        track = tl.get_track_by_id("track_1")
        assert track is not None
        assert track.volume == 0.5
        assert track.name == "Renamed"

        # Undo
        tl = stack.undo(tl)
        assert tl is not None
        track = tl.get_track_by_id("track_1")
        assert track is not None
        assert track.volume == 1.5  # Original
        assert track.name == "Video 1"  # Original


# ================================================================
# Bug 2: MoveClipToTrackCommand volume preservation
# ================================================================

class TestMoveClipToTrackCommand:
    """Tests for MoveClipToTrackCommand volume preservation regression."""

    def test_new_video_track_preserves_subtitle_tracks_and_format_settings(self) -> None:
        """Auto-created video tracks must not discard subtitle timeline state."""
        video_clip = ClipInstance(
            clip_id="video-clip",
            asset_id="asset-video",
            track_id="v1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
        )
        subtitle_clip = SubtitleClip(
            clip_id="subtitle-clip",
            track_id="s1",
            timeline_start=0,
            timeline_duration=100,
            text="Keep this subtitle",
        )
        timeline = TimelineSequence(
            timeline_id="subtitle-preservation",
            name="Subtitle preservation",
            fps=24,
            format_settings={"preview_quality": "half"},
            video_tracks=(
                EditTrack(
                    track_id="v1",
                    name="V1",
                    track_type="video",
                    clips=(video_clip,),
                ),
            ),
            subtitle_tracks=(
                EditTrack(
                    track_id="s1",
                    name="S1",
                    track_type="subtitle",
                    subtitle_clips=(subtitle_clip,),
                ),
            ),
        )
        command = MoveClipToTrackCommand.create_for_new_track(
            source_track_id="v1",
            clip_id="video-clip",
            target_track_type="video",
            new_track_position="above",
            new_start=12,
            old_start=0,
        )

        moved = command.apply(timeline)
        assert moved.subtitle_tracks == timeline.subtitle_tracks
        assert moved.format_settings == timeline.format_settings

        restored = command.undo(moved)
        assert restored.subtitle_tracks == timeline.subtitle_tracks
        assert restored.format_settings == timeline.format_settings

    def test_undo_restores_clip_and_volumes(
        self, timeline_with_two_tracks: TimelineSequence
    ) -> None:
        """Undo should restore clip and preserve all track volumes."""
        # Add clip to track_1
        clip = ClipInstance(
            clip_id="clip_1",
            asset_id="asset_1",
            track_id="track_1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            speed=SpeedParams(speed=1.0),
        )
        tl = timeline_with_two_tracks.with_clip_added("track_1", clip)

        # Move clip to a new track
        cmd = MoveClipToTrackCommand(
            command_id=str(uuid.uuid4()),
            label="Move to new track",
            source_track_id="track_1",
            clip_id="clip_1",
            target_track_type="video",
            create_new_track=True,
            new_track_position="above",
            new_timeline_start=0,
            old_timeline_start=0,
        )
        result = cmd.apply(tl)
        new_timeline = result if not isinstance(result, tuple) else result[0]

        # Verify new track created
        assert len(new_timeline.video_tracks) == 3

        # Original track_1 should still have volume=1.5
        t1 = new_timeline.get_track_by_id("track_1")
        assert t1 is not None
        assert t1.volume == 1.5

        # track_2 should still have volume=0.8
        t2 = new_timeline.get_track_by_id("track_2")
        assert t2 is not None
        assert t2.volume == 0.8

        # Undo
        restored = cmd.undo(new_timeline)
        assert len(restored.video_tracks) == 2

        # After undo, volumes should be preserved
        t1 = restored.get_track_by_id("track_1")
        t2 = restored.get_track_by_id("track_2")
        assert t1 is not None and t1.volume == 1.5
        assert t2 is not None and t2.volume == 0.8


class TestMoveClipToExistingTrackCommand:
    """Tests for MoveClipToExistingTrackCommand volume preservation regression."""

    def test_volumes_preserved_on_both_tracks(
        self, timeline_with_two_tracks: TimelineSequence
    ) -> None:
        """Volumes should be preserved on both source and target tracks."""
        # Add clip to track_1
        clip = ClipInstance(
            clip_id="clip_1",
            asset_id="asset_1",
            track_id="track_1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            speed=SpeedParams(speed=1.0),
        )
        tl = timeline_with_two_tracks.with_clip_added("track_1", clip)

        # Move clip from track_1 to track_2
        cmd = MoveClipToExistingTrackCommand(
            command_id=str(uuid.uuid4()),
            label="Move to existing track",
            source_track_id="track_1",
            target_track_id="track_2",
            clip_id="clip_1",
            new_timeline_start=0,
            old_timeline_start=0,
        )
        new_timeline = cmd.apply(tl)

        # Both tracks should have their original volumes
        t1 = new_timeline.get_track_by_id("track_1")
        t2 = new_timeline.get_track_by_id("track_2")
        assert t1 is not None
        assert t1.volume == 1.5
        assert t2 is not None
        assert t2.volume == 0.8

        # Undo
        restored = cmd.undo(new_timeline)
        t1 = restored.get_track_by_id("track_1")
        t2 = restored.get_track_by_id("track_2")
        assert t1 is not None
        assert t1.volume == 1.5
        assert t2 is not None
        assert t2.volume == 0.8


# ================================================================
# Bug 5: SlipClipCommand returns updated command
# ================================================================

class TestSlipClipCommand:
    """Tests for SlipClipCommand undo/redo regression."""

    def test_apply_returns_tuple(self, timeline_with_clip: TimelineSequence) -> None:
        """apply() should return (timeline, updated_cmd) tuple."""
        cmd = SlipClipCommand.create("track_1", "clip_1", 10, max_source_frames=200)
        result = cmd.apply(timeline_with_clip)
        assert isinstance(result, tuple)
        new_timeline, updated_cmd = result
        assert updated_cmd.old_source_start == 0

    def test_apply_undo_restores_source_start(self, timeline_with_clip: TimelineSequence) -> None:
        """After apply + undo, original source_start should be restored."""
        cmd = SlipClipCommand.create("track_1", "clip_1", 10, max_source_frames=200)
        result = cmd.apply(timeline_with_clip)
        new_timeline, updated_cmd = result

        # Verify source_start moved
        track = new_timeline.get_track_by_id("track_1")
        clip = track.get_clip_by_id("clip_1")
        assert clip.source_start == 10

        # Undo
        restored = updated_cmd.undo(new_timeline)
        track = restored.get_track_by_id("track_1")
        clip = track.get_clip_by_id("clip_1")
        assert clip.source_start == 0

    def test_command_stack_undo_restores_source_start(self, timeline_with_clip: TimelineSequence) -> None:
        """CommandStack undo should restore original source_start."""
        stack = CommandStack()
        cmd = SlipClipCommand.create("track_1", "clip_1", 10, max_source_frames=200)
        tl = stack.execute(cmd, timeline_with_clip)

        clip = tl.get_clip_by_id("clip_1")
        assert clip.source_start == 10

        tl = stack.undo(tl)
        assert tl is not None
        clip = tl.get_clip_by_id("clip_1")
        assert clip.source_start == 0


# ================================================================
# Bug 6: RippleDeleteCommand returns updated command
# ================================================================

class TestRippleDeleteCommand:
    """Tests for RippleDeleteCommand undo/redo regression."""

    def test_apply_returns_tuple(self, timeline_with_three_clips: TimelineSequence) -> None:
        """apply() should return (timeline, updated_cmd) tuple."""
        cmd = RippleDeleteCommand.create(timeline_with_three_clips, "track_1", "clip_1")
        result = cmd.apply(timeline_with_three_clips)
        assert isinstance(result, tuple)
        new_timeline, updated_cmd = result
        assert updated_cmd._deleted_clip is not None
        assert len(updated_cmd._moved_clips) == 2  # clip_2 and clip_3 moved

    def test_undo_restores_deleted_and_moved_clips(self, timeline_with_three_clips: TimelineSequence) -> None:
        """Undo should restore deleted clip and move clips back."""
        cmd = RippleDeleteCommand.create(timeline_with_three_clips, "track_1", "clip_1")
        result = cmd.apply(timeline_with_three_clips)
        new_timeline, updated_cmd = result

        # After delete, clip_2 should be at start=0, clip_3 at start=50
        track = new_timeline.get_track_by_id("track_1")
        assert len(track.clips) == 2
        clip2 = track.get_clip_by_id("clip_2")
        clip3 = track.get_clip_by_id("clip_3")
        assert clip2.timeline_start == 0  # Moved from 100 to 0
        assert clip3.timeline_start == 50  # Moved from 150 to 50

        # Undo
        restored = updated_cmd.undo(new_timeline)
        track = restored.get_track_by_id("track_1")
        assert len(track.clips) == 3

        # Verify all clips restored at original positions
        clip1 = track.get_clip_by_id("clip_1")
        clip2 = track.get_clip_by_id("clip_2")
        clip3 = track.get_clip_by_id("clip_3")
        assert clip1 is not None
        assert clip1.timeline_start == 0
        assert clip2.timeline_start == 100
        assert clip3.timeline_start == 150

    def test_command_stack_undo_restores_ripple_delete(self, timeline_with_three_clips: TimelineSequence) -> None:
        """CommandStack undo should restore all clips after ripple delete."""
        stack = CommandStack()
        cmd = RippleDeleteCommand.create(timeline_with_three_clips, "track_1", "clip_1")
        tl = stack.execute(cmd, timeline_with_three_clips)

        track = tl.get_track_by_id("track_1")
        assert len(track.clips) == 2

        tl = stack.undo(tl)
        assert tl is not None
        track = tl.get_track_by_id("track_1")
        assert len(track.clips) == 3
        assert track.get_clip_by_id("clip_1") is not None


# ================================================================
# Bug 7: SplitClipCommand speed handling
# ================================================================

class TestSplitClipCommand:
    """Tests for SplitClipCommand speed handling regression."""

    def test_split_with_speed_params(
        self, timeline_with_clip: TimelineSequence
    ) -> None:
        """Split should work with SpeedParams speed."""
        cmd = SplitClipCommand.create("track_1", "clip_1", 50)
        result = cmd.apply(timeline_with_clip)
        new_timeline = result if not isinstance(result, tuple) else result[0]

        track = new_timeline.get_track_by_id("track_1")
        assert len(track.clips) == 2

    def test_split_with_float_speed(self, timeline_with_track: TimelineSequence) -> None:
        """Split should work with float speed (not SpeedParams)."""
        clip = ClipInstance(
            clip_id="clip_float",
            asset_id="asset_float",
            track_id="track_1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            speed=1.0,  # float, not SpeedParams
        )
        tl = timeline_with_track.with_clip_added("track_1", clip)

        cmd = SplitClipCommand.create("track_1", "clip_float", 50)
        result = cmd.apply(tl)
        new_timeline = result if not isinstance(result, tuple) else result[0]

        track = new_timeline.get_track_by_id("track_1")
        assert len(track.clips) == 2

        # Verify durations
        left, right = track.clips[0], track.clips[1]
        assert left.timeline_duration == 50
        assert right.timeline_duration == 50

    def test_split_undo_with_speed_params(
        self, timeline_with_clip: TimelineSequence
    ) -> None:
        """Split + undo with SpeedParams should restore original clip."""
        cmd = SplitClipCommand.create("track_1", "clip_1", 50)
        result = cmd.apply(timeline_with_clip)
        new_timeline = result if not isinstance(result, tuple) else result[0]

        track = new_timeline.get_track_by_id("track_1")
        assert len(track.clips) == 2

        # Undo using _original_clip path (SplitClipCommand stores it via object.__setattr__)
        restored = cmd.undo(new_timeline)
        track = restored.get_track_by_id("track_1")
        assert len(track.clips) == 1
        clip = track.clips[0]
        assert clip.clip_id == "clip_1"
        assert clip.timeline_duration == 100

    def test_split_with_fast_speed(self, timeline_with_track: TimelineSequence) -> None:
        """Split should work with fast speed (2x)."""
        clip = ClipInstance(
            clip_id="clip_fast",
            asset_id="asset_fast",
            track_id="track_1",
            timeline_start=0,
            timeline_duration=50,
            source_start=0,
            source_duration=100,
            speed=SpeedParams(speed=2.0),
        )
        tl = timeline_with_track.with_clip_added("track_1", clip)

        cmd = SplitClipCommand.create("track_1", "clip_fast", 25)
        result = cmd.apply(tl)
        new_timeline = result if not isinstance(result, tuple) else result[0]

        track = new_timeline.get_track_by_id("track_1")
        assert len(track.clips) == 2
        # Left clip covers 0-25 timeline frames = 50 source frames at 2x
        # Right clip covers 25-50 timeline frames = 50 source frames at 2x
        left, right = track.clips[0], track.clips[1]
        assert left.timeline_duration == 25
        assert right.timeline_duration == 25
        assert left.source_duration == 50
        assert right.source_duration == 50
