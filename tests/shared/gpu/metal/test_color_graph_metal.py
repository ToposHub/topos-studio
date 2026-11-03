"""B12.3/B12.4 调色节点图 Metal 执行器测试（N10，E23 kernels）。

分两层：

- 结构层（无 Metal 也运行）：参数布局与 MSL struct 对齐、能力目录
  一致性（与 E30 矩阵同源）、fail-closed 判据。
- 真机层（无 Metal 设备整类跳过 = NOT RUN，不记 PASS）：
  ``graph_grading`` 基础域 vs CPU 生产 oracle（G2 双容差：
  SDR ≤1/255、HDR ≤2e-3 rel）；分支/层/key/splitter 图三后端同
  fixture 对照；逐 pass 取消；HDR 不截断；lease 复用。

输入量化注意（与 test_metal_kernels_color 同规约）：kernel 从
RGBA16F buffer 读入，CPU 参考必须用**先过 fp16 量化的输入**。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_cpu import (
    CPU_CAPABILITY_CATALOG,
    UnsupportedNode,
    corrector_production_oracle,
    run_plan_cpu,
)
from src.features.color.core.color_graph_metal import (
    METAL_CAPABILITY_CATALOG,
    ColorGraphMetalExecutor,
)
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.layer import build_curves_lut_from_data
from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.gpu.metal.kernels_color import (
    GRAPH_GRADING_PARAMS_DTYPE,
    GRAPH_SIZE_PARAMS_DTYPE,
    METAL_GRAPH_OPS_MSL,
    make_graph_grading_params,
    make_graph_size_params,
)
from src.shared.project.color_graph import unfreeze_params
from tests.color.fixtures.color_graph import (
    f01_hdr_ramp,
    f02_patches,
    _neutral_layer,
)
from tests.color.test_color_graph_migration import _stack_with_layers

requires_metal = pytest.mark.skipif(
    not MetalComputeContext.is_supported(), reason="无 Metal 设备（NOT RUN）")

SDR_TOL = 1.0 / 255.0
HDR_REL = 2e-3

GRAPH_KERNELS = (
    "graph_copy", "graph_parallel_mix", "graph_layer_mix", "graph_key_mix",
    "graph_key_complement", "graph_key_passthrough", "graph_splitter",
    "graph_combiner", "graph_masked_mix", "graph_grading",
)


def _quantized(source: np.ndarray) -> np.ndarray:
    return source.astype(np.float16).astype(np.float32)


def _assert_dual(metal: np.ndarray, ref: np.ndarray, label: str) -> None:
    metal32 = metal.astype(np.float32)
    diff = np.abs(metal32.astype(np.float64) - ref.astype(np.float64))
    mag = np.maximum(np.abs(ref.astype(np.float64)), 1e-6)
    sdr = np.abs(ref) <= 1.0
    assert diff[sdr].max(initial=0.0) <= SDR_TOL, (
        f"{label}: SDR abs {diff[sdr].max(initial=0.0):.3e}")
    rel = diff[~sdr] / mag[~sdr]
    assert rel.max(initial=0.0) <= HDR_REL, (
        f"{label}: HDR rel {rel.max(initial=0.0):.3e}")


def _rich_layer():
    layer = _neutral_layer("Rich")
    layer.grading_params.update({
        "exposure": 25.0, "temp": 12.0, "tint": -6.0, "contrast": 30.0,
        "saturation": -18.0, "vibrance": 25.0, "shadow": 12.0,
        "highlights": -20.0, "hue_shift": 40.0,
        "lift_saturation": 10.0, "gamma_saturation": -10.0,
        "gain_saturation": 5.0,
        "lift": (20.0, 0.3, 0.05), "gamma": (200.0, 0.2, -0.04),
        "gain": (120.0, 0.15, 0.08),
    })
    return layer


def _curves_layer():
    layer = _neutral_layer("Curve")
    layer.curves_data = {"M": [(0, 255), (64, 209), (191, 46), (255, 0)]}
    layer.curves_lut = build_curves_lut_from_data(layer.curves_data)
    layer.grading_params["exposure"] = 8.0
    return layer


def _payload_provider(graph):
    payloads = {n.node_id: unfreeze_params(dict(n.legacy_payload))
                for n in graph.nodes}

    def provide(node_id):
        payload = dict(payloads.get(node_id) or {})
        node = next((n for n in graph.nodes if n.node_id == node_id), None)
        if node is not None:
            payload["visible"] = node.enabled
        if payload.get("curves_data"):
            payload["curves_lut"] = build_curves_lut_from_data(
                dict(payload["curves_data"]))
        return payload
    return provide


# ---------------------------------------------------------------------------
# 结构层：布局与能力（无需 Metal）
# ---------------------------------------------------------------------------

class TestStructural:
    def test_param_dtypes_match_msl_structs(self) -> None:
        assert GRAPH_SIZE_PARAMS_DTYPE.itemsize == 16
        assert GRAPH_GRADING_PARAMS_DTYPE.itemsize == 96  # 22 f4 + 2 u4
        for name in GRAPH_GRADING_PARAMS_DTYPE.names:
            offset = GRAPH_GRADING_PARAMS_DTYPE.fields[name][1]
            assert offset % 4 == 0, name

    def test_all_kernels_present_in_msl_source(self) -> None:
        for kernel in GRAPH_KERNELS:
            assert f"kernel void {kernel}(" in METAL_GRAPH_OPS_MSL, kernel

    def test_grading_param_builder_rejects_non_finite(self) -> None:
        with pytest.raises(ValueError):
            make_graph_grading_params(
                exposure=float("nan"), temp=0, tint=0, contrast=0,
                saturation=0, vibrance=0, shadow=0, highlights=0,
                hue_shift=0, lift_saturation=0, gamma_saturation=0,
                gain_saturation=0, lift=(1, 1, 1), gamma=(1, 1, 1),
                gain=(1, 1, 1), opacity=1.0, mask_mode=0, has_curves=False)

    def test_size_param_builder_layout(self) -> None:
        params = make_graph_size_params(64, 32, a=0.5, b=2)
        assert params["width"] == 64 and params["height"] == 32
        assert params["a"] == 0.5 and params["b"] == 2

    def test_metal_catalog_consistent_with_matrix(self) -> None:
        from src.features.shared.effects.effect_catalog import (
            graph_backend_capability_matrix,
        )
        matrix = graph_backend_capability_matrix()
        for op, status in METAL_CAPABILITY_CATALOG.items():
            assert matrix[op]["metal"] == status, op
        for op, status in CPU_CAPABILITY_CATALOG.items():
            assert matrix[op]["cpu"] == status, op

    def test_structural_ops_exact_and_corrector_fallback(self) -> None:
        for op in ("source", "output", "parallel_mixer", "layer_mixer",
                   "splitter", "combiner"):
            assert METAL_CAPABILITY_CATALOG[op] == "exact", op
        assert METAL_CAPABILITY_CATALOG["corrector"] == "fallback"
        # N02：key 通道算子 Metal 未接线——显式 unsupported（N14 恢复）
        for op in ("key_mixer", "outside", "alpha_output"):
            assert METAL_CAPABILITY_CATALOG[op] == "unsupported", op
        assert METAL_CAPABILITY_CATALOG["compound"] == "unsupported"


# ---------------------------------------------------------------------------
# 真机层：AMD 6800 / Apple Silicon 实测（无设备整类跳过 = NOT RUN）
# ---------------------------------------------------------------------------

@requires_metal
class TestMetalGradingKernel:
    def _run_corrector(self, ctx, source_f16, layers):
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        provider = _payload_provider(graph)
        plan = compile_graph(freeze_graph(graph, session_id="m"),
                             size=source_f16.shape[:2][::-1])
        executor = ColorGraphMetalExecutor(ctx)
        return executor.execute_plan(
            plan, source_f16, request_id="t", source_size=None,
            layer_param_provider=provider)

    def test_neutral_identity(self, metal_ctx):
        src = f01_hdr_ramp(32)
        result = self._run_corrector(metal_ctx, src.astype(np.float16),
                                     [_neutral_layer("N")])
        assert result.published
        ref = _quantized(src)
        _assert_dual(result.output, ref, "neutral")

    def test_full_basic_domain_vs_cpu_oracle(self, metal_ctx):
        layer = _rich_layer()
        src = f01_hdr_ramp(32)
        q = _quantized(src)
        payload = {"grading_params": dict(layer.grading_params)}
        cpu = corrector_production_oracle(q, payload)
        result = self._run_corrector(metal_ctx, src.astype(np.float16),
                                     [layer])
        assert result.published
        _assert_dual(result.output, cpu, "rich")

    def test_curves_vs_cpu_oracle(self, metal_ctx):
        layer = _curves_layer()
        src = f02_patches(32)
        q = _quantized(src)
        payload = {"grading_params": dict(layer.grading_params),
                   "curves_lut": layer.curves_lut}
        cpu = corrector_production_oracle(q, payload)
        result = self._run_corrector(metal_ctx, src.astype(np.float16),
                                     [layer])
        assert result.published
        _assert_dual(result.output, cpu, "curves")

    def test_hdr_not_clamped(self, metal_ctx):
        layer = _neutral_layer("E")
        layer.grading_params["exposure"] = 100.0   # +6 档
        src = np.full((4, 4, 4), (0.5, 0.5, 0.5, 1.0), dtype=np.float32)
        result = self._run_corrector(metal_ctx, src.astype(np.float16),
                                     [layer])
        assert result.published
        assert float(result.output[..., 0].max()) > 30.0, "HDR 不截断"

    def test_grading_math_shared_with_gl_semantics(self, metal_ctx):
        """Metal/CPU/GL 三后端语义同源：Metal 与 CPU 在门内，且 CPU 已
        与 GL parity（test_color_graph_cpu_parity）——传递性即三后端一致。"""
        layer = _rich_layer()
        src = f02_patches(32)
        q = _quantized(src)
        payload = {"grading_params": dict(layer.grading_params)}
        cpu = corrector_production_oracle(q, payload)
        result = self._run_corrector(metal_ctx, src.astype(np.float16),
                                     [layer])
        _assert_dual(result.output, cpu, "tri-backend")


@pytest.fixture(scope="module")
def metal_ctx():
    ctx = MetalComputeContext()
    yield ctx


@requires_metal
class TestMetalGraphExecution:
    def test_serial_graph_vs_cpu_plan(self, metal_ctx):
        layers = [_rich_layer(), _curves_layer()]
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        provider = _payload_provider(graph)
        plan = compile_graph(freeze_graph(graph, session_id="m"),
                             size=(32, 32))
        src = f01_hdr_ramp(32)
        q = _quantized(src)
        cpu = run_plan_cpu(plan, q, lambda p: provider(p.node_id))
        executor = ColorGraphMetalExecutor(metal_ctx)
        result = executor.execute_plan(
            plan, q.astype(np.float16), request_id="serial",
            layer_param_provider=provider)
        assert result.published
        assert result.executed_passes == len(plan.passes)
        _assert_dual(result.output, cpu.output, "serial-metal")
        assert result.released_buffers >= 1

    def test_parallel_branch_graph_vs_cpu(self, metal_ctx):
        graph = migrate_legacy_stack(_stack_with_layers([_rich_layer()]))
        counter = iter(range(200))
        service = ColorGraphService(id_factory=lambda: f"pb{next(counter)}")
        grown = service.apply_intent(graph, "add_parallel_branch", {})
        assert grown.ok
        provider = _payload_provider(grown.graph)
        plan = compile_graph(freeze_graph(grown.graph, session_id="m"),
                             size=(32, 32))
        src = f02_patches(32)
        q = _quantized(src)
        cpu = run_plan_cpu(plan, q, lambda p: provider(p.node_id))
        executor = ColorGraphMetalExecutor(metal_ctx)
        result = executor.execute_plan(
            plan, q.astype(np.float16), request_id="par",
            layer_param_provider=provider)
        assert result.published
        _assert_dual(result.output, cpu.output, "parallel-metal")

    def test_splitter_pair_graph_vs_cpu(self, metal_ctx):
        graph = migrate_legacy_stack(_stack_with_layers([_rich_layer()]))
        counter = iter(range(200))
        service = ColorGraphService(id_factory=lambda: f"sp{next(counter)}")
        grown = service.apply_intent(graph, "add_splitter_pair", {})
        assert grown.ok
        provider = _payload_provider(grown.graph)
        plan = compile_graph(freeze_graph(grown.graph, session_id="m"),
                             size=(32, 32))
        src = f02_patches(32)
        q = _quantized(src)
        cpu = run_plan_cpu(plan, q, lambda p: provider(p.node_id))
        executor = ColorGraphMetalExecutor(metal_ctx)
        result = executor.execute_plan(
            plan, q.astype(np.float16), request_id="split",
            layer_param_provider=provider)
        assert result.published
        _assert_dual(result.output, cpu.output, "splitter-metal")

    def test_visible_false_bypass_passthrough(self, metal_ctx):
        graph = migrate_legacy_stack(_stack_with_layers([_rich_layer()]))
        from dataclasses import replace as _replace
        cs = [n for n in graph.nodes if n.node_type == "corrector"]
        disabled = _replace(graph, nodes=tuple(
            _replace(n, enabled=False) if n.node_id == cs[0].node_id else n
            for n in graph.nodes))
        provider = _payload_provider(disabled)
        plan = compile_graph(freeze_graph(disabled, session_id="m"),
                             size=(32, 32))
        src = f01_hdr_ramp(32)
        q = _quantized(src)
        executor = ColorGraphMetalExecutor(metal_ctx)
        result = executor.execute_plan(
            plan, q.astype(np.float16), request_id="bypass",
            layer_param_provider=provider)
        assert result.published
        _assert_dual(result.output, q, "bypass")

    def test_pool_reuse_across_executions(self, metal_ctx):
        graph = migrate_legacy_stack(_stack_with_layers([_rich_layer()]))
        provider = _payload_provider(graph)
        plan = compile_graph(freeze_graph(graph, session_id="m"),
                             size=(32, 32))
        q = f01_hdr_ramp(32).astype(np.float16)
        executor = ColorGraphMetalExecutor(metal_ctx)
        first = executor.execute_plan(plan, q, request_id="p1",
                                      layer_param_provider=provider)
        assert first.published
        executor.release_revision("p1")
        allocated_after_first = executor.pool_allocated()
        second = executor.execute_plan(plan, q, request_id="p2",
                                       layer_param_provider=provider)
        assert second.published
        executor.release_revision("p2")
        assert executor.pool_allocated() == allocated_after_first, (
            "第二次执行应完全复用池化 scratch buffer（无持续增长）")


@requires_metal
class TestMetalFailClosedAndCancel:
    def test_plugin_params_refused(self, metal_ctx):
        graph = migrate_legacy_stack(_stack_with_layers([_rich_layer()]))
        provider = _payload_provider(graph)
        payloads = dict(provider(graph.nodes[1].node_id)
                        if len(graph.nodes) > 1 else {})
        plan = compile_graph(freeze_graph(graph, session_id="m"),
                             size=(32, 32))

        def with_plugin(node_id):
            payload = dict(provider(node_id))
            payload["plugins"] = [{"name": "nope"}]
            return payload

        executor = ColorGraphMetalExecutor(metal_ctx)
        result = executor.execute_plan(
            plan, f01_hdr_ramp(32).astype(np.float16), request_id="plug",
            layer_param_provider=with_plugin)
        assert not result.published
        assert result.diagnostic_code == "param_domain_unsupported"

    def test_unknown_op_refused_by_catalog(self, metal_ctx):
        graph = migrate_legacy_stack(_stack_with_layers([_rich_layer()]))
        counter = iter(range(50))
        service = ColorGraphService(id_factory=lambda: f"x{next(counter)}")
        grown = service.apply_intent(graph, "add_parallel_branch", {})
        assert grown.ok
        plan = compile_graph(freeze_graph(grown.graph, session_id="m"),
                             size=(32, 32))
        executor = ColorGraphMetalExecutor(
            metal_ctx) if False else ColorGraphMetalExecutor(metal_ctx)
        # 目录用人为 pending 模拟未实现算子（fail-closed 预检）
        from unittest.mock import patch
        catalog = dict(METAL_CAPABILITY_CATALOG)
        catalog["parallel_mixer"] = "pending"
        with patch(
            "src.features.color.core.color_graph_metal"
            ".METAL_CAPABILITY_CATALOG", catalog):
            result = executor.execute_plan(
                plan, f01_hdr_ramp(32).astype(np.float16),
                request_id="cap")
        assert not result.published
        assert result.diagnostic_code == "capability_unsupported"
        assert "fail-closed" in result.diagnostic_message

    def test_cancel_mid_plan_no_publish(self, metal_ctx):
        graph = migrate_legacy_stack(
            _stack_with_layers([_rich_layer(), _curves_layer()]))
        provider = _payload_provider(graph)
        plan = compile_graph(freeze_graph(graph, session_id="m"),
                             size=(32, 32))
        executor = ColorGraphMetalExecutor(metal_ctx)
        calls = {"n": 0}

        def cancel_after_first(request_id):
            calls["n"] += 1
            return calls["n"] > 1

        result = executor.execute_plan(
            plan, f01_hdr_ramp(32).astype(np.float16), request_id="cx",
            layer_param_provider=provider, cancel_check=cancel_after_first)
        assert not result.published
        assert result.diagnostic_code == "cancelled"
        assert result.executed_passes == 0
        assert result.released_buffers >= 1

    def test_bad_source_format_rejected(self, metal_ctx):
        executor = ColorGraphMetalExecutor(metal_ctx)
        bad = np.zeros((4, 4, 3), dtype=np.float32)
        result = executor.execute_plan(None, bad, request_id="bad")
        assert not result.published
        assert result.diagnostic_code in ("bad_source_format",
                                          "execution_error")


@requires_metal
class TestMetalLayerMixV11Modes:
    """spec §9 v1.1：graph_layer_mix 全模式 Metal kernel vs CPU oracle。"""

    MODES = ("Overlay", "Soft Light", "Hard Light", "Color Dodge",
             "Color Burn", "Darken", "Lighten", "Difference", "Exclusion",
             "Hue", "Saturation", "Color", "Luminosity", "Addition",
             "Subtract (Clamped)", "Divide")

    def test_all_modes_match_cpu(self, metal_ctx):
        import numpy as np
        from src.features.color.core.color_graph_cpu import layer_mix
        from src.features.color.shaders.node_ops_gl import LAYER_MODE_INDEX
        rng = np.random.default_rng(5)
        size = 16
        # 1/8 栅格（fp16 精确）——极点模式对输入量化固有放大
        bottom = (np.round(rng.uniform(0, 1, (size, size, 4)) * 8) / 8
                  ).astype(np.float16)
        top = (np.round(rng.uniform(0, 1, (size, size, 4)) * 8) / 8
               ).astype(np.float16)
        qb = bottom.astype(np.float32)
        qt = top.astype(np.float32)
        ctx = metal_ctx
        in_b = ctx.create_buffer_from_numpy(np.ascontiguousarray(bottom))
        in_t = ctx.create_buffer_from_numpy(np.ascontiguousarray(top))
        out = ctx.create_buffer(size * size * 8)
        from src.shared.gpu.metal.kernels_color import (
            METAL_GRAPH_OPS_MSL,
            make_graph_size_params,
        )
        kernel = ctx.get_cached_kernel("graph_layer_mix")
        if kernel is None:
            kernel = ctx.compile_kernel("graph_layer_mix",
                                        METAL_GRAPH_OPS_MSL)
        for mode in (*self.MODES, "Normal", "Add", "Subtract",
                     "Multiply", "Screen"):
            params = make_graph_size_params(size, size, a=0.5,
                                            b=LAYER_MODE_INDEX[mode])
            ctx.dispatch(kernel,
                         buffers={0: in_b, 1: in_t, 2: out,
                                  3: ctx.create_buffer_from_numpy(params)},
                         threads=(size, size, 1))
            ctx.finish()
            metal = ctx.read_buffer(out).view(
                np.float16).reshape(size, size, 4).astype(np.float32)
            cpu = layer_mix([qb, qt], [0.5], [mode])
            diff = np.abs(metal.astype(np.float64) - cpu.astype(np.float64))
            sdr = np.abs(cpu) <= 1.0
            mag = np.maximum(np.abs(cpu.astype(np.float64)), 1e-6)
            assert diff[sdr].max(initial=0.0) <= SDR_TOL, (
                f"{mode}: SDR {diff[sdr].max(initial=0.0):.3e}")
            rel = diff[~sdr] / mag[~sdr]
            assert rel.max(initial=0.0) <= HDR_REL, (
                f"{mode}: HDR rel {rel.max(initial=0.0):.3e}")
