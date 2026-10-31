"""LIB1 转场接缝测试（计划 L / L03：X01 clock_wipe + X09 true_push）。

覆盖：
- CPU：catalog 注册/集合、compute_transition_mask 分派与端点、生产核
  push 双源移动轨迹（与 slide 的机器可辨差量）、TransitionInstance 白名单；
- GPU spec：kind=5/resample 字段与参数防御（_safe_float 口径）；
- 真实 GL：GPU_TRANSITION_FRAGMENT_SHADER_SOURCE 编译 + kind=5 mask 与
  resample push 分支对拍 CPU 核（经与既有 kind=0 相同的顶点/uv 约定渲染）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.edit.core import transition_mask, transition_mask_gpu
from src.features.edit.core.gpu_transition_shaders import (
    GPU_TRANSITION_FRAGMENT_SHADER_SOURCE,
)
from src.features.edit.core.transition_catalog import (
    LIB1_TRANSITION_TYPES,
    RESAMPLE_TRANSITION_TYPES,
    SPATIAL_TRANSITION_TYPES,
    TRANSITION_TYPE_OPTIONS,
)
from src.features.edit.core.timeline_v2 import TransitionInstance
from src.features.edit.transition_packs.library_v1 import cores

pytestmark = pytest.mark.color_core

_VERT_SRC = """
#version 330
in vec2 in_pos;
out vec2 v_texcoord;
void main() {
    v_texcoord = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""


class TestCatalogAndModel:
    def test_registered_in_catalog_and_sets(self) -> None:
        assert "lib1_clock_wipe" in TRANSITION_TYPE_OPTIONS
        assert "lib1_true_push" in TRANSITION_TYPE_OPTIONS
        assert "lib1_clock_wipe" in SPATIAL_TRANSITION_TYPES
        assert "lib1_true_push" in SPATIAL_TRANSITION_TYPES
        assert "lib1_true_push" in RESAMPLE_TRANSITION_TYPES
        assert LIB1_TRANSITION_TYPES == frozenset({
            "lib1_clock_wipe", "lib1_true_push",
            # L13 mask 家族 + L14/L15 复合家族（生产集成批次加入）
            "lib1_venetian_blinds", "lib1_checker_reveal",
            "lib1_luma_reveal", "lib1_noise_dissolve",
            "lib1_halftone_reveal",
            "lib1_staggered_tiles", "lib1_hinged_doors", "lib1_cube_faces",
            "lib1_page_curl", "lib1_pixelate_bridge",
            "lib1_displacement_bridge", "lib1_ripple_refraction",
            "lib1_scanline_slices", "lib1_kaleido_bridge",
            "lib1_burn_reveal", "lib1_voronoi_reveal",
        })

    def test_transition_instance_accepts_lib1_types(self) -> None:
        inst = TransitionInstance(
            transition_id="t1", transition_type="lib1_clock_wipe",
            track_id="v1", left_clip_id="c1", right_clip_id="c2",
            center_frame=10, duration=12, alignment="centered",
            params={"start_angle": 90.0})
        assert inst.transition_type == "lib1_clock_wipe"
        push = TransitionInstance(
            transition_id="t2", transition_type="lib1_true_push",
            track_id="v1", left_clip_id="c1", right_clip_id="c2",
            center_frame=10, duration=12, alignment="centered",
            params={"direction": "right"})
        assert push.params["direction"] == "right"


class TestCpuMasks:
    def test_dispatch_matches_production_core(self) -> None:
        for p in (0.0, 0.25, 0.5, 1.0):
            via_dispatch = transition_mask.compute_transition_mask(
                16, 8, p, "lib1_clock_wipe", {"softness": 0.1})
            direct = cores.clock_wipe_mask(16, 8, p, {"softness": 0.1})
            np.testing.assert_array_equal(via_dispatch, direct)

    def test_endpoints_exact(self) -> None:
        np.testing.assert_array_equal(
            transition_mask.compute_transition_mask(8, 8, 0.0, "lib1_clock_wipe"), 0.0)
        np.testing.assert_array_equal(
            transition_mask.compute_transition_mask(8, 8, 1.0, "lib1_clock_wipe"), 1.0)

    def test_push_constant_mask_for_generic_consumers(self) -> None:
        mask = transition_mask.compute_transition_mask(
            8, 8, 0.3, "lib1_true_push", {})
        np.testing.assert_array_equal(mask, np.float32(0.3))

    def test_push_bad_params_fall_back(self) -> None:
        # _safe_float 家族防御：NaN/Inf/非法值回退默认而非抛异常
        mask = cores.clock_wipe_mask(8, 8, 0.5, {"start_angle": float("nan")})
        assert np.all(np.isfinite(mask))
        out = cores.push_frame(
            np.zeros((4, 4, 3), np.uint8), np.zeros((4, 4, 3), np.uint8),
            0.5, "sideways")  # 非法方向回退 left，不抛
        assert out.shape == (4, 4, 3)

    def test_push_both_sources_move(self) -> None:
        """X09 差量关键：两源采样坐标随 p 移动（slide 中 A 原位不动）。"""
        a = np.zeros((4, 4, 3), np.uint8)
        a[:, 1] = 255  # A 特征列 i=1
        b = np.zeros((4, 4, 3), np.uint8)
        b[:, 0] = 200  # B 特征列 i=0
        out0 = cores.push_frame(a, b, 0.0, "left")
        out25 = cores.push_frame(a, b, 0.25, "left")
        assert int(out0[0, :, 0].argmax()) == 1     # A 特征在 i=1
        assert int(out25[0, :, 0].argmax()) == 0    # A 特征左移到 i=0
        assert out25[0, 3, 2] == 200                # B 特征（列 i=0）出现在视口 i=3

    def test_push_endpoints_bit_exact_and_dtype(self) -> None:
        a = np.arange(48, dtype=np.uint8).reshape(4, 4, 3)
        b = a[::-1].copy()
        np.testing.assert_array_equal(cores.push_frame(a, b, 0.0), a)
        np.testing.assert_array_equal(cores.push_frame(a, b, 1.0), b)
        f = cores.push_frame(a.astype(np.float32), b.astype(np.float32), 0.5)
        assert f.dtype == np.float32


class TestGpuSpec:
    def test_clock_wipe_spec(self) -> None:
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_clock_wipe", 0.3,
            {"start_angle": 90.0, "clockwise": False, "softness": 0.1,
             "center_x": 0.25, "center_y": 0.75})
        assert spec is not None and spec.kind == 5
        assert spec.angle == 90.0 and spec.reverse is True
        assert spec.softness == pytest.approx(0.1)
        assert spec.center == (0.25, 0.75)

    def test_true_push_spec(self) -> None:
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_true_push", 0.5, {"direction": "right"})
        assert spec is not None
        assert spec.resample_mode == 1 and spec.push_direction == 1
        assert spec.kind == 0
        bad = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_true_push", 0.5, {"direction": "diagonal"})
        assert bad is not None and bad.push_direction == 0  # 回退 left

    def test_legacy_specs_unaffected(self) -> None:
        spec = transition_mask_gpu.build_gpu_transition_mask_spec("wipe", 0.5, {})
        assert spec is not None and spec.kind == 0
        assert spec.resample_mode == 0 and spec.center == (0.5, 0.5)


class TestRealGl:
    """真实 GL：转场 shader 编译 + 新分支对拍 CPU 核。"""

    @pytest.fixture()
    def ctx(self, gl_context):
        return gl_context

    def _render_transition(self, ctx, left, right, uniforms: dict) -> np.ndarray:
        program = ctx.program(
            vertex_shader=_VERT_SRC,
            fragment_shader=GPU_TRANSITION_FRAGMENT_SHADER_SOURCE)
        h, w = left.shape[0], left.shape[1]
        tex_l = ctx.texture((w, h), 4, self._rgba(left).tobytes(), dtype="f4")
        tex_r = ctx.texture((w, h), 4, self._rgba(right).tobytes(), dtype="f4")
        tex_l.use(0)
        tex_r.use(1)
        program["u_left_tex"].value = 0
        program["u_right_tex"].value = 1
        program["u_left_opacity"].value = 1.0
        program["u_right_opacity"].value = 1.0
        for key, value in uniforms.items():
            program[key].value = value
        target = ctx.texture((w, h), 4, dtype="f4")
        fbo = ctx.framebuffer(color_attachments=[target])
        quad = np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype="f4")
        vbo = ctx.buffer(quad.tobytes())
        vao = ctx.vertex_array(program, vbo, "in_pos")
        fbo.use()
        vao.render(moderngl.TRIANGLE_STRIP)
        raw = fbo.read(components=4, dtype="f4")
        vao.release(); vbo.release(); target.release(); fbo.release()
        tex_l.release(); tex_r.release(); program.release()
        return np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)

    @staticmethod
    def _rgba(img: np.ndarray) -> np.ndarray:
        out = np.zeros((img.shape[0], img.shape[1], 4), dtype=np.float32)
        out[..., :3] = img
        out[..., 3] = 1.0
        return out

    def test_shader_compiles_with_new_uniforms(self, ctx) -> None:
        program = ctx.program(
            vertex_shader=_VERT_SRC,
            fragment_shader=GPU_TRANSITION_FRAGMENT_SHADER_SOURCE)
        assert "u_center" in program and "u_resample_mode" in program \
            and "u_push_dir" in program
        program.release()

    def test_clock_wipe_mask_parity(self, ctx) -> None:
        w, h = 16, 8
        left = np.zeros((h, w, 3), np.float32)
        right = np.ones((h, w, 3), np.float32)
        for cw in (True, False):
            rendered = self._render_transition(ctx, left, right, {
                "u_progress": 0.25, "u_use_procedural": 1,
                "u_transition_kind": 5, "u_direction": 0, "u_reverse": 0 if cw else 1,
                "u_angle": 0.0, "u_softness": 0.001, "u_vertex_count": 0,
                "u_effect_mode": 0, "u_transition_mode": 0,
                "u_resample_mode": 0, "u_push_dir": 0,
                "u_center": (0.5, 0.5),
            })[..., 0]
            cpu = cores.clock_wipe_mask(w, h, 0.25, {"clockwise": cw, "softness": 0.001})
            # GL v 轴与 numpy 行序上下翻转（与既有 kind 同约定），对拍翻转视图
            np.testing.assert_allclose(rendered, np.flipud(cpu), atol=1e-4)

    def test_push_parity(self, ctx) -> None:
        w, h = 8, 8
        xs = np.linspace(0, 1, w, dtype=np.float32)
        left = np.repeat(xs[None, :, None], h, axis=0)
        left = np.repeat(left, 3, axis=2)
        right = (1.0 - left).copy()
        rendered = self._render_transition(ctx, left, right, {
            "u_progress": 0.25, "u_use_procedural": 1,
            "u_transition_kind": 0, "u_direction": 0, "u_reverse": 0,
            "u_angle": 0.0, "u_softness": 0.02, "u_vertex_count": 0,
            "u_effect_mode": 0, "u_transition_mode": 0,
            "u_resample_mode": 1, "u_push_dir": 0,
            "u_center": (0.5, 0.5),
        })[..., :3]
        cpu = cores.push_frame(left, right, 0.25, "left")
        np.testing.assert_allclose(rendered, np.flipud(cpu), atol=6e-3)

    def test_push_endpoints_gl(self, ctx) -> None:
        w, h = 6, 6
        left = np.full((h, w, 3), 0.2, np.float32)
        right = np.full((h, w, 3), 0.8, np.float32)
        base = {"u_use_procedural": 1, "u_transition_kind": 0, "u_direction": 0,
                "u_reverse": 0, "u_angle": 0.0, "u_softness": 0.02,
                "u_vertex_count": 0, "u_effect_mode": 0, "u_transition_mode": 0,
                "u_center": (0.5, 0.5)}
        out0 = self._render_transition(ctx, left, right, {
            **base, "u_progress": 0.0, "u_resample_mode": 1, "u_push_dir": 0})
        np.testing.assert_allclose(out0[..., :3], np.flipud(left), atol=1e-5)
        out1 = self._render_transition(ctx, left, right, {
            **base, "u_progress": 1.0, "u_resample_mode": 1, "u_push_dir": 0})
        np.testing.assert_allclose(out1[..., :3], np.flipud(right), atol=1e-5)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
