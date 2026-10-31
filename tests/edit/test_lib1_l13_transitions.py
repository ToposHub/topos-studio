"""LIB1 L13 mask 家族生产集成测试（X02/X03/X05/X06/X08）。

覆盖：
- CPU：catalog 注册/集合、compute_transition_mask 分派 ↔ 生产核逐位一致、
  端点精确、TransitionInstance 白名单；
- GPU spec：kind=6..10 与 mask_params 打包（防御钳制）+ 旧 spec 回归；
- 真实 GL：shader 编译含 u_mask_p0/p1 + 五个 kind 的 mask 渲染 ↔ CPU 核
  对拍（kind 8 内容驱动用非对称源图验证行序约定）。
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

_L13_MASK_CASES = (
    ("lib1_venetian_blinds", {"count": 4, "softness": 0.0}),
    ("lib1_checker_reveal", {"rows": 2, "columns": 2, "softness": 0.0}),
    ("lib1_luma_reveal", {"threshold": 0.5, "span": 0.2}),
    ("lib1_noise_dissolve", {"scale": 8, "seed": 3, "softness": 0.05}),
    ("lib1_halftone_reveal", {"spacing": 8, "angle": 45.0, "softness": 0.1}),
)


class TestCpuIntegration:
    def test_registered_in_catalog_and_sets(self) -> None:
        for transition_type, _ in _L13_MASK_CASES:
            assert transition_type in TRANSITION_TYPE_OPTIONS
            assert transition_type in SPATIAL_TRANSITION_TYPES
            assert transition_type in LIB1_TRANSITION_TYPES

    def test_dispatch_matches_production_core(self) -> None:
        w, h = 24, 12
        for transition_type, params in _L13_MASK_CASES:
            dispatched = transition_mask.compute_transition_mask(
                w, h, 0.4, transition_type, params)
            core = getattr(cores, {
                "lib1_venetian_blinds": "venetian_blinds_mask",
                "lib1_checker_reveal": "checker_reveal_mask",
                "lib1_luma_reveal": "luma_reveal_mask",
                "lib1_noise_dissolve": "noise_dissolve_mask",
                "lib1_halftone_reveal": "halftone_reveal_mask",
            }[transition_type])
            expected = core(w, h, 0.4, params)
            np.testing.assert_array_equal(dispatched, expected)

    def test_luma_content_driven_differs_from_fallback(self) -> None:
        # 带源图的 luma_reveal 是内容驱动（非几何）——与无图回退的线性
        # dissolve 机器可辨（真实合成路径见 CPU 合成分支/L20 验收）
        w, h = 12, 8
        img = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :, None]
        img = np.repeat(np.repeat(img, h, axis=0), 3, axis=2)
        driven = cores.luma_reveal_mask(w, h, 0.4, {"span": 0.2}, image_a=img)
        fallback = cores.luma_reveal_mask(w, h, 0.4)
        assert not np.allclose(driven, fallback)

    def test_endpoints_exact(self) -> None:
        for transition_type, params in _L13_MASK_CASES:
            for p in (0.0, -0.3):
                np.testing.assert_array_equal(
                    transition_mask.compute_transition_mask(
                        12, 6, p, transition_type, params),
                    np.zeros((6, 12), dtype=np.float32))
            for p in (1.0, 1.7):
                np.testing.assert_array_equal(
                    transition_mask.compute_transition_mask(
                        12, 6, p, transition_type, params),
                    np.ones((6, 12), dtype=np.float32))

    def test_transition_instance_accepts_l13_types(self) -> None:
        for transition_type, params in _L13_MASK_CASES:
            inst = TransitionInstance(
                transition_id="t1", transition_type=transition_type,
                track_id="v1", left_clip_id="c1", right_clip_id="c2",
                center_frame=10, duration=12, alignment="centered",
                params=dict(params))
            assert inst.transition_type == transition_type


class TestGpuSpec:
    _KINDS = {
        "lib1_venetian_blinds": 6,
        "lib1_checker_reveal": 7,
        "lib1_luma_reveal": 8,
        "lib1_noise_dissolve": 9,
        "lib1_halftone_reveal": 10,
    }

    def test_kinds_and_param_packing(self) -> None:
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_venetian_blinds", 0.3,
            {"count": 8, "angle": 30.0, "phase": 1.7, "softness": 0.2})
        assert spec.kind == 6
        assert spec.mask_params[0] == 8.0
        assert spec.mask_params[2] == pytest.approx(0.7)  # phase mod 1

        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_checker_reveal", 0.3, {"rows": 99, "columns": 0})
        assert spec.kind == 7
        assert spec.mask_params[0] == 16.0  # clamp [1,16]
        assert spec.mask_params[1] == 1.0

        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_luma_reveal", 0.3, {"invert": True, "source": "B"})
        assert spec.kind == 8
        assert spec.mask_params[2] == 1.0 and spec.mask_params[3] == 1.0

        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_noise_dissolve", 0.3, {"scale": 200, "noise_mode": "Cell"})
        assert spec.kind == 9
        assert spec.mask_params[0] == 64.0 and spec.mask_params[3] == 1.0

        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_halftone_reveal", 0.3, {"shape": "Square"})
        assert spec.kind == 10
        assert spec.mask_params[3] == 1.0

    def test_legacy_specs_unaffected(self) -> None:
        spec = transition_mask_gpu.build_gpu_transition_mask_spec("wipe", 0.5)
        assert spec.kind == 0 and spec.mask_params == ()
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_clock_wipe", 0.5)
        assert spec.kind == 5 and spec.mask_params == ()


class TestRealGl:
    """真实 GL：五个新 kind 的编译 + mask 对拍（统一经红色通道复合）。"""

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
            if key in program:  # 未被引用的 uniform 会被链接器剔除
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

    def test_shader_compiles_with_mask_params(self, ctx) -> None:
        program = ctx.program(
            vertex_shader=_VERT_SRC,
            fragment_shader=GPU_TRANSITION_FRAGMENT_SHADER_SOURCE)
        assert "u_mask_p0" in program
        # u_mask_p1 预留位未被 kind 6-10 引用 → 链接器剔除（合理缺席）；
        # 渲染器侧按存在性守护上传（与 u_center 同纪律）。
        program.release()

    @pytest.mark.parametrize("transition_type,params,kind", [
        ("lib1_venetian_blinds", {"count": 4, "softness": 0.0}, 6),
        ("lib1_checker_reveal", {"rows": 4, "columns": 4, "softness": 0.0}, 7),
        ("lib1_noise_dissolve", {"scale": 8, "seed": 3, "softness": 0.05}, 9),
        ("lib1_halftone_reveal", {"spacing": 8, "angle": 45.0,
                                  "softness": 0.1}, 10),
    ])
    def test_mask_parity(self, ctx, transition_type, params, kind) -> None:
        w, h = 32, 16
        left = np.zeros((h, w, 3), np.float32)
        right = np.ones((h, w, 3), np.float32)
        packed = np.zeros(4, dtype=np.float32)
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            transition_type, 0.45, params)
        packed[:len(spec.mask_params)] = spec.mask_params
        rendered = self._render_transition(ctx, left, right, {
            "u_progress": 0.45, "u_use_procedural": 1,
            "u_transition_kind": kind, "u_direction": 0, "u_reverse": 0,
            "u_angle": 0.0, "u_softness": 0.02, "u_vertex_count": 0,
            "u_effect_mode": 0, "u_transition_mode": 0,
            "u_resample_mode": 0, "u_push_dir": 0,
            "u_center": (0.5, 0.5),
            "u_mask_p0": tuple(float(v) for v in packed),
            "u_mask_p1": (0.0, 0.0, 0.0, 0.0),
        })[..., 0]
        core = getattr(cores, {
            "lib1_venetian_blinds": "venetian_blinds_mask",
            "lib1_checker_reveal": "checker_reveal_mask",
            "lib1_noise_dissolve": "noise_dissolve_mask",
            "lib1_halftone_reveal": "halftone_reveal_mask",
        }[transition_type])
        cpu = core(w, h, 0.45, params)
        np.testing.assert_allclose(np.flipud(rendered), cpu, atol=1e-4)

    def test_luma_mask_parity_content_driven(self, ctx) -> None:
        """kind=8：非对称内容源图验证行序约定 + 内容驱动语义。

        left 非 0/1 → 复合输出 = left·(1−m)+right·m（非 mask 本身）；
        纹理行序与回读帧缓冲行一致（诊断实测），对拍不做 flipud。
        """
        w, h = 16, 8
        xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
        ys = np.linspace(0.0, 1.0, h, dtype=np.float32)
        left = (ys[:, None, None] * 0.7 + xs[None, :, None] * 0.3) \
            * np.ones((1, 1, 3), np.float32)
        right = np.ones((h, w, 3), np.float32)
        packed = np.zeros(4, dtype=np.float32)
        spec = transition_mask_gpu.build_gpu_transition_mask_spec(
            "lib1_luma_reveal", 0.5, {"threshold": 0.5, "span": 0.2})
        packed[:len(spec.mask_params)] = spec.mask_params
        rendered = self._render_transition(ctx, left, right, {
            "u_progress": 0.5, "u_use_procedural": 1,
            "u_transition_kind": 8, "u_direction": 0, "u_reverse": 0,
            "u_angle": 0.0, "u_softness": 0.02, "u_vertex_count": 0,
            "u_effect_mode": 0, "u_transition_mode": 0,
            "u_resample_mode": 0, "u_push_dir": 0,
            "u_center": (0.5, 0.5),
            "u_mask_p0": tuple(float(v) for v in packed),
        })[..., :3]
        cpu_mask = cores.luma_reveal_mask(w, h, 0.5,
                                          {"threshold": 0.5, "span": 0.2},
                                          image_a=left)
        expected = left * (1.0 - cpu_mask[..., None]) + right * cpu_mask[..., None]
        np.testing.assert_allclose(rendered, expected, atol=1e-4)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
