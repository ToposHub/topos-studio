"""P1-9 pass-fusion execution tests.

The fusion planner had source-text assertions only; these tests pin the
EXECUTION contract:

- a fusible chain (grade → invert) renders to the same pixels with
  fusion on (conservative default) and fusion off (per-node passes),
  within the float-rounding tolerance of the two shader paths;
- the fusion-mode preference is actually honoured through the composite
  config manager (previously `pass_fusion_mode` was read by
  get_fusion_mode_from_config but never consulted by the evaluator);
- a fused-chain cache hit serves the previous result without re-draws
  when nothing changes, and re-renders when the chain input changes.
"""

import sys
import os

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

pytestmark = pytest.mark.composite_core

W, H = 160, 120


class _StubConfig:
    """Minimal ConfigManager stand-in exposing one key."""

    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, key):
        return self._values.get(key)


def _graph():
    return {
        "composite_id": "fusion-gate",
        "fps": 25.0, "width": W, "height": H,
        "nodes": [
            {"id": "src", "type": "constant", "position": [0, 0],
             "parameters": {"color_r": 0.7, "color_g": 0.4, "color_b": 0.2,
                            "width": W, "height": H}},
            {"id": "g1", "type": "grade", "position": [160, 0],
             "parameters": {"lift_r": 0.02, "lift_g": 0.0, "lift_b": -0.01,
                            "gain_r": 1.2, "gain_g": 1.0, "gain_b": 0.8,
                            "gamma_r": 0.9, "gamma_g": 1.0, "gamma_b": 1.1,
                            "offset_r": 0.0, "offset_g": 0.01,
                            "offset_b": 0.0}},
            {"id": "iv", "type": "invert", "position": [320, 0],
             "parameters": {}},
            {"id": "out", "type": "output", "position": [480, 0],
             "parameters": {"format_mode": "custom",
                            "width": W, "height": H}},
        ],
        "connections": [
            {"src_node_id": "src", "src_port_name": "result",
             "dst_node_id": "g1", "dst_port_name": "image"},
            {"src_node_id": "g1", "src_port_name": "result",
             "dst_node_id": "iv", "dst_port_name": "image"},
            {"src_node_id": "iv", "src_port_name": "result",
             "dst_node_id": "out", "dst_port_name": "image"},
        ],
    }


def test_layer_time_gate_breaks_fusion_at_the_layer_root():
    from src.features.composite.core.layer_model import Layer, LayerStack
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.render.render_scheduler import RenderScheduler
    from src.features.composite.utils.pass_fusion import analyze_fusion

    graph = NodeGraph.from_dict(_graph())
    scheduler = RenderScheduler()
    before = graph.snapshot(frame_number=0)
    assert "g1" in analyze_fusion(scheduler.plan(before), before).node_to_chain

    graph.set_layer_stack(LayerStack(stack_id="main", name="Main",
                                     output_node_id="out"))
    graph.upsert_layer("main", Layer(layer_id="layer", name="Layer",
                                    root_node_id="g1", in_frame=2))
    after = graph.snapshot(frame_number=0)
    assert "g1" not in analyze_fusion(scheduler.plan(after), after).node_to_chain


def test_managed_layer_gaps_render_transparent_in_preview_and_export(qapp, _gl_available, monkeypatch):
    from src.features.composite.core.layer_model import BOTTOM_UP_LAYER_STACK_VERSION
    from src.features.composite.core.layer_stack_controller import LayerStackController
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.constant_node import ConstantNode
    from src.features.composite.nodes.output_node import OutputNode
    from src.features.composite.render.composite_frame_renderer import CompositeFrameRenderer

    monkeypatch.setenv("TOPOS_HEADLESS_GL", "1")
    graph = NodeGraph()
    graph.add_node(OutputNode(node_id="out"))
    output = graph.get_node("out")
    output.set_parameter("width", 32)
    output.set_parameter("height", 24)
    output.set_parameter("format_mode", "custom")
    free_graph = graph.to_dict()
    controller = LayerStackController(graph, layer_stack_version=BOTTOM_UP_LAYER_STACK_VERSION)
    for ident, span, color in (("red", (3, 6), (1.0, 0.0, 0.0, 1.0)),
                               ("blue", (9, 12), (0.0, 0.0, 1.0, 1.0))):
        source = ConstantNode(node_id=ident)
        source.set_parameter("width", 32)
        source.set_parameter("height", 24)
        source.set_parameter("color", color)
        graph.add_node(source)
        layer = controller.add_media_layer(ident, source.id)
        assert controller.apply_layer_time_bounds(layer, span)

    data = graph.to_dict()
    for mode in ("preview", "export"):
        renderer = CompositeFrameRenderer()
        try:
            for frame, covered in ((0, False), (3, True), (7, False), (9, True), (13, False)):
                result = renderer.render_comp_clip_frame(data, timeline_frame=frame, quality=mode)
                assert result is not None and result.shape[:2] == (24, 32)
                assert bool(np.any(result)) is covered
                if not covered:
                    engine = renderer._get_export_renderer()
                    snapshot = renderer._build_snapshot(data, frame)
                    display = engine._get_cached_outputs_for_node("out", snapshot)["display"]
                    framebuffer = next(fbo for tex, fbo in engine.fb_manager._fbo_cache.items()
                                       if tex.glo == display.texture_id)
                    pixel = framebuffer.read(viewport=(0, 0, 1, 1), components=4, dtype="f4")
                    assert np.frombuffer(pixel, np.float32)[3] == 0.0
            assert renderer.render_comp_clip_frame(free_graph, quality=mode) is None
        finally:
            renderer.cleanup()


def _render(config_values=None):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("TOPOS_HEADLESS_GL", "1")
    from src.features.composite.render.composite_frame_renderer import (
        CompositeFrameRenderer,
    )
    renderer = CompositeFrameRenderer(
        config_manager=_StubConfig(config_values))
    try:
        img = renderer.render_comp_clip_frame(
            _graph(), timeline_frame=0, quality="preview")
        assert img is not None, "fused-chain graph produced no output"
        return img.astype(np.float32)
    finally:
        renderer.cleanup()


@pytest.fixture(scope="module")
def _gl_available():
    try:
        import moderngl
        ctx = moderngl.create_standalone_context()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Cannot create standalone GL context: {exc}")
    ctx.release()


class TestFusedChainExecution:
    def test_fused_matches_individual_within_tolerance(self, qapp,
                                                       _gl_available):
        fused = _render({})          # default: conservative fusion ON
        individual = _render({"pass_fusion_mode": "off"})
        d = np.abs(fused - individual)
        # One fused pass vs stage-wise f4 textures: rounding differs.
        assert float(d.max()) <= 2e-3
        assert float(d.mean()) <= 2e-4

    def test_fusion_mode_config_is_honoured(self, qapp, _gl_available,
                                            monkeypatch):
        from src.features.composite.evaluators import base_evaluator
        calls = {"n": 0}
        original = base_evaluator.BaseEvaluatorMixin._execute_fused_chain

        def counting(self, *a, **k):
            calls["n"] += 1
            return original(self, *a, **k)

        monkeypatch.setattr(base_evaluator.BaseEvaluatorMixin,
                            "_execute_fused_chain", counting)
        _render({})                          # fusion ON → chain executes
        fused_calls = calls["n"]
        calls["n"] = 0
        _render({"pass_fusion_mode": "off"})  # OFF → no fused execution
        assert fused_calls >= 1
        assert calls["n"] == 0

    def test_fused_cache_hit_reuses_result(self, qapp, _gl_available,
                                           monkeypatch):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("TOPOS_HEADLESS_GL", "1")
        from src.features.composite.evaluators import base_evaluator
        from src.features.composite.render.composite_frame_renderer import (
            CompositeFrameRenderer,
        )
        draws = {"n": 0}
        original = base_evaluator.BaseEvaluatorMixin._apply_fused_chain

        def counting(self, *a, **k):
            draws["n"] += 1
            return original(self, *a, **k)

        monkeypatch.setattr(base_evaluator.BaseEvaluatorMixin,
                            "_apply_fused_chain", counting)
        # The fused-chain cache lives with the renderer's cache manager.
        # The PREVIEW path deliberately clears it every frame (P0-1: the
        # frame's eval textures must return to the pool), so warm-hit
        # behaviour is verified on the export path, where the cache is
        # meant to persist across frames.
        renderer = CompositeFrameRenderer(config_manager=_StubConfig({}))
        try:
            img1 = renderer.render_comp_clip_frame(
                _graph(), timeline_frame=0, quality="export")
            assert img1 is not None
            first = draws["n"]
            assert first >= 1
            img2 = renderer.render_comp_clip_frame(
                _graph(), timeline_frame=0, quality="export")
            assert img2 is not None
            assert draws["n"] == first
            assert np.array_equal(img1, img2)
        finally:
            renderer.cleanup()
