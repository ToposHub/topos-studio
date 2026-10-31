#!/usr/bin/env python3
"""GPU 去块原型：与 CPU 实现逐位对照 + 每帧耗时对比（判定 GPU 是否值得做）。

输入取 4K 422_Proxy 的真实三平面（Y/U/V，10-bit，qp=77 = 产品 proxy 档实测 qp），
CPU 侧调用产品 dylib 的 tc_deblock_plane（1 线程 / 16 线程），GPU 侧用 R16UI
纹理 + 两次全屏片元 pass（垂直趟 → 水平趟）。算法与 spec v2 逐位等价。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

import moderngl
import numpy as np

REPO = Path("/Users/testuser/Documents/vscode/topos-color-multitask /topos-color02")
sys.path.insert(0, str(REPO))
from src.shared.codec.topos_binding import (  # noqa: E402
    ToposCodec, ToposMovieFile, ToposDecoder, load_library,
)

_LIB = load_library(str(REPO / "native/topos_codec/build/release/topos_codec.dylib"))
_CPU = _LIB.tc_deblock_plane
_CPU.restype = None
_CPU.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint32,
                 ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                 ctypes.c_uint8]
_LIB.tc_dev_set_thread_count.restype = None
_LIB.tc_dev_set_thread_count.argtypes = [ctypes.c_int32]
_LIB.tc_dev_thread_count.restype = ctypes.c_int32
_LIB.tc_qp_scale_tbl.restype = ctypes.c_uint32
_LIB.tc_qp_scale_tbl.argtypes = [ctypes.c_uint32, ctypes.c_uint32]

DEFAULT_THREADS = int(_LIB.tc_dev_thread_count())


def cpu_deblock(plane, qp, table_sel, bd, threads):
    out = np.ascontiguousarray(plane.copy())
    _LIB.tc_dev_set_thread_count(int(threads))
    try:
        _CPU(out.ctypes.data, out.shape[1], out.shape[1], out.shape[0],
             qp, table_sel, bd)
    finally:
        _LIB.tc_dev_set_thread_count(DEFAULT_THREADS)
    return out


def blockiness(plane) -> float:
    """8 网格边界台阶（确认滤波确实生效）。"""
    f = plane.astype(np.float64)
    cols = np.arange(8, f.shape[1] - 1, 8)
    step = np.abs(f[:, cols] - f[:, cols - 1]).mean()
    inner = np.abs(np.diff(f, axis=1)[:, np.arange(3, f.shape[1] - 2, 8)]).mean()
    return float(step - inner)


VS = """
#version 410 core
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""

FS_COMMON = """
#version 410 core
uniform usampler2D u_plane;
uniform int u_alpha, u_beta, u_clip, u_maxv, u_spread;
layout(location = 0) out uvec4 o_color;

int sar(int v, int s) {          // C 的算术右移（负数向下取整）
    if (v >= 0) { return v >> s; }
    return -(((-v) + (1 << s) - 1) >> s);
}
int iabs(int v) { return v < 0 ? -v : v; }
int clampv(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }
bool gate(int p1, int p0, int q0, int q1) {
    return iabs(q0 - p0) < u_alpha && iabs(p1 - p0) < u_beta
        && iabs(q1 - q0) < u_beta;
}
int delta(int p1, int p0, int q0, int q1) {
    int d = sar(12 * (q0 - p0) + 4 * (p1 - q1) + 8, 4);
    int dd = q0 - p0;
    int hd = (dd >= 0) ? (dd >> 1) : -((-dd) >> 1);
    if (hd >= 0) { if (d > hd) d = hd; if (d < 0) d = 0; }
    else { if (d < hd) d = hd; if (d > 0) d = 0; }
    return clampv(d, -u_clip, u_clip);
}
/* 本像素相对边界的位置 lo：-2=p1 -1=p0 0=q0 1=q1 */
int adjust(int d, int lo) {
    if (lo == -1) { return d; }
    if (lo == 0)  { return -d; }
    int s = sar(d, u_spread);
    return (lo == -2) ? s : -s;
}
"""

FS_V = FS_COMMON + """
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    int w = textureSize(u_plane, 0).x;
    /* 角色由列内偏移直接判定：m=0→q0(x0=c) m=7→p0(x0=c+1) m=6→p1(x0=c+2)
     * m=1→q1(x0=c−1)，其余列不受任何边界窗覆盖（窗两两不相交）。 */
    int m = p.x & 7;
    int x0 = p.x - (m == 0 ? 0 : (m == 7 ? 1 : (m == 6 ? 2 : (m == 1 ? -1 : 99))));
    int cur = int(texelFetch(u_plane, p, 0).r);
    if (x0 < 8 || x0 + 1 >= w) { o_color = uvec4(uint(cur)); return; }
    int p1 = int(texelFetch(u_plane, ivec2(x0 - 2, p.y), 0).r);
    int p0 = int(texelFetch(u_plane, ivec2(x0 - 1, p.y), 0).r);
    int q0 = int(texelFetch(u_plane, ivec2(x0,     p.y), 0).r);
    int q1 = int(texelFetch(u_plane, ivec2(x0 + 1, p.y), 0).r);
    if (gate(p1, p0, q0, q1)) {
        cur = clampv(cur + adjust(delta(p1, p0, q0, q1), p.x - x0), 0, u_maxv);
    }
    o_color = uvec4(uint(cur));
}
"""

FS_H = FS_COMMON + """
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    int h = textureSize(u_plane, 0).y;
    int m = p.y & 7;
    int y0 = p.y - (m == 0 ? 0 : (m == 7 ? 1 : (m == 6 ? 2 : (m == 1 ? -1 : 99))));
    int cur = int(texelFetch(u_plane, p, 0).r);
    if (y0 < 8 || y0 + 1 >= h) { o_color = uvec4(uint(cur)); return; }
    int p1 = int(texelFetch(u_plane, ivec2(p.x, y0 - 2), 0).r);
    int p0 = int(texelFetch(u_plane, ivec2(p.x, y0 - 1), 0).r);
    int q0 = int(texelFetch(u_plane, ivec2(p.x, y0    ), 0).r);
    int q1 = int(texelFetch(u_plane, ivec2(p.x, y0 + 1), 0).r);
    if (gate(p1, p0, q0, q1)) {
        cur = clampv(cur + adjust(delta(p1, p0, q0, q1), p.y - y0), 0, u_maxv);
    }
    o_color = uvec4(uint(cur));
}
"""


class GpuDeblock:
    def __init__(self, ctx, w, h, uniforms):
        self.ctx, self.w, self.h = ctx, w, h
        self.tex = [ctx.texture((w, h), 1, dtype="u2") for _ in range(3)]
        for t in self.tex:
            t.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self.fbo = [ctx.framebuffer(color_attachments=[t]) for t in self.tex]
        self.prog = {"v": ctx.program(vertex_shader=VS, fragment_shader=FS_V),
                     "h": ctx.program(vertex_shader=VS, fragment_shader=FS_H)}
        self.vao = {k: ctx.vertex_array(p, []) for k, p in self.prog.items()}
        self.uniforms = uniforms

    def _pass(self, kind, src, dst):
        prog = self.prog[kind]
        self.tex[src].use(src)
        prog["u_plane"] = src
        for k, v in self.uniforms.items():
            prog[k] = v
        self.fbo[dst].use()
        self.ctx.viewport = (0, 0, self.w, self.h)
        self.vao[kind].render(moderngl.TRIANGLES, vertices=3)

    def run(self, plane, download=True):
        self.tex[0].write(np.ascontiguousarray(plane).tobytes())
        self._pass("v", 0, 1)
        self._pass("h", 1, 2)
        if not download:
            return None
        raw = self.fbo[2].read(components=1, dtype="u2")
        return np.frombuffer(raw, dtype="<u2").reshape(self.h, self.w)


def main() -> int:
    codec = ToposCodec()
    movie = ToposMovieFile(
        codec, str(REPO / "tests/test_videos/4K_topos_422_Proxy.mov"))
    dec = ToposDecoder(codec)
    try:
        buf, sz = movie.packet_into(0)
        fi = dec.prepare(buf, sz)
        planes = [np.empty(codec.plane_geometry(fi, k)[::-1], dtype="<u2")
                  for k in range(int(fi.plane_count))]
        views = dec.make_views(planes)
        dec.decode_views(buf, views, int(fi.plane_count), sz)
        planes = [np.array(p) for p in planes]
    finally:
        dec.close()
        movie.close()

    qp, table_sel, bd = 77, 0, 10
    scale = int(_LIB.tc_qp_scale_tbl(table_sel, qp))
    step = max(1, (scale * 3 + 173056 // 2) // 173056)
    alpha = min(48, max(3, step * 2))
    beta, clip = alpha // 2 + 2, alpha // 2
    print(f"4K 422_Proxy 帧 0：Y{planes[0].shape} U{planes[1].shape} "
          f"V{planes[2].shape}，qp={qp} → alpha={alpha} beta={beta} clip={clip}"
          f"，滤波前块台阶={blockiness(planes[0]):.3f}")

    cpu_out = {}
    for th in (1, 16):
        res = [cpu_deblock(p, qp, table_sel, bd, th) for p in planes]
        cpu_out[th] = res
        n = 3 if th == 1 else 10
        t0 = time.perf_counter()
        for _ in range(n):
            for p in planes:
                cpu_deblock(p, qp, table_sel, bd, th)
        dt = (time.perf_counter() - t0) / n
        t0 = time.perf_counter()
        for _ in range(n):
            for p in planes:
                np.ascontiguousarray(p.copy())
        cp = (time.perf_counter() - t0) / n
        print(f"CPU {th:2d} 线程：{dt * 1000:7.2f} ms/帧（三平面，含拷贝 "
              f"{cp * 1000:.2f} ms）  滤波后块台阶={blockiness(res[0]):.3f}")
    cpu_ref = cpu_out[1]

    ctx = moderngl.create_standalone_context(require=410)
    print(f"GPU：{ctx.info['GL_RENDERER']} / GL {ctx.info['GL_VERSION']}")
    uni = {"u_alpha": alpha, "u_beta": beta, "u_clip": clip,
           "u_maxv": (1 << bd) - 1, "u_spread": 1}
    blocks = {}

    def gpu_for(plane):
        key = (plane.shape[1], plane.shape[0])
        if key not in blocks:
            blocks[key] = GpuDeblock(ctx, key[0], key[1], uni)
        return blocks[key]

    total_diff = 0
    for i, p in enumerate(planes):
        gpu_out = gpu_for(p).run(p)
        d = int((gpu_out != cpu_ref[i]).sum())
        total_diff += d
        if d:
            for yy, xx in np.argwhere(gpu_out != cpu_ref[i])[:3]:
                print(f"   平面{i} ({yy},{xx}) gpu={gpu_out[yy, xx]} "
                      f"cpu={cpu_ref[i][yy, xx]} 输入={p[yy, xx]}")
    print("GPU vs CPU 逐位："
          + ("完全一致 ✓（三平面）" if total_diff == 0
             else f"差异 {total_diff} ✗"))

    FS_PASS = """#version 410 core
uniform usampler2D u_plane;
layout(location = 0) out uvec4 o_color;
void main() { o_color = uvec4(texelFetch(u_plane, ivec2(gl_FragCoord.xy), 0).r); }
"""
    pass_prog = ctx.program(vertex_shader=VS, fragment_shader=FS_PASS)
    pass_vao = ctx.vertex_array(pass_prog, [])
    y4k = planes[0]
    t = ctx.texture((y4k.shape[1], y4k.shape[0]), 1, dtype="u2")
    t.filter = (moderngl.NEAREST, moderngl.NEAREST)
    f2 = ctx.framebuffer(color_attachments=[t])
    t2 = ctx.texture((y4k.shape[1], y4k.shape[0]), 1, dtype="u2")
    t.write(np.ascontiguousarray(y4k).tobytes())
    tt = ctx.texture((y4k.shape[1], y4k.shape[0]), 1, dtype="u2")
    f3 = ctx.framebuffer(color_attachments=[tt])
    pass_prog["u_plane"] = 0
    t.use(0)
    ctx.viewport = (0, 0, y4k.shape[1], y4k.shape[0])
    for _ in range(3):
        f3.use(); pass_vao.render(moderngl.TRIANGLES, vertices=3)
    ctx.finish()
    t0 = time.perf_counter()
    for _ in range(30):
        f3.use(); pass_vao.render(moderngl.TRIANGLES, vertices=3)
    ctx.finish()
    print(f"控制：单趟直通 pass（4K Y 单平面，同 fragment 数）"
          f"{(time.perf_counter() - t0) / 30 * 1000:7.2f} ms/趟")
    t2.release(); tt.release(); f2.release(); f3.release()

    for _ in range(3):
        for p in planes:
            gpu_for(p).run(p, download=False)
    t0 = time.perf_counter()
    for _ in range(30):
        for p in planes:
            gpu_for(p).run(p, download=False)
    ctx.finish()
    print(f"GPU 两趟 pass：{(time.perf_counter() - t0) / 30 * 1000:7.2f} ms/帧"
          f"（三平面，不含上传/回读）")

    t0 = time.perf_counter()
    for _ in range(30):
        for p in planes:
            gpu_for(p).run(p, download=True)
    ctx.finish()
    print(f"GPU 含上传+回读：{(time.perf_counter() - t0) / 30 * 1000:7.2f} ms/帧")
    ctx.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
