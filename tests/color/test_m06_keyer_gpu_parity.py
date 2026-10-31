"""M06.5 · qualifier GPU(keyer.glsl)↔CPU(qualifier_cpu) 逐像素对拍。

standalone GL 上下文编译真实 keyer.glsl，对合成梯度图（H/S/L 全域
+ HDR 高光 + 边界过渡带）输出 matte，与 ``compute_qualifier_matte``
（numpy 权威移植）逐像素比较——GPU/CPU 同语义的像素级证据。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

pytestmark = pytest.mark.color_core

_KEYER_GLSL = Path(
    "src/features/color/shaders/keyer.glsl").read_text(encoding="utf-8")

_VERTEX = """
#version 330
in vec2 in_vert;
in vec2 in_uv;
out vec2 v_texcoord;
void main() {
    v_texcoord = in_uv;
    gl_Position = vec4(in_vert, 0.0, 1.0);
}
"""

_W, _H = 64, 64


def _synthetic_rgb() -> np.ndarray:
    """H/S/L 梯度 + HDR + 灰轴 + 纯黑的覆盖图。"""
    yy, xx = np.mgrid[0:_H, 0:_W].astype(np.float32)
    h = xx / max(1, _W - 1)
    s = yy / max(1, _H - 1)
    l = 0.5 * np.ones_like(h)
    l[: _H // 4, :] = 0.15     # 暗带
    l[3 * _H // 4:, :] = 0.85  # 亮带
    # HSL → RGB（构造域，精确转换）
    c = (1 - np.abs(2 * l - 1)) * s
    hp = h * 6.0
    x = c * (1 - np.abs(np.mod(hp, 2.0) - 1))
    m = l - c / 2.0
    r = np.select([hp < 1, hp < 2, hp < 3, hp < 4, hp < 5, hp >= 5],
                  [c, x, np.zeros_like(c), np.zeros_like(c), x, c]) + m
    g = np.select([hp < 1, hp < 2, hp < 3, hp < 4, hp < 5, hp >= 5],
                  [x, c, c, x, np.zeros_like(c), np.zeros_like(c)]) + m
    b = np.select([hp < 1, hp < 2, hp < 3, hp < 4, hp < 5, hp >= 5],
                  [np.zeros_like(c), np.zeros_like(c), x, c, c, x]) + m
    rgb = np.stack([r, g, b], axis=-1).astype(np.float32)
    # HDR 列：右缘两列乘 2.5（>1 高光——HDR 归一路径对拍）
    rgb[:, -2:, :] *= 2.5
    # 灰轴与纯黑锚点
    rgb[_H // 2, :, :] = 0.5
    rgb[0, 0, :] = 0.0
    return np.clip(rgb, 0.0, 3.0)


def _run_gpu(ctx, prog, rgb: np.ndarray, qp: dict) -> np.ndarray:
    """上传 RGB（float32 纹理），跑 keyer，读回 matte。"""
    import moderngl
    tex = ctx.texture((_W, _H), 3, rgb.tobytes(), dtype="f4")
    tex.use(0)
    fbo = ctx.simple_framebuffer((_W, _H), components=1, dtype="f4")
    fbo.use()
    ctx.clear()
    center = float(qp.get("hue_center", 0.5) or 0.5)
    # glsl hue 域经 fract(h+0.5) 预移——center 同域换算
    prog["u_input_tex"].value = 0
    prog["u_h_enabled"].value = bool(qp.get("hue_enabled", True))
    prog["u_h_center"].value = (center + 0.5) % 1.0
    prog["u_h_width"].value = float(qp.get("hue_width", 1.0) or 1.0)
    prog["u_h_soft"].value = max(
        float(qp.get("hue_soft", 0.1) or 0.1), 1e-4)
    prog["u_s_enabled"].value = bool(qp.get("sat_enabled", True))
    prog["u_s_low"].value = float(qp.get("sat_low", 0.0) or 0.0)
    prog["u_s_high"].value = float(qp.get("sat_high", 1.0) or 1.0)
    prog["u_s_s_low"].value = max(
        float(qp.get("sat_soft_low", 0.0) or 0.0), 1e-4)
    prog["u_s_s_high"].value = max(
        float(qp.get("sat_soft_high", 0.0) or 0.0), 1e-4)
    prog["u_l_enabled"].value = bool(qp.get("lum_enabled", True))
    prog["u_l_low"].value = float(qp.get("lum_low", 0.0) or 0.0)
    prog["u_l_high"].value = float(qp.get("lum_high", 1.0) or 1.0)
    prog["u_l_s_low"].value = max(
        float(qp.get("lum_soft_low", 0.0) or 0.0), 1e-4)
    prog["u_l_s_high"].value = max(
        float(qp.get("lum_soft_high", 0.0) or 0.0), 1e-4)
    prog["u_invert"].value = bool(qp.get("invert", False))
    vbo = ctx.buffer(np.array(
        [-1, -1, 0, 0, 1, -1, 1, 0, -1, 1, 0, 1, 1, 1, 1, 1],
        dtype="f4").tobytes())
    vao = ctx.vertex_array(prog, vbo, "in_vert", "in_uv")
    vao.render(moderngl.TRIANGLE_STRIP)
    data = fbo.read(components=1, dtype="f4")
    tex.release()
    fbo.release()
    vao.release()
    return np.frombuffer(data, np.float32).reshape(_H, _W)


class TestKeyerGpuCpuParity:
    @pytest.fixture()
    def ctx_prog(self):
        try:
            import moderngl
            ctx = moderngl.create_standalone_context()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"standalone GL unavailable: {exc}")
        prog = ctx.program(
            vertex_shader=_VERTEX, fragment_shader=_KEYER_GLSL)
        yield ctx, prog
        prog.release()
        ctx.release()

    @pytest.mark.parametrize("qp", [
        {"enabled": True, "hue_enabled": True, "hue_center": 0.5,
         "hue_width": 0.4, "hue_soft": 0.05,
         "sat_enabled": False, "lum_enabled": False},
        {"enabled": True, "hue_enabled": False,
         "sat_enabled": True, "sat_low": 0.2, "sat_high": 0.9,
         "sat_soft_low": 0.05, "sat_soft_high": 0.05,
         "lum_enabled": False},
        {"enabled": True, "hue_enabled": False, "sat_enabled": False,
         "lum_enabled": True, "lum_low": 0.2, "lum_high": 0.8,
         "lum_soft_low": 0.05, "lum_soft_high": 0.05},
        {"enabled": True, "hue_enabled": True, "hue_center": 0.1,
         "hue_width": 0.3, "hue_soft": 0.1,
         "sat_enabled": True, "sat_low": 0.1, "sat_high": 1.0,
         "sat_soft_low": 0.05, "sat_soft_high": 0.05,
         "lum_enabled": True, "lum_low": 0.1, "lum_high": 1.0,
         "lum_soft_low": 0.05, "lum_soft_high": 0.05,
         "invert": True},
    ], ids=["hue-only", "sat-band", "lum-band", "all+invert"])
    def test_gpu_matches_cpu(self, ctx_prog, qp):
        from src.features.color.core.qualifier_cpu import (
            compute_qualifier_matte,
        )
        ctx, prog = ctx_prog
        rgb = _synthetic_rgb()
        gpu = _run_gpu(ctx, prog, rgb, qp)
        cpu = compute_qualifier_matte(rgb, qp)[..., 0]
        # 过渡带 smoothstep 数值差（GPU 插值 vs numpy）——全域逐像素
        # 2e-3 内一致；域内值差直方图供诊断
        diff = np.abs(gpu - cpu)
        assert float(np.max(diff)) < 2e-3, (
            float(np.max(diff)), float(np.mean(diff)))
        # 非平凡性：matte 域覆盖两端（不是全 0/全 1 恒等对拍）
        assert float(gpu.min()) < 0.1 and float(gpu.max()) > 0.9
