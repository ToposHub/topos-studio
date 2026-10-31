"""L16 批2 节点（C05 mosaic / C08 crt / C09 glitch / C10 kaleidoscope）
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
    _LIB1_CRT_FRAGMENT,
    _LIB1_GLITCH_FRAGMENT,
    _LIB1_KALEIDOSCOPE_FRAGMENT,
    _LIB1_MOSAIC_FRAGMENT,
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

_L16B2_TYPES = ("lib1_mosaic", "lib1_crt", "lib1_glitch", "lib1_kaleidoscope")


class TestChainWiring:
    def test_registry_capability_library(self) -> None:
        for node_type in _L16B2_TYPES:
            assert node_type in NODE_REGISTRY
            cap = _NODE_CAPABILITY_TABLE[node_type]
            assert cap.effect_type == node_type and cap.fusible
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
        for frag in (_LIB1_MOSAIC_FRAGMENT, _LIB1_CRT_FRAGMENT,
                     _LIB1_GLITCH_FRAGMENT, _LIB1_KALEIDOSCOPE_FRAGMENT):
            prog = ctx.program(vertex_shader=_VERT, fragment_shader=frag)
            prog.release()

    def test_c05_mosaic_parity(self, ctx) -> None:
        from content_staging.library_v1.references import mosaic_ref
        rng = np.random.RandomState(45)
        image = rng.rand(16, 16, 3).astype(np.float32)
        for dither, u_dith in ((False, 0.0), (True, 1.0)):
            uniforms = {"u_block": 4.0, "u_levels": 6.0, "u_dither": u_dith}
            rendered = self._render(ctx, _LIB1_MOSAIC_FRAGMENT,
                                    self._rgba(image), uniforms)
            expected = mosaic_ref.evaluate(
                image, {"block_px": 4.0, "levels": 6.0, "dither": dither})
            # 合成器约定：纹理行 r = 回读行 r = numpy 行 r，字段与内容
            # 同坐标系（v·size），对拍无需翻转
            np.testing.assert_allclose(rendered[..., :3], expected, atol=1e-5)

    def test_c08_crt_full_mode_parity(self, ctx) -> None:
        from content_staging.library_v1.references import crt_ref
        rng = np.random.RandomState(48)
        image = rng.rand(16, 24, 3).astype(np.float32)
        uniforms = {"u_mode": 2, "u_scan_count": 8.0,
                    "u_scan_strength": 0.6, "u_mask_strength": 0.4}
        rendered = self._render(ctx, _LIB1_CRT_FRAGMENT,
                                self._rgba(image), uniforms)
        expected = crt_ref.evaluate(image, {"mode": "Full", "scan_count": 8.0,
                                            "scan_strength": 0.6,
                                            "mask_strength": 0.4})
        np.testing.assert_allclose(rendered[..., :3], expected, atol=1e-4)

    def test_c09_glitch_parity(self, ctx) -> None:
        from content_staging.library_v1.references import glitch_ref
        rng = np.random.RandomState(49)
        image = rng.rand(16, 24, 3).astype(np.float32)
        uniforms = {"u_band_height": 4.0, "u_shift_amount": 0.8,
                    "u_rgb_split": 0.5, "u_corruption": 1.0, "u_seed": 3.0}
        rendered = self._render(ctx, _LIB1_GLITCH_FRAGMENT,
                                self._rgba(image), uniforms)
        expected = glitch_ref.evaluate(image, {"band_height": 4.0,
                                               "shift_amount": 0.8,
                                               "rgb_split": 0.5,
                                               "corruption": 1.0, "seed": 3.0})
        np.testing.assert_allclose(rendered[..., :3], expected, atol=1e-5)

    def test_c10_kaleidoscope_parity(self, ctx) -> None:
        from content_staging.library_v1.references import kaleidoscope_ref
        xs = np.linspace(0.0, 1.0, 16, dtype=np.float32)
        ys = np.linspace(0.0, 1.0, 12, dtype=np.float32)
        image = (ys[:, None, None] * 0.6 + xs[None, :, None] * 0.4) \
            * np.ones((1, 1, 3), np.float32)
        # rotation=30°（非整扇区分数）：45°=半扇区恰为折叠场不动点，
        # 正反翻转碰巧全等——初版误用作定向证据，已换非对称配置
        uniforms = {"u_sectors": 4.0, "u_rotation": 30.0,
                    "u_center": (0.5, 0.5), "u_amount": 1.0}
        rendered = self._render(ctx, _LIB1_KALEIDOSCOPE_FRAGMENT,
                                self._rgba(image), uniforms)
        expected = kaleidoscope_ref.evaluate(
            image, {"sectors": 4, "rotation": 30.0, "center": (0.5, 0.5),
                    "amount": 1.0})
        np.testing.assert_allclose(rendered[..., :3], expected, atol=2e-3)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
