"""CompositeRenderer GPU scope handler integration test (real GL).

Runs ``_handle_render_gpu_scope`` against a live ModernGL context with a
realistic output ring, asserting that every scope mode yields a
``ScopeGpuFrame`` with valid texture descriptors — the exact code path the
Composite render thread executes (minus the thread itself, which the unit
tests cover).

Skipped automatically when no GPU / standalone context is available.
"""
import numpy as np
import pytest
import moderngl

from src.features.color.core.scope_types import (
    ScopeDemand, ScopeGpuFrame,
    PLAYBACK_ANALYSIS_SIZE,
)
from src.features.composite.render.composite_renderer import (
    CompositeCommand, CompositeRenderer,
)

MODES = ['waveform', 'vectorscope', 'split', 'parade', 'luma',
         'histogram', 'cie', 'falsecolor']


def _make_renderer_with_output(ctx) -> CompositeRenderer:
    """Minimal renderer double with a real output ring on ``ctx``."""
    r = CompositeRenderer.__new__(CompositeRenderer)
    r.ctx = ctx
    r.write_idx = 2  # front texture = (write_idx - 1) % 3 = 1
    r._gpu_scope_engine = None
    r._gpu_scope_failed = False
    out_w, out_h = 640, 360
    tex = ctx.texture((out_w, out_h), 4, dtype='f2')
    frame = np.zeros((out_h, out_w, 4), np.float32)
    frame[:, :, 0] = np.linspace(0, 1, out_w, dtype=np.float32)[None, :]
    frame[:, :, 1] = 0.4
    frame[:, :, 2] = 0.2
    frame[:, :, 3] = 1.0
    tex.write(frame.astype(np.float16))
    r.tex_ring = [None, tex, None]
    r.gpu_scope_ready = __import__('unittest.mock', fromlist=['Mock']).Mock()
    return r


@pytest.fixture(scope='module')
def gl_ctx():
    try:
        ctx = moderngl.create_standalone_context()
    except Exception:
        pytest.skip("no standalone GL context available")
    yield ctx
    ctx.release()


class TestCompositeGpuScopeHandler:
    def test_every_mode_emits_valid_frame(self, gl_ctx):
        r = _make_renderer_with_output(gl_ctx)
        for mode in MODES:
            emitted = []
            r.gpu_scope_ready.emit = lambda f, _e=emitted: _e.append(f)
            demand = ScopeDemand(
                revision=3, source_revision=9, mode=mode,
                display_size=(400, 300), analysis_size=PLAYBACK_ANALYSIS_SIZE,
            )
            CompositeRenderer._handle_render_gpu_scope(r, demand)
            assert len(emitted) == 1, f"{mode}: no frame emitted"
            frame = emitted[0]
            assert isinstance(frame, ScopeGpuFrame)
            assert frame.revision == 3
            assert frame.analysis_ms >= 0.0
            prefix = mode if mode != 'split' else None
            if prefix:
                tex_id = getattr(frame, f'{prefix}_texture_id')
                size = getattr(frame, f'{prefix}_size')
                assert tex_id is not None and tex_id > 0
                assert size[0] > 0 and size[1] > 0
                assert gl_ctx is not None

    def test_stale_demand_type_is_ignored(self, gl_ctx):
        r = _make_renderer_with_output(gl_ctx)
        emitted = []
        r.gpu_scope_ready.emit = lambda f, _e=emitted: _e.append(f)
        CompositeRenderer._handle_render_gpu_scope(r, {"not": "a demand"})
        assert emitted == []

    def test_command_is_registered(self):
        assert hasattr(CompositeCommand, "RENDER_GPU_SCOPE")
        assert CompositeCommand.RENDER_GPU_SCOPE == "render_gpu_scope"

    def test_drive_consumption_executes_queued_demand(self, gl_ctx):
        """Drive-loop consumer runs the queued demand on the real GL lane."""
        import queue

        r = _make_renderer_with_output(gl_ctx)
        r.command_queue = queue.Queue()
        emitted = []
        r.gpu_scope_ready.emit = lambda f, _e=emitted: _e.append(f)
        r.command_queue.put((CompositeCommand.RENDER_GPU_SCOPE, ScopeDemand(
            revision=4, source_revision=11, mode='waveform',
            display_size=(400, 300), analysis_size=PLAYBACK_ANALYSIS_SIZE,
        )))
        r.command_queue.put((CompositeCommand.CLEANUP_NODE, {"node_id": "x"}))

        CompositeRenderer._consume_pending_scope_demand(r)

        assert len(emitted) == 1
        assert emitted[0].revision == 4
        assert emitted[0].waveform_texture_id > 0
        # The non-scope command keeps its place for the regular command loop.
        assert r.command_queue.get_nowait()[0] == CompositeCommand.CLEANUP_NODE
        assert r.command_queue.empty()
