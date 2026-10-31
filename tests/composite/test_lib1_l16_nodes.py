"""L16 批1 单像素三节点（C02 rgb_matrix / C03 despill / C04 solarize）
生产集成测试：L04 全链接线（registry/capability/library/dispatch）+
真实 GL fragment 编译 + 像素对拍 oracle。"""

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
    _LIB1_DESPILL_FRAGMENT,
    _LIB1_RGB_MATRIX_FRAGMENT,
    _LIB1_SOLARIZE_FRAGMENT,
)
from src.features.composite.evaluators.shader_library import ShaderLibrary
from src.features.composite.nodes.lib1_nodes import (
    Lib1DespillNode, Lib1RgbMatrixNode, Lib1SolarizeNode,
)

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

_L16_TYPES = ("lib1_rgb_matrix", "lib1_despill", "lib1_solarize")


class TestChainWiring:
    def test_registry_and_capability(self) -> None:
        for node_type in _L16_TYPES:
            assert node_type in NODE_REGISTRY
            cap = _NODE_CAPABILITY_TABLE[node_type]
            assert cap.effect_type == node_type
            assert cap.tile_padding == 0 and cap.fusible
            assert cap.category == "Filter"

    def test_shader_library_registers(self) -> None:
        for name in _L16_TYPES:
            assert name in ShaderLibrary.STANDARD_NAMES

    def test_dispatch_branches_exist(self) -> None:
        import inspect
        from src.features.composite.evaluators import base_evaluator
        src = inspect.getsource(base_evaluator)
        for node_type in _L16_TYPES:
            assert f'effect_type == "{node_type}"' in src
        # 红线：未知 lib1_* marker 不允许透传
        assert 'effect_type.startswith("lib1_")' in src

    def test_node_classes_marker_protocol(self) -> None:
        for cls in (Lib1RgbMatrixNode, Lib1DespillNode, Lib1SolarizeNode):
            node = cls()
            assert node.node_type in _L16_TYPES
            out = node._do_evaluate({}, None, None)
            assert out == {}  # 无输入 → 空（不伪造）


class TestRealGl:
    """真实 GL：三 fragment 编译 + 像素对拍 oracle（单像素算子逐位）。"""

    @pytest.fixture()
    def ctx(self):
        try:
            context = moderngl.create_standalone_context(require=330)
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"standalone GL 不可用: {exc}")
        yield context
        context.release()

    def _render(self, ctx, fragment: str, textures: list[np.ndarray],
                uniforms: dict) -> np.ndarray:
        program = ctx.program(vertex_shader=_VERT, fragment_shader=fragment)
        h, w = textures[0].shape[0], textures[0].shape[1]
        texs = []
        for unit, arr in enumerate(textures):
            t = ctx.texture((w, h), 4, arr.tobytes(), dtype="f4")
            t.repeat_x = False
            t.repeat_y = False
            t.use(unit)
            texs.append(t)
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
        for t in texs:
            t.release()
        program.release()
        return np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)

    @staticmethod
    def _rgba(img: np.ndarray) -> np.ndarray:
        out = np.zeros((img.shape[0], img.shape[1], 4), dtype=np.float32)
        out[..., :3] = img
        out[..., 3] = img[..., 3] if img.shape[-1] == 4 else 1.0
        return out

    def test_fragments_compile(self, ctx) -> None:
        for frag in (_LIB1_RGB_MATRIX_FRAGMENT, _LIB1_DESPILL_FRAGMENT,
                     _LIB1_SOLARIZE_FRAGMENT):
            prog = ctx.program(vertex_shader=_VERT, fragment_shader=frag)
            prog.release()

    def test_c02_rgb_matrix_parity(self, ctx) -> None:
        from content_staging.library_v1.references import rgb_matrix_ref
        rng = np.random.RandomState(41)
        image = rng.rand(8, 12, 3).astype(np.float32) * 1.5 - 0.25
        params = {"row0_r": 1.2, "row0_g": 0.1, "row0_b": -0.1,
                  "row1_r": 0.05, "row1_g": 0.9, "row1_b": 0.2,
                  "row2_r": 0.0, "row2_g": 0.15, "row2_b": 1.1,
                  "bias": (0.03, -0.02, 0.05), "strength": 0.8}
        uniforms = {
            "u_m00": params["row0_r"], "u_m01": params["row0_g"],
            "u_m02": params["row0_b"], "u_m10": params["row1_r"],
            "u_m11": params["row1_g"], "u_m12": params["row1_b"],
            "u_m20": params["row2_r"], "u_m21": params["row2_g"],
            "u_m22": params["row2_b"], "u_bias": params["bias"],
            "u_strength": params["strength"],
        }
        rendered = self._render(ctx, _LIB1_RGB_MATRIX_FRAGMENT,
                                [self._rgba(image)], uniforms)
        expected = rgb_matrix_ref.evaluate(image, params)
        np.testing.assert_allclose(rendered[..., :3], expected, atol=1e-5)

    def test_c03_despill_both_modes_parity(self, ctx) -> None:
        from content_staging.library_v1.references import despill_ref
        rng = np.random.RandomState(43)
        image = (rng.rand(8, 12, 3).astype(np.float32)
                 * np.array([0.6, 1.4, 0.6], np.float32))
        for method, u_method in (("Subtract", 0), ("Scale", 1)):
            uniforms = {"u_method": u_method, "u_limit": 0.35, "u_strength": 0.9}
            rendered = self._render(ctx, _LIB1_DESPILL_FRAGMENT,
                                    [self._rgba(image)], uniforms)
            expected = despill_ref.evaluate(
                image, {"method": method, "limit": 0.35, "strength": 0.9})
            np.testing.assert_allclose(rendered[..., :3], expected, atol=1e-5)

    def test_c04_solarize_both_modes_parity(self, ctx) -> None:
        from content_staging.library_v1.references import solarize_ref
        rng = np.random.RandomState(44)
        image = rng.rand(8, 12, 3).astype(np.float32) * 2.0 - 0.3
        for mode, u_mode in (("Luma", 0), ("RGB", 1)):
            uniforms = {"u_threshold": 0.9, "u_softness": 0.25,
                        "u_mode": u_mode, "u_amount": 1.0}
            rendered = self._render(ctx, _LIB1_SOLARIZE_FRAGMENT,
                                    [self._rgba(image)], uniforms)
            expected = solarize_ref.evaluate(
                image, {"threshold": 0.9, "softness": 0.25,
                        "channel_mode": mode, "amount": 1.0})
            np.testing.assert_allclose(rendered[..., :3], expected, atol=1e-5)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
