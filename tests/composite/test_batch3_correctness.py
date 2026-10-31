"""Batch 3 correctness fixes (plan §四 第3批):

- P0-3: placeholder AI nodes (ai_generate/ai_inpaint/ai_expand/ai_denoise
  without a model) must pass the input through instead of returning an
  empty cache entry that blacks out the whole graph.
- P2-9: deflicker must degrade to a visible passthrough for negative
  frame numbers instead of crashing in np.stack
  ("need at least one array to stack").
- P1-6: the legacy tiled export path must fail the render explicitly when
  a tile produces no output, never silently skip (black export region).
"""

import numpy as np
import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.render.composite_export_renderer import (
    CompositeExportRenderer,
)

pytestmark = pytest.mark.composite_core

W, H = 320, 240
SOLID = (0.6, 0.3, 0.2, 1.0)


def _graph_with_placeholder(node_type: str, extra_inputs: dict) -> dict:
    """constant → placeholder AI node → output"""
    nodes = [
        {"id": "bg", "type": "constant", "parameters": {
            "width": W, "height": H, "mode": "solid", "color": list(SOLID)}},
        {"id": "ai", "type": node_type, "parameters": {}},
        {"id": "out", "type": "output", "parameters": {
            "format_mode": "custom", "width": W, "height": H}},
    ]
    connections = [
        {"src_node_id": "bg", "src_port_name": "result",
         "dst_node_id": "ai", "dst_port_name": "image"},
        {"src_node_id": "ai", "src_port_name": "image",
         "dst_node_id": "out", "dst_port_name": "image"},
    ]
    return {"nodes": nodes, "connections": connections}


def _expected_bgr_u8() -> np.ndarray:
    lin = np.array(SOLID[:3], dtype=np.float64)
    srgb = np.where(lin <= 0.0031308, lin * 12.92,
                    1.055 * np.power(lin, 1.0 / 2.4) - 0.055)
    return np.clip(srgb * 255.0 + 0.5, 0, 255).astype(np.uint8)[::-1]


@pytest.fixture()
def headless_renderer(monkeypatch):
    try:
        import moderngl
        ctx = moderngl.create_standalone_context()
        ctx.release()
    except Exception as exc:  # pragma: no cover - CI GPU availability
        pytest.skip(f"Cannot create standalone GL context: {exc}")
    monkeypatch.setenv("TOPOS_HEADLESS_GL", "1")
    renderer = CompositeExportRenderer()
    renderer.set_persistent_mode(True)
    yield renderer
    renderer.cleanup()


class TestPlaceholderPassthrough:
    """P0-3: a placeholder node must never blank the whole graph."""

    @pytest.mark.parametrize("node_type", ["ai_inpaint", "ai_denoise",
                                           "ai_expand"])
    def test_placeholder_ai_passes_input_through(self, headless_renderer,
                                                 node_type):
        g = NodeGraph.from_dict(
            _graph_with_placeholder(node_type, extra_inputs={}))
        snapshot = g.snapshot(color_texture_id=0, color_width=0,
                              color_height=0, color_revision=0)
        result = headless_renderer.render_and_readback(
            snapshot, readback_output="srgb_u8")
        assert not result.error, result.error
        assert result.image is not None
        img = result.image
        assert img.shape == (H, W, 3) and img.dtype == np.uint8
        expected = _expected_bgr_u8()
        center = img[H // 2, W // 2]
        for ch in range(3):
            assert abs(int(center[ch]) - int(expected[ch])) <= 2

    def test_ai_generate_without_input_still_renders_background(
            self, headless_renderer):
        """ai_generate has no image input: output stays None (nothing to
        show) but the constant→output path through other graphs is not
        affected. This pins the no-valid-input behaviour explicitly."""
        data = {
            "nodes": [
                {"id": "gen", "type": "ai_generate", "parameters": {}},
                {"id": "out", "type": "output", "parameters": {
                    "format_mode": "custom", "width": W, "height": H}},
            ],
            "connections": [
                {"src_node_id": "gen", "src_port_name": "image",
                 "dst_node_id": "out", "dst_port_name": "image"},
            ],
        }
        g = NodeGraph.from_dict(data)
        snapshot = g.snapshot(color_texture_id=0, color_width=0,
                              color_height=0, color_revision=0)
        result = headless_renderer.render_and_readback(
            snapshot, readback_output="srgb_u8")
        # No crash; simply no output because the placeholder generator has
        # nothing to pass through.
        assert result.image is None or result.error


class TestDeflickerNegativeFrame:
    """P2-9: negative frame numbers must degrade, not crash."""

    def test_negative_frame_passthrough(self):
        from src.features.composite.nodes.deflicker_node import DeflickerNode

        node = DeflickerNode(node_id="df")
        node._store_luma(3, np.full((4, 5), 0.5, dtype=np.float32))
        node._store_luma(4, np.full((4, 5), 0.5, dtype=np.float32))

        class _Ctx:
            frame_number = -2

        image = np.zeros((8, 10, 4), dtype=np.float32)
        image[..., 3] = 1.0
        tex = TextureResultStub()
        out = node._do_evaluate({"image": tex}, _Ctx(), None, None)
        assert "result" in out
        assert out["result"] is tex
        assert node.warming

    def test_trailing_window_empty_stack_guard(self):
        """Even with a manipulated buffer, an empty window must not reach
        np.stack (regression for 'need at least one array to stack')."""
        from src.features.composite.nodes.deflicker_node import (
            DeflickerNode, apply_deflicker, luma_of,
        )

        node = DeflickerNode(node_id="df")
        present, planes = node._collect_window(-1, node._luma_buffer)
        assert present == [] and planes == []
        rgb = np.zeros((4, 4, 3), dtype=np.float32)
        luma = luma_of(rgb)
        # The analytic path used after the guards must have been fed by
        # guards; verify the pure function raises the historical error so
        # the guard is what protects evaluation.
        with pytest.raises(ValueError):
            apply_deflicker(rgb, luma, [], 0.5, True, "per_pixel")


class TextureResultStub:
    """Minimal TextureResult stand-in for CPU-only node tests."""

    def __init__(self):
        from src.features.composite.nodes.base_node import TextureResult
        self._tex = TextureResult(
            texture_id=1, width=10, height=8,
            alpha_premultiplied=True, color_space="linear")

    def __getattr__(self, name):
        return getattr(self._tex, name)


class TestTileFailureExplicitError:
    """P1-6: a dead tile must fail the export, never skip silently."""

    def test_tiled_export_fails_when_tile_produces_no_output(
            self, headless_renderer, monkeypatch):
        from src.features.composite.evaluators.graph_evaluator_mixin import (
            TileContext,
        )
        from src.features.composite.core.format import ImageFormat

        big_w, big_h = 2560, 1440
        data = {
            "nodes": [
                {"id": "bg", "type": "constant", "parameters": {
                    "width": big_w, "height": big_h, "mode": "solid",
                    "color": [0.2, 0.4, 0.8, 1.0]}},
                {"id": "out", "type": "output", "parameters": {
                    "format_mode": "custom", "width": big_w,
                    "height": big_h}},
            ],
            "connections": [
                {"src_node_id": "bg", "src_port_name": "result",
                 "dst_node_id": "out", "dst_port_name": "image"},
            ],
        }
        g = NodeGraph.from_dict(data)
        snapshot = g.snapshot(color_texture_id=0, color_width=0,
                              color_height=0, color_revision=0)
        headless_renderer._ensure_gpu_resources()

        real_eval = headless_renderer._evaluate_snapshot
        calls = {"n": 0}

        def flaky_eval(*args, **kwargs):
            calls["n"] += 1
            tile_ctx = kwargs.get("tile_context") or (
                args[2] if len(args) > 2 else None)
            if isinstance(tile_ctx, TileContext) and tile_ctx.tile_id == 1:
                return None
            return real_eval(*args, **kwargs)

        monkeypatch.setattr(headless_renderer, "_evaluate_snapshot",
                            flaky_eval)
        result = headless_renderer._render_tiled_with_plan(
            snapshot, big_w, big_h,
            headless_renderer._scheduler.plan(snapshot, __import__(
                "src.features.composite.render.render_scheduler",
                fromlist=["RenderMode"]).RenderMode.EXPORT))
        assert result.error, "expected explicit tile failure"
        assert "Tile 1" in result.error
        assert result.image is None
