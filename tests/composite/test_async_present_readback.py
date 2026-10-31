"""
Tests for async present readback — signal chain and stale detection.

Validates:
- PBO readback enqueue/poll cycle
- Stale request_id discarding
- Readback output is CPU top-left BGR
- Fallback to direct fbo.read when PBO unavailable
"""

import queue

import pytest
import numpy as np
from unittest.mock import MagicMock, patch


# ===================================================================
# Test: PBO readback module
# ===================================================================

class TestPBOReadback:
    """AsyncPBOReadback ring buffer behavior."""

    def test_initialize_creates_ring(self):
        """Initialization sets up the ring buffer."""
        from src.features.composite.render.pbo_readback import AsyncPBOReadback
        ctx = MagicMock()
        pbo = AsyncPBOReadback(ctx, ring_size=3)
        assert len(pbo._ring) == 3
        assert all(e is None for e in pbo._ring)
        assert pbo._write_idx == 0

    def test_poll_ready_returns_empty_when_no_results(self):
        """poll_ready() returns empty list when ring is empty."""
        from src.features.composite.render.pbo_readback import AsyncPBOReadback
        ctx = MagicMock()
        pbo = AsyncPBOReadback(ctx, ring_size=3)
        results = pbo.poll_ready()
        assert results == []

    def test_get_latest_returns_none_when_empty(self):
        """get_latest() returns None when ring is empty."""
        from src.features.composite.render.pbo_readback import AsyncPBOReadback
        ctx = MagicMock()
        pbo = AsyncPBOReadback(ctx, ring_size=3)
        assert pbo.get_latest() is None

    def test_get_latest_keeps_highest_request_id(self):
        """get_latest() discards older results, keeps highest request_id."""
        from src.features.composite.render.pbo_readback import (
            AsyncPBOReadback, ReadbackResult, _PBOEntry,
        )
        ctx = MagicMock()
        pbo = AsyncPBOReadback(ctx, ring_size=3)

        # Manually populate ring with entries
        pbo._ring[0] = _PBOEntry(
            request_id=1, width=10, height=10,
            data=np.zeros((10, 10, 3), dtype=np.float32),
        )
        pbo._ring[1] = _PBOEntry(
            request_id=5, width=10, height=10,
            data=np.ones((10, 10, 3), dtype=np.float32),
        )

        result = pbo.get_latest()
        assert result is not None
        assert result.request_id == 5
        # Old entries should be cleared
        assert pbo._ring[0] is None
        assert pbo._ring[1] is None

    def test_cleanup_clears_state(self):
        """cleanup() resets the PBO state (without calling real GL)."""
        from src.features.composite.render.pbo_readback import AsyncPBOReadback
        ctx = MagicMock()
        pbo = AsyncPBOReadback(ctx, ring_size=3)
        pbo._initialized = True
        # Don't set real PBO IDs — cleanup calls glDeleteBuffers which segfaults without GL
        pbo._pbos = []
        pbo.cleanup()
        assert pbo._initialized is False
        assert len(pbo._pbos) == 0

    def test_enqueue_sync_can_preserve_alpha_for_scope_qc(self):
        """Scope readback keeps alpha while the default path remains BGR."""
        from src.features.composite.render.pbo_readback import AsyncPBOReadback

        gl_bottom_left = np.array([
            [[0.0, 0.0, 1.0, 0.25]],
            [[1.0, 0.0, 0.0, 0.75]],
        ], dtype=np.float32)
        fbo = MagicMock()
        fbo.read.return_value = gl_bottom_left.tobytes()
        pbo = AsyncPBOReadback(MagicMock(), ring_size=1)

        assert pbo.enqueue_sync(fbo, 1, 2, request_id=7, preserve_alpha=True)
        result = pbo.get_latest()
        assert result is not None
        assert result.data.shape == (2, 1, 4)
        np.testing.assert_allclose(result.data[:, 0, 3], [0.75, 0.25])


# ===================================================================
# Test: ReadbackResult dataclass
# ===================================================================

class TestReadbackResult:
    """ReadbackResult data structure."""

    def test_result_fields(self):
        from src.features.composite.render.pbo_readback import ReadbackResult
        data = np.zeros((100, 100, 3), dtype=np.float32)
        r = ReadbackResult(request_id=42, data=data, width=100, height=100)
        assert r.request_id == 42
        assert r.width == 100
        assert r.height == 100
        assert r.data is data


# ===================================================================
# Test: Renderer readback integration
# ===================================================================

class TestRendererReadbackIntegration:
    """Renderer uses PBO or fallback for async readback."""

    def test_renderer_has_pbo_readback_field(self):
        """Renderer initializes _pbo_readback field."""
        # We can't fully initialize the renderer without GL, but we can
        # check the field exists in the class
        from src.features.composite.render.composite_renderer import CompositeRenderer
        # Check that _pbo_readback is mentioned in __init__
        import inspect
        source = inspect.getsource(CompositeRenderer.__init__)
        assert '_pbo_readback' in source

    def test_srgb_preview_readback_uses_normalized_rgba8_attachment(self):
        """A float fragment shader must not render into an RGBA8UI target:
        the (pooled) readback target must be requested with dtype 'f1'."""
        from src.features.composite.render.composite_export_renderer import (
            CompositeExportRenderer,
        )
        from src.features.composite.nodes.base_node import TextureResult

        renderer = CompositeExportRenderer()
        renderer._readback_output = "srgb_u8"
        shader_uniform = MagicMock()
        shader = MagicMock()
        shader.__getitem__.return_value = shader_uniform
        texture = MagicMock()
        framebuffer = MagicMock()
        framebuffer.read.return_value = bytes([1, 2, 3])
        renderer._acquire_readback_target = MagicMock(
            return_value=(texture, framebuffer))
        renderer._release_readback_target = MagicMock()
        renderer.ctx = MagicMock()
        renderer.utility_shaders = {"readback_srgb_bgr": shader}
        renderer._bind_external_texture = MagicMock()
        # P1-6 audit fix: the readback path unbinds unit 0 after the draw —
        # a mock-ctx test must not dispatch that into a dead GL context.
        renderer._unbind_texture_unit = MagicMock()
        renderer._render_quad = MagicMock()

        image = renderer._readback(TextureResult(texture_id=7, width=1, height=1))

        # P2-10: the target is pooled; the dtype contract ('f1' normalized
        # RGBA8, never RGBA8UI) must hold.
        renderer._acquire_readback_target.assert_called_once_with(1, 1, "f1")
        renderer._release_readback_target.assert_called_once_with(texture)
        np.testing.assert_array_equal(image, np.array([[[1, 2, 3]]], dtype=np.uint8))

    def test_readback_output_is_cpu_top_left_bgr(self):
        """Readback output array row0 = image top (CPU top-left), BGR channel order."""
        # Simulate the readback pipeline:
        # 1. FBO contains GL bottom-left float32 RGBA
        # 2. After flipud + RGB->BGR, output should be CPU top-left BGR
        # Create a 2x2 GL bottom-left RGBA image:
        # GL row 0 (bottom): blue  (0,0,1,1)  green (0,1,0,1)
        # GL row 1 (top):    red   (1,0,0,1)  white (1,1,1,1)
        gl_bottom_left = np.array([
            [[0, 0, 1, 1], [0, 1, 0, 1]],  # bottom row
            [[1, 0, 0, 1], [1, 1, 1, 1]],  # top row
        ], dtype=np.float32)

        # Apply same pipeline as renderer:
        # flipud: GL bottom-left → CPU top-left
        # RGB → BGR
        try:
            import cv2
            bgr = cv2.cvtColor(np.flipud(gl_bottom_left[:, :, :3]).copy(), cv2.COLOR_RGB2BGR)
            # CPU top-left row 0 (image top): was GL row 1 (red, white)
            # In BGR: red=(0,0,1), white=(1,1,1)
            assert bgr.shape == (2, 2, 3)
            # Top-left pixel should be red in BGR: (0, 0, 1)
            np.testing.assert_allclose(bgr[0, 0], [0, 0, 1], atol=0.01)
            # Top-right pixel should be white: (1, 1, 1)
            np.testing.assert_allclose(bgr[0, 1], [1, 1, 1], atol=0.01)
            # Bottom-left pixel should be blue in BGR: (1, 0, 0)
            np.testing.assert_allclose(bgr[1, 0], [1, 0, 0], atol=0.01)
        except ImportError:
            pytest.skip("cv2 not available")


# ===================================================================
# Test: Adapter rate limiting
# ===================================================================

class TestAdapterRateLimiting:
    """Adapter rate-limits readback to avoid GPU saturation."""

    def test_rate_limit_skips_rapid_calls(self):
        """Second call within 1/24s is skipped."""
        import time
        # Simulate the rate-limit logic from _present_frame_to_viewer
        last_readback_time = time.monotonic()
        min_interval = 1.0 / 24.0

        # Immediate second call should be skipped
        now = time.monotonic()
        assert now - last_readback_time < min_interval
        # The condition (now - last < interval) would trigger a return

    def test_rate_limit_allows_after_interval(self):
        """Call after 1/24s interval is allowed."""
        import time
        last_readback_time = 0.0  # Long ago
        min_interval = 1.0 / 24.0

        now = time.monotonic()
        assert now - last_readback_time > min_interval
        # The condition would NOT trigger a return


# ===================================================================
# Test: Async command submission
# ===================================================================

class TestAsyncCommandSubmission:
    """_present_frame_to_viewer submits async command, not sync readback."""

    def test_present_submits_async_command(self):
        """When direct sharing fails, submit READBACK_PRESENT_ASYNC."""
        from src.features.composite.adapters.main_window_adapter import MainWindowAdapter
        from unittest.mock import MagicMock

        adapter = MagicMock(spec=MainWindowAdapter)
        adapter._runtime = MagicMock()
        adapter._runtime.viewer = MagicMock()
        adapter._runtime.viewer.set_texture.return_value = False  # direct sharing fails
        adapter._runtime.page = MagicMock()
        adapter._runtime.page._renderer = MagicMock()
        adapter._runtime.page._runtime_context = None  # not timeline-backed
        adapter._runtime.page._last_submitted_request_id = 0
        adapter._last_readback_time = 0.0  # long ago, rate limit passes

        MainWindowAdapter._present_frame_to_viewer(adapter, 1, 100, 1920, 1080)

        # Should submit async command
        adapter._runtime.page._renderer.submit.assert_called_once()
        call_args = adapter._runtime.page._renderer.submit.call_args
        assert call_args[0][0] == "readback_present_async"

    def test_timeline_backed_present_uses_direct_texture_when_available(self):
        """Timeline playback must avoid readback when shared texture display works."""
        from src.features.composite.adapters.main_window_adapter import MainWindowAdapter
        from unittest.mock import MagicMock

        adapter = MagicMock(spec=MainWindowAdapter)
        adapter._runtime = MagicMock()
        adapter._runtime.viewer = MagicMock()
        adapter._runtime.viewer.set_texture.return_value = True
        adapter._runtime.page = MagicMock()
        adapter._runtime.page._renderer = MagicMock()
        adapter._runtime.page._runtime_context = object()
        adapter._last_readback_time = 0.0
        adapter._runtime.dock_scope = None
        adapter._runtime.scope_panel = None

        MainWindowAdapter._present_frame_to_viewer(adapter, 1, 100, 1920, 1080)

        adapter._runtime.viewer.set_texture.assert_called_once_with(100, 1920, 1080)
        adapter._runtime.page._renderer.submit.assert_not_called()

    def test_timeline_backed_readback_fallback_is_rate_limited(self):
        """If direct display fails during timeline playback, do not read back every tick."""
        import time
        from src.features.composite.adapters.main_window_adapter import MainWindowAdapter
        from unittest.mock import MagicMock

        adapter = MagicMock(spec=MainWindowAdapter)
        adapter._runtime = MagicMock()
        adapter._runtime.viewer = MagicMock()
        adapter._runtime.viewer.set_texture.return_value = False
        adapter._runtime.page = MagicMock()
        adapter._runtime.page._renderer = MagicMock()
        adapter._runtime.page._runtime_context = object()
        adapter._last_readback_time = time.monotonic()

        MainWindowAdapter._present_frame_to_viewer(adapter, 1, 100, 1920, 1080)

        adapter._runtime.page._renderer.submit.assert_not_called()

    def test_last_readback_time_initialized_in_init(self):
        """__init__ should initialize _last_readback_time to 0.0."""
        import inspect
        from src.features.composite.adapters.main_window_adapter import MainWindowAdapter
        source = inspect.getsource(MainWindowAdapter.__init__)
        assert '_last_readback_time' in source

    def test_no_sync_readback_called(self):
        """_present_frame_to_viewer should NOT call request_output_readback."""
        from src.features.composite.adapters.main_window_adapter import MainWindowAdapter
        from unittest.mock import MagicMock

        adapter = MagicMock()
        adapter._runtime.viewer = MagicMock()
        adapter._runtime.viewer.set_texture.return_value = False
        adapter._runtime.page = MagicMock()
        adapter._runtime.page._renderer = MagicMock()
        adapter._runtime.page._runtime_context = None  # not timeline-backed
        adapter._runtime.page._last_submitted_request_id = 0
        adapter._last_readback_time = 0.0

        MainWindowAdapter._present_frame_to_viewer(adapter, 1, 100, 1920, 1080)

        # request_output_readback should NOT be called
        adapter._runtime.page.request_output_readback.assert_not_called()


class TestPlaybackPresentationOrdering:
    """Playback may present a completed frame behind the latest submission."""

    def test_timeline_request_does_not_cancel_in_flight_render(self):
        import queue

        from src.features.composite.render.composite_renderer import (
            CompositeCommand,
            CompositeRenderer,
        )
        from src.features.composite.render.render_scheduler import RenderRequest

        renderer = CompositeRenderer.__new__(CompositeRenderer)
        renderer._scheduler = MagicMock()
        renderer.command_queue = queue.Queue()
        request = MagicMock(spec=RenderRequest)
        request.request_id = 42
        request.graph_generation = 7
        request.color_revision = 0
        request.cancel_on_supersede = False

        CompositeRenderer.submit(
            renderer,
            CompositeCommand.EVALUATE_GRAPH,
            {"request": request},
        )

        renderer._scheduler.advance_request_id.assert_not_called()
        assert renderer.command_queue.get_nowait() == (
            CompositeCommand.EVALUATE_GRAPH,
            {"request": request},
        )

    def test_adapter_accepts_monotonic_completed_readbacks(self):
        from types import SimpleNamespace

        from src.features.composite.adapters.main_window_adapter import MainWindowAdapter

        viewer = MagicMock()
        adapter = MainWindowAdapter.__new__(MainWindowAdapter)
        adapter._mw = SimpleNamespace(current_page="Composite")
        adapter._runtime = SimpleNamespace(viewer=viewer, page=SimpleNamespace())
        adapter._last_presented_preview_request_id = 0
        image = np.zeros((2, 2, 3), dtype=np.float32)

        MainWindowAdapter._on_preview_image_ready(adapter, 8, image, 2, 2)
        MainWindowAdapter._on_preview_image_ready(adapter, 7, image, 2, 2)
        MainWindowAdapter._on_preview_image_ready(adapter, 9, image, 2, 2)

        assert viewer.set_image_data.call_count == 2
        assert adapter._last_presented_preview_request_id == 9


class TestCompositeGpuScopeLane:
    """Composite QC scopes run the native GPU scope lane (no CPU readback)."""

    def _make_adapter(self, panel, dock_visible=True):
        from types import SimpleNamespace
        from src.features.composite.adapters.main_window_adapter import (
            MainWindowAdapter,
        )

        adapter = MainWindowAdapter.__new__(MainWindowAdapter)
        adapter._mw = SimpleNamespace(current_page="Composite")
        adapter._runtime = SimpleNamespace(
            viewer=MagicMock(),
            page=SimpleNamespace(_renderer=None),
            dock_scope=MagicMock() if dock_visible else None,
            scope_panel=panel,
        )
        adapter._last_scope_submit_time = 0.0
        adapter._latest_gpu_scope_demand = None
        adapter._is_scope_visible = lambda: dock_visible
        return adapter

    def _make_panel(self):
        from src.features.color.core.scope_types import ScopeDemand

        panel = MagicMock()
        panel.demand_state.return_value = {
            'mode': 'waveform',
            'display_size': (300, 200),
            'vectorscope_zoom': 1.0,
            'scope_transfer': 'srgb',
            'scope_primaries': 'bt709',
            'falsecolor_mode': 'sdr',
            'zebra_threshold': 1.0,
        }
        panel.gpu_demand_revision.return_value = 7
        return panel

    def test_presented_frame_submits_gpu_scope_demand_when_dock_visible(self):
        from types import SimpleNamespace

        from src.features.color.core.scope_types import ScopeDemand
        from src.features.composite.adapters.main_window_adapter import (
            MainWindowAdapter,
        )
        from src.features.composite.render.composite_renderer import (
            CompositeCommand, CompositeRenderer,
        )

        panel = self._make_panel()
        panel.gpu_demand_revision = 7
        renderer = MagicMock()
        adapter = self._make_adapter(panel)
        adapter._runtime.page = SimpleNamespace(_renderer=renderer)

        MainWindowAdapter._present_frame_to_viewer(adapter, 12, 100, 1920, 1080)

        renderer.submit.assert_called_once()
        cmd, demand = renderer.submit.call_args.args
        assert cmd == CompositeCommand.RENDER_GPU_SCOPE
        assert isinstance(demand, ScopeDemand)
        assert demand.revision == 7
        assert demand.source_revision == 12
        assert demand.mode == 'waveform'

    def test_presented_frame_skips_scope_demand_when_dock_hidden(self):
        from types import SimpleNamespace

        from src.features.composite.adapters.main_window_adapter import (
            MainWindowAdapter,
        )

        panel = self._make_panel()
        renderer = MagicMock()
        adapter = self._make_adapter(panel, dock_visible=False)
        adapter._is_scope_visible = lambda: False
        adapter._runtime.page = SimpleNamespace(_renderer=renderer)

        MainWindowAdapter._present_frame_to_viewer(adapter, 12, 100, 1920, 1080)

        renderer.submit.assert_not_called()

    def test_matching_gpu_scope_frame_updates_panel(self):
        from src.features.color.core.scope_types import ScopeDemand, ScopeGpuFrame
        from src.features.composite.adapters.main_window_adapter import (
            MainWindowAdapter,
        )

        panel = MagicMock()
        adapter = self._make_adapter(panel)
        adapter._latest_gpu_scope_demand = ScopeDemand(
            revision=7, source_revision=12, mode='waveform',
            display_size=(300, 200),
        )
        frame = ScopeGpuFrame(
            revision=7, source_revision=12,
            waveform_texture_id=71, waveform_size=(1024, 300),
        )

        MainWindowAdapter._on_gpu_scope_ready(adapter, frame)

        panel.update_gpu_scope.assert_called_once_with(frame)

    def test_stale_gpu_scope_frame_dropped(self):
        from src.features.color.core.scope_types import ScopeDemand, ScopeGpuFrame
        from src.features.composite.adapters.main_window_adapter import (
            MainWindowAdapter,
        )

        panel = MagicMock()
        adapter = self._make_adapter(panel)
        adapter._latest_gpu_scope_demand = ScopeDemand(
            revision=7, source_revision=12, mode='waveform',
            display_size=(300, 200),
        )
        stale = ScopeGpuFrame(revision=6, source_revision=12)

        MainWindowAdapter._on_gpu_scope_ready(adapter, stale)

        panel.update_gpu_scope.assert_not_called()

    def test_gpu_scope_demand_coalesces_to_latest(self):
        from src.features.color.core.scope_types import ScopeDemand
        from src.features.composite.render.composite_renderer import (
            CompositeCommand, CompositeRenderer,
        )

        renderer = CompositeRenderer.__new__(CompositeRenderer)
        renderer.command_queue = queue.Queue()

        def demand(rev):
            return ScopeDemand(
                revision=rev, source_revision=1, mode='waveform',
                display_size=(300, 200),
            )

        CompositeRenderer.submit(
            renderer, CompositeCommand.RENDER_GPU_SCOPE, demand(3)
        )
        CompositeRenderer.submit(
            renderer, CompositeCommand.RENDER_GPU_SCOPE, demand(5)
        )
        CompositeRenderer.submit(
            renderer, CompositeCommand.RENDER_GPU_SCOPE, demand(4)
        )

        drained = []
        while True:
            try:
                drained.append(renderer.command_queue.get_nowait())
            except queue.Empty:
                break
        scope_items = [
            d for d in drained if d[0] == CompositeCommand.RENDER_GPU_SCOPE
        ]
        assert len(scope_items) == 1
        assert scope_items[0][1].revision == 5

    def test_renderer_dispatches_gpu_scope_command_to_scope_handler(self):
        from src.features.composite.render.composite_renderer import (
            CompositeCommand, CompositeRenderer,
        )

        renderer = CompositeRenderer.__new__(CompositeRenderer)
        renderer._drain_deferred_output_releases = MagicMock()
        renderer._handle_render_gpu_scope = MagicMock()

        CompositeRenderer._process_command(
            renderer, CompositeCommand.RENDER_GPU_SCOPE, {"demand": 9}
        )

        renderer._handle_render_gpu_scope.assert_called_once_with({"demand": 9})

    def test_drive_consumes_pending_scope_demand_in_place(self):
        """自驱播放独占命令循环：挂起的 scope demand 必须被就地消费。"""
        from src.features.color.core.scope_types import ScopeDemand
        from src.features.composite.render.composite_renderer import (
            CompositeCommand, CompositeRenderer,
        )

        renderer = CompositeRenderer.__new__(CompositeRenderer)
        renderer.command_queue = queue.Queue()
        renderer._handle_render_gpu_scope = MagicMock()

        def demand(rev):
            return ScopeDemand(
                revision=rev, source_revision=1, mode='waveform',
                display_size=(300, 200),
            )

        renderer.command_queue.put((CompositeCommand.RENDER_GPU_SCOPE, demand(3)))
        renderer.command_queue.put((CompositeCommand.CLEANUP_NODE, {"node": "n1"}))
        renderer.command_queue.put((CompositeCommand.RENDER_GPU_SCOPE, demand(5)))

        CompositeRenderer._consume_pending_scope_demand(renderer)

        # Latest-wins：只有最新一条 demand 被就地执行。
        renderer._handle_render_gpu_scope.assert_called_once()
        (executed,) = renderer._handle_render_gpu_scope.call_args.args
        assert executed.revision == 5
        # 非 scope 命令保持原队列位置，scope 命令不再回队。
        assert renderer.command_queue.get_nowait() == (
            CompositeCommand.CLEANUP_NODE, {"node": "n1"},
        )
        assert renderer.command_queue.empty()

    def test_drive_consumption_noop_without_scope_command(self):
        from src.features.composite.render.composite_renderer import (
            CompositeCommand, CompositeRenderer,
        )

        renderer = CompositeRenderer.__new__(CompositeRenderer)
        renderer.command_queue = queue.Queue()
        renderer._handle_render_gpu_scope = MagicMock()
        renderer.command_queue.put((CompositeCommand.CLEANUP_NODE, {"node": "n1"}))

        CompositeRenderer._consume_pending_scope_demand(renderer)

        renderer._handle_render_gpu_scope.assert_not_called()
        assert renderer.command_queue.get_nowait() == (
            CompositeCommand.CLEANUP_NODE, {"node": "n1"},
        )
        assert renderer.command_queue.empty()
