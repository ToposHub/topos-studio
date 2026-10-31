"""
Tests for clip fade in/out functionality.

Verifies:
- ChangeClipFadeCommand apply/undo
- Fade in/out duration validation
- Combined fade changes
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.commands import ChangeClipFadeCommand
from src.shared.media.metadata import Rational


def _make_clip(clip_id: str = "c1", track_id: str = "v1",
               start: int = 0, duration: int = 100) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=f"a_{clip_id}", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration,
    )


def _make_timeline(clips=None) -> TimelineSequence:
    clips = clips or [_make_clip()]
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=tuple(clips))
    return TimelineSequence(
        timeline_id="tl1", name="Test", fps=Rational(24, 1),
        video_tracks=(track,),
    )


class TestChangeClipFadeCommand:
    """Verify ChangeClipFadeCommand apply and undo."""

    def test_fade_in(self):
        tl = _make_timeline()
        cmd = ChangeClipFadeCommand.create_fade_in("v1", "c1", 20)
        new_tl, updated = cmd.apply(tl)
        clip = new_tl.get_clip_by_id("c1")
        assert clip.fade_in_duration == 20

    def test_fade_out(self):
        tl = _make_timeline()
        cmd = ChangeClipFadeCommand.create_fade_out("v1", "c1", 30)
        new_tl, updated = cmd.apply(tl)
        clip = new_tl.get_clip_by_id("c1")
        assert clip.fade_out_duration == 30

    def test_fade_both(self):
        tl = _make_timeline()
        cmd = ChangeClipFadeCommand.create("v1", "c1", 10, 15)
        new_tl, updated = cmd.apply(tl)
        clip = new_tl.get_clip_by_id("c1")
        assert clip.fade_in_duration == 10
        assert clip.fade_out_duration == 15

    def test_fades_cannot_cross(self):
        clip = _make_clip().with_fade(70, 60)
        assert clip.fade_in_duration == 70
        assert clip.fade_out_duration == 30

        clip = _make_clip().with_fade_out(40).with_fade_in(70)
        assert clip.fade_in_duration == 60
        assert clip.fade_out_duration == 40

    def test_undo_restores_original(self):
        tl = _make_timeline()
        cmd = ChangeClipFadeCommand.create("v1", "c1", 20, 30)
        new_tl, updated = cmd.apply(tl)
        restored = updated.undo(new_tl)
        clip = restored.get_clip_by_id("c1")
        assert clip.fade_in_duration == 0
        assert clip.fade_out_duration == 0

    def test_undo_fade_in_only(self):
        tl = _make_timeline([_make_clip()])
        # First set fade_in to 10
        clip = tl.get_clip_by_id("c1")
        clip = clip.with_fade_in(10)
        track = tl.get_track_by_id("v1")
        track = track.with_clip_updated(clip)
        tl = tl.with_track_updated(track)

        # Now change fade_in to 30
        cmd = ChangeClipFadeCommand.create_fade_in("v1", "c1", 30)
        new_tl, updated = cmd.apply(new_tl := tl)
        assert new_tl.get_clip_by_id("c1").fade_in_duration == 30

        restored = updated.undo(new_tl)
        assert restored.get_clip_by_id("c1").fade_in_duration == 10

    def test_undo_uses_pre_drag_values_from_live_preview(self):
        """A live preview must not turn the committed edit into a no-op undo."""
        tl = _make_timeline()
        preview_clip = tl.get_clip_by_id("c1").with_fade(20, 30)
        preview_track = tl.get_track_by_id("v1").with_clip_updated(preview_clip)
        preview_tl = tl.with_track_updated(preview_track)

        cmd = ChangeClipFadeCommand.create(
            "v1", "c1", 20, 30, old_fade_in=0, old_fade_out=0
        )
        committed_tl, updated = cmd.apply(preview_tl)
        restored = updated.undo(committed_tl)

        restored_clip = restored.get_clip_by_id("c1")
        assert restored_clip.fade_in_duration == 0
        assert restored_clip.fade_out_duration == 0

    def test_invalid_track_raises(self):
        tl = _make_timeline()
        cmd = ChangeClipFadeCommand.create_fade_in("nonexistent", "c1", 10)
        with pytest.raises(ValueError):
            cmd.apply(tl)

    def test_invalid_clip_raises(self):
        tl = _make_timeline()
        cmd = ChangeClipFadeCommand.create_fade_in("v1", "nonexistent", 10)
        with pytest.raises(ValueError):
            cmd.apply(tl)

    def test_invalid_fade_type_raises(self):
        tl = _make_timeline()
        cmd = ChangeClipFadeCommand(
            command_id="cmd1", label="test", track_id="v1", clip_id="c1",
            fade_type="invalid",
        )
        with pytest.raises(ValueError):
            cmd.apply(tl)

    def test_serialization_roundtrip(self):
        cmd = ChangeClipFadeCommand.create("v1", "c1", 20, 30, 5, 8)
        d = cmd.to_dict()
        restored = ChangeClipFadeCommand.from_dict(d)
        assert restored.clip_id == "c1"
        assert restored.new_fade_in == 20
        assert restored.new_fade_out == 30
