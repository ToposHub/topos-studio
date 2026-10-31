#!/usr/bin/env python3
"""微基准：合成页 blur_separable shader 在 1080p 上的纯 GPU 成本。

隔离变量：standalone moderngl context + shaders.py 原样 shader，
测不同 radius/dtype/纹理格式的单帧 H+V 耗时。
"""
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import moderngl
import numpy as np
from src.features.composite.core.shaders import (
    _BLUR_SEPARABLE_H_FRAGMENT, _BLUR_SEPARABLE_V_FRAGMENT)

VERT = """
#version 330
in vec2 in_vert;
in vec2 in_texcoord;
out vec2 v_uv;
void main() {
    v_uv = in_texcoord;
    gl_Position = vec4(in_vert, 0.0, 1.0);
}
"""

W, H = 1920, 1080

ctx = moderngl.create_standalone_context()
prog_h = ctx.program(vertex_shader=VERT, fragment_shader=_BLUR_SEPARABLE_H_FRAGMENT)
prog_v = ctx.program(vertex_shader=VERT, fragment_shader=_BLUR_SEPARABLE_V_FRAGMENT)

quad = ctx.vertex_array(
    prog_h,
    [(ctx.buffer(np.array([-1, -1, 1, -1, -1, 1, 3, -1, -1, 1, 3, 1], dtype=np.float32)
                 .tobytes()), '2f', 'in_vert')],
)
# 用 fullscreen triangle 更简单：单独 vbo for uv
verts = np.array([-1, -1, 0, 0,  3, -1, 2, 0,  -1, 3, 0, 2], dtype=np.float32)
vbo = ctx.buffer(verts.tobytes())
vao = ctx.vertex_array(prog_h, [(vbo, '2f 2f', 'in_vert', 'in_texcoord')])


def bench(dtype: str, radius: float, kernel_mode: int, n=30):
    src = ctx.texture((W, H), 4, dtype=dtype)
    src.filter = (moderngl.LINEAR, moderngl.LINEAR)
    tmp = ctx.texture((W, H), 4, dtype=dtype)
    out = ctx.texture((W, H), 4, dtype=dtype)
    fbo_tmp = ctx.framebuffer([tmp])
    fbo_out = ctx.framebuffer([out])

    def one_pass():
        fbo_tmp.use()
        ctx.viewport = (0, 0, W, H)
        src.use(0)
        prog_h['u_texture'].value = 0
        prog_h['u_resolution'].value = (W, H)
        prog_h['u_radius'].value = radius
        prog_h['u_kernel_mode'].value = kernel_mode
        prog_h['u_edge_policy'].value = 0
        vao.render(moderngl.TRIANGLES)

        fbo_out.use()
        tmp.use(0)
        prog_v['u_texture'].value = 0
        prog_v['u_resolution'].value = (W, H)
        prog_v['u_radius'].value = radius
        prog_v['u_kernel_mode'].value = kernel_mode
        prog_v['u_channel_mask'].value = (1.0, 1.0, 1.0, 0.0)
        prog_v['u_mix'].value = 1.0
        prog_v['u_edge_policy'].value = 0
        vao.render(moderngl.TRIANGLES)

    for _ in range(5):
        one_pass()
    ctx.finish()
    t0 = time.perf_counter()
    for _ in range(n):
        one_pass()
    ctx.finish()
    ms = (time.perf_counter() - t0) / n * 1000
    src.release(); tmp.release(); out.release()
    fbo_tmp.release(); fbo_out.release()
    return ms


print(f"{'dtype':>5} {'mode':>7} {'radius':>7} {'ms/frame':>9}")
for dtype in ('f2', 'f4'):
    for mode, name in ((0, 'box'), (1, 'gauss')):
        for radius in (6.0, 20.0, 60.0):
            ms = bench(dtype, radius, mode)
            print(f"{dtype:>5} {name:>7} {radius:>7.0f} {ms:>9.2f}")
