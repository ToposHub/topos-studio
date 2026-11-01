"""A completed Color preview must belong to the loaded and selected clip."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QWidget

from src.app.controllers.edit_controller import EditControllerMixin
from src.app.controllers import edit_controller


class EngineSignals(QObject):
    frame_ready = pyqtSignal(int)


class PreviewHost(QWidget, EditControllerMixin):
    pass


@pytest.fixture
def host(qtbot, monkeypatch):
    value = PreviewHost()
    qtbot.addWidget(value)
    value.current_page = "Color"
    value.engine = EngineSignals(value)
    pixmap = QPixmap(32, 18)
    value.grab_preview = MagicMock(return_value=pixmap)
    value.canvas = SimpleNamespace(gl_viewport=SimpleNamespace(grab=value.grab_preview))
    value.color_timeline_clip_strip = SimpleNamespace(
        _active_clip_id="a", set_graded_preview=MagicMock(),
    )
    value._color_timeline_clip_playback_context = {"clip": SimpleNamespace(clip_id="a")}
    value.callbacks = []
    monkeypatch.setattr(edit_controller.QTimer, "singleShot", lambda delay, fn: value.callbacks.append(fn))
    return value


def test_does_not_capture_outgoing_frame_into_incoming_clip(host):
    host.color_timeline_clip_strip._active_clip_id = "b"
    host._schedule_color_timeline_clip_preview_sync("b")
    host.engine.frame_ready.emit(1)
    host.callbacks[-1]()
    host.grab_preview.assert_not_called()
    host.color_timeline_clip_strip.set_graded_preview.assert_not_called()


def test_does_not_capture_after_selection_changes(host):
    host._schedule_color_timeline_clip_preview_sync("a")
    host.color_timeline_clip_strip._active_clip_id = "b"
    host.engine.frame_ready.emit(1)
    host.callbacks[-1]()
    host.grab_preview.assert_not_called()


def test_old_paint_callback_cannot_complete_a_new_capture(host):
    host._schedule_color_timeline_clip_preview_sync("a")
    host.engine.frame_ready.emit(1)
    old_fallback = host.callbacks[-1]
    old_fallback()
    assert host.grab_preview.call_count == 1
    host._schedule_color_timeline_clip_preview_sync("a")
    old_fallback()
    assert host.grab_preview.call_count == 1
    host.engine.frame_ready.emit(1)
    host.callbacks[-1]()
    assert host.grab_preview.call_count == 2
    host.engine.frame_ready.emit(1)
    host.callbacks[-1]()
    assert host.grab_preview.call_count == 2


def test_image_clip_without_video_context_can_update_preview(host):
    host._color_timeline_clip_playback_context = None
    host._schedule_color_timeline_clip_preview_sync("a")
    host.engine.frame_ready.emit(1)
    host.callbacks[-1]()
    host.color_timeline_clip_strip.set_graded_preview.assert_called_once()


def test_capture_waits_for_a_finished_frame(host):
    host._schedule_color_timeline_clip_preview_sync("a")
    assert host.callbacks == []
    host.grab_preview.assert_not_called()


def test_pending_source_cannot_capture_previous_frame(host):
    host.engine.renderer = SimpleNamespace(video_texture_pending=True)
    host._schedule_color_timeline_clip_preview_sync("a")
    host.engine.frame_ready.emit(1)
    host.callbacks[-1]()
    host.grab_preview.assert_not_called()
    host.engine.renderer.video_texture_pending = False
    host.engine.frame_ready.emit(2)
    host.callbacks[-1]()
    host.grab_preview.assert_called_once()


def test_neutral_commit_cancels_older_grade_capture(host):
    host._schedule_color_timeline_clip_preview_sync("a")
    host.engine.frame_ready.emit(1)
    old_callback = host.callbacks[-1]
    host._current_edit_project = SimpleNamespace(color_stacks={"s": object()})
    host._find_timeline_clip = lambda clip_id: SimpleNamespace(color_stack_id="s", color_shared=False)
    host.color_timeline_clip_strip.stack_has_grade = lambda *args: False
    host.color_timeline_clip_strip.invalidate_graded_preview_stacks = MagicMock()
    host._schedule_color_timeline_clip_preview_sync("a")
    old_callback()
    host.grab_preview.assert_not_called()
    host.color_timeline_clip_strip.invalidate_graded_preview_stacks.assert_called_once_with({"s"})


def test_paste_to_shared_peer_refreshes_current_owner(qtbot, monkeypatch):
    from src.features.color.adapters.color_stack_hydrator import ColorStackHydrator
    from src.features.color.core.grade_clipboard import GradeClipboard
    from src.features.color.core.layer import Layer
    from src.features.edit.core.commands import ProjectCommandStack, UpdateColorStackCommand
    from src.features.edit.core.project import EditProject
    from src.features.color.ui.color_wheel import GradingPanel

    value = PreviewHost()
    qtbot.addWidget(value)
    project, stack_id = EditProject.create(name="shared").create_color_stack()
    clips = {cid: SimpleNamespace(clip_id=cid, asset_id="asset", color_stack_id=stack_id)
             for cid in ("a", "b")}
    stack = ProjectCommandStack()
    value._current_edit_project = project
    value._find_timeline_clip = clips.get
    value.media_pool_panel = SimpleNamespace(get_command_stack=lambda: stack)
    value.engine = SimpleNamespace(render_pipeline=MagicMock())
    value.status_bar = SimpleNamespace(showMessage=MagicMock())
    value.color_timeline_clip_strip = SimpleNamespace(
        _active_clip_id="a", invalidate_graded_preview_stacks=MagicMock(),
    )
    value._schedule_color_timeline_clip_preview_sync = MagicMock()
    value._schedule_idle_color_timeline_clip_strip_refresh = MagicMock()
    value.grading_panel = GradingPanel()
    qtbot.addWidget(value.grading_panel)
    value.grading_panel.current_stack_id = stack_id
    value._grading_save_stack_id = stack_id
    value._pending_grading_color_stack = (stack_id, {"saturation": 0.0})

    def save_visible_params(params):
        command = UpdateColorStackCommand.create(stack_id=stack_id, grading_params=params)
        value._current_edit_project = stack.execute(command, value._current_edit_project)

    value.grading_panel.set_save_callback(save_visible_params)
    hydrate = MagicMock()
    monkeypatch.setattr(ColorStackHydrator, "apply_stack_to_engine", hydrate)
    donor = Layer("donor")
    donor.grading_params["gain"] = (0.1, 0.1, 0.1)
    donor.grading_params["saturation"] = -100.0
    clipboard = GradeClipboard()
    clipboard.copy_grade([donor])

    assert value._paste_grade_to_clip("b", {"primary"}, clipboard=clipboard)
    hydrate.assert_called_once_with(value._current_edit_project.color_stacks[stack_id], value.engine)
    value._schedule_color_timeline_clip_preview_sync.assert_called_once_with("a")
    value.color_timeline_clip_strip.invalidate_graded_preview_stacks.assert_called_once_with({stack_id})
    assert value.grading_panel.sat_slider.value() == -100.0
    assert value._pending_grading_color_stack is None
    value._flush_active_grading_stack_before_switch("another-stack")
    assert value._current_edit_project.color_stacks[stack_id].grading_params["saturation"] == -100.0
