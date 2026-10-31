"""Real Qt release routing and canonical Inspector refresh regressions."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.clip_params import SpeedParams, TransformParams
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel
from src.features.edit.ui.timeline_interaction import InteractionState
from src.features.edit.ui.timeline_items import EditTool
from src.features.edit.ui.timeline_view import TimelineView

pytestmark = pytest.mark.edit_core


def _sequence(speed: float = 1.0, reverse: bool = False) -> TimelineSequence:
    left = ClipInstance(
        clip_id="left", asset_id="missing", track_id="v", timeline_start=0,
        timeline_duration=100, source_start=100,
        source_duration=int(100 * speed), speed=SpeedParams(speed=speed, reverse=reverse),
    )
    right = replace(left, clip_id="right", timeline_start=100, source_start=400)
    return TimelineSequence(
        timeline_id="t", name="T", fps=24,
        video_tracks=(EditTrack(track_id="v", name="V", track_type="video", index=0,
                                clips=(left, right)),),
    )


def _roll_view(qtbot: Any, sequence: TimelineSequence) -> TimelineView:
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1400, 650)
    view.show()
    view.load_timeline(sequence)
    view.set_zoom(3)
    view.set_snap_enabled(False)
    view.set_tool(EditTool.ROLL)
    for item in view._items_map.values():
        item._max_source_frames = 1000
    QTest.qWait(5)
    return view


@pytest.mark.parametrize("speed,reverse", [(0.5, False), (2.0, False), (2.0, True)])
def test_real_roll_release_commits_preview_and_undo(
        qtbot: Any, speed: float, reverse: bool) -> None:
    sequence = _sequence(speed, reverse)
    view = _roll_view(qtbot, sequence)
    handle = next(iter(view._roll_handles.values()))
    point = view.mapFromScene(handle.mapToScene(handle.boundingRect().center()))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(view.viewport(), point + QPoint(30, 0), 10)
    assert view._interaction.state == InteractionState.ROLLING
    preview = {
        clip_id: (item._source_start, item._source_duration)
        for clip_id, item in view._items_map.items()
    }
    assert preview["left"] == sequence.get_clip_by_id("left").with_right_edge_source_values(110)
    assert preview["right"] == sequence.get_clip_by_id("right").with_left_edge_source_values(110)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point + QPoint(30, 0))
    assert view._timeline.get_clip_by_id("left").timeline_duration == 110
    for clip_id, values in preview.items():
        clip = view._timeline.get_clip_by_id(clip_id)
        assert (clip.source_start, clip.source_duration) == values
    assert len(view._command_stack._undo_stack) == 1
    assert view._interaction.is_idle
    assert view._roll_preview_state is None
    view.undo()
    assert view._timeline.get_clip_by_id("left") == sequence.get_clip_by_id("left")
    assert view._timeline.get_clip_by_id("right") == sequence.get_clip_by_id("right")


def test_roll_click_finishes_without_history_or_monitor(qtbot: Any) -> None:
    view = _roll_view(qtbot, _sequence())
    handle = next(iter(view._roll_handles.values()))
    point = view.mapFromScene(handle.mapToScene(handle.boundingRect().center()))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert view._interaction.is_idle
    assert view._roll_preview_state is None
    assert not view._trim_monitor_dialog.isVisible()
    assert not view._command_stack.can_undo()


class _Host(EditControllerMixin):
    def _schedule_auto_render_cache(self) -> None:
        pass

    def _schedule_audio_mixer_sync(self, delay_ms: int = 100) -> None:
        pass

    def _invalidate_motion_graphic_caches_for_timeline_change(self, before: Any, after: Any) -> None:
        pass


def test_undo_then_next_properties_field_uses_restored_snapshot(qtbot: Any) -> None:
    sequence = _sequence()
    panel = EditPropertiesPanel()
    qtbot.addWidget(panel)
    panel.set_clips(list(sequence.video_tracks[0].clips), media_type="video")
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(sequence)
    host = _Host()
    host.edit_properties_panel = panel
    host.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
    host.edit_page = SimpleNamespace(_current_timeline=sequence)
    host.edit_page.set_timeline = lambda seq: setattr(host.edit_page, "_current_timeline", seq)
    host._current_edit_project = None
    host._current_timeline_sequences = {sequence.timeline_id: sequence}
    host.timeline_preview_renderer = None
    host.edit_info_panel = None
    view.timeline_changed.connect(host._on_edit_timeline_changed)
    panel.clips_param_batch_changed.connect(host._on_edit_clips_param_batch_changed)
    panel._route_clip_param("left", "transform", "translate_x", 55.0)
    assert all(clip.transform.translate_x == 55 for clip in panel._current_clips)
    view.undo()
    assert all(clip.transform.translate_x == 0 for clip in panel._current_clips)
    panel._route_clip_param("left", "transform", "translate_y", 66.0)
    assert all(clip.transform.translate_x == 0 and clip.transform.translate_y == 66
               for clip in view._timeline.video_tracks[0].clips)


def test_mixed_numeric_same_as_primary_is_an_explicit_batch_edit(qtbot: Any) -> None:
    sequence = _sequence()
    clips = [replace(sequence.get_clip_by_id("left"),
                     transform=TransformParams(translate_x=10, translate_y=11)),
             replace(sequence.get_clip_by_id("right"),
                     transform=TransformParams(translate_x=20, translate_y=21))]
    panel = EditPropertiesPanel()
    qtbot.addWidget(panel)
    batches: list[list] = []
    panel.clips_param_batch_changed.connect(batches.append)
    panel.set_clips(clips, media_type="video")
    widget = panel._sections["clip:left:transform"]._param_widgets["translate_x"]["widget"]
    assert widget.isMixedValue()
    assert widget._value_label.text() == "—"
    assert not batches
    widget._begin_edit()
    widget._edit.setText("10")
    widget._commit_edit()
    assert len(batches) == 1
    assert [entry[3].translate_x for entry in batches[0]] == [10, 10]
    assert [entry[3].translate_y for entry in batches[0]] == [11, 21]
    panel.update_clips([replace(clip, transform=entry[3]) for clip, entry in zip(clips, batches[0])])
    assert not widget.isMixedValue()
    assert widget._value_label.text() != "—"


def test_mixed_checkbox_accepts_checked_without_losing_other_speed_fields(qtbot: Any) -> None:
    sequence = _sequence()
    clips = [sequence.get_clip_by_id("left"),
             replace(sequence.get_clip_by_id("right"), speed=SpeedParams(speed=2, reverse=True))]
    panel = EditPropertiesPanel()
    qtbot.addWidget(panel)
    batches: list[list] = []
    panel.clips_param_batch_changed.connect(batches.append)
    panel.set_clips(clips, media_type="video")
    checkbox = panel._sections["clip:left:speed"]._param_widgets["reverse"]["widget"]
    assert checkbox.checkState() == Qt.CheckState.PartiallyChecked
    checkbox.nextCheckState()
    assert len(batches) == 1
    assert all(entry[3].reverse for entry in batches[0])
    assert [entry[3].speed for entry in batches[0]] == [1, 2]


def test_raw_group_reset_derives_complete_defaults_for_every_selected_clip(qtbot: Any) -> None:
    from src.features.edit.core.clip_params import RawParams

    sequence = _sequence()
    clips = [replace(sequence.get_clip_by_id("left"), raw=RawParams(exposure_stops=2)),
             replace(sequence.get_clip_by_id("right"), raw=RawParams(wb_gain_r=2))]
    panel = EditPropertiesPanel()
    qtbot.addWidget(panel)
    batches: list[list] = []
    panel.clips_param_batch_changed.connect(batches.append)
    panel.set_clips(clips, media_type="video")
    section = panel._sections["clip:left:raw"]
    section._reset_btn.click()
    assert len(batches) == 1
    assert len(batches[0]) == 2
    assert all(entry[3] == RawParams() for entry in batches[0])


def test_roll_true_external_window_activation_cancels_before_late_release(qtbot: Any) -> None:
    from PyQt6.QtWidgets import QWidget

    view = _roll_view(qtbot, _sequence())
    view.activateWindow()
    view.setFocus()
    qtbot.waitUntil(view.hasFocus)
    handle = next(iter(view._roll_handles.values()))
    point = view.mapFromScene(handle.mapToScene(handle.boundingRect().center()))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(view.viewport(), point + QPoint(30, 0), 10)
    assert view._interaction.state == InteractionState.ROLLING
    external = QWidget()
    qtbot.addWidget(external)
    external.show()
    external.activateWindow()
    QTest.qWait(5)
    assert view._interaction.is_idle
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point + QPoint(30, 0))
    assert not view.can_undo()
    assert view._timeline.get_clip_by_id("left").timeline_duration == 100
    assert view._roll_preview_state is None


def test_application_deactivation_cancels_roll_after_passive_monitor_focus(qtbot: Any) -> None:
    from PyQt6.QtWidgets import QApplication

    view = _roll_view(qtbot, _sequence())
    handle = next(iter(view._roll_handles.values()))
    point = view.mapFromScene(handle.mapToScene(handle.boundingRect().center()))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(view.viewport(), point + QPoint(30, 0), 10)
    assert view._interaction.state == InteractionState.ROLLING
    QApplication.instance().applicationStateChanged.emit(Qt.ApplicationState.ApplicationInactive)
    assert view._interaction.is_idle
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point + QPoint(30, 0))
    assert not view.can_undo()
    assert view._timeline.get_clip_by_id("left").timeline_duration == 100
    assert view._roll_preview_state is None


@pytest.mark.parametrize("speed,source_limit,expected_delta", [(0.5, 155, 10), (2.0, 310, 5)])
def test_real_roll_clamps_source_headroom_in_timeline_frames(
        qtbot: Any, speed: float, source_limit: int, expected_delta: int) -> None:
    view = _roll_view(qtbot, _sequence(speed))
    view._items_map["left"]._max_source_frames = source_limit
    handle = next(iter(view._roll_handles.values()))
    point = view.mapFromScene(handle.mapToScene(handle.boundingRect().center()))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(view.viewport(), point + QPoint(150, 0), 10)
    assert view._roll_preview_state["delta"] == expected_delta
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point + QPoint(150, 0))
    clip = view._timeline.get_clip_by_id("left")
    assert clip.timeline_duration == 100 + expected_delta
    assert clip.source_start + clip.source_duration == source_limit
    assert len(view._command_stack._undo_stack) == 1
