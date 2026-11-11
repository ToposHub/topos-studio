# -*- coding: utf-8 -*-
"""CW-B03:CPU 参考实现 ↔ GPU(GLSL)真实执行 parity + MSL 源 parity。

GLSL:用 moderngl standalone 上下文编译**生产源码**(shader_nodes 的
COMMON_DEFINITIONS + WARPER_SNIPPET),对 B03 冻结 fixture 全色执行
apply_warper,与 CPU 镜像逐点比对(容差=fp32 量级)。无 GL 环境时 skip
并注明原因——skip 不算跨平台验收完成(计划 §5.2)。

MSL:本机无 Metal 执行入口,按源级 parity 断言(常量/算子链与 GLSL
一致);真实 Metal 数值 parity 留待 B08 Metal 平台矩阵。
"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.shader_nodes import COMMON_DEFINITIONS, WARPER_SNIPPET
from src.features.color.core.warper_color import apply_warper_legacy_hsv

from tests.color.test_warper_color import COLORS

# 与生产纹理一致:RG32F、LINEAR、CLAMP_TO_EDGE
GRID_SIZE = 8
RNG = np.random.default_rng(20261112)


def _make_grid(identity: bool) -> np.ndarray:
    if identity:
        return np.zeros((GRID_SIZE, GRID_SIZE, 2), dtype=np.float32)
    grid = RNG.normal(0.0, 0.15, (GRID_SIZE, GRID_SIZE, 2)).astype(np.float32)
    grid[..., 0] = np.clip(grid[..., 0], -0.3, 0.3)   # Δhue
    grid[..., 1] = np.clip(grid[..., 1], -0.4, 0.4)   # Δsat
    return np.ascontiguousarray(grid)


def _gpu_apply(colors: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """在生产 GLSL 源码上真实执行 apply_warper。"""
    moderngl = pytest.importorskip("moderngl")
    try:
        ctx = moderngl.create_context(standalone=True)
    except Exception as exc:  # 无 GL 环境(CI/无头):如实 skip
        pytest.skip(f"no standalone GL context: {exc}")

    fragment = "#version 330\n" + COMMON_DEFINITIONS + WARPER_SNIPPET + """
uniform sampler2D u_warper_tex;
uniform sampler2D u_colors;
out vec4 out_color;
void main() {
    vec3 c = texelFetch(u_colors, ivec2(int(gl_FragCoord.x), 0), 0).rgb;
    out_color = vec4(apply_warper(c, u_warper_tex, 8.0), 1.0);
}
"""
    vertex = """
#version 330
in vec2 in_pos;
void main() { gl_Position = vec4(in_pos, 0.0, 1.0); }
"""
    program = ctx.program(vertex_shader=vertex, fragment_shader=fragment)
    quad = ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype=np.float32).tobytes())
    vao = ctx.vertex_array(program, [(quad, "2f", "in_pos")])

    warper_tex = ctx.texture((GRID_SIZE, GRID_SIZE), 2, grid.tobytes(), dtype="f4")
    warper_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    warper_tex.repeat_x = warper_tex.repeat_y = False
    colors_tex = ctx.texture((len(colors), 1), 4,
                             np.ascontiguousarray(
                                 np.concatenate([colors, np.ones((len(colors), 1))], axis=1,
                                                dtype=np.float32)).tobytes(), dtype="f4")
    colors_tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
    fbo = ctx.simple_framebuffer((len(colors), 1), components=4, dtype="f4")
    fbo.use()
    warper_tex.use(0)
    program["u_warper_tex"].value = 0
    colors_tex.use(1)
    program["u_colors"].value = 1
    vao.render(moderngl.TRIANGLE_STRIP)

    result = np.frombuffer(fbo.read(components=4, dtype="f4"), dtype=np.float32).reshape(1, len(colors), 4)
    warper_tex.release()
    colors_tex.release()
    fbo.release()
    quad.release()
    vao.release()
    program.release()
    ctx.release()
    return result[0, :, :3].copy()


@pytest.mark.parametrize("identity_grid", [True, False], ids=["identity", "warping"])
def test_gpu_glsl_matches_cpu_reference(identity_grid: bool) -> None:
    grid = _make_grid(identity_grid)
    colors = np.array(COLORS, dtype=np.float32)
    gpu = _gpu_apply(colors, grid)
    # 实测本机(AMD RX 6800/GL 4.1)fp32 噪声:warping 网格下最大
    # abs ≈1.2e-3、rel ≈7.7e-3(hue→fract→clamp 链的 fp32 放大,均匀
    # 分布、无离群)。语义错误特征是 O(0.1+) 的孤点差;容差取噪声
    # 上界的 1.6 倍并另加全局上限兜底。
    max_abs = 0.0
    for index, color in enumerate(COLORS):
        cpu = apply_warper_legacy_hsv(color, grid)
        max_abs = max(max_abs, float(np.abs(gpu[index] - cpu).max()))
        np.testing.assert_allclose(
            gpu[index], cpu, rtol=1e-2, atol=2e-3,
            err_msg=f"GLSL/CPU parity {color}")
    assert max_abs < 2e-3, f"aggregate fp32 noise bound exceeded: {max_abs}"


def test_gpu_identity_grid_returns_inputs() -> None:
    grid = _make_grid(identity=True)
    colors = np.array(COLORS, dtype=np.float32)
    gpu = _gpu_apply(colors, grid)
    for index, color in enumerate(COLORS):
        # sat<0.02 的输入原样;其余经 rgb→hsv→rgb fp32 往返(ε 量级差)
        np.testing.assert_allclose(gpu[index], color, rtol=1e-4, atol=1e-5)


def test_msl_source_parity_with_glsl() -> None:
    """MSL 无本机执行入口:源级 parity(同常量/同算子链/同防扩缩)。"""
    from src.features.color.core.shader_nodes_msl import MSL_WARPER

    for constant in ("0.02", "0.15"):
        assert constant in WARPER_SNIPPET and constant in MSL_WARPER, constant
    for token in ("sign(", "abs(", "rgb2hsv", "hsv2rgb", "smoothstep", "fract", "clamp"):
        assert token in WARPER_SNIPPET, token
        msl_token = {"rgb2hsv": "msl_rgb2hsv", "hsv2rgb": "msl_hsv2rgb",
                     "sign(": "sign(", "fract": "fract"}.get(token, token)
        assert msl_token.replace("fract", "fract") in MSL_WARPER, msl_token
    # 输出 sat clamp 与 hue fract 两端一致
    assert "clamp(" in MSL_WARPER and "fract(" in MSL_WARPER
    # grid_size 形参在两端都保留 ABI 且未参与数值
    assert "grid_size" in WARPER_SNIPPET and "grid_size" in MSL_WARPER
