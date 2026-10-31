"""L16 批3b 节点（C06 halftone / C07 hatching）生产集成测试。"""

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
    _LIB1_HALFTONE_FRAGMENT,
    _LIB1_HATCHING_FRAGMENT,
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
        for node_type in ("lib1_halftone", "lib1_hatching"):
            assert node_type in NODE_REGISTRY
            assert _NODE_CAPABILITY_TABLE[node_type].effect_type == node_type
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
        for frag in (_LIB1_HALFTONE_FRAGMENT, _LIB1_HATCHING_FRAGMENT):
            prog = ctx.program(vertex_shader=_VERT, fragment_shader=frag)
            prog.release()

    def test_c06_halftone_parity(self, ctx) -> None:
        """对拍收口（三轮）：①plate 源/网屏 q 基准按 oracle meshgrid 的
        **整数格**（yy=j 无像素中心偏移——q=j+0.5、源位=整数 texel）；
        ②plate 偏移符号（C 版 −off / Y 版 +off）与 rot 分量（sy=sa·qx+ca·qy、
        sx=ca·qx−sa·qy）；③CPU _bilinear 是 **clip(pos) 后 floor**（边界
        f=0 精确 texel），非钳索引保外推——手动双线性同口径后逐位闭合。"""
        from content_staging.library_v1.references import print_registration_ref
        rng = np.random.RandomState(66)
        image = rng.rand(12, 16, 3).astype(np.float32)
        uniforms = {"u_sep": 0, "u_offset": (1.0, 0.0),
                    "u_paper_base": 0.9, "u_ink": 0.8}
        rendered = self._render(ctx, _LIB1_HALFTONE_FRAGMENT,
                                self._rgba(image), uniforms)
        expected = print_registration_ref.evaluate(
            image, {"cmyk_separation": "CMY approx", "reg_offset_x": 1.0,
                    "reg_offset_y": 0.0, "paper_base": 0.9, "ink_amount": 0.8})
        np.testing.assert_allclose(rendered[..., :3], expected, atol=2e-3)

    def test_c07_hatching_parity(self, ctx) -> None:
        from content_staging.library_v1.references import hatching_ref
        xs = np.linspace(0.0, 1.0, 16, dtype=np.float32)
        ys = np.linspace(0.0, 1.0, 12, dtype=np.float32)
        image = (ys[:, None, None] * 0.7 + xs[None, :, None] * 0.3) \
            * np.ones((1, 1, 3), np.float32)
        uniforms = {"u_spacing": 6.0, "u_angle": 45.0, "u_layers": 2,
                    "u_ink_color": (0.05, 0.05, 0.08), "u_paper": 0.9,
                    "u_amount": 1.0}
        rendered = self._render(ctx, _LIB1_HATCHING_FRAGMENT,
                                self._rgba(image), uniforms)
        expected = hatching_ref.evaluate(
            image, {"line_spacing": 6.0, "angle": 45.0, "layers": "2",
                    "ink_color": (0.05, 0.05, 0.08), "paper": 0.9,
                    "amount": 1.0})
        np.testing.assert_allclose(rendered[..., :3], expected, atol=2e-3)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
