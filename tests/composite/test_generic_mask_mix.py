"""CP-044 / CP-045 回归测试：通用 mask 输入 + 通用 mix 混合。

覆盖三层：
1. 端口注入 —— 声明了输入端口且无 mask 端点的效果节点自动追加通用
   mask 端口；路由/组织节点（Output/Dot/NoOp/Switch/Group/Precomp/
   占位）opt-out；自带 mask 的 12 个旧节点恰好保留一个 mask。
2. marker 协议 —— Node.evaluate 在 mask 输入有效时挂 _generic_mask
   （不与旧节点使用的 _effect_mask 冲突）。
3. GPU 后处理 —— _apply_generic_mask_mix 单 pass：mask 门控、invert、
   通道选择、mix 混合；无 mask 时仅 mix 起作用；自带 mix 参数的节点
   （blur）不被双重混合。
"""

import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import (
    NODE_REGISTRY, NODE_CATEGORIES,
)
from src.features.composite.core.format import BBox
from src.features.composite.nodes.base_node import TextureResult

pytestmark = pytest.mark.composite_core

_W, _H = 16, 8


# ── 1. Port injection ───────────────────────────────────────────────────────

class TestGenericMaskPortInjection:
    _OPT_OUT = {
        "output", "file_output", "dot", "noop", "switch", "group",
        "precomp", "ai_generate", "ai_expand", "ai_denoise",
        "unknown_placeholder",
        # ai_inpaint declares its own legacy mask port (placeholder family)
    }

    def test_effect_nodes_gain_mask_port(self):
        from src.features.composite.nodes.blur_node import BlurNode
        from src.features.composite.nodes.glow_node import GlowNode
        from src.features.composite.nodes.transform_node import TransformNode
        from src.features.composite.nodes.edge_detect_node import EdgeDetectNode
        for cls in (BlurNode, GlowNode, TransformNode, EdgeDetectNode):
            names = [p.name for p in cls(node_id="t").inputs]
            assert "mask" in names, f"{cls.__name__} missing generic mask port"

    def test_opt_out_nodes_have_no_mask_port(self):
        for node_type in self._OPT_OUT:
            cls = NODE_REGISTRY.get(node_type)
            if cls is None:
                continue
            names = [p.name for p in cls(node_id="t").inputs]
            assert "mask" not in names, f"{node_type} should opt out"

    def test_legacy_mask_nodes_keep_single_mask_port(self):
        from src.features.composite.nodes.blend_node import MergeNode
        names = [p.name for p in MergeNode(node_id="t").inputs]
        assert names.count("mask") == 1

    def test_sources_without_inputs_get_nothing(self):
        from src.features.composite.nodes.constant_node import ConstantNode
        names = [p.name for p in ConstantNode(node_id="t").inputs]
        assert names == []

    def test_registry_wide_injection_count(self):
        """效果类节点（有输入、未 opt-out、无自带 mask）必须全部拿到通用端口。"""
        injected = opted = legacy = 0
        for node_type, cls in NODE_REGISTRY.items():
            if cls is None or node_type in self._OPT_OUT:
                continue
            node = cls(node_id="t")
            names = [p.name for p in node.inputs]
            if not names:
                continue
            if node._generic_mask_port:
                injected += 1
                # 通用参数就位
                pdefs = node.get_param_defs()
                assert {"mask_channel", "mask_invert", "mix"} <= set(pdefs)
            elif "mask" in names:
                legacy += 1
            else:
                opted += 1
        # 全部效果节点都应被三类之一覆盖；注入数应显著大于 12 个旧节点
        assert injected >= 40, f"expected broad injection, got {injected}"
        assert legacy >= 10

    def test_roundtrip_keeps_mask_params(self):
        from src.features.composite.nodes.blur_node import BlurNode
        node = BlurNode(node_id="rt")
        data = node.to_dict()
        restored = BlurNode.from_dict(data)
        assert "mask_channel" in restored.parameters
        assert [p.name for p in restored.inputs].count("mask") == 1


# ── 2. Marker protocol ──────────────────────────────────────────────────────

def _tex_ok():
    return TextureResult(texture_id=1, width=_W, height=_H,
                         bbox=BBox.full_frame(_W, _H),
                         alpha_premultiplied=True, color_space="linear")


class TestGenericMaskMarker:
    def test_evaluate_attaches_generic_mask_marker(self):
        from src.features.composite.nodes.sharpen_node import SharpenNode
        node = SharpenNode(node_id="m")
        out = node.evaluate({"image": _tex_ok(), "mask": _tex_ok()},
                            SimpleNamespace(frame_number=0), None)
        assert out.get("_generic_mask") is not None
        assert "_effect_mask" not in out

    def test_evaluate_no_marker_without_mask(self):
        from src.features.composite.nodes.sharpen_node import SharpenNode
        node = SharpenNode(node_id="m2")
        out = node.evaluate({"image": _tex_ok()},
                            SimpleNamespace(frame_number=0), None)
        assert "_generic_mask" not in out


# ── 3. GPU post-pass ────────────────────────────────────────────────────────

class TestGenericMaskMixGPUPass:
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

        class _Pool:
            def __init__(self, ctx):
                self.ctx = ctx
                self._pooled_textures = {}

            def acquire_texture(self, w, h, components=4, dtype="f4"):
                tex = self.ctx.texture((w, h), components, dtype=dtype)
                self._pooled_textures.setdefault(dtype, []).append(tex)
                return tex

        class _H(BaseEvaluatorMixin, FilterEvaluatorMixin):
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

    @staticmethod
    def _ns(node_type, **params):
        return SimpleNamespace(node_type=node_type, parameters=params)

    def _effect(self, harness, value):
        """A 'result' texture filled with a constant value."""
        return self._upload(
            harness, np.full((_H, _W, 4), value, dtype=np.float32))

    def test_mask_gates_effect(self, harness, qapp):
        harness
        eff = self._effect(harness, 1.0)
        base = self._effect(harness, 0.25)
        # 左半 mask=1，右半 mask=0
        mask = np.zeros((_H, _W, 4), dtype=np.float32)
        mask[:, :_W // 2] = 1.0
        mask_tex = self._upload(harness, mask)
        out = harness._apply_generic_mask_mix(
            self._ns("sharpen", mask_channel="luma", mix=1.0),
            {"_generic_mask": mask_tex, "_effect_input": base},
            {"result": eff})
        got = self._read(harness, out["result"])
        assert np.allclose(got[:, :_W // 2], 1.0, atol=1e-4)   # mask=1 → effect
        assert np.allclose(got[:, _W // 2:], 0.25, atol=1e-4)  # mask=0 → base

    def test_mask_invert(self, harness, qapp):
        harness
        eff = self._effect(harness, 1.0)
        base = self._effect(harness, 0.25)
        mask = np.zeros((_H, _W, 4), dtype=np.float32)
        mask[:, :_W // 2] = 1.0
        mask_tex = self._upload(harness, mask)
        out = harness._apply_generic_mask_mix(
            self._ns("sharpen", mask_channel="luma", mask_invert=True,
                     mix=1.0),
            {"_generic_mask": mask_tex, "_effect_input": base},
            {"result": eff})
        got = self._read(harness, out["result"])
        assert np.allclose(got[:, :_W // 2], 0.25, atol=1e-4)
        assert np.allclose(got[:, _W // 2:], 1.0, atol=1e-4)

    def test_mask_channel_alpha(self, harness, qapp):
        harness
        eff = self._effect(harness, 1.0)
        base = self._effect(harness, 0.25)
        mask = np.zeros((_H, _W, 4), dtype=np.float32)
        mask[:, :, 3] = 1.0          # alpha=1 处处
        mask[:, :_W // 2, 0] = 0.0   # luma 应为 0（左）
        mask[:, _W // 2:, 0] = 1.0   # luma=1（右）
        mask_tex = self._upload(harness, mask)
        out = harness._apply_generic_mask_mix(
            self._ns("sharpen", mask_channel="a", mix=1.0),
            {"_generic_mask": mask_tex, "_effect_input": base},
            {"result": eff})
        got = self._read(harness, out["result"])
        assert np.allclose(got, 1.0, atol=1e-4)  # alpha 通道恒 1 → 全 effect

    def test_mix_only_blends_without_mask(self, harness, qapp):
        harness
        eff = self._effect(harness, 1.0)
        base = self._effect(harness, 0.0)
        out = harness._apply_generic_mask_mix(
            self._ns("sharpen", mix=0.5),
            {"_effect_input": base},
            {"result": eff})
        got = self._read(harness, out["result"])
        assert np.allclose(got, 0.5, atol=1e-4)

    def test_own_mix_nodes_skip_generic_blend(self, harness, qapp):
        harness
        from src.features.composite.nodes.blur_node import BlurNode
        assert any(p.name == "mix" for p in BlurNode.PARAM_DEFS)
        eff = self._effect(harness, 1.0)
        base = self._effect(harness, 0.0)
        out = harness._apply_generic_mask_mix(
            self._ns("blur", mix=0.5),
            {"_effect_input": base},
            {"result": eff})
        # blur 自带 mix → 通用 pass 不动结果
        assert out["result"] is eff

    def test_noop_when_mix_one_and_no_mask(self, harness, qapp):
        harness
        eff = self._effect(harness, 1.0)
        out = harness._apply_generic_mask_mix(
            self._ns("sharpen", mix=1.0), {}, {"result": eff})
        assert out["result"] is eff


# ── 4. Fusion guard ─────────────────────────────────────────────────────────

class TestFusionRespectsGenericMix:
    def test_generic_mix_defuses(self):
        from src.features.composite.utils.pass_fusion import _params_fusable
        assert _params_fusable("shuffle", {"mix": 0.5}) is False
        assert _params_fusable("shuffle", {"mix": 1.0}) is True
        assert _params_fusable("shuffle", {}) is True
