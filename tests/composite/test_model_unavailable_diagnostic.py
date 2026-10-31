"""T-P1-60: evaluator-level MODEL_UNAVAILABLE structured diagnostic.

ai_generate 等 AI 占位节点缺模型包时，求值器不再只留"无解释 None"：
节点级诊断 ``{"code": "MODEL_UNAVAILABLE", ...}`` 经 ExportResult.diagnostics
与 CompositeFrameRenderer.last_diagnostics 结构化上抛，出图路径（占位直通）
与失败路径（无输入）都要可查。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

pytestmark = pytest.mark.composite_core


def _graph(nodes: list, connections: list) -> dict:
    return {
        "composite_id": "diag-graph",
        "fps": 25.0,
        "width": 320,
        "height": 240,
        "nodes": nodes,
        "connections": connections,
    }


def _ai_generate_alone_graph() -> dict:
    return _graph(
        nodes=[
            {"id": "ai", "type": "ai_generate", "position": [0, 0],
             "parameters": {}},
            {"id": "out", "type": "output", "position": [200, 0],
             "parameters": {"format_mode": "custom",
                            "width": 320, "height": 240}},
        ],
        connections=[
            {"src_node_id": "ai", "src_port_name": "image",
             "dst_node_id": "out", "dst_port_name": "image"},
        ],
    )


def _constant_source_graph() -> dict:
    return _graph(
        nodes=[
            {"id": "src", "type": "constant", "position": [-200, 0],
             "parameters": {"color_r": 0.8, "color_g": 0.3, "color_b": 0.1}},
            {"id": "ai", "type": "ai_generate", "position": [0, 0],
             "parameters": {}},
            {"id": "out", "type": "output", "position": [200, 0],
             "parameters": {"format_mode": "custom",
                            "width": 320, "height": 240}},
        ],
        connections=[
            # constant 的输出端口名是 result；ai_generate 是 image。
            {"src_node_id": "src", "src_port_name": "result",
             "dst_node_id": "ai", "dst_port_name": "image"},
            {"src_node_id": "ai", "src_port_name": "image",
             "dst_node_id": "out", "dst_port_name": "image"},
        ],
    )


def _render(graph_data: dict):
    from src.features.composite.render.composite_frame_renderer import (
        CompositeFrameRenderer,
    )

    renderer = CompositeFrameRenderer()
    try:
        img = renderer.render_comp_clip_frame(
            graph_data=graph_data, timeline_frame=0, source_frame=0,
            quality="preview")
        return img, list(renderer.last_diagnostics or [])
    finally:
        try:
            renderer.cleanup()
        except Exception:
            pass


def _assert_model_unavailable(diagnostics: list) -> dict:
    hits = [d for d in diagnostics
            if str(d.get("code", "")).upper() == "MODEL_UNAVAILABLE"]
    assert hits, f"expected MODEL_UNAVAILABLE diagnostics, got {diagnostics!r}"
    entry = hits[0]
    assert entry.get("node_type") == "ai_generate"
    return entry


class TestModelUnavailableDiagnostic:
    @pytest.fixture(autouse=True)
    def _offscreen(self, monkeypatch):
        monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
        monkeypatch.setenv("TOPOS_HEADLESS_GL", "1")

    def test_no_input_still_structured_not_silent_none(self):
        """无输入的 ai_generate：图无输出，但诊断必须说明缺模型。"""
        img, diagnostics = _render(_ai_generate_alone_graph())
        assert img is None
        entry = _assert_model_unavailable(diagnostics)
        assert entry.get("node_id") == "ai"

    def test_passthrough_image_carries_diagnostic(self):
        """有输入的 ai_generate：占位直通出图，同时带上缺模型诊断。"""
        import numpy as np
        img, diagnostics = _render(_constant_source_graph())
        assert img is not None, "placeholder pass-through should keep output live"
        arr = np.asarray(img)
        assert float(arr.std()) > 0.0
        _assert_model_unavailable(diagnostics)

    def test_frame_trace_attribution_exposed(self):
        """T-P1-60 步骤 6：performance attribution（frame trace）可查。"""
        from src.features.composite.render.composite_frame_renderer import (
            CompositeFrameRenderer,
        )

        renderer = CompositeFrameRenderer()
        try:
            img = renderer.render_comp_clip_frame(
                graph_data=_constant_source_graph(), timeline_frame=0,
                source_frame=0, quality="preview")
            assert img is not None
            trace = renderer.last_frame_trace
            assert isinstance(trace, dict) and trace
            assert trace.get("eval_ms") is not None and trace["eval_ms"] >= 0.0
            assert trace.get("total_ms") is not None and trace["total_ms"] >= 0.0
            assert trace.get("backend") == "opengl"
            # GPU 时间戳缺测量必须显式标记，而不是 0。
            assert trace.get("gpu_time_unavailable") is True
        finally:
            try:
                renderer.cleanup()
            except Exception:
                pass
