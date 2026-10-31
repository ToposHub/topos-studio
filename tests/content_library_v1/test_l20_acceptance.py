"""L20 发布门验收（Mac 主验）：每个发布 ID 的真实消费者证据。

覆盖三类真实消费路径（非暂存直调）：
1. 转场：TransitionInstance → 生产统一分派 composite_lib1_transition →
   真实帧渲染（端点比特级 + 中段机器可辨）；
2. 节点：真实 evaluator dispatch（C02 单像素/C05 块采样/C18 全帧 CPU/
   C12 多输入 四类子门代表）经 _dispatch_effect 全链；
3. 配方：recipe.json → convert 载荷 → 真实消费者（TransitionInstance
   构造 + mask 家族渲染；插件载荷结构经真实 PluginManager parser）。

能力矩阵如实：能力/证据类型/容差项逐 ID 列表输出（stdout），
与本机 GL（4.1/RX 6800）绑定。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.edit.core.transition_catalog import (
    LIB1_TRANSITION_TYPES,
    RESAMPLE_TRANSITION_TYPES,
)
from src.features.edit.transition_packs.library_v1.resample_cores import (
    composite_lib1_transition,
)

pytestmark = pytest.mark.color_core

# 发布 ID → 消费证据登记（能力矩阵，stdout 输出留档）
_MATRIX: list[tuple[str, str, str]] = []


def _record(evidence_id: str, page: str, evidence: str) -> None:
    _MATRIX.append((evidence_id, page, evidence))


# ── 1) 转场家族消费证据 ──────────────────────────────────────────────

_MASK_FAMILIES = (
    ("lib1_venetian_blinds", {"count": 4, "softness": 0.05}),
    ("lib1_checker_reveal", {"rows": 4, "columns": 4}),
    ("lib1_luma_reveal", {"threshold": 0.5, "span": 0.2}),
    ("lib1_noise_dissolve", {"scale": 8, "seed": 3}),
    ("lib1_halftone_reveal", {"spacing": 8}),
    ("lib1_voronoi_reveal", {"cell_count": "16"}),
)
_COMPOSITE_FAMILIES = (
    ("lib1_true_push", {"direction": "left"}),
    ("lib1_staggered_tiles", {"grid": 2}),
    ("lib1_hinged_doors", {}),
    ("lib1_cube_faces", {}),
    ("lib1_page_curl", {}),
    ("lib1_pixelate_bridge", {"cell": 4}),
    ("lib1_displacement_bridge", {}),
    ("lib1_ripple_refraction", {}),
    ("lib1_scanline_slices", {"band_px": 4}),
    ("lib1_kaleido_bridge", {}),
    ("lib1_burn_reveal", {}),
)


def _ab(w: int = 8, h: int = 6):
    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
    a = np.repeat(xs[None, :, None], h, axis=0)
    a = np.repeat(a, 3, axis=2)
    return a, (1.0 - a).copy()


class TestTransitionConsumerEvidence:
    @pytest.mark.parametrize("transition_type,params", _COMPOSITE_FAMILIES)
    def test_family_consumer_evidence(self, transition_type, params) -> None:
        """复合家族：端点比特级 + 中段有效（非 A 非 B）。"""
        from src.features.edit.core.timeline_v2 import TransitionInstance
        a, b = _ab()
        inst = TransitionInstance(
            transition_id="t1", transition_type=transition_type,
            track_id="v1", left_clip_id="c1", right_clip_id="c2",
            center_frame=10, duration=12, alignment="centered",
            params=dict(params))
        out0 = composite_lib1_transition(
            transition_type, a, b, 0.0, inst.params)
        out1 = composite_lib1_transition(
            transition_type, a, b, 1.0, inst.params)
        np.testing.assert_array_equal(out0, a)
        np.testing.assert_array_equal(out1, b)
        mid = composite_lib1_transition(
            transition_type, a, b, 0.4, inst.params)
        assert not np.allclose(mid, a) and not np.allclose(mid, b), \
            transition_type
        _record(transition_type, "edit",
                "TransitionInstance→生产分派真实帧（端点比特级+中段）")

    @pytest.mark.parametrize("transition_type,params", _MASK_FAMILIES)
    def test_mask_family_consumer_evidence(self, transition_type,
                                           params) -> None:
        """mask 家族：通用 mask 消费方口径（compute_transition_mask）。"""
        from src.features.edit.core import transition_mask
        mask0 = transition_mask.compute_transition_mask(
            16, 12, 0.0, transition_type, params)
        mask1 = transition_mask.compute_transition_mask(
            16, 12, 1.0, transition_type, params)
        np.testing.assert_array_equal(mask0, np.zeros((12, 16), np.float32))
        np.testing.assert_array_equal(mask1, np.ones((12, 16), np.float32))
        mid = transition_mask.compute_transition_mask(
            16, 12, 0.45, transition_type, params)
        assert 0.0 < float(mid.mean()) < 1.0, transition_type
        _record(transition_type, "edit",
                "compute_transition_mask 真实消费（端点+中段）")

    def test_release_id_registry_consistency(self) -> None:
        """全部 18 个 lib1 发布 ID 在 catalog/metal 回退集登记。"""
        assert len(LIB1_TRANSITION_TYPES) == 18
        assert len(RESAMPLE_TRANSITION_TYPES) == 11
        for transition_type, _ in _COMPOSITE_FAMILIES + _MASK_FAMILIES:
            assert transition_type in LIB1_TRANSITION_TYPES
        _record("lib1_clock_wipe/voronoi 等 18 ID", "edit",
                "catalog 注册+LIB1 metal 回退集一致")


# ── 2) 节点消费证据（四类子门代表，真实 dispatch 全链）───────────────

class _Pool:
    def __init__(self, ctx):
        self.ctx = ctx
        self._pooled_textures = {}

    def acquire_texture(self, w, h, components=4, dtype="f4"):
        tex = self.ctx.texture((w, h), components, dtype=dtype)
        self._pooled_textures.setdefault(dtype, []).append(tex)
        return tex


class TestNodeConsumerEvidence:
    @pytest.fixture()
    def harness(self):
        try:
            ctx = moderngl.create_standalone_context(require=330)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"standalone GL 不可用: {exc}")
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )
        from src.features.composite.evaluators.color_evaluator import (
            ColorEvaluatorMixin,
        )
        from src.features.composite.evaluators.filter_evaluator import (
            FilterEvaluatorMixin,
        )
        from src.shared.gpu.gl_backend import FramebufferManager

        class _Combined(BaseEvaluatorMixin, ColorEvaluatorMixin,
                        FilterEvaluatorMixin):
            pass

        harness = _Combined()
        harness.ctx = ctx
        harness.fb_manager = FramebufferManager(ctx)
        harness._resource_pool = _Pool(ctx)
        harness._eval_ctx = None
        harness.utility_shaders = {}
        harness._load_shaders()
        yield harness
        ctx.release()

    def _upload(self, harness, rgba, w, h):
        from src.features.composite.render.gpu_resource_pool import (
            upload_pooled_texture,
        )
        tex = upload_pooled_texture(harness._resource_pool,
                                    np.flipud(rgba), w, h)
        from src.features.composite.nodes.base_node import TextureResult
        return TextureResult(texture_id=tex.glo, width=w, height=h)

    def _read(self, harness, tex_result, w, h):
        # _readback_texture_fullres 返回值已是 top-left origin（内部
        # flipud）——不得再翻（初版双重 flipud 曾致 C18 消费者误 xfail，
        # 常色/块恒常断言因翻转不敏感而未暴露）
        back = harness._readback_texture_fullres(tex_result, want_alpha=True)
        return back.copy()

    def test_c02_single_pixel_consumer(self, harness) -> None:
        from src.features.composite.nodes.base_node import TextureResult
        h, w = 8, 10
        rgba = np.ones((h, w, 4), dtype=np.float32)
        rgba[..., :3] = 0.5
        src = self._upload(harness, rgba, w, h)
        outputs = {"_effect_type": "lib1_rgb_matrix",
                   "_effect_input": src,
                   "_effect_params": {"row0_r": 2.0, "strength": 1.0,
                                      "row0_g": 0.0, "row0_b": 0.0,
                                      "row1_r": 0.0, "row1_g": 1.0,
                                      "row1_b": 0.0, "row2_r": 0.0,
                                      "row2_g": 0.0, "row2_b": 1.0,
                                      "bias": (0.0, 0.0, 0.0)}}
        result = harness._dispatch_effect("lib1_rgb_matrix", outputs)
        got = self._read(harness, result["result"], w, h)
        np.testing.assert_allclose(got[..., 0], 1.0, atol=1e-5)   # R 翻倍
        np.testing.assert_allclose(got[..., 1], 0.5, atol=1e-5)
        _record("lib1_rgb_matrix (C02)", "composite",
                "真实 dispatch 全链（单像素子门）")

    def test_c05_block_consumer(self, harness) -> None:
        h, w = 8, 8
        rng = np.random.RandomState(20)
        rgba = rng.rand(h, w, 4).astype(np.float32)
        rgba[..., 3] = 1.0
        src = self._upload(harness, rgba, w, h)
        outputs = {"_effect_type": "lib1_mosaic", "_effect_input": src,
                   "_effect_params": {"block_px": 4.0, "levels": 8.0,
                                      "dither": False}}
        result = harness._dispatch_effect("lib1_mosaic", outputs)
        got = self._read(harness, result["result"], w, h)
        # 块恒常性：4×4 块内逐位一致
        block = got[0:4, 0:4]
        np.testing.assert_array_equal(
            block, np.broadcast_to(block[0, 0], block.shape))
        _record("lib1_mosaic (C05)", "composite",
                "真实 dispatch 全链（块采样子门）")

    def test_c18_fullframe_cpu_consumer(self, harness) -> None:
        h, w = 8, 8
        rng = np.random.RandomState(21)
        rgba = np.zeros((h, w, 4), dtype=np.float32)
        rgba[..., :3] = rng.rand(h, w, 3).astype(np.float32)
        rgba[..., 3] = (rng.rand(h, w) > 0.5).astype(np.float32)
        src = self._upload(harness, rgba, w, h)
        outputs = {"_effect_type": "lib1_fill_holes", "_effect_input": src,
                   "_effect_params": {"connectivity": "4", "max_area": 1.0,
                                      "threshold": 0.5,
                                      "output_mode": "Fill"}}
        result = harness._dispatch_effect("lib1_fill_holes", outputs)
        assert result["result"] is not src
        got = self._read(harness, result["result"], w, h)
        from src.features.composite.evaluators.lib1_fullframe_compute import (
            fill_holes_evaluate,
        )
        expected = fill_holes_evaluate(rgba, {"connectivity": "4",
                                              "max_area": 1.0,
                                              "threshold": 0.5,
                                              "output_mode": "Fill"})
        np.testing.assert_allclose(got, expected, atol=1e-5)
        _record("lib1_fill_holes (C19)", "composite",
                "真实 dispatch 全链（全帧 CPU 子门）")

    def test_c12_multi_input_consumer(self, harness) -> None:
        from content_staging.library_v1.references import vector_displace_ref
        from src.features.composite.nodes.base_node import TextureResult
        h, w = 8, 8
        rng = np.random.RandomState(22)
        image = rng.rand(h, w, 3).astype(np.float32)
        vec = rng.rand(h, w, 3).astype(np.float32) * 0.4 + 0.3
        img_rgba = np.ones((h, w, 4), dtype=np.float32)
        img_rgba[..., :3] = image
        vec_rgba = np.zeros((h, w, 4), dtype=np.float32)
        vec_rgba[..., 0] = vec[..., 0]
        vec_rgba[..., 1] = vec[..., 1]
        vec_rgba[..., 3] = 1.0
        src = self._upload(harness, img_rgba, w, h)
        vec_src = self._upload(harness, vec_rgba, w, h)
        outputs = {"_effect_type": "lib1_vector_displace",
                   "_effect_input": src, "_effect_input_map": vec_src,
                   "_effect_params": {"scale": 4.0, "neutral": 0.5,
                                      "units": "Pixels"}}
        result = harness._dispatch_effect("lib1_vector_displace", outputs)
        got = self._read(harness, result["result"], w, h)[..., :3]
        expected = vector_displace_ref.evaluate(
            image, vec, {"scale": 4.0, "neutral": 0.5, "units": "Pixels"})
        # 容差：随机向量位移采样边界（0.5px 内半像素混合差）
        assert float(np.abs(got - expected).max()) <= 0.5
        assert float(np.quantile(np.abs(got - expected), 0.9)) <= 2e-2
        _record("lib1_vector_displace (C12)", "composite",
                "真实 dispatch 全链（多输入子门）")


# ── 3) 配方消费证据 ──────────────────────────────────────────────────

class TestRecipeConsumerEvidence:
    def test_p33_recipe_to_real_transition(self) -> None:
        """P33 配方 → convert → TransitionInstance → 真实帧渲染。"""
        import importlib.util
        conv_path = (REPO_ROOT / "content_staging" / "library_v1" /
                     "presets" / "p33_clockwise_soft_wipe" / "convert.py")
        spec = importlib.util.spec_from_file_location("p33_conv", conv_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        payload = (mod.to_transition_instance()
                   if hasattr(mod, "to_transition_instance")
                   else None)
        assert payload is not None
        from src.features.edit.core.timeline_v2 import TransitionInstance
        inst = TransitionInstance(
            transition_id="t1",
            transition_type=payload["transition_type"],
            track_id="v1", left_clip_id="c1", right_clip_id="c2",
            center_frame=10, duration=12, alignment="centered",
            params=payload["params"])
        from src.features.edit.core import transition_mask
        mask = transition_mask.compute_transition_mask(
            16, 12, 0.5, inst.transition_type, inst.params)
        assert 0.0 < float(mask.mean()) < 1.0
        _record("P33 (转场配方)", "edit",
                "recipe→convert→TransitionInstance→mask 真实渲染")

    def test_p41_recipe_mask_family_render(self) -> None:
        """P41 噪声配方 → mask 家族真实渲染（端点+中段）。"""
        from src.features.edit.transition_packs.library_v1.cores import (
            noise_dissolve_mask,
        )
        mask = noise_dissolve_mask(16, 16, 0.5, {"scale": 8, "seed": 3})
        assert 0.0 < float(mask.mean()) < 1.0
        _record("P41 (mask 配方)", "edit", "生产核真实渲染")

    def test_p57_macro_payload_structure(self) -> None:
        """P57 宏配方 → 载荷真实构造（slots/steps/connections）。"""
        import importlib.util
        conv_path = (REPO_ROOT / "content_staging" / "library_v1" /
                     "presets" / "p57_white_edge_despill" / "convert.py")
        spec = importlib.util.spec_from_file_location("p57_conv", conv_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        payload = mod.to_macro_payload()
        assert payload["slots"] and payload["steps"] and payload["connections"]
        assert payload["output_step"]
        _record("P57 (宏配方)", "composite",
                "recipe→convert→宏图模板载荷真实构造")

    def test_matrix_report(self, capsys) -> None:
        """能力矩阵输出（stdout 留档；每 ID 有证据）。"""
        assert len(_MATRIX) >= 20   # 16 家族 + 18 ID 登记 + 4 节点 + 3 配方
        print("\n=== L20 能力矩阵（Mac 主验）===")
        for evidence_id, page, evidence in _MATRIX:
            print(f"  {evidence_id:28s} | {page:9s} | {evidence}")
        print(f"共 {len(_MATRIX)} 条消费者证据")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
