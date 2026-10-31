"""
Edit Commands Tests Module - Test Command Pattern Implementation (P4).

Tests for:
- AddClipCommand: Add and undo
- DeleteClipCommand: Delete and undo
- MoveClipCommand: Move and undo
- TrimClipCommand: Trim and undo
- SplitClipCommand: Split and undo
- CommandStack: Undo/redo management

Reference:
- docs/edit_page_timeline_upgrade_plan_2026-05-17.md P4
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import uuid

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    EditTrack,
    ClipInstance,
    TransitionInstance,
)
from src.features.edit.core.clip_params import TransformParams, CropParams, SpeedParams
from src.features.edit.core.commands import (
    AddClipCommand,
    DeleteClipCommand,
    MoveClipCommand,
    TrimClipCommand,
    RollTrimCommand,
    SplitClipCommand,
    CommandStack,
)


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
        timeline_start=100,  # After first clip
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
    timeline_with_clip: TimelineSequence,
    second_clip: ClipInstance
) -> TimelineSequence:
    """Create a timeline with two clips."""
    return timeline_with_clip.with_clip_added("track_1", second_clip)


# ================================================================
# AddClipCommand Tests
# ================================================================

class TestAddClipCommand:
    """Tests for AddClipCommand."""

    def test_create_add_clip_command(self) -> None:
        """Test command factory creation."""
        clip = ClipInstance(
            clip_id="test_clip",
            asset_id="asset_1",
            track_id="track_1",
            timeline_start=0,
            timeline_duration=50,
            source_start=0,
            source_duration=50,
        )

        cmd = AddClipCommand.create("track_1", clip)

        assert cmd.command_id is not None
        assert cmd.track_id == "track_1"
        assert cmd.clip.clip_id == "test_clip"

    def test_apply_add_clip(
        self,
        timeline_with_track: TimelineSequence,
        sample_clip: ClipInstance
    ) -> None:
        """Test applying AddClipCommand."""
        cmd = AddClipCommand.create("track_1", sample_clip)

        new_timeline = cmd.apply(timeline_with_track)

        # Verify clip was added
        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        assert len(track.clips) == 1
        assert track.clips[0].clip_id == "clip_1"

    def test_undo_add_clip(
        self,
        timeline_with_track: TimelineSequence,
        sample_clip: ClipInstance
    ) -> None:
        """Test undoing AddClipCommand."""
        cmd = AddClipCommand.create("track_1", sample_clip)

        # Apply
        new_timeline = cmd.apply(timeline_with_track)

        # Undo
        restored_timeline = cmd.undo(new_timeline)

        # Verify clip was removed
        track = restored_timeline.get_track_by_id("track_1")
        assert track is not None
        assert len(track.clips) == 0

    def test_add_clip_overlap_error(
        self,
        timeline_with_clip: TimelineSequence,
        sample_clip: ClipInstance
    ) -> None:
        """Test that adding overlapping clip raises error."""
        # Create overlapping clip
        overlap_clip = ClipInstance(
            clip_id="overlap_clip",
            asset_id="asset_2",
            track_id="track_1",
            timeline_start=50,  # Overlaps with clip_1 [0, 100)
            timeline_duration=50,
            source_start=0,
            source_duration=50,
        )

        cmd = AddClipCommand.create("track_1", overlap_clip)

        with pytest.raises(ValueError, match="overlap"):
            cmd.apply(timeline_with_clip)


# ================================================================
# DeleteClipCommand Tests
# ================================================================

class TestDeleteClipCommand:
    """Tests for DeleteClipCommand."""

    def test_create_delete_clip_command(self) -> None:
        """Test command factory creation."""
        cmd = DeleteClipCommand.create("track_1", "clip_1")

        assert cmd.command_id is not None
        assert cmd.track_id == "track_1"
        assert cmd.clip_id == "clip_1"

    def test_apply_delete_clip(
        self,
        timeline_with_clip: TimelineSequence
    ) -> None:
        """Test applying DeleteClipCommand."""
        cmd = DeleteClipCommand.create("track_1", "clip_1")

        result = cmd.apply(timeline_with_clip)

        # Handle tuple return (timeline, updated_command)
        if isinstance(result, tuple):
            new_timeline = result[0]
        else:
            new_timeline = result

        # Verify clip was deleted
        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        assert len(track.clips) == 0

    def test_undo_delete_clip(
        self,
        timeline_with_clip: TimelineSequence
    ) -> None:
        """Test undoing DeleteClipCommand."""
        cmd = DeleteClipCommand.create("track_1", "clip_1")

        # Apply and get updated command with stored clip
        result = cmd.apply(timeline_with_clip)
        if isinstance(result, tuple):
            new_timeline, updated_cmd = result
        else:
            new_timeline = result
            updated_cmd = cmd

        # Undo
        restored_timeline = updated_cmd.undo(new_timeline)

        # Verify clip was restored
        track = restored_timeline.get_track_by_id("track_1")
        assert track is not None
        assert len(track.clips) == 1
        assert track.clips[0].clip_id == "clip_1"

    def test_delete_nonexistent_clip_error(
        self,
        timeline_with_clip: TimelineSequence
    ) -> None:
        """Test that deleting nonexistent clip raises error."""
        cmd = DeleteClipCommand.create("track_1", "nonexistent")

        with pytest.raises(ValueError, match="Clip not found"):
            cmd.apply(timeline_with_clip)


# ================================================================
# MoveClipCommand Tests
# ================================================================

class TestMoveClipCommand:
    """Tests for MoveClipCommand."""

    def test_create_move_clip_command(self) -> None:
        """Test command factory creation."""
        cmd = MoveClipCommand.create("track_1", "clip_1", 200)

        assert cmd.command_id is not None
        assert cmd.new_timeline_start == 200

    def test_apply_move_clip(
        self,
        timeline_with_two_clips: TimelineSequence
    ) -> None:
        """Test applying MoveClipCommand."""
        # Move second clip to position 200
        cmd = MoveClipCommand.create("track_1", "clip_2", 200)

        new_timeline = cmd.apply(timeline_with_two_clips)

        # Verify clip moved
        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        clip_2 = track.get_clip_by_id("clip_2")
        assert clip_2 is not None
        assert clip_2.timeline_start == 200

    def test_undo_move_clip(
        self,
        timeline_with_two_clips: TimelineSequence
    ) -> None:
        """Test undoing MoveClipCommand."""
        # Move clip and store old position
        cmd = MoveClipCommand.create("track_1", "clip_2", 200, old_start=100)

        new_timeline = cmd.apply(timeline_with_two_clips)

        # Undo
        restored_timeline = cmd.undo(new_timeline)

        # Verify clip restored to original position
        track = restored_timeline.get_track_by_id("track_1")
        assert track is not None
        clip_2 = track.get_clip_by_id("clip_2")
        assert clip_2 is not None
        assert clip_2.timeline_start == 100

    def test_move_clip_overlap_error(
        self,
        timeline_with_two_clips: TimelineSequence
    ) -> None:
        """Test that moving to overlapping position raises error."""
        # Try to move clip_2 to overlap with clip_1
        cmd = MoveClipCommand.create("track_1", "clip_2", 50)

        with pytest.raises(ValueError, match="overlap"):
            cmd.apply(timeline_with_two_clips)


# ================================================================
# TrimClipCommand Tests
# ================================================================

class TestTrimClipCommand:
    """Tests for TrimClipCommand."""

    def test_create_trim_left_command(self) -> None:
        """Test left trim command factory."""
        cmd = TrimClipCommand.create_left_trim("track_1", "clip_1", 10, 10)

        assert cmd.trim_side == "left"
        assert cmd.new_timeline_start == 10

    def test_create_trim_right_command(self) -> None:
        """Test right trim command factory."""
        cmd = TrimClipCommand.create_right_trim("track_1", "clip_1", 80, 80)

        assert cmd.trim_side == "right"
        assert cmd.new_timeline_duration == 80

    def test_apply_trim_left(
        self,
        timeline_with_clip: TimelineSequence
    ) -> None:
        """Test applying left trim."""
        cmd = TrimClipCommand.create_left_trim(
            "track_1",
            "clip_1",
            20,  # New start
            20,  # New source start
            old_start=0,
            old_source_start=0,
            old_duration=100,
            old_source_duration=100,
        )

        new_timeline = cmd.apply(timeline_with_clip)

        # Verify trim
        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        clip = track.get_clip_by_id("clip_1")
        assert clip is not None
        assert clip.timeline_start == 20
        assert clip.timeline_duration == 80  # 100 - 20

    def test_apply_trim_right(
        self,
        timeline_with_clip: TimelineSequence
    ) -> None:
        """Test applying right trim."""
        cmd = TrimClipCommand.create_right_trim(
            "track_1",
            "clip_1",
            50,  # New duration
            50,  # New source duration
            old_duration=100,
            old_source_duration=100,
        )

        new_timeline = cmd.apply(timeline_with_clip)

        # Verify trim
        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        clip = track.get_clip_by_id("clip_1")
        assert clip is not None
        assert clip.timeline_duration == 50

    def test_undo_trim(
        self,
        timeline_with_clip: TimelineSequence
    ) -> None:
        """Test undoing trim."""
        # Trim right
        cmd = TrimClipCommand.create_right_trim(
            "track_1",
            "clip_1",
            50,
            50,
            old_duration=100,
            old_source_duration=100,
        )

        new_timeline = cmd.apply(timeline_with_clip)

        # Undo
        restored_timeline = cmd.undo(new_timeline)

        # Verify restored
        track = restored_timeline.get_track_by_id("track_1")
        assert track is not None
        clip = track.get_clip_by_id("clip_1")
        assert clip is not None
        assert clip.timeline_duration == 100


# ================================================================
# RollTrimCommand Tests
# ================================================================

class TestRollTrimCommand:
    """Tests for moving one cut point across both adjacent clips."""

    def test_apply_roll_trim_updates_both_clips(
        self,
        timeline_with_two_clips: TimelineSequence,
    ) -> None:
        cmd = RollTrimCommand.create(
            "track_1",
            "clip_1",
            "clip_2",
            delta_frames=10,
        )

        new_timeline, _ = cmd.apply(timeline_with_two_clips)
        track = new_timeline.get_track_by_id("track_1")
        assert track is not None

        left = track.get_clip_by_id("clip_1")
        right = track.get_clip_by_id("clip_2")
        assert left is not None and right is not None
        assert (left.timeline_duration, left.source_duration) == (110, 110)
        assert (right.timeline_start, right.timeline_duration) == (110, 40)
        assert (right.source_start, right.source_duration) == (10, 40)

    def test_apply_roll_trim_left_updates_without_transient_overlap(
        self,
        timeline_with_two_clips: TimelineSequence,
    ) -> None:
        track = timeline_with_two_clips.get_track_by_id("track_1")
        assert track is not None
        right = track.get_clip_by_id("clip_2")
        assert right is not None
        # Give the right clip source headroom so a leftward roll is valid and
        # reaches the update-order path under test.
        track = track.with_clip_updated(replace(right, source_start=20))
        timeline_with_headroom = timeline_with_two_clips.with_track_updated(track)

        cmd = RollTrimCommand.create(
            "track_1",
            "clip_1",
            "clip_2",
            delta_frames=-10,
        )

        new_timeline, _ = cmd.apply(timeline_with_headroom)
        track = new_timeline.get_track_by_id("track_1")
        assert track is not None

        left = track.get_clip_by_id("clip_1")
        right = track.get_clip_by_id("clip_2")
        assert left is not None and right is not None
        assert (left.timeline_duration, left.source_duration) == (90, 90)
        assert (right.timeline_start, right.timeline_duration) == (90, 60)
        assert (right.source_start, right.source_duration) == (10, 60)


# ================================================================
# SplitClipCommand Tests
# ================================================================

class TestSplitClipCommand:
    """Tests for SplitClipCommand."""

    def test_create_split_command(self) -> None:
        """Test split command factory."""
        cmd = SplitClipCommand.create("track_1", "clip_1", 50)

        assert cmd.split_frame == 50

    def test_apply_split_clip(
        self,
        timeline_with_clip: TimelineSequence
    ) -> None:
        """Test applying split at middle."""
        cmd = SplitClipCommand.create("track_1", "clip_1", 50)

        new_timeline = cmd.apply(timeline_with_clip)

        # Verify split created two clips
        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        assert len(track.clips) == 2

        # First clip: 0-50
        first_clip = track.clips[0]
        assert first_clip.timeline_start == 0
        assert first_clip.timeline_duration == 50

        # Second clip: 50-100
        second_clip = track.clips[1]
        assert second_clip.timeline_start == 50
        assert second_clip.timeline_duration == 50

    def test_undo_split_clip(
        self,
        timeline_with_clip: TimelineSequence
    ) -> None:
        """Test undoing split."""
        cmd = SplitClipCommand.create("track_1", "clip_1", 50)

        # Apply
        new_timeline = cmd.apply(timeline_with_clip)

        # Undo
        restored_timeline = cmd.undo(new_timeline)

        # Verify merged back
        track = restored_timeline.get_track_by_id("track_1")
        assert track is not None
        assert len(track.clips) == 1

    def test_split_at_edge_error(
        self,
        timeline_with_clip: TimelineSequence
    ) -> None:
        """Test that splitting at edge raises error."""
        # Try to split at start (not inside clip)
        cmd = SplitClipCommand.create("track_1", "clip_1", 0)

        with pytest.raises(ValueError, match="must be inside clip"):
            cmd.apply(timeline_with_clip)


# ================================================================
# CommandStack Tests
# ================================================================

class TestCommandStack:
    """Tests for CommandStack."""

    def test_execute_command(
        self,
        timeline_with_track: TimelineSequence,
        sample_clip: ClipInstance
    ) -> None:
        """Test executing command through stack."""
        stack = CommandStack()
        cmd = AddClipCommand.create("track_1", sample_clip)

        new_timeline = stack.execute(cmd, timeline_with_track)

        # Verify executed
        track = new_timeline.get_track_by_id("track_1")
        assert track is not None
        assert len(track.clips) == 1
        assert stack.can_undo()

    def test_undo_redo(
        self,
        timeline_with_track: TimelineSequence,
        sample_clip: ClipInstance
    ) -> None:
        """Test undo and redo."""
        stack = CommandStack()
        cmd = AddClipCommand.create("track_1", sample_clip)

        # Execute
        timeline = stack.execute(cmd, timeline_with_track)

        # Undo
        timeline = stack.undo(timeline)
        assert timeline is not None
        track = timeline.get_track_by_id("track_1")
        assert len(track.clips) == 0
        assert stack.can_redo()

        # Redo
        timeline = stack.redo(timeline)
        assert timeline is not None
        track = timeline.get_track_by_id("track_1")
        assert len(track.clips) == 1

    def test_multiple_commands(
        self,
        timeline_with_track: TimelineSequence,
        sample_clip: ClipInstance,
        second_clip: ClipInstance
    ) -> None:
        """Test multiple commands and undo history."""
        stack = CommandStack()

        # Add first clip
        cmd1 = AddClipCommand.create("track_1", sample_clip)
        timeline = stack.execute(cmd1, timeline_with_track)

        # Add second clip
        cmd2 = AddClipCommand.create("track_1", second_clip)
        timeline = stack.execute(cmd2, timeline)

        # Verify two clips
        track = timeline.get_track_by_id("track_1")
        assert len(track.clips) == 2

        # Undo twice
        timeline = stack.undo(timeline)
        track = timeline.get_track_by_id("track_1")
        assert len(track.clips) == 1

        timeline = stack.undo(timeline)
        track = timeline.get_track_by_id("track_1")
        assert len(track.clips) == 0

    def test_redo_cleared_on_new_command(
        self,
        timeline_with_track: TimelineSequence,
        sample_clip: ClipInstance
    ) -> None:
        """Test that redo stack is cleared on new command."""
        stack = CommandStack()
        cmd1 = AddClipCommand.create("track_1", sample_clip)

        # Execute and undo
        timeline = stack.execute(cmd1, timeline_with_track)
        timeline = stack.undo(timeline)
        assert stack.can_redo()

        # Execute new command
        new_clip = ClipInstance(
            clip_id="new_clip",
            asset_id="asset_new",
            track_id="track_1",
            timeline_start=0,
            timeline_duration=30,
            source_start=0,
            source_duration=30,
        )
        cmd2 = AddClipCommand.create("track_1", new_clip)
        timeline = stack.execute(cmd2, timeline)

        # Redo should be cleared
        assert not stack.can_redo()

    def test_get_undo_label(
        self,
        timeline_with_track: TimelineSequence,
        sample_clip: ClipInstance
    ) -> None:
        """Test getting undo label."""
        stack = CommandStack()
        cmd = AddClipCommand.create("track_1", sample_clip)

        stack.execute(cmd, timeline_with_track)

        label = stack.get_undo_label()
        assert label is not None
        assert "Add Clip" in label or "at" in label


# ================================================================
# Serialization Tests
# ================================================================

class TestCommandSerialization:
    """Tests for command serialization."""

    def test_add_clip_to_dict(self, sample_clip: ClipInstance) -> None:
        """Test AddClipCommand serialization."""
        cmd = AddClipCommand.create("track_1", sample_clip)

        data = cmd.to_dict()
        assert data["command_type"] == "AddClipCommand"
        assert data["track_id"] == "track_1"
        assert "clip" in data

    def test_add_clip_from_dict(self, sample_clip: ClipInstance) -> None:
        """Test AddClipCommand deserialization."""
        cmd = AddClipCommand.create("track_1", sample_clip)
        data = cmd.to_dict()

        restored = AddClipCommand.from_dict(data)
        assert restored.track_id == "track_1"
        assert restored.clip.clip_id == "clip_1"

    def test_split_clip_to_dict(self) -> None:
        """Test SplitClipCommand serialization."""
        cmd = SplitClipCommand.create("track_1", "clip_1", 50)

        data = cmd.to_dict()
        assert data["command_type"] == "SplitClipCommand"
        assert data["split_frame"] == 50
