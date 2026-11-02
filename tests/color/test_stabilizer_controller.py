"""Exercise the production palette/controller lifecycle without a GL renderer."""
from __future__ import annotations

import copy
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
from PyQt6.QtCore import QCoreApplication, QObject, QSize

from src.app.controllers.stabilizer_controller import StabilizerControllerMixin
from src.features.color.ui.tracker_panel import TrackerPanel
from src.shared.tracking.stabilization_state import validate_stabilization_state
from src.shared.tracking.tracker_2d import TrackResult
from src.shared.tracking.types import TrackTransform, TrackStatus, TrackingMode

pytestmark = pytest.mark.usefixtures('qapp')


class Host(QObject, StabilizerControllerMixin):
    def __init__(self) -> None:
        super().__init__()
        self.context = ('clip-a', '/tmp/a.mov', 19, 10, 29)
        self._tracker_frame_context = lambda: self.context
        self.video_controller = SimpleNamespace(
            is_video_loaded=MagicMock(return_value=True), stop_playback=MagicMock(),
            invalidate_cached_render_params=MagicMock(), _playback_clock=SimpleNamespace(fps=25.))
        self.canvas = SimpleNamespace(logical_size=QSize(1920, 1080), hide_tracker_overlay=MagicMock())
        self.engine = SimpleNamespace(video_stabilization={}, get_active_layer=lambda: None)
        self.engine.get_video_stabilization_state = lambda: copy.deepcopy(self.engine.video_stabilization)
        self.engine.set_video_stabilization_state = self.set_state
        self.grading_panel = SimpleNamespace(trigger_save=MagicMock())
        self.tracker_panel = TrackerPanel()
        self.request_render = MagicMock()
        self._trigger_autosave = MagicMock()
        self._push_mask_structure_snapshot = MagicMock()
        for name in ('on_interaction_started', 'on_interaction_ended',
                     '_on_tracker_graph_scrub_started', '_on_tracker_graph_frame_clicked',
                     '_on_tracker_graph_scrub_finished'):
            setattr(self, name, MagicMock())
        self._init_stabilizer()
        self.stabilization_service.start = MagicMock()
        self._sync_stabilizer_panel()

    def set_state(self, state: dict) -> None:
        self.engine.video_stabilization = validate_stabilization_state(state)

    def finish(self) -> None:
        transforms = {}
        for frame in range(10, 30):
            matrix = np.eye(3)
            matrix[0, 2] = (frame - 10) * 2. + (-1) ** frame
            transforms[frame] = TrackTransform.from_matrix(frame, matrix, 1., TrackStatus.VALID)
        self._on_stabilization_finished(
            self.stabilization_service.task_key('clip-a', '__camera__'), 1,
            TrackResult(generation=1, cancelled=False, transforms=transforms,
                        observations={}, diagnostics={}))
        self.wait_plans()

    def wait_plans(self) -> None:
        deadline = time.monotonic() + 3.
        while self.stabilization_plan_service.is_pending() and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(.001)
        assert not self.stabilization_plan_service.is_pending()


@pytest.fixture
def host(qapp):
    value = Host()
    yield value
    value._stabilizer_params_timer.stop()
    value.stabilization_service.shutdown()
    value.stabilization_plan_service.shutdown()
    value.tracker_panel.close()
    value.tracker_panel.deleteLater()
    qapp.processEvents()


def test_full_clip_analysis_without_window_or_mask_and_persistent_commit(host):
    host.tracker_panel.stabilizer.btn_stabilize.click()
    request = host.stabilization_service.start.call_args.kwargs
    assert (request['reference_frame'], request['frame_start'], request['frame_end']) == (10, 10, 29)
    assert request['shape_id'] == '__camera__'
    assert request['mode'] == TrackingMode.HOMOGRAPHY
    assert host._stabilizer_fps() == 25.
    host.finish()
    assert set(host.engine.video_stabilization['corrections']) == {str(f) for f in range(10, 30)}
    assert host._push_mask_structure_snapshot.call_count == 1
    host.grading_panel.trigger_save.assert_called_once()
    host.request_render.assert_called_once()
    host._trigger_autosave.assert_called_once()
    assert not host.tracker_panel.stabilizer._running


@pytest.mark.parametrize('action', ['cancel', 'switch', 'image'])
def test_cancel_source_change_and_image_reject_late_results(host, action):
    host._start_stabilization()
    if action == 'cancel':
        host._cancel_stabilization()
    elif action == 'switch':
        host.context = ('clip-b', '/tmp/b.mov', 3, 0, 49)
        host._sync_stabilizer_panel()
    else:
        host.video_controller.is_video_loaded.return_value = False
        host._sync_stabilizer_panel()
        assert not host.tracker_panel.stabilizer.btn_stabilize.isEnabled()
    host.finish()
    assert host.engine.video_stabilization == {}
    host.grading_panel.trigger_save.assert_not_called()
    assert not host.tracker_panel.stabilizer._running


def test_params_and_bypass_survive_pending_solver_change_and_reset(host):
    host._start_stabilization()
    host.finish()
    panel = host.tracker_panel.stabilizer
    panel.mode_combo.setCurrentIndex(panel.mode_combo.findData('translation'))
    panel.bypass_box.click()
    host._stabilizer_params_timer.stop()
    host._recompute_stabilizer()
    host.wait_plans()
    assert host.engine.video_stabilization['params']['bypass']
    assert host.engine.video_stabilization['params']['mode'] == 'perspective'
    assert panel.get_params()['mode'] == 'translation'
    host._reset_stabilization()
    assert host.engine.video_stabilization == {}
    assert panel.get_params()['mode'] == 'perspective'


def test_pending_parameter_edit_does_not_apply_to_a_different_clip(host):
    host._start_stabilization()
    host.finish()
    original = copy.deepcopy(host.engine.video_stabilization)
    host.tracker_panel.stabilizer.bypass_box.click()
    host._stabilizer_params_timer.stop()
    host.context = ('clip-b', '/tmp/b.mov', 3, 0, 49)
    host._recompute_stabilizer()
    host.wait_plans()
    assert host.engine.video_stabilization == original


def test_worker_start_error_does_not_escape_qt_slot(host):
    host.stabilization_service.start.side_effect = OSError('Cannot open source')
    host._start_stabilization()
    panel = host.tracker_panel.stabilizer
    assert not panel._running
    assert panel.status_label.text() == 'Cannot open source'


def test_camera_lock_ignores_disabled_strength_but_retains_control_values(host):
    from src.shared.tracking.stabilization_state import stabilization_matrix
    host._start_stabilization()
    host.finish()
    panel = host.tracker_panel.stabilizer
    panel.controls['strength'].setValue(0.)
    panel.camera_lock_box.click()
    host._stabilizer_params_timer.stop()
    host._recompute_stabilizer()
    host.wait_plans()
    assert panel.get_params()['strength'] == 0.
    assert stabilization_matrix(host.engine.video_stabilization, 20) is not None


def test_shared_title_reset_is_available_without_a_window(host):
    host.tracker_panel.title_mode_buttons['stabilizer'].click()
    assert host.tracker_panel.btn_clear.isEnabled()
    host._start_stabilization()
    host.finish()
    host.tracker_panel.btn_clear.click()
    assert host.engine.video_stabilization == {}
    host.tracker_panel.title_mode_buttons['window'].click()
    assert not host.tracker_panel.btn_clear.isEnabled()


def test_numeric_drag_reuses_single_global_gesture_snapshot(host):
    host._start_stabilization()
    host.finish()
    host._push_mask_structure_snapshot.reset_mock()
    panel = host.tracker_panel.stabilizer
    panel.interaction_started.emit()
    for value in (.3, .4):
        panel.controls['smooth'].setValue(value)
        host._stabilizer_params_timer.stop()
        host._recompute_stabilizer()
        host.wait_plans()
    panel.controls['smooth'].setValue(.5)
    panel.interaction_ended.emit()
    host.wait_plans()
    host.on_interaction_started.assert_called_once()
    host.on_interaction_ended.assert_called_once()
    host._push_mask_structure_snapshot.assert_not_called()
    assert host.engine.video_stabilization['params']['smooth'] == .5
