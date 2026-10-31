"""CP-011 / CP-012 / CP-019 回归测试。

- CP-011 Keymix：注册、端口、GPU lerp 语义（mask=0→B、mask=1→A、invert、mix）。
- CP-012 Blend 新模式：from / exclusion / conjoint / disjoint 的像素语义。
- CP-019 Grain / DeGrain：注册、动画种子帧依赖、GPU 颗粒叠加与门控去颗粒。
"""

import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.format import BBox
from src.features.composite.core.node_engine import NODE_REGISTRY, NODE_CATEGORIES
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.blend_node import BLEND_MODES

pytestmark = pytest.mark.composite_core

_W, _H = 16, 8


# ── Registration ────────────────────────────────────────────────────────────

class TestNewNodeRegistration:
    def test_keymix_registered_in_merge_category(self):
        from src.features.composite.nodes.keymix_node import KeymixNode
        assert NODE_REGISTRY.get("keymix") is KeymixNode
        assert any(t == "keymix" for t, _ in NODE_CATEGORIES.get("Merge", []))

    def test_grain_degrain_registered_in_filter_category(self):
        from src.features.composite.nodes.grain_node import GrainNode
        from src.features.composite.nodes.degrain_node import DeGrainNode
        assert NODE_REGISTRY.get("grain") is GrainNode
        assert NODE_REGISTRY.get("degrain") is DeGrainNode
        filter_types = [t for t, _ in NODE_CATEGORIES.get("Filter", [])]
        assert "grain" in filter_types and "degrain" in filter_types

    def test_keymix_ports(self):
        from src.features.composite.nodes.keymix_node import KeymixNode
        names = [p.name for p in KeymixNode(node_id="k").inputs]
        assert names == ["A", "B", "mask"]

    def test_grain_seed_is_frame_dependent_when_animated(self):
        from src.features.composite.nodes.grain_node import GrainNode
        node = GrainNode(node_id="g")
        node.parameters["animate"] = True
        assert GrainNode.params_require_frame_cache({"animate": True})
        assert not GrainNode.params_require_frame_cache({"animate": False})
        assert node.effective_seed(0) != node.effective_seed(1)
        node.parameters["animate"] = False
        assert node.effective_seed(0) == node.effective_seed(1)


# ── CP-012 blend mode list ──────────────────────────────────────────────────

class TestBlendModesExtended:
    def test_nuke_modes_present(self):
        for mode in ("stencil", "mask", "in", "out", "atop", "xor",
                     "from", "exclusion", "conjoint", "disjoint"):
            assert mode in BLEND_MODES, f"{mode} missing from BLEND_MODES"


# ── GPU semantics harness ───────────────────────────────────────────────────

class _GLHarness:
    @pytest.fixture()
    def harness(self):
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"Cannot create standalone GL context: {exc}")
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )
        from src.features.composite.evaluators.filter_evaluator import (
            FilterEvaluatorMixin,
        )
        from src.features.composite.evaluators.merge_evaluator import (
            MergeEvaluatorMixin,
        )
        from src.features.composite.evaluators.color_evaluator import (
            ColorEvaluatorMixin,
        )
        from src.features.composite.evaluators.blur_evaluator import (
            BlurEvaluatorMixin,
        )

        class _Pool:
            def __init__(self, ctx):
                self.ctx = ctx
                self._pooled_textures = {}

            def acquire_texture(self, w, h, components=4, dtype="f4"):
                tex = self.ctx.texture((w, h), components, dtype=dtype)
                self._pooled_textures.setdefault(dtype, []).append(tex)
                return tex

        class _H(BaseEvaluatorMixin, MergeEvaluatorMixin,
                 FilterEvaluatorMixin, ColorEvaluatorMixin,
                 BlurEvaluatorMixin):
            def __init__(self, ctx):
                self.ctx = ctx
                self.utility_shaders = {}
                self._resource_pool = _Pool(ctx)
                from src.shared.gpu.gl_backend import FramebufferManager
                self.fb_manager = FramebufferManager(ctx)
                self._load_shaders()

        h = _H(ctx)
        yield h
        ctx.release()

    @staticmethod
    def _upload(harness, rgba):
        h, w = rgba.shape[:2]
        tex = harness._resource_pool.acquire_texture(w, h)
        tex.write(np.ascontiguousarray(np.flipud(rgba)).tobytes())
        return TextureResult(
            texture_id=tex.glo, width=w, height=h,
            bbox=BBox.full_frame(w, h),
            alpha_premultiplied=False, color_space="linear")

    @staticmethod
    def _read(harness, tex_result):
        tex = harness._get_texture_by_id(tex_result.texture_id)
        fbo = harness.fb_manager.get_fbo(tex)
        fbo.use()
        harness.ctx.viewport = (0, 0, tex_result.width, tex_result.height)
        raw = fbo.read(components=4, dtype="f4")
        arr = np.frombuffer(raw, dtype=np.float32).reshape(
            tex_result.height, tex_result.width, 4)
        return np.flipud(arr).copy()


class TestKeymixGPU(_GLHarness):
    def _a_b(self):
        a = np.full((_H, _W, 4), (1.0, 0.0, 0.0, 1.0), dtype=np.float32)
        b = np.full((_H, _W, 4), (0.0, 0.0, 1.0, 1.0), dtype=np.float32)
        return a, b

    def test_mask_zero_returns_b_mask_one_returns_a(self, harness, qapp):
        harness
        a, b = self._a_b()
        mask = np.zeros((_H, _W, 4), dtype=np.float32)
        mask[:, :_W // 2] = 1.0
        out = harness._apply_keymix({
            "_effect_input_a": self._upload(harness, a),
            "_effect_input_b": self._upload(harness, b),
            "_effect_mask": self._upload(harness, mask),
            "_effect_params": {"mask_channel": "luma", "mix": 1.0},
        })
        got = self._read(harness, out["result"])
        assert np.allclose(got[:, :_W // 2, 0], 1.0, atol=1e-4)   # A
        assert np.allclose(got[:, _W // 2:, 2], 1.0, atol=1e-4)   # B

    def test_invert_flips(self, harness, qapp):
        harness
        a, b = self._a_b()
        mask = np.zeros((_H, _W, 4), dtype=np.float32)
        mask[:, :_W // 2] = 1.0
        out = harness._apply_keymix({
            "_effect_input_a": self._upload(harness, a),
            "_effect_input_b": self._upload(harness, b),
            "_effect_mask": self._upload(harness, mask),
            "_effect_params": {"mask_channel": "luma", "mask_invert": True,
                               "mix": 1.0},
        })
        got = self._read(harness, out["result"])
        assert np.allclose(got[:, :_W // 2, 2], 1.0, atol=1e-4)

    def test_half_mix_blends(self, harness, qapp):
        harness
        a, b = self._a_b()
        out = harness._apply_keymix({
            "_effect_input_a": self._upload(harness, a),
            "_effect_input_b": self._upload(harness, b),
            "_effect_mask": None,
            "_effect_params": {"mask_channel": "luma", "mix": 0.5},
        })
        got = self._read(harness, out["result"])
        # 无 mask 时 t = mix = 0.5 → RGB 各半
        assert np.allclose(got[:, :, 0], 0.5, atol=1e-3)

    def test_missing_mask_open_gate(self, harness, qapp):
        harness
        a, b = self._a_b()
        out = harness._apply_keymix({
            "_effect_input_a": self._upload(harness, a),
            "_effect_input_b": self._upload(harness, b),
            "_effect_mask": None,
            "_effect_params": {"mask_channel": "luma", "mix": 1.0},
        })
        got = self._read(harness, out["result"])
        assert np.allclose(got[:, :, 0], 1.0, atol=1e-4)


class TestBlendNewModesGPU(_GLHarness):
    def _run_blend(self, harness, mode, a_rgba, b_rgba, opacity=1.0):
        a = self._upload(harness, a_rgba)
        b = self._upload(harness, b_rgba)
        out = harness._apply_merge_blend(
            a, b, None, mode, opacity, "from_b", "union", "auto")
        return self._read(harness, out["result"])

    def test_from_subtracts_b_from_a(self, harness, qapp):
        harness
        a = np.full((_H, _W, 4), (0.9, 0.9, 0.9, 1.0), dtype=np.float32)
        b = np.full((_H, _W, 4), (0.4, 0.4, 0.4, 1.0), dtype=np.float32)
        got = self._run_blend(harness, "from", a, b)
        assert np.allclose(got[:, :, :3], 0.5, atol=1e-3)

    def test_exclusion_midgray(self, harness, qapp):
        harness
        # A=B=0.5 → 0.5+0.5-2*0.25 = 0.5
        a = np.full((_H, _W, 4), (0.5, 0.5, 0.5, 1.0), dtype=np.float32)
        b = np.full((_H, _W, 4), (0.5, 0.5, 0.5, 1.0), dtype=np.float32)
        got = self._run_blend(harness, "exclusion", a, b)
        assert np.allclose(got[:, :, :3], 0.5, atol=1e-3)

    def test_conjoint_caps_alpha_at_one(self, harness, qapp):
        harness
        a = np.full((_H, _W, 4), (0.5, 0.0, 0.0, 0.8), dtype=np.float32)
        b = np.full((_H, _W, 4), (0.0, 0.0, 0.5, 0.6), dtype=np.float32)
        got = self._run_blend(harness, "conjoint", a, b)
        assert np.allclose(got[:, :, 3], 1.0, atol=1e-3)  # 0.8+0.6 capped

    def test_disjoint_sums_when_no_overlap(self, harness, qapp):
        harness
        a = np.full((_H, _W, 4), (0.5, 0.0, 0.0, 0.4), dtype=np.float32)
        b = np.full((_H, _W, 4), (0.0, 0.0, 0.5, 0.4), dtype=np.float32)
        got = self._run_blend(harness, "disjoint", a, b)
        assert np.allclose(got[:, :, 3], 0.8, atol=1e-3)  # 0.4+0.4 ≤ 1
        # 预乘域：0.5*0.4 + 0 = 0.2（两路各按自身 alpha 贡献）
        assert np.allclose(got[:, :, 0], 0.2, atol=1e-3)
        assert np.allclose(got[:, :, 2], 0.2, atol=1e-3)


class TestGrainDeGrainGPU(_GLHarness):
    def test_grain_changes_pixels_with_seed(self, harness, qapp):
        harness
        base = np.full((_H, _W, 4), (0.18, 0.18, 0.18, 1.0), dtype=np.float32)
        tex = self._upload(harness, base)
        out1 = harness._apply_film_grain(tex, {
            "grain_size": 1.5, "grain_intensity": 0.4,
            "grain_type": "fine", "color_grain": False,
            "luminance_only": False, "seed": 1.0}, _W, _H)
        out2 = harness._apply_film_grain(tex, {
            "grain_size": 1.5, "grain_intensity": 0.4,
            "grain_type": "fine", "color_grain": False,
            "luminance_only": False, "seed": 99.0}, _W, _H)
        g1 = self._read(harness, out1["result"])
        g2 = self._read(harness, out2["result"])
        # 不同种子产生不同颗粒；均值仍在原值附近（零均值调制）
        assert not np.allclose(g1, g2, atol=1e-4)
        assert abs(g1[:, :, :3].mean() - 0.18) < 0.05

    def test_grain_zero_intensity_identity(self, harness, qapp):
        harness
        base = np.full((_H, _W, 4), (0.3, 0.3, 0.3, 1.0), dtype=np.float32)
        tex = self._upload(harness, base)
        out = harness._apply_film_grain(tex, {
            "grain_size": 1.5, "grain_intensity": 0.0,
            "grain_type": "fine", "color_grain": False,
            "luminance_only": False, "seed": 1.0}, _W, _H)
        got = self._read(harness, out["result"])
        assert np.allclose(got, base, atol=1e-5)

    def test_degrain_removes_small_deviation_keeps_edges(self, harness, qapp):
        harness
        # 平坦中灰 + 小幅颗粒 + 一条强边缘
        base = np.full((_H, _W, 4), (0.18, 0.18, 0.18, 1.0), dtype=np.float32)
        rng = np.random.default_rng(7)
        base[:, :, :3] += rng.normal(0, 0.005, (_H, _W, 3))  # grain < threshold
        base[:, :2, :3] = 0.8                                  # strong edge
        tex = self._upload(harness, base)
        out = harness._apply_degrain(tex, {
            "grain_threshold": 0.03, "spatial_sigma": 1.5,
            "amount": 1.0}, _W, _H)
        got = self._read(harness, out["result"])
        # 边缘保留
        assert np.allclose(got[:, :2, 0], 0.8, atol=0.05)
        # 平坦区方差显著下降
        flat_orig = base[:, 4:, :3].std()
        flat_out = got[:, 4:, :3].std()
        assert flat_out < flat_orig


# ── CP-020/021 VectorBlur ───────────────────────────────────────────────────

class TestVectorBlur:
    def test_registered_in_filter_category(self):
        from src.features.composite.nodes.vectorblur_node import VectorBlurNode
        assert NODE_REGISTRY.get("vectorblur") is VectorBlurNode
        assert any(t == "vectorblur"
                   for t, _ in NODE_CATEGORIES.get("Filter", []))

    def test_ports_image_velocity(self):
        from src.features.composite.nodes.vectorblur_node import VectorBlurNode
        names = [p.name for p in VectorBlurNode(node_id="v").inputs]
        # image + velocity + CP-044 通用 mask
        assert names[:2] == ["image", "velocity"] and "mask" in names

    def test_project_shutter_wins(self):
        from src.features.composite.nodes.vectorblur_node import VectorBlurNode
        node = VectorBlurNode(node_id="v")
        node.parameters["shutter"] = 0.1
        ctx = SimpleNamespace(motion_blur_shutter=0.8)
        assert node.effective_shutter(ctx) == 0.8
        node.parameters["use_project_shutter"] = False
        assert node.effective_shutter(ctx) == 0.1

    def test_falls_back_without_project_shutter(self):
        from src.features.composite.nodes.vectorblur_node import VectorBlurNode
        node = VectorBlurNode(node_id="v")
        node.parameters["shutter"] = 0.3
        assert node.effective_shutter(SimpleNamespace()) == 0.3
        assert node.effective_shutter(None) == 0.3


class TestVectorBlurGPU(_GLHarness):
    def test_zero_velocity_is_identity(self, harness, qapp):
        harness
        base = np.full((_H, _W, 4), (0.2, 0.2, 0.2, 1.0), dtype=np.float32)
        base[:, :2] = (1.0, 1.0, 1.0, 1.0)
        velocity = np.zeros((_H, _W, 4), dtype=np.float32)
        out = harness._apply_vectorblur(
            self._upload(harness, base),
            {"_velocity_tex": self._upload(harness, velocity),
             "shutter": 0.5}, _W, _H)
        got = self._read(harness, out["result"])
        assert np.allclose(got, base, atol=1e-4)

    def test_uniform_velocity_blurs_edge(self, harness, qapp):
        harness
        base = np.full((_H, _W, 4), (0.2, 0.2, 0.2, 1.0), dtype=np.float32)
        base[:, :2] = (1.0, 1.0, 1.0, 1.0)     # 左侧亮柱
        velocity = np.zeros((_H, _W, 4), dtype=np.float32)
        velocity[:, :, 0] = 0.08               # 每帧向 +U 位移 8%UV
        out = harness._apply_vectorblur(
            self._upload(harness, base),
            {"_velocity_tex": self._upload(harness, velocity),
             "shutter": 1.0, "edge_policy": "clamp"}, _W, _H)
        got = self._read(harness, out["result"])
        # 亮柱右缘被向右拖糊：第 2 列应介于 0.2 与 1.0 之间
        assert 0.25 < got[:, 2, 0].mean() < 0.99
        # 远处仍是暗底（末两列因测试纹理 REPEAT 边界双线性回卷排除在断言外）
        assert np.allclose(got[:, 3:_W - 2, 0], 0.2, atol=5e-2)


class TestProjectShutterResolution:
    def test_resolver_reads_output_node(self):
        from types import SimpleNamespace as NS
        from src.features.composite.core.eval_context import (
            resolve_project_motion_blur_shutter,
        )
        snap = NS(nodes={
            "blur1": NS(node_type="blur", parameters={}),
            "out1": NS(node_type="output",
                       parameters={"motion_blur_shutter": 0.75}),
        })
        assert resolve_project_motion_blur_shutter(snap) == 0.75
        snap2 = NS(nodes={"blur1": NS(node_type="blur", parameters={})})
        assert resolve_project_motion_blur_shutter(snap2) is None


# ── CP-014 ColorLookup ──────────────────────────────────────────────────────

class TestColorLookupData:
    def test_parse_curve_garbage_falls_back_identity(self):
        from src.features.composite.nodes.colorlookup_node import (
            parse_curve_points,
        )
        assert parse_curve_points("not json") == [(0.0, 0.0), (1.0, 1.0)]
        assert parse_curve_points(None) == [(0.0, 0.0), (1.0, 1.0)]

    def test_parse_curve_sorts_and_dedups(self):
        from src.features.composite.nodes.colorlookup_node import (
            parse_curve_points,
        )
        pts = parse_curve_points("[[0.8, 0.4], [0.2, 0.1], [0.8, 0.9]]")
        assert [p[0] for p in pts] == [0.2, 0.8]

    def test_bake_curve_endpoints_and_mid(self):
        from src.features.composite.nodes.colorlookup_node import (
            bake_curve_lut,
        )
        lut = bake_curve_lut([(0.0, 0.0), (0.5, 1.0), (1.0, 0.0)], 0.0, 1.0)
        assert lut[0] < lut[128] > lut[255]     # 峰在中部
        assert lut[0] == pytest.approx(0.0, abs=1e-3)
        # 256 级端点对齐量化：128/255≈0.502 处峰值 ≈0.996
        assert lut[128] == pytest.approx(1.0, abs=5e-3)

    def test_registered_in_color_category(self):
        from src.features.composite.nodes.colorlookup_node import ColorLookupNode
        assert NODE_REGISTRY.get("colorlookup") is ColorLookupNode
        assert any(t == "colorlookup"
                   for t, _ in NODE_CATEGORIES.get("Color", []))

    def test_generic_mask_port_present(self):
        from src.features.composite.nodes.colorlookup_node import ColorLookupNode
        names = [p.name for p in ColorLookupNode(node_id="cl").inputs]
        assert "mask" in names  # CP-044


class TestColorLookupGPU(_GLHarness):
    def test_identity_curves_passthrough(self, harness, qapp):
        harness
        base = np.linspace(0.1, 0.9, _W * _H).reshape(_H, _W)
        rgba = np.stack([base, base * 0.5, 1.0 - base,
                         np.ones((_H, _W))], axis=-1).astype(np.float32)
        ident = [[0.0, 0.0], [1.0, 1.0]]
        out = harness._apply_colorlookup(
            self._upload(harness, rgba),
            {"red_curve": ident, "green_curve": ident,
             "blue_curve": ident, "master_curve": ident,
             "input_min": 0.0, "input_max": 1.0}, _W, _H)
        got = self._read(harness, out["result"])
        assert np.allclose(got, rgba, atol=5e-3)

    def test_red_curve_lifts_lows(self, harness, qapp):
        harness
        base = np.full((_H, _W, 4), (0.25, 0.25, 0.25, 1.0), dtype=np.float32)
        ident = [[0.0, 0.0], [1.0, 1.0]]
        red_up = [[0.0, 0.5], [1.0, 1.0]]  # 低值抬到 0.5+
        out = harness._apply_colorlookup(
            self._upload(harness, base),
            {"red_curve": red_up, "green_curve": ident,
             "blue_curve": ident, "master_curve": ident,
             "input_min": 0.0, "input_max": 1.0}, _W, _H)
        got = self._read(harness, out["result"])
        assert got[:, :, 0].mean() > 0.5   # 红通道被抬升
        assert np.allclose(got[:, :, 1], 0.25, atol=5e-3)
        assert np.allclose(got[:, :, 2], 0.25, atol=5e-3)

    def test_master_scales_all(self, harness, qapp):
        harness
        base = np.full((_H, _W, 4), (0.5, 0.5, 0.5, 1.0), dtype=np.float32)
        ident = [[0.0, 0.0], [1.0, 1.0]]
        master = [[0.0, 0.0], [1.0, 0.5]]  # 0.5 → 0.25
        out = harness._apply_colorlookup(
            self._upload(harness, base),
            {"red_curve": ident, "green_curve": ident,
             "blue_curve": ident, "master_curve": master,
             "input_min": 0.0, "input_max": 1.0}, _W, _H)
        got = self._read(harness, out["result"])
        assert np.allclose(got[:, :, :3], 0.25, atol=5e-3)


# ── CP-015 CDL ──────────────────────────────────────────────────────────────

class TestCDLNode:
    def test_registered_in_color_category(self):
        from src.features.composite.nodes.cdl_node import CDLNode
        assert NODE_REGISTRY.get("cdl") is CDLNode
        assert any(t == "cdl" for t, _ in NODE_CATEGORIES.get("Color", []))

    def test_cdl_file_roundtrip(self, tmp_path):
        from src.features.composite.nodes.cdl_node import (
            CDLNode, parse_cdl_file, write_cdl_file,
        )
        node = CDLNode(node_id="cdl")
        node.set_parameter("slope_r", 1.2)
        node.set_parameter("offset_g", 0.05)
        node.set_parameter("power_b", 0.9)
        node.set_parameter("saturation", 0.8)
        p = str(tmp_path / "test.cdl")
        assert node.save_cdl(p)
        values = parse_cdl_file(p)
        assert values["slope"] == (1.2, 1.0, 1.0)
        assert values["offset"] == (0.0, 0.05, 0.0)
        assert values["power"] == (1.0, 1.0, 0.9)
        assert values["saturation"] == 0.8
        # 导入回一个新节点
        node2 = CDLNode(node_id="cdl2")
        assert node2.load_cdl(p)
        assert node2.parameters["slope_r"] == 1.2
        assert node2.parameters["saturation"] == 0.8

    def test_cdl_garbage_file_returns_none(self, tmp_path):
        from src.features.composite.nodes.cdl_node import parse_cdl_file
        bad = tmp_path / "bad.cdl"
        bad.write_text("not xml <<<")
        assert parse_cdl_file(str(bad)) is None

    def test_generic_mask_port(self):
        from src.features.composite.nodes.cdl_node import CDLNode
        assert "mask" in [p.name for p in CDLNode(node_id="c").inputs]


class TestCDLGPU(_GLHarness):
    def test_identity_cdl_passthrough(self, harness, qapp):
        harness
        base = np.full((_H, _W, 4), (0.25, 0.5, 0.75, 1.0), dtype=np.float32)
        out = harness._apply_cdl(
            self._upload(harness, base),
            {"slope": (1.0, 1.0, 1.0), "offset": (0.0, 0.0, 0.0),
             "power": (1.0, 1.0, 1.0), "saturation": 1.0}, _W, _H)
        got = self._read(harness, out["result"])
        assert np.allclose(got, base, atol=1e-4)

    def test_slope_scales_channels(self, harness, qapp):
        harness
        base = np.full((_H, _W, 4), (0.5, 0.5, 0.5, 1.0), dtype=np.float32)
        out = harness._apply_cdl(
            self._upload(harness, base),
            {"slope": (2.0, 1.0, 0.5), "offset": (0.0, 0.0, 0.0),
             "power": (1.0, 1.0, 1.0), "saturation": 1.0}, _W, _H)
        got = self._read(harness, out["result"])
        assert np.allclose(got[:, :, 0], 1.0, atol=1e-3)
        assert np.allclose(got[:, :, 1], 0.5, atol=1e-3)
        assert np.allclose(got[:, :, 2], 0.25, atol=1e-3)

    def test_desaturate_pulls_to_luma(self, harness, qapp):
        harness
        base = np.full((_H, _W, 4), (1.0, 0.0, 0.0, 1.0), dtype=np.float32)
        out = harness._apply_cdl(
            self._upload(harness, base),
            {"slope": (1.0, 1.0, 1.0), "offset": (0.0, 0.0, 0.0),
             "power": (1.0, 1.0, 1.0), "saturation": 0.0}, _W, _H)
        got = self._read(harness, out["result"])
        luma = 0.2126
        assert np.allclose(got[:, :, :3], luma, atol=1e-3)


# ── CP-058 变速/往复播放 ────────────────────────────────────────────────────

class TestPlaybackSpeed:
    def _panel(self, qapp):
        from src.features.composite.ui.keyframes_panel import KeyframesPanel
        panel = KeyframesPanel()
        return panel

    def test_speed_clamped(self, qapp):
        panel = self._panel(qapp)
        try:
            panel.set_playback_speed(99.0)
            assert panel._playback_speed == 4.0
            panel.set_playback_speed(0.01)
            assert panel._playback_speed == 0.25
        finally:
            panel.deleteLater()

    def test_pingpong_resets_direction(self, qapp):
        panel = self._panel(qapp)
        try:
            panel._play_direction = -1
            panel.set_play_pingpong(True)
            assert panel._play_direction == 1
            assert panel._play_pingpong
        finally:
            panel.deleteLater()


# ── 复查回归：隐藏 bug 修复验证（2026-10-14）─────────────────────────────────

class TestVectorBlurNoVelocityPassthrough(_GLHarness):
    """无 velocity 输入必须直通（修复前会走 5px 角度模糊路径）。"""

    def test_missing_velocity_returns_input(self, harness, qapp):
        harness
        base = np.full((_H, _W, 4), (0.3, 0.3, 0.3, 1.0), dtype=np.float32)
        base[:, :2] = (1.0, 1.0, 1.0, 1.0)
        tex = self._upload(harness, base)
        out = harness._apply_vectorblur(
            tex, {"_velocity_tex": None, "shutter": 1.0}, _W, _H)
        # 直通 = 返回同一 TextureResult（连亮柱边缘都不得被糊开）
        assert out["result"] is tex


class TestMultipart3ChannelBeauty:
    """3 通道 beauty 补不透明 alpha（修复前 IndexError 被吞、静默回落）。"""

    def test_rgb_beauty_gets_alpha(self, tmp_path):
        OpenEXR = pytest.importorskip("OpenEXR")
        from src.features.composite.core.exr_multipart_writer import (
            write_exr_multipart,
        )
        h, w = 4, 6
        rgb = np.full((h, w, 3), 0.5, dtype=np.float32)
        out = str(tmp_path / "rgb_beauty.exr")
        assert write_exr_multipart(out, rgb, {})
        f = OpenEXR.File(out, separate_channels=True)
        a = np.asarray(f.parts[0].channels["A"].pixels)
        assert np.allclose(a, 1.0, atol=1e-5)


class TestExrZipChannelOrder:
    """CP-006 绑定路径的 BGR→RGB 顺序（修复前 BGRA 全反转成 ARGB）。"""

    def test_bgra_roundtrip_channel_order(self, tmp_path, monkeypatch):
        pytest.importorskip("OpenEXR")
        import cv2  # noqa: F401 — 路径需要 cv2，但绑定写出绕开它
        from src.features.composite.nodes.file_output_node import FileOutputNode
        import numpy as np

        node = FileOutputNode(node_id="fo-zip")
        h, w = 4, 6
        # _prepare_for_export 约定输出 BGRA：B=0.1, G=0.2, R=0.3, A=0.5
        bgra = np.zeros((h, w, 4), dtype=np.float32)
        bgra[..., 0] = 0.1   # B
        bgra[..., 1] = 0.2   # G
        bgra[..., 2] = 0.3   # R
        bgra[..., 3] = 0.5   # A
        node.parameters["format"] = "EXR"
        node.parameters["compression"] = "zip"
        path = str(tmp_path / "order.exr")
        assert node._save_to_disk(bgra, path)

        import OpenEXR
        f = OpenEXR.File(path, separate_channels=True)
        ch = f.parts[0].channels
        assert np.allclose(np.asarray(ch["R"].pixels), 0.3, atol=1e-4)
        assert np.allclose(np.asarray(ch["G"].pixels), 0.2, atol=1e-4)
        assert np.allclose(np.asarray(ch["B"].pixels), 0.1, atol=1e-4)
        assert np.allclose(np.asarray(ch["A"].pixels), 0.5, atol=1e-4)
