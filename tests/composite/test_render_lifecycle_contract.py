"""P0-01/P0-08 regression tests for cooperative render shutdown."""

import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import pytest
from PyQt6.QtCore import QCoreApplication

from src.features.composite.render.composite_render_worker import (
    CompositeRenderExporter,
    CompositeRenderWorker,
)
from src.features.composite.render.composite_renderer import CompositeRenderer
from src.features.composite.ui.composite_page import CompositePage


pytestmark = pytest.mark.composite_core


@pytest.fixture(scope="module")
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication([])
    return app


def test_worker_cancel_is_thread_safe_and_resettable():
    worker = CompositeRenderWorker()
    assert worker._cancel_event.is_set() is False

    worker.request_cancel()
    assert worker._cancel_requested is True
    assert worker._cancel_event.is_set() is True

    worker.reset_cancel()
    assert worker._cancel_requested is False
    assert worker._cancel_event.is_set() is False


def test_exporter_cleanup_stops_idle_worker_thread(qt_app):
    exporter = CompositeRenderExporter()
    try:
        assert exporter._worker_thread.isRunning()
        assert exporter.cleanup(timeout_ms=1000) is True
        assert exporter._worker_thread.isRunning() is False
        # Teardown is idempotent; callers may clean up from both a terminal
        # callback and page close.
        assert exporter.cleanup(timeout_ms=1000) is True
    finally:
        exporter.cleanup(timeout_ms=1000)


def test_renderer_cleanup_is_idempotent_and_continues_after_one_release_error(monkeypatch):
    from src.features.composite.render3d.mesh_vbo_pool import MeshVBOPool

    renderer = SimpleNamespace()
    renderer._cleanup_lock = threading.Lock()
    renderer._cleanup_started = False
    renderer._cleanup_complete = threading.Event()
    renderer._initialized = True
    renderer._release_output_buffers = Mock()
    renderer.fbo_ring = [None, None, None]
    renderer.tex_ring = [None, None, None]
    renderer.write_idx = 0
    renderer._pbo_readback = Mock()
    pbo_readback = renderer._pbo_readback
    renderer._quad_vbo = Mock()
    renderer._vao_cache = {"ok": Mock(), "bad": Mock()}
    renderer._vao_cache["bad"].release.side_effect = RuntimeError("driver error")
    renderer._cache_manager = Mock()
    renderer._resource_pool = Mock()
    renderer.fb_manager = Mock()
    renderer._render_nodes = {"node": Mock(cleanup=Mock())}
    renderer._shader_lib = Mock()
    renderer._compute_shader_manager = Mock()
    renderer.ctx = object()
    released_contexts = []
    monkeypatch.setattr(
        MeshVBOPool, "release_context",
        staticmethod(lambda ctx: released_contexts.append(ctx)),
    )

    CompositeRenderer._cleanup(renderer)
    CompositeRenderer._cleanup(renderer)

    assert renderer._cleanup_complete.is_set()
    pbo_readback.cleanup.assert_called_once()
    renderer._cache_manager.clear.assert_called_once()
    renderer._resource_pool.cleanup.assert_called_once()
    assert renderer._render_nodes == {}
    assert len(released_contexts) == 1
    assert released_contexts[0] is renderer.ctx


def test_composite_page_stop_playback_does_not_create_timer():
    timer = Mock()
    renderer = Mock()
    page = SimpleNamespace(
        _comp_playback_timer=timer,
        _comp_playing=True,
        _drive_active=True,
        _renderer=renderer,
        _notify_kf_panel_play_state=Mock(),
    )

    CompositePage._stop_comp_playback(page)

    timer.stop.assert_called_once_with()
    renderer.stop_playback_drive.assert_called_once_with()
    page._notify_kf_panel_play_state.assert_called_once_with()
    assert page._comp_playing is False
    assert page._drive_active is False
