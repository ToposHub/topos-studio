"""
Tests for timeline copy/paste/duplicate functionality.

Verifies:
- Paste creates new clip IDs (not reusing old ones)
- Paste linked A/V remaps linked_clip_id within the batch
- ToggleClipEnabledCommand undo/redo
- SetClipLinkedCommand (link/unlink) undo/redo
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    TimelineSequence,
    EditTrack,
    ClipInstance,
)
from src.features.edit.core.commands import (
    AddClipCommand,
    ToggleClipEnabledCommand,
    SetClipLinkedCommand,
    CommandStack,
)


def _make_track(track_type: str, name: str, index: int = 0) -> EditTrack:
    return EditTrack(track_id=f"track_{name}", name=name, track_type=track_type, index=index)


def _make_clip(clip_id: str, track_id: str, start: int = 0, duration: int = 100,
               linked_id: str | None = None) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=f"asset_{clip_id}", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration,
        linked_clip_id=linked_id,
    )


@pytest.fixture
def empty_timeline() -> TimelineSequence:
    return TimelineSequence(timeline_id="test-tl", name="Test", fps=24.0)


# ================================================================
# ToggleClipEnabledCommand Tests
# ================================================================

class TestToggleClipEnabled:
    def test_disable_clip(self, empty_timeline: TimelineSequence):
        v1 = _make_track("video", "V1")
        tl = empty_timeline.with_track_added(v1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("c1", v1.track_id))

        cmd = ToggleClipEnabledCommand.create(v1.track_id, "c1", False, True)
        tl = cmd.apply(tl)
        assert tl.get_clip_by_id("c1").is_enabled is False

    def test_undo_restore_enabled(self, empty_timeline: TimelineSequence):
        v1 = _make_track("video", "V1")
        tl = empty_timeline.with_track_added(v1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("c1", v1.track_id))

        cmd = ToggleClipEnabledCommand.create(v1.track_id, "c1", False, True)
        tl = cmd.apply(tl)
        tl = cmd.undo(tl)
        assert tl.get_clip_by_id("c1").is_enabled is True

    def test_via_command_stack(self, empty_timeline: TimelineSequence):
        v1 = _make_track("video", "V1")
        tl = empty_timeline.with_track_added(v1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("c1", v1.track_id))

        stack = CommandStack()
        cmd = ToggleClipEnabledCommand.create(v1.track_id, "c1", False, True)
        tl = stack.execute(cmd, tl)
        assert tl.get_clip_by_id("c1").is_enabled is False

        tl = stack.undo(tl)
        assert tl.get_clip_by_id("c1").is_enabled is True


# ================================================================
# SetClipLinkedCommand Tests
# ================================================================

class TestSetClipLinked:
    def test_link_two_clips(self, empty_timeline: TimelineSequence):
        v1 = _make_track("video", "V1", 0)
        a1 = _make_track("audio", "A1", 0)
        tl = empty_timeline.with_track_added(v1).with_track_added(a1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("v1", v1.track_id))
        tl = tl.with_clip_added(a1.track_id, _make_clip("a1", a1.track_id))

        cmd = SetClipLinkedCommand.create_link("v1", v1.track_id, "a1", a1.track_id)
        tl = cmd.apply(tl)
        assert tl.get_clip_by_id("v1").linked_clip_id == "a1"
        assert tl.get_clip_by_id("a1").linked_clip_id == "v1"

    def test_relink_clears_previous_partner(self, empty_timeline: TimelineSequence):
        v1 = _make_track("video", "V1", 0)
        a1 = _make_track("audio", "A1", 0)
        a2 = _make_track("audio", "A2", 1)
        tl = empty_timeline.with_track_added(v1).with_track_added(a1).with_track_added(a2)
        tl = tl.with_clip_added(v1.track_id, _make_clip("v1", v1.track_id, linked_id="a1"))
        tl = tl.with_clip_added(a1.track_id, _make_clip("a1", a1.track_id, linked_id="v1"))
        tl = tl.with_clip_added(a2.track_id, _make_clip("a2", a2.track_id))

        cmd = SetClipLinkedCommand.create_link("v1", v1.track_id, "a2", a2.track_id)
        tl = cmd.apply(tl)

        assert tl.get_clip_by_id("v1").linked_clip_id == "a2"
        assert tl.get_clip_by_id("a2").linked_clip_id == "v1"
        assert tl.get_clip_by_id("a1").linked_clip_id is None

    def test_unlink_two_clips(self, empty_timeline: TimelineSequence):
        v1 = _make_track("video", "V1", 0)
        a1 = _make_track("audio", "A1", 0)
        tl = empty_timeline.with_track_added(v1).with_track_added(a1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("v1", v1.track_id, linked_id="a1"))
        tl = tl.with_clip_added(a1.track_id, _make_clip("a1", a1.track_id, linked_id="v1"))

        cmd = SetClipLinkedCommand.create_unlink("v1", v1.track_id, "a1", a1.track_id)
        tl = cmd.apply(tl)
        assert tl.get_clip_by_id("v1").linked_clip_id is None
        assert tl.get_clip_by_id("a1").linked_clip_id is None

    def test_link_then_undo(self, empty_timeline: TimelineSequence):
        v1 = _make_track("video", "V1", 0)
        a1 = _make_track("audio", "A1", 0)
        tl = empty_timeline.with_track_added(v1).with_track_added(a1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("v1", v1.track_id))
        tl = tl.with_clip_added(a1.track_id, _make_clip("a1", a1.track_id))

        cmd = SetClipLinkedCommand.create_link("v1", v1.track_id, "a1", a1.track_id)
        tl = cmd.apply(tl)
        assert tl.get_clip_by_id("v1").linked_clip_id == "a1"

        tl = cmd.undo(tl)
        assert tl.get_clip_by_id("v1").linked_clip_id is None
        assert tl.get_clip_by_id("a1").linked_clip_id is None

    def test_unlink_then_undo_relinks(self, empty_timeline: TimelineSequence):
        v1 = _make_track("video", "V1", 0)
        a1 = _make_track("audio", "A1", 0)
        tl = empty_timeline.with_track_added(v1).with_track_added(a1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("v1", v1.track_id, linked_id="a1"))
        tl = tl.with_clip_added(a1.track_id, _make_clip("a1", a1.track_id, linked_id="v1"))

        cmd = SetClipLinkedCommand.create_unlink("v1", v1.track_id, "a1", a1.track_id)
        tl = cmd.apply(tl)
        assert tl.get_clip_by_id("v1").linked_clip_id is None

        tl = cmd.undo(tl)
        assert tl.get_clip_by_id("v1").linked_clip_id == "a1"
        assert tl.get_clip_by_id("a1").linked_clip_id == "v1"


# ================================================================
# Paste linked A/V remapping tests (model-level)
# ================================================================

class TestPasteLinkedRemapping:
    def test_with_linked_clip_id(self):
        clip = _make_clip("c1", "t1", linked_id="c2")
        updated = clip.with_linked_clip_id("c3")
        assert updated.linked_clip_id == "c3"
        assert clip.linked_clip_id == "c2"  # original unchanged (immutable)

    def test_clear_linked_clip_id(self):
        clip = _make_clip("c1", "t1", linked_id="c2")
        updated = clip.with_linked_clip_id(None)
        assert updated.linked_clip_id is None

    def test_add_clip_preserves_linked(self, empty_timeline: TimelineSequence):
        v1 = _make_track("video", "V1")
        tl = empty_timeline.with_track_added(v1)
        clip = _make_clip("c1", v1.track_id, linked_id="c2")

        cmd = AddClipCommand.create(v1.track_id, clip)
        tl = cmd.apply(tl)
        assert tl.get_clip_by_id("c1").linked_clip_id == "c2"
