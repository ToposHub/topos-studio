"""调色节点图的 Metal 纹理原生 kernels（迁移计划阶段 4.2/4.3）。

与 ``src/shared/gpu/metal/kernels_color.py`` 的 buffer 载体 kernels
（``METAL_GRAPH_OPS_MSL``，NumPy staging 桥参考实现）共享**同一份点态
调色数学**：本模块直接拼接 ``METAL_POINTWISE_GRADING_MSL``（grading/
curves 的权威翻译），再叠加 ``shader_nodes_msl`` 已验证（msl_verified）
的效果片段（HSL 曲线 / LUT3D / mixer / warper / blend modes / soft
clip），按 Metal **纹理**（``texture2d<float, access::read/write>``）
重新发射图算子——输入输出为同尺寸浮点 RGBA 纹理，遮罩为同尺寸单通道
（R16Float）纹理，不再经过 NumPy 暂存（迁移计划 §阶段 4 / 参考实现 4A）。

约束（.clinerules + 计划）：

- **HDR**：RGB 路径不 clamp（matte/key 域按 spec 有界）；数值全部带
  ``.0``；循环上限为编译期常量。
- **采样器**：MetalComputeContext.dispatch 只支持 buffers/textures
  绑定，sampler 一律用 ``constexpr sampler``（linear + clamp_to_edge，
  与 GL 生产纹理的 LINEAR/CLAMP_TO_EDGE 同语义）。
- **语义权威**：corrector 段序 = GL composition main
  （shader_compiler.py）——measured primary → grading → curves → hsl_curve →
  soft_clip → curves_soft_clip → mixer → warper → lut3d，尾部
  ``mix(src, apply_blend_mode(src, graded), matte*opacity)``。
- **qualifier**：keyer.glsl 的逐条翻译；hue 域经 ``fract(h+0.5)`` 预移
  ——宿主打包参数时按 M06 对拍口径把 CPU 域的 ``hue_center`` 换算到
  shader 域（``(center + 0.5) % 1``）。
- ``test_linear_gain_with_matte``：迁移计划「参考实现 4A」原文 kernel
  （GPU 接线验证用；乘法作用在线性空间，原 Alpha 原样写出）。

本模块只做源码生成与参数打包（纯 CPU、无线程无 GPU 资源）；执行归
``color_graph_texture_metal.ColorGraphMetalTextureExecutor``。
"""

from __future__ import annotations

from typing import Any, Mapping, Tuple

import numpy as np

from src.features.color.core.midtone_detail_calibration import (
    DETAIL_SHADER_GAIN, detail_shader_linear_sampling, detail_shader_taps,
)
from src.features.color.core.mist import MIST_MSL
from src.features.color.core.shader_nodes_msl import (
    MSL_BLEND_MODES,
    MSL_CURVES,
    MSL_CURVES_HELPERS,
    MSL_CURVES_SOFT_CLIP,
    MSL_HSV_FUNCS,
    MSL_LUT3D,
    MSL_MIXER,
    MSL_RESOLVE_HS709,
    MSL_RESOLVE_HS709_V2,
    MSL_SOFT_CLIP,
    MSL_SOFT_CLIP_HELPERS,
    MSL_WARPER,
    MSL_WARPER_CHROMA709,
)
from src.features.color.core.warper_color import (
    RESOLVE_HS709_SPACE, RESOLVE_HS709_V2_SPACE, RESOLVE_HS709_V3_SPACE, WARPER_CHROMA709_SPACE,
    WARPER_X_GRADE_SPACE,
)
from src.features.color.core.x_grade_shader import MSL_X_GRADE
from src.features.color.core.base_grade_runtime import metal_graph_base_grade_source
from src.features.color.core.chromogen_runtime import metal_managed_grading_source
from src.shared.gpu.metal.kernels_color import build_metal_pointwise_grading_msl

_MSL_HEADER = """#include <metal_stdlib>
using namespace metal;
"""

# 与 GL 生产纹理（LINEAR + CLAMP_TO_EDGE）同语义；texture3d 的 LUT 采样
# 由此获得 trilinear 插值（filter 是 sampler 属性，不是纹理属性）。
_TEX_SAMPLERS = """
constexpr sampler tg_sampler(coord::normalized, filter::linear,
                              address::clamp_to_edge);
"""

# ── TexExtraParams：corrector 的扩展域参数（4 字节标量紧排） ────────────
#
# GraphGradingParams（kernels_color，172 B）保持字节不变，经独立 buffer
# 绑定；本 struct 只承载纹理域扩展位与参数。字段顺序与
# TEX_EXTRA_PARAMS_DTYPE 逐一对齐（不得单侧插入字段）。

_TEX_EXTRA_PARAMS_MSL = """
struct TexExtraParams {
    uint  has_hsl;                 // HSL 曲线纹理位（u_hsl_curve_tex）
    uint  has_lut3d;               // LUT3D 纹理位（u_lut_tex）
    uint  has_mixer;               // 8 通道混色器位
    uint  has_warper;              // Warper 纹理位
    uint  has_soft_clip;           // legacy 8 参数 soft clip 位
    uint  has_curves_soft_clip;    // 零为禁用，其余为算法版本加一
    int   blend_mode;              // Layer.BLEND_MODES 索引（0=Normal）
    float lut_intensity;           // LUT3D 强度（R05）
    int   warper_space;            // legacy HSV / calibrated 709
    float sc_high;
    float sc_white_point;
    float sc_shoulder_strength;
    float sc_low;
    float sc_black_point;
    float sc_toe_strength;
    float sc_exposure;
    float sc_contrast;
    float cs_low_clip_x;
    float cs_low_clip_y;
    float cs_low_clip_z;
    float cs_low_soft_x;
    float cs_low_soft_y;
    float cs_low_soft_z;
    float cs_high_clip_x;
    float cs_high_clip_y;
    float cs_high_clip_z;
    float cs_high_soft_x;
    float cs_high_soft_y;
    float cs_high_soft_z;
    float mixer[24];               // 8 通道 × (x, y, z) 平铺
    uint  has_primary_calibration;
    float mid_detail;
    uint  mid_detail_calibrated;
    float mist_spread;
    float mist_amount;
    uint  rec709_calibration_encoded;
    uint  mist_calibrated;
};
"""

_TEX_QUALIFIER_PARAMS_MSL = """
// keyer.glsl 的 uniform 布尔 → uint（constant 地址空间 4 字节语义）
struct TexQualifierParams {
    uint  h_enabled;
    float h_center;                // shader 域（宿主已 (c+0.5)%1 预移）
    float h_width;
    float h_soft;
    uint  s_enabled;
    float s_low;
    float s_high;
    float s_s_low;
    float s_s_high;
    uint  l_enabled;
    float l_low;
    float l_high;
    float l_s_low;
    float l_s_high;
    uint  invert;
};
"""

# ── 参考实现 4A（迁移计划原文） ──────────────────────────────────────────

_REFERENCE_4A_KERNEL_MSL = """
kernel void test_linear_gain_with_matte(
    texture2d<float, access::read> source [[texture(0)]],
    texture2d<float, access::read> matte [[texture(1)]],
    texture2d<float, access::write> output [[texture(2)]],
    constant float& gain [[buffer(0)]],
    uint2 p [[thread_position_in_grid]]
) {
    if (p.x >= output.get_width() || p.y >= output.get_height()) {
        return;
    }
    const float4 input = source.read(p);
    const float coverage = clamp(matte.read(p).r, 0.0f, 1.0f);
    const float3 rgb = mix(input.rgb, input.rgb * gain, coverage);
    output.write(float4(rgb, input.a), p);
}
"""

# ── 图算子 kernels（纹理载体） ────────────────────────────────────────────
#
# 数学与 kernels_color 的 buffer 版（_GRAPH_COPY_MIX_MSL /
# _GRAPH_NODE_OPS_MSL）逐条同式；corrector 段序 = GL composition main。

_TEX_GRAPH_KERNELS_MSL = """
static inline float3 tg_mix3(float3 x, float3 y, float t) {
    return x * (1.0 - t) + y * t;
}

kernel void tg_copy(
    texture2d<float, access::read> src [[texture(0)]],
    texture2d<float, access::write> out [[texture(1)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out.get_width() || gid.y >= out.get_height()) { return; }
    out.write(src.read(gid), gid);
}

static inline float3 tg_apply_primary_calibration(
    float3 color, texture3d<float, access::sample> response_tex, bool encoded_input)
{
    float3 sdr = clamp(color, 0.0, 1.0);
    float3 encoded = encoded_input ? sdr : pow(sdr, float3(1.0 / 2.4));
    float grid_size = float(response_tex.get_width());
    float3 uv = (encoded * (grid_size - 1.0) + 0.5) / grid_size;
    float3 delta = response_tex.sample(tg_sampler, uv).rgb;
    float3 mapped = clamp(encoded + delta, 0.0, 1.0);
    float3 graded = encoded_input ? mapped : pow(mapped, float3(2.4));
    return graded + (color - sdr);
}

static inline float3 tg_apply_mid_detail(
    float3 color, texture2d<float, access::sample> src, uint2 gid, float amount)
{
    float2 size = float2(src.get_width(), src.get_height());
    float2 uv = (float2(gid) + 0.5) / size;
    float radius = 3.0 * (size.x / 2048.0);
    float2 dx = float2(radius / size.x, 0.0);
    float2 dy = float2(0.0, radius / size.y);
    float3 average = (src.sample(tg_sampler, uv + dx).rgb
                    + src.sample(tg_sampler, uv - dx).rgb
                    + src.sample(tg_sampler, uv + dy).rgb
                    + src.sample(tg_sampler, uv - dy).rgb) * 0.25;
    float lum = dot(color, float3(0.299, 0.587, 0.114));
    float weight = smoothstep(0.0, 0.5, lum) * (1.0 - smoothstep(0.5, 1.0, lum)) * 4.0;
    return color + (color - average) * (amount / 100.0) * weight;
}

""" + detail_shader_linear_sampling(metal=True) + """
static inline float3 tg_apply_calibrated_detail(
    float3 color, float3 source_color, texture2d<float, access::sample> src, uint2 gid, float amount,
    bool encoded_input)
{
    if (encoded_input) source_color = detail_decode_rec709(source_color);
    const float3 taps[6] = {""" + detail_shader_taps("float3") + """};
    const float3 luma = float3(0.2126, 0.7152, 0.0722);
    float2 size = float2(src.get_width(), src.get_height());
    float2 uv = (float2(gid) + 0.5) / size;
    float2 scale = (size.x / 1920.0) / size;
    float3 mean = float3(0.0);
    float second = 0.0;
    for (int i = 0; i < 6; ++i) {
        float2 offset = taps[i].xy;
        for (int j = 0; j < 4; ++j) {
            float2 sample_uv = uv + scale * offset;
            float3 pixel = encoded_input
                ? detail_sample_linear(src, sample_uv, size)
                : src.sample(tg_sampler, sample_uv).rgb;
            mean += pixel * taps[i].z;
            second += dot(pixel * pixel, luma) * taps[i].z;
            offset = float2(-offset.y, offset.x);
        }
    }
    float center_luma = dot(source_color, luma);
    float blur_luma = dot(mean, luma);
    float variance = second - dot(mean * mean, luma);
""" + DETAIL_SHADER_GAIN + """
}

// Corrector 单 pass：mist → primary calibration → grading → curves(buffer) → hsl → soft_clip →
// curves_soft_clip → mixer → warper → lut3d → blend/matte epilogue。
// 禁用域的纹理槽仍须绑定有效纹理（占位 identity），由 flag 守卫不读取。
kernel void tg_corrector(
    texture2d<float, access::sample> src  [[texture(0)]],
    texture2d<float, access::read> matte  [[texture(1)]],
    texture2d<float, access::write> out   [[texture(2)]],
    texture2d<float, access::sample> hsl_tex    [[texture(3)]],
    texture2d<float, access::sample> warper_tex [[texture(4)]],
    texture3d<float, access::sample> lut_tex    [[texture(5)]],
    texture3d<float, access::sample> primary_tex [[texture(6)]],
    device const float* curves                [[buffer(0)]],
    constant GraphGradingParams& g            [[buffer(1)]],
    constant TexExtraParams& x                [[buffer(2)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out.get_width() || gid.y >= out.get_height()) { return; }
    float4 src4 = src.read(gid);
    float3 color = src4.rgb;

    if (x.mist_amount != 0.0) {
        float2 size = float2(src.get_width(), src.get_height());
        float2 uv = (float2(gid) + 0.5) / size;
        color += mist_delta(src, tg_sampler, uv, color, size, x.mist_spread,
                            x.mist_amount, x.mist_calibrated != 0u);
    }
    float3 source_color = color;
    bool encoded_input = x.rec709_calibration_encoded != 0u;
    if (x.has_primary_calibration != 0u) {
        color = tg_apply_primary_calibration(color, primary_tex, encoded_input);
    }
    color = graph_grading_before_detail(color, g);
    if (x.mid_detail != 0.0) {
        color = x.mid_detail_calibrated != 0u
            ? tg_apply_calibrated_detail(color, source_color, src, gid, x.mid_detail, encoded_input)
            : tg_apply_mid_detail(color, src, gid, x.mid_detail);
    }
    color = graph_grading_after_detail(color, source_color, g);
    if (g.has_curves == 2u) {
        color = graph_apply_calibrated_curves(color, curves, encoded_input);
    } else if (g.has_curves != 0u) {
        color = graph_apply_curves(color, curves);
    }
    if (x.has_hsl != 0u) {
        color = msl_apply_hsl_curves(color, hsl_tex, tg_sampler, encoded_input);
    }
    if (x.has_soft_clip != 0u) {
        color = msl_apply_soft_clip(color, x.sc_high, x.sc_white_point,
                                    x.sc_shoulder_strength, x.sc_low,
                                    x.sc_black_point, x.sc_toe_strength,
                                    x.sc_exposure, x.sc_contrast);
    }
    if (x.has_curves_soft_clip != 0u) {
        color = msl_apply_curves_soft_clip_profile(
            color,
            float3(x.cs_low_clip_x, x.cs_low_clip_y, x.cs_low_clip_z),
            float3(x.cs_low_soft_x, x.cs_low_soft_y, x.cs_low_soft_z),
            float3(x.cs_high_clip_x, x.cs_high_clip_y, x.cs_high_clip_z),
            float3(x.cs_high_soft_x, x.cs_high_soft_y, x.cs_high_soft_z),
            int(x.has_curves_soft_clip - 1u), encoded_input);
    }
    if (x.has_mixer != 0u) {
        color = msl_apply_mixer(
            color,
            float3(x.mixer[0], x.mixer[1], x.mixer[2]),
            float3(x.mixer[3], x.mixer[4], x.mixer[5]),
            float3(x.mixer[6], x.mixer[7], x.mixer[8]),
            float3(x.mixer[9], x.mixer[10], x.mixer[11]),
            float3(x.mixer[12], x.mixer[13], x.mixer[14]),
            float3(x.mixer[15], x.mixer[16], x.mixer[17]),
            float3(x.mixer[18], x.mixer[19], x.mixer[20]),
            float3(x.mixer[21], x.mixer[22], x.mixer[23]));
    }
    if (x.has_warper != 0u) {
        if (x.warper_space == tg_warper_x_grade) {
            color = msl_apply_x_grade(color, warper_tex, encoded_input);
        } else if (x.warper_space == tg_warper_chroma709) {
            color = msl_apply_warper_chroma709(color, warper_tex, encoded_input);
        } else if (x.warper_space == tg_warper_hs709_v2 || x.warper_space == tg_warper_hs709_v3) {
            color = msl_apply_resolve_hs709_v2(color, warper_tex, encoded_input);
        } else if (x.warper_space == tg_warper_hs709) {
            color = msl_apply_resolve_hs709(color, warper_tex, encoded_input);
        } else {
            color = msl_apply_warper(color, warper_tex, tg_sampler, 0.0);
        }
    }
    if (x.has_lut3d != 0u) {
        color = msl_apply_3dlut(color, lut_tex, tg_sampler,
                                x.lut_intensity);
    }

    // composition main 尾部：blend epilogue + matte*opacity 混合
    float k = g.opacity;
    if (g.mask_mode == 1u) {
        k = clamp(matte.read(gid).r, 0.0, 1.0) * g.opacity;
    }
    float3 blended = color;
    if (x.blend_mode > 0) {
        blended = msl_apply_blend_mode(src4.rgb, color, x.blend_mode);
    }
    out.write(float4(tg_mix3(src4.rgb, blended, k), src4.a), gid);
}

// keyer.glsl 的逐条翻译（rgb_to_hsl 含 HDR 归一；hue 域 +0.5 预移）
static inline float3 tg_rgb_to_hsl(float3 color) {
    float3 c = max(color, float3(0.0));
    float max_orig = max(max(c.r, c.g), c.b);
    if (max_orig > 1.0) { c /= max_orig; }

    float min_val = min(min(c.r, c.g), c.b);
    float max_val = max(max(c.r, c.g), c.b);
    float delta = max_val - min_val;
    float h = 0.0;
    float s = 0.0;
    float l = (max_val + min_val) / 2.0;

    if (delta > 0.00001) {
        s = l < 0.5 ? delta / max((max_val + min_val), 0.00001)
                   : delta / max((2.0 - max_val - min_val), 0.00001);
        if (c.r == max_val) {
            h = (c.g - c.b) / delta + (c.g < c.b ? 6.0 : 0.0);
        } else if (c.g == max_val) {
            h = (c.b - c.r) / delta + 2.0;
        } else {
            h = (c.r - c.g) / delta + 4.0;
        }
        h /= 6.0;
    }
    if (max_orig > 1.0) { l *= max_orig; }
    return float3(h, s, l);
}

kernel void tg_qualifier(
    texture2d<float, access::read> src [[texture(0)]],
    texture2d<float, access::write> matte [[texture(1)]],
    constant TexQualifierParams& p [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= matte.get_width() || gid.y >= matte.get_height()) { return; }
    float3 hsl = tg_rgb_to_hsl(src.read(gid).rgb);

    float m = 1.0;
    if (p.h_enabled != 0u) {
        float h = fract(hsl.x + 0.5);
        float dist = fabs(h - p.h_center);
        if (dist > 0.5) { dist = 1.0 - dist; }
        float half_w = p.h_width * 0.5;
        m *= 1.0 - smoothstep(half_w, half_w + max(p.h_soft, 0.0001), dist);
    }
    if (p.s_enabled != 0u) {
        float s = hsl.y;
        float s_matte = 1.0;
        if (p.s_low > 0.0 || p.s_s_low > 0.0) {
            s_matte *= smoothstep(p.s_low - max(p.s_s_low, 0.0001),
                                  p.s_low, s);
        }
        if (p.s_high < 1.0 || p.s_s_high > 0.0) {
            s_matte *= (1.0 - smoothstep(p.s_high,
                                         p.s_high + max(p.s_s_high, 0.0001),
                                         s));
        }
        m *= s_matte;
    }
    if (p.l_enabled != 0u) {
        float l = hsl.z;
        float l_matte = 1.0;
        if (p.l_low > 0.0 || p.l_s_low > 0.0) {
            l_matte *= smoothstep(p.l_low - max(p.l_s_low, 0.0001),
                                  p.l_low, l);
        }
        if (p.l_high < 1.0 || p.l_s_high > 0.0) {
            l_matte *= (1.0 - smoothstep(p.l_high,
                                         p.l_high + max(p.l_s_high, 0.0001),
                                         l));
        }
        m *= l_matte;
    }
    if (p.invert != 0u) { m = 1.0 - m; }
    matte.write(float(clamp(m, 0.0, 1.0)), gid);
}

// spec §3：key 布尔混合（0=add/max、1=subtract、2=intersect），R 通道
kernel void tg_key_mix(
    texture2d<float, access::read> ka [[texture(0)]],
    texture2d<float, access::read> kb [[texture(1)]],
    texture2d<float, access::write> out [[texture(2)]],
    constant int& mode [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out.get_width() || gid.y >= out.get_height()) { return; }
    float a = clamp(ka.read(gid).r, 0.0, 1.0);
    float b = clamp(kb.read(gid).r, 0.0, 1.0);
    float k;
    if (mode == 0)      { k = max(a, b); }
    else if (mode == 1) { k = clamp(a - b, 0.0, 1.0); }
    else                { k = a * b; }
    out.write(float(k), gid);
}

// spec §4：key 补集（matte 域有界）
kernel void tg_key_complement(
    texture2d<float, access::read> key [[texture(0)]],
    texture2d<float, access::write> out [[texture(1)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out.get_width() || gid.y >= out.get_height()) { return; }
    float k = clamp(key.read(gid).r, 0.0, 1.0);
    out.write(float(1.0 - k), gid);
}

kernel void tg_key_passthrough(
    texture2d<float, access::read> key [[texture(0)]],
    texture2d<float, access::write> out [[texture(1)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out.get_width() || gid.y >= out.get_height()) { return; }
    float k = clamp(key.read(gid).r, 0.0, 1.0);
    out.write(float4(k, k, k, 1.0), gid);
}

// spec §5：channel 0/1/2 → 灰度载体（alpha 随图像）
kernel void tg_splitter(
    texture2d<float, access::read> src [[texture(0)]],
    texture2d<float, access::write> out [[texture(1)]],
    constant int& channel [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out.get_width() || gid.y >= out.get_height()) { return; }
    float4 t = src.read(gid);
    float v = t.r;
    if (channel == 1) { v = t.g; }
    else if (channel == 2) { v = t.b; }
    out.write(float4(float3(v), t.a), gid);
}

// spec §5：R/G/B 载体重组（alpha 取 R 载体）
kernel void tg_combiner(
    texture2d<float, access::read> r_in [[texture(0)]],
    texture2d<float, access::read> g_in [[texture(1)]],
    texture2d<float, access::read> b_in [[texture(2)]],
    texture2d<float, access::write> out [[texture(3)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out.get_width() || gid.y >= out.get_height()) { return; }
    float r = r_in.read(gid).r;
    float g = g_in.read(gid).g;
    float b = b_in.read(gid).b;
    float a = r_in.read(gid).a;
    out.write(float4(r, g, b, a), gid);
}

static inline float tg_overlay(float b, float a) {
    return b < 0.5 ? (2.0 * b * a) : (1.0 - 2.0 * (1.0 - b) * (1.0 - a));
}

static inline float tg_soft_light(float b, float a) {
    return (a < 0.5) ? (2.0 * b * a + b * b * (1.0 - 2.0 * a))
                     : (sqrt(max(b, 0.0)) * (2.0 * a - 1.0)
                        + 2.0 * b * (1.0 - a));
}

// spec §2 v1 + §9 v1.1（与 graph_layer_mix 同式；mode 索引 = LAYER_MODE_INDEX）
kernel void tg_layer_mix(
    texture2d<float, access::read> bottom [[texture(0)]],
    texture2d<float, access::read> top    [[texture(1)]],
    texture2d<float, access::write> out   [[texture(2)]],
    constant float& opacity [[buffer(0)]],
    constant int& mode [[buffer(1)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out.get_width() || gid.y >= out.get_height()) { return; }
    float4 bot = bottom.read(gid);
    float3 a = bot.rgb;
    float3 b = top.read(gid).rgb;
    float op = opacity;
    float3 outc;
    if (mode == 0)      { outc = a * (1.0 - op) + b * op; }
    else if (mode == 1) { outc = a + b * op; }
    else if (mode == 2) { outc = a - b * op; }
    else if (mode == 3) { outc = a * (1.0 - op + op * b); }
    else if (mode == 4) { outc = 1.0 - (1.0 - a) * (1.0 - b * op); }
    else {
        float3 blended;
        if (mode == 5) {
            blended = float3(tg_overlay(a.r, b.r), tg_overlay(a.g, b.g),
                             tg_overlay(a.b, b.b));
        } else if (mode == 6) {
            blended = float3(tg_soft_light(a.r, b.r),
                             tg_soft_light(a.g, b.g),
                             tg_soft_light(a.b, b.b));
        } else if (mode == 7) {
            blended = float3(tg_overlay(b.r, a.r), tg_overlay(b.g, a.g),
                             tg_overlay(b.b, a.b));
        } else if (mode == 8)  { blended = a / max(float3(0.001), 1.0 - b); }
        else if (mode == 9)  { blended = 1.0 - (1.0 - a) / max(float3(0.001), b); }
        else if (mode == 10) { blended = min(a, b); }
        else if (mode == 11) { blended = max(a, b); }
        else if (mode == 12) { blended = fabs(a - b); }
        else if (mode == 13) { blended = a + b - 2.0 * a * b; }
        else if (mode == 14) {
            float3 ha = graph_rgb2hsv(a); float3 hb = graph_rgb2hsv(b);
            blended = graph_hsv2rgb(float3(hb.x, ha.y, ha.z));
        } else if (mode == 15) {
            float3 ha = graph_rgb2hsv(a); float3 hb = graph_rgb2hsv(b);
            blended = graph_hsv2rgb(float3(ha.x, hb.y, ha.z));
        } else if (mode == 16) {
            float3 ha = graph_rgb2hsv(a); float3 hb = graph_rgb2hsv(b);
            blended = graph_hsv2rgb(float3(hb.x, hb.y, ha.z));
        } else if (mode == 17) {
            float3 ha = graph_rgb2hsv(a); float3 hb = graph_rgb2hsv(b);
            blended = graph_hsv2rgb(float3(ha.x, ha.y, hb.z));
        } else if (mode == 18) { blended = a + b; }
        else if (mode == 19) { blended = max(a - b, 0.0); }
        else                 { blended = a / max(float3(0.001), b); }
        outc = a * (1.0 - op) + blended * op;
    }
    out.write(float4(outc, bot.a), gid);
}

// spec §1：out.rgb = mix(base.rgb, branch.rgb, k)；alpha = base.a
kernel void tg_parallel_mix(
    texture2d<float, access::read> base   [[texture(0)]],
    texture2d<float, access::read> branch [[texture(1)]],
    texture2d<float, access::read> key    [[texture(2)]],
    texture2d<float, access::write> out   [[texture(3)]],
    uint2 gid [[thread_position_in_grid]])
{
    if (gid.x >= out.get_width() || gid.y >= out.get_height()) { return; }
    float4 b = base.read(gid);
    float3 br = branch.read(gid).rgb;
    float k = clamp(key.read(gid).r, 0.0, 1.0);
    out.write(float4(tg_mix3(b.rgb, br, k), b.a), gid);
}
"""

_TEX_GRAPH_BODY_MSL = (
    MSL_HSV_FUNCS
    + MSL_CURVES_HELPERS
    + MSL_CURVES                       # msl_apply_hsl_curves（纹理采样）
    + MSL_BLEND_MODES
    + MSL_LUT3D
    + MSL_MIXER
    + MSL_WARPER
    + MSL_WARPER_CHROMA709
    + MSL_RESOLVE_HS709
    + MSL_RESOLVE_HS709_V2
    + MSL_X_GRADE
    + MSL_SOFT_CLIP_HELPERS
    + MSL_SOFT_CLIP
    + MSL_CURVES_SOFT_CLIP
    + MIST_MSL
    + _TEX_SAMPLERS
    + _TEX_EXTRA_PARAMS_MSL
    + _TEX_QUALIFIER_PARAMS_MSL
    + f"\nconstant int tg_warper_hs709 = {RESOLVE_HS709_SPACE};\n"
    + f"constant int tg_warper_hs709_v2 = {RESOLVE_HS709_V2_SPACE};\n"
    + f"constant int tg_warper_hs709_v3 = {RESOLVE_HS709_V3_SPACE};\n"
    + f"constant int tg_warper_chroma709 = {WARPER_CHROMA709_SPACE};\n"
    + f"constant int tg_warper_x_grade = {WARPER_X_GRADE_SPACE};\n"
    + _TEX_GRAPH_KERNELS_MSL
)

# Keep the standard graph library independent of the look-stage variant.
# ShaderPipeline uses the same active-only policy on OpenGL.
METAL_TEX_GRAPH_OPS_MSL = (
    _MSL_HEADER + build_metal_pointwise_grading_msl(metal_graph_base_grade_source())
    + _TEX_GRAPH_BODY_MSL
)
METAL_TEX_GRAPH_CHROMOGEN_MSL = (
    "#define tg_corrector tg_corrector_chromogen\n"
    + _MSL_HEADER + build_metal_pointwise_grading_msl(metal_managed_grading_source())
    + _TEX_GRAPH_BODY_MSL
)

#: 参考实现 4A 的独立源（kernel 名与计划原文一致）
METAL_REFERENCE_4A_MSL = _MSL_HEADER + _REFERENCE_4A_KERNEL_MSL


# ── numpy 参数布局（与 MSL struct 逐字段对齐；4 字节紧排） ────────────────

TEX_EXTRA_PARAMS_DTYPE = np.dtype([
    ("has_hsl", "<u4"),
    ("has_lut3d", "<u4"),
    ("has_mixer", "<u4"),
    ("has_warper", "<u4"),
    ("has_soft_clip", "<u4"),
    ("has_curves_soft_clip", "<u4"),
    ("blend_mode", "<i4"),
    ("lut_intensity", "<f4"),
    ("warper_space", "<i4"),
    ("sc_high", "<f4"),
    ("sc_white_point", "<f4"),
    ("sc_shoulder_strength", "<f4"),
    ("sc_low", "<f4"),
    ("sc_black_point", "<f4"),
    ("sc_toe_strength", "<f4"),
    ("sc_exposure", "<f4"),
    ("sc_contrast", "<f4"),
    ("cs_low_clip_x", "<f4"),
    ("cs_low_clip_y", "<f4"),
    ("cs_low_clip_z", "<f4"),
    ("cs_low_soft_x", "<f4"),
    ("cs_low_soft_y", "<f4"),
    ("cs_low_soft_z", "<f4"),
    ("cs_high_clip_x", "<f4"),
    ("cs_high_clip_y", "<f4"),
    ("cs_high_clip_z", "<f4"),
    ("cs_high_soft_x", "<f4"),
    ("cs_high_soft_y", "<f4"),
    ("cs_high_soft_z", "<f4"),
    ("mixer", "<f4", (24,)),
    ("has_primary_calibration", "<u4"),
    ("mid_detail", "<f4"),
    ("mid_detail_calibrated", "<u4"),
    ("mist_spread", "<f4"),
    ("mist_amount", "<f4"),
    ("rec709_calibration_encoded", "<u4"),
    ("mist_calibrated", "<u4"),
])

TEX_QUALIFIER_PARAMS_DTYPE = np.dtype([
    ("h_enabled", "<u4"),
    ("h_center", "<f4"),
    ("h_width", "<f4"),
    ("h_soft", "<f4"),
    ("s_enabled", "<u4"),
    ("s_low", "<f4"),
    ("s_high", "<f4"),
    ("s_s_low", "<f4"),
    ("s_s_high", "<f4"),
    ("l_enabled", "<u4"),
    ("l_low", "<f4"),
    ("l_high", "<f4"),
    ("l_s_low", "<f4"),
    ("l_s_high", "<f4"),
    ("invert", "<u4"),
])

#: mixer 8 通道的 struct 平铺顺序（msl_apply_mixer 形参序）
_MIXER_CHANNELS: Tuple[str, ...] = (
    "red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta")


def _as_float(value: Any, default: float) -> float:
    """Mapping 取标量；None/畸形回退默认（0.0 是合法值，不能用 or）。"""
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def make_tex_extra_params(
    values: Mapping[str, Any],
    *,
    has_hsl: bool = False,
    has_lut3d: bool = False,
    has_mixer: bool = False,
    has_warper: bool = False,
    has_soft_clip: bool = False,
    has_curves_soft_clip: bool = False,
    has_primary_calibration: bool = False,
) -> np.ndarray:
    """从 :func:`composition_uniform_values` 的提取结果打包扩展参数。

    ``values`` 键名以 composition manifest 为准（``u_lut_intensity``、
    ``u_sc_*``、``u_cs_*``、``u_<channel>_p``、``u_blend_mode``）。混合
    模式索引由 values 携带（composition_uniform_values 已按
    ``Layer.BLEND_MODES`` 归一，未知回退 Normal=0）。
    """
    params = np.zeros(1, dtype=TEX_EXTRA_PARAMS_DTYPE)
    params["has_hsl"] = 1 if has_hsl else 0
    params["has_lut3d"] = 1 if has_lut3d else 0
    params["has_mixer"] = 1 if has_mixer else 0
    params["has_warper"] = 1 if has_warper else 0
    params["has_soft_clip"] = 1 if has_soft_clip else 0
    params["has_primary_calibration"] = int(has_primary_calibration)
    params["mid_detail"] = _as_float(values.get("u_mid_detail"), 0.0)
    params["mid_detail_calibrated"] = bool(values.get("u_mid_detail_calibrated"))
    if has_curves_soft_clip:
        params["has_curves_soft_clip"] = 1 + int(values.get("u_cs_profile", 0))
    params["blend_mode"] = int(_as_float(values.get("u_blend_mode"), 0.0))
    params["lut_intensity"] = _as_float(values.get("u_lut_intensity"), 1.0)
    params["warper_space"] = int(_as_float(values.get("u_warper_space"), 0.0))
    params["mist_spread"] = _as_float(values.get("u_mist_spread"), 0.0)
    params["mist_amount"] = _as_float(values.get("u_mist_amount"), 0.0)
    params["rec709_calibration_encoded"] = bool(values.get("u_rec709_calibration_encoded"))
    params["mist_calibrated"] = bool(values.get("u_mist_calibrated"))
    for field in _SC_DEFAULTS:
        params[field] = _as_float(values.get(f"u_{field}"),
                                  _SC_DEFAULTS[field])
    # curves_soft_clip：composition_uniform_values 产出 u_cs_<band> 三元组
    # （非逐分量键）——按分量展开到 struct 平铺字段
    for band, default in (("low_clip", (0.0, 0.0, 0.0)),
                          ("low_soft", (0.0, 0.0, 0.0)),
                          ("high_clip", (1.0, 1.0, 1.0)),
                          ("high_soft", (0.0, 0.0, 0.0))):
        value = values.get(f"u_cs_{band}", default)
        triple = tuple(value)[:3] if isinstance(
            value, (list, tuple)) else default
        for axis, component in zip("xyz", triple):
            params[f"cs_{band}_{axis}"] = float(component)
    mixer = np.asarray(params["mixer"])
    flat: list[float] = []
    for channel in _MIXER_CHANNELS:
        value = values.get(f"u_{channel}_p", (0.0, 0.0, 0.0))
        triple = tuple(value)[:3] if isinstance(
            value, (list, tuple)) else (0.0, 0.0, 0.0)
        flat.extend(float(v) for v in triple)
    mixer[0, :] = flat
    scalars = [float(v) for v in flat]
    scalars += [float(params[name][0]) for name in params.dtype.names[7:29]]
    scalars += [float(params[name][0]) for name in ("mist_spread", "mist_amount")]
    scalars.append(float(params["mid_detail"][0]))
    if not all(np.isfinite(v) for v in scalars):
        raise ValueError("tg_corrector: 扩展参数含非有限值（fail-closed）")
    return params


#: soft_clip / curves_soft_clip 的中性默认（与 composition_uniform_values
#: 的取值默认一致——manifest 缺键时按中性打包）
_SC_DEFAULTS: Mapping[str, float] = {
    "sc_high": 1.0, "sc_white_point": 1.1, "sc_shoulder_strength": 1.0,
    "sc_low": 0.0, "sc_black_point": -0.1, "sc_toe_strength": 1.0,
    "sc_exposure": 0.0, "sc_contrast": 0.0,
}
#: curves_soft_clip 默认：low_clip/low_soft/high_soft = 0，high_clip = 1
#: （恒等映射；与 composition_uniform_values 的 _CS_DEFAULTS 同值）
_CS_ZERO_FIELDS: Tuple[str, ...] = tuple(
    f"cs_{band}_{axis}" for band in ("low_clip", "low_soft", "high_soft")
    for axis in "xyz")
_CS_DEFAULTS: Mapping[str, float] = {
    **{name: 0.0 for name in _CS_ZERO_FIELDS},
    **{f"cs_high_clip_{axis}": 1.0 for axis in "xyz"},
}


def make_tex_qualifier_params(qualifier: Mapping[str, Any]) -> np.ndarray:
    """``qualifier_params`` → ``TexQualifierParams``（keyer.glsl 同域）。

    hue_center 为 CPU 权威域（与 qualifier_cpu/标准 HSL 色相同轴）；
    keyer.glsl 的 hue 轴经 ``fract(h+0.5)`` 预移，center 在此换算到
    shader 域（M06 对拍口径）。
    """
    center = _as_float(qualifier.get("hue_center"), 0.5) % 1.0
    params = np.zeros(1, dtype=TEX_QUALIFIER_PARAMS_DTYPE)
    params["h_enabled"] = 1 if qualifier.get("hue_enabled", True) else 0
    params["h_center"] = (center + 0.5) % 1.0
    params["h_width"] = _as_float(qualifier.get("hue_width"), 1.0)
    params["h_soft"] = _as_float(qualifier.get("hue_soft"), 0.1)
    params["s_enabled"] = 1 if qualifier.get("sat_enabled", True) else 0
    params["s_low"] = _as_float(qualifier.get("sat_low"), 0.0)
    params["s_high"] = _as_float(qualifier.get("sat_high"), 1.0)
    params["s_s_low"] = _as_float(qualifier.get("sat_soft_low"), 0.0)
    params["s_s_high"] = _as_float(qualifier.get("sat_soft_high"), 0.0)
    params["l_enabled"] = 1 if qualifier.get("lum_enabled", True) else 0
    params["l_low"] = _as_float(qualifier.get("lum_low"), 0.0)
    params["l_high"] = _as_float(qualifier.get("lum_high"), 1.0)
    params["l_s_low"] = _as_float(qualifier.get("lum_soft_low"), 0.0)
    params["l_s_high"] = _as_float(qualifier.get("lum_soft_high"), 0.0)
    params["invert"] = 1 if qualifier.get("invert", False) else 0
    return params
