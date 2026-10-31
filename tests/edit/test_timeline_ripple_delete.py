"""
Tests for ripple delete functionality.

Verifies:
- RippleDeleteCommand basic apply/undo
- Linked A/V ripple delete
- Gap closing
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.commands import RippleDeleteCommand
from src.shared.media.metadata import Rational


def _make_clip(clip_id: str, track_id: str, start: int, duration: int = 100,
               linked_id: str | None = None) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=f"a_{clip_id}", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration,
        linked_clip_id=linked_id,
    )


def _make_timeline(video_clips=None, audio_clips=None) -> TimelineSequence:
    v_clips = tuple(video_clips) if video_clips else ()
    a_clips = tuple(audio_clips) if audio_clips else ()
    v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=v_clips)
    a1 = EditTrack(track_id="a1", name="A1", track_type="audio", index=0, clips=a_clips)
    return TimelineSequence(
        timeline_id="tl1", name="Test", fps=Rational(24, 1),
        video_tracks=(v1,), audio_tracks=(a1,),
    )


class TestRippleDeleteBasic:
    """Verify basic ripple delete behavior."""

    def test_delete_closes_gap(self):
        """After ripple delete, subsequent clips move left."""
        clips = [
            _make_clip("c1", "v1", 0, 50),
            _make_clip("c2", "v1", 50, 50),
            _make_clip("c3", "v1", 100, 50),
        ]
        tl = _make_timeline(video_clips=clips)
        cmd = RippleDeleteCommand(
            command_id="cmd1", label="test", track_id="v1", clip_id="c2",
        )
        new_tl, _ = cmd.apply(tl)

        # c2 deleted, c3 should have moved to frame 50
        assert new_tl.get_clip_by_id("c2") is None
        c3 = new_tl.get_clip_by_id("c3")
        assert c3 is not None
        assert c3.timeline_start == 50

    def test_undo_restores_positions(self):
        """Undo should restore all clips to original positions."""
        clips = [
            _make_clip("c1", "v1", 0, 50),
            _make_clip("c2", "v1", 50, 50),
            _make_clip("c3", "v1", 100, 50),
        ]
        tl = _make_timeline(video_clips=clips)
        cmd = RippleDeleteCommand(
            command_id="cmd1", label="test", track_id="v1", clip_id="c2",
        )
        new_tl, updated = cmd.apply(tl)
        restored = updated.undo(new_tl)

        # All clips restored
        assert restored.get_clip_by_id("c1") is not None
        assert restored.get_clip_by_id("c2") is not None
        assert restored.get_clip_by_id("c3") is not None
        assert restored.get_clip_by_id("c2").timeline_start == 50
        assert restored.get_clip_by_id("c3").timeline_start == 100

    def test_delete_first_clip(self):
        """Deleting the first clip should move all subsequent clips."""
        clips = [
            _make_clip("c1", "v1", 0, 50),
            _make_clip("c2", "v1", 50, 50),
        ]
        tl = _make_timeline(video_clips=clips)
        cmd = RippleDeleteCommand(
            command_id="cmd1", label="test", track_id="v1", clip_id="c1",
        )
        new_tl, _ = cmd.apply(tl)

        assert new_tl.get_clip_by_id("c1") is None
        c2 = new_tl.get_clip_by_id("c2")
        assert c2.timeline_start == 0


class TestRippleDeleteLinkedAV:
    """Verify linked A/V ripple delete."""

    def test_linked_video_deletes_audio(self):
        """Deleting linked video clip also ripple-deletes linked audio clip."""
        v_clips = [
            _make_clip("v_c1", "v1", 0, 50, linked_id="a_c1"),
            _make_clip("v_c2", "v1", 50, 50),
        ]
        a_clips = [
            _make_clip("a_c1", "a1", 0, 50, linked_id="v_c1"),
            _make_clip("a_c2", "a1", 50, 50),
        ]
        tl = _make_timeline(video_clips=v_clips, audio_clips=a_clips)

        cmd = RippleDeleteCommand(
            command_id="cmd1", label="test", track_id="v1", clip_id="v_c1",
        )
        new_tl, _ = cmd.apply(tl)

        # Video clip deleted
        assert new_tl.get_clip_by_id("v_c1") is None
        # Linked audio clip also deleted
        assert new_tl.get_clip_by_id("a_c1") is None
        # Subsequent clips moved
        v_c2 = new_tl.get_clip_by_id("v_c2")
        assert v_c2.timeline_start == 0
        a_c2 = new_tl.get_clip_by_id("a_c2")
        assert a_c2.timeline_start == 0

    def test_undo_linked_restores_both(self):
        """Undo linked ripple delete restores both clips."""
        v_clips = [
            _make_clip("v_c1", "v1", 0, 50, linked_id="a_c1"),
            _make_clip("v_c2", "v1", 50, 50),
        ]
        a_clips = [
            _make_clip("a_c1", "a1", 0, 50, linked_id="v_c1"),
            _make_clip("a_c2", "a1", 50, 50),
        ]
        tl = _make_timeline(video_clips=v_clips, audio_clips=a_clips)

        cmd = RippleDeleteCommand(
            command_id="cmd1", label="test", track_id="v1", clip_id="v_c1",
        )
        new_tl, updated = cmd.apply(tl)
        restored = updated.undo(new_tl)

        # Both clips restored
        assert restored.get_clip_by_id("v_c1") is not None
        assert restored.get_clip_by_id("a_c1") is not None
        assert restored.get_clip_by_id("v_c1").timeline_start == 0
        assert restored.get_clip_by_id("a_c1").timeline_start == 0
        # Subsequent clips back to original
        assert restored.get_clip_by_id("v_c2").timeline_start == 50
        assert restored.get_clip_by_id("a_c2").timeline_start == 50

    def test_unlinked_only_deletes_target(self):
        """Clip without linked_clip_id only deletes itself."""
        v_clips = [
            _make_clip("v_c1", "v1", 0, 50),
            _make_clip("v_c2", "v1", 50, 50),
        ]
        a_clips = [
            _make_clip("a_c1", "a1", 0, 50),
        ]
        tl = _make_timeline(video_clips=v_clips, audio_clips=a_clips)

        cmd = RippleDeleteCommand(
            command_id="cmd1", label="test", track_id="v1", clip_id="v_c1",
        )
        new_tl, _ = cmd.apply(tl)

        # Video deleted, audio unaffected
        assert new_tl.get_clip_by_id("v_c1") is None
        assert new_tl.get_clip_by_id("a_c1") is not None
        assert new_tl.get_clip_by_id("a_c1").timeline_start == 0
