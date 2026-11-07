"""Regression tests for atomic ColorStack switching."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.app.controllers.edit_controller import EditControllerMixin
from src.app.controllers.page_controller import PageManagementMixin


class _GradingPanel:
    def __init__(self, stack_id: str) -> None:
        self.current_stack_id = stack_id
        self._save_callback = MagicMock()
        self.trigger_save = MagicMock()

    def get_current_params(self) -> dict[str, float]:
        return {"exposure": 1.0}


class _Controller(EditControllerMixin):
    def __init__(self, stack_id: str) -> None:
        self.grading_panel = _GradingPanel(stack_id)
        self._grading_save_stack_id = stack_id


def test_switch_flushes_the_departing_color_stack() -> None:
    controller = _Controller('stack-a')

    controller._flush_active_grading_stack_before_switch('stack-b')

    controller.grading_panel.trigger_save.assert_called_once_with()


def test_switch_does_not_flush_when_reopening_the_same_stack() -> None:
    controller = _Controller('stack-a')

    controller._flush_active_grading_stack_before_switch('stack-a')

    controller.grading_panel.trigger_save.assert_not_called()


def test_hydration_and_stale_panel_signals_cannot_write_a_stack() -> None:
    controller = _Controller('stack-b')
    controller._color_stack_hydration_in_progress = True

    controller._on_grading_panel_changed('stack-b')

    controller.grading_panel._save_callback.assert_not_called()

    controller._color_stack_hydration_in_progress = False
    controller._on_grading_panel_changed('stack-a')

    controller.grading_panel._save_callback.assert_not_called()


def test_timeline_media_reset_cannot_overwrite_departing_stack() -> None:
    """load_video.reset_all emits grading_changed while the old stack is active."""
    controller = _Controller('stack-a')

    def reset_controls() -> None:
        assert controller._color_stack_hydration_in_progress is True
        controller._on_grading_panel_changed('stack-a')

    PageManagementMixin._run_color_stack_hydration_step(controller, reset_controls)

    assert getattr(controller, '_color_stack_hydration_in_progress', False) is False
    controller.grading_panel._save_callback.assert_not_called()


def test_grading_drag_commits_color_stack_only_when_interaction_ends() -> None:
    """A high-frequency live preview must produce one project-level commit."""
    controller = _Controller('stack-a')
    controller._on_grading_changed_for_color_stack = MagicMock()

    controller._on_grading_panel_changed('stack-a')

    assert controller._pending_grading_color_stack == (
        'stack-a',
        {'exposure': 1.0},
    )
    controller._on_grading_changed_for_color_stack.assert_not_called()

    controller._commit_pending_grading_color_stack('stack-a')

    controller._on_grading_changed_for_color_stack.assert_called_once_with(
        'stack-a', {'exposure': 1.0}
    )
    assert controller._pending_grading_color_stack is None


def test_stack_hydration_invalidates_and_refreshes_the_video_preview() -> None:
    """Saved controls must also replace cached realtime playback params."""
    controller = _Controller('stack-a')
    controller.current_file_is_video = True
    controller._invalidate_video_render_params_if_playing = MagicMock()
    controller.video_controller = MagicMock()
    controller.video_controller.is_video_loaded.return_value = True
    controller.video_controller.refresh_current_frame_view.return_value = True

    PageManagementMixin._refresh_video_preview_after_color_stack_hydration(controller)

    controller._invalidate_video_render_params_if_playing.assert_called_once_with()
    controller.video_controller.refresh_current_frame_view.assert_called_once_with()


def test_graph_gesture_cannot_queue_a_second_legacy_stack_commit() -> None:
    controller = _Controller('stack-a')
    controller.color_graph_host = MagicMock()
    controller.color_graph_host.grade_gesture_open.return_value = True
    controller._pending_grading_color_stack = ('stack-a', {'exposure': 0.0})
    controller._on_grading_changed_for_color_stack = MagicMock()
    controller._on_grading_panel_changed('stack-a')
    controller.color_graph_host.grade_gesture_open.return_value = False
    controller._commit_pending_grading_color_stack('stack-a')
    assert controller._pending_grading_color_stack is None
    controller._on_grading_changed_for_color_stack.assert_not_called()
