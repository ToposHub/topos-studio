"""
Tests for track management commands.

Verifies:
- AddTrackCommand / DeleteTrackCommand / RenameTrackCommand / MoveTrackCommand
- Undo for each command
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.commands import (
    AddTrackCommand,
    DeleteTrackCommand,
    RenameTrackCommand,
    MoveTrackCommand,
)
from src.shared.media.metadata import Rational


def _make_clip(clip_id: str, track_id: str, start: int = 0, duration: int = 100) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=f"a_{clip_id}", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration,
    )


def _make_timeline(video_tracks=None, audio_tracks=None) -> TimelineSequence:
    v = tuple(video_tracks) if video_tracks else ()
    a = tuple(audio_tracks) if audio_tracks else ()
    return TimelineSequence(
        timeline_id="tl1", name="Test", fps=Rational(24, 1),
        video_tracks=v, audio_tracks=a,
    )


class TestAddTrackCommand:
    """Verify AddTrackCommand."""

    def test_add_video_track(self):
        tl = _make_timeline()
        cmd = AddTrackCommand(
            command_id="cmd1", label="test", track_type="video",
        )
        new_tl, updated = cmd.apply(tl)
        assert len(new_tl.video_tracks) == 1
        assert new_tl.video_tracks[0].track_type == "video"

    def test_add_audio_track(self):
        tl = _make_timeline()
        cmd = AddTrackCommand(
            command_id="cmd1", label="test", track_type="audio",
        )
        new_tl, updated = cmd.apply(tl)
        assert len(new_tl.audio_tracks) == 1

    def test_add_with_custom_name(self):
        tl = _make_timeline()
        cmd = AddTrackCommand(
            command_id="cmd1", label="test", track_type="video", name="Custom",
        )
        new_tl, updated = cmd.apply(tl)
        assert new_tl.video_tracks[0].name == "Custom"

    def test_undo_add(self):
        tl = _make_timeline()
        cmd = AddTrackCommand(
            command_id="cmd1", label="test", track_type="video",
        )
        new_tl, updated = cmd.apply(tl)
        restored = updated.undo(new_tl)
        assert len(restored.video_tracks) == 0

    def test_invalid_track_type_raises(self):
        tl = _make_timeline()
        cmd = AddTrackCommand(
            command_id="cmd1", label="test", track_type="invalid",
        )
        with pytest.raises(ValueError):
            cmd.apply(tl)


class TestDeleteTrackCommand:
    """Verify DeleteTrackCommand."""

    def test_delete_video_track(self):
        track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
        tl = _make_timeline(video_tracks=[track])
        cmd = DeleteTrackCommand(
            command_id="cmd1", label="test", track_id="v1",
        )
        new_tl, updated = cmd.apply(tl)
        assert len(new_tl.video_tracks) == 0

    def test_delete_preserves_other_tracks(self):
        v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
        v2 = EditTrack(track_id="v2", name="V2", track_type="video", index=1)
        tl = _make_timeline(video_tracks=[v1, v2])
        cmd = DeleteTrackCommand(
            command_id="cmd1", label="test", track_id="v1",
        )
        new_tl, updated = cmd.apply(tl)
        assert len(new_tl.video_tracks) == 1
        assert new_tl.video_tracks[0].track_id == "v2"
        assert new_tl.video_tracks[0].index == 0  # re-indexed

    def test_undo_delete(self):
        track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
        tl = _make_timeline(video_tracks=[track])
        cmd = DeleteTrackCommand(
            command_id="cmd1", label="test", track_id="v1",
        )
        new_tl, updated = cmd.apply(tl)
        restored = updated.undo(new_tl)
        assert len(restored.video_tracks) == 1
        assert restored.video_tracks[0].name == "V1"

    def test_delete_nonexistent_raises(self):
        tl = _make_timeline()
        cmd = DeleteTrackCommand(
            command_id="cmd1", label="test", track_id="nonexistent",
        )
        with pytest.raises(ValueError):
            cmd.apply(tl)


class TestRenameTrackCommand:
    """Verify RenameTrackCommand."""

    def test_rename_track(self):
        track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
        tl = _make_timeline(video_tracks=[track])
        cmd = RenameTrackCommand(
            command_id="cmd1", label="test", track_id="v1", new_name="My Video",
        )
        new_tl, updated = cmd.apply(tl)
        assert new_tl.video_tracks[0].name == "My Video"

    def test_undo_rename(self):
        track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
        tl = _make_timeline(video_tracks=[track])
        cmd = RenameTrackCommand(
            command_id="cmd1", label="test", track_id="v1", new_name="New Name",
        )
        new_tl, updated = cmd.apply(tl)
        restored = updated.undo(new_tl)
        assert restored.video_tracks[0].name == "V1"

    def test_rename_nonexistent_raises(self):
        tl = _make_timeline()
        cmd = RenameTrackCommand(
            command_id="cmd1", label="test", track_id="nonexistent", new_name="X",
        )
        with pytest.raises(ValueError):
            cmd.apply(tl)


class TestMoveTrackCommand:
    """Verify MoveTrackCommand."""

    def test_move_track_down(self):
        v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
        v2 = EditTrack(track_id="v2", name="V2", track_type="video", index=1)
        tl = _make_timeline(video_tracks=[v1, v2])
        cmd = MoveTrackCommand(
            command_id="cmd1", label="test", track_id="v1", direction="down",
        )
        new_tl = cmd.apply(tl)
        assert new_tl.video_tracks[0].track_id == "v2"
        assert new_tl.video_tracks[1].track_id == "v1"

    def test_move_track_up(self):
        v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
        v2 = EditTrack(track_id="v2", name="V2", track_type="video", index=1)
        tl = _make_timeline(video_tracks=[v1, v2])
        cmd = MoveTrackCommand(
            command_id="cmd1", label="test", track_id="v2", direction="up",
        )
        new_tl = cmd.apply(tl)
        assert new_tl.video_tracks[0].track_id == "v2"
        assert new_tl.video_tracks[1].track_id == "v1"

    def test_undo_move(self):
        v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
        v2 = EditTrack(track_id="v2", name="V2", track_type="video", index=1)
        tl = _make_timeline(video_tracks=[v1, v2])
        cmd = MoveTrackCommand(
            command_id="cmd1", label="test", track_id="v1", direction="down",
        )
        new_tl = cmd.apply(tl)
        restored = cmd.undo(new_tl)
        assert restored.video_tracks[0].track_id == "v1"
        assert restored.video_tracks[1].track_id == "v2"

    def test_move_first_track_up_noop(self):
        v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
        tl = _make_timeline(video_tracks=[v1])
        cmd = MoveTrackCommand(
            command_id="cmd1", label="test", track_id="v1", direction="up",
        )
        new_tl = cmd.apply(tl)
        # No change
        assert new_tl.video_tracks[0].track_id == "v1"
