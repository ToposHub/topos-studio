#!/usr/bin/env python3
"""P1-05: render-only A/B — Metal vs OpenGL on the same GPU, same workload.

Answers plan §13 open question #5 and the K1 kill-criterion input: on this
machine (AMD RX 6800, macOS GL 4.1 compat layer), is the same fragment
workload faster through Metal than through the deprecated OpenGL stack?

Workload (identical math in MSL and GLSL 330):
  3x3 gaussian blur (9 texture samples) -> USM sharpen -> lift/gamma/gain
  -> saturation.  Render-only: the input texture is GPU-resident and never
  re-uploaded; draws do not depend on each other.

Timing:
  Metal  : commandBuffer GPUStartTime/GPUEndTime over a batch of N draws.
  OpenGL : moderngl ctx.query(time=True) over the same N draws.

Usage:
  .venv/bin/python tools/bench_render_ab_metal_gl.py [--frames 300] [--reps 5]
      [--sizes 1920x1080,3840x2160] [--formats bgra8,rgba16f]
      [--output-json PATH]

Output: paired JSON + MD in tests/media/results/ (backend_compare_*).
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "tests" / "media" / "results"

# ---------------------------------------------------------------------------
# shared shader math
# ---------------------------------------------------------------------------

GLSL_VS = """
#version 330
in vec2 in_pos;
out vec2 v_uv;
void main() {
    v_uv = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

GLSL_FS = """
#version 330
in vec2 v_uv;
out vec4 f_color;
uniform sampler2D src_tex;
uniform float amount;
uniform float lift;
uniform float gamma_inv;
uniform float gain;
uniform float saturation;
uniform vec2 texel;

vec3 sample3x3(vec2 uv) {
    vec3 sum = vec3(0.0);
    float w[3] = float[3](1.0, 2.0, 1.0);
    for (int j = 0; j < 3; ++j) {
        for (int i = 0; i < 3; ++i) {
            vec2 off = vec2(float(i - 1), float(j - 1)) * texel;
            float wgt = w[i] * w[j];
            sum += texture(src_tex, uv + off).rgb * wgt;
        }
    }
    return sum / 16.0;
}

void main() {
    vec3 c = texture(src_tex, v_uv).rgb;
    vec3 blur = sample3x3(v_uv);
    c = c + amount * (c - blur);              // USM
    c = c * gain + lift;                      // lift/gain
    c = pow(max(c, vec3(0.0)), vec3(gamma_inv));
    float luma = dot(c, vec3(0.2126, 0.7152, 0.0722));
    c = mix(vec3(luma), c, saturation);
    f_color = vec4(c, 1.0);
}
"""

MSL_COMMON = """
#include <metal_stdlib>
using namespace metal;
struct Raster { float4 pos [[position]]; float2 uv; };
"""

MSL_VS = """
vertex Raster vs_quad(uint vid [[vertex_id]]) {
    float2 positions[6] = {
        float2(-1.0, -1.0), float2(1.0, -1.0), float2(-1.0, 1.0),
        float2(-1.0, 1.0), float2(1.0, -1.0), float2(1.0, 1.0)
    };
    float2 p = positions[vid];
    Raster r;
    r.pos = float4(p, 0.0, 1.0);
    r.uv = p * 0.5 + 0.5;
    r.uv.y = 1.0 - r.uv.y;
    return r;
}
"""

MSL_FS = """
fragment float4 fs_usm(Raster r [[stage_in]],
                       texture2d<float> src_tex [[texture(0)]],
                       sampler smp [[sampler(0)]],
                       constant float4& params [[buffer(0)]]) {
    // params: x=amount y=lift z=gamma_inv w=gain ; saturation via buffer(1)
    float amount = params.x, lift = params.y, gamma_inv = params.z, gain = params.w;
    float3 c = src_tex.sample(smp, r.uv).rgb;
    float3 sum = float3(0.0);
    float w[3] = {1.0, 2.0, 1.0};
    for (int j = 0; j < 3; ++j) {
        for (int i = 0; i < 3; ++i) {
            float2 off = float2(i - 1, j - 1) * (1.0 / float2(src_tex.get_width(), src_tex.get_height()));
            sum += src_tex.sample(smp, r.uv + off).rgb * (w[i] * w[j]);
        }
    }
    float3 blur = sum / 16.0;
    c = c + amount * (c - blur);
    c = c * gain + lift;
    c = pow(max(c, float3(0.0)), float3(gamma_inv));
    float luma = dot(c, float3(0.2126, 0.7152, 0.0722));
    c = mix(float3(luma), c, 1.2);
    return float4(c, 1.0);
}
"""

PARAMS = dict(amount=0.6, lift=0.005, gamma_inv=1.0 / 1.05, gain=1.02, saturation=1.2)


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Metal path
# ---------------------------------------------------------------------------

def bench_metal(width: int, height: int, fmt_name: str, frames: int, reps: int) -> Dict:
    import Metal
    import objc

    pix_fmt = 80 if fmt_name == "bgra8" else 110  # BGRA8Unorm / RGBA16Float
    dev = Metal.MTLCreateSystemDefaultDevice()
    queue = dev.newCommandQueue()
    lib, err = dev.newLibraryWithSource_options_error_(
        MSL_COMMON + MSL_VS + MSL_FS, None, None)
    if lib is None:
        raise RuntimeError(f"MSL compile failed: {err}")
    vfn = lib.newFunctionWithName_("vs_quad")
    ffn = lib.newFunctionWithName_("fs_usm")
    desc = Metal.MTLRenderPipelineDescriptor.alloc().init()
    desc.setVertexFunction_(vfn)
    desc.setFragmentFunction_(ffn)
    desc.colorAttachments().objectAtIndexedSubscript_(0).setPixelFormat_(pix_fmt)
    pso, perr = dev.newRenderPipelineStateWithDescriptor_error_(desc, None)
    if pso is None:
        raise RuntimeError(f"pipeline failed: {perr}")

    tdesc = Metal.MTLTextureDescriptor.texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
        pix_fmt, width, height, False)
    tdesc.setUsage_(Metal.MTLTextureUsageShaderRead | Metal.MTLTextureUsageRenderTarget)
    tdesc.setStorageMode_(Metal.MTLStorageModePrivate)
    src_tex = dev.newTextureWithDescriptor_(tdesc)
    dst_tex = dev.newTextureWithDescriptor_(tdesc)

    sdesc = Metal.MTLSamplerDescriptor.alloc().init()
    sdesc.setMinFilter_(Metal.MTLSamplerMinMagFilterLinear)
    sdesc.setMagFilter_(Metal.MTLSamplerMinMagFilterLinear)
    sdesc.setSAddressMode_(Metal.MTLSamplerAddressModeClampToEdge)
    sdesc.setTAddressMode_(Metal.MTLSamplerAddressModeClampToEdge)
    sampler = dev.newSamplerStateWithDescriptor_(sdesc)

    import struct as _s
    pbuf = dev.newBufferWithBytes_length_options_(
        _s.pack("4f", PARAMS["amount"], PARAMS["lift"], PARAMS["gamma_inv"], PARAMS["gain"]),
        16, Metal.MTLResourceStorageModeShared)
    sbuf = dev.newBufferWithBytes_length_options_(
        _s.pack("f", PARAMS["saturation"]), 4, Metal.MTLResourceStorageModeShared)

    def run_batch(n: int) -> float:
        cb = queue.commandBuffer()
        for _ in range(n):
            rpd = Metal.MTLRenderPassDescriptor.renderPassDescriptor()
            ca = rpd.colorAttachments().objectAtIndexedSubscript_(0)
            ca.setTexture_(dst_tex)
            ca.setLoadAction_(Metal.MTLLoadActionDontCare)
            ca.setStoreAction_(Metal.MTLStoreActionStore)
            enc = cb.renderCommandEncoderWithDescriptor_(rpd)
            enc.setRenderPipelineState_(pso)
            enc.setFragmentTexture_atIndex_(src_tex, 0)
            enc.setFragmentSamplerState_atIndex_(sampler, 0)
            enc.setFragmentBuffer_offset_atIndex_(pbuf, 0, 0)
            enc.drawPrimitives_vertexStart_vertexCount_(Metal.MTLPrimitiveTypeTriangle, 0, 6)
            enc.endEncoding()
        cb.commit()
        cb.waitUntilCompleted()
        if cb.error() is not None:
            raise RuntimeError(f"command buffer error: {cb.error()}")
        return (cb.GPUEndTime() - cb.GPUStartTime()) * 1000.0

    run_batch(50)  # warmup
    samples = [run_batch(frames) / frames for _ in range(reps)]
    dev_name = dev.name()
    return {
        "backend": "metal",
        "device": dev_name,
        "ms_per_frame_samples": samples,
        "ms_per_frame_median": statistics.median(samples),
    }


# ---------------------------------------------------------------------------
# OpenGL path (ModernGL)
# ---------------------------------------------------------------------------

def bench_opengl(width: int, height: int, fmt_name: str, frames: int, reps: int) -> Dict:
    import moderngl
    import numpy as np

    ctx = moderngl.create_standalone_context()
    dtype = "f1" if fmt_name == "bgra8" else "f2"
    tex = ctx.texture((width, height), 4, dtype=dtype)
    tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
    dst = ctx.texture((width, height), 4, dtype=dtype)
    fbo = ctx.framebuffer(color_attachments=[dst])
    prog = ctx.program(vertex_shader=GLSL_VS, fragment_shader=GLSL_FS)
    vbo = ctx.buffer(data=np.array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1], dtype="f4").tobytes())
    vao = ctx.simple_vertex_array(prog, vbo, "in_pos")
    prog["src_tex"].value = 0
    prog["amount"].value = PARAMS["amount"]
    prog["lift"].value = PARAMS["lift"]
    prog["gamma_inv"].value = PARAMS["gamma_inv"]
    prog["gain"].value = PARAMS["gain"]
    prog["saturation"].value = PARAMS["saturation"]
    prog["texel"].value = (1.0 / width, 1.0 / height)

    def run_batch(n: int) -> float:
        q = ctx.query(time=True)
        tex.use(0)
        fbo.use()
        with q:  # single begin/end around the whole batch
            for _ in range(n):
                vao.render(moderngl.TRIANGLES)
        ctx.finish()
        return q.elapsed / 1e9 * 1000.0  # ns -> ms

    run_batch(50)
    samples = [run_batch(frames) / frames for _ in range(reps)]
    info = ctx.info
    result = {
        "backend": "opengl",
        "device": str(info.get("GL_RENDERER", "")),
        "gl_version": str(info.get("GL_VERSION", "")),
        "ms_per_frame_samples": samples,
        "ms_per_frame_median": statistics.median(samples),
    }
    vao.release(); vbo.release(); prog.release(); fbo.release(); dst.release(); tex.release(); ctx.release()
    return result


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=300)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--sizes", type=str, default="1920x1080,3840x2160")
    parser.add_argument("--formats", type=str, default="bgra8,rgba16f")
    parser.add_argument("--output-json", type=str, default=None)
    args = parser.parse_args()

    sizes = [tuple(map(int, s.split("x"))) for s in args.sizes.split(",")]
    formats = args.formats.split(",")
    env = {
        "git_sha": _git_sha(),
        "timestamp": datetime.now().isoformat(),
        "os": platform.platform(),
        "machine": platform.machine(),
        "frames_per_batch": args.frames,
        "reps": args.reps,
        "workload": "3x3 gaussian -> USM -> lift/gamma/gain -> saturation",
    }
    print(f"git={env['git_sha'][:12]}  frames={args.frames}  reps={args.reps}")

    results = []
    for (w, h) in sizes:
        for fmt in formats:
            entry: Dict = {"size": f"{w}x{h}", "format": fmt}
            print(f"\n== {w}x{h} {fmt} ==")
            entry["opengl"] = bench_opengl(w, h, fmt, args.frames, args.reps)
            print(f"  OpenGL : {entry['opengl']['ms_per_frame_median']:.4f} ms/frame  "
                  f"({entry['opengl']['device']})")
            entry["metal"] = bench_metal(w, h, fmt, args.frames, args.reps)
            print(f"  Metal  : {entry['metal']['ms_per_frame_median']:.4f} ms/frame  "
                  f"({entry['metal']['device']})")
            gl = entry["opengl"]["ms_per_frame_median"]
            mt = entry["metal"]["ms_per_frame_median"]
            entry["metal_speedup_percent"] = (gl - mt) / gl * 100.0 if gl > 0 else 0.0
            entry["k1_threshold_percent"] = 15.0
            entry["k1_pass"] = entry["metal_speedup_percent"] >= 15.0
            print(f"  speedup: {entry['metal_speedup_percent']:+.1f}%  "
                  f"(K1 needs >=15%)")
            results.append(entry)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_json = Path(args.output_json) if args.output_json else OUT_DIR / "backend_compare_render_ab.json"
    doc = {"schema": "topos.render_ab.v1", "environment": env, "results": results}
    out_json.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")

    md = ["# P1-05 render-only A/B：Metal vs OpenGL", "",
          f"> {env['timestamp']}  git `{env['git_sha'][:12]}`  {env['os']}", "",
          "| 尺寸 | 格式 | OpenGL ms/f | Metal ms/f | Metal 加速 | K1 (≥15%) |", "|---|---|---:|---:|---:|---|"]
    md += [f"| {r['size']} | {r['format']} | {r['opengl']['ms_per_frame_median']:.4f} | "
           f"{r['metal']['ms_per_frame_median']:.4f} | {r['metal_speedup_percent']:+.1f}% | "
           f"{'PASS' if r['k1_pass'] else 'FAIL'} |" for r in results]
    md += ["", "归因：render-only（GPU-resident 输入，无上传/解码），Metal 计时取 "
           "commandBuffer GPU 时间，OpenGL 取 timer query。K1 中止条件（计划 §1.4）"
           "以 render-only 加速 <15% 为未达门槛。", ""]
    out_json.with_suffix(".md").write_text("\n".join(md))
    print(f"\nwrote {out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
