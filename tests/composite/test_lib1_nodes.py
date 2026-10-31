"""LIB1 节点接缝测试（计划 L / L04：C01 gradient_map + C17 edge_extend）。

覆盖：registry/categories/capability 一致性、节点 marker 协议、
shader_library 注册、dispatch 分支、未知 lib1_* marker 精确拒绝（红线）、
真实 GL 像素对拍（C01 vs F16 oracle / C17 乒乓链 vs edge_extend oracle）。
完整 evaluator 消费链（GraphEvaluatorMixin→CompositeExportRenderer）属
L20 端到端验收。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytestmark = pytest.mark.composite

moderngl = pytest.importorskip("moderngl")

from content_staging.library_v1.references import edge_extend_ref, gradient_map_ref
from src.features.composite.core.node_capabilities import (
    check_registry_consistency, get_node_capability,
)
from src.features.composite.core.node_engine import NODE_CATEGORIES, NODE_REGISTRY
from src.features.composite.core.shaders import (
    _LIB1_EDGE_EXTEND_DILATE_FRAGMENT, _LIB1_EDGE_EXTEND_OUTPUT_FRAGMENT,
    _LIB1_GRADIENT_MAP_FRAGMENT,
)
from src.features.composite.evaluators.shader_library import ShaderLibrary
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.nodes.lib1_nodes import (
    Lib1EdgeExtendNode, Lib1GradientMapNode,
)

_VERT = """
#version 330
in vec2 in_vert;
in vec2 in_uv;
out vec2 v_uv;
void main() { gl_Position = vec4(in_vert, 0.0, 1.0); v_uv = in_uv; }
"""
_QUAD = np.array([-1, -1, 0, 0, 1, -1, 1, 0, -1, 1, 0, 1, 1, 1, 1, 1], dtype="f4")


class TestRegistryWiring:
    def test_registry_categories_capability_consistent(self) -> None:
        assert NODE_REGISTRY.get("lib1_gradient_map") is Lib1GradientMapNode
        assert NODE_REGISTRY.get("lib1_edge_extend") is Lib1EdgeExtendNode
        filter_types = [t for t, _ in NODE_CATEGORIES["Filter"]]
        assert "lib1_gradient_map" in filter_types
        assert "lib1_edge_extend" in filter_types
        issues = check_registry_consistency()
        assert all(not v for v in issues.values()), issues

    def test_capabilities(self) -> None:
        c01 = get_node_capability("lib1_gradient_map")
        assert c01.effect_type == "lib1_gradient_map"
        assert c01.tile_padding == 0 and c01.fusible is True
        c17 = get_node_capability("lib1_edge_extend")
        assert c17.effect_type == "lib1_edge_extend"
        assert c17.tile_padding == 8 and c17.fusible is False

    def test_shader_library_registers_lib1(self) -> None:
        for name in ("lib1_gradient_map", "lib1_edge_extend_dilate",
                     "lib1_edge_extend_output"):
            assert name in ShaderLibrary.STANDARD_NAMES
            assert name in ShaderLibrary._SHADER_SOURCES if hasattr(
                ShaderLibrary, "_SHADER_SOURCES") else True

    def test_dispatch_branches_exist_and_unknown_rejected(self) -> None:
        import inspect

        from src.features.composite.evaluators import base_evaluator
        src = inspect.getsource(base_evaluator)
        assert 'effect_type == "lib1_gradient_map"' in src
        assert 'effect_type == "lib1_edge_extend"' in src
        # 红线：未知 lib1_* marker 不允许透传
        assert 'effect_type.startswith("lib1_")' in src
        from src.features.composite.evaluators.filter_evaluator import (
            FilterEvaluatorMixin,
        )
        fsrc = inspect.getsource(FilterEvaluatorMixin)
        assert "_apply_lib1_gradient_map" in fsrc
        assert "_apply_lib1_edge_extend" in fsrc


class TestNodeMarkers:
    def test_c01_marker(self) -> None:
        node = Lib1GradientMapNode()
        tex = TextureResult(texture_id=3, width=4, height=4)
        out = node._do_evaluate({"image": tex}, None, None)
        assert out["_effect_type"] == "lib1_gradient_map"
        assert out["_effect_input"] is tex
        assert out["_effect_params"]["amount"] == 1.0

    def test_c17_marker_and_padding(self) -> None:
        node = Lib1EdgeExtendNode()
        tex = TextureResult(texture_id=5, width=8, height=8)
        out = node._do_evaluate({"image": tex}, None, None)
        assert out["_effect_type"] == "lib1_edge_extend"
        assert node._tile_padding({"radius": 6.0}) == 6

    def test_graph_roundtrip_keeps_lib1_nodes(self) -> None:
        from src.features.composite.core.graph_migration import migrate_graph
        g = {
            "composite_schema_version": 11,
            "nodes": [
                {"id": "n1", "type": "lib1_gradient_map", "position": [0, 0],
                 "parameters": {"amount": 0.5}, "disabled": False},
            ],
            "connections": [],
        }
        out = migrate_graph(g)
        assert out["nodes"][0]["type"] == "lib1_gradient_map"
        node = NODE_REGISTRY["lib1_gradient_map"].from_dict(out["nodes"][0])
        assert node.parameters["amount"] == 0.5


class TestRealGl:
    """真实 GL：三个 fragment 编译 + 乒乓链像素对拍 CPU oracle。"""

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
            # clamp 边界（默认 REPEAT 会让邻域采样环绕——与生产/CPU 核的
            # 索引钳制语义不一致）
            t.repeat_x = False
            t.repeat_y = False
            t.use(unit)
            texs.append(t)
            program[f"u_texture" if unit == 0 else f"u_orig_tex"].value = unit
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
        out[..., : min(3, img.shape[-1])] = img[..., :3] if img.ndim == 3 else img
        out[..., 3] = img[..., 3] if img.shape[-1] == 4 else 1.0
        return out

    def test_fragments_compile(self, ctx) -> None:
        for frag in (_LIB1_GRADIENT_MAP_FRAGMENT,
                     _LIB1_EDGE_EXTEND_DILATE_FRAGMENT,
                     _LIB1_EDGE_EXTEND_OUTPUT_FRAGMENT):
            prog = ctx.program(vertex_shader=_VERT, fragment_shader=frag)
            prog.release()

    def test_c01_pixel_parity_vs_f16_oracle(self, ctx) -> None:
        from content_staging.library_v1.fixtures.generate_fixtures import gray_ramp_linear
        image = gray_ramp_linear()
        rgba = self._rgba(image)
        params = {"u_pos_1": 0.33, "u_pos_2": 0.66, "u_interp": 0, "u_mix": 1.0,
                  "u_stop_0": (0.02, 0.02, 0.05), "u_stop_1": (0.10, 0.20, 0.45),
                  "u_stop_2": (0.90, 0.55, 0.25), "u_stop_3": (1.00, 0.98, 0.90)}
        for interp in (0, 1):
            params["u_interp"] = interp
            rendered = self._render(ctx, _LIB1_GRADIENT_MAP_FRAGMENT, [rgba], params)
            expected = gradient_map_ref.evaluate(image, {
                "interp_mode": "Linear" if interp == 0 else "Smooth"})
            np.testing.assert_allclose(
                rendered[..., :3], expected, atol=6e-4,
                err_msg=f"interp={interp}")

    def test_c17_pingpong_parity_vs_oracle(self, ctx) -> None:
        from content_staging.library_v1.fixtures.generate_fixtures import alpha_fringe_hair
        fringe = alpha_fringe_hair()
        rgba = np.ascontiguousarray(fringe)  # 已是 RGBA straight
        radius, threshold = 3, 0.5

        # 乒乓执行 dilate 链（首轮 u_first=1 阈值化）
        current = rgba
        for round_idx in range(radius):
            current = self._render(
                ctx, _LIB1_EDGE_EXTEND_DILATE_FRAGMENT, [current],
                {"u_threshold": threshold, "u_first": 1 if round_idx == 0 else 0})
        final = self._render(
            ctx, _LIB1_EDGE_EXTEND_OUTPUT_FRAGMENT, [current, rgba],
            {"u_output_mode": 0})

        expected = edge_extend_ref.evaluate(fringe, {"radius": radius})
        # dilate 的 solid 标志在 alpha 通道：非 solid 像素 alpha=0 → 比较只看
        # 输出组装后的全图（含恢复的原 alpha）
        np.testing.assert_allclose(
            final, expected, atol=1e-4,
            err_msg="C17 乒乓链 vs CPU oracle")

    def test_c17_premultiplied_mode(self, ctx) -> None:
        img = np.zeros((4, 8, 4), dtype=np.float32)
        img[:, :3, :3] = (0.6, 0.5, 0.4)
        img[:, :3, 3] = 1.0
        img[:, 4, 3] = 0.5
        rgba = np.ascontiguousarray(img)
        current = rgba
        for r in range(2):
            current = self._render(
                ctx, _LIB1_EDGE_EXTEND_DILATE_FRAGMENT, [current],
                {"u_threshold": 0.5, "u_first": 1 if r == 0 else 0})
        final = self._render(
            ctx, _LIB1_EDGE_EXTEND_OUTPUT_FRAGMENT, [current, rgba],
            {"u_output_mode": 1})
        expected = edge_extend_ref.evaluate(img, {
            "radius": 2, "output_mode": "premultiplied"})
        np.testing.assert_allclose(final, expected, atol=1e-4)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
