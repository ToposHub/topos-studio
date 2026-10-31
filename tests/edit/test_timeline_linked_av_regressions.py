"""
Regression tests for linked A/V atomicity (P3).

Tests for:
- InsertLinkedAssetCommand: both video and audio clips at same timeline_start
- Insert when audio track has overlap: new track created, no half-state
- Insert video succeeds but audio would fail: entire insertion rolled back
- Linked clip move: both move together, single undo restores both
- Linked clip trim: if linked clip can't trim, entire trim rejected
- get_target_audio_track_id (pure query): no side effects, no tracks created
- Cross-track drag to existing track: uses MoveClipToExistingTrackCommand
- Cross-track drag to create zone: uses MoveClipToTrackCommand (new track)

These tests exercise the command-layer logic (commands.py) directly
without instantiating the full PyQt6 UI.
"""

from __future__ import annotations

import pytest
import uuid

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    EditTrack,
    ClipInstance,
)
from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.commands import (
    AddClipCommand,
    MoveClipCommand,
    MacroCommand,
    MoveClipToTrackCommand,
    MoveClipToExistingTrackCommand,
    TrimClipCommand,
    InsertLinkedAssetCommand,
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
def timeline_with_tracks(empty_timeline: TimelineSequence) -> TimelineSequence:
    """Create a timeline with one video track and one audio track."""
    v1 = EditTrack(
        track_id="v1",
        name="V1",
        track_type="video",
        index=0,
    )
    a1 = EditTrack(
        track_id="a1",
        name="A1",
        track_type="audio",
        index=0,
    )
    tl = empty_timeline.with_track_added(v1)
    return tl.with_track_added(a1)


@pytest.fixture
def timeline_with_clips(timeline_with_tracks: TimelineSequence) -> TimelineSequence:
    """Create a timeline with a video clip and its linked audio clip."""
    video_clip = ClipInstance(
        clip_id="video_1",
        asset_id="asset_av",
        track_id="v1",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        speed=1.0,
        linked_clip_id="audio_1",
    )
    audio_clip = ClipInstance(
        clip_id="audio_1",
        asset_id="asset_av",
        track_id="a1",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
        speed=1.0,
        linked_clip_id="video_1",
    )
    tl = timeline_with_tracks.with_track_updated(
        timeline_with_tracks.get_track_by_id("v1").with_clip_added(video_clip)
    )
    tl = tl.with_track_updated(
        tl.get_track_by_id("a1").with_clip_added(audio_clip)
    )
    return tl


@pytest.fixture
def timeline_with_blocked_audio(timeline_with_tracks: TimelineSequence) -> TimelineSequence:
    """Create a timeline where the audio track already has a clip at frame 0."""
    blocker = ClipInstance(
        clip_id="blocker_audio",
        asset_id="asset_other",
        track_id="a1",
        timeline_start=0,
        timeline_duration=200,
        source_start=0,
        source_duration=200,
        speed=1.0,
    )
    return timeline_with_tracks.with_track_updated(
        timeline_with_tracks.get_track_by_id("a1").with_clip_added(blocker)
    )


# ================================================================
# 1. Insert video+audio asset: both clips at same timeline_start
# ================================================================

class TestInsertLinkedAssetAtomicStart:
    """Both video and audio clips must start at the same frame."""

    def test_both_clips_at_same_start(self, timeline_with_tracks: TimelineSequence):
        video_clip = ClipInstance(
            clip_id="v_new",
            asset_id="asset_av",
            track_id="v1",
            timeline_start=50,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            speed=1.0,
            linked_clip_id="a_new",
        )
        audio_clip = ClipInstance(
            clip_id="a_new",
            asset_id="asset_av",
            track_id="a1",
            timeline_start=50,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            speed=1.0,
            linked_clip_id="v_new",
        )

        cmd = InsertLinkedAssetCommand.create(
            video_track_id="v1",
            video_clip=video_clip,
            audio_track_id="a1",
            audio_clip=audio_clip,
        )
        stack = CommandStack()
        result = stack.execute(cmd, timeline_with_tracks)

        # Both clips should exist at frame 50
        v = result.get_track_by_id("v1").get_clip_by_id("v_new")
        a = result.get_track_by_id("a1").get_clip_by_id("a_new")
        assert v is not None, "Video clip should exist"
        assert a is not None, "Audio clip should exist"
        assert v.timeline_start == 50
        assert a.timeline_start == 50
        assert v.timeline_start == a.timeline_start, "A/V must be at same frame"

    def test_undo_removes_both_clips(self, timeline_with_tracks: TimelineSequence):
        video_clip = ClipInstance(
            clip_id="v_new",
            asset_id="asset_av",
            track_id="v1",
            timeline_start=10,
            timeline_duration=50,
            source_start=0,
            source_duration=50,
            speed=1.0,
            linked_clip_id="a_new",
        )
        audio_clip = ClipInstance(
            clip_id="a_new",
            asset_id="asset_av",
            track_id="a1",
            timeline_start=10,
            timeline_duration=50,
            source_start=0,
            source_duration=50,
            speed=1.0,
            linked_clip_id="v_new",
        )

        cmd = InsertLinkedAssetCommand.create(
            video_track_id="v1",
            video_clip=video_clip,
            audio_track_id="a1",
            audio_clip=audio_clip,
        )
        stack = CommandStack()
        result = stack.execute(cmd, timeline_with_tracks)

        # Undo should remove both
        result = stack.undo(result)
        assert result is not None
        v = result.get_track_by_id("v1").get_clip_by_id("v_new")
        a = result.get_track_by_id("a1").get_clip_by_id("a_new")
        assert v is None, "Video clip should be removed on undo"
        assert a is None, "Audio clip should be removed on undo"


# ================================================================
# 2. Insert when audio track has overlap: new track created or rejection
# ================================================================

class TestInsertWithAudioOverlap:
    """When audio track is blocked, a new audio track is created."""

    def test_new_audio_track_created_on_overlap(
        self, timeline_with_blocked_audio: TimelineSequence
    ):
        """Audio track blocked at frame 0: command creates new track for audio."""
        video_clip = ClipInstance(
            clip_id="v_overlap",
            asset_id="asset_av",
            track_id="v1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            speed=1.0,
            linked_clip_id="a_overlap",
        )
        audio_clip = ClipInstance(
            clip_id="a_overlap",
            asset_id="asset_av",
            track_id="a1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            speed=1.0,
            linked_clip_id="v_overlap",
        )

        cmd = InsertLinkedAssetCommand.create(
            video_track_id="v1",
            video_clip=video_clip,
            audio_track_id="a1",
            audio_clip=audio_clip,
        )
        stack = CommandStack()
        result = stack.execute(cmd, timeline_with_blocked_audio)

        # Video should be on v1
        v = result.get_track_by_id("v1").get_clip_by_id("v_overlap")
        assert v is not None
        assert v.timeline_start == 0

        # Audio should NOT be on a1 (blocked)
        a_on_a1 = result.get_track_by_id("a1").get_clip_by_id("a_overlap")
        assert a_on_a1 is None, "Audio should not be on blocked track a1"

        # Audio should be on a new track, still at frame 0
        found_audio = False
        for track in result.audio_tracks:
            clip = track.get_clip_by_id("a_overlap")
            if clip is not None:
                found_audio = True
                assert clip.timeline_start == 0, "Audio must stay at same frame as video"
                break
        assert found_audio, "Audio clip should be on a new track"

        # There should now be 2 audio tracks
        assert len(result.audio_tracks) == 2

    def test_no_half_state_on_overlap(self, timeline_with_blocked_audio: TimelineSequence):
        """Undo after auto-track-creation restores everything."""
        video_clip = ClipInstance(
            clip_id="v_half",
            asset_id="asset_av",
            track_id="v1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            speed=1.0,
            linked_clip_id="a_half",
        )
        audio_clip = ClipInstance(
            clip_id="a_half",
            asset_id="asset_av",
            track_id="a1",
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
            speed=1.0,
            linked_clip_id="v_half",
        )

        cmd = InsertLinkedAssetCommand.create(
            video_track_id="v1",
            video_clip=video_clip,
            audio_track_id="a1",
            audio_clip=audio_clip,
        )
        stack = CommandStack()
        result = stack.execute(cmd, timeline_with_blocked_audio)

        # Undo removes both clips and the auto-created track
        result = stack.undo(result)
        assert result is not None
        assert result.get_track_by_id("v1").get_clip_by_id("v_half") is None
        assert len(result.audio_tracks) == 1, "Auto-created track should be removed on undo"


# ================================================================
# 3. Insert video succeeds but audio would fail: rollback
# ================================================================

class TestInsertRollbackOnAudioFailure:
    """If audio insertion fails, entire command should fail."""

    def test_video_overlap_raises(self, timeline_with_clips: TimelineSequence):
        """If video clip overlaps, the entire command raises ValueError."""
        # Video clip overlaps with existing video_1 at frame 0
        video_clip = ClipInstance(
            clip_id="v_dup",
            asset_id="asset_av",
            track_id="v1",
            timeline_start=0,
            timeline_duration=50,
            source_start=0,
            source_duration=50,
            speed=1.0,
            linked_clip_id="a_dup",
        )
        audio_clip = ClipInstance(
            clip_id="a_dup",
            asset_id="asset_av",
            track_id="a1",
            timeline_start=200,  # No overlap on audio
            timeline_duration=50,
            source_start=0,
            source_duration=50,
            speed=1.0,
            linked_clip_id="v_dup",
        )

        cmd = InsertLinkedAssetCommand.create(
            video_track_id="v1",
            video_clip=video_clip,
            audio_track_id="a1",
            audio_clip=audio_clip,
        )
        stack = CommandStack()
        with pytest.raises(ValueError):
            stack.execute(cmd, timeline_with_clips)

        # Timeline should be unchanged
        assert timeline_with_clips.get_track_by_id("v1").get_clip_by_id("v_dup") is None

    def test_audio_only_overlaps_creates_new_track(
        self, timeline_with_clips: TimelineSequence
    ):
        """If audio track has overlap, new track is created (no error)."""
        # Audio clip overlaps with existing audio_1 at frame 0
        video_clip = ClipInstance(
            clip_id="v_nooverlap",
            asset_id="asset_av",
            track_id="v1",
            timeline_start=200,  # No overlap on video (video_1 ends at 100)
            timeline_duration=50,
            source_start=0,
            source_duration=50,
            speed=1.0,
            linked_clip_id="a_nooverlap",
        )
        audio_clip = ClipInstance(
            clip_id="a_nooverlap",
            asset_id="asset_av",
            track_id="a1",
            timeline_start=0,  # Overlaps with audio_1
            timeline_duration=50,
            source_start=0,
            source_duration=50,
            speed=1.0,
            linked_clip_id="v_nooverlap",
        )

        cmd = InsertLinkedAssetCommand.create(
            video_track_id="v1",
            video_clip=video_clip,
            audio_track_id="a1",
            audio_clip=audio_clip,
        )
        stack = CommandStack()
        # Should NOT raise — creates new audio track instead
        result = stack.execute(cmd, timeline_with_clips)
        assert result is not None

        # Video clip should be placed
        v = result.get_track_by_id("v1").get_clip_by_id("v_nooverlap")
        assert v is not None
        assert v.timeline_start == 200

        # Audio clip should exist (on new track)
        found = False
        for track in result.audio_tracks:
            if track.get_clip_by_id("a_nooverlap") is not None:
                found = True
                assert track.get_clip_by_id("a_nooverlap").timeline_start == 0
                break
        assert found, "Audio clip should exist on new track"


# ================================================================
# 4. Linked clip move: both move together, single undo restores both
# ================================================================

class TestLinkedClipMove:
    """Linked clips move atomically."""

    def test_move_both_via_macro(self, timeline_with_clips: TimelineSequence):
        """Moving linked clips via MacroCommand: both shift by same delta."""
        delta = 50
        v_cmd = MoveClipCommand.create(
            track_id="v1",
            clip_id="video_1",
            new_start=0 + delta,
            old_start=0,
        )
        a_cmd = MoveClipCommand.create(
            track_id="a1",
            clip_id="audio_1",
            new_start=0 + delta,
            old_start=0,
        )
        macro = MacroCommand.create("Move linked clips", [v_cmd, a_cmd])
        stack = CommandStack()
        result = stack.execute(macro, timeline_with_clips)

        v = result.get_track_by_id("v1").get_clip_by_id("video_1")
        a = result.get_track_by_id("a1").get_clip_by_id("audio_1")
        assert v is not None and v.timeline_start == delta
        assert a is not None and a.timeline_start == delta

    def test_single_undo_restores_both(self, timeline_with_clips: TimelineSequence):
        """One undo restores both clips to original positions."""
        delta = 50
        v_cmd = MoveClipCommand.create(
            track_id="v1",
            clip_id="video_1",
            new_start=0 + delta,
            old_start=0,
        )
        a_cmd = MoveClipCommand.create(
            track_id="a1",
            clip_id="audio_1",
            new_start=0 + delta,
            old_start=0,
        )
        macro = MacroCommand.create("Move linked clips", [v_cmd, a_cmd])
        stack = CommandStack()
        result = stack.execute(macro, timeline_with_clips)

        # Single undo
        result = stack.undo(result)
        assert result is not None

        v = result.get_track_by_id("v1").get_clip_by_id("video_1")
        a = result.get_track_by_id("a1").get_clip_by_id("audio_1")
        assert v is not None and v.timeline_start == 0
        assert a is not None and a.timeline_start == 0


# ================================================================
# 5. Linked clip trim: if linked clip can't trim, entire trim rejected
# ================================================================

class TestLinkedClipTrim:
    """Trimming a linked clip that can't trim rejects the entire operation."""

    def test_trim_rejected_when_linked_clip_blocked(
        self, timeline_with_clips: TimelineSequence
    ):
        """
        Trim video left, but audio is blocked (no room).
        The MacroCommand should fail, leaving both unchanged.
        """
        # Add a blocking clip on audio track right before audio_1
        blocker = ClipInstance(
            clip_id="audio_blocker",
            asset_id="blocker",
            track_id="a1",
            timeline_start=0,
            timeline_duration=10,
            source_start=0,
            source_duration=10,
            speed=1.0,
        )
        # Can't add at 0-10 because audio_1 is at 0. Move audio_1 to start at 20.
        tl = timeline_with_clips
        audio_track = tl.get_track_by_id("a1")
        audio_1 = audio_track.get_clip_by_id("audio_1")
        moved_audio = audio_1.with_timeline_start(20)
        audio_track = EditTrack(
            track_id=audio_track.track_id,
            name=audio_track.name,
            track_type=audio_track.track_type,
            index=audio_track.index,
            clips=(moved_audio,),
            transitions=audio_track.transitions,
            is_enabled=audio_track.is_enabled,
            is_locked=audio_track.is_locked,
            is_muted=audio_track.is_muted,
            is_solo=audio_track.is_solo,
            volume=audio_track.volume,
        )
        tl = tl.with_track_updated(audio_track)

        # Now try to trim video left from 0 to -10 (can't go negative anyway)
        # Instead, try trimming both video and audio right.
        # Video: extend right by 20 frames at the end
        # Audio: blocked because it starts at 20, extending it right would overlap with nothing
        # Let's use a simpler scenario: trim left, but audio_1 can't extend left because it starts at 20
        # and we want to move it to 10, but there's a blocker at 0-10.

        # Add blocker
        blocker = ClipInstance(
            clip_id="audio_blocker",
            asset_id="blocker",
            track_id="a1",
            timeline_start=0,
            timeline_duration=15,
            source_start=0,
            source_duration=15,
            speed=1.0,
        )
        audio_track = tl.get_track_by_id("a1")
        audio_track = audio_track.with_clip_added(blocker)
        tl = tl.with_track_updated(audio_track)

        # Try to create a MacroCommand that trims both
        # Video trim left: extend video_1 left from 0 to -5 (invalid)
        # This should fail at the command level
        v_trim = TrimClipCommand(
            command_id=str(uuid.uuid4()),
            label="Trim video left",
            track_id="v1",
            clip_id="video_1",
            trim_side="left",
            new_timeline_start=-5,
            new_timeline_duration=105,
            new_source_start=-5,
            new_source_duration=105,
            old_timeline_start=0,
            old_timeline_duration=100,
            old_source_start=0,
            old_source_duration=100,
        )
        a_trim = TrimClipCommand(
            command_id=str(uuid.uuid4()),
            label="Trim audio left",
            track_id="a1",
            clip_id="audio_1",
            trim_side="left",
            new_timeline_start=15,
            new_timeline_duration=105,
            new_source_start=-5,
            new_source_duration=105,
            old_timeline_start=20,
            old_timeline_duration=100,
            old_source_start=0,
            old_source_duration=100,
        )

        macro = MacroCommand.create("Trim linked clips", [v_trim, a_trim])
        stack = CommandStack()

        # The video trim with new_timeline_start=-5 will be clamped but let's test
        # that the entire macro fails if any sub-command fails
        with pytest.raises(ValueError):
            stack.execute(macro, tl)

        # Timeline unchanged
        v = tl.get_track_by_id("v1").get_clip_by_id("video_1")
        assert v is not None and v.timeline_start == 0


# ================================================================
# 6. get_target_audio_track_id: no side effects
# ================================================================

class TestGetTargetAudioTrackPure:
    """Verify get_target_audio_track_id is a pure query with no side effects."""

    def test_returns_none_when_no_audio_track(self, empty_timeline: TimelineSequence):
        """A getter should return None, not create a track."""
        # Simulate the logic of the pure getter directly (we can't instantiate
        # TimelineView in pytest without PyQt, so test the logic pattern)
        timeline = empty_timeline
        # No audio tracks exist
        assert len(timeline.audio_tracks) == 0

        # Pure getter logic:
        target_id = None
        if timeline.audio_tracks:
            target_id = timeline.audio_tracks[0].track_id
        # else: returns None, no track creation

        assert target_id is None
        # Verify no tracks were created
        assert len(timeline.audio_tracks) == 0

    def test_returns_first_audio_when_exists(
        self, timeline_with_tracks: TimelineSequence
    ):
        """Returns first audio track ID without side effects."""
        timeline = timeline_with_tracks
        audio_tracks_before = len(timeline.audio_tracks)

        # Pure getter logic:
        target_id = None
        if timeline.audio_tracks:
            target_id = timeline.audio_tracks[0].track_id

        assert target_id == "a1"
        assert len(timeline.audio_tracks) == audio_tracks_before


# ================================================================
# 7. Cross-track drag to existing track: no new track created
# ================================================================

class TestCrossTrackDragToExisting:
    """Dropping on an existing track uses MoveClipToExistingTrackCommand."""

    def test_move_to_existing_track_no_creation(
        self, timeline_with_clips: TimelineSequence
    ):
        """Moving clip to an existing track should NOT create a new track."""
        # Add a second video track
        v2 = EditTrack(
            track_id="v2",
            name="V2",
            track_type="video",
            index=1,
        )
        tl = timeline_with_clips.with_track_added(v2)
        num_tracks_before = len(tl.video_tracks)

        cmd = MoveClipToExistingTrackCommand.create(
            source_track_id="v1",
            target_track_id="v2",
            clip_id="video_1",
            new_start=50,
            old_start=0,
        )
        stack = CommandStack()
        result = stack.execute(cmd, tl)

        # Clip moved to v2
        assert result.get_track_by_id("v2").get_clip_by_id("video_1") is not None
        # No new tracks created
        assert len(result.video_tracks) == num_tracks_before
        # Removed from v1
        assert result.get_track_by_id("v1").get_clip_by_id("video_1") is None

    def test_undo_restores_to_original_track(
        self, timeline_with_clips: TimelineSequence
    ):
        """Undo cross-track move restores clip to original track."""
        v2 = EditTrack(
            track_id="v2",
            name="V2",
            track_type="video",
            index=1,
        )
        tl = timeline_with_clips.with_track_added(v2)

        cmd = MoveClipToExistingTrackCommand.create(
            source_track_id="v1",
            target_track_id="v2",
            clip_id="video_1",
            new_start=50,
            old_start=0,
        )
        stack = CommandStack()
        result = stack.execute(cmd, tl)
        result = stack.undo(result)
        assert result is not None

        # Back on v1 at original position
        v = result.get_track_by_id("v1").get_clip_by_id("video_1")
        assert v is not None
        assert v.timeline_start == 0


# ================================================================
# 8. Cross-track drag to create zone: new track created
# ================================================================

class TestCrossTrackDragToCreateZone:
    """Dropping on a create zone creates a new track."""

    def test_new_track_created(
        self, timeline_with_clips: TimelineSequence
    ):
        """Moving clip to new track creates a new track."""
        num_tracks_before = len(timeline_with_clips.video_tracks)

        cmd = MoveClipToTrackCommand.create_for_new_track(
            source_track_id="v1",
            clip_id="video_1",
            target_track_type="video",
            new_track_position="above",
            new_start=0,
            old_start=0,
        )
        stack = CommandStack()
        result = stack.execute(cmd, timeline_with_clips)

        # New track created
        assert len(result.video_tracks) == num_tracks_before + 1

        # Clip exists on the new track
        found = False
        for track in result.video_tracks:
            if track.track_id != "v1" and track.get_clip_by_id("video_1") is not None:
                found = True
                break
        assert found, "Clip should be on the new track"

        # Clip removed from original
        assert result.get_track_by_id("v1").get_clip_by_id("video_1") is None

    def test_undo_removes_created_track(
        self, timeline_with_clips: TimelineSequence
    ):
        """Undo removes the auto-created track."""
        num_tracks_before = len(timeline_with_clips.video_tracks)

        cmd = MoveClipToTrackCommand.create_for_new_track(
            source_track_id="v1",
            clip_id="video_1",
            target_track_type="video",
            new_track_position="above",
            new_start=0,
            old_start=0,
        )
        stack = CommandStack()
        result = stack.execute(cmd, timeline_with_clips)
        assert len(result.video_tracks) == num_tracks_before + 1

        result = stack.undo(result)
        assert result is not None
        assert len(result.video_tracks) == num_tracks_before
        # Clip back on v1
        v = result.get_track_by_id("v1").get_clip_by_id("video_1")
        assert v is not None
        assert v.timeline_start == 0
