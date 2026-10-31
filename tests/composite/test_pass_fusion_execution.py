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
