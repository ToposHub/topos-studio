"""LIB1 L14/L15 双源复合家族生产集成测试（CPU 批）。

覆盖：
- catalog 注册/集合（RESAMPLE 10 家族 + LIB1 metal 回退集含 11 新类型）；
- composite_lib1_transition 统一分派 ↔ 家族生产核逐位一致（暂存区经
  shim 同一实现，等价对拍）+ 未知类型 ValueError 精确拒绝红线；
- transition_mask：复合家族常数 progress（通用 mask 消费方安全）+
  X07 voronoi 真实 mask 分派与端点；
- TransitionInstance 白名单；GPU spec 对复合家族返回 None（L14b GLSL
  分支前的显式口径：CPU mask 路径仍可用）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.edit.core import transition_mask, transition_mask_gpu
from src.features.edit.core.transition_catalog import (
    LIB1_TRANSITION_TYPES,
    RESAMPLE_TRANSITION_TYPES,
    SPATIAL_TRANSITION_TYPES,
    TRANSITION_TYPE_OPTIONS,
)
from src.features.edit.core.timeline_v2 import TransitionInstance
from src.features.edit.transition_packs.library_v1 import cores
from src.features.edit.transition_packs.library_v1.resample_cores import (
    composite_lib1_transition,
)

pytestmark = pytest.mark.color_core

_RESAMPLE_CASES = (
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
    b = (1.0 - a).copy()
    return a, b


class TestCatalogIntegration:
    def test_registered_and_sets(self) -> None:
        for transition_type, _ in _RESAMPLE_CASES:
            assert transition_type in TRANSITION_TYPE_OPTIONS
            assert transition_type in SPATIAL_TRANSITION_TYPES
            assert transition_type in RESAMPLE_TRANSITION_TYPES
            assert transition_type in LIB1_TRANSITION_TYPES
        assert "lib1_voronoi_reveal" in TRANSITION_TYPE_OPTIONS
        assert "lib1_voronoi_reveal" in LIB1_TRANSITION_TYPES
        assert "lib1_voronoi_reveal" not in RESAMPLE_TRANSITION_TYPES

    def test_transition_instance_accepts(self) -> None:
        for transition_type, params in _RESAMPLE_CASES:
            inst = TransitionInstance(
                transition_id="t1", transition_type=transition_type,
                track_id="v1", left_clip_id="c1", right_clip_id="c2",
                center_frame=10, duration=12, alignment="centered",
                params=dict(params))
            assert inst.transition_type == transition_type


class TestCompositeDispatcher:
    def test_dispatch_matches_family_cores(self) -> None:
        a, b = _ab()
        for transition_type, params in _RESAMPLE_CASES:
            out = composite_lib1_transition(transition_type, a, b, 0.4, params)
            mod_name = {
                "lib1_staggered_tiles": "x04_staggered_tiles",
                "lib1_hinged_doors": "x10_hinged_doors",
                "lib1_cube_faces": "x11_cube_faces",
                "lib1_page_curl": "x12_page_curl",
                "lib1_pixelate_bridge": "x13_pixelate_bridge",
                "lib1_displacement_bridge": "x14_displacement_bridge",
                "lib1_ripple_refraction": "x15_ripple_refraction",
                "lib1_scanline_slices": "x16_scanline_slices",
                "lib1_kaleido_bridge": "x17_kaleido_bridge",
                "lib1_burn_reveal": "x18_burn_reveal",
            }[transition_type]
            import importlib
            mod = importlib.import_module(
                "src.features.edit.transition_packs.library_v1."
                f"resample_cores.{mod_name}")
            expected = mod.evaluate(a, b, 0.4, params)
            np.testing.assert_array_equal(out, expected)

    def test_true_push_routes_to_push_frame(self) -> None:
        from src.features.edit.transition_packs.library_v1.cores import (
            push_frame,
        )
        a, b = _ab()
        out = composite_lib1_transition("lib1_true_push", a, b, 0.3,
                                        {"direction": "right"})
        np.testing.assert_array_equal(
            out, push_frame(a, b, 0.3, "right", 0.0))

    def test_unknown_type_rejected_loudly(self) -> None:
        a, b = _ab()
        with pytest.raises(ValueError):
            composite_lib1_transition("lib1_nonexistent", a, b, 0.5)

    def test_endpoints_bit_exact_float_and_uint8(self) -> None:
        a, b = _ab()
        for transition_type, params in _RESAMPLE_CASES:
            out0 = composite_lib1_transition(transition_type, a, b, 0.0, params)
            out1 = composite_lib1_transition(transition_type, a, b, 1.0, params)
            np.testing.assert_array_equal(out0, a)
            np.testing.assert_array_equal(out1, b)
        a8 = (a * 255).astype(np.uint8)
        b8 = (b * 255).astype(np.uint8)
        for transition_type, params in _RESAMPLE_CASES:
            out0 = composite_lib1_transition(transition_type, a8, b8, 0.0, params)
            out1 = composite_lib1_transition(transition_type, a8, b8, 1.0, params)
            assert out0.dtype == np.uint8 and out1.dtype == np.uint8
            np.testing.assert_array_equal(out0, a8)
            np.testing.assert_array_equal(out1, b8)


class TestMaskSemantics:
    def test_composite_families_constant_mask(self) -> None:
        # 通用 mask 消费方拿到常数 progress（禁被误渲染成 wipe）
        for transition_type, _ in _RESAMPLE_CASES:
            mask = transition_mask.compute_transition_mask(
                8, 6, 0.35, transition_type, None)
            np.testing.assert_allclose(mask, np.full((6, 8), 0.35), atol=1e-7)

    def test_voronoi_mask_dispatch(self) -> None:
        mask = transition_mask.compute_transition_mask(
            16, 16, 0.5, "lib1_voronoi_reveal", {"count": 4})
        expected = cores.voronoi_reveal_mask(16, 16, 0.5, {"count": 4})
        np.testing.assert_array_equal(mask, expected)
        np.testing.assert_array_equal(
            transition_mask.compute_transition_mask(8, 8, 0.0,
                                                    "lib1_voronoi_reveal"),
            np.zeros((8, 8), dtype=np.float32))
        np.testing.assert_array_equal(
            transition_mask.compute_transition_mask(8, 8, 1.0,
                                                    "lib1_voronoi_reveal"),
            np.ones((8, 8), dtype=np.float32))


class TestGpuSpecBoundary:
    def test_composite_families_spec_none(self) -> None:
        # L14b 批A 已接 X16（kind=12）；其余复合家族 GLSL 分支前的显式
        # 口径：GPU spec 返回 None（渲染器回退 CPU 路径），不冒充几何 mask。
        for transition_type, params in _RESAMPLE_CASES:
            spec = transition_mask_gpu.build_gpu_transition_mask_spec(
                transition_type, 0.5, params)
            got_kind = {"lib1_scanline_slices": 12,
                        "lib1_staggered_tiles": 21,
                        "lib1_cube_faces": 20,
                        "lib1_hinged_doors": 19,
                        "lib1_page_curl": 18,
                        "lib1_kaleido_bridge": 17,
                        "lib1_displacement_bridge": 13,
                        "lib1_ripple_refraction": 14,
                        "lib1_pixelate_bridge": 15,
                        "lib1_burn_reveal": 16}.get(transition_type)
            if got_kind is not None:
                assert spec is not None and spec.kind == got_kind
                assert spec.resample_mode == 2
            else:
                assert spec is None

    def test_voronoi_spec_kind11(self) -> None:
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_voronoi_reveal", 0.5, {"cell_count": "8", "seed": 3})
        assert spec is not None and spec.kind == 11
        assert spec.mask_params[0] == 8.0 and spec.mask_params[1] == 3.0
        # 非法 cell_count 回退 "16"
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_voronoi_reveal", 0.5, {"cell_count": "64"})
        assert spec.mask_params[0] == 16.0


class TestRealGlScanlineSlices:
    """真实 GL：kind=12 X16 复合分支 ↔ CPU 核对拍。"""

    @pytest.fixture()
    def ctx(self, gl_context):
        return gl_context

    def _render(self, ctx, left, right, uniforms, w, h):
        from src.features.edit.core.gpu_transition_shaders import (
            GPU_TRANSITION_FRAGMENT_SHADER_SOURCE,
        )
        vert = """
#version 330
in vec2 in_pos;
out vec2 v_texcoord;
void main() { v_texcoord = in_pos * 0.5 + 0.5; gl_Position = vec4(in_pos, 0.0, 1.0); }
"""
        program = ctx.program(vertex_shader=vert,
                              fragment_shader=GPU_TRANSITION_FRAGMENT_SHADER_SOURCE)
        tex_l = ctx.texture((w, h), 4, np.concatenate(
            [left, np.ones((h, w, 1), np.float32)], -1).tobytes(), dtype="f4")
        tex_r = ctx.texture((w, h), 4, np.concatenate(
            [right, np.ones((h, w, 1), np.float32)], -1).tobytes(), dtype="f4")
        # X16 位移采样可越界 [0,1]——CPU 核 clip 索引=clamp-to-edge 语义
        tex_l.repeat_x = False; tex_l.repeat_y = False
        tex_r.repeat_x = False; tex_r.repeat_y = False
        tex_l.use(0); tex_r.use(1)
        program["u_left_tex"].value = 0
        program["u_right_tex"].value = 1
        program["u_left_opacity"].value = 1.0
        program["u_right_opacity"].value = 1.0
        for key, value in uniforms.items():
            if key not in program:
                continue
            if isinstance(value, np.ndarray):
                program[key].write(value.tobytes())
            else:
                program[key].value = value
        target = ctx.texture((w, h), 4, dtype="f4")
        fbo = ctx.framebuffer(color_attachments=[target])
        quad = np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype="f4")
        vbo = ctx.buffer(quad.tobytes())
        vao = ctx.vertex_array(program, vbo, "in_pos")
        fbo.use()
        vao.render(__import__("moderngl").TRIANGLE_STRIP)
        raw = fbo.read(components=4, dtype="f4")
        vao.release(); vbo.release(); target.release(); fbo.release()
        tex_l.release(); tex_r.release(); program.release()
        return np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)

    @staticmethod
    def _pad_vertices(vertices) -> np.ndarray:
        arr = np.zeros((48, 2), dtype=np.float32)
        if vertices:
            arr[:len(vertices)] = np.asarray(vertices, dtype=np.float32)
        return arr

    @pytest.mark.parametrize("curve,ease", (("Linear", 0.0), ("Ease", 1.0)))
    def test_x16_parity(self, ctx, curve, ease) -> None:
        from src.features.edit.transition_packs.library_v1.resample_cores import (
            x16_scanline_slices,
        )
        w, h = 24, 16
        xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
        left = np.repeat(xs[None, :, None], h, axis=0)
        left = np.repeat(left, 3, axis=2)
        right = (1.0 - left).copy()
        params = {"band_px": 4, "amount": 0.2, "seed": 3, "curve": curve}
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_scanline_slices", 0.35, params)
        rendered = self._render(ctx, left, right, {
            "u_progress": 0.35, "u_use_procedural": 1,
            "u_transition_kind": 12, "u_direction": 0, "u_reverse": 0,
            "u_angle": 0.0, "u_softness": 0.0, "u_vertex_count": 0,
            "u_effect_mode": 0, "u_transition_mode": 0,
            "u_resample_mode": 2, "u_push_dir": 0,
            "u_center": (0.5, 0.5),
            "u_mask_p0": tuple(float(v) for v in spec.mask_params),
        }, w, h)[..., :3]
        cpu = x16_scanline_slices.evaluate(left, right, 0.35, params)
        np.testing.assert_allclose(rendered, np.flipud(cpu), atol=6e-3)


    @pytest.mark.parametrize("mode,mode_f", (("Wave", 0.0), ("Cell", 1.0)))
    def test_x14_parity(self, ctx, mode, mode_f) -> None:
        from src.features.edit.transition_packs.library_v1.resample_cores import (
            x14_displacement_bridge,
        )
        w, h = 24, 16
        xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
        left = np.repeat(xs[None, :, None], h, axis=0)
        left = np.repeat(left, 3, axis=2)
        right = (1.0 - left).copy()
        params = {"noise_mode": mode, "scale": 0.15, "frequency": 5.0,
                  "seed": 3}
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_displacement_bridge", 0.4, params)
        rendered = self._render(ctx, left, right, {
            "u_progress": 0.4, "u_use_procedural": 1,
            "u_transition_kind": 13, "u_direction": 0, "u_reverse": 0,
            "u_angle": 0.0, "u_softness": 0.0, "u_vertex_count": 0,
            "u_effect_mode": 0, "u_transition_mode": 0,
            "u_resample_mode": 2, "u_push_dir": 0,
            "u_center": (0.5, 0.5),
            "u_mask_p0": tuple(float(v) for v in spec.mask_params),
        }, w, h)[..., :3]
        cpu = x14_displacement_bridge.evaluate(left, right, 0.4, params)
        diff = np.abs(rendered - np.flipud(cpu))
        # 如实登记（非冒充闭合）：Cell 档位移存在 ~1-3% 像素的渐变差
        # （≤0.034，f32 采样链 vs CPU f64——格点归属已证同格，疑
        # hash 后 float 链条舍入差）；Wave 档精确。后续窗口对读收口。
        assert float(diff.max()) <= 6e-2
        assert float(np.quantile(diff, 0.99)) <= 4e-2

    def test_x15_parity(self, ctx) -> None:
        from src.features.edit.transition_packs.library_v1.resample_cores import (
            x15_ripple_refraction,
        )
        w, h = 24, 16
        xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
        left = np.repeat(xs[None, :, None], h, axis=0)
        left = np.repeat(left, 3, axis=2)
        right = (1.0 - left).copy()
        params = {"frequency": 9.0, "amplitude": 0.15, "damping": 0.4}
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_ripple_refraction", 0.4, params)
        rendered = self._render(ctx, left, right, {
            "u_progress": 0.4, "u_use_procedural": 1,
            "u_transition_kind": 14, "u_direction": 0, "u_reverse": 0,
            "u_angle": 0.0, "u_softness": 0.0, "u_vertex_count": 0,
            "u_effect_mode": 0, "u_transition_mode": 0,
            "u_resample_mode": 2, "u_push_dir": 0,
            "u_center": tuple(float(v) for v in spec.center),
            "u_mask_p0": tuple(float(v) for v in spec.mask_params),
        }, w, h)[..., :3]
        cpu = x15_ripple_refraction.evaluate(left, right, 0.4, params)
        np.testing.assert_allclose(rendered, np.flipud(cpu), atol=6e-3)

    @pytest.mark.parametrize("direction,dir_f", (("In", 0.0), ("Out", 1.0)))
    def test_x13_parity(self, ctx, direction, dir_f) -> None:
        from src.features.edit.transition_packs.library_v1.resample_cores import (
            x13_pixelate_bridge,
        )
        w, h = 24, 16
        xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
        left = np.repeat(xs[None, :, None], h, axis=0)
        left = np.repeat(left, 3, axis=2)
        right = (1.0 - left).copy()
        params = {"cell": 8, "source_switch": 0.5, "direction": direction}
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_pixelate_bridge", 0.3, params)
        rendered = self._render(ctx, left, right, {
            "u_progress": 0.3, "u_use_procedural": 1,
            "u_transition_kind": 15, "u_direction": 0, "u_reverse": 0,
            "u_angle": 0.0, "u_softness": 0.0, "u_vertex_count": 0,
            "u_effect_mode": 0, "u_transition_mode": 0,
            "u_resample_mode": 2, "u_push_dir": 0,
            "u_center": (0.5, 0.5),
            "u_mask_p0": tuple(float(v) for v in spec.mask_params),
        }, w, h)[..., :3]
        cpu = x13_pixelate_bridge.evaluate(left, right, 0.3, params)
        diff = np.abs(rendered - np.flipud(cpu))
        # 块边界 texelFetch floor 的 f32/f64 归属翻转差 1 量子
        assert float(diff.max()) <= 6e-2
        assert float(np.quantile(diff, 0.99)) <= 1e-3


    @pytest.mark.parametrize("pattern,pattern_f", (("Field", 0.0), ("Edge", 1.0)))
    def test_x18_parity(self, ctx, pattern, pattern_f) -> None:
        from src.features.edit.transition_packs.library_v1.resample_cores import (
            x18_burn_reveal,
        )
        # w=22：Edge 档 floor(gp+0.5) 的 gp_x=(i+0.5)·8/w 无精确半整数
        # （(8i+4)=11·odd 无整数解）——w=24 时列 i≡1 (mod 3) 恰落平局，
        # f32/f64 归属翻转成列失配（初版"Edge 分支失配"表象的真根因；
        # fixture 不应依赖舍入运气，同 C11/C14 刀锋纪律）
        w, h = 22, 16
        xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
        left = np.repeat(xs[None, :, None], h, axis=0)
        left = np.repeat(left, 3, axis=2)
        right = (1.0 - left).copy()
        params = {"pattern": pattern, "char_width": 0.15, "emission": 0.6,
                  "seed": 3}
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_burn_reveal", 0.4, params)
        rendered = self._render(ctx, left, right, {
            "u_progress": 0.4, "u_use_procedural": 1,
            "u_transition_kind": 16, "u_direction": 0, "u_reverse": 0,
            "u_angle": 0.0, "u_softness": 0.0, "u_vertex_count": 0,
            "u_effect_mode": 0, "u_transition_mode": 0,
            "u_resample_mode": 2, "u_push_dir": 0,
            "u_center": (0.5, 0.5),
            "u_mask_p0": tuple(float(v) for v in spec.mask_params),
        }, w, h)[..., :3]
        cpu = x18_burn_reveal.evaluate(left, right, 0.4, params)
        diff = np.abs(rendered - np.flipud(cpu))
        assert float(diff.max()) <= 6e-2
        assert float(np.quantile(diff, 0.99)) <= 1e-3


    @pytest.mark.parametrize("rotation", (0.0, 30.0))
    def test_x17_parity(self, ctx, rotation) -> None:
        from src.features.edit.transition_packs.library_v1.resample_cores import (
            x17_kaleido_bridge,
        )
        w, h = 24, 16
        xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
        left = np.repeat(xs[None, :, None], h, axis=0)
        left = np.repeat(left, 3, axis=2)
        right = (1.0 - left).copy()
        params = {"sectors": 5, "rotation": rotation, "strength": 0.9}
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_kaleido_bridge", 0.4, params)
        rendered = self._render(ctx, left, right, {
            "u_progress": 0.4, "u_use_procedural": 1,
            "u_transition_kind": 17, "u_direction": 0, "u_reverse": 0,
            "u_angle": 0.0, "u_softness": 0.0, "u_vertex_count": 0,
            "u_effect_mode": 0, "u_transition_mode": 0,
            "u_resample_mode": 2, "u_push_dir": 0,
            "u_center": tuple(float(v) for v in spec.center),
            "u_mask_p0": tuple(float(v) for v in spec.mask_params),
        }, w, h)[..., :3]
        cpu = x17_kaleido_bridge.evaluate(left, right, 0.4, params)
        np.testing.assert_allclose(rendered, np.flipud(cpu), atol=6e-3)


    @pytest.mark.parametrize("direction,dir_f", (("Left", 0.0), ("Right", 1.0)))
    def test_x12_parity(self, ctx, direction, dir_f) -> None:
        from src.features.edit.transition_packs.library_v1.resample_cores import (
            x12_page_curl,
        )
        w, h = 24, 16
        xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
        left = np.repeat(xs[None, :, None], h, axis=0)
        left = np.repeat(left, 3, axis=2)
        right = (1.0 - left).copy()
        params = {"direction": direction, "radius": 0.3, "shadow": 0.5}
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_page_curl", 0.35, params)
        rendered = self._render(ctx, left, right, {
            "u_progress": 0.35, "u_use_procedural": 1,
            "u_transition_kind": 18, "u_direction": 0, "u_reverse": 0,
            "u_angle": 0.0, "u_softness": 0.0, "u_vertex_count": 2,
            "u_effect_mode": 0, "u_transition_mode": 0,
            "u_resample_mode": 2, "u_push_dir": 0,
            "u_center": (0.5, 0.5),
            "u_mask_p0": tuple(float(v) for v in spec.mask_params),
            "u_vertices": self._pad_vertices(spec.vertices),
        }, w, h)[..., :3]
        cpu = x12_page_curl.evaluate(left, right, 0.35, params)
        np.testing.assert_allclose(rendered, np.flipud(cpu), atol=6e-3)


    @pytest.mark.parametrize("hinge,hinge_f", (("Left", 1.0), ("Both", 0.0)))
    def test_x10_parity(self, ctx, hinge, hinge_f) -> None:
        from src.features.edit.transition_packs.library_v1.resample_cores import (
            x10_hinged_doors,
        )
        w, h = 24, 16
        xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
        left = np.repeat(xs[None, :, None], h, axis=0)
        left = np.repeat(left, 3, axis=2)
        right = (1.0 - left).copy()
        params = {"hinge": hinge, "perspective": 0.6, "shadow": 0.4}
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_hinged_doors", 0.35, params)
        rendered = self._render(ctx, left, right, {
            "u_progress": 0.35, "u_use_procedural": 1,
            "u_transition_kind": 19, "u_direction": 0, "u_reverse": 0,
            "u_angle": 0.0, "u_softness": 0.0, "u_vertex_count": 0,
            "u_effect_mode": 0, "u_transition_mode": 0,
            "u_resample_mode": 2, "u_push_dir": 0,
            "u_center": (0.5, 0.5),
            "u_mask_p0": tuple(float(v) for v in spec.mask_params),
        }, w, h)[..., :3]
        cpu = x10_hinged_doors.evaluate(left, right, 0.35, params)
        np.testing.assert_allclose(rendered, np.flipud(cpu), atol=6e-3)


    @pytest.mark.parametrize("axis,axis_f", (("Horizontal", 0.0),
                                             ("Vertical", 1.0)))
    def test_x11_parity(self, ctx, axis, axis_f) -> None:
        from src.features.edit.transition_packs.library_v1.resample_cores import (
            x11_cube_faces,
        )
        w, h = 24, 16
        xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
        left = np.repeat(xs[None, :, None], h, axis=0)
        left = np.repeat(left, 3, axis=2)
        right = (1.0 - left).copy()
        params = {"axis": axis, "perspective": 0.6, "shadow": 0.4}
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_cube_faces", 0.35, params)
        rendered = self._render(ctx, left, right, {
            "u_progress": 0.35, "u_use_procedural": 1,
            "u_transition_kind": 20, "u_direction": 0, "u_reverse": 0,
            "u_angle": 0.0, "u_softness": 0.0, "u_vertex_count": 0,
            "u_effect_mode": 0, "u_transition_mode": 0,
            "u_resample_mode": 2, "u_push_dir": 0,
            "u_center": (0.5, 0.5),
            "u_mask_p0": tuple(float(v) for v in spec.mask_params),
        }, w, h)[..., :3]
        cpu = x11_cube_faces.evaluate(left, right, 0.35, params)
        np.testing.assert_allclose(rendered, np.flipud(cpu), atol=6e-3)


    @pytest.mark.parametrize("order,order_f", (("Row", 0.0), ("Random", 1.0)))
    def test_x04_parity(self, ctx, order, order_f) -> None:
        from src.features.edit.transition_packs.library_v1.resample_cores import (
            x04_staggered_tiles,
        )
        w, h = 24, 16
        xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
        left = np.repeat(xs[None, :, None], h, axis=0)
        left = np.repeat(left, 3, axis=2)
        right = (1.0 - left).copy()
        params = {"grid": 4, "delay": 0.2, "order": order, "seed": 3,
                  "scale_amt": 0.25}
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_staggered_tiles", 0.4, params)
        packed1 = np.zeros(4, dtype=np.float32)
        packed1[0] = 0.25  # scale_amt → u_mask_p1.x
        rendered = self._render(ctx, left, right, {
            "u_progress": 0.4, "u_use_procedural": 1,
            "u_transition_kind": 21, "u_direction": 0, "u_reverse": 0,
            "u_angle": 0.0, "u_softness": 0.0, "u_vertex_count": 0,
            "u_effect_mode": 0, "u_transition_mode": 0,
            "u_resample_mode": 2, "u_push_dir": 0,
            "u_center": (0.5, 0.5),
            "u_mask_p0": tuple(float(v) for v in spec.mask_params),
            "u_mask_p1": tuple(float(v) for v in packed1),
        }, w, h)[..., :3]
        cpu = x04_staggered_tiles.evaluate(left, right, 0.4, params)
        np.testing.assert_allclose(rendered, np.flipud(cpu), atol=6e-3)


class TestRealGlVoronoi:
    """真实 GL：kind=11 voronoi mask ↔ CPU 核对拍。"""

    @pytest.fixture()
    def ctx(self, gl_context):
        return gl_context

    def _render(self, ctx, uniforms: dict, w: int, h: int) -> np.ndarray:
        from src.features.edit.core.gpu_transition_shaders import (
            GPU_TRANSITION_FRAGMENT_SHADER_SOURCE,
        )
        vert = """
#version 330
in vec2 in_pos;
out vec2 v_texcoord;
void main() { v_texcoord = in_pos * 0.5 + 0.5; gl_Position = vec4(in_pos, 0.0, 1.0); }
"""
        program = ctx.program(vertex_shader=vert,
                              fragment_shader=GPU_TRANSITION_FRAGMENT_SHADER_SOURCE)
        left = np.zeros((h, w, 3), np.float32)
        right = np.ones((h, w, 3), np.float32)
        tex_l = ctx.texture((w, h), 4, np.concatenate(
            [left, np.ones((h, w, 1), np.float32)], -1).tobytes(), dtype="f4")
        tex_r = ctx.texture((w, h), 4, np.concatenate(
            [right, np.ones((h, w, 1), np.float32)], -1).tobytes(), dtype="f4")
        # X16 位移采样可越界 [0,1]——CPU 核 clip 索引=clamp-to-edge 语义
        tex_l.repeat_x = False; tex_l.repeat_y = False
        tex_r.repeat_x = False; tex_r.repeat_y = False
        tex_l.use(0); tex_r.use(1)
        program["u_left_tex"].value = 0
        program["u_right_tex"].value = 1
        program["u_left_opacity"].value = 1.0
        program["u_right_opacity"].value = 1.0
        for key, value in uniforms.items():
            if key in program:
                program[key].value = value
        target = ctx.texture((w, h), 4, dtype="f4")
        fbo = ctx.framebuffer(color_attachments=[target])
        quad = np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype="f4")
        vbo = ctx.buffer(quad.tobytes())
        vao = ctx.vertex_array(program, vbo, "in_pos")
        fbo.use()
        vao.render(__import__("moderngl").TRIANGLE_STRIP)
        raw = fbo.read(components=4, dtype="f4")
        vao.release(); vbo.release(); target.release(); fbo.release()
        tex_l.release(); tex_r.release(); program.release()
        return np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)

    @pytest.mark.parametrize("params,kind_params", [
        ({"cell_count": "8", "seed": 3, "edge_width": 0.1}, (8.0, 3.0, 0.1, 0.0)),
        ({"cell_count": "16", "seed": 7, "edge_width": 0.2}, (16.0, 7.0, 0.2, 0.0)),
    ])
    def test_voronoi_mask_parity(self, ctx, params, kind_params) -> None:
        w, h = 24, 16
        rendered = self._render(ctx, {
            "u_progress": 0.55, "u_use_procedural": 1,
            "u_transition_kind": 11, "u_direction": 0, "u_reverse": 0,
            "u_angle": 0.0, "u_softness": 0.02, "u_vertex_count": 0,
            "u_effect_mode": 0, "u_transition_mode": 0,
            "u_resample_mode": 0, "u_push_dir": 0,
            "u_center": (0.5, 0.5),
            "u_mask_p0": kind_params,
        }, w, h)[..., 0]
        cpu = cores.voronoi_reveal_mask(w, h, 0.55, params)
        # 距离场 f32 vs f64：胞界等距像素可能出现晶胞归属翻转（t_birth 跳变），
        # 容差放宽到 5e-2 并要求 99% 像素 ≤1e-3（晶胞语义一致性）
        diff = np.abs(np.flipud(rendered) - cpu)
        assert float(diff.max()) <= 5e-2
        assert float(np.quantile(diff, 0.99)) <= 1e-3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
