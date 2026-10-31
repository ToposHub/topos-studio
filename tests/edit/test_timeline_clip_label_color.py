"""
Tests for clip label color and timeline range commands.

Verifies:
- SetClipLabelColorCommand apply/undo
- SetTimelineRangeCommand apply/undo
- with_label_color immutability
- with_range immutability
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
    SetClipLabelColorCommand,
    SetClipWaveformDisplayCommand,
    SetTimelineRangeCommand,
    CommandStack,
)


def _make_track(track_type: str, name: str, index: int = 0) -> EditTrack:
    return EditTrack(track_id=f"track_{name}", name=name, track_type=track_type, index=index)


def _make_clip(clip_id: str, track_id: str, start: int = 0, duration: int = 100,
               label_color: str | None = None) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=f"asset_{clip_id}", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration,
        label_color=label_color,
    )


@pytest.fixture
def empty_timeline() -> TimelineSequence:
    return TimelineSequence(timeline_id="test-tl", name="Test", fps=24.0)


# ================================================================
# Clip label color tests
# ================================================================

class TestClipLabelColor:
    def test_with_label_color_immutable(self):
        clip = _make_clip("c1", "t1", label_color=None)
        updated = clip.with_label_color("#FF0000")
        assert updated.label_color == "#FF0000"
        assert clip.label_color is None  # original unchanged

    def test_clear_label_color(self):
        clip = _make_clip("c1", "t1", label_color="#00FF00")
        updated = clip.with_label_color(None)
        assert updated.label_color is None

    def test_set_label_color_command(self, empty_timeline: TimelineSequence):
        v1 = _make_track("video", "V1")
        tl = empty_timeline.with_track_added(v1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("c1", v1.track_id))

        cmd = SetClipLabelColorCommand.create(v1.track_id, "c1", "#FF0000")
        tl = cmd.apply(tl)
        assert tl.get_clip_by_id("c1").label_color == "#FF0000"

    def test_set_label_color_undo(self, empty_timeline: TimelineSequence):
        v1 = _make_track("video", "V1")
        tl = empty_timeline.with_track_added(v1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("c1", v1.track_id, label_color="#0000FF"))

        cmd = SetClipLabelColorCommand.create(v1.track_id, "c1", "#FF0000", old_color="#0000FF")
        tl = cmd.apply(tl)
        assert tl.get_clip_by_id("c1").label_color == "#FF0000"

        tl = cmd.undo(tl)
        assert tl.get_clip_by_id("c1").label_color == "#0000FF"

    def test_set_label_color_auto_captures_old(self, empty_timeline: TimelineSequence):
        v1 = _make_track("video", "V1")
        tl = empty_timeline.with_track_added(v1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("c1", v1.track_id, label_color="#AAAAAA"))

        cmd = SetClipLabelColorCommand.create(v1.track_id, "c1", "#FF0000")
        tl = cmd.apply(tl)
        assert tl.get_clip_by_id("c1").label_color == "#FF0000"

        tl = cmd.undo(tl)
        assert tl.get_clip_by_id("c1").label_color == "#AAAAAA"

    def test_via_command_stack(self, empty_timeline: TimelineSequence):
        v1 = _make_track("video", "V1")
        tl = empty_timeline.with_track_added(v1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("c1", v1.track_id))

        stack = CommandStack()
        cmd = SetClipLabelColorCommand.create(v1.track_id, "c1", "#FF0000")
        tl = stack.execute(cmd, tl)
        assert tl.get_clip_by_id("c1").label_color == "#FF0000"

        tl = stack.undo(tl)
        assert tl.get_clip_by_id("c1").label_color is None


class TestClipWaveformDisplay:
    def test_defaults_to_mono_and_round_trips(self):
        clip = _make_clip("c1", "t1")
        assert clip.waveform_show_channels is False

        stereo = clip.with_waveform_show_channels(True)
        assert stereo.waveform_show_channels is True
        assert ClipInstance.from_dict(stereo.to_dict()).waveform_show_channels is True

    def test_set_and_undo_waveform_display(self, empty_timeline: TimelineSequence):
        v1 = _make_track("audio", "A1")
        tl = empty_timeline.with_track_added(v1)
        tl = tl.with_clip_added(v1.track_id, _make_clip("c1", v1.track_id))

        cmd = SetClipWaveformDisplayCommand.create(v1.track_id, "c1", True)
        tl = cmd.apply(tl)
        assert tl.get_clip_by_id("c1").waveform_show_channels is True

        tl = cmd.undo(tl)
        assert tl.get_clip_by_id("c1").waveform_show_channels is False


# ================================================================
# Timeline range command tests
# ================================================================

class TestTimelineRangeCommand:
    def test_set_range(self, empty_timeline: TimelineSequence):
        cmd = SetTimelineRangeCommand.create(48, 144)
        tl = cmd.apply(empty_timeline)
        assert tl.range_start == 48
        assert tl.range_end == 144

    def test_set_range_undo(self, empty_timeline: TimelineSequence):
        tl = empty_timeline.with_range(10, 20)
        cmd = SetTimelineRangeCommand.create(48, 144, old_start=10, old_end=20)
        tl = cmd.apply(tl)
        assert tl.range_start == 48

        tl = cmd.undo(tl)
        assert tl.range_start == 10
        assert tl.range_end == 20

    def test_clear_range(self, empty_timeline: TimelineSequence):
        tl = empty_timeline.with_range(48, 144)
        cmd = SetTimelineRangeCommand.create(None, None, old_start=48, old_end=144)
        tl = cmd.apply(tl)
        assert tl.range_start is None
        assert tl.range_end is None

    def test_range_via_command_stack(self, empty_timeline: TimelineSequence):
        stack = CommandStack()
        cmd = SetTimelineRangeCommand.create(48, 144)
        tl = stack.execute(cmd, empty_timeline)
        assert tl.range_start == 48

        tl = stack.undo(tl)
        assert tl.range_start is None

    def test_with_range_immutable(self, empty_timeline: TimelineSequence):
        tl2 = empty_timeline.with_range(48, 144)
        assert empty_timeline.range_start is None  # original unchanged
        assert tl2.range_start == 48
