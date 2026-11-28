"""阶段 4.5 —— Metal compute 示波器引擎（波形/Parade/矢量/直方图/CIE）。

与 GL 引擎（``src.shared.gpu.gpu_scope_engine.GpuScopeEngine``）同一套
**采样与显示统计语义**，但把点溅栅格（POINTS + additive blend）改为
compute 原子累加——**GPU 数据留在设备上**（密度/直方图是 MTLBuffer 里的
uint 定点计数，只在展示/诊断时读回最终显示图，不做逐像素 CPU 读回）：

- **密度累加**：每个分析像素对目标 bin 做
  ``atomic_add(weight × 65536)``（定点 uint，确定性、可精确对拍）。
  GL 的 2-3px 高斯点溅在 Metal 侧改为**单位 bin 累加 + 展示卷积**——
  展示 kernel 完整移植 GL 展示着色器的 7-tap 纵向卷积与 tone-map，观感
  同源（溅射核差异在报告中如实登记）。
- **直方图**：uint 原子直接按列累加（GL 的散点+归约是为绕开 f16 精度；
  uint 原子无需该绕路），列和 = log1p 在展示 kernel 计算。
- **CIE**：与 GL CIE 着色器同一套 transfer 线性化（srgb/bt709^2.4/
  PQ/HLG/scene）与 primaries→XYZ 矩阵，xy bin 窗口 [0,0.8]×[0,0.9]，
  预缩 1/64 由展示侧补偿。
- **异步提交**：``render_demand`` 只 dispatch（编码进当前 command
  buffer，不提交、不等待——播放时示波器不阻塞视口）；
  ``submit_async`` 挂 completion 回调（驱动线程只记录），
  ``poll``（渲染线程）发布 :class:`MetalScopeFrame`。需求按 revision
  单调丢弃（latest-wins）。
- **展示读回计数**：``read_display`` 计入 ``display_readbacks``（播放
  展示读回）；显式像素比对/截图走 :class:`MetalReadbackQueue`
  （4.6，``explicit_readbacks``）——计数能区分两类读回。

线程边界：无 Qt；单渲染线程独占（与 ``MetalComputeContext`` 约定一致）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.features.color.core.scope_types import SCOPE_RANGE_MAX, SCOPE_RANGE_MIN, waveform_plot_range
from src.shared.gpu.metal.context import MetalComputeContext

# ── 常量（与 GL 引擎/scope_types 同口径） ────────────────────────────────

WEIGHT_SCALE = 65536.0                    # 定点累加刻度（uint32 安全：
                                          # 4K 分析图全落一 bin 亦 < 2^32/2^16）
CIE_X_RANGE = (0.0, 0.8)
CIE_Y_RANGE = (0.0, 0.9)
CIE_PRESCALE = 64.0
HISTOGRAM_BINS = 512

_TRANSFER_CODES = {"srgb": 0, "bt709": 1, "pq": 2, "hlg": 3, "scene": 4}
_PRIMARY_CODES = {"bt709": 0, "p3": 1, "bt2020": 2}

#: 展示图的像素格式（RGBA8Unorm——展示统计的最终显示面）
DISPLAY_PIXEL_FORMAT = 80          # MTLPixelFormatRGBA8Unorm

# ── MSL ──────────────────────────────────────────────────────────────────

METAL_SCOPE_MSL = """
#include <metal_stdlib>
using namespace metal;

// ── 采样：源 → 分析纹理（手动双线性，clamp-to-edge；dispatch 不绑 sampler）
float4 sample_bilinear_rgba(texture2d<float, access::read> tex,
                            float2 texel_coord, uint2 size) {
    float2 u = texel_coord - 0.5;
    int2 i0 = int2(floor(u));
    float2 f = u - float2(i0);
    int2 i1 = i0 + int2(1);
    int2 c0 = clamp(i0, int2(0), int2(size) - int2(1));
    int2 c1 = clamp(i1, int2(0), int2(size) - int2(1));
    float4 v00 = tex.read(uint2(c0));
    float4 v10 = tex.read(uint2(c1.x, c0.y));
    float4 v01 = tex.read(uint2(c0.x, c1.y));
    float4 v11 = tex.read(uint2(c1));
    float4 a = mix(v00, v10, f.x);
    float4 b = mix(v01, v11, f.x);
    return mix(a, b, f.y);
}

kernel void scope_sample(
    texture2d<float, access::read> src      [[texture(0)]],
    texture2d<float, access::write> analysis [[texture(1)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= analysis.get_width() || gid.y >= analysis.get_height()) {
        return;
    }
    uint2 src_size = uint2(src.get_width(), src.get_height());
    float2 norm = (float2(gid) + 0.5) / float2(float(analysis.get_width()),
                                               float(analysis.get_height()));
    float2 texel = norm * float2(src_size);
    float4 c = sample_bilinear_rgba(src, texel, src_size);
    // 与 GL scope_sample 同一窗口：保留 ±越界诊断余量，防 NaN/无界 HDR
    // 毒化累加；mask 加权未接线（v1 全 1——差异见模块 docstring）。
    analysis.write(float4(clamp(c.rgb, float3(-0.1), float3(1.2)), 1.0), gid);
}

// ── 波形/Parade/Luma 密度累加（uint 定点原子；5 平面拼接单 buffer：
//    [r][g][b][over][under]，每平面 density_w*density_h）
kernel void scope_waveform_accum(
    texture2d<float, access::read> analysis [[texture(0)]],
    device atomic_uint* density             [[buffer(0)]],
    constant int& mode_p0, constant int& show_excursions,
    constant int& sample_w, constant int& sample_h,
    constant int& density_w, constant int& density_h,
    constant float& range_min, constant float& range_max,
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= uint(sample_w) || gid.y >= uint(sample_h)) {
        return;
    }
    float4 s = analysis.read(gid);
    float3 c = s.rgb;
    float weight = s.a;
    uint plane_area = uint(density_w) * uint(density_h);
    float value_scale = 1.0 / max(range_max - range_min, 0.0001);

    float vals[3];
    vals[0] = c.r; vals[1] = c.g; vals[2] = c.b;
    float luma = dot(c, float3(0.2126, 0.7152, 0.0722));

    uint fixed_weight = uint(clamp(weight, 0.0, 1.0) * 65536.0 + 0.5);

    // Density rows run from range_min to range_max. The display pass flips
    // them into top-down image coordinates.
    uint column_span = uint(density_w);
    if (mode_p0 == 1) {
        column_span = uint(density_w / 3);   // Parade：三等分
    }

    for (int ch = 0; ch < 3; ch++) {                    // 常量界循环
        float val;
        if (mode_p0 == 2) {
            // Luma：白迹——单次迭代写 R=G=B 三平面（循环只跑 1 次）
            val = luma;
            if (ch > 0) { break; }
        } else {
            val = vals[ch];
        }
        if (show_excursions == 0 && (val < 0.0 || val > 1.0)) {
            continue;
        }
        int col = int((float(gid.x) + 0.5) / float(sample_w)
                      * float(column_span));
        int third_base = (mode_p0 == 1) ? ch * int(column_span) : 0;
        col = clamp(col + third_base, 0, density_w - 1);
        float t = clamp((val - range_min) * value_scale, 0.0, 1.0);
        int row = int(t * float(density_h));
        row = clamp(row, 0, density_h - 1);
        uint idx_base = uint(row) * uint(density_w) + uint(col);
        if (mode_p0 == 2) {
            // Luma 写入 R=G=B（GL luma 分支 w=vec3(weight)）
            atomic_fetch_add_explicit(density + idx_base,
                                       fixed_weight, memory_order_relaxed);
            atomic_fetch_add_explicit(density + plane_area + idx_base,
                                       fixed_weight, memory_order_relaxed);
            atomic_fetch_add_explicit(density + plane_area * 2u + idx_base,
                                       fixed_weight, memory_order_relaxed);
        } else {
            atomic_fetch_add_explicit(
                density + uint(ch) * plane_area + idx_base,
                fixed_weight, memory_order_relaxed);
        }
        // 越界诊断密度（GL alpha 通道的符号语义 → over/under 双平面）
        if (val > 1.0) {
            atomic_fetch_add_explicit(density + plane_area * 3u + idx_base,
                                       fixed_weight, memory_order_relaxed);
        } else if (val < 0.0) {
            atomic_fetch_add_explicit(density + plane_area * 4u + idx_base,
                                       fixed_weight, memory_order_relaxed);
        }
    }
}

// ── 矢量示波器密度（Cb/Cr Rec.709；行 0 = +Cr 上）
kernel void scope_vectorscope_accum(
    texture2d<float, access::read> analysis [[texture(0)]],
    device atomic_uint* density             [[buffer(0)]],
    constant int& sample_w, constant int& sample_h,
    constant int& density_w, constant int& density_h,
    constant float& zoom, constant int& pad0,
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= uint(sample_w) || gid.y >= uint(sample_h)) {
        return;
    }
    float3 c = analysis.read(gid).rgb;
    float weight = analysis.read(gid).a;
    float cb = dot(c, float3(-0.169, -0.331, 0.500));
    float cr = dot(c, float3( 0.500, -0.419, -0.081));
    float half_cell = 1.0 / float(density_w);
    float x_n = clamp(cb * 2.0 * zoom, -1.0 + half_cell, 1.0 - half_cell);
    float y_n = clamp(cr * 2.0 * zoom, -1.0 + half_cell, 1.0 - half_cell);
    // GL NDC y=+1 = 展示图顶部 → top-down 行 = (1-(y_n+1)/2)*h
    int col = int((x_n + 1.0) * 0.5 * float(density_w));
    int row = int((1.0 - (y_n + 1.0) * 0.5) * float(density_h));
    col = clamp(col, 0, density_w - 1);
    row = clamp(row, 0, density_h - 1);
    uint fixed_weight = uint(clamp(weight, 0.0, 1.0) * 65536.0 + 0.5);
    atomic_fetch_add_explicit(
        density + uint(row) * uint(density_w) + uint(col),
        fixed_weight, memory_order_relaxed);
}

// ── 直方图（直接按列 uint 原子；hist 布局 [512][4]：R,G,B,Y）
kernel void scope_histogram_accum(
    texture2d<float, access::read> analysis [[texture(0)]],
    device atomic_uint* hist                [[buffer(0)]],
    constant int& sample_w, constant int& sample_h,
    constant int& axis, constant int& pad0, constant int& pad1,
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= uint(sample_w) || gid.y >= uint(sample_h)) {
        return;
    }
    float4 s = analysis.read(gid);
    float3 c = s.rgb;
    float weight = s.a;
    float hi = max(max(c.r, c.g), c.b);
    float lo = min(min(c.r, c.g), c.b);
    float delta = hi - lo;
    float saturation = hi > 0.000001 ? clamp(delta / hi, 0.0, 1.0) : 0.0;
    float w = clamp(weight, 0.0, 1.0) * 65536.0;

    float vals[3];
    vals[0] = c.r; vals[1] = c.g; vals[2] = c.b;
    for (int ch = 0; ch < 3; ch++) {                    // 常量界循环
        float val = vals[ch];
        int col = clamp(int(clamp(val, 0.0, 1.0) * 512.0), 0, 511);
        atomic_fetch_add_explicit(
            hist + uint(ch) * 512u + uint(col),
            uint(w + 0.5), memory_order_relaxed);
    }
    // Y（axis 派生：0=Rec709 Y / 1=hue / 2=sat / 3=HSL luma——GL 同码）
    float val_y = dot(c, float3(0.2126, 0.7152, 0.0722));
    float w_y = w;
    if (axis == 1) {
        float hue = 0.0;
        if (delta > 0.000001) {
            if (hi == c.r) hue = (c.g - c.b) / delta;
            else if (hi == c.g) hue = 2.0 + (c.b - c.r) / delta;
            else hue = 4.0 + (c.r - c.g) / delta;
        }
        val_y = fract(hue / 6.0 + 1.0);
        w_y *= saturation;
    } else if (axis == 2) {
        val_y = saturation;
    } else if (axis == 3) {
        val_y = dot(c, float3(0.299, 0.587, 0.114));
    }
    int col_y = clamp(int(clamp(val_y, 0.0, 1.0) * 512.0), 0, 511);
    atomic_fetch_add_explicit(hist + 3u * 512u + uint(col_y),
                              uint(clamp(w_y, 0.0, 65536.0) + 0.5),
                              memory_order_relaxed);
}

// ── CIE xy 色度密度（transfer 线性化与 GL CIE 着色器同式）
float srgb_eotf(float c) {
    float cc = clamp(c, 0.0, 1.0);
    return cc <= 0.0405 ? cc / 12.92 : pow((cc + 0.055) / 1.055, 2.4);
}
float pq_code_to_nits(float code) {
    float v = pow(clamp(code, 0.0, 1.0), 1.0 / 78.84375);
    float num = max(v - 0.8359375, 0.0);
    float den = max(18.8515625 - 18.6875 * v, 0.0000001);
    return pow(num / den, 1.0 / 0.1593017578125) * 10000.0;
}
float hlg_code_to_scene(float code) {
    float e = clamp(code, 0.0, 1.0);
    float low = (e * e) / 3.0;
    float high = (exp((e - 0.55991073) / 0.17883277) + 0.28466892) / 12.0;
    return e <= 0.5 ? low : high;
}
float3 scope_linearize(float3 c, int transfer) {
    if (transfer == 0) {
        return float3(srgb_eotf(c.r), srgb_eotf(c.g), srgb_eotf(c.b));
    }
    if (transfer == 1) {
        return pow(clamp(c, 0.0, 1.0), float3(2.4));
    }
    if (transfer == 2) {
        return float3(pq_code_to_nits(c.r), pq_code_to_nits(c.g),
                      pq_code_to_nits(c.b)) / 100.0;
    }
    if (transfer == 3) {
        return float3(hlg_code_to_scene(c.r), hlg_code_to_scene(c.g),
                      hlg_code_to_scene(c.b));
    }
    return max(c, float3(0.0));
}
float3 scope_rgb_to_xyz(float3 rgb, int primaries) {
    if (primaries == 1) {
        return float3(dot(rgb, float3(0.4866, 0.2657, 0.1982)),
                      dot(rgb, float3(0.2290, 0.6917, 0.0793)),
                      dot(rgb, float3(0.0000, 0.0451, 1.0439)));
    }
    if (primaries == 2) {
        return float3(dot(rgb, float3(0.6370, 0.1446, 0.1689)),
                      dot(rgb, float3(0.2627, 0.6780, 0.0593)),
                      dot(rgb, float3(0.0000, 0.0281, 1.0610)));
    }
    return float3(dot(rgb, float3(0.4124, 0.3576, 0.1805)),
                  dot(rgb, float3(0.2126, 0.7152, 0.0722)),
                  dot(rgb, float3(0.0193, 0.1192, 0.9505)));
}

kernel void scope_cie_accum(
    texture2d<float, access::read> analysis [[texture(0)]],
    device atomic_uint* density             [[buffer(0)]],
    constant int& sample_w, constant int& sample_h,
    constant int& density_w, constant int& density_h,
    constant int& transfer, constant int& primaries, constant int& pad0,
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= uint(sample_w) || gid.y >= uint(sample_h)) {
        return;
    }
    float4 s = analysis.read(gid);
    float3 lin = max(scope_linearize(s.rgb, transfer), float3(0.0));
    float3 xyz = scope_rgb_to_xyz(lin, primaries);
    float ssum = xyz.x + xyz.y + xyz.z;
    if (ssum <= 0.000000001) {
        return;
    }
    float cx = xyz.x / ssum;
    float cy = xyz.y / ssum;
    if (cx < 0.0 || cx >= 0.8 || cy < 0.0 || cy >= 0.9) {
        return;
    }
    int col = clamp(int(cx / 0.8 * float(density_w)), 0, density_w - 1);
    // 行 0 = 顶部（cy 大）——与 GL 展示的图像方向一致
    int row = clamp(int((1.0 - cy / 0.9) * float(density_h)),
                    0, density_h - 1);
    // /64 预缩（GL 同式，防 f16 溢出；uint 定点下保留为语义等价刻度）
    float fixed_weight = clamp(s.a, 0.0, 1.0) * 65536.0 / 64.0;
    atomic_fetch_add_explicit(
        density + uint(row) * uint(density_w) + uint(col),
        uint(fixed_weight + 0.5), memory_order_relaxed);
}

// ── 清零（dispatch 覆盖全部 uint）
kernel void scope_clear_buffer(
    device atomic_uint* buffer [[buffer(0)]],
    constant uint& length                     [[buffer(1)]],
    uint gid [[thread_position_in_grid]])
{
    if (gid >= length) { return; }
    atomic_store_explicit(buffer + gid, 0u, memory_order_relaxed);
}

// ── 展示：波形/Parade/Luma（GL SCOPE_WAVEFORM_DISPLAY_FRAGMENT 全量移植）
float3 wf_density_tap(const device uint* density, uint area,
                      int density_w, int density_h,
                      int row_bu, int col, float dy, float dx) {
    int r = clamp(row_bu + int(round(dy)), 0, density_h - 1);
    int c = clamp(col + int(round(dx)), 0, density_w - 1);
    uint base = uint(r) * uint(density_w) + uint(c);
    return float3(float(density[base]) * (1.0 / 65536.0),
                  float(density[area + base]) * (1.0 / 65536.0),
                  float(density[area * 2u + base]) * (1.0 / 65536.0));
}
kernel void scope_display_waveform(
    const device uint* density               [[buffer(0)]],
    texture2d<float, access::write> out_tex  [[texture(0)]],
    constant int& density_w, constant int& density_h,
    constant float& max_inv, constant float& weight_inv,
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out_tex.get_width() || gid.y >= out_tex.get_height()) {
        return;
    }
    uint area = uint(density_w) * uint(density_h);
    // 底起 y（GL v_texcoord.y 语义）：行 r(top-down) → yb ∈ (0,1)
    float yb = (float(density_h - 1 - int(gid.y)) + 0.5) / float(density_h);
    float xf = (float(gid.x) + 0.5) / float(density_w);
    int col = clamp(int(xf * float(density_w)), 0, density_w - 1);
    int row_bu = density_h - 1 - int(gid.y);

    float texel_y = 1.0;
    float texel_x = 1.0;
    float3 density_rgb = wf_density_tap(
        density, area, density_w, density_h, row_bu, col, 0.0, 0.0) * 0.40;
    density_rgb += wf_density_tap(
        density, area, density_w, density_h, row_bu, col, texel_y, 0.0) * 0.16;
    density_rgb += wf_density_tap(
        density, area, density_w, density_h, row_bu, col, -texel_y, 0.0) * 0.16;
    density_rgb += wf_density_tap(
        density, area, density_w, density_h, row_bu, col,
        texel_y * 2.0, 0.0) * 0.06;
    density_rgb += wf_density_tap(
        density, area, density_w, density_h, row_bu, col,
        -texel_y * 2.0, 0.0) * 0.06;
    density_rgb += wf_density_tap(
        density, area, density_w, density_h, row_bu, col,
        0.0, texel_x) * 0.08;
    density_rgb += wf_density_tap(
        density, area, density_w, density_h, row_bu, col,
        0.0, -texel_x) * 0.08;
    uint base = uint(row_bu) * uint(density_w) + uint(col);
    float over_density = float(density[area * 3u + base]) * weight_inv;
    float under_density = float(density[area * 4u + base]) * weight_inv;

    float3 norm_density = clamp(density_rgb * max_inv, float3(0.0), float3(1.0));
    float3 mapped = pow(norm_density, float3(0.54));
    mapped = clamp(mapped * 1.32, float3(0.0), float3(1.0));
    float overlap = min(mapped.r, min(mapped.g, mapped.b));
    float white_lift = overlap * 0.24
        + smoothstep(0.12, 0.45, overlap) * 0.32;
    mapped = clamp(mapped + white_lift, float3(0.0), float3(1.0));
    float over_alert = clamp(pow(over_density * max_inv, 0.54) * 1.45,
                             0.0, 1.0);
    float under_alert = clamp(pow(under_density * max_inv, 0.54) * 1.45,
                              0.0, 1.0);
    mapped = mix(mapped, float3(1.0, 0.035, 0.0), over_alert);
    mapped = mix(mapped, float3(0.02, 0.20, 1.0), under_alert);

    out_tex.write(float4(mapped, 1.0), gid);
}

// ── 展示：矢量示波器（GL SCOPE_VECTORSCOPE_DISPLAY_FRAGMENT 全量移植）
kernel void scope_display_vectorscope(
    const device uint* density               [[buffer(0)]],
    texture2d<float, access::write> out_tex  [[texture(0)]],
    constant int& density_w, constant int& density_h,
    constant float& max_inv, constant float& zoom,
    constant float& weight_inv, constant int& pad0,
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out_tex.get_width() || gid.y >= out_tex.get_height()) {
        return;
    }
    float2 uv = (float2(gid) + 0.5) / float2(float(out_tex.get_width()),
                                             float(out_tex.get_height()));
    // top-down uv → GL 底起坐标
    float2 uv_gl = float2(uv.x, 1.0 - uv.y);
    float2 ndc = uv_gl * 2.0 - 1.0;
    float2 cbcr = ndc * 0.5 / zoom;
    float sat = length(cbcr);

    int col = clamp(int(uv.x * float(density_w)), 0, density_w - 1);
    int row = clamp(int(uv.y * float(density_h)), 0, density_h - 1);
    float d = float(density[uint(row) * uint(density_w) + uint(col)])
              * weight_inv;
    float norm = d * max_inv;
    float brightness = clamp(pow(norm, 0.7) * 2.0, 0.0, 1.0);
    float y = 0.5;
    float r = y + 1.402 * cbcr.y;
    float g = y - 0.344136 * cbcr.x - 0.714136 * cbcr.y;
    float b = y + 1.772 * cbcr.x;
    float3 heat = clamp(float3(r, g, b), float3(0.0), float3(1.0));
    float luma = dot(heat, float3(0.2126, 0.7152, 0.0722));
    float3 boosted = mix(float3(luma), heat, 1.5);
    float3 out_rgb = boosted * brightness;
    if (sat > 0.5) { out_rgb = float3(0.0); }

    // GL 的 p = uv-0.5（y 向上）→ top-down 的 y 取反
    float2 p = float2(uv.x - 0.5, 0.5 - uv.y);
    float radius = length(p);
    float pixel = 1.0 / float(density_w);
    float rim_radius = 0.497;
    float rim = 1.0 - smoothstep(pixel * 0.25, pixel * 1.1,
                                 fabs(radius - rim_radius));
    const float tau = 6.28318530718;
    float tick_position = radius > pixel ? atan2(p.y, p.x) / tau * 72.0 : 0.0;
    float tick_index = floor(tick_position + 0.5);
    float tick_distance = fabs(tick_position - tick_index) * tau / 72.0
                          * radius;
    float major = 1.0 - step(0.5, fmod(fabs(tick_index), 3.0));
    float tick_length = mix(0.018, 0.035, major);
    float tick = (1.0 - smoothstep(pixel * 0.2, pixel, tick_distance))
        * step(rim_radius - tick_length, radius) * step(radius, rim_radius);
    float cross = (1.0 - smoothstep(pixel * 0.2, pixel,
                                     min(fabs(p.x), fabs(p.y))))
        * step(radius, rim_radius);
    float grid = max(rim * 0.85, max(tick * 0.75, cross * 0.55));
    float3 amber = float3(0.40, 0.32, 0.075);
    out_tex.write(float4(max(out_rgb, amber * grid), 1.0), gid);
}

// ── 展示：直方图（GL SCOPE_HISTOGRAM_DISPLAY_FRAGMENT 全量移植）
kernel void scope_display_histogram(
    const device uint* hist                  [[buffer(0)]],
    texture2d<float, access::write> out_tex  [[texture(0)]],
    constant int& display_w, constant int& display_h,
    constant float& max_inv, constant float& weight_inv,
    constant int& pad0, constant int& pad1,
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out_tex.get_width() || gid.y >= out_tex.get_height()) {
        return;
    }
    float2 uv = (float2(gid) + 0.5) / float2(float(out_tex.get_width()),
                                             float(out_tex.get_height()));
    float col_f = uv.x * 512.0 - 0.5;
    int col = int(clamp(col_f, 0.0, 511.0));
    // top-down：R 顶 / G 中 / B 底（GL 底起 B 底的同屏方向）
    float row_position = min(uv.y * 3.0, 2.999999);
    int channel = min(int(row_position), 2);
    float y_in_band = 1.0 - fract(row_position);
    // MSL 无 log1p：log(1+x) 数值稳定式（x ≥ 0 时与 numpy log1p 同义）
    float hist_w = float(hist[uint(channel) * 512u + uint(col)])
                   * weight_inv;
    float log_sum = log(1.0 + hist_w);
    float norm = clamp(log_sum * max_inv, 0.0, 1.0);
    float colors0r = channel == 0 ? 1.0 : 0.025;
    float colors0g = channel == 1 ? 1.0 : (channel == 2 ? 0.40 : 0.035);
    float colors0b = channel == 2 ? 1.0 : (channel == 0 ? 0.06 : 0.035);
    float3 channel_color = float3(colors0r, colors0g, colors0b);
    float height = norm * 0.94;
    float trace_width = 3.0 / float(out_tex.get_height());
    float line = 1.0 - smoothstep(trace_width * 0.35, trace_width,
                                  fabs(y_in_band - height));
    float under_curve = step(y_in_band, height);
    float3 signal = channel_color * (under_curve * 0.16 + line * 0.82);
    float gx = fabs(uv.x * 8.0 - floor(uv.x * 8.0 + 0.5)) / 8.0;
    float grid = 1.0 - smoothstep(0.25 / float(out_tex.get_width()),
                                  1.0 / float(out_tex.get_width()), gx);
    float baseline = 1.0 - smoothstep(0.0, trace_width * 0.6, y_in_band);
    float3 amber = float3(0.30, 0.24, 0.055);
    out_tex.write(
        float4(max(signal, amber * max(grid * 0.65, baseline * 0.75)), 1.0),
        gid);
}

// ── 展示：CIE 热图（GL SCOPE_CIE_DISPLAY_FRAGMENT 全量移植）
kernel void scope_display_cie(
    const device uint* density               [[buffer(0)]],
    texture2d<float, access::write> out_tex  [[texture(0)]],
    constant int& density_w, constant int& density_h,
    constant float& max_inv, constant float& weight_inv,
    constant int& pad0, constant int& pad1,
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out_tex.get_width() || gid.y >= out_tex.get_height()) {
        return;
    }
    float2 uv = (float2(gid) + 0.5) / float2(float(out_tex.get_width()),
                                             float(out_tex.get_height()));
    int col = clamp(int(uv.x * float(density_w)), 0, density_w - 1);
    int row = clamp(int(uv.y * float(density_h)), 0, density_h - 1);
    float d = float(density[uint(row) * uint(density_w) + uint(col)])
              * weight_inv;
    float norm = clamp(d * max_inv, 0.0, 1.0);
    float t = pow(norm, 0.45);
    float3 c1 = float3(10.0, 60.0, 160.0) / 255.0;
    float3 c2 = float3(60.0, 220.0, 235.0) / 255.0;
    float3 c3 = float3(235.0, 240.0, 245.0) / 255.0;
    float3 heat;
    if (t < 0.25) {
        heat = mix(float3(0.0), c1, t / 0.25);
    } else if (t < 0.6) {
        heat = mix(c1, c2, (t - 0.25) / 0.35);
    } else {
        heat = mix(c2, c3, (t - 0.6) / 0.4);
    }
    float3 col_rgb = max(heat, float3(0.012));
    out_tex.write(float4(clamp(col_rgb, float3(0.0), float3(1.0)), 1.0), gid);
}
"""

def expected_waveform_peak(analysis_h: int, single_channel: bool) -> float:
    """GL display 的固定归一化峰值（移植自 GpuScopeEngine.display_waveform）。"""
    divisor = 3.0 if single_channel else 1.0
    return max(8.0, float(analysis_h) * 0.10 / divisor)


def expected_cie_max_inv(samples: int) -> float:
    """GL CIE 展示的固定增益（移植自 GpuScopeEngine.display_cie）。"""
    return CIE_PRESCALE / max(CIE_PRESCALE, samples * 0.02)


def expected_histogram_max_inv(samples: int) -> float:
    """GL 直方图展示的固定增益（log1p 域）。"""
    return 1.0 / max(1.0, float(np.log1p(samples / 48.0)))


@dataclass
class MetalScopeFrame:
    """一次示波器需求的完成描述（纹理留在设备上；无像素数组）。"""

    revision: int
    source_revision: int
    mode: str
    textures: Dict[str, Tuple[Any, Tuple[int, int]]] = field(default_factory=dict)
    analysis_ms: float = 0.0
    #: 供面板拒绝迟到结果（与 ScopeGpuFrame.sample_identity 同契约）
    sample_identity: Any = None

    def readback_free(self) -> bool:
        return not self.textures


class MetalScopeEngine:
    """compute 示波器引擎（渲染线程独占；无 Qt / 无 GL）。"""

    def __init__(self, ctx: MetalComputeContext) -> None:
        import Metal  # noqa: F401

        self._metal = Metal
        self.ctx = ctx
        self._kernels: Dict[str, Any] = {}
        self._pool: Dict[Tuple[int, int], dict] = {}
        self._active_size: Optional[Tuple[int, int]] = None
        self._pending: List[Tuple[int, Any]] = []   # (revision, demand)
        self._lock = threading.Lock()
        self._awaiting_submit = 0        # 已提交未完成的 command buffer 数
        self._done_submit = 0
        self._last_error: Optional[str] = None
        self.stats: Dict[str, int] = {
            "demands": 0, "submitted": 0, "published": 0,
            "dropped_stale": 0, "display_readbacks": 0,
        }

    # ── kernel ──

    def _kernel(self, name: str) -> Any:
        kernel = self._kernels.get(name)
        if kernel is None:
            kernel = self.ctx.compile_kernel(name, METAL_SCOPE_MSL)
            self._kernels[name] = kernel
        return kernel

    @property
    def _rgba16f(self) -> Any:
        return self._metal.MTLPixelFormatRGBA16Float

    @property
    def _rgba8(self) -> Any:
        return self._metal.MTLPixelFormatRGBA8Unorm

    # ── 资源池（按分析尺寸缓存；尺寸变化整体作废——N09 债务语义） ──

    def _ensure_pool(self, analysis: Tuple[int, int],
                     waveform: Tuple[int, int],
                     vectorscope: Tuple[int, int]) -> dict:
        key = tuple(analysis)
        if key != self._active_size or key not in self._pool:
            self._release_pool()
            aw, ah = analysis
            ww, wh = waveform
            vw, vh = vectorscope
            pool = {
                "analysis": self.ctx.create_texture_2d(
                    self._rgba16f, aw, ah, shader_write=True),
                "waveform_density": self.ctx.create_buffer(
                    ww * wh * 5 * 4),
                "vectorscope_density": self.ctx.create_buffer(vw * vh * 4),
                "histogram": self.ctx.create_buffer(HISTOGRAM_BINS * 4 * 4),
                "cie_density": self.ctx.create_buffer(384 * 432 * 4),
                "sizes": {
                    "analysis": (aw, ah), "waveform": (ww, wh),
                    "vectorscope": (vw, vh), "cie": (384, 432),
                },
                "displays": {},
            }
            self._pool[key] = pool
            self._active_size = key
        return self._pool[key]

    def _display_texture(self, pool: dict, name: str,
                         size: Tuple[int, int]) -> Any:
        displays = pool["displays"]
        current = displays.get(name)
        if current is not None and pool["sizes"].get(name) == size:
            return current
        texture = self.ctx.create_texture_2d(
            self._rgba8, size[0], size[1], shader_write=True)
        displays[name] = texture
        pool["sizes"][name] = size
        return texture

    def _release_pool(self) -> None:
        # MTLBuffer/MTLTexture 由 Python 引用离场释放（无 GL deferred GC 需求）
        self._pool.clear()
        self._active_size = None

    def release(self) -> None:
        self._release_pool()
        self._kernels.clear()

    # ── 标量绑定助手（dispatch 的 buffers 按参数序） ──

    # ── 主入口 ──

    def render_demand(
        self,
        demand: Any,
        source_texture: Any,
        *,
        source_size: Optional[Tuple[int, int]] = None,
    ) -> bool:
        """Encode and register a demand for async publication through poll."""
        frame = self.encode_display(demand, source_texture, source_size=source_size)
        if frame is None:
            return False
        self._pending.append((int(demand.revision), demand))
        return True

    def encode_display(
        self,
        demand: Any,
        source_texture: Any,
        *,
        source_size: Optional[Tuple[int, int]] = None,
    ) -> Optional[MetalScopeFrame]:
        """Encode the scope and expose its textures for same-command-buffer use."""
        from src.features.color.core.scope_types import (
            DEFAULT_CIE_SIZE,
            DEFAULT_HISTOGRAM_DISPLAY_SIZE,
            DEFAULT_VECTORSCOPE_DENSITY_SIZE,
            DEFAULT_WAVEFORM_DENSITY_SIZE,
            mode_needs_cie,
            mode_needs_falsecolor,
            mode_needs_histogram,
            mode_needs_luma,
            mode_needs_parade,
            mode_needs_vectorscope,
            mode_needs_waveform,
        )

        if mode_needs_falsecolor(demand.mode):
            # falsecolor 单 pass 读源纹理——Metal 侧未接线（如实拒绝，
            # 不产出少效果结果）
            return None
        width = int(source_texture.width())
        height = int(source_texture.height())
        if source_size not in (None, (0, 0)) and tuple(source_size) \
                != (width, height):
            return None
        analysis = tuple(int(v) for v in demand.analysis_size)
        pool = self._ensure_pool(
            analysis, DEFAULT_WAVEFORM_DENSITY_SIZE,
            DEFAULT_VECTORSCOPE_DENSITY_SIZE)
        aw, ah = analysis
        ww, wh = pool["sizes"]["waveform"]
        vw, vh = pool["sizes"]["vectorscope"]
        mode = demand.mode

        # 1. 采样（所有密度 pass 共享一次降采样）
        self.ctx.dispatch(
            self._kernel("scope_sample"),
            buffers={},
            textures={0: source_texture, 1: pool["analysis"]},
            threads=(aw, ah, 1))

        needs_waveform = mode_needs_waveform(mode)
        needs_parade = mode_needs_parade(mode)
        needs_luma = mode_needs_luma(mode)
        needs_vectorscope = mode_needs_vectorscope(mode)
        needs_histogram = mode_needs_histogram(mode)
        needs_cie = mode_needs_cie(mode)

        # 2/3. 密度累加 + 展示（**逐项交错**：波形族共享同一密度 buffer，
        #      清零→累加→展示按编码序执行——与 GL 单 FBO 的 pass 顺序
        #      同语义；全部只 dispatch，不提交不等待）
        for scope_mode, name, single in (
                (0, "waveform", False), (1, "parade", False),
                (2, "luma", True)):
            wanted = {0: needs_waveform, 1: needs_parade,
                      2: needs_luma}[scope_mode]
            if not wanted:
                continue
            self._clear(pool["waveform_density"], ww * wh * 5)
            self._waveform_accum(pool, scope_mode, getattr(demand, "show_waveform_excursions", True))
            self._display_waveform(pool, name, (ww, wh),
                                   single_channel=single)
        if needs_vectorscope:
            self._clear(pool["vectorscope_density"], vw * vh)
            self._vectorscope_accum(pool, demand.vectorscope_zoom)
            self._display_vectorscope(pool, demand.vectorscope_zoom)
        if needs_histogram:
            self._clear(pool["histogram"], HISTOGRAM_BINS * 4)
            self._histogram_accum(pool, getattr(demand, "curves_mode",
                                                "Custom"))
            self._display_histogram(pool, DEFAULT_HISTOGRAM_DISPLAY_SIZE)
        if needs_cie:
            self._clear(pool["cie_density"], 384 * 432)
            self._cie_accum(pool,
                            getattr(demand, "scope_transfer", "srgb"),
                            getattr(demand, "scope_primaries", "bt709"))
            self._display_cie(pool, DEFAULT_CIE_SIZE)

        self.stats["demands"] += 1
        return self._make_frame(demand)

    # ── 提交/完成 ──

    def submit_async(self) -> bool:
        """提交已编码的示波器工作（completion 回调只记录；poll 发布）。

        播放期间不阻塞视口：无 wait/finish。
        """
        if not self.ctx.has_pending_work():
            return False
        with self._lock:
            self._awaiting_submit += 1
        self.ctx.submit(completion=self._on_complete)
        self.stats["submitted"] += 1
        return True

    def _on_complete(self, command_buffer: Any) -> None:
        # 驱动线程：只计数（不触资源、不回调用户代码）
        error = None
        if command_buffer is not None and command_buffer.error() is not None:
            error = str(command_buffer.error())
        with self._lock:
            self._done_submit += 1
            if error is not None:
                self._last_error = error

    @property
    def gpu_busy(self) -> bool:
        """是否有已提交未完成的示波器工作（诊断/测试用）。"""
        with self._lock:
            return self._done_submit < self._awaiting_submit

    def poll(self) -> List[MetalScopeFrame]:
        """发布已完成的帧（渲染线程；latest-wins 丢弃过期需求）。

        GPU 未完成时返回空（不阻塞、不提前发布半帧）。
        """
        with self._lock:
            # 未提交（awaiting==0）或已提交未完成（done<awaiting）都不发布
            if self._awaiting_submit == 0 \
                    or self._done_submit < self._awaiting_submit:
                return []
            error, self._last_error = self._last_error, None
            pending = self._pending
            self._pending = []
            self._awaiting_submit = 0
            self._done_submit = 0
        frames: List[MetalScopeFrame] = []
        if error is not None:
            self.stats["dropped_stale"] += len(pending)
            return frames
        latest_revision = pending[-1][0] if pending else -1
        for revision, demand in pending:
            if revision != latest_revision:
                self.stats["dropped_stale"] += 1
                continue
            frames.append(self._make_frame(demand))
            self.stats["published"] += 1
        return frames

    def _make_frame(self, demand: Any) -> MetalScopeFrame:
        from src.features.color.core.scope_types import (
            DEFAULT_CIE_SIZE,
            DEFAULT_HISTOGRAM_DISPLAY_SIZE,
            mode_needs_cie, mode_needs_histogram, mode_needs_luma,
            mode_needs_parade, mode_needs_vectorscope, mode_needs_waveform,
        )

        pool = self._pool.get(self._active_size or (-1, -1))
        textures: Dict[str, Tuple[Any, Tuple[int, int]]] = {}
        if pool is not None:
            if mode_needs_waveform(demand.mode):
                textures["waveform"] = (
                    pool["displays"]["waveform"], pool["sizes"]["waveform"])
            if mode_needs_parade(demand.mode):
                textures["parade"] = (
                    pool["displays"]["parade"], pool["sizes"]["waveform"])
            if mode_needs_luma(demand.mode):
                textures["luma"] = (
                    pool["displays"]["luma"], pool["sizes"]["waveform"])
            if mode_needs_vectorscope(demand.mode):
                textures["vectorscope"] = (
                    pool["displays"]["vectorscope"],
                    pool["sizes"]["vectorscope"])
            if mode_needs_histogram(demand.mode):
                textures["histogram"] = (
                    pool["displays"]["histogram"],
                    DEFAULT_HISTOGRAM_DISPLAY_SIZE)
            if mode_needs_cie(demand.mode):
                textures["cie"] = (pool["displays"]["cie"], DEFAULT_CIE_SIZE)
        return MetalScopeFrame(
            revision=int(demand.revision),
            source_revision=int(demand.source_revision),
            mode=str(demand.mode),
            textures=textures,
            sample_identity=getattr(demand, "sample_identity", None))

    # ── 展示图读回（小尺寸显示面；计数区别于显式像素读回） ──

    def read_display(self, frame: MetalScopeFrame, name: str) -> np.ndarray:
        """读回一张展示图 (H, W, 4) uint8（展示统计读回，非逐像素分析）。"""
        texture, size = frame.textures[name]
        width, height = size
        self.stats["display_readbacks"] += 1
        raw = self.ctx.read_texture(
            texture, width * 4, height, dtype=np.uint8, region_width=width)
        return raw.reshape(height, width, 4)

    # ── 统计读回（对拍/诊断：密度与直方图计数；GPU 完成后调用） ──

    def _pool_or_none(self) -> Optional[dict]:
        return self._pool.get(self._active_size or (-1, -1))

    def read_waveform_density(self) -> np.ndarray:
        """(5, H, W) uint32：r/g/b/over/under 平面计数（对拍验证用）。"""
        pool = self._pool_or_none()
        assert pool is not None, "无活动资源池"
        ww, wh = pool["sizes"]["waveform"]
        raw = self.ctx.read_buffer(pool["waveform_density"])
        return np.frombuffer(raw, dtype=np.uint32).reshape(5, wh, ww).copy()

    def read_vectorscope_density(self) -> np.ndarray:
        """(H, W) uint32 矢量密度计数。"""
        pool = self._pool_or_none()
        assert pool is not None, "无活动资源池"
        vw, vh = pool["sizes"]["vectorscope"]
        raw = self.ctx.read_buffer(pool["vectorscope_density"])
        return np.frombuffer(raw, dtype=np.uint32).reshape(vh, vw).copy()

    def read_histogram_counts(self) -> np.ndarray:
        """(4, 512) uint32：R/G/B/Y 列计数。"""
        pool = self._pool_or_none()
        assert pool is not None, "无活动资源池"
        raw = self.ctx.read_buffer(pool["histogram"])
        return np.frombuffer(raw, dtype=np.uint32).reshape(4, 512).copy()

    def read_cie_density(self) -> np.ndarray:
        """(H, W) uint32 CIE xy 密度计数（已含 1/64 预缩刻度）。"""
        pool = self._pool_or_none()
        assert pool is not None, "无活动资源池"
        cw, ch = pool["sizes"]["cie"]
        raw = self.ctx.read_buffer(pool["cie_density"])
        return np.frombuffer(raw, dtype=np.uint32).reshape(ch, cw).copy()

    # ── 内部 dispatch ──

    def _clear(self, buffer: Any, length: int) -> None:
        length_buf = self.ctx.create_buffer_from_numpy(
            np.array([np.uint32(length)]))
        self.ctx.dispatch(
            self._kernel("scope_clear_buffer"),
            buffers={0: buffer, 1: length_buf},
            threads=(int(length), 1, 1))

    def _int_buffer(self, value: int) -> Any:
        return self.ctx.create_buffer_from_numpy(
            np.array([np.int32(value)]))

    def _float_buffer(self, value: float) -> Any:
        return self.ctx.create_buffer_from_numpy(
            np.array([np.float32(value)]))

    def _waveform_accum(self, pool: dict, mode: int, show_excursions: bool = True) -> None:
        aw, ah = pool["sizes"]["analysis"]
        ww, wh = pool["sizes"]["waveform"]
        range_min, range_max = waveform_plot_range(show_excursions)
        self.ctx.dispatch(
            self._kernel("scope_waveform_accum"),
            buffers={
                0: pool["waveform_density"],
                1: self._int_buffer(mode),
                2: self._int_buffer(int(show_excursions)),
                3: self._int_buffer(aw),
                4: self._int_buffer(ah),
                5: self._int_buffer(ww),
                6: self._int_buffer(wh),
                7: self._float_buffer(range_min),
                8: self._float_buffer(range_max),
            },
            textures={0: pool["analysis"]},
            threads=(aw, ah, 1))

    def _vectorscope_accum(self, pool: dict, zoom: float) -> None:
        aw, ah = pool["sizes"]["analysis"]
        vw, vh = pool["sizes"]["vectorscope"]
        self.ctx.dispatch(
            self._kernel("scope_vectorscope_accum"),
            buffers={
                0: pool["vectorscope_density"],
                1: self._int_buffer(aw),
                2: self._int_buffer(ah),
                3: self._int_buffer(vw),
                4: self._int_buffer(vh),
                5: self._float_buffer(float(zoom)),
                6: self._int_buffer(0),
            },
            textures={0: pool["analysis"]},
            threads=(aw, ah, 1))

    def _histogram_accum(self, pool: dict, curve_mode: str) -> None:
        aw, ah = pool["sizes"]["analysis"]
        axis = 1 if str(curve_mode).startswith("Hue") else (
            2 if str(curve_mode).startswith("Sat") else (
                3 if str(curve_mode) == "LumSat" else 0))
        self.ctx.dispatch(
            self._kernel("scope_histogram_accum"),
            buffers={
                0: pool["histogram"],
                1: self._int_buffer(aw),
                2: self._int_buffer(ah),
                3: self._int_buffer(axis),
                4: self._int_buffer(0),
                5: self._int_buffer(0),
            },
            textures={0: pool["analysis"]},
            threads=(aw, ah, 1))

    def _cie_accum(self, pool: dict, transfer: str, primaries: str) -> None:
        aw, ah = pool["sizes"]["analysis"]
        cw, chh = pool["sizes"]["cie"]
        self.ctx.dispatch(
            self._kernel("scope_cie_accum"),
            buffers={
                0: pool["cie_density"],
                1: self._int_buffer(aw),
                2: self._int_buffer(ah),
                3: self._int_buffer(cw),
                4: self._int_buffer(chh),
                5: self._int_buffer(_TRANSFER_CODES.get(transfer, 0)),
                6: self._int_buffer(_PRIMARY_CODES.get(primaries, 0)),
                7: self._int_buffer(0),
            },
            textures={0: pool["analysis"]},
            threads=(aw, ah, 1))

    def _display_waveform(self, pool: dict, name: str,
                          size: Tuple[int, int], *,
                          single_channel: bool) -> None:
        ww, wh = size
        ah = pool["sizes"]["analysis"][1]
        texture = self._display_texture(pool, name, size)
        self.ctx.dispatch(
            self._kernel("scope_display_waveform"),
            buffers={
                0: pool["waveform_density"],
                1: self._int_buffer(ww),
                2: self._int_buffer(wh),
                3: self._float_buffer(
                    1.0 / expected_waveform_peak(ah, single_channel)),
                4: self._float_buffer(1.0 / WEIGHT_SCALE),
            },
            textures={0: texture},
            threads=(ww, wh, 1))

    def _display_vectorscope(self, pool: dict, zoom: float) -> None:
        vw, vh = pool["sizes"]["vectorscope"]
        texture = self._display_texture(pool, "vectorscope", (vw, vh))
        self.ctx.dispatch(
            self._kernel("scope_display_vectorscope"),
            buffers={
                0: pool["vectorscope_density"],
                1: self._int_buffer(vw),
                2: self._int_buffer(vh),
                3: self._float_buffer(1.0),
                4: self._float_buffer(float(zoom)),
                5: self._float_buffer(1.0 / WEIGHT_SCALE),
                6: self._int_buffer(0),
            },
            textures={0: texture},
            threads=(vw, vh, 1))

    def _display_histogram(self, pool: dict,
                           size: Tuple[int, int]) -> None:
        aw, ah = pool["sizes"]["analysis"]
        texture = self._display_texture(pool, "histogram", size)
        self.ctx.dispatch(
            self._kernel("scope_display_histogram"),
            buffers={
                0: pool["histogram"],
                1: self._int_buffer(size[0]),
                2: self._int_buffer(size[1]),
                3: self._float_buffer(
                    expected_histogram_max_inv(aw * ah)),
                4: self._float_buffer(1.0 / WEIGHT_SCALE),
                5: self._int_buffer(0),
                6: self._int_buffer(0),
            },
            textures={0: texture},
            threads=(size[0], size[1], 1))

    def _display_cie(self, pool: dict, size: Tuple[int, int]) -> None:
        aw, ah = pool["sizes"]["analysis"]
        texture = self._display_texture(pool, "cie", size)
        self.ctx.dispatch(
            self._kernel("scope_display_cie"),
            buffers={
                0: pool["cie_density"],
                1: self._int_buffer(size[0]),
                2: self._int_buffer(size[1]),
                3: self._float_buffer(expected_cie_max_inv(aw * ah)),
                4: self._float_buffer(1.0 / WEIGHT_SCALE),
                5: self._int_buffer(0),
                6: self._int_buffer(0),
            },
            textures={0: texture},
            threads=(size[0], size[1], 1))


__all__ = [
    "CIE_PRESCALE",
    "CIE_X_RANGE",
    "CIE_Y_RANGE",
    "DISPLAY_PIXEL_FORMAT",
    "HISTOGRAM_BINS",
    "MetalScopeEngine",
    "MetalScopeFrame",
    "SCOPE_RANGE_MAX",
    "SCOPE_RANGE_MIN",
    "WEIGHT_SCALE",
    "expected_cie_max_inv",
    "expected_histogram_max_inv",
    "expected_waveform_peak",
]
