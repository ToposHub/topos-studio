"""L17 批3 多输入节点（C12 vector_displace / C13 stmap）生产集成测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.composite.core.node_capabilities import _NODE_CAPABILITY_TABLE
from src.features.composite.core.node_engine import NODE_REGISTRY
from src.features.composite.core.shaders import (
    _LIB1_STMAP_FRAGMENT,
    _LIB1_VECTOR_DISPLACE_FRAGMENT,
)
from src.features.composite.evaluators.shader_library import ShaderLibrary

pytestmark = pytest.mark.composite

_VERT = """
#version 330
in vec2 in_vert;
in vec2 in_uv;
out vec2 v_uv;
void main() {
    v_uv = in_uv;
    gl_Position = vec4(in_vert, 0.0, 1.0);
}
"""

_QUAD = np.array([-1, -1, 0, 0, 1, -1, 1, 0, -1, 1, 0, 1, 1, 1, 1, 1],
                 dtype="f4")


class TestChainWiring:
    def test_registry_capability_library(self) -> None:
        for node_type in ("lib1_vector_displace", "lib1_stmap"):
            assert node_type in NODE_REGISTRY
            assert _NODE_CAPABILITY_TABLE[node_type].effect_type == node_type
            assert node_type in ShaderLibrary.STANDARD_NAMES

    def test_c12_port_role_contract(self) -> None:
        from src.features.composite.nodes.lib1_nodes import Lib1VectorDisplaceNode
        node = Lib1VectorDisplaceNode()
        assert [p.name for p in node.inputs] == ["image", "vector", "mask"]
        # 缺 vector → 空（多输入必填端口不伪造）
        img = np.zeros((4, 4, 4), np.float32)
        from src.features.composite.nodes.base_node import TextureResult
        tex = TextureResult(texture_id=1, width=4, height=4)
        assert node._do_evaluate({"image": tex}, None, None) == {}


class TestRealGl:
    @pytest.fixture()
    def ctx(self):
        try:
            context = moderngl.create_standalone_context(require=330)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"standalone GL 不可用: {exc}")
        yield context
        context.release()

    def _render(self, ctx, fragment: str, textures: list, uniforms: dict,
                w: int, h: int) -> np.ndarray:
        # 生产约定：上传前 flipud（GL-natural），fbo.read 自底向上 →
        # 读回再 flipud 回 top-left CPU 行序。初版未翻上传曾以镜像
        # 共轭掩盖 y 向符号缺陷（C12 dy / C13 origin 对调同根）
        program = ctx.program(vertex_shader=_VERT, fragment_shader=fragment)
        texs = []
        for unit, arr in enumerate(textures):
            t = ctx.texture((w, h), 4,
                            np.ascontiguousarray(np.flipud(arr)).tobytes(),
                            dtype="f4")
            t.repeat_x = False
            t.repeat_y = False
            t.use(unit)
            texs.append(t)
        program["u_image_tex"].value = 0
        if "u_map_tex" in program:
            program["u_map_tex"].value = 1
        if "u_mask_tex" in program and len(textures) > 2:
            program["u_mask_tex"].value = 2
        for key, value in uniforms.items():
            if key in program:
                program[key].value = value
        target = ctx.texture((w, h), 4, dtype="f4")
        fbo = ctx.framebuffer(color_attachments=[target])
        vbo = ctx.buffer(_QUAD.tobytes())
        vao = ctx.vertex_array(program, vbo, "in_vert", "in_uv")
        fbo.use()
        vao.render(moderngl.TRIANGLE_STRIP)
        raw = fbo.read(components=4, dtype="f4")
        vao.release(); vbo.release(); target.release(); fbo.release()
        for t in texs:
            t.release()
        program.release()
        arr = np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)
        return np.flipud(arr).copy()

    def test_fragments_compile(self, ctx) -> None:
        for frag in (_LIB1_VECTOR_DISPLACE_FRAGMENT, _LIB1_STMAP_FRAGMENT):
            prog = ctx.program(vertex_shader=_VERT, fragment_shader=frag)
            prog.release()

    def test_c12_parity_pixels_units_with_mask(self, ctx) -> None:
        from content_staging.library_v1.references import vector_displace_ref
        rng = np.random.RandomState(91)
        h, w = 12, 16
        image = rng.rand(h, w, 3).astype(np.float32)
        vec = (rng.rand(h, w, 3).astype(np.float32) * 0.4 + 0.3)
        mask = (rng.rand(h, w, 1).astype(np.float32) > 0.4).astype(np.float32)
        mask_rgba = np.zeros((h, w, 4), np.float32)
        mask_rgba[..., 0] = mask[..., 0]
        mask_rgba[..., 3] = mask[..., 0]   # CPU 4 通道 mask 取 α
        params = {"scale": 6.0, "neutral": 0.5, "units": "Pixels"}
        uniforms = {"u_has_mask": 1.0, "u_scale": 6.0, "u_neutral": 0.5,
                    "u_units": 0}
        rendered = self._render(ctx, _LIB1_VECTOR_DISPLACE_FRAGMENT,
                                [self._rgba(image), self._rgba(vec), mask_rgba],
                                uniforms, w, h)[..., :3]
        expected = vector_displace_ref.evaluate(
            image, vec, params, mask=mask)
        np.testing.assert_allclose(rendered, expected, atol=2e-3)

    def test_c13_parity_identity_anchor(self, ctx) -> None:
        from content_staging.library_v1.references import stmap_ref
        rng = np.random.RandomState(93)
        h, w = 8, 12
        image = rng.rand(h, w, 3).astype(np.float32)
        # 恒等锚（BottomLeft → v 翻转）：重采样回原图
        uu, vv = np.meshgrid(np.linspace(0, 1, w), np.linspace(0, 1, h))
        map_rgba = np.zeros((h, w, 4), np.float32)
        map_rgba[..., 0] = uu
        map_rgba[..., 1] = 1.0 - vv
        params = {"channels": "RG", "origin": "BottomLeft",
                  "invalid": "Clamp", "filter": "Linear"}
        uniforms = {"u_channels": 0, "u_origin": 0, "u_invalid": 0,
                    "u_filter": 1}
        rendered = self._render(ctx, _LIB1_STMAP_FRAGMENT,
                                [self._rgba(image), map_rgba],
                                uniforms, w, h)[..., :3]
        expected = stmap_ref.evaluate(image, map_rgba[..., :2], params)
        np.testing.assert_allclose(rendered, expected, atol=2e-3)

    @staticmethod
    def _rgba(img: np.ndarray) -> np.ndarray:
        out = np.zeros((img.shape[0], img.shape[1], 4), dtype=np.float32)
        out[..., :3] = img
        out[..., 3] = img[..., 3] if img.shape[-1] == 4 else 1.0
        return out


class TestDispatcherLevel:
    @pytest.fixture()
    def ctx(self):
        try:
            context = moderngl.create_standalone_context(require=330)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"standalone GL 不可用: {exc}")
        yield context
        context.release()

    """dispatcher 级测试：经 _apply_lib1_vector_displace 全链（含绑定契约）。

    教训留痕：fragment 级测试绑定直连绕过了 dispatcher——初版把
    TextureResult 对象传给 _bind_external_texture（生产契约是 int
    texture_id），fragment 测试全绿而 dispatcher 实际是坏的。本类钉住。
    """

    def test_c12_dispatch_full_chain(self, ctx) -> None:
        from content_staging.library_v1.references import vector_displace_ref
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )
        from src.features.composite.evaluators.color_evaluator import (
            ColorEvaluatorMixin,
        )
        from src.features.composite.evaluators.filter_evaluator import (
            FilterEvaluatorMixin,
        )
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.render.gpu_resource_pool import (
            upload_pooled_texture,
        )
        from src.shared.gpu.gl_backend import FramebufferManager

        # 生产合成类同构：_set_uniform_if_present 在 ColorEvaluatorMixin
        class _Combined(BaseEvaluatorMixin, ColorEvaluatorMixin,
                        FilterEvaluatorMixin):
            pass

        class _Pool:
            def __init__(self, ctx):
                self.ctx = ctx
                self._pooled_textures = {}

            def acquire_texture(self, w, h, components=4, dtype="f4"):
                tex = ctx.texture((w, h), components, dtype=dtype)
                self._pooled_textures.setdefault(dtype, []).append(tex)
                return tex

        rng = np.random.RandomState(95)
        h, w = 10, 14
        image = rng.rand(h, w, 3).astype(np.float32)
        vec = rng.rand(h, w, 3).astype(np.float32) * 0.4 + 0.3
        harness = _Combined()
        harness.ctx = ctx
        harness.fb_manager = FramebufferManager(ctx)
        pool = _Pool(ctx)
        harness._resource_pool = pool
        harness._eval_ctx = None
        harness.utility_shaders = {}
        harness._load_shaders()   # 生产同一初始化（ShaderLibrary 全量编译）

        img_rgba = np.ones((h, w, 4), dtype=np.float32)
        img_rgba[..., :3] = image
        img_tex = TextureResult(
            texture_id=upload_pooled_texture(
                pool, np.flipud(img_rgba), w, h).glo,
            width=w, height=h)
        # 向量图 RGBA 化：CPU 只读 RG（B/A 防御填充不参与）
        vec_rgba = np.zeros((h, w, 4), dtype=np.float32)
        vec_rgba[..., 0] = vec[..., 0]
        vec_rgba[..., 1] = vec[..., 1]
        vec_rgba[..., 3] = 1.0
        vec_tex = TextureResult(
            texture_id=upload_pooled_texture(
                pool, np.flipud(vec_rgba), w, h).glo,
            width=w, height=h)
        outputs = {
            "_effect_type": "lib1_vector_displace",
            "_effect_input": img_tex,
            "_effect_input_map": vec_tex,
            "_effect_params": {"scale": 5.0, "neutral": 0.5, "units": "Pixels"},
        }
        result = harness._dispatch_effect("lib1_vector_displace", outputs)
        got = result["result"]
        assert got is not img_tex   # dispatcher 必须产出新纹理（绑定契约生效）
        # _readback_texture_fullres 已是 top-left origin——不得再翻（初版
        # 双重 flipud 即"采样错位"表象的根因）
        back = harness._readback_texture_fullres(got, want_bgr=False)
        expected = vector_displace_ref.evaluate(
            image, vec, {"scale": 5.0, "neutral": 0.5, "units": "Pixels"})
        np.testing.assert_allclose(back, expected, atol=2e-3)

    def test_c13_dispatch_full_chain(self, ctx) -> None:
        """C13 dispatcher 级对拍：钉住 u_map_tex 采样器绑定（初版漏设
        使 map 采样落到 unit0=图像纹理——fragment 级测试测不出的契约层
        缺陷，与 C12 同族）与 GL-natural origin 口径。"""
        from content_staging.library_v1.references import stmap_ref
        from src.features.composite.evaluators.base_evaluator import (
            BaseEvaluatorMixin,
        )
        from src.features.composite.evaluators.color_evaluator import (
            ColorEvaluatorMixin,
        )
        from src.features.composite.evaluators.filter_evaluator import (
            FilterEvaluatorMixin,
        )
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.render.gpu_resource_pool import (
            upload_pooled_texture,
        )
        from src.shared.gpu.gl_backend import FramebufferManager

        class _Combined(BaseEvaluatorMixin, ColorEvaluatorMixin,
                        FilterEvaluatorMixin):
            pass

        class _Pool:
            def __init__(self, ctx):
                self.ctx = ctx
                self._pooled_textures = {}

            def acquire_texture(self, w, h, components=4, dtype="f4"):
                tex = ctx.texture((w, h), components, dtype=dtype)
                self._pooled_textures.setdefault(dtype, []).append(tex)
                return tex

        rng = np.random.RandomState(96)
        h, w = 10, 14
        image = rng.rand(h, w, 3).astype(np.float32)
        uu, vv = np.meshgrid(np.linspace(0, 1, w), np.linspace(0, 1, h))
        # 非恒等 map（BottomLeft：v=0=图像底部；翻转场避免对称掩盖）
        map2 = np.zeros((h, w, 2), dtype=np.float32)
        map2[..., 0] = uu
        map2[..., 1] = np.clip(1.0 - vv + 0.25 * np.sin(3.0 * uu), 0.0, 1.0)
        harness = _Combined()
        harness.ctx = ctx
        harness.fb_manager = FramebufferManager(ctx)
        pool = _Pool(ctx)
        harness._resource_pool = pool
        harness._eval_ctx = None
        harness.utility_shaders = {}
        harness._load_shaders()

        img_rgba = np.ones((h, w, 4), dtype=np.float32)
        img_rgba[..., :3] = image
        img_tex = TextureResult(
            texture_id=upload_pooled_texture(
                pool, np.flipud(img_rgba), w, h).glo,
            width=w, height=h)
        map_rgba = np.zeros((h, w, 4), dtype=np.float32)
        map_rgba[..., :2] = map2
        map_rgba[..., 3] = 1.0
        map_tex = TextureResult(
            texture_id=upload_pooled_texture(
                pool, np.flipud(map_rgba), w, h).glo,
            width=w, height=h)
        outputs = {
            "_effect_type": "lib1_stmap",
            "_effect_input": img_tex,
            "_effect_input_map": map_tex,
            "_effect_params": {"channels": "RG", "origin": "BottomLeft",
                               "invalid": "Clamp", "filter": "Linear"},
        }
        result = harness._dispatch_effect("lib1_stmap", outputs)
        got = result["result"]
        assert got is not img_tex
        back = harness._readback_texture_fullres(got, want_bgr=False)
        expected = stmap_ref.evaluate(
            image, map2, {"channels": "RG", "origin": "BottomLeft",
                          "invalid": "Clamp", "filter": "Linear"})
        np.testing.assert_allclose(back[..., :3], expected, atol=2e-3)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
