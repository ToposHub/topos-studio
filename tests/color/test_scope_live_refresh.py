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
