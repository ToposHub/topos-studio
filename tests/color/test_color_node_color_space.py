"""N05：Node Color Space/Gamma 后端消费（变换位置 + GL↔CPU 同源 + 恒等）。

验收（计划 N05）：多空间色卡（合成已知向量）、Gamma roundtrip、HDR、
LUT 顺序（变换包裹 grade 链——LUT 在内侧）；CPU/GL 按能力验证（Metal/
导出显式拒绝）；关选项 = 原结果；菜单勾选与保存一致（params.color 持
久化 + node_options 回显，见 controller 既有路径）。
"""
from __future__ import annotations

from dataclasses import replace as _replace

import numpy as np
import pytest

pytest.importorskip("moderngl")

from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_cpu import run_plan_cpu
from src.features.color.core.color_graph_executor import ColorGraphExecutor
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.color_graph_param_source import (
    graph_node_layer_provider,
)
from src.features.color.core.color_node_color_space import (
    COLOR_SPACES, GAMMAS, apply_node_color_space_cpu,
    node_color_transform_spec, node_color_is_identity,
)
from src.features.color.core.graph_lut_export import graph_pointwise_export_errors
from src.shared.project.color_graph import (
    ColorEdgeData, ColorGraphData, ColorNodeData, NodeType,
)


def _chart() -> np.ndarray:
    """合成色卡： primaries + 灰阶 + HDR 超白（含 >1 线性值）。"""
    primaries = np.array([
        [1, 0, 0], [0, 1, 0], [0, 0, 1],
        [1, 1, 1], [0.5, 0.5, 0.5], [0.18, 0.18, 0.18],
        [1, 1, 0], [0, 1, 1], [1, 0, 1],
        [2.0, 2.0, 2.0], [4.0, 0.5, 0.25], [0.1, 3.0, 0.02],
    ], dtype=np.float64)
    return np.clip(primaries, 0.0, None)


# ── numpy 参考：roundtrip + 已知向量 ───────────────────────────────────


class TestReferenceMath:
    @pytest.mark.parametrize("gamma", GAMMAS)
    def test_transfer_roundtrip(self, gamma: str):
        x = _chart() / np.maximum(_chart().max(), 1.0)  # 编码域 ∈ [0,1]
        spec = node_color_transform_spec("Rec.709", gamma)
        if spec is None:
            assert node_color_is_identity("Rec.709", gamma)
            return
        back = apply_node_color_space_cpu(
            apply_node_color_space_cpu(x, spec), spec, inverse=True)
        np.testing.assert_allclose(back, x, atol=5e-3,
                                   err_msg=f"roundtrip 失败: {gamma}")

    @pytest.mark.parametrize("cs", COLOR_SPACES)
    def test_gamut_matrix_roundtrip(self, cs: str):
        if cs == "Use Timeline":
            return
        spec = node_color_transform_spec(cs, "Linear")
        x = _chart()
        back = apply_node_color_space_cpu(
            apply_node_color_space_cpu(x, spec), spec, inverse=True)
        np.testing.assert_allclose(back, x, atol=1e-6,
                                   err_msg=f"gamut roundtrip: {cs}")

    def test_known_vectors(self):
        spec = node_color_transform_spec("Rec.709", "sRGB")
        enc = apply_node_color_space_cpu(
            np.array([[0.5, 0.5, 0.5]]), spec, inverse=True)
        np.testing.assert_allclose(enc, 0.735357, atol=1e-4)  # sRGB(0.5)
        spec2084 = node_color_transform_spec("Rec.709", "ST2084")
        pq = apply_node_color_space_cpu(
            np.array([[1.0, 1.0, 1.0]]), spec2084, inverse=True)
        np.testing.assert_allclose(pq, 1.0, atol=1e-3)  # 10000nit → 1.0
        spec_hlg = node_color_transform_spec("Rec.709", "HLG")
        # BT.2100：场景线性 1/12 ↔ 信号 0.5（连续点）；0.5 线性 → ~0.872
        hlg_kink = apply_node_color_space_cpu(
            np.array([[1.0 / 12.0] * 3]), spec_hlg, inverse=True)
        np.testing.assert_allclose(hlg_kink, 0.5, atol=1e-4)
        hlg_half = apply_node_color_space_cpu(
            np.array([[0.5, 0.5, 0.5]]), spec_hlg, inverse=True)
        np.testing.assert_allclose(hlg_half, 0.8716, atol=1e-3)

    def test_identity_option_is_noop(self):
        assert node_color_transform_spec("Use Timeline", "Use Timeline") is None
        assert node_color_transform_spec(None, None) is None


# ── 图级：GL ↔ CPU 同源 + 关选项位级 ───────────────────────────────────


def _graph(color_domain: dict | None, exposure: float = 0.25):
    params = {"color": dict(color_domain)} if color_domain else {}
    nodes = [
        ColorNodeData(node_id="src", node_type=NodeType.SOURCE.value),
        ColorNodeData(node_id="a", node_type=NodeType.CORRECTOR.value,
                      params=params,
                      legacy_payload={"id": "a", "grading_params": {
                          "exposure": exposure}}),
        ColorNodeData(node_id="out", node_type=NodeType.OUTPUT.value),
    ]
    edges = [ColorEdgeData("e1", "src", "image", "a", "image"),
             ColorEdgeData("e2", "a", "image", "out", "image")]
    return ColorGraphData(nodes=tuple(nodes), edges=tuple(edges),
                          source_node_id="src", output_node_id="out")


def _source(w=24, h=16):
    ramp = np.linspace(0.0, 1.0, w, dtype=np.float32)
    rgb = np.stack([ramp, ramp * 0.7 + 0.15, ramp * 0.4 + 0.3], axis=-1)
    img = np.repeat(rgb[None, :, :], h, axis=0)
    return np.dstack([img, np.ones((h, w, 1), np.float32)])


def _params_of(graph):
    payloads = {n.node_id: dict(n.legacy_payload or {})
                for n in graph.nodes}

    def provide(key):
        node_id = key if isinstance(key, str) else key.node_id
        return dict(payloads.get(node_id) or {})
    return provide


class TestGraphExecution:
    def test_gl_cpu_parity_with_color_domain(self):
        import moderngl
        domain = {"color_space": "Rec.2020", "gamma": "sRGB"}
        graph = _graph(domain)
        cpu = run_plan_cpu(
            compile_graph(freeze_graph(graph, session_id="s"), size=(24, 16)),
            _source(), _params_of(graph)).output
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            plan = compile_graph(freeze_graph(graph, session_id="s"),
                                 size=(24, 16))
            src = ctx.texture((24, 16), 4, dtype="f2",
                              data=_source().astype(np.float16).tobytes())
            result = executor.execute_plan(
                plan, src, request_id="cs1", source_size=(24, 16),
                layer_param_provider=_params_of(graph))
            assert result.published, result.diagnostic_message
            fbo = ctx.framebuffer(color_attachments=[result.output_texture])
            fbo.use()
            gpu = np.frombuffer(fbo.read(components=4, dtype="f2"),
                                dtype=np.float16).reshape(
                                    16, 24, 4).astype(np.float32)
            executor.release_revision("cs1")
            src.release()
            executor.release_pool()
        finally:
            ctx.release()
        # f16 纹理 + 变换非线性 → 宽容差；关键在两侧同公式同顺序
        np.testing.assert_allclose(gpu[..., :3], cpu[..., :3], atol=8e-3)

    def test_use_timeline_bitwise_identity(self):
        """关选项 = 原结果：color 域缺席与 Use Timeline 位级一致。"""
        import moderngl
        ctx = moderngl.create_standalone_context(require=330)
        try:
            outs = []
            for domain in (None, {"color_space": "Use Timeline",
                                  "gamma": "Use Timeline"}):
                graph = _graph(domain)
                executor = ColorGraphExecutor(ctx)
                plan = compile_graph(freeze_graph(graph, session_id="s"),
                                     size=(24, 16))
                src = ctx.texture((24, 16), 4, dtype="f2",
                                  data=_source().astype(np.float16).tobytes())
                result = executor.execute_plan(
                    plan, src, request_id=f"id-{domain is None}",
                    source_size=(24, 16),
                    layer_param_provider=_params_of(graph))
                assert result.published
                fbo = ctx.framebuffer(
                    color_attachments=[result.output_texture])
                fbo.use()
                outs.append(np.frombuffer(
                    fbo.read(components=4, dtype="f2"),
                    dtype=np.float16).reshape(16, 24, 4).copy())
                executor.release_revision(f"id-{domain is None}")
                src.release()
                executor.release_pool()
            np.testing.assert_array_equal(outs[0], outs[1])
        finally:
            ctx.release()

    def test_transform_changes_output_vs_identity(self):
        """强 grade 在「变换内侧」执行——与无变换执行显著不同。

        （近似恒等 grade 下包裹≈往返恒等是**正确性属性**；本用例用
        强 exposure 拉开两种执行顺序的差。）
        """
        domain = {"color_space": "Rec.2020", "gamma": "ST2084"}
        graph = _graph(domain, exposure=60.0)   # 百分比尺度 → ~2.16 档
        out = run_plan_cpu(
            compile_graph(freeze_graph(graph, session_id="s"), size=(24, 16)),
            _source(), _params_of(graph)).output
        base = run_plan_cpu(
            compile_graph(freeze_graph(_graph(None, exposure=60.0),
                                       session_id="s"), size=(24, 16)),
            _source(), _params_of(_graph(None))).output
        delta = np.abs(out[..., :3] - base[..., :3])
        assert delta.max() > 1e-3, delta.max()

    def test_export_rejects_color_domain(self):
        graph = _graph({"color_space": "Rec.2020", "gamma": "sRGB"})
        errors = graph_pointwise_export_errors(graph)
        assert any("color 域" in e for e in errors), errors

    def test_spec_versioned(self):
        from src.features.color.core.color_node_color_space import (
            NODE_COLOR_SCHEMA_VERSION,
        )
        spec = node_color_transform_spec("P3-D65", "Linear")
        assert spec["schema_version"] == NODE_COLOR_SCHEMA_VERSION == 1
