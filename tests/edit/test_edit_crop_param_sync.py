"""Regression tests for Crop parameter edits and their interactive preview."""

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.clip_params import CropParams, SpeedParams
from src.features.edit.core.commands import CommandStack
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.edit_properties_panel import EditPropertiesPanel


def _make_timeline() -> TimelineSequence:
    clip = ClipInstance(
        clip_id="clip-1",
        asset_id="asset-1",
        track_id="track-1",
        timeline_start=0,
        timeline_duration=24,
        source_start=0,
        source_duration=24,
        speed=SpeedParams(speed=1.0),
        crop=CropParams(),
    )
    return TimelineSequence(
        timeline_id="timeline-1",
        name="Test",
        fps=24.0,
        width=1920,
        height=1080,
        sample_rate=48000,
    ).with_track_added(
        EditTrack(track_id="track-1", name="Video 1", track_type="video", index=0)
    ).with_clip_added("track-1", clip)


def test_consecutive_crop_edits_accumulate_all_sides_and_feather() -> None:
    """Editing one side must not rebuild the next value from a stale clip."""
    timeline = _make_timeline()
    panel = SimpleNamespace(_current_clips=[timeline.get_clip_by_id("clip-1")])

    def update_current_clip(clip) -> None:
        panel._current_clips[0] = clip

    panel.update_current_clip = update_current_clip

    controller = EditControllerMixin.__new__(EditControllerMixin)
    controller.edit_page = SimpleNamespace(
        _current_timeline=timeline,
        _command_stack=SimpleNamespace(
            execute=lambda command, current: command.apply(current),
        ),
    )
    controller.edit_properties_panel = panel
    controller.timeline_preview_renderer = None
    controller._request_program_viewer_update = lambda: None

    for param_name, value in (
        ("left", 25.0),
        ("right", 20.0),
        ("top", 15.0),
        ("bottom", 10.0),
        ("feather", 12.0),
    ):
        current = panel._current_clips[0].crop
        if param_name == "feather":
            new_crop = current.with_feather(value)
        else:
            new_crop = current.with_crop(**{param_name: value / 100.0})
        controller._on_edit_clip_param_changed(
            "clip-1", "crop", param_name, new_crop,
        )

    crop = controller.edit_page._current_timeline.get_clip_by_id("clip-1").crop
    assert crop == CropParams(
        left=0.25, right=0.20, top=0.15, bottom=0.10, feather=12.0,
    )


@pytest.mark.parametrize("control", ["widget", "slider"])
@pytest.mark.parametrize("finish", ["commit", "cancel", "no_change", "ungrab", "external_change"])
def test_crop_drag_previews_before_one_batch_commit(
    qtbot: Any, control: str, finish: str,
) -> None:
    timeline = _make_timeline()
    clip = timeline.get_clip_by_id("clip-1")
    other = replace(clip, clip_id="clip-2", timeline_start=24, crop=CropParams(top=0.1))
    timeline = timeline.with_clip_added("track-1", other)
    stack = CommandStack()
    view = SimpleNamespace(_timeline=timeline)
    previews = []
    restored = []
    requests = []
    controller = EditControllerMixin.__new__(EditControllerMixin)
    controller.edit_page = SimpleNamespace(_current_timeline=timeline)
    controller.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
    controller.timeline_preview_renderer = SimpleNamespace(
        update_sequence_silent=lambda sequence, *, preview_only: previews.append(sequence),
        set_sequence=restored.append,
    )
    controller._request_program_viewer_update = lambda: requests.append(True)

    def execute(command: Any) -> None:
        view._timeline = stack.execute(command, view._timeline)
        controller.edit_page._current_timeline = view._timeline

    view.execute_command = execute
    panel = EditPropertiesPanel()
    controller.edit_properties_panel = panel
    direct_commits = []
    panel.clip_param_changed.connect(lambda *update: direct_commits.append(update))
    panel.clips_param_batch_changed.connect(direct_commits.append)
    panel.crop_drag_preview.connect(controller._on_edit_crop_drag_preview)
    panel.crop_drag_finished.connect(controller._on_edit_crop_drag_finished)
    qtbot.addWidget(panel)
    panel.resize(520, 900)
    panel.set_clips([clip, other])
    panel.show()
    section = panel._sections["clip:clip-1:crop"]
    if section._is_collapsed:
        section.toggle_collapse()
    qtbot.waitUntil(section._content_widget.isVisible)
    widget = section._param_widgets["right"][control]
    start = (widget._handle_rect().center() if control == "slider"
             else QPoint(7, widget.height() // 2))
    end = start + QPoint(32, 0)
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(widget, end)
    qtbot.waitUntil(lambda: len(previews) > 0)

    preview_clip = previews[-1].get_clip_by_id("clip-1")
    preview_other = previews[-1].get_clip_by_id("clip-2")
    assert preview_clip.crop.right > 0.0
    assert preview_other.crop == preview_clip.crop.with_crop(top=0.1)
    assert requests and view._timeline is timeline and not stack.can_undo()

    expected = timeline
    if finish == "cancel":
        panel.clear()
        QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=end + QPoint(5, 0))
    elif finish == "ungrab":
        QApplication.sendEvent(widget, QEvent(QEvent.Type.UngrabMouse))
        QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=end + QPoint(5, 0))
    elif finish == "external_change":
        updated = clip.with_crop(CropParams(top=0.2))
        expected = timeline.with_clip_updated(updated)
        view._timeline = controller.edit_page._current_timeline = expected
        panel.update_clips([updated])
        QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=end + QPoint(5, 0))
    else:
        QTest.mouseRelease(
            widget, Qt.MouseButton.LeftButton, pos=end if finish == "commit" else start,
        )
    assert not direct_commits
    if finish == "commit":
        assert view._timeline.get_clip_by_id("clip-1").crop == preview_clip.crop
        assert view._timeline.get_clip_by_id("clip-2").crop == preview_other.crop
        undone = stack.undo(view._timeline)
        assert undone.get_clip_by_id("clip-1").crop == clip.crop
        assert undone.get_clip_by_id("clip-2").crop == other.crop
        assert not stack.can_undo()
        assert stack.redo(undone) == view._timeline
    else:
        assert not stack.can_undo()
        assert restored == [expected]
