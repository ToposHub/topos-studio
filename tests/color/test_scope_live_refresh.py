"""Focused tests for the GPU-only Color-page scope refresh path."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

import numpy as np

from src.app.controllers.layer_controller import LayerOpsMixin
from src.features.color.core.scope_types import PLAYBACK_ANALYSIS_SIZE


class _ScopeRefreshHost(LayerOpsMixin):
    """Minimal host for exercising the controller's GPU scope path."""

    def __init__(self) -> None:
        self.dock_analysis = MagicMock()
        self.dock_analysis.isVisible.return_value = True
        self.canvas = MagicMock()
        self.scope_panel = MagicMock()
        self.scope_panel.isVisible.return_value = True
        self.scope_panel.demand_state.return_value = {
            'mode': 'waveform',
            'display_size': (320, 180),
            'vectorscope_zoom': 1.0,
        }
        self.scope_panel.gpu_demand_revision = 3
        self.video_controller = None
        self.engine = MagicMock()
        self.engine.renderer = MagicMock()
        self.engine.renderer.gpu_scope_enabled = True
        self.engine.is_interacting = False
        self.engine.scope_source_revision = 10
        self.engine.scope_snapshots_enabled = False
        self.engine.get_active_layer.return_value = None
        self.finished: list[tuple[object, bool]] = []

    def on_render_finished(self, result_img: object, is_full_res: bool) -> None:
        self.finished.append((result_img, is_full_res))


def test_scope_submits_gpu_demand_during_color_interaction() -> None:
    host = _ScopeRefreshHost()
    host.engine.is_interacting = True

    with patch('src.app.controllers.layer_controller.time.monotonic', return_value=1.0):
        host.on_frame_ready(42)

    host.engine.request_scope_snapshot.assert_not_called()
    host.engine.request_gpu_scope.assert_called_once()
    demand = host.engine.request_gpu_scope.call_args.args[0]
    assert demand.mode == 'waveform'
    assert demand.source_revision == 10
    assert demand.is_playing is False
    assert demand.mask_enabled is False
    host.canvas.gl_viewport.makeCurrent.assert_not_called()
    assert len(host.finished) == 1


def test_mask_scope_demand_references_renderer_owned_mask_texture() -> None:
    host = _ScopeRefreshHost()
    host.engine.show_mask_mode = True
    host.engine.get_active_layer.return_value = SimpleNamespace(id='layer-7')

    with patch('src.app.controllers.layer_controller.time.monotonic', return_value=1.0):
        host.on_frame_ready(42)

    demand = host.engine.request_gpu_scope.call_args.args[0]
    assert demand.mask_enabled is True
    assert demand.mask_layer_id == 'layer-7'


def test_scope_gpu_demand_is_rate_limited() -> None:
    host = _ScopeRefreshHost()
    host.engine.is_interacting = True

    with patch('src.app.controllers.layer_controller.time.monotonic', side_effect=(1.0, 1.099)):
        host.on_frame_ready(42)
        host.on_frame_ready(42)

    host.engine.request_scope_snapshot.assert_not_called()
    host.engine.request_gpu_scope.assert_called_once()
    assert host.finished == [(None, False), (None, False)]


def test_scope_gpu_demand_refreshes_at_rate_limit_boundary() -> None:
    host = _ScopeRefreshHost()
    host.engine.is_interacting = True

    with patch('src.app.controllers.layer_controller.time.monotonic', side_effect=(1.0, 1.1)):
        host.on_frame_ready(42)
        host.on_frame_ready(42)

    host.engine.request_scope_snapshot.assert_not_called()
    assert host.engine.request_gpu_scope.call_count == 2


def test_grading_interaction_uses_playback_quality_scope() -> None:
    """Dragging a grade uses the same 512x288 density as playback."""
    host = _ScopeRefreshHost()
    host.engine.is_interacting = True

    with patch('src.app.controllers.layer_controller.time.monotonic', return_value=1.0):
        host.on_frame_ready(42)

    demand = host.engine.request_gpu_scope.call_args.args[0]
    assert demand.analysis_size == PLAYBACK_ANALYSIS_SIZE


def test_scope_snapshot_is_only_accepted_by_explicit_legacy_opt_in() -> None:
    host = _ScopeRefreshHost()
    host.engine.scope_snapshots_enabled = True
    image = np.zeros((2, 2, 3), dtype=np.float32)

    host.on_scope_snapshot_ready(image)

    host.scope_panel.update_scope.assert_called_once_with(image)


def test_disabled_scope_snapshots_ignore_an_already_queued_result() -> None:
    host = _ScopeRefreshHost()
    host.engine.scope_snapshots_enabled = False

    host.on_scope_snapshot_ready(np.zeros((2, 2, 3), dtype=np.float32))

    host.scope_panel.update_scope.assert_not_called()


def test_hidden_scope_panel_never_requests_scope_work() -> None:
    host = _ScopeRefreshHost()
    host.scope_panel.isVisible.return_value = False

    host.on_frame_ready(42)

    host.engine.request_scope_snapshot.assert_not_called()
    host.engine.request_gpu_scope.assert_not_called()


def test_playback_uses_gpu_scope_without_cpu_snapshot() -> None:
    host = _ScopeRefreshHost()
    host.video_controller = MagicMock()
    host.video_controller.is_playing = True

    host.on_frame_ready(42)

    host.engine.request_scope_snapshot.assert_not_called()
    host.engine.request_gpu_scope.assert_called_once()
    demand = host.engine.request_gpu_scope.call_args.args[0]
    assert demand.analysis_size == PLAYBACK_ANALYSIS_SIZE


def test_scope_ready_rejects_frame_older_than_latest_demand() -> None:
    host = _ScopeRefreshHost()
    host._latest_gpu_scope_demand = SimpleNamespace(
        revision=3, source_revision=10,
    )

    host.on_gpu_scope_ready(SimpleNamespace(revision=3, source_revision=9))

    host.scope_panel.update_gpu_scope.assert_not_called()


def test_scope_ready_accepts_newer_front_for_current_demand() -> None:
    host = _ScopeRefreshHost()
    host._latest_gpu_scope_demand = SimpleNamespace(
        revision=3, source_revision=10,
    )
    frame = SimpleNamespace(revision=3, source_revision=11)

    host.on_gpu_scope_ready(frame)

    host.scope_panel.update_gpu_scope.assert_called_once_with(frame)


def _show_curves(host: _ScopeRefreshHost) -> None:
    host.dock_curves = MagicMock()
    host.dock_curves.isVisible.return_value = True
    host.curves_panel = MagicMock()
    host.curves_panel.isVisible.return_value = True
    host.curves_widget = MagicMock()
    host.curves_widget.histogram_display_metrics.return_value = ((1024, 512), 1.0)


def test_curves_only_playback_submits_no_scope_work() -> None:
    host = _ScopeRefreshHost()
    _show_curves(host)
    host.scope_panel.isVisible.return_value = False
    host.video_controller = SimpleNamespace(is_playing=True)
    host.on_frame_ready(42)
    host.engine.request_gpu_scope.assert_not_called()


def test_playback_keeps_standalone_scope_but_excludes_curves_overlay() -> None:
    host = _ScopeRefreshHost()
    _show_curves(host)
    host.video_controller = SimpleNamespace(is_playing=True)
    host.on_frame_ready(42)
    assert host.engine.request_gpu_scope.call_args.args[0].histogram_overlay is False


def test_pause_refreshes_curves_and_cancels_inflight_demand_on_play() -> None:
    host = _ScopeRefreshHost()
    _show_curves(host)
    host._on_scope_play_state_changed(True)
    host.engine.discard_pending_gpu_scope.assert_called_once()
    assert host._latest_gpu_scope_demand is None
    host._on_scope_play_state_changed(False)
    assert host.engine.request_gpu_scope.call_args.args[0].histogram_overlay is True


def test_timeline_playback_also_freezes_curves() -> None:
    host = _ScopeRefreshHost()
    _show_curves(host)
    host.scope_panel.isVisible.return_value = False
    host._color_timeline_playback_controller = lambda: SimpleNamespace(is_playing=True)
    host.on_frame_ready(42)
    host.engine.request_gpu_scope.assert_not_called()


def test_late_scope_results_do_not_replace_frozen_curve_histogram() -> None:
    from src.features.color.core.scope_types import ScopeGpuFrame
    host = _ScopeRefreshHost()
    _show_curves(host)
    host._latest_gpu_scope_demand = SimpleNamespace(revision=3, source_revision=10)
    host.video_controller = SimpleNamespace(is_playing=True)
    frame = ScopeGpuFrame(3, 10, curves_histogram_texture_id=76,
                          curves_histogram_size=(1024, 512))
    host.on_gpu_scope_ready(frame)
    host.curves_widget.set_scope_frame.assert_not_called()
    host.video_controller.is_playing = False
    host.on_gpu_scope_ready(frame)
    host.curves_widget.set_scope_frame.assert_called_once_with(76, 1024, 512)


def test_curves_only_scrub_does_not_request_histogram_even_when_forced() -> None:
    host = _ScopeRefreshHost()
    _show_curves(host)
    host.scope_panel.isVisible.return_value = False
    host._curves_scope_scrubbing = True
    host._submit_gpu_scope_demand(is_playing=False, force=True)
    host.engine.request_gpu_scope.assert_not_called()


def test_curve_histogram_sampling_does_not_breathe_between_quality_tiers() -> None:
    from src.features.color.core.scope_types import HIGH_QUALITY_ANALYSIS_SIZE
    host = _ScopeRefreshHost()
    _show_curves(host)
    for interacting, last_activity in ((False, 1.0), (False, 0.0)):
        host.engine.is_interacting = interacting
        host._gpu_scope_last_activity = last_activity
        with patch('src.app.controllers.layer_controller.time.monotonic', return_value=1.1):
            host._submit_gpu_scope_demand(is_playing=False, force=True)
        assert host.engine.request_gpu_scope.call_args.args[0].analysis_size == HIGH_QUALITY_ANALYSIS_SIZE


def test_scrub_preserves_standalone_scopes_without_curve_overlay() -> None:
    host = _ScopeRefreshHost()
    _show_curves(host)
    host._viewport_scrub_active = True
    host.on_frame_ready(42)
    assert host.engine.request_gpu_scope.call_args.args[0].histogram_overlay is False


def test_scrub_start_cancels_pending_and_release_schedules_refresh() -> None:
    host = _ScopeRefreshHost()
    _show_curves(host)
    with patch('src.app.controllers.layer_controller.QTimer.singleShot') as schedule:
        host._on_scope_scrub_state_changed(True)
        assert host._scope_scrub_is_active()
        host.engine.discard_pending_gpu_scope.assert_called_once()
        host._on_scope_play_state_changed(False)  # Pause emitted by scrub start.
        assert host.engine.request_gpu_scope.call_args.args[0].histogram_overlay is False
        host._on_scope_scrub_state_changed(False)
        assert not host._scope_scrub_is_active()
        schedule.assert_called_once_with(150, host._on_scope_demand_changed)


def test_scrub_rejects_late_curve_descriptor() -> None:
    from src.features.color.core.scope_types import ScopeGpuFrame
    host = _ScopeRefreshHost()
    _show_curves(host)
    host._latest_gpu_scope_demand = SimpleNamespace(revision=3, source_revision=10)
    host._curves_scope_scrubbing = True
    host.on_gpu_scope_ready(ScopeGpuFrame(3, 10, curves_histogram_texture_id=76,
                                        curves_histogram_size=(1024, 512)))
    host.curves_widget.set_scope_frame.assert_not_called()


def test_gpu_presenter_preserves_frame_when_render_mutex_is_busy(qapp) -> None:
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    from src.shared.ui.widgets.gpu_scope_widget import GpuScopeWidget
    presenter = GpuScopeWidget()
    assert presenter.updateBehavior() == QOpenGLWidget.UpdateBehavior.PartialUpdate
    presenter._gl_ready = True
    presenter._frame_tex_id = 123
    presenter._mutex = MagicMock()
    presenter._mutex.tryLock.return_value = False
    presenter._blit_shared_texture = MagicMock()
    presenter.paintGL()
    presenter._blit_shared_texture.assert_not_called()
    presenter._mutex.unlock.assert_not_called()
    assert presenter._frame_tex_id == 123


def test_hsl_palette_demand_and_descriptor_target_the_active_hsl_canvas() -> None:
    from src.features.color.core.scope_types import ScopeGpuFrame
    host = _ScopeRefreshHost()
    _show_curves(host)
    host.curves_panel.curve_mode = 'HueSat'
    hsl = MagicMock()
    hsl.histogram_display_metrics.return_value = ((1200, 400), 1.0)
    host.hsl_curves_panel = SimpleNamespace(hsl_curves_widget=hsl)
    host.on_frame_ready(42)
    demand = host.engine.request_gpu_scope.call_args.args[0]
    assert demand.curves_mode == 'HueSat'
    assert demand.curves_display_size == (1200, 400)
    host.on_gpu_scope_ready(ScopeGpuFrame(3, 10, curves_histogram_texture_id=76,
                                         curves_histogram_size=(1200, 400)))
    hsl.set_scope_frame.assert_not_called()  # Late Custom texture.
    host.on_gpu_scope_ready(ScopeGpuFrame(3, 10, curves_histogram_texture_id=76,
                                         curves_histogram_size=(1200, 400), curves_mode='HueSat'))
    hsl.set_scope_frame.assert_called_once_with(76, 1200, 400)
    host.curves_widget.set_scope_frame.assert_not_called()


def test_curves_only_adjustment_never_requests_work_even_when_forced() -> None:
    host = _ScopeRefreshHost()
    _show_curves(host)
    host.scope_panel.isVisible.return_value = False
    host.engine.is_interacting = True
    for _ in range(10):
        host._submit_gpu_scope_demand(is_playing=False, force=True)
    host.engine.request_gpu_scope.assert_not_called()


def test_adjustment_retains_standalone_scope_without_curve_histogram() -> None:
    host = _ScopeRefreshHost()
    _show_curves(host)
    host.engine.is_interacting = True
    host.on_frame_ready(42)
    assert host.engine.request_gpu_scope.call_args.args[0].histogram_overlay is False


def test_inactive_curve_dock_tab_does_not_request_background_histogram() -> None:
    host = _ScopeRefreshHost()
    _show_curves(host)
    host.scope_panel.isVisible.return_value = False
    # Qt isVisible() can remain true on a tabified, unselected dock.
    host._on_curves_scope_visibility_changed(False)
    host.on_frame_ready(42)
    host.engine.request_gpu_scope.assert_not_called()
    host._on_curves_scope_visibility_changed(True)
    assert host.engine.request_gpu_scope.call_args.args[0].histogram_overlay is True


def test_curves_only_final_image_gets_one_histogram(qapp) -> None:
    from src.features.color.core.scope_types import ScopeGpuFrame
    host = _ScopeRefreshHost()
    _show_curves(host)
    host.scope_panel.isVisible.return_value = False
    host.engine.is_interacting = True
    host.on_frame_ready(42)
    host.engine.request_gpu_scope.assert_not_called()
    host.engine.is_interacting = False
    for _ in range(4):
        host._submit_gpu_scope_demand(is_playing=False, force=True)
    host.engine.request_gpu_scope.assert_called_once()
    host.on_gpu_scope_ready(ScopeGpuFrame(3, 10, curves_histogram_texture_id=76,
                                         curves_histogram_size=(1024, 512)))
    host.scope_panel.update_gpu_scope.assert_not_called()
    for _ in range(4):
        host._submit_gpu_scope_demand(is_playing=False, force=True)
    host.engine.request_gpu_scope.assert_called_once()
    host.engine.scope_source_revision = 11
    host._submit_gpu_scope_demand(is_playing=False, force=True)
    assert host.engine.request_gpu_scope.call_count == 2


def test_interaction_cancels_pending_and_rejects_late_curve_frame() -> None:
    from src.features.color.core.scope_types import ScopeGpuFrame
    host = _ScopeRefreshHost()
    _show_curves(host)
    host.on_frame_ready(42)
    host._freeze_curves_scope_for_interaction()
    host.engine.discard_pending_gpu_scope.assert_called_once()
    host.engine.is_interacting = True
    host.on_gpu_scope_ready(ScopeGpuFrame(3, 10, curves_histogram_texture_id=76,
                                         curves_histogram_size=(1024, 512)))
    host.curves_widget.set_scope_frame.assert_not_called()
