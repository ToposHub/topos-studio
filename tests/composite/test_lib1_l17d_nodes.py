"""L17 批3b 多输出节点（C21 frequency_separation）生产集成测试。"""

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
    _LIB1_FREQ_BLUR_FRAGMENT,
    _LIB1_FREQ_HIGH_FRAGMENT,
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
    def test_registry_capability_multi_output(self) -> None:
        assert "lib1_frequency_separation" in NODE_REGISTRY
        cap = _NODE_CAPABILITY_TABLE["lib1_frequency_separation"]
        assert cap.effect_type == "lib1_frequency_separation"
        assert cap.fusible is False   # 双输出不参与融合
        assert "lib1_freq_blur" in ShaderLibrary.STANDARD_NAMES
        assert "lib1_freq_high" in ShaderLibrary.STANDARD_NAMES

    def test_dual_output_marker(self) -> None:
        from src.features.composite.nodes.base_node import TextureResult
        from src.features.composite.nodes.lib1_nodes import (
            Lib1FrequencySeparationNode,
        )
        node = Lib1FrequencySeparationNode()
        assert [p.name for p in node.outputs] == ["low", "high"]
        tex = TextureResult(texture_id=1, width=4, height=4)
        out = node._do_evaluate({"image": tex}, None, None)
        assert "low" in out and "high" in out   # 双占位 marker
        assert out["_effect_type"] == "lib1_frequency_separation"


class TestRealGl:
    @pytest.fixture()
    def ctx(self):
        try:
            context = moderngl.create_standalone_context(require=330)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"standalone GL 不可用: {exc}")
        yield context
        context.release()

    def _run_pipeline(self, ctx, image: np.ndarray, radius: float,
                      filt: int) -> tuple[np.ndarray, np.ndarray]:
        h, w = image.shape[0], image.shape[1]
        src = ctx.texture((w, h), 4, image.tobytes(), dtype="f4")
        src.repeat_x = False
        src.repeat_y = False
        tmp = ctx.texture((w, h), 4, dtype="f4")
        low = ctx.texture((w, h), 4, dtype="f4")
        high = ctx.texture((w, h), 4, dtype="f4")
        prog_blur = ctx.program(vertex_shader=_VERT,
                                fragment_shader=_LIB1_FREQ_BLUR_FRAGMENT)
        prog_high = ctx.program(vertex_shader=_VERT,
                                fragment_shader=_LIB1_FREQ_HIGH_FRAGMENT)
        vbo = ctx.buffer(_QUAD.tobytes())
        vao = ctx.vertex_array(prog_blur, vbo, "in_vert", "in_uv")
        tgt = ctx.framebuffer(color_attachments=[tmp])
        tgt.use()
        ctx.viewport = (0, 0, w, h)
        src.use(0)
        prog_blur["u_texture"].value = 0
        prog_blur["u_radius"].value = radius
        prog_blur["u_filter"].value = filt
        prog_blur["u_direction"].value = 0
        vao.render(moderngl.TRIANGLE_STRIP)
        tgt2 = ctx.framebuffer(color_attachments=[low])
        tgt2.use()
        ctx.viewport = (0, 0, w, h)
        tmp.use(0)
        prog_blur["u_texture"].value = 0
        prog_blur["u_direction"].value = 1
        vao.render(moderngl.TRIANGLE_STRIP)
        vao_high = ctx.vertex_array(prog_high, vbo, "in_vert", "in_uv")
        tgt3 = ctx.framebuffer(color_attachments=[high])
        tgt3.use()
        ctx.viewport = (0, 0, w, h)
        src.use(0)
        prog_high["u_image_tex"].value = 0
        low.use(1)
        prog_high["u_low_tex"].value = 1
        vao_high.render(moderngl.TRIANGLE_STRIP)
        low_data = np.frombuffer(low.read(),
                                 dtype=np.float32).reshape(h, w, 4)
        high_data = np.frombuffer(high.read(),
                                  dtype=np.float32).reshape(h, w, 4)
        for t in (src, tmp, low, high):
            t.release()
        prog_blur.release(); prog_high.release()
        vao.release(); vao_high.release(); vbo.release()
        tgt.release(); tgt2.release(); tgt3.release()
        return low_data, high_data

    def test_fragments_compile(self, ctx) -> None:
        for frag in (_LIB1_FREQ_BLUR_FRAGMENT, _LIB1_FREQ_HIGH_FRAGMENT):
            prog = ctx.program(vertex_shader=_VERT, fragment_shader=frag)
            prog.release()

    @pytest.mark.parametrize("filt,filt_i", (("Box", 0), ("Gaussian", 1)))
    def test_c21_parity(self, ctx, filt, filt_i) -> None:
        from content_staging.library_v1.references import (
            frequency_separation_ref,
        )
        rng = np.random.RandomState(97)
        h, w = 12, 16
        rgba = rng.rand(h, w, 4).astype(np.float32)
        low_g, high_g = self._run_pipeline(ctx, rgba, 3.0, filt_i)
        result = frequency_separation_ref.evaluate(
            rgba, {"radius": 3.0, "filter": filt, "mode": "LowHigh"})
        low_c, high_c = result["low"], result["high"]
        # C21 场用直接行坐标（v·size，无 y_down）→ 纹理行=回读行=numpy 行，
        # 对拍无需翻转（与 L16 批2 坐标系纪律一致；转场家族的 flipud 只
        # 适用于其 y_down 屏幕坐标约定）
        np.testing.assert_allclose(low_g[..., :3], low_c[..., :3], atol=2e-3)
        np.testing.assert_allclose(high_g[..., :3], high_c[..., :3], atol=2e-3)
        # α 契约：low 保留原 α、high α=0
        np.testing.assert_allclose(low_g[..., 3], rgba[..., 3], atol=1e-6)
        assert float(np.abs(high_g[..., 3]).max()) == 0.0

    def test_c21_reconstruct_roundtrip(self, ctx) -> None:
        """声明域重建：low+high = 输入（GL 双输出相加逐位回归）。"""
        rng = np.random.RandomState(99)
        h, w = 8, 12
        rgba = rng.rand(h, w, 4).astype(np.float32)
        low_g, high_g = self._run_pipeline(ctx, rgba, 2.0, 0)
        recon = low_g[..., :3] + high_g[..., :3]
        np.testing.assert_allclose(recon, rgba[..., :3], atol=1e-5)


class TestDispatcherLevel:
    """dispatcher 级 C21：经生产 _apply_lib1_frequency_separation 全链
    （双 pass 绑定 + u_low_tex 采样器 + 双输出端口）。

    教训（C12 同族）：fragment 级测试自设 uniform 测不出分派器把采样器
    名写错（u_low_unit 不存在被静默跳过 → high ≡ image−image ≡ 0）。
    """

    @pytest.fixture()
    def ctx(self):
        try:
            context = moderngl.create_standalone_context(require=330)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"standalone GL 不可用: {exc}")
        yield context
        context.release()

    @pytest.mark.parametrize("filt", ("Box", "Gaussian"))
    def test_c21_dispatch_full_chain(self, ctx, filt) -> None:
        from content_staging.library_v1.references import (
            frequency_separation_ref,
        )
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

        rng = np.random.RandomState(98)
        h, w = 10, 14
        rgba = rng.rand(h, w, 4).astype(np.float32)
        harness = _Combined()
        harness.ctx = ctx
        harness.fb_manager = FramebufferManager(ctx)
        pool = _Pool(ctx)
        harness._resource_pool = pool
        harness._eval_ctx = None
        harness.utility_shaders = {}
        harness._load_shaders()
        harness._get_texture_by_id = lambda tid: next(
            (t for lst in pool._pooled_textures.values() for t in lst
             if t.glo == int(tid)), None)

        src = TextureResult(
            texture_id=upload_pooled_texture(
                pool, np.flipud(rgba), w, h).glo,
            width=w, height=h)
        outputs = {"_effect_type": "lib1_frequency_separation",
                   "_effect_input": src,
                   "_effect_params": {"radius": 3.0, "filter": filt,
                                      "mode": "LowHigh"}}
        result = harness._dispatch_effect("lib1_frequency_separation",
                                          outputs)
        assert set(result.keys()) >= {"low", "high"}
        low_g = harness._readback_texture_fullres(result["low"],
                                                  want_alpha=True)
        high_g = harness._readback_texture_fullres(result["high"],
                                                   want_alpha=True)
        ref = frequency_separation_ref.evaluate(
            rgba, {"radius": 3.0, "filter": filt, "mode": "LowHigh"})
        np.testing.assert_allclose(low_g[..., :3], ref["low"][..., :3],
                                   atol=2e-3)
        np.testing.assert_allclose(high_g[..., :3], ref["high"][..., :3],
                                   atol=2e-3)
        # α 契约：low 保留原 α、high α=0
        np.testing.assert_allclose(low_g[..., 3], rgba[..., 3], atol=1e-6)
        assert float(np.abs(high_g[..., 3]).max()) == 0.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
