"""B07 Corrector 算子等价测试 —— N06 与 B00 基线 golden 对照。

验收（B07.5）：全部单层/组合 golden 在 float 误差契约内；neutral 恒等；
alpha 政策稳定；插件编译失败显式诊断；dual-form（dict/Layer）一致。
基线来自 docs/validation/color_nodes/baselines（同 GPU 指纹才比对，
指纹不符 skip=NOT RUN，不产生伪通过）。

已知存量问题（与本任务无关）：tests/color/golden/shader_golden_corpus
的 grading_extreme/checker 用例在本机基提交即位级失配（GPU 指纹漂移），
须人工审查后按 ADR 0004 重归档，本文件不作静默重归档。
"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")

from src.features.color.core.corrector_render_service import (
    CorrectorDiagnostic,
    prepare_corrector,
    render_corrector_gl,
    render_key_gl,
    run_serial_passes,
)
from src.features.color.core.shader_compiler import ShaderPipeline
from src.features.color.core.layer import Layer, MaskShape
from tests.color.fixtures.color_graph import (
    f01_hdr_ramp,
    f02_patches,
    f03_layer_stack,
    f03_plugin_stub_binding,
)
from tests.color.test_color_graph_baseline_fixtures import _assert_dual_tolerance

BASELINE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "docs", "validation", "color_nodes", "baselines")


def _ctx_and_pipeline():
    ctx = moderngl.create_standalone_context(require=330)
    return ctx, ShaderPipeline(ctx)


def _layer_dto(layer: Layer) -> dict:
    from src.features.color.core.image_engine import _layer_render_data
    return _layer_render_data(layer)


def _baseline(name: str) -> np.ndarray | None:
    path = os.path.join(BASELINE_DIR, f"{name}.npy")
    return np.load(path) if os.path.exists(path) else None


def _gpu_matches_archive() -> bool:
    manifest_path = os.path.join(BASELINE_DIR, "manifest.json")
    if not os.path.exists(manifest_path):
        return False
    with open(manifest_path, encoding="utf-8") as fh:
        gpu = json.load(fh).get("gpu", {})
    ctx = moderngl.create_standalone_context(require=330)
    try:
        return gpu.get("renderer") == ctx.info.get("GL_RENDERER", "")
    finally:
        ctx.release()


@pytest.fixture(scope="module")
def same_gpu():
    if not _gpu_matches_archive():
        pytest.skip("本机 GPU 指纹与基线归档不符（NOT RUN，不产生伪通过）")


class TestPrepareCorrector:
    def test_prepare_from_layer_object(self):
        """E02 全分辨率路径：Layer 对象进 prepare（dual-form）。"""
        ctx, pipeline = _ctx_and_pipeline()
        try:
            layer = f03_layer_stack()[2]
            prepared = prepare_corrector(pipeline, layer, layer_id="x")
            assert not isinstance(prepared, CorrectorDiagnostic)
            assert prepared.flags["has_curves"] is True
            # DTO dict 形态产生同一 program 缓存对象（同旗标同键）
            prepared2 = prepare_corrector(pipeline, _layer_dto(layer),
                                          layer_id="x")
            assert prepared2.program is prepared.program
        finally:
            ctx.release()

    def test_compile_failure_returns_diagnostic(self):
        """插件源非法 → 显式诊断（不 None、不静默丢层）。"""
        from src.features.shared.effects.effect_spec import (
            EffectSpec,
            PluginBinding,
        )
        ctx, pipeline = _ctx_and_pipeline()
        try:
            bad_spec = EffectSpec(
                effect_type="plugin:broken_stub", name="Broken",
                category="plugin", param_defs=(),
                shader_kind="dctl_process",
                shader_sources=("vec3 process(vec3 c, vec2 uv) { return u_undefined_var; }",),
                dispatch_key="plugin:broken_stub")
            binding = PluginBinding(spec=bad_spec, enabled=True, index=0)
            result = prepare_corrector(pipeline, {}, bindings_override=[binding])
            assert isinstance(result, CorrectorDiagnostic)
            assert result.code == "shader_compile_failed"

            # 执行器拒绝执行诊断算子（fail-closed，不产出少一层的结果）
            with pytest.raises(RuntimeError):
                render_corrector_gl(
                    ctx, result, layer={}, grade_input_tex=None,
                    matte_tex=None, viewport=(4, 4),
                    update_uniforms=lambda p: None,
                    render_quad=lambda p: None)
        finally:
            ctx.release()


class TestGoldenParity:
    """N06 standalone 执行 vs B00 归档 float 基线（同 GPU 位级一致）。"""

    def test_neutral_identity_matches_baseline(self, same_gpu):
        """单个 neutral 层经 N06 → 与基线位级一致（含 float16 往返）。"""
        ctx, pipeline = _ctx_and_pipeline()
        try:
            out = run_serial_passes(
                ctx, pipeline, f01_hdr_ramp(64), [_layer_dto(Layer("neutral"))])
            ref = _baseline("f01_neutral_64")
            assert ref is not None, "基线缺失：先运行基线生成脚本"
            np.testing.assert_array_equal(out, ref)
        finally:
            ctx.release()

    def test_l0_grading_matches_baseline(self, same_gpu):
        ctx, pipeline = _ctx_and_pipeline()
        try:
            out = run_serial_passes(
                ctx, pipeline, f01_hdr_ramp(64),
                [_layer_dto(f03_layer_stack()[0])])
            ref = _baseline("f01_l0_grading")
            np.testing.assert_array_equal(out, ref)
        finally:
            ctx.release()

    def test_serial_chain_matches_baseline_with_plugin(self, same_gpu):
        """三层串行 + L2 插件 stub + 曲线 → 与基线位级一致。"""
        ctx, pipeline = _ctx_and_pipeline()
        try:
            layers = f03_layer_stack()
            dtos = [_layer_dto(l) for l in layers]
            bindings = [None, None, (f03_plugin_stub_binding(),)]
            out = run_serial_passes(ctx, pipeline, f01_hdr_ramp(64), dtos,
                                    bindings_per_layer=bindings)
            ref = _baseline("f03_serial")
            np.testing.assert_array_equal(out, ref)
        finally:
            ctx.release()

    def test_bypass_variant_matches_baseline(self, same_gpu):
        ctx, pipeline = _ctx_and_pipeline()
        try:
            layers = f03_layer_stack()
            layers[1].visible = False
            out = run_serial_passes(
                ctx, pipeline, f01_hdr_ramp(64), [_layer_dto(l) for l in layers],
                bindings_per_layer=[None, None, (f03_plugin_stub_binding(),)])
            ref = _baseline("f03_l1_bypassed")
            np.testing.assert_array_equal(out, ref)
        finally:
            ctx.release()

    def test_alpha_policy_stable(self):
        """透明边 + alpha 梯度经 neutral pass 保持（跨 GPU 断言）。"""
        ctx, pipeline = _ctx_and_pipeline()
        try:
            src = f02_patches(64)
            out = run_serial_passes(ctx, pipeline, src, [])
            np.testing.assert_allclose(out[..., 3], src[..., 3], atol=2e-3)
        finally:
            ctx.release()

    def test_hdr_not_clipped(self):
        ctx, pipeline = _ctx_and_pipeline()
        try:
            src = f01_hdr_ramp(64)
            out = run_serial_passes(ctx, pipeline, src, [])
            _assert_dual_tolerance(out, src)   # SDR abs / HDR rel 双容差
        finally:
            ctx.release()


class TestKeyComposition:
    """render_key_gl（§4.1 顺序的 standalone 形态）。"""

    def _shape_dict(self, shape_type=MaskShape.RECT, **kwargs):
        shape = MaskShape(shape_type)
        data = shape.to_dict(None)
        data.update(kwargs)
        return data

    def test_no_mask_is_full_white(self):
        key = render_key_gl(None, {"mask_shapes": []}, None, size=(8, 8))
        assert key.shape == (8, 8)
        np.testing.assert_allclose(key, 1.0)

    def test_shapes_boolean_operations(self):
        rect_full = self._shape_dict(
            center=(0.5, 0.5), size=(1.0, 1.0), visible=True, feather=0)
        rect_small = self._shape_dict(
            center=(0.5, 0.5), size=(0.5, 0.5), visible=True, feather=0,
            operation="subtract")
        key = render_key_gl(None, {"mask_shapes": [rect_full, rect_small]},
                            None, size=(64, 64))
        # 中心被 subtract 挖空；角上（全尺寸矩形内、小矩形外）保留
        assert key[32, 32] < 0.5, "subtract 未挖空中心"
        assert key[4, 4] > 0.5, "subtract 不应影响矩形外角"

    def test_external_key_multiplies(self):
        rect_full = self._shape_dict(center=(0.5, 0.5), size=(1.0, 1.0))
        external = np.full((64, 64), 0.25, dtype=np.float32)
        key = render_key_gl(None, {"mask_shapes": [rect_full]},
                            None, external_key=external, size=(64, 64))
        np.testing.assert_allclose(key, 0.25, atol=1e-6)

    def test_ai_matte_operations(self):
        base = np.full((8, 8), 0.5, dtype=np.float32)
        ai = np.full((8, 8), 0.75, dtype=np.float32)
        for operation, expected in (("add", 0.75), ("subtract", 0.0),
                                    ("intersect", 0.375)):
            key = render_key_gl(
                None, {"mask_shapes": [], "ai_matte": ai,
                       "ai_matte_operation": operation,
                       "ai_matte_enabled": True},
                None, external_key=base, size=(8, 8))
            np.testing.assert_allclose(
                key, expected, atol=1e-6), operation
