"""B12.2 CPU 生产域 oracle 与 GL 执行器的 parity 测试。

同一 fixture（F01/F02）、同一生产参数（grading 基础点态域 + curves）：
CPU oracle（N09 ``corrector_production_oracle``）vs GL 执行器（N07，
生产 composition shader）在双容差门（计划 §7.1 / G2）内一致：

- SDR（0..1）：abs ≤ 1/255；
- HDR（>1 或 <0）：rel ≤ 2e-3（near-zero 有 abs 下限）。

fail-closed 断言：插件/邻域/非 Normal 混合在 CPU 域显式
``UnsupportedNode``，不静默近似。
"""

from __future__ import annotations

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")

from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_cpu import (
    UnsupportedNode,
    corrector_production_oracle,
    production_layer_domain_errors,
    run_plan_cpu,
)
from src.features.color.core.color_graph_executor import ColorGraphExecutor
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.layer import build_curves_lut_from_data
from src.shared.project.color_graph import unfreeze_params
from tests.color.fixtures.color_graph import f01_hdr_ramp, f02_patches
from tests.color.test_color_graph_migration import _stack_with_layers
from tests.color.fixtures.color_graph import _neutral_layer

SDR_TOL = 1.0 / 255.0
HDR_REL = 2e-3


def _rich_layer(name: str = "Rich") -> object:
    """全基础参数域非零的生产 Corrector（f03 同构造路径，无插件）。

    色轮参数为 (hue°, sat, bright) 刻度（``params_to_rgb`` 输入域）。
    """
    layer = _neutral_layer(name)
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


def _curves_layer(name: str = "Curve") -> object:
    layer = _neutral_layer(name)
    layer.curves_data = {"M": [(0, 255), (64, 209), (191, 46), (255, 0)]}
    layer.curves_lut = build_curves_lut_from_data(layer.curves_data)
    layer.grading_params["exposure"] = 8.0
    return layer


def _provider(graph):
    """node_id → 生产形态 payload（与 GL 执行测试同一构造语义）。"""
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


def _assert_dual_tolerance(cpu: np.ndarray, gl: np.ndarray,
                           label: str) -> None:
    diff = np.abs(cpu.astype(np.float64) - gl.astype(np.float64))
    mag = np.maximum(np.abs(gl.astype(np.float64)), 1e-6)
    sdr = np.abs(gl) <= 1.0
    hdr = ~sdr
    assert diff[sdr].max(initial=0.0) <= SDR_TOL, (
        f"{label}: SDR abs {diff[sdr].max(initial=0.0):.3e} > {SDR_TOL:.3e}")
    rel = diff[hdr] / mag[hdr]
    assert rel.max(initial=0.0) <= HDR_REL, (
        f"{label}: HDR rel {rel.max(initial=0.0):.3e} > {HDR_REL:.3e}")


class TestProductionCorrectorParity:
    def _run_pair(self, layers, source):
        graph = migrate_legacy_stack(_stack_with_layers(layers))
        provider = _provider(graph)
        plan = compile_graph(freeze_graph(graph, session_id="p"),
                             size=source.shape[:2][::-1])
        # GL 从 RGBA16F 纹理读入（fp16 量化）；CPU 参考必须用同一份
        # 量化输入（hue/contrast 的 S 曲线会放大量化差，见
        # test_metal_kernels_color 的同类先例）
        quantized = source.astype(np.float16).astype(np.float32)
        cpu = run_plan_cpu(plan, quantized,
                           lambda p: provider(p.node_id))
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            h, w = source.shape[:2]
            src_tex = ctx.texture((w, h), components=4, dtype="f2",
                                  data=source.astype(np.float16))
            result = executor.execute_plan(
                plan, src_tex, request_id="parity", source_size=(w, h),
                layer_param_provider=provider)
            assert result.published, result.diagnostic_message
            fbo = ctx.framebuffer(color_attachments=[result.output_texture])
            fbo.use()
            gl_out = np.frombuffer(
                fbo.read(components=4, dtype="f2"),
                dtype=np.float16).reshape(h, w, 4).astype(np.float32)
            executor.release_revision("parity")
            src_tex.release()
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()
        return cpu.output, gl_out

    def test_neutral_layer_identity_both_backends(self):
        src = f01_hdr_ramp(64)
        cpu, gl = self._run_pair([_neutral_layer("N")], src)
        _assert_dual_tolerance(cpu, gl, "neutral")
        _assert_dual_tolerance(
            cpu, src.astype(np.float16).astype(np.float32),
            "neutral-cpu-vs-quantized-input")

    def test_full_basic_domain_f01(self):
        cpu, gl = self._run_pair([_rich_layer()], f01_hdr_ramp(64))
        _assert_dual_tolerance(cpu, gl, "rich-f01")

    def test_full_basic_domain_f02_patches(self):
        cpu, gl = self._run_pair([_rich_layer()], f02_patches(64))
        _assert_dual_tolerance(cpu, gl, "rich-f02")
        # alpha 政策：透明边不被 grading 改写
        border = 2
        assert np.allclose(gl[:border, :, 3], cpu[:border, :, 3], atol=0)

    def test_curves_layer_parity(self):
        cpu, gl = self._run_pair([_curves_layer()], f01_hdr_ramp(64))
        _assert_dual_tolerance(cpu, gl, "curves")

    def test_serial_two_correctors_non_commutative(self):
        layers = [_rich_layer("A"), _curves_layer("B")]
        cpu, gl = self._run_pair(layers, f01_hdr_ramp(64))
        _assert_dual_tolerance(cpu, gl, "serial-AB")
        reversed_pair, _ = self._run_pair(list(reversed(layers)),
                                          f01_hdr_ramp(64))
        assert not np.allclose(cpu, reversed_pair, atol=1e-3), (
            "参数域应非交换（构造性检查，防止双侧同错恒等）")

    def test_opacity_fold_parity(self):
        layer = _rich_layer("O")
        layer.opacity = 0.35
        cpu, gl = self._run_pair([layer], f02_patches(64))
        _assert_dual_tolerance(cpu, gl, "opacity-0.35")


class TestCpuDomainFailClosed:
    def test_plugin_params_rejected(self):
        layer = {"grading_params": {}, "plugins": [{"name": "p"}]}
        with pytest.raises(UnsupportedNode):
            corrector_production_oracle(f01_hdr_ramp(16), layer)

    def test_neighborhood_params_rejected(self):
        layer = {"grading_params": {"mid_detail": 5.0}}
        with pytest.raises(UnsupportedNode):
            corrector_production_oracle(f01_hdr_ramp(16), layer)

    def test_unknown_grading_key_rejected(self):
        layer = {"grading_params": {"mystery_knob": 1.0}}
        assert any("mystery_knob" in e for e in
                   production_layer_domain_errors(layer))

    def test_non_normal_blend_rejected(self):
        layer = {"grading_params": {}, "blend_mode": "Screen"}
        assert any("blend_mode" in e for e in
                   production_layer_domain_errors(layer))

    def test_curves_data_without_lut_rejected(self):
        layer = {"grading_params": {}, "curves_data": {"M": [(0, 255)]}}
        assert any("curves_lut" in e for e in
                   production_layer_domain_errors(layer))
