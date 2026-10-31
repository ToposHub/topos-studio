"""L16 批3a 节点（C15 median / C16 convolution / C20 halation）
生产集成测试：L04 全链接线 + 真实 GL 编译 + 像素对拍 oracle。"""

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
    _LIB1_CONVOLUTION_FRAGMENT,
    _LIB1_HALATION_FRAGMENT,
    _LIB1_MEDIAN_FRAGMENT,
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
        for node_type in ("lib1_median", "lib1_convolution", "lib1_halation"):
            assert node_type in NODE_REGISTRY
            cap = _NODE_CAPABILITY_TABLE[node_type]
            assert cap.effect_type == node_type
            assert node_type in ShaderLibrary.STANDARD_NAMES


class TestRealGl:
    @pytest.fixture()
    def ctx(self):
        try:
            context = moderngl.create_standalone_context(require=330)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"standalone GL 不可用: {exc}")
        yield context
        context.release()

    def _render(self, ctx, fragment: str, image: np.ndarray,
                uniforms: dict) -> np.ndarray:
        program = ctx.program(vertex_shader=_VERT, fragment_shader=fragment)
        h, w = image.shape[0], image.shape[1]
        t = ctx.texture((w, h), 4, image.tobytes(), dtype="f4")
        t.repeat_x = False
        t.repeat_y = False
        t.use(0)
        program["u_texture"].value = 0
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
        t.release()
        program.release()
        return np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)

    @staticmethod
    def _rgba(img: np.ndarray) -> np.ndarray:
        out = np.zeros((img.shape[0], img.shape[1], 4), dtype=np.float32)
        out[..., :3] = img
        out[..., 3] = 1.0
        return out

    def test_fragments_compile(self, ctx) -> None:
        for frag in (_LIB1_MEDIAN_FRAGMENT, _LIB1_CONVOLUTION_FRAGMENT,
                     _LIB1_HALATION_FRAGMENT):
            prog = ctx.program(vertex_shader=_VERT, fragment_shader=frag)
            prog.release()

    def test_c15_median_parity(self, ctx) -> None:
        from content_staging.library_v1.references import median_rank_ref
        rng = np.random.RandomState(51)
        image = rng.rand(12, 16, 3).astype(np.float32)
        image[6, 8] = 10.0  # 离群点
        for mode, u_mode in (("RGB", 0), ("Luma", 1)):
            uniforms = {"u_radius": 1, "u_percentile": 0.5,
                        "u_mode": u_mode, "u_amount": 1.0}
            rendered = self._render(ctx, _LIB1_MEDIAN_FRAGMENT,
                                    self._rgba(image), uniforms)
            expected = median_rank_ref.evaluate(
                image, {"kernel_size": "3x3", "percentile": 0.5,
                        "channel_mode": mode, "amount": 1.0})
            np.testing.assert_allclose(rendered[..., :3], expected, atol=1e-5)

    def test_c16_convolution_parity(self, ctx) -> None:
        from content_staging.library_v1.references import convolution_ref
        rng = np.random.RandomState(56)
        image = rng.rand(10, 14, 3).astype(np.float32)
        params = {"k00": 0.5, "k01": 0.25, "k11": 0.5, "k22": -0.25,
                  "normalize": True, "bias": 0.05}
        uniforms = {"u_k00": 0.5, "u_k01": 0.25, "u_k02": 0.0,
                    "u_k10": 0.0, "u_k11": 0.5, "u_k12": 0.0,
                    "u_k20": 0.0, "u_k21": 0.0, "u_k22": -0.25,
                    "u_normalize": 1.0, "u_bias": 0.05}
        rendered = self._render(ctx, _LIB1_CONVOLUTION_FRAGMENT,
                                self._rgba(image), uniforms)
        expected = convolution_ref.evaluate(image, params)
        np.testing.assert_allclose(rendered[..., :3], expected, atol=1e-5)

    def test_c20_halation_parity(self, ctx) -> None:
        from content_staging.library_v1.references import (
            multiscale_halation_ref as f06,
        )
        rng = np.random.RandomState(60)
        image = rng.rand(12, 16, 3).astype(np.float32) * 1.4
        image[5, 8] = 3.0  # 高光点
        uniforms = {"u_strength": 0.7, "u_radius": 0.6,
                    "u_red_scatter": 0.5, "u_threshold": 0.55}
        rendered = self._render(ctx, _LIB1_HALATION_FRAGMENT,
                                self._rgba(image), uniforms)
        expected = f06.evaluate_degraded_single_scale(
            image, {"strength": 0.7, "radius": 0.6, "red_scatter": 0.5,
                    "threshold": 0.55})
        np.testing.assert_allclose(rendered[..., :3], expected, atol=2e-3)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
