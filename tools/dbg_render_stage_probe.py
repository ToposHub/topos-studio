#!/usr/bin/env python3
"""导出 render 段分解 + GPU 合成原型探针（P0 可行性实测）。

背景：1080p→4K HQ 导出实测 2.22 fps，render 段占 ~92% 墙钟（~414ms/帧）。
本探针在脱离 GUI 的前提下复刻 timeline_export._composite_layers 单视频层
CPU 链（extract→grade→resize→normalise→blend→quantize），逐段计时；并用
ExportVideoDenoiseBridge 同款 standalone moderngl context 搭 GPU 原型
（u16 平面上传 → YUV→RGB pass@源分辨率 → bilinear+grade pass@输出分辨率
→ u8 回读），成对对比吞吐与一致性。

CPU 侧 grade 用真实产品函数 GradingMath.apply_grading（与导出/预览 CPU
路径同一实现）；GPU 侧 grade shader 为 apply_grading 的 ALU 等价移植
（仅用于负载测量，产品实现应复用预览侧已有 parity 的 grade shader）。

暴露的段（CPU）：
  extract   = 平面 yuv422p10le → BGR u8（_convert_yuv_planes_to_bgr 等价）
  grade     = _apply_color_grade 等价（真实 GradingMath）
  resize    = cv2.resize INTER_LINEAR 1080p→4K（_resize_frame 等价）
  normalise = 4K u8→float32 [0,1]（_normalise_bgr_frame 等价）
  blend     = 单层 lerp 合成（_composite_layers 混合段等价）
  quantize  = float32→u8（_quantize_export_frame 等价）

GPU 变体：
  gpu-yuv   = u16 平面上传 + pass1(YUV→RGB@1080p) + pass2(bilinear+grade@4K) + 回读
  gpu-bgr   = CPU extract 保留，BGR u8 上传 + 单 pass(bilinear+grade@4K→BGR) + 回读

用法：
    python tools/dbg_render_stage_probe.py [--frames 24] [--src-h 1080]
        [--out-w 3840] [--out-h 2160] [--no-gpu]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# ── 调色参数档（复刻导出口径的 primary grade 子集）──
GRADE_TYPICAL = {
    "exposure": 8.0, "temp": 4.0, "tint": -2.0, "contrast": 6.0,
    "lift": (1.5, 0.0, -1.5), "gamma": (0.0, 2.0, 0.0),
    "gain": (2.0, 0.0, -2.0), "shadow": -8.0, "highlights": 6.0,
    "vibrance": 10.0, "saturation": 5.0,
}
GRADE_HEAVY = dict(GRADE_TYPICAL, hue_shift=15.0)  # +RGB↔HSV 双往返


def make_source(src_w: int, src_h: int):
    """合成 1080p yuv422p10le 平面（梯度+正弦+噪声，内容密度足够）。"""
    rng = np.random.default_rng(7)
    xs = np.linspace(0.0, 1.0, src_w, dtype=np.float32)
    ys = np.linspace(0.0, 1.0, src_h, dtype=np.float32)
    xg, yg = np.meshgrid(xs, ys)
    base = (0.5 + 0.25 * np.sin(12 * xg * 3.14159) * np.cos(9 * yg * 3.14159)
            + 0.08 * rng.standard_normal((src_h, src_w)).astype(np.float32))
    u_base = 0.5 + 0.2 * np.sin(7 * xg * 3.14159 + 1.0)
    v_base = 0.5 + 0.2 * np.cos(5 * yg * 3.14159 + 0.5)
    y16 = np.rint(np.clip(base, 0.0, 1.0) * 876.0 + 64.0).astype(np.uint16)
    u16 = np.rint(np.clip(u_base, 0.0, 1.0) * 896.0 + 512.0).astype(np.uint16)[:, ::2]
    v16 = np.rint(np.clip(v_base, 0.0, 1.0) * 896.0 + 512.0).astype(np.uint16)[:, ::2]
    return np.ascontiguousarray(y16), np.ascontiguousarray(u16), np.ascontiguousarray(v16)


# ═══════════════════════ CPU 链（复刻 timeline_export 单层路径）═══════════════════════

def cpu_extract(y: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """平面 yuv422p10le(bt709 limited) → BGR u8，同 dbg_export_packed_probe 的口径。"""
    h, w = y.shape
    uf = np.repeat(u, 2, axis=1)[:, :w].astype(np.float32)
    vf = np.repeat(v, 2, axis=1)[:, :w].astype(np.float32)
    yf = y.astype(np.float32)
    yn = (yf - 64.0) / 876.0
    un = (uf - 512.0) / 896.0
    vn = (vf - 512.0) / 896.0
    r = yn + 1.5748 * vn
    g = yn - 0.1873 * un - 0.4681 * vn
    b = yn + 1.8556 * un
    bgr = np.stack([b, g, r], axis=-1)
    np.clip(bgr, 0.0, 1.0, out=bgr)
    return np.rint(bgr * 255.0).astype(np.uint8)


def cpu_grade(bgr_u8: np.ndarray, params: dict) -> np.ndarray:
    """_apply_color_grade 等价：BGR u8 → RGB f32 → 真实 GradingMath → BGR u8。"""
    from src.features.color.core.the_decompiler import GradingMath

    rgb = bgr_u8[..., ::-1].astype(np.float32) / 255.0
    graded = GradingMath.apply_grading(rgb.reshape(-1, 3), params).reshape(rgb.shape)
    result = bgr_u8.copy()
    result[..., :3] = np.rint(
        np.clip(graded[..., ::-1], 0.0, 1.0) * 255.0).astype(np.uint8)
    return result


def run_cpu_chain(y16, u16, v16, out_w, out_h, grade_params, frames, warmup,
                  collect=None):
    """逐段计时跑 CPU 链；collect(frame_idx)->np.ndarray 收集输出做一致性对比。"""
    acc = {k: 0.0 for k in
           ("extract", "grade", "resize", "normalise", "blend", "quantize")}
    out_frames = {}
    n_total = frames + warmup
    t_chain0 = time.perf_counter()
    for i in range(n_total):
        t0 = time.perf_counter_ns()
        bgr = cpu_extract(y16, u16, v16)
        t1 = time.perf_counter_ns()
        if grade_params is not None:
            bgr = cpu_grade(bgr, grade_params)
        t2 = time.perf_counter_ns()
        import cv2
        resized = cv2.resize(bgr, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
        t3 = time.perf_counter_ns()
        layer_float = resized.astype(np.float32) / 255.0
        t4 = time.perf_counter_ns()
        composite = layer_float * 1.0  # 单层首层路径（opacity=1）
        t5 = time.perf_counter_ns()
        final = np.rint(np.clip(composite, 0.0, 1.0) * 255.0).astype(np.uint8)
        t6 = time.perf_counter_ns()
        if i >= warmup:
            acc["extract"] += (t1 - t0) / 1e6
            acc["grade"] += (t2 - t1) / 1e6
            acc["resize"] += (t3 - t2) / 1e6
            acc["normalise"] += (t4 - t3) / 1e6
            acc["blend"] += (t5 - t4) / 1e6
            acc["quantize"] += (t6 - t5) / 1e6
            if collect is not None and i in collect:
                out_frames[i] = final.copy()
    chain_ms = (time.perf_counter() - t_chain0) / n_total * 1000
    return {k: v / frames for k, v in acc.items()}, chain_ms, out_frames


# ═══════════════════════ GPU 原型（standalone moderngl）═══════════════════════

_VS = """
#version 330
in vec2 in_pos;
out vec2 v_uv;
void main() {
    v_uv = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

# pass1：u16 平面 yuv422p10le(bt709 limited) → RGB u8 @ 源分辨率
# chroma 水平最邻近（与 CPU np.repeat 口径一致）
_FS_YUV = """
#version 330
in vec2 v_uv;
uniform usampler2D tex_y;
uniform usampler2D tex_u;
uniform usampler2D tex_v;
out vec4 out_color;
void main() {
    ivec2 ysz = textureSize(tex_y, 0);
    ivec2 csz = textureSize(tex_u, 0);
    ivec2 yp = clamp(ivec2(vec2(v_uv.x * float(ysz.x), v_uv.y * float(ysz.y))),
                     ivec2(0), ysz - ivec2(1));
    int cx = clamp(int(v_uv.x * float(csz.x)), 0, csz.x - 1);
    int cy = clamp(yp.y, 0, csz.y - 1);
    float yn = (float(texelFetch(tex_y, yp, 0).r) - 64.0) / 876.0;
    float un = (float(texelFetch(tex_u, ivec2(cx, cy), 0).r) - 512.0) / 896.0;
    float vn = (float(texelFetch(tex_v, ivec2(cx, cy), 0).r) - 512.0) / 896.0;
    float r = yn + 1.5748 * vn;
    float g = yn - 0.1873 * un - 0.4681 * vn;
    float b = yn + 1.8556 * un;
    out_color = vec4(clamp(vec3(r, g, b), 0.0, 1.0), 1.0);
}
"""

# pass2：bilinear 采样(LINEAR) + grade(ALU 等价移植) → BGR u8 @ 输出分辨率
_FS_GRADE = """
#version 330
in vec2 v_uv;
uniform sampler2D tex_rgb;
uniform int u_grade;
out vec4 out_color;

vec3 params_to_rgb(float hue, float sat, float bright) {
    float stops = (bright * bright) * 6.0;
    if (bright < 0.0) { stops = -stops; }
    float b_mult = pow(2.0, stops);
    if (sat == 0.0) { return vec3(b_mult); }
    float h = hue / 60.0;
    float fi = floor(h);
    float f = h - fi;
    float p = 1.0 * (1.0 - sat);
    float q = 1.0 * (1.0 - sat * f);
    float t = 1.0 * (1.0 - sat * (1.0 - f));
    float r, g, b;
    int ii = int(mod(fi, 6.0));
    if (ii == 0)      { r = 1.0; g = t;   b = p; }
    else if (ii == 1) { r = q;   g = 1.0; b = p; }
    else if (ii == 2) { r = p;   g = 1.0; b = t; }
    else if (ii == 3) { r = p;   g = q;   b = 1.0; }
    else if (ii == 4) { r = t;   g = p;   b = 1.0; }
    else              { r = 1.0; g = p;   b = q; }
    float avg = max(1e-6, (r + g + b) / 3.0);
    return vec3(r / avg * b_mult, g / avg * b_mult, b / avg * b_mult);
}

vec3 apply_grade(vec3 res) {
    // 1. exposure
    float exposure = 8.0;
    float exp_stops = (exposure / 100.0) * (exposure / 100.0) * 6.0;
    res *= pow(2.0, exp_stops);
    // 2. white balance
    float temp = 4.0, tint = -2.0;
    res *= vec3(1.0 + temp / 200.0, 1.0 - tint / 200.0, 1.0 - temp / 200.0);
    // 3. contrast
    float c_factor = 1.0 + 6.0 / 100.0;
    res = sign(res) * pow(abs(res) / 0.18 + 1e-10, vec3(c_factor)) * 0.18;
    // 4. lift / gain / gamma
    vec3 l_rgb = params_to_rgb(1.5, 0.0, -1.5);
    vec3 gn_rgb = params_to_rgb(2.0, 0.0, -2.0);
    vec3 l_adj = (l_rgb - 1.0) * 0.2;
    res = res * gn_rgb * (1.0 - l_adj) + l_adj;
    vec3 gm_rgb = params_to_rgb(0.0, 2.0, 0.0);
    vec3 gm_pow = pow(vec3(2.0), -log2(max(gm_rgb, vec3(0.01))));
    bvec3 gt0 = greaterThan(res, vec3(0.0));
    bvec3 le1 = lessThanEqual(res, vec3(1.0));
    bvec3 mid = bvec3(gt0.x && le1.x, gt0.y && le1.y, gt0.z && le1.z);
    bvec3 high = greaterThan(res, vec3(1.0));
    vec3 lo_b = gm_pow * res;
    vec3 md_b = pow(max(res, vec3(1e-10)), gm_pow);
    vec3 hi_b = gm_pow * (res - 1.0) + 1.0;
    res = mix(mix(lo_b, md_b, vec3(mid)), hi_b, vec3(high));
    // 5. shadow
    float sf = 1.0 + (-8.0) / 100.0;
    vec3 cl = clamp(res, 0.0, 1.0);
    res = pow(cl, vec3(1.0 / sf)) + (res - cl);
    // 6. highlights（Reinhard，逐像素 max）
    float hl = 6.0;
    float max_c = max(max(res.r, res.g), res.b);
    if (max_c > 0.1) {
        float mapped;
        float k = (hl / 100.0) * 0.5;
        mapped = 0.1 + (max_c - 0.1) * (1.0 + k);
        res *= mapped / max_c;
    }
    // 7. vibrance
    float gray = dot(res, vec3(0.299, 0.587, 0.114));
    float mv = max(max(res.r, res.g), res.b);
    float boost = (10.0 / 100.0) * (1.0 - clamp(mv - gray, 0.0, 1.0));
    res = res * (1.0 + boost) + gray * (-boost);
    // 8. saturation
    float gray2 = dot(res, vec3(0.299, 0.587, 0.114));
    res = gray2 + (res - gray2) * (1.0 + 5.0 / 100.0);
    return res;
}

void main() {
    vec3 rgb = texture(tex_rgb, v_uv).rgb;
    if (u_grade == 1) { rgb = apply_grade(rgb); }
    out_color = vec4(rgb.b, rgb.g, rgb.r, 1.0);
}
"""


class GpuExportProto:
    """standalone moderngl 双 pass 导出合成原型（本线程独占 context）。"""

    def __init__(self, src_w: int, src_h: int, out_w: int, out_h: int):
        import moderngl

        self.ctx = moderngl.create_standalone_context(require=330)
        self.src_w, self.src_h = src_w, src_h
        self.out_w, self.out_h = out_w, out_h

        self.tex_y = self.ctx.texture((src_w, src_h), 1, dtype="u2")
        self.tex_u = self.ctx.texture((src_w // 2, src_h), 1, dtype="u2")
        self.tex_v = self.ctx.texture((src_w // 2, src_h), 1, dtype="u2")
        for t in (self.tex_y, self.tex_u, self.tex_v):
            t.filter = (moderngl.NEAREST, moderngl.NEAREST)
            t.repeat_x = t.repeat_y = False

        self.tex_mid = self.ctx.texture((src_w, src_h), 3, dtype="f1")
        self.tex_mid.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.tex_mid.repeat_x = self.tex_mid.repeat_y = False
        self.fbo_mid = self.ctx.framebuffer(self.tex_mid)

        self.tex_out = self.ctx.texture((out_w, out_h), 3, dtype="f1")
        self.fbo_out = self.ctx.framebuffer(self.tex_out)

        self.prog_yuv = self.ctx.program(
            vertex_shader=_VS, fragment_shader=_FS_YUV)
        self.prog_grade = self.ctx.program(
            vertex_shader=_VS, fragment_shader=_FS_GRADE)
        # 注：program 复用同一 vs 源，各自持有编译实例
        self.vao_yuv = self._vao(self.prog_yuv)
        self.vao_grade = self._vao(self.prog_grade)

    def _vao(self, prog):
        vbo = self.ctx.buffer(
            np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype=np.float32).tobytes())
        return self.ctx.vertex_array(prog, [(vbo, "2f", "in_pos")])

    def frame_yuv(self, y16, u16, v16, graded: bool, out: np.ndarray = None):
        """u16 平面 → 完整 GPU 链 → 回读 BGR u8 (out_w, out_h, 3)。"""
        import moderngl

        t = {}
        t0 = time.perf_counter_ns()
        self.tex_y.write(y16.tobytes())
        self.tex_u.write(u16.tobytes())
        self.tex_v.write(v16.tobytes())
        t1 = time.perf_counter_ns()
        self.tex_y.use(0); self.tex_u.use(1); self.tex_v.use(2)
        self.prog_yuv["tex_y"].value = 0
        self.prog_yuv["tex_u"].value = 1
        self.prog_yuv["tex_v"].value = 2
        self.fbo_mid.use()
        self.vao_yuv.render(mode=moderngl.TRIANGLE_STRIP)
        t2 = time.perf_counter_ns()
        self.tex_mid.use(0)
        self.prog_grade["tex_rgb"].value = 0
        self.prog_grade["u_grade"].value = 1 if graded else 0
        self.fbo_out.use()
        self.vao_grade.render(mode=moderngl.TRIANGLE_STRIP)
        t3 = time.perf_counter_ns()
        data = self.fbo_out.read(components=3, dtype="f1", alignment=1)
        t4 = time.perf_counter_ns()
        arr = np.frombuffer(data, dtype=np.uint8).reshape(self.out_h, self.out_w, 3)
        t["upload"] = (t1 - t0) / 1e6
        t["pass1"] = (t2 - t1) / 1e6
        t["pass2"] = (t3 - t2) / 1e6
        t["read"] = (t4 - t3) / 1e6
        return arr, t

    def release(self):
        for obj in (self.tex_y, self.tex_u, self.tex_v, self.tex_mid,
                    self.fbo_mid, self.tex_out, self.fbo_out):
            try:
                obj.release()
            except Exception:
                pass
        try:
            self.ctx.release()
        except Exception:
            pass


def run_gpu(y16, u16, v16, out_w, out_h, graded, frames, warmup, collect=None):
    proto = GpuExportProto(y16.shape[1], y16.shape[0], out_w, out_h)
    try:
        acc = {"upload": 0.0, "pass1": 0.0, "pass2": 0.0, "read": 0.0}
        outs = {}
        t0 = time.perf_counter()
        n_total = frames + warmup
        for i in range(n_total):
            arr, t = proto.frame_yuv(y16, u16, v16, graded)
            if i >= warmup:
                for k in acc:
                    acc[k] += t[k]
                if collect is not None and i in collect:
                    outs[i] = arr.copy()
        wall_ms = (time.perf_counter() - t0) / frames * 1000
        return {k: v / frames for k, v in acc.items()}, wall_ms, outs
    finally:
        proto.release()


def report_stage_line(name, ms, total_ms):
    print(f"  {name:<10} {ms:8.1f} ms  ({ms / total_ms * 100:4.1f}%)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--warmup", type=int, default=4)
    ap.add_argument("--src-w", type=int, default=1920)
    ap.add_argument("--src-h", type=int, default=1080)
    ap.add_argument("--out-w", type=int, default=3840)
    ap.add_argument("--out-h", type=int, default=2160)
    ap.add_argument("--no-gpu", action="store_true")
    args = ap.parse_args()

    y16, u16, v16 = make_source(args.src_w, args.src_h)
    print(f"== RENDER 探针 {args.src_w}x{args.src_h} yuv422p10le → "
          f"{args.out_w}x{args.out_h} bgr24，{args.frames} 帧 ==")

    collect = {args.warmup}  # 采一帧做一致性对比

    # ── CPU 链分解 ──
    for label, params in (("无调色", None), ("典型调色", GRADE_TYPICAL),
                          ("重调色(+hue_shift)", GRADE_HEAVY)):
        acc, chain_ms, outs = run_cpu_chain(
            y16, u16, v16, args.out_w, args.out_h, params,
            args.frames, args.warmup, collect=collect)
        total = sum(acc.values())
        print(f"\n-- CPU 链 [{label}] 段合计 {total:.1f} ms | 全链 wall(含预热均值) "
              f"{chain_ms:.1f} ms | {1000.0 / max(total, 1e-9):.2f} fps(段合计口径) --")
        for k in ("extract", "grade", "resize", "normalise", "blend", "quantize"):
            report_stage_line(k, acc[k], total)
        if params is None:
            cpu_ref = outs[args.warmup]

    if args.no_gpu:
        return

    # ── GPU 原型 ──
    print("\n-- GPU 原型 (standalone GL, u16 平面上传) --")
    gpu_ref = None
    for label, graded in (("恒等(不调色)", False), ("典型调色(shader)", True)):
        acc, wall_ms, outs = run_gpu(
            y16, u16, v16, args.out_w, args.out_h, graded,
            args.frames, args.warmup, collect=collect)
        total = sum(acc.values())
        print(f"[{label}] 段合计 {total:.1f} ms | wall(均) {wall_ms:.1f} ms | "
              f"{1000.0 / max(wall_ms, 1e-9):.2f} fps")
        for k in ("upload", "pass1", "pass2", "read"):
            report_stage_line(k, acc[k], total)
        if not graded:
            gpu_ref = outs[args.warmup]

    # ── 一致性（恒等链：GPU vs CPU）──
    if gpu_ref is not None and cpu_ref is not None:
        a, b = np.asarray(cpu_ref), np.asarray(gpu_ref)
        if a.shape != b.shape:
            print(f"\n一致性: 形状不匹配 CPU{a.shape} vs GPU{b.shape}")
        else:
            d = np.abs(a.astype(np.int16) - b.astype(np.int16))
            print(f"\n一致性(恒等链): maxdiff {d.max()} | 平均 |Δ| "
                  f"{d.mean():.4f} | >2 LSB 占比 {(d > 2).mean() * 100:.3f}%")
            # 方向自检：若上下翻转则角点对角相等——报告角点便于诊断
            print(f"  角点 CPU[0,0]={a[0, 0]} GPU[0,0]={b[0, 0]} "
                  f"CPU[-1,-1]={a[-1, -1]} GPU[-1,-1]={b[-1, -1]}")


if __name__ == "__main__":
    main()
