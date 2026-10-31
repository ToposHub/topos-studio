"""Editing a sliced curve keeps its continuous source entry in the new clock."""
from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace
from typing import Any

import pytest

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.commands import TrimClipCommand
from src.features.edit.core.timeline_ops import timeline_duration_for_source
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
from src.features.edit.ui.timeline_view import TimelineView


def _trimmed(reverse: bool = False) -> TimelineSequence:
    speed = SpeedParams(speed=1.0, reverse=reverse, speed_points=(SpeedPoint(80, 2.0),))
    clip = ClipInstance(clip_id="c", asset_id="a", track_id="v", timeline_start=0,
        timeline_duration=90, source_start=100, source_duration=100, speed=speed)
    sequence = TimelineSequence(timeline_id="t", video_tracks=(EditTrack(track_id="v", clips=(clip,)),))
    source, duration = clip.with_left_edge_source_values(20)
    return TrimClipCommand.create_left_trim("v", "c", 20, source,
                                            new_source_duration=duration).apply(sequence)


@pytest.mark.parametrize("reverse", [False, True])
def test_trim_then_base_speed_change_keeps_source_entry_and_correct_duration(reverse: bool) -> None:
    clip = _trimmed(reverse).get_clip_by_id("c")
    before = clip.to_dict()
    updated = clip.with_speed_params(clip.speed.with_speed(2.0))
    assert updated.timeline_to_source_frame(updated.timeline_start) == clip.timeline_to_source_frame(clip.timeline_start)
    assert updated.timeline_duration == 40
    assert updated.speed.curve_time_offset == 10.0
    assert updated.speed.curve_offset == 20
    assert updated.speed.curve_length == 100
    assert updated.speed.speed_points == clip.speed.speed_points
    assert clip.to_dict() == before
    restored = ClipInstance.from_dict(json.loads(json.dumps(updated.to_dict())))
    assert restored.to_dict() == updated.to_dict()
    assert [restored.timeline_to_source_frame(f) for f in range(restored.timeline_start, restored.timeline_end)] == [
        updated.timeline_to_source_frame(f) for f in range(updated.timeline_start, updated.timeline_end)]


@pytest.mark.parametrize("operation", ["base", "insert", "point_speed", "easing"])
@pytest.mark.parametrize("entry", [-8.375, 24.375, 50.125])
def test_curve_factories_reanchor_fractional_entry_without_mutating_original(operation: str, entry: float) -> None:
    original = SpeedParams(speed=0.75, interpolation="frame_blend",
        speed_points=(SpeedPoint(30, 2.0, "smooth"), SpeedPoint(80, 1.25)),
        curve_length=120)
    domain_entry = original._curve_position_at_time(entry, 120)
    offset = int(domain_entry)
    sliced = replace(original, curve_offset=offset, curve_time_offset=entry)
    before = sliced.to_dict()
    new = {
        "base": lambda: sliced.with_speed(1.5),
        "insert": lambda: sliced.with_speed_point(70 - offset, 120 - offset),
        "point_speed": lambda: sliced.with_speed_point_speed(30 - offset, 3.0),
        "easing": lambda: sliced.with_point_easing(30 - offset, "linear"),
    }[operation]()
    assert new._curve_position_at_time(0, 120) == pytest.approx(domain_entry, abs=1e-11)
    assert new.source_position_at_time(0, 120) == pytest.approx(sliced.source_position_at_time(0, 120), abs=1e-11)
    assert new.timeline_to_source_frame_pair(0, 120) == pytest.approx(
        sliced.timeline_to_source_frame_pair(0, 120), abs=1e-11)
    assert new.curve_offset == sliced.curve_offset
    assert new.curve_length == sliced.curve_length
    assert sliced.to_dict() == before
    restored = SpeedParams.from_dict(json.loads(json.dumps(new.to_dict())))
    assert restored.to_dict() == new.to_dict()


class _Host(EditControllerMixin):
    def _schedule_auto_render_cache(self) -> None:
        pass

    def _schedule_audio_mixer_sync(self, delay_ms: int = 100) -> None:
        pass

    def _invalidate_motion_graphic_caches_for_timeline_change(self, before: Any, after: Any) -> None:
        pass


def test_inspector_speed_command_after_trim_keeps_entry_and_undo_redo(qtbot: Any) -> None:
    sequence = _trimmed()
    clip = sequence.get_clip_by_id("c")
    panel = EditPropertiesPanel()
    qtbot.addWidget(panel)
    panel.set_clips([clip], media_type="video")
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(sequence)
    host = _Host()
    host.edit_properties_panel = panel
    host.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
    host.edit_page = SimpleNamespace(_current_timeline=sequence)
    host.edit_page.set_timeline = lambda value: setattr(host.edit_page, "_current_timeline", value)
    host._current_edit_project = None
    host._current_timeline_sequences = {"t": sequence}
    host.timeline_preview_renderer = None
    host.edit_info_panel = None
    view.timeline_changed.connect(host._on_edit_timeline_changed)
    panel.clips_param_batch_changed.connect(host._on_edit_clips_param_batch_changed)
    panel.clip_param_changed.connect(host._on_edit_clip_param_changed)
    panel._route_clip_param("c", "speed", "speed", 2.0)
    changed = view._timeline.get_clip_by_id("c")
    assert changed.timeline_duration == 40
    assert changed.timeline_to_source_frame(changed.timeline_start) == 120
    assert timeline_duration_for_source(changed.source_duration, changed.speed) == 40
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view._timeline.get_clip_by_id("c").to_dict() == clip.to_dict()
    view.redo()
    assert view._timeline.get_clip_by_id("c").to_dict() == changed.to_dict()
