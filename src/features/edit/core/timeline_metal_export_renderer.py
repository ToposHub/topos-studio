"""Metal compute path for ordinary timeline export frames.

The input frame is uploaded once, then source conversion/primary grade,
scaling, and (for H.264) BT.709 YUV420 or (for Topos) YUV422P10 conversion run
on Metal. Only the final encoder planes are read back. Unsupported timeline
constructs are filtered by
``TimelineExporter`` and continue through its established CPU compositor.
"""

from __future__ import annotations

import logging
import math
import threading
from functools import wraps
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import numpy as np

from src.shared.gpu.metal import MetalComputeContext, MetalKernel
from src.shared.export.gpu_encode_surface import GpuEncodeSurface
from src.shared.export.gpu_trail import ExportGpuTrail

logger = logging.getLogger(__name__)


def _serialize_renderer_call(method: Callable[..., Any]) -> Callable[..., Any]:
    """Protect a renderer entrypoint's reusable Metal resources as one frame."""
    @wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._render_lock:
            return method(self, *args, **kwargs)
    return wrapped


_PARAM_DTYPE = np.dtype([
    ("source_output_dims", "<u4", (4,)),  # source w/h, output w/h
    ("chroma_dims", "<u4", (4,)),        # U w/h, V w/h
    ("source_flags", "<u4", (4,)),       # bit depth, chroma div x/y, is YUV
    ("control", "<f4", (4,)),            # opacity, grade, YUV output, full range
    ("lift", "<f4", (4,)),
    ("gamma", "<f4", (4,)),
    ("gain", "<f4", (4,)),
    ("grade_a", "<f4", (4,)),            # exposure, temp, tint, contrast
    ("grade_b", "<f4", (4,)),            # shadow, highlights, sat, vibrance
    ("grade_c", "<f4", (4,)),            # hue shift, padding
    ("yuv", "<f4", (4,)),                # Kr, Kb, padding
    ("effect", "<f4", (4,)),             # kind, radius x/y, mix or amount
    ("effect_sigma", "<f4", (4,)),        # sigma x/y, Glow tint r/g
    ("effect_extra", "<f4", (4,)),        # Glow threshold/intensity/tint b; Sharpen threshold/channels
    ("mip_dims", "<u4", (8, 4)),           # Defocus mip width/height/byte offset
])

_MIP_PARAMS_DTYPE = np.dtype([
    ("dims", "<u4", (4,)),       # source w/h, destination w/h
    ("offsets", "<u4", (4,)),    # source byte offset, destination byte offset, source-is-atlas
])


_TIMELINE_METAL_MSL = r"""
#include <metal_stdlib>
using namespace metal;

struct TimelineParams {
    uint4 source_output_dims;
    uint4 chroma_dims;
    uint4 source_flags;
    float4 control;
    float4 lift;
    float4 gamma;
    float4 gain;
    float4 grade_a;
    float4 grade_b;
    float4 grade_c;
    float4 yuv;
    float4 effect;
    float4 effect_sigma;
    float4 effect_extra;
    uint4 mip_dims[8];
};

struct DefocusMipmapParams {
    uint4 dims;
    uint4 offsets;
};

float3 rgb2hsv(float3 c) {
    float4 K = float4(0.0f, -1.0f / 3.0f, 2.0f / 3.0f, -1.0f);
    float4 p = mix(float4(c.b, c.g, K.w, K.z),
                   float4(c.g, c.b, K.x, K.y), step(c.b, c.g));
    float4 q = mix(float4(p.x, p.y, p.w, c.r),
                   float4(c.r, p.y, p.z, p.x), step(p.x, c.r));
    float d = q.x - min(q.w, q.y);
    return float3(abs(q.z + (q.w - q.y) / (6.0f * d + 1.0e-10f)),
                  d / (q.x + 1.0e-10f), q.x);
}

float3 hsv2rgb(float3 c) {
    float4 K = float4(1.0f, 2.0f / 3.0f, 1.0f / 3.0f, 3.0f);
    float3 p = abs(fract(c.xxx + K.xyz) * 6.0f - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0f, 1.0f), c.y);
}

float3 apply_primary_grade(float3 color, constant TimelineParams& p) {
    float exposure = p.grade_a.x;
    float exp_stops = (exposure / 100.0f) * (exposure / 100.0f) * 6.0f;
    if (exposure < 0.0f) exp_stops = -exp_stops;
    color *= pow(2.0f, exp_stops);
    color *= float3(1.0f + p.grade_a.y / 200.0f,
                    1.0f - p.grade_a.z / 200.0f,
                    1.0f - p.grade_a.y / 200.0f);
    if (p.grade_a.w != 0.0f) {
        color = sign(color) * pow(max(abs(color), float3(1.0e-10f)) / 0.18f,
                                  float3(1.0f + p.grade_a.w / 100.0f)) * 0.18f;
    }
    float3 lift_adjustment = (p.lift.xyz - 1.0f) * 0.2f;
    color = color * p.gain.xyz * (1.0f - lift_adjustment) + lift_adjustment;
    float3 gamma_power = exp2(-log2(max(p.gamma.xyz, float3(0.01f))));
    for (uint i = 0; i < 3; ++i) {
        if (color[i] > 0.0f && color[i] <= 1.0f) {
            color[i] = pow(color[i], gamma_power[i]);
        } else if (color[i] > 1.0f) {
            color[i] = gamma_power[i] * (color[i] - 1.0f) + 1.0f;
        } else {
            color[i] *= gamma_power[i];
        }
    }
    // RENDER-02/R31：暗部保护——与 Color 权威 MSL/CPU 逐条一致
    //（gamma 之后、shadows 之前；luma<0 的当前像素做 <-0.5 软压缩与
    // 通道平衡）。此前导出复制体缺失该级（审计实测 u16 差 45936）。
    {
        float luma_post_grading = dot(color, float3(0.299f, 0.587f, 0.114f));
        if (luma_post_grading < 0.0f) {
            for (uint i = 0; i < 3; ++i) {
                float x = color[i];
                if (x < -0.5f) {
                    x = -0.5f - (fabs(x + 0.5f) * 0.1f);
                }
                color[i] = x;
            }
            float min_channel = min(min(color.r, color.g), color.b);
            float max_channel = max(max(color.r, color.g), color.b);
            if (max_channel - min_channel > 0.15f && min_channel < 0.0f) {
                float balance_factor = clamp(fabs(min_channel) * 0.25f, 0.0f, 0.15f);
                color = mix(color, float3(luma_post_grading), balance_factor);
            }
        }
    }
    if (p.grade_b.x != 0.0f) {
        float shadow_factor = 1.0f + p.grade_b.x / 100.0f;
        if (shadow_factor > 0.0f) {
            float3 bounded = clamp(color, 0.0f, 1.0f);
            color = pow(bounded, float3(1.0f / shadow_factor)) + (color - bounded);
        }
    }
    if (p.grade_b.y != 0.0f) {
        float maximum = max(max(color.r, color.g), color.b);
        if (maximum > 0.1f) {
            float mapped = maximum;
            if (p.grade_b.y < 0.0f) {
                float k = abs(p.grade_b.y / 100.0f) * 2.0f;
                mapped = 0.1f + (maximum - 0.1f) / (1.0f + k * (maximum - 0.1f));
            } else {
                mapped = 0.1f + (maximum - 0.1f) * (1.0f + (p.grade_b.y / 100.0f) * 0.5f);
            }
            color *= mapped / maximum;
        }
    }
    float gray = dot(color, float3(0.299f, 0.587f, 0.114f));
    if (p.grade_b.w != 0.0f) {
        float extent = max(color.r, max(color.g, color.b)) - gray;
        float boost = (p.grade_b.w / 100.0f) * (1.0f - clamp(extent, 0.0f, 1.0f));
        color = mix(color, float3(gray), -boost);
    }
    if (p.grade_c.x != 0.0f) {
        float3 hsv = rgb2hsv(color);
        hsv.x = fract(hsv.x + p.grade_c.x / 360.0f);
        color = hsv2rgb(hsv);
    }
    // R31：saturation 用 post-hue 灰度（与 CPU/GL 导出同语义）
    gray = dot(color, float3(0.299f, 0.587f, 0.114f));
    if (p.grade_b.z != 0.0f) {
        color = mix(float3(gray), color, 1.0f + p.grade_b.z / 100.0f);
    }
    return color;
}

float read_chroma(device const ushort* plane, uint width, uint height, float2 pos,
                  uint bit_depth) {
    int2 p0 = int2(floor(pos));
    float2 f = pos - float2(p0);
    int2 hi = int2(int(width) - 1, int(height) - 1);
    int2 a = clamp(p0, int2(0), hi);
    int2 b = clamp(p0 + int2(1), int2(0), hi);
    float c00 = float(plane[uint(a.y) * width + uint(a.x)]);
    float c10 = float(plane[uint(a.y) * width + uint(b.x)]);
    float c01 = float(plane[uint(b.y) * width + uint(a.x)]);
    float c11 = float(plane[uint(b.y) * width + uint(b.x)]);
    float value = mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y);
    // OpenGL's established conversion rounds 8-bit chroma half-up and
    // 10/12/16-bit chroma to nearest-even before applying the matrix.
    return bit_depth > 8u ? rint(value) : floor(value + 0.5f);
}

uchar to_u8(float v) {
    return uchar(clamp(rint(v), 0.0f, 255.0f));
}

ushort to_u16(float v) {
    return ushort(clamp(rint(v), 0.0f, 65535.0f));
}

float3 rgb_code_to_yuv422p10(float3 rgb_code, float kr, float kb,
                             bool full_range) {
    float3 rgb = clamp(rgb_code / 255.0f, 0.0f, 1.0f);
    float kg = 1.0f - kr - kb;
    float y = dot(rgb, float3(kr, kg, kb));
    float cb = (rgb.b - y) / (2.0f * (1.0f - kb));
    float cr = (rgb.r - y) / (2.0f * (1.0f - kr));
    if (full_range) {
        return float3(y * 1023.0f, (cb + 0.5f) * 1023.0f,
                      (cr + 0.5f) * 1023.0f);
    }
    return float3(64.0f + y * 876.0f,
                  64.0f + (cb + 0.5f) * 896.0f,
                  64.0f + (cr + 0.5f) * 896.0f);
}

int reflect101_coord(int position, int limit) {
    if (limit <= 1) return 0;
    while (position < 0 || position >= limit) {
        position = position < 0 ? -position : 2 * limit - position - 2;
    }
    return position;
}

int effect_edge_coord(int position, int limit, constant TimelineParams& p) {
    uint kind = uint(p.effect.x + 0.5f);
    if (kind == 6u || kind == 7u) return clamp(position, 0, limit - 1);
    if ((kind == 8u || kind == 10u || kind == 11u) && limit > 0) {
        int policy = int((kind == 8u ? p.effect_extra.x : p.effect_sigma.z)
                         + 0.5f);
        if (policy == 0) return clamp(position, 0, limit - 1);
        if (policy == 1) {
            int wrapped = position % limit;
            return wrapped < 0 ? wrapped + limit : wrapped;
        }
        if (policy == 2) {
            int period = 2 * limit;
            int mirrored = position % period;
            if (mirrored < 0) mirrored += period;
            return mirrored < limit ? mirrored : period - 1 - mirrored;
        }
    }
    if ((kind == 1u || kind == 2u || kind == 5u) && limit > 0) {
        int policy = int(p.effect_sigma.z + 0.5f);
        if (policy == 0) return clamp(position, 0, limit - 1);
        if (policy == 1) {
            int wrapped = position % limit;
            return wrapped < 0 ? wrapped + limit : wrapped;
        }
        if (policy == 2) {
            int period = 2 * limit;
            int mirrored = position % period;
            if (mirrored < 0) mirrored += period;
            return mirrored < limit ? mirrored : period - 1 - mirrored;
        }
    }
    return reflect101_coord(position, limit);
}

float3 prepare_source_pixel_unfiltered(
    uint2 gid,
    device const uchar* bgr,
    device const ushort* y_plane,
    device const ushort* u_plane,
    device const ushort* v_plane,
    constant TimelineParams& p) {
    uint width = p.source_output_dims.x;
    uint index = gid.y * width + gid.x;
    float3 color;
    if (p.source_flags.w == 0u) {
        uint i = index * 3u;
        color = float3(float(bgr[i + 2u]), float(bgr[i + 1u]), float(bgr[i])) / 255.0f;
    } else {
        uint bit_depth = p.source_flags.x;
        float max_code = float((1u << bit_depth) - 1u);
        float y_code = float(y_plane[index]);
        float2 chroma_pos = (float2(gid) + 0.5f)
            / float2(p.source_flags.y, p.source_flags.z) - 0.5f;
        float cb_code = read_chroma(u_plane, p.chroma_dims.x, p.chroma_dims.y,
                                    chroma_pos, bit_depth);
        float cr_code = read_chroma(v_plane, p.chroma_dims.z, p.chroma_dims.w,
                                    chroma_pos, bit_depth);
        float y, cb, cr;
        if (p.control.w > 0.5f) {
            y = y_code / max_code;
            cb = (cb_code - max_code * 0.5f) / max_code;
            cr = (cr_code - max_code * 0.5f) / max_code;
        } else {
            float scale_8 = float(1u << (bit_depth - 8u));
            y = (y_code - 16.0f * scale_8) / (219.0f * scale_8);
            cb = (cb_code - 128.0f * scale_8) / (224.0f * scale_8);
            cr = (cr_code - 128.0f * scale_8) / (224.0f * scale_8);
        }
        float kr = p.yuv.x;
        float kb = p.yuv.y;
        float kg = 1.0f - kr - kb;
        color = clamp(float3(y + 2.0f * (1.0f - kr) * cr,
                             y - 2.0f * kb * (1.0f - kb) / kg * cb
                               - 2.0f * kr * (1.0f - kr) / kg * cr,
                             y + 2.0f * (1.0f - kb) * cb), 0.0f, 1.0f);
    }
    if (p.control.y > 0.5f) color = apply_primary_grade(color, p);
    return float3(to_u8(clamp(color.r, 0.0f, 1.0f) * 255.0f),
                  to_u8(clamp(color.g, 0.0f, 1.0f) * 255.0f),
                  to_u8(clamp(color.b, 0.0f, 1.0f) * 255.0f));
}

float3 effect_filter_input(float3 color, constant TimelineParams& p) {
    if (uint(p.effect.x + 0.5f) == 4u) {
        float threshold = clamp(p.effect_extra.x, 0.0f, 1.0f) * 255.0f;
        return select(float3(0.0f), color, color >= threshold);
    }
    return color;
}

float3 fold_morphology_pixel(
    float3 aggregate, float3 sample, constant TimelineParams& p) {
    return p.effect_extra.x < 0.5f
        ? min(aggregate, sample)
        : max(aggregate, sample);
}

float3 finish_effect_pixel(
    float3 center, float3 filtered, constant TimelineParams& p) {
    uint kind = uint(p.effect.x + 0.5f);
    if (kind == 5u) return filtered;
    if (kind == 3u) {
        float3 diff = center - filtered;
        float diff_luma = dot(diff, float3(0.2126f, 0.7152f, 0.0722f));
        float threshold = max(p.effect_extra.z, 0.0f) * 255.0f;
        if (threshold > 0.0f) {
            diff *= smoothstep(0.0f, threshold, abs(diff_luma));
        }
        uint channels = uint(p.effect_extra.w + 0.5f);
        float3 sharpened;
        if (channels == 2u) {
            sharpened = center + diff_luma * p.effect.w
                * float3(0.2126f, 0.7152f, 0.0722f);
        } else {
            sharpened = center + diff * p.effect.w;
        }
        return float3(to_u8(sharpened.r), to_u8(sharpened.g),
                      to_u8(sharpened.b));
    }
    if (kind == 4u) {
        float3 source = center / 255.0f;
        float3 tint = float3(p.effect_sigma.z, p.effect_sigma.w,
                             p.effect_extra.z);
        float3 bloom = clamp(filtered * p.effect_extra.y * tint,
                             0.0f, 255.0f)
            / 255.0f;
        float3 screen = 1.0f - (1.0f - source) * (1.0f - bloom);
        float3 mixed = mix(source, screen, clamp(p.effect.w, 0.0f, 1.0f));
        return float3(to_u8(mixed.r * 255.0f), to_u8(mixed.g * 255.0f),
                      to_u8(mixed.b * 255.0f));
    }
    float3 blended = mix(center, filtered, clamp(p.effect.w, 0.0f, 1.0f));
    return floor(clamp(blended, 0.0f, 255.0f));
}

float3 finish_edge_detect(
    float gx, float gy, float laplacian, constant TimelineParams& p) {
    uint direction = uint(p.effect_sigma.z + 0.5f);
    uint output_mode = uint(p.effect_sigma.w + 0.5f);
    uint method = uint(p.effect_extra.z + 0.5f);
    float magnitude;
    if (method == 3u) magnitude = abs(laplacian);
    else if (direction == 1u) magnitude = abs(gx);
    else if (direction == 2u) magnitude = abs(gy);
    else magnitude = length(float2(gx, gy));
    magnitude *= 0.25f;

    float output_value;
    if (method == 3u && output_mode == 2u) {
        output_value = 0.5f;
    } else if (output_mode == 0u) {
        if (method == 1u) {
            output_value = magnitude < p.effect_sigma.x
                ? 0.0f
                : (magnitude < p.effect_sigma.y ? 0.5f : 1.0f);
        } else {
            // Match the CPU reference around exact float32 threshold ties
            // despite a possible one-ULP difference from Metal's fused
            // arithmetic.
            output_value = magnitude + 1.0e-6f >= p.effect_sigma.x
                ? 1.0f : 0.0f;
        }
        output_value *= p.effect.w;
    } else if (output_mode == 1u) {
        output_value = magnitude * p.effect.w;
    } else {
        float angle = (gx * gx + gy * gy) > 1.0e-12f
            ? atan2(gy, gx) : 0.0f;
        output_value = (angle + 3.14159f) / (2.0f * 3.14159f);
    }
    if (p.effect_extra.x > 0.5f) output_value = 1.0f - output_value;
    float code = clamp(output_value, 0.0f, 1.0f) * 255.0f;
    return float3(code);
}

float3 edge_detect_from_luma(
    float tl, float tm, float tr,
    float ml, float center, float mr,
    float bl, float bm, float br,
    constant TimelineParams& p) {
    uint method = uint(p.effect_extra.z + 0.5f);
    float gx, gy;
    if (method == 2u) {
        gx = -tl + tr - ml + mr - bl + br;
        gy = -tl - tm - tr + bl + bm + br;
    } else {
        gx = -tl + tr - 2.0f * ml + 2.0f * mr - bl + br;
        gy = -tl - 2.0f * tm - tr + bl + 2.0f * bm + br;
    }
    float laplacian = tm + bm + ml + mr - 4.0f * center;
    return finish_edge_detect(gx, gy, laplacian, p);
}

float edge_detect_luma(float3 color) {
    return dot(color, float3(0.2126f, 0.7152f, 0.0722f)) / 255.0f;
}

float edge_detect_channel(float3 color, constant TimelineParams& p) {
    uint channel = uint(p.effect_extra.y + 0.5f);
    if (channel == 1u) return 1.0f;
    if (channel == 2u) return color.r / 255.0f;
    if (channel == 3u) return color.g / 255.0f;
    if (channel == 4u) return color.b / 255.0f;
    return edge_detect_luma(color);
}

float sample_source_edge_luma(
    int2 position,
    device const uchar* bgr,
    device const ushort* y_plane,
    device const ushort* u_plane,
    device const ushort* v_plane,
    constant TimelineParams& p) {
    int2 coord = int2(
        effect_edge_coord(position.x, int(p.source_output_dims.x), p),
        effect_edge_coord(position.y, int(p.source_output_dims.y), p));
    return edge_detect_channel(prepare_source_pixel_unfiltered(
        uint2(coord), bgr, y_plane, u_plane, v_plane, p), p);
}

float edge_sobel_smoothing_weight(int offset) {
    return float(3 - abs(offset));
}

float3 edge_detect_source_pixel(
    int2 center_position,
    device const uchar* bgr,
    device const ushort* y_plane,
    device const ushort* u_plane,
    device const ushort* v_plane,
    constant TimelineParams& p) {
    uint method = uint(p.effect_extra.z + 0.5f);
    int kernel_size = int(p.effect.y + 0.5f);
    float gx = 0.0f, gy = 0.0f;
    if (kernel_size == 5 && (method == 0u || method == 1u)) {
        for (int dy = -2; dy <= 2; ++dy) {
            for (int dx = -2; dx <= 2; ++dx) {
                float value = sample_source_edge_luma(
                    center_position + int2(dx, dy), bgr,
                    y_plane, u_plane, v_plane, p);
                float coefficient = edge_sobel_smoothing_weight(dy)
                    * float(dx);
                gx += value * coefficient;
                // Match the existing CPU 5x5 Sobel reference coefficient
                // ordering exactly; its gy matrix currently mirrors gx.
                gy += value * coefficient;
            }
        }
        gx /= 12.0f;
        gy /= 12.0f;
    }
    float tl = sample_source_edge_luma(center_position + int2(-1, -1),
        bgr, y_plane, u_plane, v_plane, p);
    float tm = sample_source_edge_luma(center_position + int2(0, -1),
        bgr, y_plane, u_plane, v_plane, p);
    float tr = sample_source_edge_luma(center_position + int2(1, -1),
        bgr, y_plane, u_plane, v_plane, p);
    float ml = sample_source_edge_luma(center_position + int2(-1, 0),
        bgr, y_plane, u_plane, v_plane, p);
    float center = sample_source_edge_luma(center_position,
        bgr, y_plane, u_plane, v_plane, p);
    float mr = sample_source_edge_luma(center_position + int2(1, 0),
        bgr, y_plane, u_plane, v_plane, p);
    float bl = sample_source_edge_luma(center_position + int2(-1, 1),
        bgr, y_plane, u_plane, v_plane, p);
    float bm = sample_source_edge_luma(center_position + int2(0, 1),
        bgr, y_plane, u_plane, v_plane, p);
    float br = sample_source_edge_luma(center_position + int2(1, 1),
        bgr, y_plane, u_plane, v_plane, p);
    if (kernel_size != 5 || (method != 0u && method != 1u)) {
        bool use_prewitt = method == 2u && kernel_size != 5;
        gx = use_prewitt
            ? -tl + tr - ml + mr - bl + br
            : -tl + tr - 2.0f * ml + 2.0f * mr - bl + br;
        gy = use_prewitt
            ? -tl - tm - tr + bl + bm + br
            : -tl - 2.0f * tm - tr + bl + 2.0f * bm + br;
    }
    float laplacian = (kernel_size == 5 && method == 3u)
        ? tm + bm + ml + mr + 0.5f * (tl + tr + bl + br) - 8.0f * center
        : tm + bm + ml + mr - 4.0f * center;
    return finish_edge_detect(gx, gy, laplacian, p);
}

float effect_axis_weight(uint kind, int offset, int half_size, float sigma) {
    if (kind == 1u) return 1.0f;
    if (kind == 5u) return offset == 0 ? 1.0f : 0.0f;
    if (kind == 2u) {
        // OpenCV uses fixed, bit-exact kernels for radii 1..4 when
        // GaussianBlur receives sigma=0 (the CPU timeline export contract).
        // Those kernels are not exact samples of one Gaussian sigma, so use
        // their coefficients directly instead of approximating them with exp.
        int distance = abs(offset);
        if (half_size == 1) return distance == 0 ? 0.5f : 0.25f;
        if (half_size == 2) {
            if (distance == 0) return 0.375f;
            if (distance == 1) return 0.25f;
            return 0.0625f;
        }
        if (half_size == 3) {
            if (distance == 0) return 0.28125f;
            if (distance == 1) return 0.21875f;
            if (distance == 2) return 0.109375f;
            return 0.03125f;
        }
        if (half_size == 4) {
            if (distance == 0) return 0.234375f;
            if (distance == 1) return 0.19921875f;
            if (distance == 2) return 0.1171875f;
            if (distance == 3) return 0.05078125f;
            return 0.015625f;
        }
    }
    return exp(-float(offset * offset) / (2.0f * sigma * sigma));
}

float3 load_packed_bgr_pixel(uint2 position, device const uchar* bgr,
                             uint width) {
    uint index = (position.y * width + position.x) * 3u;
    return float3(float(bgr[index + 2u]), float(bgr[index + 1u]),
                  float(bgr[index]));
}

float3 sample_packed_linear_pixel(float2 position, device const uchar* bgr,
                                  constant TimelineParams& p) {
    int2 base = int2(floor(position));
    float2 fraction = position - float2(base);
    int width = int(p.source_output_dims.x);
    int height = int(p.source_output_dims.y);
    int2 a = int2(effect_edge_coord(base.x, width, p),
                  effect_edge_coord(base.y, height, p));
    int2 b = int2(effect_edge_coord(base.x + 1, width, p),
                  effect_edge_coord(base.y + 1, height, p));
    float3 c00 = load_packed_bgr_pixel(uint2(a), bgr, uint(width));
    float3 c10 = load_packed_bgr_pixel(uint2(b.x, a.y), bgr, uint(width));
    float3 c01 = load_packed_bgr_pixel(uint2(a.x, b.y), bgr, uint(width));
    float3 c11 = load_packed_bgr_pixel(uint2(b), bgr, uint(width));
    return mix(mix(c00, c10, fraction.x),
               mix(c01, c11, fraction.x), fraction.y);
}

float3 apply_packed_motion_blur(uint2 gid, float3 center,
                                device const uchar* bgr,
                                constant TimelineParams& p) {
    float radius = p.effect.y;
    float shutter = p.effect.z;
    if (radius < 0.5f || shutter <= 0.0f) return center;
    int count = clamp(int(p.effect_sigma.w + 0.5f), 4, 32);
    // Direction is precomputed once while building frame params; computing
    // trigonometry here would repeat it for every output pixel.
    float2 direction = p.effect_sigma.xy;
    float2 pixel_scale = float2(p.source_output_dims.xy)
        / float(max(p.source_output_dims.x, p.source_output_dims.y));
    float3 sum = float3(0.0f);
    for (int i = 0; i < 32; ++i) {
        if (i >= count) break;
        float t = (float(i) / float(count - 1)) * 2.0f - 1.0f;
        float distance = t * radius * shutter;
        float2 position = float2(gid) + direction * pixel_scale * distance;
        sum += sample_packed_linear_pixel(position, bgr, p);
    }
    float3 blurred = sum / float(count);
    float3 mask = float3(p.effect_extra.x, p.effect_extra.y,
                         p.effect_extra.z);
    return mix(center, blurred, mask * clamp(p.effect.w, 0.0f, 1.0f));
}

float3 apply_packed_bokeh_blur(uint2 gid, float3 center,
                               device const uchar* bgr,
                               device const float* sample_offsets,
                               constant TimelineParams& p) {
    float radius = p.effect.y;
    if (radius < 0.5f) return center;
    int count = clamp(int(p.effect_sigma.w + 0.5f), 8, 48);
    float highlight_gain = p.effect_sigma.y;
    float3 sum = float3(0.0f);
    float weight_sum = 0.0f;
    for (int i = 0; i < 48; ++i) {
        if (i >= count) break;
        float2 offset = float2(sample_offsets[i * 2],
                               sample_offsets[i * 2 + 1]);
        float3 sample = sample_packed_linear_pixel(
            float2(gid) + offset, bgr, p);
        float luma = dot(sample, float3(0.2126f, 0.7152f, 0.0722f));
        float weight = highlight_gain > 0.001f
            ? pow(max(luma / 255.0f, 0.0f), highlight_gain) + 1.0f
            : 1.0f;
        sum += sample * weight;
        weight_sum += weight;
    }
    float3 blurred = sum / max(weight_sum, 0.0001f);
    float3 mask = float3(p.effect_extra.x, p.effect_extra.y,
                         p.effect_extra.z);
    return mix(center, blurred, mask * clamp(p.effect.w, 0.0f, 1.0f));
}

kernel void timeline_build_bokeh_offsets(
    device float* offsets [[buffer(0)]],
    constant TimelineParams& p [[buffer(1)]],
    uint index [[thread_position_in_grid]]) {
    if (index >= 48u) return;
    int count = clamp(int(p.effect_sigma.w + 0.5f), 8, 48);
    if (int(index) >= count) {
        offsets[index * 2u] = 0.0f;
        offsets[index * 2u + 1u] = 0.0f;
        return;
    }
    float radius = p.effect.y;
    float anamorphic = p.effect_sigma.x;
    float blades = p.effect.z;
    float segment = 6.28318530718f / max(blades, 1.0f);
    float2 aspect_scale = float2(
        1.0f + anamorphic * 1.5f,
        1.0f / (1.0f + anamorphic * 1.5f));
    float2 pixel_scale = 1.0f / float2(p.source_output_dims.xy);
    constexpr float GOLDEN = 2.39996323f;
    float fi = float(index) + 0.5f;
    float sample_radius = sqrt(fi / float(count));
    float theta = fi * GOLDEN;
    float2 disc = float2(cos(theta), sin(theta)) * sample_radius;
    if (blades > 0.5f) {
        float angle = fmod(atan2(disc.y, disc.x), segment);
        if (angle < 0.0f) angle += segment;
        float polygon_radius = cos(segment * 0.5f)
            / max(cos(angle - segment * 0.5f), 0.0001f);
        disc *= clamp(polygon_radius, 0.0f, 1.0f);
    }
    float2 offset = disc * aspect_scale * radius * pixel_scale;
    offset *= float2(p.source_output_dims.xy);
    offsets[index * 2u] = offset.x;
    offsets[index * 2u + 1u] = offset.y;
}

float denoise_luma(float3 color) {
    return dot(color, float3(0.2126f, 0.7152f, 0.0722f));
}

float3 apply_packed_denoise_pixel(
    uint2 gid,
    float3 center_code,
    device const uchar* bgr,
    device const float* weights_x,
    device const float* weights_y,
    constant TimelineParams& p) {
    int half_x = int(p.effect.y + 0.5f);
    int half_y = int(p.effect.z + 0.5f);
    float3 center = center_code / 255.0f;
    float center_luma = denoise_luma(center);
    float3 center_chroma = center - center_luma;
    float sigma_range = max(p.effect_sigma.y, 0.1f);
    float range_denominator = 2.0f * sigma_range * sigma_range;
    bool luminance_only = p.effect_extra.x > 0.5f;
    bool chroma_only = p.effect_extra.y > 0.5f;
    float3 sum = float3(0.0f);
    float weight_sum = 0.0f;
    int width = int(p.source_output_dims.x);
    int height = int(p.source_output_dims.y);
    for (int dy = -half_y; dy <= half_y; ++dy) {
        int sy = effect_edge_coord(int(gid.y) + dy, height, p);
        float weight_y = weights_y[dy + 32];
        for (int dx = -half_x; dx <= half_x; ++dx) {
            int sx = effect_edge_coord(int(gid.x) + dx, width, p);
            float3 sample = load_packed_bgr_pixel(
                uint2(uint(sx), uint(sy)), bgr, uint(width)) / 255.0f;
            float range_diff_sq;
            if (luminance_only) {
                float difference = center_luma - denoise_luma(sample);
                range_diff_sq = difference * difference;
            } else if (chroma_only) {
                float3 sample_chroma = sample - denoise_luma(sample);
                float3 difference = center_chroma - sample_chroma;
                range_diff_sq = dot(difference, difference);
            } else {
                float3 difference = center - sample;
                range_diff_sq = dot(difference, difference);
            }
            float weight = weights_x[dx + 32] * weight_y
                * exp(-range_diff_sq / range_denominator);
            sum += sample * weight;
            weight_sum += weight;
        }
    }
    float3 filtered = sum / max(weight_sum, 1.0e-4f);
    float strength = clamp(p.effect.w, 0.0f, 1.0f);
    float3 result = mix(center, filtered, strength);
    if (luminance_only) {
        float result_luma = mix(center_luma, denoise_luma(filtered), strength);
        result = result_luma + center_chroma;
    } else if (chroma_only) {
        float3 filtered_chroma = filtered - denoise_luma(filtered);
        result = center_luma + mix(center_chroma, filtered_chroma, strength);
    }
    return clamp(result, 0.0f, 1.0f) * 255.0f;
}

float3 apply_packed_bilateral_pixel(
    uint2 gid,
    float3 center_code,
    device const uchar* bgr,
    device const float* weights_x,
    device const float* weights_y,
    constant TimelineParams& p) {
    int half_x = int(p.effect.y + 0.5f);
    int half_y = int(p.effect.z + 0.5f);
    float3 center = center_code / 255.0f;
    float sigma_range = max(p.effect_sigma.y, 0.001f);
    float range_denominator = 2.0f * sigma_range * sigma_range;
    float3 sum = float3(0.0f);
    float weight_sum = 0.0f;
    int width = int(p.source_output_dims.x);
    int height = int(p.source_output_dims.y);
    for (int dy = -half_y; dy <= half_y; ++dy) {
        int sy = effect_edge_coord(int(gid.y) + dy, height, p);
        float weight_y = weights_y[dy + 32];
        for (int dx = -half_x; dx <= half_x; ++dx) {
            if (dx * dx + dy * dy > half_x * half_x) continue;
            int sx = effect_edge_coord(int(gid.x) + dx, width, p);
            float3 sample = load_packed_bgr_pixel(
                uint2(uint(sx), uint(sy)), bgr, uint(width)) / 255.0f;
            // OpenCV's 8-bit 3-channel bilateral path indexes the range LUT
            // by the sum of absolute B/G/R differences, then squares that
            // scalar. Keep the timeline Metal path aligned with that CPU
            // export reference rather than using an RGB Euclidean norm.
            float range_distance = abs(center.r - sample.r)
                + abs(center.g - sample.g) + abs(center.b - sample.b);
            float weight = weights_x[dx + 32] * weight_y
                * exp(-(range_distance * range_distance) / range_denominator);
            sum += sample * weight;
            weight_sum += weight;
        }
    }
    float3 filtered = sum / max(weight_sum, 1.0e-4f);
    float3 result = mix(center, filtered, clamp(p.effect.w, 0.0f, 1.0f));
    if (p.effect_extra.x < 0.5f) result.r = center.r;
    if (p.effect_extra.y < 0.5f) result.g = center.g;
    if (p.effect_extra.z < 0.5f) result.b = center.b;
    return clamp(result, 0.0f, 1.0f) * 255.0f;
}

float3 load_defocus_mipmap_pixel(
    int2 coordinate,
    uint level,
    device const uchar* bgr,
    device const uchar* mip_atlas,
    constant TimelineParams& p) {
    uint4 dims = p.mip_dims[level];
    int2 position = clamp(coordinate, int2(0), int2(int(dims.x) - 1, int(dims.y) - 1));
    if (level == 0u) {
        return load_packed_bgr_pixel(
            uint2(position), bgr, p.source_output_dims.x);
    }
    uint index = dims.z + (uint(position.y) * dims.x + uint(position.x)) * 3u;
    return float3(float(mip_atlas[index + 2u]),
                  float(mip_atlas[index + 1u]),
                  float(mip_atlas[index]));
}

float3 sample_defocus_mipmap(
    float2 uv,
    uint level,
    device const uchar* bgr,
    device const uchar* mip_atlas,
    constant TimelineParams& p) {
    uint4 dims = p.mip_dims[level];
    float2 position = uv * float2(dims.xy) - 0.5f;
    int2 origin = int2(floor(position));
    float2 fraction = position - float2(origin);
    float3 c00 = load_defocus_mipmap_pixel(
        origin, level, bgr, mip_atlas, p);
    float3 c10 = load_defocus_mipmap_pixel(
        origin + int2(1, 0), level, bgr, mip_atlas, p);
    float3 c01 = load_defocus_mipmap_pixel(
        origin + int2(0, 1), level, bgr, mip_atlas, p);
    float3 c11 = load_defocus_mipmap_pixel(
        origin + int2(1, 1), level, bgr, mip_atlas, p);
    return mix(mix(c00, c10, fraction.x), mix(c01, c11, fraction.x), fraction.y);
}

float3 apply_packed_defocus_pixel(
    uint2 gid,
    float3 center,
    device const uchar* bgr,
    device const uchar* mip_atlas,
    constant TimelineParams& p) {
    float max_radius = p.effect_sigma.w;
    if (max_radius < 0.1f) return center;
    float2 uv = (float2(gid) + 0.5f)
        / float2(p.source_output_dims.xy);
    float2 distance = abs(uv - p.effect_sigma.xy);
    float dist_factor = length(distance) / max(p.effect_sigma.z, 0.01f);
    float local_radius = max_radius * min(dist_factor * dist_factor, 1.0f);
    if (local_radius < 0.5f) return center;
    if (p.effect_extra.y > 0.5f) {
        uint max_level = 0u;
        for (uint level = 1u; level < 8u; ++level) {
            if (p.mip_dims[level].x > 0u) max_level = level;
        }
        float lod = min(log2(local_radius + 1.0f), float(max_level));
        uint low_level = uint(floor(lod));
        uint high_level = min(low_level + 1u, max_level);
        float blend = lod - float(low_level);
        float3 low = sample_defocus_mipmap(
            uv, low_level, bgr, mip_atlas, p);
        float3 high = sample_defocus_mipmap(
            uv, high_level, bgr, mip_atlas, p);
        return mix(low, high, blend);
    }
    float sigma = max(local_radius * 0.5f, 0.1f);
    float denominator = 2.0f * sigma * sigma;
    uint width = p.source_output_dims.x;
    uint height = p.source_output_dims.y;
    float3 sum = float3(0.0f);
    float weight_sum = 0.0f;
    for (int dx = -15; dx <= 15; ++dx) {
        int sx = effect_edge_coord(int(gid.x) + dx, int(width), p);
        float3 sample = load_packed_bgr_pixel(
            uint2(uint(sx), gid.y), bgr, width);
        float weight = exp(-float(dx * dx) / denominator);
        sum += sample * weight;
        weight_sum += weight;
    }
    return sum / max(weight_sum, 1.0e-4f);
}

float sample_packed_edge_luma(int2 position, device const uchar* bgr,
                              constant TimelineParams& p) {
    int2 coord = int2(
        effect_edge_coord(position.x, int(p.source_output_dims.x), p),
        effect_edge_coord(position.y, int(p.source_output_dims.y), p));
    return edge_detect_channel(load_packed_bgr_pixel(
        uint2(coord), bgr, p.source_output_dims.x), p);
}

float3 edge_detect_packed_pixel(
    int2 center_position, device const uchar* bgr,
    constant TimelineParams& p) {
    uint method = uint(p.effect_extra.z + 0.5f);
    int kernel_size = int(p.effect.y + 0.5f);
    float gx = 0.0f, gy = 0.0f;
    if (kernel_size == 5 && (method == 0u || method == 1u)) {
        for (int dy = -2; dy <= 2; ++dy) {
            for (int dx = -2; dx <= 2; ++dx) {
                float value = sample_packed_edge_luma(
                    center_position + int2(dx, dy), bgr, p);
                float coefficient = edge_sobel_smoothing_weight(dy)
                    * float(dx);
                gx += value * coefficient;
                gy += value * coefficient;
            }
        }
        gx /= 12.0f;
        gy /= 12.0f;
    }
    float tl = sample_packed_edge_luma(center_position + int2(-1, -1), bgr, p);
    float tm = sample_packed_edge_luma(center_position + int2(0, -1), bgr, p);
    float tr = sample_packed_edge_luma(center_position + int2(1, -1), bgr, p);
    float ml = sample_packed_edge_luma(center_position + int2(-1, 0), bgr, p);
    float center = sample_packed_edge_luma(center_position, bgr, p);
    float mr = sample_packed_edge_luma(center_position + int2(1, 0), bgr, p);
    float bl = sample_packed_edge_luma(center_position + int2(-1, 1), bgr, p);
    float bm = sample_packed_edge_luma(center_position + int2(0, 1), bgr, p);
    float br = sample_packed_edge_luma(center_position + int2(1, 1), bgr, p);
    if (kernel_size != 5 || (method != 0u && method != 1u)) {
        bool use_prewitt = method == 2u && kernel_size != 5;
        gx = use_prewitt
            ? -tl + tr - ml + mr - bl + br
            : -tl + tr - 2.0f * ml + 2.0f * mr - bl + br;
        gy = use_prewitt
            ? -tl - tm - tr + bl + bm + br
            : -tl - 2.0f * tm - tr + bl + 2.0f * bm + br;
    }
    float laplacian = (kernel_size == 5 && method == 3u)
        ? tm + bm + ml + mr + 0.5f * (tl + tr + bl + br) - 8.0f * center
        : tm + bm + ml + mr - 4.0f * center;
    return finish_edge_detect(gx, gy, laplacian, p);
}

float sample_packed_edge_channel_linear(
    float2 position, device const uchar* bgr, constant TimelineParams& p) {
    int2 base = int2(floor(position));
    float2 fraction = position - float2(base);
    // cv2.remap(INTER_LINEAR) quantizes interpolation fractions to 1/32.
    fraction = floor(fraction * 32.0f + 0.5f) / 32.0f;
    int width = int(p.source_output_dims.x);
    int height = int(p.source_output_dims.y);
    int2 a = int2(effect_edge_coord(base.x, width, p),
                  effect_edge_coord(base.y, height, p));
    int2 b = int2(effect_edge_coord(base.x + 1, width, p),
                  effect_edge_coord(base.y + 1, height, p));
    float c00 = edge_detect_channel(load_packed_bgr_pixel(
        uint2(a), bgr, uint(width)), p);
    float c10 = edge_detect_channel(load_packed_bgr_pixel(
        uint2(b.x, a.y), bgr, uint(width)), p);
    float c01 = edge_detect_channel(load_packed_bgr_pixel(
        uint2(a.x, b.y), bgr, uint(width)), p);
    float c11 = edge_detect_channel(load_packed_bgr_pixel(
        uint2(b), bgr, uint(width)), p);
    return mix(mix(c00, c10, fraction.x),
               mix(c01, c11, fraction.x), fraction.y);
}

kernel void timeline_edge_preblur(
    device const uchar* source [[buffer(0)]],
    device uchar* output [[buffer(1)]],
    constant TimelineParams& p [[buffer(2)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.x;
    uint height = p.source_output_dims.y;
    if (gid.x >= width || gid.y >= height) return;
    float2 center = float2(gid);
    float radius = p.effect_extra.w;
    float value = 4.0f * sample_packed_edge_channel_linear(center, source, p);
    value += 2.0f * sample_packed_edge_channel_linear(
        center + float2(radius, 0.0f), source, p);
    value += 2.0f * sample_packed_edge_channel_linear(
        center - float2(radius, 0.0f), source, p);
    value += 2.0f * sample_packed_edge_channel_linear(
        center + float2(0.0f, radius), source, p);
    value += 2.0f * sample_packed_edge_channel_linear(
        center - float2(0.0f, radius), source, p);
    value += sample_packed_edge_channel_linear(
        center + float2(radius, radius), source, p);
    value += sample_packed_edge_channel_linear(
        center - float2(radius, radius), source, p);
    value += sample_packed_edge_channel_linear(
        center + float2(radius, -radius), source, p);
    value += sample_packed_edge_channel_linear(
        center + float2(-radius, radius), source, p);
    uchar code = to_u8((value / 16.0f) * 255.0f);
    uint index = (gid.y * width + gid.x) * 3u;
    output[index] = code;
    output[index + 1u] = code;
    output[index + 2u] = code;
}

float3 apply_packed_effect_pixel(
    uint2 gid,
    device const uchar* bgr,
    device const float* weights_x,
    device const float* weights_y,
    device const uchar* mip_atlas,
    constant TimelineParams& p) {
    uint width = p.source_output_dims.x;
    uint height = p.source_output_dims.y;
    float3 center = load_packed_bgr_pixel(gid, bgr, width);
    int half_x = int(p.effect.y + 0.5f);
    int half_y = int(p.effect.z + 0.5f);
    uint kind = uint(p.effect.x + 0.5f);
    if (kind == 7u) {
        return apply_packed_denoise_pixel(
            gid, center, bgr, weights_x, weights_y, p);
    }
    if (kind == 9u) {
        return apply_packed_bilateral_pixel(
            gid, center, bgr, weights_x, weights_y, p);
    }
    if (kind == 10u) {
        return apply_packed_motion_blur(gid, center, bgr, p);
    }
    if (kind == 11u) {
        return apply_packed_bokeh_blur(gid, center, bgr, weights_x, p);
    }
    if (kind == 8u) {
        return apply_packed_defocus_pixel(gid, center, bgr, mip_atlas, p);
    }
    if (kind == 6u) {
        return edge_detect_packed_pixel(int2(gid), bgr, p);
    }
    if (kind == 5u) {
        float3 filtered = center;
        for (int dy = -half_y; dy <= half_y; ++dy) {
            int sy = effect_edge_coord(int(gid.y) + dy, int(height), p);
            for (int dx = -half_x; dx <= half_x; ++dx) {
                int sx = effect_edge_coord(int(gid.x) + dx, int(width), p);
                filtered = fold_morphology_pixel(
                    filtered,
                    load_packed_bgr_pixel(uint2(uint(sx), uint(sy)), bgr, width),
                    p);
            }
        }
        return filtered;
    }
    float3 filtered = 0.0f;
    for (int dy = -half_y; dy <= half_y; ++dy) {
        int sy = effect_edge_coord(int(gid.y) + dy, int(height), p);
        float weight_y = weights_y[dy + 32];
        for (int dx = -half_x; dx <= half_x; ++dx) {
            int sx = effect_edge_coord(int(gid.x) + dx, int(width), p);
            float3 sample = load_packed_bgr_pixel(
                uint2(uint(sx), uint(sy)), bgr, width);
            filtered += effect_filter_input(sample, p)
                * (weights_x[dx + 32] * weight_y);
        }
    }
    return finish_effect_pixel(center, filtered, p);
}

float3 prepare_separable_effect_pixel(
    uint2 gid,
    device const uchar* bgr,
    device const ushort* y_plane,
    device const ushort* u_plane,
    device const ushort* v_plane,
    device const half4* horizontal,
    device const float* weights_y,
    constant TimelineParams& p) {
    uint width = p.source_output_dims.x;
    float3 center = prepare_source_pixel_unfiltered(
        gid, bgr, y_plane, u_plane, v_plane, p);
    uint kind = uint(p.effect.x + 0.5f);
    int half_y = int(p.effect.z + 0.5f);
    float3 filtered = kind == 5u ? center : float3(0.0f);
    for (int dy = -half_y; dy <= half_y; ++dy) {
        int sy = effect_edge_coord(int(gid.y) + dy,
                                   int(p.source_output_dims.y), p);
        float3 sample = float3(horizontal[uint(sy) * width + gid.x].rgb);
        if (kind == 5u) {
            filtered = fold_morphology_pixel(filtered, sample, p);
        } else {
            filtered += sample * weights_y[dy + 32];
        }
    }
    return finish_effect_pixel(center, filtered, p);
}

float3 sample_scaled_separable_effect_pixel(
    uint2 gid,
    device const uchar* bgr,
    device const ushort* y_plane,
    device const ushort* u_plane,
    device const ushort* v_plane,
    device const half4* horizontal,
    device const float* weights_y,
    constant TimelineParams& p) {
    uint sw = p.source_output_dims.x, sh = p.source_output_dims.y;
    uint dw = p.source_output_dims.z, dh = p.source_output_dims.w;
    if (sw == dw && sh == dh) {
        return prepare_separable_effect_pixel(
            gid, bgr, y_plane, u_plane, v_plane, horizontal, weights_y, p)
            * p.control.x;
    }
    float2 pos = (float2(gid) + 0.5f) * float2(sw, sh)
        / float2(dw, dh) - 0.5f;
    int2 p0 = int2(floor(pos));
    float2 f = pos - float2(p0);
    int2 hi = int2(int(sw) - 1, int(sh) - 1);
    int2 a = clamp(p0, int2(0), hi);
    int2 b = clamp(p0 + int2(1), int2(0), hi);
    float3 c00 = prepare_separable_effect_pixel(
        uint2(a), bgr, y_plane, u_plane, v_plane, horizontal, weights_y, p);
    float3 c10 = prepare_separable_effect_pixel(
        uint2(b.x, a.y), bgr, y_plane, u_plane, v_plane,
        horizontal, weights_y, p);
    float3 c01 = prepare_separable_effect_pixel(
        uint2(a.x, b.y), bgr, y_plane, u_plane, v_plane,
        horizontal, weights_y, p);
    float3 c11 = prepare_separable_effect_pixel(
        uint2(b), bgr, y_plane, u_plane, v_plane, horizontal, weights_y, p);
    return mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y)
        * p.control.x;
}

float3 prepare_source_pixel(
    uint2 gid,
    device const uchar* bgr,
    device const ushort* y_plane,
    device const ushort* u_plane,
    device const ushort* v_plane,
    constant TimelineParams& p) {
    float3 center = prepare_source_pixel_unfiltered(
        gid, bgr, y_plane, u_plane, v_plane, p);
    uint kind = uint(p.effect.x + 0.5f);
    if (kind == 0u) return center;

    if (kind == 6u) {
        return edge_detect_source_pixel(
            int2(gid), bgr, y_plane, u_plane, v_plane, p);
    }

    int half_x = int(p.effect.y + 0.5f);
    int half_y = int(p.effect.z + 0.5f);
    if (kind == 5u) {
        float3 filtered = center;
        for (int dy = -half_y; dy <= half_y; ++dy) {
            int sy = effect_edge_coord(int(gid.y) + dy,
                                       int(p.source_output_dims.y), p);
            for (int dx = -half_x; dx <= half_x; ++dx) {
                int sx = effect_edge_coord(int(gid.x) + dx,
                                           int(p.source_output_dims.x), p);
                filtered = fold_morphology_pixel(filtered,
                    prepare_source_pixel_unfiltered(
                        uint2(uint(sx), uint(sy)), bgr,
                        y_plane, u_plane, v_plane, p), p);
            }
        }
        return filtered;
    }
    float sigma_x = max(p.effect_sigma.x, 0.01f);
    float sigma_y = max(p.effect_sigma.y, 0.01f);
    float3 filtered = 0.0f;
    float weights_x[33];
    float weights_y[33];
    float weight_sum_x = 0.0f;
    float weight_sum_y = 0.0f;
    // Gaussian is separable. Precompute each 1D axis once per output pixel
    // rather than evaluating exp() for both axes at every 2D source tap.
    for (int dx = -half_x; dx <= half_x; ++dx) {
        float wx = effect_axis_weight(kind, dx, half_x, sigma_x);
        weights_x[dx + 16] = wx;
        weight_sum_x += wx;
    }
    for (int dy = -half_y; dy <= half_y; ++dy) {
        float wy = effect_axis_weight(kind, dy, half_y, sigma_y);
        weights_y[dy + 16] = wy;
        weight_sum_y += wy;
    }
    for (int dy = -half_y; dy <= half_y; ++dy) {
        int sy = effect_edge_coord(int(gid.y) + dy,
                                   int(p.source_output_dims.y), p);
        for (int dx = -half_x; dx <= half_x; ++dx) {
            int sx = effect_edge_coord(int(gid.x) + dx,
                                       int(p.source_output_dims.x), p);
            float weight = weights_x[dx + 16] * weights_y[dy + 16];
            filtered += effect_filter_input(prepare_source_pixel_unfiltered(
                uint2(sx, sy), bgr, y_plane, u_plane, v_plane, p), p) * weight;
        }
    }
    filtered /= max(weight_sum_x * weight_sum_y, 1.0e-6f);
    return finish_effect_pixel(center, filtered, p);
}

float3 sample_scaled_pixel(
    uint2 gid,
    device const uchar* bgr,
    device const ushort* y_plane,
    device const ushort* u_plane,
    device const ushort* v_plane,
    constant TimelineParams& p) {
    uint sw = p.source_output_dims.x, sh = p.source_output_dims.y;
    uint dw = p.source_output_dims.z, dh = p.source_output_dims.w;
    if (sw == dw && sh == dh) {
        return prepare_source_pixel(gid, bgr, y_plane, u_plane, v_plane, p)
            * p.control.x;
    }
    float2 pos = (float2(gid) + 0.5f) * float2(sw, sh) / float2(dw, dh) - 0.5f;
    int2 p0 = int2(floor(pos));
    float2 f = pos - float2(p0);
    int2 hi = int2(int(sw) - 1, int(sh) - 1);
    int2 a = clamp(p0, int2(0), hi);
    int2 b = clamp(p0 + int2(1), int2(0), hi);
    float3 c00 = prepare_source_pixel(uint2(a), bgr, y_plane, u_plane, v_plane, p);
    float3 c10 = prepare_source_pixel(uint2(b.x, a.y), bgr, y_plane, u_plane, v_plane, p);
    float3 c01 = prepare_source_pixel(uint2(a.x, b.y), bgr, y_plane, u_plane, v_plane, p);
    float3 c11 = prepare_source_pixel(uint2(b), bgr, y_plane, u_plane, v_plane, p);
    return mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y) * p.control.x;
}

float read_texture_code(float value, uint bit_depth) {
    if (bit_depth > 8u) {
        float packed_shift = float(1u << (16u - bit_depth));
        return value * 65535.0f / packed_shift;
    }
    return value * 255.0f;
}

float2 read_chroma_texture(
    texture2d<float, access::read> uv_plane,
    uint2 coord,
    uint bit_depth) {
    float2 sample = uv_plane.read(coord).rg;
    float2 code = float2(read_texture_code(sample.x, bit_depth),
                         read_texture_code(sample.y, bit_depth));
    return bit_depth > 8u ? rint(code) : floor(code + 0.5f);
}

float2 sample_chroma_texture(
    texture2d<float, access::read> uv_plane,
    float2 pos,
    uint bit_depth) {
    int2 p0 = int2(floor(pos));
    float2 f = pos - float2(p0);
    int2 hi = int2(int(uv_plane.get_width()) - 1,
                   int(uv_plane.get_height()) - 1);
    int2 a = clamp(p0, int2(0), hi);
    int2 b = clamp(p0 + int2(1), int2(0), hi);
    float2 c00 = read_chroma_texture(uv_plane, uint2(a), bit_depth);
    float2 c10 = read_chroma_texture(uv_plane, uint2(b.x, a.y), bit_depth);
    float2 c01 = read_chroma_texture(uv_plane, uint2(a.x, b.y), bit_depth);
    float2 c11 = read_chroma_texture(uv_plane, uint2(b), bit_depth);
    float2 code = mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y);
    return bit_depth > 8u ? rint(code) : floor(code + 0.5f);
}

float3 prepare_texture_source_pixel_unfiltered(
    uint2 gid,
    texture2d<float, access::read> y_plane,
    texture2d<float, access::read> uv_plane,
    constant TimelineParams& p) {
    uint bit_depth = p.source_flags.x;
    float max_code = float((1u << bit_depth) - 1u);
    float y_code = read_texture_code(y_plane.read(gid).r, bit_depth);
    float2 chroma_pos = (float2(gid) + 0.5f)
        / float2(p.source_flags.y, p.source_flags.z) - 0.5f;
    float2 chroma = sample_chroma_texture(uv_plane, chroma_pos, bit_depth);
    float y, cb, cr;
    if (p.control.w > 0.5f) {
        y = y_code / max_code;
        cb = (chroma.x - max_code * 0.5f) / max_code;
        cr = (chroma.y - max_code * 0.5f) / max_code;
    } else {
        float scale_8 = float(1u << (bit_depth - 8u));
        y = (y_code - 16.0f * scale_8) / (219.0f * scale_8);
        cb = (chroma.x - 128.0f * scale_8) / (224.0f * scale_8);
        cr = (chroma.y - 128.0f * scale_8) / (224.0f * scale_8);
    }
    float kr = p.yuv.x;
    float kb = p.yuv.y;
    float kg = 1.0f - kr - kb;
    float3 color = clamp(float3(
        y + 2.0f * (1.0f - kr) * cr,
        y - 2.0f * kb * (1.0f - kb) / kg * cb
          - 2.0f * kr * (1.0f - kr) / kg * cr,
        y + 2.0f * (1.0f - kb) * cb), 0.0f, 1.0f);
    if (p.control.y > 0.5f) color = apply_primary_grade(color, p);
    return float3(to_u8(color.r * 255.0f), to_u8(color.g * 255.0f),
                  to_u8(color.b * 255.0f));
}

float sample_texture_edge_luma(
    int2 position,
    texture2d<float, access::read> y_plane,
    texture2d<float, access::read> uv_plane,
    constant TimelineParams& p) {
    int2 coord = int2(
        effect_edge_coord(position.x, int(p.source_output_dims.x), p),
        effect_edge_coord(position.y, int(p.source_output_dims.y), p));
    return edge_detect_channel(prepare_texture_source_pixel_unfiltered(
        uint2(coord), y_plane, uv_plane, p), p);
}

float3 edge_detect_texture_pixel(
    int2 center_position,
    texture2d<float, access::read> y_plane,
    texture2d<float, access::read> uv_plane,
    constant TimelineParams& p) {
    uint method = uint(p.effect_extra.z + 0.5f);
    int kernel_size = int(p.effect.y + 0.5f);
    float gx = 0.0f, gy = 0.0f;
    if (kernel_size == 5 && (method == 0u || method == 1u)) {
        for (int dy = -2; dy <= 2; ++dy) {
            for (int dx = -2; dx <= 2; ++dx) {
                float value = sample_texture_edge_luma(
                    center_position + int2(dx, dy), y_plane, uv_plane, p);
                float coefficient = edge_sobel_smoothing_weight(dy)
                    * float(dx);
                gx += value * coefficient;
                gy += value * coefficient;
            }
        }
        gx /= 12.0f;
        gy /= 12.0f;
    }
    float tl = sample_texture_edge_luma(center_position + int2(-1, -1), y_plane, uv_plane, p);
    float tm = sample_texture_edge_luma(center_position + int2(0, -1), y_plane, uv_plane, p);
    float tr = sample_texture_edge_luma(center_position + int2(1, -1), y_plane, uv_plane, p);
    float ml = sample_texture_edge_luma(center_position + int2(-1, 0), y_plane, uv_plane, p);
    float center = sample_texture_edge_luma(center_position, y_plane, uv_plane, p);
    float mr = sample_texture_edge_luma(center_position + int2(1, 0), y_plane, uv_plane, p);
    float bl = sample_texture_edge_luma(center_position + int2(-1, 1), y_plane, uv_plane, p);
    float bm = sample_texture_edge_luma(center_position + int2(0, 1), y_plane, uv_plane, p);
    float br = sample_texture_edge_luma(center_position + int2(1, 1), y_plane, uv_plane, p);
    if (kernel_size != 5 || (method != 0u && method != 1u)) {
        bool use_prewitt = method == 2u && kernel_size != 5;
        gx = use_prewitt
            ? -tl + tr - ml + mr - bl + br
            : -tl + tr - 2.0f * ml + 2.0f * mr - bl + br;
        gy = use_prewitt
            ? -tl - tm - tr + bl + bm + br
            : -tl - 2.0f * tm - tr + bl + 2.0f * bm + br;
    }
    float laplacian = (kernel_size == 5 && method == 3u)
        ? tm + bm + ml + mr + 0.5f * (tl + tr + bl + br) - 8.0f * center
        : tm + bm + ml + mr - 4.0f * center;
    return finish_edge_detect(gx, gy, laplacian, p);
}

float3 prepare_separable_texture_effect_pixel(
    uint2 gid,
    texture2d<float, access::read> y_plane,
    texture2d<float, access::read> uv_plane,
    device const half4* horizontal,
    device const float* weights_y,
    constant TimelineParams& p) {
    uint width = p.source_output_dims.x;
    float3 center = prepare_texture_source_pixel_unfiltered(
        gid, y_plane, uv_plane, p);
    uint kind = uint(p.effect.x + 0.5f);
    int half_y = int(p.effect.z + 0.5f);
    float3 filtered = kind == 5u ? center : float3(0.0f);
    for (int dy = -half_y; dy <= half_y; ++dy) {
        int sy = effect_edge_coord(int(gid.y) + dy,
                                   int(p.source_output_dims.y), p);
        float3 sample = float3(horizontal[uint(sy) * width + gid.x].rgb);
        if (kind == 5u) {
            filtered = fold_morphology_pixel(filtered, sample, p);
        } else {
            filtered += sample * weights_y[dy + 32];
        }
    }
    return finish_effect_pixel(center, filtered, p);
}

float3 sample_scaled_separable_texture_effect_pixel(
    uint2 gid,
    texture2d<float, access::read> y_plane,
    texture2d<float, access::read> uv_plane,
    device const half4* horizontal,
    device const float* weights_y,
    constant TimelineParams& p) {
    uint sw = p.source_output_dims.x, sh = p.source_output_dims.y;
    uint dw = p.source_output_dims.z, dh = p.source_output_dims.w;
    if (sw == dw && sh == dh) {
        return prepare_separable_texture_effect_pixel(
            gid, y_plane, uv_plane, horizontal, weights_y, p) * p.control.x;
    }
    float2 pos = (float2(gid) + 0.5f) * float2(sw, sh)
        / float2(dw, dh) - 0.5f;
    int2 p0 = int2(floor(pos));
    float2 f = pos - float2(p0);
    int2 hi = int2(int(sw) - 1, int(sh) - 1);
    int2 a = clamp(p0, int2(0), hi);
    int2 b = clamp(p0 + int2(1), int2(0), hi);
    float3 c00 = prepare_separable_texture_effect_pixel(
        uint2(a), y_plane, uv_plane, horizontal, weights_y, p);
    float3 c10 = prepare_separable_texture_effect_pixel(
        uint2(b.x, a.y), y_plane, uv_plane, horizontal, weights_y, p);
    float3 c01 = prepare_separable_texture_effect_pixel(
        uint2(a.x, b.y), y_plane, uv_plane, horizontal, weights_y, p);
    float3 c11 = prepare_separable_texture_effect_pixel(
        uint2(b), y_plane, uv_plane, horizontal, weights_y, p);
    return mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y)
        * p.control.x;
}

float3 prepare_texture_source_pixel(
    uint2 gid,
    texture2d<float, access::read> y_plane,
    texture2d<float, access::read> uv_plane,
    constant TimelineParams& p) {
    float3 center = prepare_texture_source_pixel_unfiltered(
        gid, y_plane, uv_plane, p);
    uint kind = uint(p.effect.x + 0.5f);
    if (kind == 0u) return center;

    if (kind == 6u) {
        return edge_detect_texture_pixel(
            int2(gid), y_plane, uv_plane, p);
    }

    int half_x = int(p.effect.y + 0.5f);
    int half_y = int(p.effect.z + 0.5f);
    if (kind == 5u) {
        float3 filtered = center;
        for (int dy = -half_y; dy <= half_y; ++dy) {
            int sy = effect_edge_coord(int(gid.y) + dy,
                                       int(p.source_output_dims.y), p);
            for (int dx = -half_x; dx <= half_x; ++dx) {
                int sx = effect_edge_coord(int(gid.x) + dx,
                                           int(p.source_output_dims.x), p);
                filtered = fold_morphology_pixel(filtered,
                    prepare_texture_source_pixel_unfiltered(
                        uint2(uint(sx), uint(sy)), y_plane, uv_plane, p), p);
            }
        }
        return filtered;
    }
    float sigma_x = max(p.effect_sigma.x, 0.01f);
    float sigma_y = max(p.effect_sigma.y, 0.01f);
    float3 filtered = 0.0f;
    float weights_x[33];
    float weights_y[33];
    float weight_sum_x = 0.0f;
    float weight_sum_y = 0.0f;
    // The zero-copy VideoToolbox texture route is the common macOS path, so
    // keep its Gaussian weights separable too; do not recompute exp() at
    // every two-dimensional source tap.
    for (int dx = -half_x; dx <= half_x; ++dx) {
        float wx = effect_axis_weight(kind, dx, half_x, sigma_x);
        weights_x[dx + 16] = wx;
        weight_sum_x += wx;
    }
    for (int dy = -half_y; dy <= half_y; ++dy) {
        float wy = effect_axis_weight(kind, dy, half_y, sigma_y);
        weights_y[dy + 16] = wy;
        weight_sum_y += wy;
    }
    for (int dy = -half_y; dy <= half_y; ++dy) {
        int sy = effect_edge_coord(int(gid.y) + dy,
                                   int(p.source_output_dims.y), p);
        for (int dx = -half_x; dx <= half_x; ++dx) {
            int sx = effect_edge_coord(int(gid.x) + dx,
                                       int(p.source_output_dims.x), p);
            float weight = weights_x[dx + 16] * weights_y[dy + 16];
            filtered += effect_filter_input(prepare_texture_source_pixel_unfiltered(
                uint2(sx, sy), y_plane, uv_plane, p), p) * weight;
        }
    }
    filtered /= max(weight_sum_x * weight_sum_y, 1.0e-6f);
    return finish_effect_pixel(center, filtered, p);
}

float3 sample_scaled_texture_pixel(
    uint2 gid,
    texture2d<float, access::read> y_plane,
    texture2d<float, access::read> uv_plane,
    constant TimelineParams& p) {
    uint sw = p.source_output_dims.x, sh = p.source_output_dims.y;
    uint dw = p.source_output_dims.z, dh = p.source_output_dims.w;
    if (sw == dw && sh == dh) {
        return prepare_texture_source_pixel(gid, y_plane, uv_plane, p)
            * p.control.x;
    }
    float2 pos = (float2(gid) + 0.5f) * float2(sw, sh)
        / float2(dw, dh) - 0.5f;
    int2 p0 = int2(floor(pos));
    float2 f = pos - float2(p0);
    int2 hi = int2(int(sw) - 1, int(sh) - 1);
    int2 a = clamp(p0, int2(0), hi);
    int2 b = clamp(p0 + int2(1), int2(0), hi);
    float3 c00 = prepare_texture_source_pixel(uint2(a), y_plane, uv_plane, p);
    float3 c10 = prepare_texture_source_pixel(uint2(b.x, a.y), y_plane, uv_plane, p);
    float3 c01 = prepare_texture_source_pixel(uint2(a.x, b.y), y_plane, uv_plane, p);
    float3 c11 = prepare_texture_source_pixel(uint2(b), y_plane, uv_plane, p);
    return mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y)
        * p.control.x;
}

// Topos 4:2:2 native surface path.  The decoder writes three uint16 planes
// into IOSurface; Metal imports each plane as an R16Uint texture.  Keep the
// code-value/range/matrix math identical to the buffer path above, while
// avoiding a NumPy -> MTLBuffer upload for every frame.
float sample_planar_u16(
    texture2d<ushort, access::read> plane,
    float2 pos) {
    int2 p0 = int2(floor(pos));
    float2 f = pos - float2(p0);
    int2 hi = int2(int(plane.get_width()) - 1,
                   int(plane.get_height()) - 1);
    int2 a = clamp(p0, int2(0), hi);
    int2 b = clamp(p0 + int2(1), int2(0), hi);
    float c00 = float(plane.read(uint2(a)).r);
    float c10 = float(plane.read(uint2(b.x, a.y)).r);
    float c01 = float(plane.read(uint2(a.x, b.y)).r);
    float c11 = float(plane.read(uint2(b)).r);
    return rint(mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y));
}

float3 prepare_planar_texture_source_pixel(
    uint2 gid,
    texture2d<ushort, access::read> y_plane,
    texture2d<ushort, access::read> u_plane,
    texture2d<ushort, access::read> v_plane,
    constant TimelineParams& p) {
    uint bit_depth = p.source_flags.x;
    float max_code = float((1u << bit_depth) - 1u);
    uint2 ycoord = min(gid, uint2(y_plane.get_width() - 1u,
                                  y_plane.get_height() - 1u));
    float y_code = float(y_plane.read(ycoord).r);
    float2 chroma_pos = (float2(gid) + 0.5f)
        / float2(p.source_flags.y, p.source_flags.z) - 0.5f;
    float u_code = sample_planar_u16(u_plane, chroma_pos);
    float v_code = sample_planar_u16(v_plane, chroma_pos);
    float y, cb, cr;
    if (p.control.w > 0.5f) {
        y = y_code / max_code;
        cb = (u_code - max_code * 0.5f) / max_code;
        cr = (v_code - max_code * 0.5f) / max_code;
    } else {
        float scale_8 = float(1u << (bit_depth - 8u));
        y = (y_code - 16.0f * scale_8) / (219.0f * scale_8);
        cb = (u_code - 128.0f * scale_8) / (224.0f * scale_8);
        cr = (v_code - 128.0f * scale_8) / (224.0f * scale_8);
    }
    float kr = p.yuv.x;
    float kb = p.yuv.y;
    float kg = 1.0f - kr - kb;
    float3 color = clamp(float3(
        y + 2.0f * (1.0f - kr) * cr,
        y - 2.0f * kb * (1.0f - kb) / kg * cb
          - 2.0f * kr * (1.0f - kr) / kg * cr,
        y + 2.0f * (1.0f - kb) * cb), 0.0f, 1.0f);
    if (p.control.y > 0.5f) color = apply_primary_grade(color, p);
    return float3(to_u8(color.r * 255.0f), to_u8(color.g * 255.0f),
                  to_u8(color.b * 255.0f));
}

float3 sample_scaled_planar_texture_pixel(
    uint2 gid,
    texture2d<ushort, access::read> y_plane,
    texture2d<ushort, access::read> u_plane,
    texture2d<ushort, access::read> v_plane,
    constant TimelineParams& p) {
    uint sw = p.source_output_dims.x, sh = p.source_output_dims.y;
    uint dw = p.source_output_dims.z, dh = p.source_output_dims.w;
    if (sw == dw && sh == dh) {
        return prepare_planar_texture_source_pixel(
            gid, y_plane, u_plane, v_plane, p) * p.control.x;
    }
    float2 pos = (float2(gid) + 0.5f) * float2(sw, sh)
        / float2(dw, dh) - 0.5f;
    int2 p0 = int2(floor(pos));
    float2 f = pos - float2(p0);
    int2 hi = int2(int(sw) - 1, int(sh) - 1);
    int2 a = clamp(p0, int2(0), hi);
    int2 b = clamp(p0 + int2(1), int2(0), hi);
    float3 c00 = prepare_planar_texture_source_pixel(
        uint2(a), y_plane, u_plane, v_plane, p);
    float3 c10 = prepare_planar_texture_source_pixel(
        uint2(b.x, a.y), y_plane, u_plane, v_plane, p);
    float3 c01 = prepare_planar_texture_source_pixel(
        uint2(a.x, b.y), y_plane, u_plane, v_plane, p);
    float3 c11 = prepare_planar_texture_source_pixel(
        uint2(b), y_plane, u_plane, v_plane, p);
    return mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y)
        * p.control.x;
}

kernel void timeline_fused_export_texture_planar_yuv422p10(
    texture2d<ushort, access::read> y_plane [[texture(0)]],
    texture2d<ushort, access::read> u_plane [[texture(1)]],
    texture2d<ushort, access::read> v_plane [[texture(2)]],
    device ushort* output [[buffer(2)]],
    constant TimelineParams& p [[buffer(3)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.z, height = p.source_output_dims.w;
    if (gid.x >= width || gid.y >= height) return;
    float3 color = sample_scaled_planar_texture_pixel(
        gid, y_plane, u_plane, v_plane, p);
    float3 yuv = rgb_code_to_yuv422p10(
        color, p.yuv.x, p.yuv.y, p.control.w > 0.5f);
    uint luma_size = width * height;
    uint chroma_width = width / 2u;
    uint chroma_size = chroma_width * height;
    uint index = gid.y * width + gid.x;
    output[index] = to_u16(yuv.x);
    if (gid.x < chroma_width) {
        uint2 p0 = uint2(gid.x * 2u, gid.y);
        float3 c0 = sample_scaled_planar_texture_pixel(
            p0, y_plane, u_plane, v_plane, p);
        float3 c1 = sample_scaled_planar_texture_pixel(
            p0 + uint2(1u, 0u), y_plane, u_plane, v_plane, p);
        float3 chroma = rgb_code_to_yuv422p10(
            (c0 + c1) * 0.5f, p.yuv.x, p.yuv.y, p.control.w > 0.5f);
        uint ci = gid.y * chroma_width + gid.x;
        output[luma_size + ci] = to_u16(chroma.y);
        output[luma_size + chroma_size + ci] = to_u16(chroma.z);
    }
}

kernel void timeline_fused_export(
    device const uchar* bgr [[buffer(0)]],
    device const ushort* y_plane [[buffer(1)]],
    device const ushort* u_plane [[buffer(2)]],
    device const ushort* v_plane [[buffer(3)]],
    device uchar* output [[buffer(4)]],
    constant TimelineParams& p [[buffer(5)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.z, height = p.source_output_dims.w;
    if (gid.x >= width || gid.y >= height) return;
    float3 color = sample_scaled_pixel(gid, bgr, y_plane, u_plane, v_plane, p);
    uint pixel = (gid.y * width + gid.x) * 3u;
    if (p.control.z < 0.5f) {
        output[pixel] = to_u8(color.b);
        output[pixel + 1u] = to_u8(color.g);
        output[pixel + 2u] = to_u8(color.r);
        return;
    }
    uint luma_size = width * height;
    uint chroma_width = width / 2u, chroma_height = height / 2u;
    uint chroma_size = chroma_width * chroma_height;
    float3 rgb = float3(to_u8(color.r), to_u8(color.g), to_u8(color.b)) / 255.0f;
    float y = dot(rgb, float3(0.2126f, 0.7152f, 0.0722f));
    output[gid.y * width + gid.x] = to_u8(clamp(16.0f + 219.0f * y, 16.0f, 235.0f));
    if (gid.x < chroma_width && gid.y < chroma_height) {
        uint2 p00 = gid * 2u;
        float3 c0 = sample_scaled_pixel(p00, bgr, y_plane, u_plane, v_plane, p);
        float3 c1 = sample_scaled_pixel(p00 + uint2(1u, 0u), bgr, y_plane, u_plane, v_plane, p);
        float3 c2 = sample_scaled_pixel(p00 + uint2(0u, 1u), bgr, y_plane, u_plane, v_plane, p);
        float3 c3 = sample_scaled_pixel(p00 + uint2(1u, 1u), bgr, y_plane, u_plane, v_plane, p);
        float3 avg = (float3(to_u8(c0.r), to_u8(c0.g), to_u8(c0.b))
                    + float3(to_u8(c1.r), to_u8(c1.g), to_u8(c1.b))
                    + float3(to_u8(c2.r), to_u8(c2.g), to_u8(c2.b))
                    + float3(to_u8(c3.r), to_u8(c3.g), to_u8(c3.b))) / 1020.0f;
        float yy = dot(avg, float3(0.2126f, 0.7152f, 0.0722f));
        float cb = (avg.b - yy) / 1.8556f;
        float cr = (avg.r - yy) / 1.5748f;
        uint ci = gid.y * chroma_width + gid.x;
        output[luma_size + ci] = to_u8(clamp(128.0f + 224.0f * cb, 16.0f, 240.0f));
        output[luma_size + chroma_size + ci] = to_u8(clamp(128.0f + 224.0f * cr, 16.0f, 240.0f));
    }
}

// High-depth packed delivery for Topos/prores 10/12-bit encoders.
// AUD-18601 (comment correction): the compositor's working set is the
// 8-bit code domain (prepare_*_source_pixel quantizes with to_u8), so the
// uint16 frame below carries 8-bit effective precision (code * 257), not a
// true 16-bit signal.  Host-side routing therefore fails closed for >8-bit
// sources on u16/yuv422p10 delivery ("metal_8bit_chain_high_depth_source")
// so those exports fall back to the GL RGB16UI chain / CPU BGR16 path;
// 8-bit sources keep this fast fused path (mirrors the GL renderer's
// ">8-bit 源走 RGB16UI 全链，8-bit 源仅出口 u16" contract).
kernel void timeline_fused_export_u16(
    device const uchar* bgr [[buffer(0)]],
    device const ushort* y_plane [[buffer(1)]],
    device const ushort* u_plane [[buffer(2)]],
    device const ushort* v_plane [[buffer(3)]],
    device ushort* output [[buffer(4)]],
    constant TimelineParams& p [[buffer(5)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.z, height = p.source_output_dims.w;
    if (gid.x >= width || gid.y >= height) return;
    float3 color = sample_scaled_pixel(gid, bgr, y_plane, u_plane, v_plane, p);
    uint pixel = (gid.y * width + gid.x) * 3u;
    output[pixel] = to_u16(color.b * (65535.0f / 255.0f));
    output[pixel + 1u] = to_u16(color.g * (65535.0f / 255.0f));
    output[pixel + 2u] = to_u16(color.r * (65535.0f / 255.0f));
}

kernel void timeline_fused_export_yuv422p10(
    device const uchar* bgr [[buffer(0)]],
    device const ushort* y_plane [[buffer(1)]],
    device const ushort* u_plane [[buffer(2)]],
    device const ushort* v_plane [[buffer(3)]],
    device ushort* output [[buffer(4)]],
    constant TimelineParams& p [[buffer(5)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.z, height = p.source_output_dims.w;
    if (gid.x >= width || gid.y >= height) return;
    float3 color = sample_scaled_pixel(gid, bgr, y_plane, u_plane, v_plane, p);
    float3 yuv = rgb_code_to_yuv422p10(
        color, p.yuv.x, p.yuv.y, p.control.w > 0.5f);
    uint luma_size = width * height;
    uint chroma_width = width / 2u;
    uint chroma_size = chroma_width * height;
    uint index = gid.y * width + gid.x;
    output[index] = to_u16(yuv.x);
    if (gid.x < chroma_width) {
        uint2 p0 = uint2(gid.x * 2u, gid.y);
        float3 c0 = sample_scaled_pixel(p0, bgr, y_plane, u_plane, v_plane, p);
        float3 c1 = sample_scaled_pixel(p0 + uint2(1u, 0u), bgr,
                                        y_plane, u_plane, v_plane, p);
        float3 chroma = rgb_code_to_yuv422p10(
            (c0 + c1) * 0.5f, p.yuv.x, p.yuv.y, p.control.w > 0.5f);
        uint ci = gid.y * chroma_width + gid.x;
        output[luma_size + ci] = to_u16(chroma.y);
        output[luma_size + chroma_size + ci] = to_u16(chroma.z);
    }
}

kernel void timeline_fused_export_texture(
    texture2d<float, access::read> y_plane [[texture(0)]],
    texture2d<float, access::read> uv_plane [[texture(1)]],
    device uchar* output [[buffer(2)]],
    constant TimelineParams& p [[buffer(3)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.z, height = p.source_output_dims.w;
    if (gid.x >= width || gid.y >= height) return;
    float3 color = sample_scaled_texture_pixel(gid, y_plane, uv_plane, p);
    uint pixel = (gid.y * width + gid.x) * 3u;
    if (p.control.z < 0.5f) {
        output[pixel] = to_u8(color.b);
        output[pixel + 1u] = to_u8(color.g);
        output[pixel + 2u] = to_u8(color.r);
        return;
    }
    uint luma_size = width * height;
    uint chroma_width = width / 2u, chroma_height = height / 2u;
    uint chroma_size = chroma_width * chroma_height;
    float3 rgb = float3(to_u8(color.r), to_u8(color.g), to_u8(color.b)) / 255.0f;
    float y = dot(rgb, float3(0.2126f, 0.7152f, 0.0722f));
    output[gid.y * width + gid.x] = to_u8(clamp(16.0f + 219.0f * y, 16.0f, 235.0f));
    if (gid.x < chroma_width && gid.y < chroma_height) {
        uint2 p00 = gid * 2u;
        float3 c0 = sample_scaled_texture_pixel(p00, y_plane, uv_plane, p);
        float3 c1 = sample_scaled_texture_pixel(p00 + uint2(1u, 0u), y_plane, uv_plane, p);
        float3 c2 = sample_scaled_texture_pixel(p00 + uint2(0u, 1u), y_plane, uv_plane, p);
        float3 c3 = sample_scaled_texture_pixel(p00 + uint2(1u, 1u), y_plane, uv_plane, p);
        float3 avg = (float3(to_u8(c0.r), to_u8(c0.g), to_u8(c0.b))
                    + float3(to_u8(c1.r), to_u8(c1.g), to_u8(c1.b))
                    + float3(to_u8(c2.r), to_u8(c2.g), to_u8(c2.b))
                    + float3(to_u8(c3.r), to_u8(c3.g), to_u8(c3.b))) / 1020.0f;
        float yy = dot(avg, float3(0.2126f, 0.7152f, 0.0722f));
        float cb = (avg.b - yy) / 1.8556f;
        float cr = (avg.r - yy) / 1.5748f;
        uint ci = gid.y * chroma_width + gid.x;
        output[luma_size + ci] = to_u8(clamp(128.0f + 224.0f * cb, 16.0f, 240.0f));
        output[luma_size + chroma_size + ci] = to_u8(clamp(128.0f + 224.0f * cr, 16.0f, 240.0f));
    }
}

// Direct VideoToolbox surface paths.  The destination planes are
// IOSurface-backed CVPixelBuffer textures imported by CVMetalTextureCache;
// the kernels write them in place and never materialize a CPU YUV array.
float3 rgb_code_to_yuv420p8(float3 rgb_code) {
    float3 rgb = clamp(rgb_code / 255.0f, 0.0f, 1.0f);
    float y = dot(rgb, float3(0.2126f, 0.7152f, 0.0722f));
    float cb = (rgb.b - y) / 1.8556f;
    float cr = (rgb.r - y) / 1.5748f;
    return float3(
        clamp(16.0f + 219.0f * y, 16.0f, 235.0f),
        clamp(128.0f + 224.0f * cb, 16.0f, 240.0f),
        clamp(128.0f + 224.0f * cr, 16.0f, 240.0f));
}

kernel void timeline_fused_export_texture_yuv420_surface(
    texture2d<float, access::read> y_plane [[texture(0)]],
    texture2d<float, access::read> uv_plane [[texture(1)]],
    texture2d<float, access::write> y_output [[texture(2)]],
    texture2d<float, access::write> uv_output [[texture(3)]],
    constant TimelineParams& p [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.z, height = p.source_output_dims.w;
    if (gid.x >= width || gid.y >= height) return;
    float3 yuv = rgb_code_to_yuv420p8(
        sample_scaled_texture_pixel(gid, y_plane, uv_plane, p));
    y_output.write(float4(yuv.x / 255.0f, 0.0f, 0.0f, 1.0f), gid);
    if ((gid.x & 1u) == 0u && (gid.y & 1u) == 0u) {
        uint2 p00 = gid;
        float3 c0 = rgb_code_to_yuv420p8(
            sample_scaled_texture_pixel(p00, y_plane, uv_plane, p));
        float3 c1 = rgb_code_to_yuv420p8(
            sample_scaled_texture_pixel(p00 + uint2(1u, 0u), y_plane, uv_plane, p));
        float3 c2 = rgb_code_to_yuv420p8(
            sample_scaled_texture_pixel(p00 + uint2(0u, 1u), y_plane, uv_plane, p));
        float3 c3 = rgb_code_to_yuv420p8(
            sample_scaled_texture_pixel(p00 + uint2(1u, 1u), y_plane, uv_plane, p));
        float2 chroma = (float2(c0.y, c0.z) + float2(c1.y, c1.z)
                       + float2(c2.y, c2.z) + float2(c3.y, c3.z))
                      / (4.0f * 255.0f);
        uv_output.write(float4(chroma.x, chroma.y, 0.0f, 1.0f),
                        uint2(gid.x / 2u, gid.y / 2u));
    }
}

float3 rgb_code_to_yuv420p10(float3 rgb_code) {
    float3 rgb = clamp(rgb_code / 255.0f, 0.0f, 1.0f);
    float y = dot(rgb, float3(0.2126f, 0.7152f, 0.0722f));
    float cb = (rgb.b - y) / 1.8556f;
    float cr = (rgb.r - y) / 1.5748f;
    return float3(
        clamp(64.0f + 876.0f * y, 64.0f, 940.0f),
        clamp(512.0f + 896.0f * cb, 64.0f, 960.0f),
        clamp(512.0f + 896.0f * cr, 64.0f, 960.0f));
}

// P010 stores 10-bit video-range code values in the high bits of a 16-bit
// sample.  R16Unorm/RG16Unorm are the formats exposed by CVMetalTextureCache
// for CoreVideo's x420 pixel buffers, so normalized writes preserve the
// IOSurface-owned surface contract without a CPU pack or staging buffer.
kernel void timeline_fused_export_texture_p010_surface(
    texture2d<float, access::read> y_plane [[texture(0)]],
    texture2d<float, access::read> uv_plane [[texture(1)]],
    texture2d<float, access::write> y_output [[texture(2)]],
    texture2d<float, access::write> uv_output [[texture(3)]],
    constant TimelineParams& p [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.z, height = p.source_output_dims.w;
    if (gid.x >= width || gid.y >= height) return;
    float3 yuv = rgb_code_to_yuv420p10(
        sample_scaled_texture_pixel(gid, y_plane, uv_plane, p));
    y_output.write(float4(yuv.x / 1023.0f, 0.0f, 0.0f, 1.0f), gid);
    if ((gid.x & 1u) == 0u && (gid.y & 1u) == 0u) {
        uint2 p00 = gid;
        float3 c0 = rgb_code_to_yuv420p10(
            sample_scaled_texture_pixel(p00, y_plane, uv_plane, p));
        float3 c1 = rgb_code_to_yuv420p10(
            sample_scaled_texture_pixel(p00 + uint2(1u, 0u), y_plane, uv_plane, p));
        float3 c2 = rgb_code_to_yuv420p10(
            sample_scaled_texture_pixel(p00 + uint2(0u, 1u), y_plane, uv_plane, p));
        float3 c3 = rgb_code_to_yuv420p10(
            sample_scaled_texture_pixel(p00 + uint2(1u, 1u), y_plane, uv_plane, p));
        float2 chroma = (float2(c0.y, c0.z) + float2(c1.y, c1.z)
                       + float2(c2.y, c2.z) + float2(c3.y, c3.z))
                      / (4.0f * 1023.0f);
        uv_output.write(float4(chroma.x, chroma.y, 0.0f, 1.0f),
                        uint2(gid.x / 2u, gid.y / 2u));
    }
}

// T-P0-31: TOPOS 4:2:2 (three R16Uint planes) -> VideoToolbox 4:2:0 surface.
//
// Sampling contract (documented, matches the CPU reference in
// tests/media/test_topos_422_surface_kernels.py):
//   * Luma: 1:1, sampled through the shared planar pipeline
//     (range/matrix decode -> optional grade -> opacity -> 8-bit RGB code).
//   * Chroma: output (cx, cy) averages the four luma positions
//     (2cx,2cy) (2cx+1,2cy) (2cx,2cy+1) (2cx+1,2cy+1) AFTER encoding each
//     through the identical pipeline — the 4:2:2 source chroma is
//     horizontally co-sited, resolved by the planar sampler's bilinear
//     chroma fetch at each luma pixel's centre.
//   * Output signal: BT.709-limited NV12 (16-235 / 128±112) or P010
//     (64-940 / 512±448, 10-bit in the high bits of the surface sample) —
//     same surface contract as the NV12/P010-source kernels above.
//   * Odd output dimensions are rejected host-side before dispatch, so no
//     implicit odd-edge drop exists; source edges clamp inside the sampler.
kernel void timeline_fused_export_topos_texture_yuv420_surface(
    texture2d<ushort, access::read> y_plane [[texture(0)]],
    texture2d<ushort, access::read> u_plane [[texture(1)]],
    texture2d<ushort, access::read> v_plane [[texture(2)]],
    texture2d<float, access::write> y_output [[texture(3)]],
    texture2d<float, access::write> uv_output [[texture(4)]],
    constant TimelineParams& p [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.z, height = p.source_output_dims.w;
    if (gid.x >= width || gid.y >= height) return;
    float3 yuv = rgb_code_to_yuv420p8(
        sample_scaled_planar_texture_pixel(gid, y_plane, u_plane, v_plane, p));
    y_output.write(float4(yuv.x / 255.0f, 0.0f, 0.0f, 1.0f), gid);
    if ((gid.x & 1u) == 0u && (gid.y & 1u) == 0u) {
        uint2 p00 = gid;
        float3 c0 = rgb_code_to_yuv420p8(
            sample_scaled_planar_texture_pixel(p00, y_plane, u_plane, v_plane, p));
        float3 c1 = rgb_code_to_yuv420p8(
            sample_scaled_planar_texture_pixel(
                p00 + uint2(1u, 0u), y_plane, u_plane, v_plane, p));
        float3 c2 = rgb_code_to_yuv420p8(
            sample_scaled_planar_texture_pixel(
                p00 + uint2(0u, 1u), y_plane, u_plane, v_plane, p));
        float3 c3 = rgb_code_to_yuv420p8(
            sample_scaled_planar_texture_pixel(
                p00 + uint2(1u, 1u), y_plane, u_plane, v_plane, p));
        float2 chroma = (float2(c0.y, c0.z) + float2(c1.y, c1.z)
                       + float2(c2.y, c2.z) + float2(c3.y, c3.z))
                      / (4.0f * 255.0f);
        uv_output.write(float4(chroma.x, chroma.y, 0.0f, 1.0f),
                        uint2(gid.x / 2u, gid.y / 2u));
    }
}

kernel void timeline_fused_export_topos_texture_p010_surface(
    texture2d<ushort, access::read> y_plane [[texture(0)]],
    texture2d<ushort, access::read> u_plane [[texture(1)]],
    texture2d<ushort, access::read> v_plane [[texture(2)]],
    texture2d<float, access::write> y_output [[texture(3)]],
    texture2d<float, access::write> uv_output [[texture(4)]],
    constant TimelineParams& p [[buffer(0)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.z, height = p.source_output_dims.w;
    if (gid.x >= width || gid.y >= height) return;
    float3 yuv = rgb_code_to_yuv420p10(
        sample_scaled_planar_texture_pixel(gid, y_plane, u_plane, v_plane, p));
    y_output.write(float4(yuv.x / 1023.0f, 0.0f, 0.0f, 1.0f), gid);
    if ((gid.x & 1u) == 0u && (gid.y & 1u) == 0u) {
        uint2 p00 = gid;
        float3 c0 = rgb_code_to_yuv420p10(
            sample_scaled_planar_texture_pixel(p00, y_plane, u_plane, v_plane, p));
        float3 c1 = rgb_code_to_yuv420p10(
            sample_scaled_planar_texture_pixel(
                p00 + uint2(1u, 0u), y_plane, u_plane, v_plane, p));
        float3 c2 = rgb_code_to_yuv420p10(
            sample_scaled_planar_texture_pixel(
                p00 + uint2(0u, 1u), y_plane, u_plane, v_plane, p));
        float3 c3 = rgb_code_to_yuv420p10(
            sample_scaled_planar_texture_pixel(
                p00 + uint2(1u, 1u), y_plane, u_plane, v_plane, p));
        float2 chroma = (float2(c0.y, c0.z) + float2(c1.y, c1.z)
                       + float2(c2.y, c2.z) + float2(c3.y, c3.z))
                      / (4.0f * 1023.0f);
        uv_output.write(float4(chroma.x, chroma.y, 0.0f, 1.0f),
                        uint2(gid.x / 2u, gid.y / 2u));
    }
}

kernel void timeline_fused_export_texture_u16(
    texture2d<float, access::read> y_plane [[texture(0)]],
    texture2d<float, access::read> uv_plane [[texture(1)]],
    device ushort* output [[buffer(2)]],
    constant TimelineParams& p [[buffer(3)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.z, height = p.source_output_dims.w;
    if (gid.x >= width || gid.y >= height) return;
    float3 color = sample_scaled_texture_pixel(gid, y_plane, uv_plane, p);
    uint pixel = (gid.y * width + gid.x) * 3u;
    output[pixel] = to_u16(color.b * (65535.0f / 255.0f));
    output[pixel + 1u] = to_u16(color.g * (65535.0f / 255.0f));
    output[pixel + 2u] = to_u16(color.r * (65535.0f / 255.0f));
}

kernel void timeline_fused_export_texture_yuv422p10(
    texture2d<float, access::read> y_plane [[texture(0)]],
    texture2d<float, access::read> uv_plane [[texture(1)]],
    device ushort* output [[buffer(2)]],
    constant TimelineParams& p [[buffer(3)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.z, height = p.source_output_dims.w;
    if (gid.x >= width || gid.y >= height) return;
    float3 color = sample_scaled_texture_pixel(gid, y_plane, uv_plane, p);
    float3 yuv = rgb_code_to_yuv422p10(
        color, p.yuv.x, p.yuv.y, p.control.w > 0.5f);
    uint luma_size = width * height;
    uint chroma_width = width / 2u;
    uint chroma_size = chroma_width * height;
    uint index = gid.y * width + gid.x;
    output[index] = to_u16(yuv.x);
    if (gid.x < chroma_width) {
        uint2 p0 = uint2(gid.x * 2u, gid.y);
        float3 c0 = sample_scaled_texture_pixel(p0, y_plane, uv_plane, p);
        float3 c1 = sample_scaled_texture_pixel(
            p0 + uint2(1u, 0u), y_plane, uv_plane, p);
        float3 chroma = rgb_code_to_yuv422p10(
            (c0 + c1) * 0.5f, p.yuv.x, p.yuv.y, p.control.w > 0.5f);
        uint ci = gid.y * chroma_width + gid.x;
        output[luma_size + ci] = to_u16(chroma.y);
        output[luma_size + chroma_size + ci] = to_u16(chroma.z);
    }
}

kernel void timeline_materialize_source(
    device const uchar* bgr [[buffer(0)]],
    device const ushort* y_plane [[buffer(1)]],
    device const ushort* u_plane [[buffer(2)]],
    device const ushort* v_plane [[buffer(3)]],
    device uchar* output [[buffer(4)]],
    constant TimelineParams& p [[buffer(5)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.x;
    uint height = p.source_output_dims.y;
    if (gid.x >= width || gid.y >= height) return;
    float3 rgb = prepare_source_pixel_unfiltered(
        gid, bgr, y_plane, u_plane, v_plane, p);
    uint index = (gid.y * width + gid.x) * 3u;
    output[index] = to_u8(rgb.b);
    output[index + 1u] = to_u8(rgb.g);
    output[index + 2u] = to_u8(rgb.r);
}

kernel void timeline_materialize_texture(
    texture2d<float, access::read> y_plane [[texture(0)]],
    texture2d<float, access::read> uv_plane [[texture(1)]],
    device uchar* output [[buffer(2)]],
    constant TimelineParams& p [[buffer(3)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.x;
    uint height = p.source_output_dims.y;
    if (gid.x >= width || gid.y >= height) return;
    float3 rgb = prepare_texture_source_pixel_unfiltered(
        gid, y_plane, uv_plane, p);
    uint index = (gid.y * width + gid.x) * 3u;
    output[index] = to_u8(rgb.b);
    output[index + 1u] = to_u8(rgb.g);
    output[index + 2u] = to_u8(rgb.r);
}

kernel void timeline_materialize_texture_planar(
    texture2d<ushort, access::read> y_plane [[texture(0)]],
    texture2d<ushort, access::read> u_plane [[texture(1)]],
    texture2d<ushort, access::read> v_plane [[texture(2)]],
    device uchar* output [[buffer(2)]],
    constant TimelineParams& p [[buffer(3)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.x;
    uint height = p.source_output_dims.y;
    if (gid.x >= width || gid.y >= height) return;
    float3 rgb = prepare_planar_texture_source_pixel(
        gid, y_plane, u_plane, v_plane, p);
    uint index = (gid.y * width + gid.x) * 3u;
    output[index] = to_u8(rgb.b);
    output[index + 1u] = to_u8(rgb.g);
    output[index + 2u] = to_u8(rgb.r);
}

kernel void timeline_defocus_downsample(
    device const uchar* source [[buffer(0)]],
    device uchar* mip_atlas [[buffer(1)]],
    constant DefocusMipmapParams& p [[buffer(2)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint source_width = p.dims.x, source_height = p.dims.y;
    uint target_width = p.dims.z, target_height = p.dims.w;
    if (gid.x >= target_width || gid.y >= target_height) return;
    uint x0 = min(gid.x * 2u, source_width - 1u);
    uint x1 = min(x0 + 1u, source_width - 1u);
    uint y0 = min(gid.y * 2u, source_height - 1u);
    uint y1 = min(y0 + 1u, source_height - 1u);
    float3 sum = float3(0.0f);
    for (uint dy = 0u; dy < 2u; ++dy) {
        uint sy = dy == 0u ? y0 : y1;
        for (uint dx = 0u; dx < 2u; ++dx) {
            uint sx = dx == 0u ? x0 : x1;
            uint source_index = p.offsets.x + (sy * source_width + sx) * 3u;
            sum += float3(float(source[source_index + 2u]),
                          float(source[source_index + 1u]),
                          float(source[source_index]));
        }
    }
    uint target_index = p.offsets.y + (gid.y * target_width + gid.x) * 3u;
    mip_atlas[target_index] = to_u8(sum.b * 0.25f);
    mip_atlas[target_index + 1u] = to_u8(sum.g * 0.25f);
    mip_atlas[target_index + 2u] = to_u8(sum.r * 0.25f);
}

kernel void timeline_apply_effect_packed(
    device const uchar* bgr [[buffer(0)]],
    device uchar* output [[buffer(1)]],
    device const ushort* y_plane [[buffer(2)]],
    device const ushort* u_plane [[buffer(3)]],
    device const ushort* v_plane [[buffer(4)]],
    constant TimelineParams& p [[buffer(5)]],
    device const half4* horizontal [[buffer(6)]],
    device const float* weights_y [[buffer(7)]],
    device const float* weights_x [[buffer(8)]],
    device const uchar* mip_atlas [[buffer(9)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.x;
    uint height = p.source_output_dims.y;
    if (gid.x >= width || gid.y >= height) return;
    float3 rgb;
    uint effect_kind = uint(p.effect.x + 0.5f);
    if ((effect_kind != 6u && effect_kind != 7u && effect_kind != 8u && effect_kind != 9u
         && effect_kind != 10u && effect_kind != 11u
         && max(p.effect.y, p.effect.z) > 5.0f)
        || (effect_kind == 5u && max(p.effect.y, p.effect.z) > 1.0f)) {
        rgb = prepare_separable_effect_pixel(
            gid, bgr, y_plane, u_plane, v_plane, horizontal, weights_y, p);
    } else {
        rgb = apply_packed_effect_pixel(
            gid, bgr, weights_x, weights_y, mip_atlas, p);
    }
    uint index = (gid.y * width + gid.x) * 3u;
    output[index] = to_u8(rgb.b);
    output[index + 1u] = to_u8(rgb.g);
    output[index + 2u] = to_u8(rgb.r);
}

kernel void timeline_apply_bilateral_tiled_packed(
    device const uchar* bgr [[buffer(0)]],
    device uchar* output [[buffer(1)]],
    constant TimelineParams& p [[buffer(2)]],
    device const float* weights_x [[buffer(3)]],
    device const float* weights_y [[buffer(4)]],
    uint2 gid [[thread_position_in_grid]],
    uint2 local [[thread_position_in_threadgroup]],
    uint2 group [[threadgroup_position_in_grid]]) {
    constexpr uint TILE_SIZE = 36u;
    constexpr uint TILE_PIXELS = TILE_SIZE * TILE_SIZE;
    constexpr uint HALO = 10u;
    threadgroup uchar tile[TILE_PIXELS * 3u];
    uint local_index = local.y * 16u + local.x;
    uint width = p.source_output_dims.x;
    uint height = p.source_output_dims.y;
    for (uint tile_index = local_index; tile_index < TILE_PIXELS;
         tile_index += 256u) {
        int2 tile_position = int2(int(tile_index % TILE_SIZE),
                                  int(tile_index / TILE_SIZE));
        int2 source_position = int2(group * 16u) + tile_position
            - int2(HALO);
        int sx = effect_edge_coord(source_position.x, int(width), p);
        int sy = effect_edge_coord(source_position.y, int(height), p);
        uint source_index = (uint(sy) * width + uint(sx)) * 3u;
        uint tile_byte = tile_index * 3u;
        tile[tile_byte] = bgr[source_index];
        tile[tile_byte + 1u] = bgr[source_index + 1u];
        tile[tile_byte + 2u] = bgr[source_index + 2u];
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);
    if (gid.x >= width || gid.y >= height) return;

    int half_x = int(p.effect.y + 0.5f);
    int half_y = int(p.effect.z + 0.5f);
    uint center_x = local.x + HALO;
    uint center_y = local.y + HALO;
    uint center_tile_index = (center_y * TILE_SIZE + center_x) * 3u;
    float3 center_code = float3(
        float(tile[center_tile_index + 2u]),
        float(tile[center_tile_index + 1u]),
        float(tile[center_tile_index]));
    float3 center = center_code / 255.0f;
    float sigma_range = max(p.effect_sigma.y, 0.001f);
    float range_denominator = 2.0f * sigma_range * sigma_range;
    float3 sum = float3(0.0f);
    float weight_sum = 0.0f;
    for (int dy = -half_y; dy <= half_y; ++dy) {
        float weight_y = weights_y[dy + 32];
        for (int dx = -half_x; dx <= half_x; ++dx) {
            if (dx * dx + dy * dy > half_x * half_x) continue;
            uint sample_index = (
                (center_y + uint(dy)) * TILE_SIZE + center_x + uint(dx)) * 3u;
            float3 sample = float3(
                float(tile[sample_index + 2u]),
                float(tile[sample_index + 1u]),
                float(tile[sample_index])) / 255.0f;
            float range_distance = abs(center.r - sample.r)
                + abs(center.g - sample.g) + abs(center.b - sample.b);
            float weight = weights_x[dx + 32] * weight_y
                * exp(-(range_distance * range_distance) / range_denominator);
            sum += sample * weight;
            weight_sum += weight;
        }
    }
    float3 filtered = sum / max(weight_sum, 1.0e-4f);
    float3 result = mix(center, filtered, clamp(p.effect.w, 0.0f, 1.0f));
    if (p.effect_extra.x < 0.5f) result.r = center.r;
    if (p.effect_extra.y < 0.5f) result.g = center.g;
    if (p.effect_extra.z < 0.5f) result.b = center.b;
    result = clamp(result, 0.0f, 1.0f) * 255.0f;
    uint output_index = (gid.y * width + gid.x) * 3u;
    output[output_index] = to_u8(result.b);
    output[output_index + 1u] = to_u8(result.g);
    output[output_index + 2u] = to_u8(result.r);
}

kernel void timeline_apply_denoise_tiled_packed(
    device const uchar* bgr [[buffer(0)]],
    device uchar* output [[buffer(1)]],
    constant TimelineParams& p [[buffer(2)]],
    device const float* weights_x [[buffer(3)]],
    device const float* weights_y [[buffer(4)]],
    uint2 gid [[thread_position_in_grid]],
    uint2 local [[thread_position_in_threadgroup]],
    uint2 group [[threadgroup_position_in_grid]]) {
    constexpr uint TILE_SIZE = 36u;
    constexpr uint TILE_PIXELS = TILE_SIZE * TILE_SIZE;
    constexpr uint HALO = 10u;
    threadgroup uchar tile[TILE_PIXELS * 3u];
    uint local_index = local.y * 16u + local.x;
    uint width = p.source_output_dims.x;
    uint height = p.source_output_dims.y;
    for (uint tile_index = local_index; tile_index < TILE_PIXELS;
         tile_index += 256u) {
        int2 tile_position = int2(int(tile_index % TILE_SIZE),
                                  int(tile_index / TILE_SIZE));
        int2 source_position = int2(group * 16u) + tile_position
            - int2(HALO);
        int sx = effect_edge_coord(source_position.x, int(width), p);
        int sy = effect_edge_coord(source_position.y, int(height), p);
        uint source_index = (uint(sy) * width + uint(sx)) * 3u;
        uint tile_byte = tile_index * 3u;
        tile[tile_byte] = bgr[source_index];
        tile[tile_byte + 1u] = bgr[source_index + 1u];
        tile[tile_byte + 2u] = bgr[source_index + 2u];
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);
    if (gid.x >= width || gid.y >= height) return;

    int half_x = int(p.effect.y + 0.5f);
    int half_y = int(p.effect.z + 0.5f);
    uint center_x = local.x + HALO;
    uint center_y = local.y + HALO;
    uint center_tile_index = (center_y * TILE_SIZE + center_x) * 3u;
    float3 center = float3(
        float(tile[center_tile_index + 2u]),
        float(tile[center_tile_index + 1u]),
        float(tile[center_tile_index])) / 255.0f;
    float center_luma = denoise_luma(center);
    float3 center_chroma = center - center_luma;
    float sigma_range = max(p.effect_sigma.y, 0.1f);
    float range_denominator = 2.0f * sigma_range * sigma_range;
    bool luminance_only = p.effect_extra.x > 0.5f;
    bool chroma_only = p.effect_extra.y > 0.5f;
    float3 sum = float3(0.0f);
    float weight_sum = 0.0f;
    for (int dy = -half_y; dy <= half_y; ++dy) {
        float weight_y = weights_y[dy + 32];
        for (int dx = -half_x; dx <= half_x; ++dx) {
            uint sample_index = (
                (center_y + uint(dy)) * TILE_SIZE + center_x + uint(dx)) * 3u;
            float3 sample = float3(
                float(tile[sample_index + 2u]),
                float(tile[sample_index + 1u]),
                float(tile[sample_index])) / 255.0f;
            float range_diff_sq;
            if (luminance_only) {
                float difference = center_luma - denoise_luma(sample);
                range_diff_sq = difference * difference;
            } else if (chroma_only) {
                float3 sample_chroma = sample - denoise_luma(sample);
                float3 difference = center_chroma - sample_chroma;
                range_diff_sq = dot(difference, difference);
            } else {
                float3 difference = center - sample;
                range_diff_sq = dot(difference, difference);
            }
            float weight = weights_x[dx + 32] * weight_y
                * exp(-range_diff_sq / range_denominator);
            sum += sample * weight;
            weight_sum += weight;
        }
    }
    float3 filtered = sum / max(weight_sum, 1.0e-4f);
    float strength = clamp(p.effect.w, 0.0f, 1.0f);
    float3 result = mix(center, filtered, strength);
    if (luminance_only) {
        float result_luma = mix(center_luma, denoise_luma(filtered), strength);
        result = result_luma + center_chroma;
    } else if (chroma_only) {
        float3 filtered_chroma = filtered - denoise_luma(filtered);
        result = center_luma + mix(center_chroma, filtered_chroma, strength);
    }
    result = clamp(result, 0.0f, 1.0f) * 255.0f;
    uint output_index = (gid.y * width + gid.x) * 3u;
    output[output_index] = to_u8(result.b);
    output[output_index + 1u] = to_u8(result.g);
    output[output_index + 2u] = to_u8(result.r);
}

struct LayerBlendParams {
    uint4 dims;       // output width, height
    uint4 options;    // timeline clip blend mode code
    float4 control;   // opacity, first-layer, transform-enabled, crop-enabled
    float4 inverse_x; // inverse affine: x = x*x + y*y + translation
    float4 inverse_y; // inverse affine: y = x*x + y*y + translation
    float4 crop;      // normalized left, right, top, bottom
    float4 geometry;  // source/output crop feather, reserved, reserved
};

float crop_smooth(float distance, float feather) {
    float t = clamp(distance / feather, 0.0f, 1.0f);
    return t * t * (3.0f - 2.0f * t);
}

float crop_matte_pixel(int2 pixel, uint2 dims,
                       constant LayerBlendParams& p) {
    if (pixel.x < 0 || pixel.y < 0
        || pixel.x >= int(dims.x) || pixel.y >= int(dims.y)) return 0.0f;
    if (p.control.w < 0.5f) return 1.0f;
    float feather = max(p.geometry.x, 0.0f);
    float2 center = float2(pixel) + 0.5f;
    float2 low = float2(p.crop.x * float(dims.x),
                        p.crop.z * float(dims.y));
    float2 high = float2((1.0f - p.crop.y) * float(dims.x),
                         (1.0f - p.crop.w) * float(dims.y));
    if (feather > 0.5f) {
        return crop_smooth(center.x - low.x, feather)
            * crop_smooth(high.x - center.x, feather)
            * crop_smooth(center.y - low.y, feather)
            * crop_smooth(high.y - center.y, feather);
    }
    int2 first = int2(ceil(low));
    int2 end = int2(floor(high));
    return (pixel.x >= first.x && pixel.y >= first.y
            && pixel.x < end.x && pixel.y < end.y) ? 1.0f : 0.0f;
}

float crop_matte_resized(uint2 pixel, constant TimelineParams& source,
                         constant LayerBlendParams& blend) {
    uint2 source_dims = source.source_output_dims.xy;
    uint2 output_dims = source.source_output_dims.zw;
    float2 pos = (float2(pixel) + 0.5f) * float2(source_dims)
        / float2(output_dims) - 0.5f;
    int2 p0 = int2(floor(pos));
    float2 f = pos - float2(p0);
    int2 hi = int2(source_dims) - 1;
    int2 a = clamp(p0, int2(0), hi);
    int2 b = clamp(p0 + 1, int2(0), hi);
    float c00 = crop_matte_pixel(a, source_dims, blend);
    float c10 = crop_matte_pixel(int2(b.x, a.y), source_dims, blend);
    float c01 = crop_matte_pixel(int2(a.x, b.y), source_dims, blend);
    float c11 = crop_matte_pixel(b, source_dims, blend);
    return mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y);
}

float2 inverse_layer_position(uint2 pixel,
                              constant LayerBlendParams& blend) {
    if (blend.control.z < 0.5f) return float2(pixel);
    float2 p = float2(pixel);
    return float2(dot(blend.inverse_x.xyz, float3(p, 1.0f)),
                  dot(blend.inverse_y.xyz, float3(p, 1.0f)));
}

float blend_luma(float3 color) {
    return dot(color, float3(0.3f, 0.59f, 0.11f));
}

float3 blend_clip_color(float3 color) {
    float luma = blend_luma(color);
    float minimum = min(color.r, min(color.g, color.b));
    float maximum = max(color.r, max(color.g, color.b));
    float3 low = minimum < 0.0f
        ? (color - luma) * luma / max(luma - minimum, 1.0e-6f)
        : color;
    // Keep the same historical formula used by the CPU blend implementation.
    return maximum > 1.0f
        ? (color - luma) * (1.0f - luma) / max(maximum - luma, 1.0e-6f)
        : low;
}

float3 blend_set_luma(float3 color, float luma) {
    float3 shifted = color + (luma - blend_luma(color));
    return blend_clip_color(shifted);
}

float3 blend_set_saturation(float3 color, float saturation) {
    float minimum = min(color.r, min(color.g, color.b));
    float maximum = max(color.r, max(color.g, color.b));
    if (maximum <= minimum) return float3(0.0f);
    float middle = color.r + color.g + color.b - maximum - minimum;
    float mapped_middle = (middle - minimum) * saturation
        / max(maximum - minimum, 1.0e-6f);
    if (color.r <= color.g && color.r <= color.b) {
        return color.g <= color.b
            ? float3(0.0f, mapped_middle, saturation)
            : float3(0.0f, saturation, mapped_middle);
    }
    if (color.g <= color.r && color.g <= color.b) {
        return color.r <= color.b
            ? float3(mapped_middle, 0.0f, saturation)
            : float3(saturation, 0.0f, mapped_middle);
    }
    return color.r <= color.g
        ? float3(mapped_middle, saturation, 0.0f)
        : float3(saturation, mapped_middle, 0.0f);
}

float3 apply_layer_blend(uint mode, float3 base_rgb, float3 source_rgb) {
    if (mode == 0u) return source_rgb;
    float3 base = base_rgb;
    float3 source = source_rgb;
    float3 result = source;
    if (mode == 1u) result = source * base;
    else if (mode == 2u) result = source + base - source * base;
    else if (mode == 3u) {
        result = select(2.0f * source * base,
            1.0f - 2.0f * (1.0f - source) * (1.0f - base),
            base > 0.5f);
    } else if (mode == 4u) result = min(source, base);
    else if (mode == 5u) result = max(source, base);
    else if (mode == 6u) {
        result = select(min(1.0f, base / max(1.0f - source, 1.0e-6f)),
                        1.0f, source >= 1.0f - 1.0e-6f);
    } else if (mode == 7u) {
        result = select(max(0.0f, 1.0f - (1.0f - base)
                            / max(source, 1.0e-6f)),
                        0.0f, source <= 1.0e-6f);
    } else if (mode == 8u) {
        result = select(2.0f * source * base,
            1.0f - 2.0f * (1.0f - source) * (1.0f - base),
            source > 0.5f);
    } else if (mode == 9u) {
        float3 g = select(sqrt(clamp(base, 0.0f, 1.0f)),
            ((16.0f * base - 12.0f) * base + 4.0f) * base,
            base <= 0.25f);
        result = select(base + (2.0f * source - 1.0f) * (g - base),
            base - (1.0f - 2.0f * source) * base * (1.0f - base),
            source <= 0.5f);
    } else if (mode == 10u) result = abs(base - source);
    else if (mode == 11u) result = source + base - 2.0f * source * base;
    else if (mode == 12u) {
        float luma = blend_luma(base);
        result = blend_set_luma(
            blend_set_saturation(source,
                max(base.r, max(base.g, base.b))
                - min(base.r, min(base.g, base.b))), luma);
    } else if (mode == 13u) {
        float source_saturation = max(source.r, max(source.g, source.b))
            - min(source.r, min(source.g, source.b));
        result = blend_set_luma(
            blend_set_saturation(base, source_saturation), blend_luma(base));
    } else if (mode == 14u) {
        result = blend_set_luma(source, blend_luma(base));
    } else if (mode == 15u) {
        result = blend_set_luma(base, blend_luma(source));
    }
    return clamp(result, 0.0f, 1.0f);
}

float crop_matte_transformed(float2 position, uint2 output_dims,
                             constant TimelineParams& source,
                             constant LayerBlendParams& blend) {
    int2 p0 = int2(floor(position));
    float2 f = position - float2(p0);
    int2 p1 = p0 + 1;
    int2 hi = int2(output_dims) - 1;
    float c00 = all(p0 >= 0) && all(p0 <= hi)
        ? crop_matte_resized(uint2(p0), source, blend) : 0.0f;
    float c10 = p1.x >= 0 && p1.x <= hi.x && p0.y >= 0 && p0.y <= hi.y
        ? crop_matte_resized(uint2(p1.x, p0.y), source, blend) : 0.0f;
    float c01 = p0.x >= 0 && p0.x <= hi.x && p1.y >= 0 && p1.y <= hi.y
        ? crop_matte_resized(uint2(p0.x, p1.y), source, blend) : 0.0f;
    float c11 = all(p1 >= 0) && all(p1 <= hi)
        ? crop_matte_resized(uint2(p1), source, blend) : 0.0f;
    float warped = mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y);
    return warped;
}

float3 sample_layer_packed_rgb(
    float2 position, device const uchar* bgr,
    device const ushort* y_plane, device const ushort* u_plane,
    device const ushort* v_plane, constant TimelineParams& source) {
    int2 p0 = int2(floor(position));
    float2 f = position - float2(p0);
    int2 p1 = p0 + 1;
    int2 hi = int2(source.source_output_dims.zw) - 1;
    float3 c00 = all(p0 >= 0) && all(p0 <= hi)
        ? sample_scaled_pixel(uint2(p0), bgr, y_plane, u_plane, v_plane, source)
        : float3(0.0f);
    float3 c10 = p1.x >= 0 && p1.x <= hi.x && p0.y >= 0 && p0.y <= hi.y
        ? sample_scaled_pixel(uint2(p1.x, p0.y), bgr, y_plane, u_plane, v_plane, source)
        : float3(0.0f);
    float3 c01 = p0.x >= 0 && p0.x <= hi.x && p1.y >= 0 && p1.y <= hi.y
        ? sample_scaled_pixel(uint2(p0.x, p1.y), bgr, y_plane, u_plane, v_plane, source)
        : float3(0.0f);
    float3 c11 = all(p1 >= 0) && all(p1 <= hi)
        ? sample_scaled_pixel(uint2(p1), bgr, y_plane, u_plane, v_plane, source)
        : float3(0.0f);
    return mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y);
}

float3 sample_layer_packed_effect_rgb(
    float2 position, device const uchar* bgr,
    device const ushort* y_plane, device const ushort* u_plane,
    device const ushort* v_plane, device const half4* horizontal,
    device const float* weights_y, constant TimelineParams& source) {
    int2 p0 = int2(floor(position));
    float2 f = position - float2(p0);
    int2 p1 = p0 + 1;
    int2 hi = int2(source.source_output_dims.zw) - 1;
    float3 c00 = all(p0 >= 0) && all(p0 <= hi)
        ? sample_scaled_separable_effect_pixel(uint2(p0), bgr, y_plane, u_plane,
            v_plane, horizontal, weights_y, source) : float3(0.0f);
    float3 c10 = p1.x >= 0 && p1.x <= hi.x && p0.y >= 0 && p0.y <= hi.y
        ? sample_scaled_separable_effect_pixel(uint2(p1.x, p0.y), bgr, y_plane,
            u_plane, v_plane, horizontal, weights_y, source) : float3(0.0f);
    float3 c01 = p0.x >= 0 && p0.x <= hi.x && p1.y >= 0 && p1.y <= hi.y
        ? sample_scaled_separable_effect_pixel(uint2(p0.x, p1.y), bgr, y_plane,
            u_plane, v_plane, horizontal, weights_y, source) : float3(0.0f);
    float3 c11 = all(p1 >= 0) && all(p1 <= hi)
        ? sample_scaled_separable_effect_pixel(uint2(p1), bgr, y_plane, u_plane,
            v_plane, horizontal, weights_y, source) : float3(0.0f);
    return mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y);
}

float3 sample_layer_texture_rgb(
    float2 position, texture2d<float, access::read> y_plane,
    texture2d<float, access::read> uv_plane,
    constant TimelineParams& source) {
    int2 p0 = int2(floor(position));
    float2 f = position - float2(p0);
    int2 p1 = p0 + 1;
    int2 hi = int2(source.source_output_dims.zw) - 1;
    float3 c00 = all(p0 >= 0) && all(p0 <= hi)
        ? sample_scaled_texture_pixel(uint2(p0), y_plane, uv_plane, source)
        : float3(0.0f);
    float3 c10 = p1.x >= 0 && p1.x <= hi.x && p0.y >= 0 && p0.y <= hi.y
        ? sample_scaled_texture_pixel(uint2(p1.x, p0.y), y_plane, uv_plane, source)
        : float3(0.0f);
    float3 c01 = p0.x >= 0 && p0.x <= hi.x && p1.y >= 0 && p1.y <= hi.y
        ? sample_scaled_texture_pixel(uint2(p0.x, p1.y), y_plane, uv_plane, source)
        : float3(0.0f);
    float3 c11 = all(p1 >= 0) && all(p1 <= hi)
        ? sample_scaled_texture_pixel(uint2(p1), y_plane, uv_plane, source)
        : float3(0.0f);
    return mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y);
}

float3 sample_layer_planar_rgb(
    float2 position,
    texture2d<ushort, access::read> y_plane,
    texture2d<ushort, access::read> u_plane,
    texture2d<ushort, access::read> v_plane,
    constant TimelineParams& source) {
    int2 p0 = int2(floor(position));
    float2 f = position - float2(p0);
    int2 p1 = p0 + 1;
    int2 hi = int2(source.source_output_dims.zw) - 1;
    float3 c00 = all(p0 >= 0) && all(p0 <= hi)
        ? sample_scaled_planar_texture_pixel(
            uint2(p0), y_plane, u_plane, v_plane, source)
        : float3(0.0f);
    float3 c10 = p1.x >= 0 && p1.x <= hi.x && p0.y >= 0 && p0.y <= hi.y
        ? sample_scaled_planar_texture_pixel(
            uint2(p1.x, p0.y), y_plane, u_plane, v_plane, source)
        : float3(0.0f);
    float3 c01 = p0.x >= 0 && p0.x <= hi.x && p1.y >= 0 && p1.y <= hi.y
        ? sample_scaled_planar_texture_pixel(
            uint2(p0.x, p1.y), y_plane, u_plane, v_plane, source)
        : float3(0.0f);
    float3 c11 = all(p1 >= 0) && all(p1 <= hi)
        ? sample_scaled_planar_texture_pixel(
            uint2(p1), y_plane, u_plane, v_plane, source)
        : float3(0.0f);
    return mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y);
}

float3 sample_layer_texture_effect_rgb(
    float2 position, texture2d<float, access::read> y_plane,
    texture2d<float, access::read> uv_plane,
    device const half4* horizontal, device const float* weights_y,
    constant TimelineParams& source) {
    int2 p0 = int2(floor(position));
    float2 f = position - float2(p0);
    int2 p1 = p0 + 1;
    int2 hi = int2(source.source_output_dims.zw) - 1;
    float3 c00 = all(p0 >= 0) && all(p0 <= hi)
        ? sample_scaled_separable_texture_effect_pixel(
            uint2(p0), y_plane, uv_plane, horizontal, weights_y, source)
        : float3(0.0f);
    float3 c10 = p1.x >= 0 && p1.x <= hi.x && p0.y >= 0 && p0.y <= hi.y
        ? sample_scaled_separable_texture_effect_pixel(
            uint2(p1.x, p0.y), y_plane, uv_plane, horizontal, weights_y, source)
        : float3(0.0f);
    float3 c01 = p0.x >= 0 && p0.x <= hi.x && p1.y >= 0 && p1.y <= hi.y
        ? sample_scaled_separable_texture_effect_pixel(
            uint2(p0.x, p1.y), y_plane, uv_plane, horizontal, weights_y, source)
        : float3(0.0f);
    float3 c11 = all(p1 >= 0) && all(p1 <= hi)
        ? sample_scaled_separable_texture_effect_pixel(
            uint2(p1), y_plane, uv_plane, horizontal, weights_y, source)
        : float3(0.0f);
    return mix(mix(c00, c10, f.x), mix(c01, c11, f.x), f.y);
}

kernel void timeline_effect_horizontal(
    device const uchar* bgr [[buffer(0)]],
    device const ushort* y_plane [[buffer(1)]],
    device const ushort* u_plane [[buffer(2)]],
    device const ushort* v_plane [[buffer(3)]],
    device half4* horizontal [[buffer(4)]],
    constant TimelineParams& p [[buffer(5)]],
    device const float* weights_x [[buffer(6)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.x, height = p.source_output_dims.y;
    if (gid.x >= width || gid.y >= height) return;
    int half_x = int(p.effect.y + 0.5f);
    uint kind = uint(p.effect.x + 0.5f);
    float3 filtered = kind == 5u
        ? prepare_source_pixel_unfiltered(
            uint2(gid.x, gid.y), bgr, y_plane, u_plane, v_plane, p)
        : float3(0.0f);
    for (int dx = -half_x; dx <= half_x; ++dx) {
        int sx = effect_edge_coord(int(gid.x) + dx, int(width), p);
        float3 sample = prepare_source_pixel_unfiltered(
            uint2(uint(sx), gid.y), bgr, y_plane, u_plane, v_plane, p);
        if (kind == 5u) {
            filtered = fold_morphology_pixel(filtered, sample, p);
        } else {
            filtered += effect_filter_input(sample, p) * weights_x[dx + 32];
        }
    }
    uint index = gid.y * width + gid.x;
    horizontal[index] = half4(half3(filtered), half(0.0f));
}

kernel void timeline_effect_horizontal_texture(
    texture2d<float, access::read> y_plane [[texture(0)]],
    texture2d<float, access::read> uv_plane [[texture(1)]],
    device half4* horizontal [[buffer(4)]],
    constant TimelineParams& p [[buffer(5)]],
    device const float* weights_x [[buffer(6)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.source_output_dims.x, height = p.source_output_dims.y;
    if (gid.x >= width || gid.y >= height) return;
    int half_x = int(p.effect.y + 0.5f);
    uint kind = uint(p.effect.x + 0.5f);
    float3 filtered = kind == 5u
        ? prepare_texture_source_pixel_unfiltered(
            uint2(gid.x, gid.y), y_plane, uv_plane, p)
        : float3(0.0f);
    for (int dx = -half_x; dx <= half_x; ++dx) {
        int sx = effect_edge_coord(int(gid.x) + dx, int(width), p);
        float3 sample = prepare_texture_source_pixel_unfiltered(
            uint2(uint(sx), gid.y), y_plane, uv_plane, p);
        if (kind == 5u) {
            filtered = fold_morphology_pixel(filtered, sample, p);
        } else {
            filtered += effect_filter_input(sample, p) * weights_x[dx + 32];
        }
    }
    uint index = gid.y * width + gid.x;
    horizontal[index] = half4(half3(filtered), half(0.0f));
}

kernel void timeline_stack_layer(
    device const uchar* bgr [[buffer(0)]],
    device const ushort* y_plane [[buffer(1)]],
    device const ushort* u_plane [[buffer(2)]],
    device const ushort* v_plane [[buffer(3)]],
    device const float4* accumulator [[buffer(4)]],
    device float4* output [[buffer(5)]],
    constant TimelineParams& source_params [[buffer(6)]],
    constant LayerBlendParams& blend_params [[buffer(7)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = blend_params.dims.x, height = blend_params.dims.y;
    if (gid.x >= width || gid.y >= height) return;
    uint index = gid.y * width + gid.x;
    float opacity = clamp(blend_params.control.x, 0.0f, 1.0f);
    float2 sample_position = inverse_layer_position(gid, blend_params);
    float3 foreground = sample_layer_packed_rgb(
        sample_position, bgr, y_plane, u_plane, v_plane, source_params) / 255.0f;
    if (blend_params.control.w > 0.5f) {
        float source_alpha = crop_matte_transformed(
            sample_position, blend_params.dims.xy, source_params, blend_params);
        float output_alpha = crop_matte_pixel(
            int2(gid), blend_params.dims.xy, blend_params);
        opacity *= source_alpha * output_alpha;
    }
    if (blend_params.control.y > 0.5f) {
        output[index] = float4(foreground * opacity, opacity);
    } else {
        float4 base = accumulator[index];
        float3 blended = apply_layer_blend(
            blend_params.options.x, base.rgb, foreground);
        output[index] = float4(mix(base.rgb, blended, opacity),
                               opacity + base.a * (1.0f - opacity));
    }
}

kernel void timeline_stack_layer_texture(
    texture2d<float, access::read> y_plane [[texture(0)]],
    texture2d<float, access::read> uv_plane [[texture(1)]],
    device const float4* accumulator [[buffer(4)]],
    device float4* output [[buffer(5)]],
    constant TimelineParams& source_params [[buffer(6)]],
    constant LayerBlendParams& blend_params [[buffer(7)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = blend_params.dims.x, height = blend_params.dims.y;
    if (gid.x >= width || gid.y >= height) return;
    uint index = gid.y * width + gid.x;
    float opacity = clamp(blend_params.control.x, 0.0f, 1.0f);
    float2 sample_position = inverse_layer_position(gid, blend_params);
    float3 foreground = sample_layer_texture_rgb(
        sample_position, y_plane, uv_plane, source_params) / 255.0f;
    if (blend_params.control.w > 0.5f) {
        float source_alpha = crop_matte_transformed(
            sample_position, blend_params.dims.xy, source_params, blend_params);
        float output_alpha = crop_matte_pixel(
            int2(gid), blend_params.dims.xy, blend_params);
        opacity *= source_alpha * output_alpha;
    }
    if (blend_params.control.y > 0.5f) {
        output[index] = float4(foreground * opacity, opacity);
    } else {
        float4 base = accumulator[index];
        float3 blended = apply_layer_blend(
            blend_params.options.x, base.rgb, foreground);
        output[index] = float4(mix(base.rgb, blended, opacity),
                               opacity + base.a * (1.0f - opacity));
    }
}

kernel void timeline_stack_layer_texture_planar(
    texture2d<ushort, access::read> y_plane [[texture(0)]],
    texture2d<ushort, access::read> u_plane [[texture(1)]],
    texture2d<ushort, access::read> v_plane [[texture(2)]],
    device const float4* accumulator [[buffer(4)]],
    device float4* output [[buffer(5)]],
    constant TimelineParams& source_params [[buffer(6)]],
    constant LayerBlendParams& blend_params [[buffer(7)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = blend_params.dims.x, height = blend_params.dims.y;
    if (gid.x >= width || gid.y >= height) return;
    uint index = gid.y * width + gid.x;
    float opacity = clamp(blend_params.control.x, 0.0f, 1.0f);
    float2 sample_position = inverse_layer_position(gid, blend_params);
    float3 foreground = sample_layer_planar_rgb(
        sample_position, y_plane, u_plane, v_plane, source_params) / 255.0f;
    if (blend_params.control.w > 0.5f) {
        float source_alpha = crop_matte_transformed(
            sample_position, blend_params.dims.xy, source_params, blend_params);
        float output_alpha = crop_matte_pixel(
            int2(gid), blend_params.dims.xy, blend_params);
        opacity *= source_alpha * output_alpha;
    }
    if (blend_params.control.y > 0.5f) {
        output[index] = float4(foreground * opacity, opacity);
    } else {
        float4 base = accumulator[index];
        float3 blended = apply_layer_blend(
            blend_params.options.x, base.rgb, foreground);
        output[index] = float4(mix(base.rgb, blended, opacity),
                               opacity + base.a * (1.0f - opacity));
    }
}

kernel void timeline_stack_layer_effect(
    device const uchar* bgr [[buffer(0)]],
    device const ushort* y_plane [[buffer(1)]],
    device const ushort* u_plane [[buffer(2)]],
    device const ushort* v_plane [[buffer(3)]],
    device const float4* accumulator [[buffer(4)]],
    device float4* output [[buffer(5)]],
    constant TimelineParams& source_params [[buffer(6)]],
    constant LayerBlendParams& blend_params [[buffer(7)]],
    device const half4* horizontal [[buffer(8)]],
    device const float* weights_y [[buffer(9)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = blend_params.dims.x, height = blend_params.dims.y;
    if (gid.x >= width || gid.y >= height) return;
    uint index = gid.y * width + gid.x;
    float opacity = clamp(blend_params.control.x, 0.0f, 1.0f);
    float2 sample_position = inverse_layer_position(gid, blend_params);
    float3 foreground = sample_layer_packed_effect_rgb(
        sample_position, bgr, y_plane, u_plane, v_plane, horizontal,
        weights_y, source_params) / 255.0f;
    if (blend_params.control.w > 0.5f) {
        float source_alpha = crop_matte_transformed(
            sample_position, blend_params.dims.xy, source_params, blend_params);
        float output_alpha = crop_matte_pixel(
            int2(gid), blend_params.dims.xy, blend_params);
        opacity *= source_alpha * output_alpha;
    }
    if (blend_params.control.y > 0.5f) {
        output[index] = float4(foreground * opacity, opacity);
    } else {
        float4 base = accumulator[index];
        float3 blended = apply_layer_blend(
            blend_params.options.x, base.rgb, foreground);
        output[index] = float4(mix(base.rgb, blended, opacity),
                               opacity + base.a * (1.0f - opacity));
    }
}

kernel void timeline_stack_layer_texture_effect(
    texture2d<float, access::read> y_plane [[texture(0)]],
    texture2d<float, access::read> uv_plane [[texture(1)]],
    device const float4* accumulator [[buffer(4)]],
    device float4* output [[buffer(5)]],
    constant TimelineParams& source_params [[buffer(6)]],
    constant LayerBlendParams& blend_params [[buffer(7)]],
    device const half4* horizontal [[buffer(8)]],
    device const float* weights_y [[buffer(9)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = blend_params.dims.x, height = blend_params.dims.y;
    if (gid.x >= width || gid.y >= height) return;
    uint index = gid.y * width + gid.x;
    float opacity = clamp(blend_params.control.x, 0.0f, 1.0f);
    float2 sample_position = inverse_layer_position(gid, blend_params);
    float3 foreground = sample_layer_texture_effect_rgb(
        sample_position, y_plane, uv_plane, horizontal, weights_y,
        source_params) / 255.0f;
    if (blend_params.control.w > 0.5f) {
        float source_alpha = crop_matte_transformed(
            sample_position, blend_params.dims.xy, source_params, blend_params);
        float output_alpha = crop_matte_pixel(
            int2(gid), blend_params.dims.xy, blend_params);
        opacity *= source_alpha * output_alpha;
    }
    if (blend_params.control.y > 0.5f) {
        output[index] = float4(foreground * opacity, opacity);
    } else {
        float4 base = accumulator[index];
        float3 blended = apply_layer_blend(
            blend_params.options.x, base.rgb, foreground);
        output[index] = float4(mix(base.rgb, blended, opacity),
                               opacity + base.a * (1.0f - opacity));
    }
}

struct WipeBlendParams {
    uint4 dims;      // output width, height
    float4 control;  // element opacity, left opacity, right opacity, first
    float4 transition; // mode, progress, reserved, reserved
};

struct TransitionMaskParams {
    uint4 dims;
    float4 control;  // progress, angle, softness, reverse
    uint4 options;   // kind, direction, vertex count, effect mode
    float2 vertices[48];
    float4 effect_control; // radius/ratio, mix, shutter, strength
    float4 effect_extra;   // spin angle, split amount, split mix, mode
};

float procedural_wipe_coord(uint2 gid, constant TransitionMaskParams& p) {
    float width = float(max(p.dims.x, 1u));
    float height = float(max(p.dims.y, 1u));
    float x = float(gid.x) / max(width - 1.0f, 1.0f);
    float y = float(gid.y) / max(height - 1.0f, 1.0f);
    uint direction = p.options.y;
    if (direction == 0u) return x;
    if (direction == 1u) return 1.0f - x;
    if (direction == 2u) return y;
    if (direction == 3u) return 1.0f - y;
    float theta = p.control.y * 0.0174532925199433f;
    float cx = (float(gid.x) - (width - 1.0f) * 0.5f) * cos(theta);
    float cy = (float(gid.y) - (height - 1.0f) * 0.5f) * sin(theta);
    float min_projection = -abs((width - 1.0f) * 0.5f * cos(theta))
        - abs((height - 1.0f) * 0.5f * sin(theta));
    float max_projection = -min_projection;
    return (cx + cy - min_projection)
        / max(max_projection - min_projection, 1.0e-6f);
}

bool procedural_polygon_inside(float2 point, constant TransitionMaskParams& p) {
    bool inside = false;
    uint count = min(p.options.z, 48u);
    for (uint i = 0u; i < 48u; ++i) {
        if (i >= count) continue;
        uint next = (i + 1u) % max(count, 1u);
        float2 a = p.vertices[i] * p.control.x;
        float2 b = p.vertices[next] * p.control.x;
        bool crosses = (a.y > point.y) != (b.y > point.y);
        float denominator = b.y - a.y;
        float safe_denominator = abs(denominator) > 1.0e-8f
            ? denominator : 1.0e-8f;
        float edge_x = (b.x - a.x) * (point.y - a.y)
            / safe_denominator + a.x;
        if (crosses && point.x < edge_x) inside = !inside;
    }
    return inside;
}

kernel void timeline_transition_mask(
    device float* output [[buffer(0)]],
    constant TransitionMaskParams& p [[buffer(1)]],
    uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= p.dims.x || gid.y >= p.dims.y) return;
    uint index = gid.y * p.dims.x + gid.x;
    float progress = clamp(p.control.x, 0.0f, 1.0f);
    if (p.control.w > 0.5f) progress = 1.0f - progress;
    if (progress <= 0.0f) { output[index] = 0.0f; return; }
    if (progress >= 1.0f) { output[index] = 1.0f; return; }

    uint kind = p.options.x;
    if (kind == 4u) {
        output[index] = progress * progress * (3.0f - 2.0f * progress);
        return;
    }
    float mask = 0.0f;
    if (kind == 0u) {
        float coord = procedural_wipe_coord(gid, p);
        float feather = max(1.0f, p.control.z
            * float(max(p.dims.x, p.dims.y)));
        float feather_norm = feather / float(max(p.dims.x, p.dims.y));
        float t = (progress + feather_norm - coord)
            / max(2.0f * feather_norm, 1.0e-6f);
        t = clamp(t, 0.0f, 1.0f);
        mask = t * t * (3.0f - 2.0f * t);
    } else if (kind == 1u || kind == 2u) {
        float width = float(max(p.dims.x, 1u));
        float height = float(max(p.dims.y, 1u));
        float2 point = (float2(float(gid.x) + 0.5f,
                               float(gid.y) + 0.5f)
                        / float2(width, height)) * 2.0f - 1.0f;
        point.x *= width / height;
        float radius = 1.55f * progress;
        float value = kind == 2u
            ? (point.x * point.x / max(radius * radius, 1.0e-6f)
               + point.y * point.y / max(radius * 0.62f * radius * 0.62f, 1.0e-6f))
            : (point.x * point.x + point.y * point.y)
              / max(radius * radius, 1.0e-6f);
        mask = value <= 1.0f ? 1.0f : 0.0f;
    } else {
        float width = float(max(p.dims.x, 1u));
        float height = float(max(p.dims.y, 1u));
        float2 point = (float2(float(gid.x) + 0.5f,
                               float(gid.y) + 0.5f)
                        / float2(width, height)) * 2.0f - 1.0f;
        point.x *= width / height;
        mask = procedural_polygon_inside(point, p) ? 1.0f : 0.0f;
    }
    output[index] = mask;
}

uint transition_sample_index(float2 point, uint width, uint height) {
    int x = clamp(int(floor(point.x)), 0, int(max(width, 1u)) - 1);
    int y = clamp(int(floor(point.y)), 0, int(max(height, 1u)) - 1);
    return uint(y) * width + uint(x);
}

float3 transition_sample(
    device const float4* image, float2 point, uint width, uint height) {
    return image[transition_sample_index(point, width, height)].rgb;
}

float3 transition_directional_blur(
    device const float4* image,
    float2 point,
    float2 axis,
    float radius,
    uint width,
    uint height) {
    float3 result = float3(0.0f);
    for (int tap = -3; tap <= 3; ++tap) {
        float amount = float(tap) / 3.0f;
        result += transition_sample(
            image, point + axis * radius * amount, width, height) / 7.0f;
    }
    return result;
}

float2 transition_rotate(float2 point, float radians) {
    float c = cos(radians);
    float s = sin(radians);
    return float2(point.x * c - point.y * s,
                  point.x * s + point.y * c);
}

float3 transition_radial_blur(
    device const float4* image,
    float2 point,
    bool spin,
    float strength,
    uint width,
    uint height) {
    float2 centre = (float2(float(width), float(height)) - 1.0f) * 0.5f;
    float2 delta = point - centre;
    float3 result = float3(0.0f);
    for (int tap = -3; tap <= 3; ++tap) {
        float amount = float(tap) / 3.0f;
        float2 sample_delta = spin
            ? transition_rotate(delta, amount * strength)
            : delta * (1.0f + amount * strength);
        result += transition_sample(
            image, centre + sample_delta, width, height) / 7.0f;
    }
    return result;
}

float3 transition_source_effect(
    device const float4* image,
    float2 point,
    constant TransitionMaskParams& p) {
    uint width = p.dims.x;
    uint height = p.dims.y;
    uint effect_mode = p.options.w;
    float progress = clamp(p.control.x, 0.0f, 1.0f);
    float pulse = sin(3.14159265358979323846f * progress);
    uint transition_mode = uint(max(p.effect_extra.w, 0.0f) + 0.5f);
    if (transition_mode <= 3u && p.options.x == 4u) {
        float radius = max(0.0f, p.effect_control.x)
            * float(min(max(width, 1u), max(height, 1u)))
            * pow(max(pulse, 0.0f), 0.72f);
        float mix_amount = pow(max(pulse, 0.0f), 0.65f);
        float scale = transition_mode == 1u ? 1.65f
            : (transition_mode == 3u ? 1.25f : 1.0f);
        float3 raw = transition_sample(image, point, width, height);
        float3 filtered = transition_directional_blur(
            image, point, float2(0.70710678f, 0.70710678f),
            radius, width, height);
        return mix(raw, filtered, min(1.0f, mix_amount * scale));
    }
    if (effect_mode == 1u || effect_mode == 2u) {
        float2 axis = (p.options.y == 2u || p.options.y == 3u)
            ? float2(0.0f, 1.0f) : float2(1.0f, 0.0f);
        float radius = max(0.0f, p.effect_control.x)
            * pow(max(pulse, 0.0f), effect_mode == 2u ? 0.72f : 0.90f)
            * clamp(p.effect_control.z, 0.0f, 2.0f);
        float mix_amount = clamp(p.effect_control.y, 0.0f, 1.0f)
            * pow(max(pulse, 0.0f), 0.35f);
        float2 shifted = point;
        if (effect_mode == 2u) {
            float travel = 0.06f * pulse * pow(max(pulse, 0.0f), 0.65f);
            if (p.options.y == 0u || p.options.y == 2u) travel = -travel;
            shifted += axis * travel * float2(width, height);
        }
        float3 raw = transition_sample(image, shifted, width, height);
        float3 filtered = transition_directional_blur(
            image, shifted, axis, radius, width, height);
        return mix(raw, filtered, mix_amount);
    }
    if (effect_mode == 3u || effect_mode == 4u) {
        float strength = effect_mode == 3u
            ? max(0.0f, p.effect_control.w)
                * pow(max(pulse, 0.0f), 0.72f)
            : max(0.0f, p.effect_extra.x) * 0.0174532925199433f
                * pow(max(pulse, 0.0f), 0.72f);
        if (p.options.y == 1u || p.options.y == 3u) strength = -strength;
        float3 raw = transition_sample(image, point, width, height);
        float3 filtered = transition_radial_blur(
            image, point, effect_mode == 4u, strength, width, height);
        float mix_amount = clamp(p.effect_control.y, 0.0f, 1.0f)
            * pow(max(pulse, 0.0f), 0.35f);
        return mix(raw, filtered, mix_amount);
    }
    if (effect_mode == 5u) {
        float2 axis = (p.options.y == 2u || p.options.y == 3u)
            ? float2(0.0f, 1.0f) : float2(1.0f, 0.0f);
        float sign = (p.options.y == 0u || p.options.y == 2u)
            ? -1.0f : 1.0f;
        float amount = max(0.0f, p.effect_control.w)
            * pow(max(pulse, 0.0f), 0.7f);
        float3 raw = transition_sample(image, point, width, height);
        float3 shifted = raw;
        shifted.r = transition_sample(
            image, point + axis * amount * sign * float2(width, height),
            width, height).r;
        shifted.b = transition_sample(
            image, point - axis * amount * sign * float2(width, height),
            width, height).b;
        return mix(raw, shifted, clamp(p.effect_extra.z, 0.0f, 1.0f)
                   * pow(max(pulse, 0.0f), 0.25f));
    }
    return transition_sample(image, point, width, height);
}

kernel void timeline_transition_source_effect(
    device const float4* left_input [[buffer(0)]],
    device const float4* right_input [[buffer(1)]],
    device float4* left_output [[buffer(2)]],
    device float4* right_output [[buffer(3)]],
    constant TransitionMaskParams& p [[buffer(4)]],
    uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= p.dims.x || gid.y >= p.dims.y) return;
    uint index = gid.y * p.dims.x + gid.x;
    float2 point = float2(gid);
    float3 left = transition_source_effect(left_input, point, p);
    float3 right = transition_source_effect(right_input, point, p);
    left_output[index] = float4(left, left_input[index].a);
    right_output[index] = float4(right, right_input[index].a);
}

float3 transition_to_linear(float3 value) {
    return pow(max(value, float3(0.0f)), float3(2.2f));
}

float3 transition_from_linear(float3 value) {
    return pow(max(value, float3(0.0f)), float3(1.0f / 2.2f));
}

kernel void timeline_wipe_composite(
    device const float4* accumulator [[buffer(0)]],
    device const float4* left_layer [[buffer(1)]],
    device const float4* right_layer [[buffer(2)]],
    device const float* mask [[buffer(3)]],
    device float4* output [[buffer(4)]],
    constant WipeBlendParams& p [[buffer(5)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.dims.x, height = p.dims.y;
    if (gid.x >= width || gid.y >= height) return;
    uint index = gid.y * width + gid.x;
    float amount = clamp(mask[index], 0.0f, 1.0f);
    float3 wipe = left_layer[index].rgb * (1.0f - amount)
            * clamp(p.control.y, 0.0f, 1.0f)
        + right_layer[index].rgb * amount
            * clamp(p.control.z, 0.0f, 1.0f);
    if (p.transition.x > 1.5f && p.transition.x < 2.5f) {
        wipe = transition_from_linear(transition_to_linear(
            left_layer[index].rgb * clamp(p.control.y, 0.0f, 1.0f))
            * (1.0f - amount)
            + transition_to_linear(
                right_layer[index].rgb * clamp(p.control.z, 0.0f, 1.0f))
            * amount);
    }
    if (p.control.w > 0.5f) {
        output[index] = float4(wipe, 1.0f);
        return;
    }
    float3 base = accumulator[index].rgb;
    output[index] = float4(mix(base, wipe,
                               clamp(p.control.x, 0.0f, 1.0f)), 1.0f);
}

struct StackFinalizeParams {
    uint4 dims;       // output width, height
    float4 control;   // dip factor, output-yuv flag, reserved, reserved
    float4 dip_color; // RGB
};

kernel void timeline_finalize_stack(
    device const float4* accumulator [[buffer(0)]],
    device uchar* output [[buffer(1)]],
    constant StackFinalizeParams& p [[buffer(2)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.dims.x, height = p.dims.y;
    if (gid.x >= width || gid.y >= height) return;
    uint index = gid.y * width + gid.x;
    float dip_factor = clamp(p.control.x, 0.0f, 1.0f);
    float3 normalized = accumulator[index].rgb;
    normalized = mix(normalized, p.dip_color.rgb, dip_factor);
    float3 color = normalized * 255.0f;
    if (p.control.y < 0.5f) {
        uint pixel = index * 3u;
        output[pixel] = to_u8(color.b);
        output[pixel + 1u] = to_u8(color.g);
        output[pixel + 2u] = to_u8(color.r);
        return;
    }
    uint luma_size = width * height;
    uint chroma_width = width / 2u, chroma_height = height / 2u;
    uint chroma_size = chroma_width * chroma_height;
    float3 rgb = float3(to_u8(color.r), to_u8(color.g), to_u8(color.b)) / 255.0f;
    float y = dot(rgb, float3(0.2126f, 0.7152f, 0.0722f));
    output[index] = to_u8(clamp(16.0f + 219.0f * y, 16.0f, 235.0f));
    if (gid.x < chroma_width && gid.y < chroma_height) {
        uint2 p00 = gid * 2u;
        uint i0 = p00.y * width + p00.x;
        uint i1 = i0 + 1u;
        uint i2 = i0 + width;
        uint i3 = i2 + 1u;
        float3 c0n = mix(accumulator[i0].rgb, p.dip_color.rgb, dip_factor);
        float3 c1n = mix(accumulator[i1].rgb, p.dip_color.rgb, dip_factor);
        float3 c2n = mix(accumulator[i2].rgb, p.dip_color.rgb, dip_factor);
        float3 c3n = mix(accumulator[i3].rgb, p.dip_color.rgb, dip_factor);
        float3 c0 = float3(to_u8(c0n.r * 255.0f), to_u8(c0n.g * 255.0f),
                           to_u8(c0n.b * 255.0f));
        float3 c1 = float3(to_u8(c1n.r * 255.0f), to_u8(c1n.g * 255.0f),
                           to_u8(c1n.b * 255.0f));
        float3 c2 = float3(to_u8(c2n.r * 255.0f), to_u8(c2n.g * 255.0f),
                           to_u8(c2n.b * 255.0f));
        float3 c3 = float3(to_u8(c3n.r * 255.0f), to_u8(c3n.g * 255.0f),
                           to_u8(c3n.b * 255.0f));
        float3 avg = (c0 + c1 + c2 + c3) / 1020.0f;
        float yy = dot(avg, float3(0.2126f, 0.7152f, 0.0722f));
        float cb = (avg.b - yy) / 1.8556f;
        float cr = (avg.r - yy) / 1.5748f;
        uint ci = gid.y * chroma_width + gid.x;
        output[luma_size + ci] = to_u8(clamp(128.0f + 224.0f * cb, 16.0f, 240.0f));
        output[luma_size + chroma_size + ci] = to_u8(clamp(128.0f + 224.0f * cr, 16.0f, 240.0f));
    }
}

kernel void timeline_finalize_stack_yuv420_surface(
    device const float4* accumulator [[buffer(0)]],
    constant StackFinalizeParams& p [[buffer(1)]],
    texture2d<float, access::write> y_output [[texture(0)]],
    texture2d<float, access::write> uv_output [[texture(1)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.dims.x, height = p.dims.y;
    if (gid.x >= width || gid.y >= height) return;
    uint index = gid.y * width + gid.x;
    float dip_factor = clamp(p.control.x, 0.0f, 1.0f);
    float3 normalized = mix(accumulator[index].rgb, p.dip_color.rgb,
                            dip_factor);
    float3 yuv = rgb_code_to_yuv420p8(normalized * 255.0f);
    y_output.write(float4(yuv.x / 255.0f, 0.0f, 0.0f, 1.0f), gid);
    if ((gid.x & 1u) == 0u && (gid.y & 1u) == 0u) {
        uint2 p00 = gid;
        uint i0 = p00.y * width + p00.x;
        uint i1 = i0 + 1u;
        uint i2 = i0 + width;
        uint i3 = i2 + 1u;
        float3 n0 = mix(accumulator[i0].rgb, p.dip_color.rgb, dip_factor);
        float3 n1 = mix(accumulator[i1].rgb, p.dip_color.rgb, dip_factor);
        float3 n2 = mix(accumulator[i2].rgb, p.dip_color.rgb, dip_factor);
        float3 n3 = mix(accumulator[i3].rgb, p.dip_color.rgb, dip_factor);
        float3 c0 = rgb_code_to_yuv420p8(n0 * 255.0f);
        float3 c1 = rgb_code_to_yuv420p8(n1 * 255.0f);
        float3 c2 = rgb_code_to_yuv420p8(n2 * 255.0f);
        float3 c3 = rgb_code_to_yuv420p8(n3 * 255.0f);
        float2 chroma = (float2(c0.y, c0.z) + float2(c1.y, c1.z)
                       + float2(c2.y, c2.z) + float2(c3.y, c3.z))
                      / (4.0f * 255.0f);
        uv_output.write(float4(chroma.x, chroma.y, 0.0f, 1.0f),
                        uint2(gid.x / 2u, gid.y / 2u));
    }
}

kernel void timeline_finalize_stack_p010_surface(
    device const float4* accumulator [[buffer(0)]],
    constant StackFinalizeParams& p [[buffer(1)]],
    texture2d<float, access::write> y_output [[texture(0)]],
    texture2d<float, access::write> uv_output [[texture(1)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.dims.x, height = p.dims.y;
    if (gid.x >= width || gid.y >= height) return;
    uint index = gid.y * width + gid.x;
    float dip_factor = clamp(p.control.x, 0.0f, 1.0f);
    float3 normalized = mix(accumulator[index].rgb, p.dip_color.rgb,
                            dip_factor);
    float3 yuv = rgb_code_to_yuv420p10(normalized * 255.0f);
    y_output.write(float4(yuv.x / 1023.0f, 0.0f, 0.0f, 1.0f), gid);
    if ((gid.x & 1u) == 0u && (gid.y & 1u) == 0u) {
        uint2 p00 = gid;
        uint i0 = p00.y * width + p00.x;
        uint i1 = i0 + 1u;
        uint i2 = i0 + width;
        uint i3 = i2 + 1u;
        float3 n0 = mix(accumulator[i0].rgb, p.dip_color.rgb, dip_factor);
        float3 n1 = mix(accumulator[i1].rgb, p.dip_color.rgb, dip_factor);
        float3 n2 = mix(accumulator[i2].rgb, p.dip_color.rgb, dip_factor);
        float3 n3 = mix(accumulator[i3].rgb, p.dip_color.rgb, dip_factor);
        float3 c0 = rgb_code_to_yuv420p10(n0 * 255.0f);
        float3 c1 = rgb_code_to_yuv420p10(n1 * 255.0f);
        float3 c2 = rgb_code_to_yuv420p10(n2 * 255.0f);
        float3 c3 = rgb_code_to_yuv420p10(n3 * 255.0f);
        float2 chroma = (float2(c0.y, c0.z) + float2(c1.y, c1.z)
                       + float2(c2.y, c2.z) + float2(c3.y, c3.z))
                      / (4.0f * 1023.0f);
        uv_output.write(float4(chroma.x, chroma.y, 0.0f, 1.0f),
                        uint2(gid.x / 2u, gid.y / 2u));
    }
}

kernel void timeline_finalize_stack_u16(
    device const float4* accumulator [[buffer(0)]],
    device ushort* output [[buffer(1)]],
    constant StackFinalizeParams& p [[buffer(2)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.dims.x, height = p.dims.y;
    if (gid.x >= width || gid.y >= height) return;
    uint index = gid.y * width + gid.x;
    float dip_factor = clamp(p.control.x, 0.0f, 1.0f);
    float3 normalized = mix(accumulator[index].rgb, p.dip_color.rgb, dip_factor);
    uint pixel = index * 3u;
    output[pixel] = to_u16(normalized.b * 65535.0f);
    output[pixel + 1u] = to_u16(normalized.g * 65535.0f);
    output[pixel + 2u] = to_u16(normalized.r * 65535.0f);
}

kernel void timeline_finalize_stack_yuv422p10(
    device const float4* accumulator [[buffer(0)]],
    device ushort* output [[buffer(1)]],
    constant StackFinalizeParams& p [[buffer(2)]],
    uint2 gid [[thread_position_in_grid]]) {
    uint width = p.dims.x, height = p.dims.y;
    if (gid.x >= width || gid.y >= height) return;
    uint index = gid.y * width + gid.x;
    float dip_factor = clamp(p.control.x, 0.0f, 1.0f);
    float3 normalized = mix(accumulator[index].rgb, p.dip_color.rgb, dip_factor);
    float3 yuv = rgb_code_to_yuv422p10(
        normalized * 255.0f, 0.2126f, 0.0722f, false);
    uint luma_size = width * height;
    uint chroma_width = width / 2u;
    uint chroma_size = chroma_width * height;
    output[index] = to_u16(yuv.x);
    if (gid.x < chroma_width) {
        uint i0 = gid.y * width + gid.x * 2u;
        uint i1 = i0 + 1u;
        float3 n0 = mix(accumulator[i0].rgb, p.dip_color.rgb, dip_factor);
        float3 n1 = mix(accumulator[i1].rgb, p.dip_color.rgb, dip_factor);
        float3 chroma = rgb_code_to_yuv422p10(
            (n0 + n1) * 127.5f, 0.2126f, 0.0722f, false);
        uint ci = gid.y * chroma_width + gid.x;
        output[luma_size + ci] = to_u16(chroma.y);
        output[luma_size + chroma_size + ci] = to_u16(chroma.z);
    }
}

"""


_LAYER_BLEND_PARAMS_DTYPE = np.dtype([
    ("dims", "<u4", (4,)),
    ("options", "<u4", (4,)),
    ("control", "<f4", (4,)),
    ("inverse_x", "<f4", (4,)),
    ("inverse_y", "<f4", (4,)),
    ("crop", "<f4", (4,)),
    ("geometry", "<f4", (4,)),
])

_STACK_FINALIZE_PARAMS_DTYPE = np.dtype([
    ("dims", "<u4", (4,)),
    ("control", "<f4", (4,)),
    ("dip_color", "<f4", (4,)),
])

_WIPE_BLEND_PARAMS_DTYPE = np.dtype([
    ("dims", "<u4", (4,)),
    ("control", "<f4", (4,)),
    ("transition", "<f4", (4,)),
])

_TRANSITION_MASK_PARAMS_DTYPE = np.dtype([
    ("dims", "<u4", (4,)),
    ("control", "<f4", (4,)),  # progress, angle, softness, reverse
    ("options", "<u4", (4,)),  # kind, direction, vertex_count, effect
    ("vertices", "<f4", (48, 2)),
    ("effect_control", "<f4", (4,)),  # radius/ratio, mix, shutter, strength
    ("effect_extra", "<f4", (4,)),  # spin angle, split amount, mix, mode
])


@dataclass(frozen=True)
class _MetalBufferImage:
    """A BGR8 image already resident in this renderer's Metal buffers."""

    buffer: Any
    width: int
    height: int


class TimelineMetalExportRenderer:
    """Metal source conversion, primary grade, scaling, stack blend and export."""

    def __init__(self, gpu_trail: Optional[ExportGpuTrail] = None) -> None:
        self._render_lock = threading.RLock()
        self._context: Optional[MetalComputeContext] = None
        self._kernels: Dict[str, MetalKernel] = {}
        self._buffers: Dict[str, Any] = {}
        self._failed = False
        self._released = False
        self.metal_errors = 0
        self.frames_rendered = 0
        self.last_fallback_reason: Optional[str] = None
        self._logged_fallback_reasons: set[str] = set()
        self._gpu_trail = gpu_trail
        # AUD-18602：当前帧 CPU 上传字节数（_upload_array 累计；公开渲染
        # 入口重置）。render_layer_stack 等未显式传 cpu_upload_bytes 的
        # 完成记录用它，full-GPU 证据不再把 stack 路径记成 0 字节搬运。
        self._frame_cpu_upload_bytes = 0

    def set_gpu_trail(self, gpu_trail: Optional[ExportGpuTrail]) -> None:
        """Attach the export evidence sink before rendering begins."""
        with self._render_lock:
            self._gpu_trail = gpu_trail

    def _record_gpu_sample(
        self,
        name: str,
        *,
        gpu_submit_ms: Optional[float],
        cpu_upload_bytes: int = 0,
        cpu_download_bytes: int = 0,
    ) -> None:
        trail = self._gpu_trail
        if trail is None:
            return
        trail.record_stage_sample(
            name=name,
            backend="metal",
            gpu_submit_ms=gpu_submit_ms,
            cpu_upload_bytes=cpu_upload_bytes,
            cpu_download_bytes=cpu_download_bytes,
        )

    def _record_fallback(self, reason: str) -> None:
        self.last_fallback_reason = reason
        if self._gpu_trail is not None:
            self._gpu_trail.set_fallback_reason(reason)

    def _record_render_completion(
        self,
        context: MetalComputeContext,
        output_size: int,
        *,
        yuv_output: bool,
        cpu_upload_bytes: Optional[int] = None,
        readback: bool = True,
    ) -> None:
        """Record GPU timing and the current renderer's explicit readback.

        Grading and YUV conversion are currently fused into one kernel. The
        measured command interval is therefore attributed to ``render``; the
        YUV stage stays unmeasured until it gets its own encoder boundary.
        That keeps the full-GPU gate fail-closed.

        AUD-18602：``cpu_upload_bytes=None``（默认）时使用本帧
        ``_upload_array`` 累计值——render_layer_stack 大量上传（源平面/
        wipe 参数/逐层 blend 参数/效果权重）此前记 0 字节，full-GPU 门
        据此得出偏乐观结论；显式传值仍优先（_render 的精确和保持不变）。
        """
        if cpu_upload_bytes is None:
            cpu_upload_bytes = self._frame_cpu_upload_bytes
        if yuv_output:
            self._record_gpu_sample(
                "render",
                gpu_submit_ms=context.last_gpu_time_ms,
                cpu_upload_bytes=cpu_upload_bytes,
            )
            self._record_gpu_sample(
                "yuv_conversion",
                gpu_submit_ms=None,
                cpu_download_bytes=(int(output_size) if readback else 0),
            )
        else:
            self._record_gpu_sample(
                "render",
                gpu_submit_ms=context.last_gpu_time_ms,
                cpu_upload_bytes=cpu_upload_bytes,
                cpu_download_bytes=(int(output_size) if readback else 0),
            )

    def _resolve_surface_buffer(
        self,
        surface: GpuEncodeSurface,
        expected_size: int,
    ) -> Optional[Any]:
        """Resolve a caller-owned linear Metal output buffer.

        This is the first no-readback seam. It intentionally accepts only one
        Metal buffer plane because the existing fused kernels write a packed
        linear YUV layout; CVPixelBuffer plane textures will be added once the
        VideoToolbox encoder bridge is available.
        """
        context = self._context
        if context is None:
            self._fallback("metal_renderer_unavailable")
            return None
        device_identity = getattr(context, "device_identity", context.device_name)
        reason = surface.validate(
            backend="metal", device_identity=str(device_identity))
        if reason:
            self._fallback(reason)
            return None
        planes = tuple(surface.descriptor.planes)
        if len(planes) != 1:
            self._fallback("unsupported_encode_surface_layout")
            return None
        buffer = planes[0].resolved_handle()
        length = getattr(buffer, "length", None)
        if not callable(length):
            self._fallback("encode_surface_not_metal_buffer")
            return None
        if int(length()) < int(expected_size):
            self._fallback("encode_surface_buffer_too_small")
            return None
        return buffer

    def _resolve_surface_yuv420_textures(
        self,
        surface: GpuEncodeSurface,
        out_width: int,
        out_height: int,
    ) -> Optional[tuple[Any, Any]]:
        """Resolve two IOSurface-backed Metal planes for a VT 4:2:0 target."""
        context = self._context
        if context is None:
            self._fallback("metal_renderer_unavailable")
            return None
        if surface.descriptor.backend != "videotoolbox":
            self._fallback("unsupported_encode_surface_backend")
            return None
        device_identity = getattr(context, "device_identity", context.device_name)
        reason = surface.validate(
            backend="videotoolbox", device_identity=str(device_identity))
        if reason:
            self._fallback(reason)
            return None
        pixel_format = str(surface.descriptor.pixel_format).lower()
        if pixel_format not in {"nv12", "p010"}:
            self._fallback("unsupported_videotoolbox_surface_format")
            return None
        planes = {plane.component: plane for plane in surface.descriptor.planes}
        y_plane, uv_plane = planes.get("Y"), planes.get("UV")
        if y_plane is None or uv_plane is None:
            self._fallback("unsupported_encode_surface_layout")
            return None
        if (y_plane.api != "metal" or uv_plane.api != "metal"
                or y_plane.resolved_handle() is None
                or uv_plane.resolved_handle() is None):
            self._fallback("encode_surface_not_metal_texture")
            return None
        if (int(y_plane.width) != int(out_width)
                or int(y_plane.height) != int(out_height)
                or int(uv_plane.width) != int(out_width) // 2
                or int(uv_plane.height) != int(out_height) // 2):
            self._fallback("encode_surface_dimensions_mismatch")
            return None
        return y_plane.resolved_handle(), uv_plane.resolved_handle()

    @property
    def failed(self) -> bool:
        return self._failed

    @staticmethod
    def is_supported() -> bool:
        return MetalComputeContext.is_supported()

    def _ensure_ready(self) -> bool:
        if self._context is not None:
            return True
        if self._failed or self._released:
            return False
        try:
            self._context = MetalComputeContext()
            # The MSL library is shared by every entrypoint, but creating all
            # pipeline states here forces a no-effect export to compile every
            # filter kernel too. Build each PSO on first use instead.
            return True
        except Exception as error:  # device, PyObjC, MSL or PSO failure
            self._failed = True
            self._record_fallback("metal_initialization_failure")
            logger.warning("Metal timeline renderer initialization failed: %s", error)
            return False

    def _kernel(self, name: str) -> MetalKernel:
        """Lazily compile and cache only the pipeline used by this render path."""
        context = self._context
        if context is None:
            raise RuntimeError("Metal context is not initialized")
        kernel = self._kernels.get(name)
        if kernel is None:
            try:
                kernel = context.compile_kernel(name, _TIMELINE_METAL_MSL)
            except Exception as error:
                self._failed = True
                self._record_fallback("metal_initialization_failure")
                logger.warning(
                    "Metal timeline pipeline compilation failed (%s): %s",
                    name, error,
                )
                raise
            self._kernels[name] = kernel
        return kernel

    @staticmethod
    def _should_materialize_effect_chain(
        effect_chain: tuple[Dict[str, Any], ...],
        is_metal_texture: bool,
    ) -> bool:
        """Decide whether the layer's effect chain must be materialized first.

        AUD-18605: Timeline effects sample RGB neighbours.  On the fused
        path (``timeline_stack_layer_effect``) *every* effect tap repeats
        the full per-tap source decode (bilinear YUV->RGB for planar
        sources) plus the primary grade — a single small-radius effect
        (1 < half <= 5) on a planar/packed source therefore costs up to
        121 full decodes per output pixel at 4K.  Materializing once
        (one decode+grade per pixel) before the blend is strictly cheaper
        whenever taps > 1.
        """
        if len(effect_chain) > 1:
            return True
        if len(effect_chain) == 1:
            effect = effect_chain[0]
            if int(round(float(effect.get("kind", 0.0)))) in (6, 7, 8, 9, 10, 11):
                return True
            # Materialize a Metal YUV texture once before any single effect
            # so each tap reuses decoded RGB instead of repeating YUV->RGB.
            if is_metal_texture:
                return True
            # AUD-18605: single effect on a planar/packed source with a
            # multi-tap neighbourhood (> 1 tap per axis) also materializes.
            try:
                half_x = float(effect.get("half_x", 0.0))
                half_y = float(effect.get("half_y", 0.0))
            except (TypeError, ValueError):
                half_x = half_y = 0.0
            if max(half_x, half_y) > 1.0:
                return True
        return False

    def _apply_effect_chain(
        self,
        *,
        prefix: str,
        source_width: int,
        source_height: int,
        packed_bgr: np.ndarray,
        y_plane: np.ndarray,
        u_plane: np.ndarray,
        v_plane: np.ndarray,
        is_metal_texture: bool,
        metal_y_texture: Any,
        metal_uv_texture: Any,
        metal_v_texture: Any,
        source_params: np.ndarray,
        effects: tuple[Dict[str, Any], ...],
    ) -> _MetalBufferImage:
        """Decode/grade and apply an ordered effect chain without CPU readback."""
        if self._context is None:
            raise RuntimeError("Metal context is not initialized")
        if source_width <= 0 or source_height <= 0 or len(effects) < 1:
            raise ValueError("Metal effect-chain input is invalid")

        ctx = self._context
        pixel_bytes = int(source_width) * int(source_height) * 3
        # All chain dispatches are encoded in layer order into one command
        # buffer and finish before this method returns. Reuse a single
        # ping-pong pair across layers to bound scratch memory by the largest
        # active source frame, rather than the number of effected layers.
        image_a = self._private_buffer("stack_effect_chain_a", pixel_bytes)
        image_b = self._private_buffer("stack_effect_chain_b", pixel_bytes)
        params_buffer = self._upload_array(
            prefix + "chain_source_params", source_params)

        if is_metal_texture:
            if metal_y_texture is None or metal_uv_texture is None:
                raise ValueError("Metal texture effect-chain source is incomplete")
            ctx.dispatch(
                self._kernel(
                    "timeline_materialize_texture_planar"
                    if metal_v_texture is not None
                    else "timeline_materialize_texture"),
                buffers={2: image_a, 3: params_buffer},
                textures=(
                    {0: metal_y_texture, 1: metal_uv_texture, 2: metal_v_texture}
                    if metal_v_texture is not None
                    else {0: metal_y_texture, 1: metal_uv_texture}
                ),
                threads=(source_width, source_height, 1),
            )
        else:
            source_buffers = {
                0: self._upload_array(prefix + "chain_source_bgr", packed_bgr),
                1: self._upload_array(prefix + "chain_source_y", y_plane),
                2: self._upload_array(prefix + "chain_source_u", u_plane),
                3: self._upload_array(prefix + "chain_source_v", v_plane),
                4: image_a,
                5: params_buffer,
            }
            ctx.dispatch(
                self._kernel("timeline_materialize_source"),
                buffers=source_buffers,
                threads=(source_width, source_height, 1),
            )

        dummy_plane = self._upload_array(
            prefix + "chain_dummy_plane", np.zeros(1, dtype=np.uint16))
        dummy_mip_atlas = self._upload_array(
            prefix + "chain_dummy_mip_atlas", np.zeros(1, dtype=np.uint8))
        horizontal = self._buffers.get("stack_effect_chain_horizontal")
        current, alternate = image_a, image_b
        for effect_index, effect in enumerate(effects):
            params = self._make_params(
                source_width, source_height, source_width, source_height,
                1, 1, 1, 1, 8, 1, 1, False, False,
                1.0, False, False, 0.2126, 0.0722, None, effect,
            )
            effect_kind = int(round(float(effect["kind"])))
            if effect_kind == 11:
                weights_x = self._private_buffer(
                    f"{prefix}chain_effect_{effect_index}_bokeh_offsets",
                    48 * 2 * 4,
                )
            else:
                weights_x = self._upload_array(
                    f"{prefix}chain_effect_{effect_index}_weights_x",
                    self._effect_axis_weights(effect, "x"),
                )
            weights_y = self._upload_array(
                f"{prefix}chain_effect_{effect_index}_weights_y",
                self._effect_axis_weights(effect, "y"),
            )
            half_x = int(round(float(effect["half_x"])))
            half_y = int(round(float(effect["half_y"])))
            mip_atlas = dummy_mip_atlas
            if effect_kind == 8 and float(effect.get("mode", 0.0)) > 0.5:
                max_radius = max(0.0, float(effect.get("max_radius", 20.0)))
                max_level = min(7, max(1, int(np.ceil(np.log2(max_radius + 1.0)))))
                mip_levels: list[tuple[int, int, int, int]] = [
                    (int(source_width), int(source_height), 0, 0)
                ]
                mip_width, mip_height = int(source_width), int(source_height)
                mip_offset = 0
                for _level in range(1, max_level + 1):
                    next_width = max(1, (mip_width + 1) // 2)
                    next_height = max(1, (mip_height + 1) // 2)
                    if next_width == mip_width and next_height == mip_height:
                        break
                    mip_levels.append((next_width, next_height, mip_offset, 0))
                    mip_offset += next_width * next_height * 3
                    mip_width, mip_height = next_width, next_height
                mip_atlas = self._private_buffer(
                    "stack_effect_chain_mipmap", max(1, mip_offset))
                params["mip_dims"][0, :len(mip_levels)] = np.asarray(
                    mip_levels, dtype=np.uint32)
                source_mip_offset = 0
                for mip_level, level_info in enumerate(mip_levels[1:], start=1):
                    level_width, level_height, dest_offset, _ = level_info
                    source_width_for_mip, source_height_for_mip = (
                        mip_levels[mip_level - 1][0],
                        mip_levels[mip_level - 1][1],
                    )
                    downsample_params = np.zeros(1, dtype=_MIP_PARAMS_DTYPE)
                    downsample_params["dims"] = (
                        source_width_for_mip, source_height_for_mip,
                        level_width, level_height,
                    )
                    downsample_params["offsets"] = (
                        source_mip_offset, dest_offset,
                        0 if mip_level == 1 else 1, 0,
                    )
                    downsample_source = current if mip_level == 1 else mip_atlas
                    downsample_buffer = self._upload_array(
                        f"{prefix}chain_effect_{effect_index}_mip_{mip_level}_params",
                        downsample_params,
                    )
                    ctx.dispatch(
                        self._kernel("timeline_defocus_downsample"),
                        buffers={0: downsample_source, 1: mip_atlas,
                                 2: downsample_buffer},
                        threads=(level_width, level_height, 1),
                    )
                    source_mip_offset = dest_offset
            if effect_kind == 6 and float(effect.get("preblur", 0.0)) > 0.0:
                preblur_output = self._upload_array(
                    f"{prefix}chain_effect_{effect_index}_preblur_params",
                    params,
                )
                ctx.dispatch(
                    self._kernel("timeline_edge_preblur"),
                    buffers={0: current, 1: alternate, 2: preblur_output},
                    threads=(source_width, source_height, 1),
                )
                current, alternate = alternate, current
                # The separate Metal preblur dispatch already applied the
                # CPU export's 3x3 fractional Gaussian, so Sobel samples the
                # resulting scalar plane directly.
                params["effect_extra"][0, 3] = 0.0
            effect_params_buffer = self._upload_array(
                f"{prefix}chain_effect_{effect_index}_params", params)
            if effect_kind == 11:
                ctx.dispatch(
                    self._kernel("timeline_build_bokeh_offsets"),
                    buffers={0: weights_x, 1: effect_params_buffer},
                    threads=(48, 1, 1),
                )
            separable = (
                (effect_kind not in (6, 7, 8, 9, 10, 11)
                 and max(half_x, half_y) > 5)
                or (effect_kind == 5 and max(half_x, half_y) > 1)
            )
            if separable:
                horizontal = self._private_buffer(
                    "stack_effect_chain_horizontal",
                    int(source_width) * int(source_height) * 8,
                )
                ctx.dispatch(
                    self._kernel("timeline_effect_horizontal"),
                    buffers={
                        0: current, 1: dummy_plane, 2: dummy_plane,
                        3: dummy_plane, 4: horizontal,
                        5: effect_params_buffer, 6: weights_x,
                    },
                    threads=(source_width, source_height, 1),
                )
            elif horizontal is None:
                # Bound for a direct kernel without allocating a frame-sized
                # scratch when every effect in this chain is small.
                horizontal = self._private_buffer(
                    "stack_effect_chain_horizontal", 8)
            if effect_kind in (7, 9) and max(half_x, half_y) <= 10:
                padded_width = ((source_width + 15) // 16) * 16
                padded_height = ((source_height + 15) // 16) * 16
                ctx.dispatch(
                    self._kernel(
                        "timeline_apply_denoise_tiled_packed"
                        if effect_kind == 7
                        else "timeline_apply_bilateral_tiled_packed"),
                    buffers={
                        0: current, 1: alternate,
                        2: effect_params_buffer,
                        3: weights_x, 4: weights_y,
                    },
                    threads=(padded_width, padded_height, 1),
                    group=(16, 16, 1),
                )
            else:
                ctx.dispatch(
                    self._kernel("timeline_apply_effect_packed"),
                    buffers={
                        0: current, 1: alternate,
                        2: dummy_plane, 3: dummy_plane, 4: dummy_plane,
                        5: effect_params_buffer, 6: horizontal,
                        7: weights_y, 8: weights_x, 9: mip_atlas,
                    },
                    threads=(source_width, source_height, 1),
                )
            current, alternate = alternate, current

        return _MetalBufferImage(current, int(source_width), int(source_height))

    def _fallback(self, reason: str) -> None:
        """Record and report a coverage fallback without treating it as a GPU error."""
        self._record_fallback(reason)
        if reason not in self._logged_fallback_reasons:
            self._logged_fallback_reasons.add(reason)
            logger.info(
                "Metal timeline render does not support %s; using CPU fallback",
                reason,
            )

    @_serialize_renderer_call
    def render_planar(
        self,
        source: Any,
        grade_uniforms: Optional[Dict[str, Any]],
        out_width: int,
        out_height: int,
        opacity: float,
        output_u16: bool = False,
        output_yuv420p: bool = False,
        output_yuv422p10: bool = False,
    ) -> Optional[Any]:
        from src.features.edit.core.timeline_gpu_export_renderer import GpuYuvSource
        if not isinstance(source, GpuYuvSource):
            self._fallback("unsupported_planar_source")
            return None
        return self._render(source, grade_uniforms, out_width, out_height,
                            opacity, output_u16, output_yuv420p,
                            output_yuv422p10)

    def _render_topos_metal_texture_frame(
        self,
        source: Any,
        grade_uniforms: Optional[Dict[str, Any]],
        out_width: int,
        out_height: int,
        opacity: float,
        output_u16: bool,
        output_yuv420p: bool,
        output_yuv422p10: bool,
        output_surface: Optional[GpuEncodeSurface] = None,
    ) -> Optional[Any]:
        """Render a Topos IOSurface Y/U/V frame without staging through NumPy.

        Topos 4:2:2 is already an encoder-native format.  The Metal kernel
        samples the imported ``R16Uint`` planes, performs range/matrix/grade
        conversion and writes the final planar 10-bit output buffer.  The
        buffer is the only GPU→CPU transfer because the native encoder ABI
        expects host memory for submission.
        """
        from src.features.edit.core.timeline_gpu_export_renderer import (
            GpuYuv422P10Output,
        )
        from src.shared.media.gpu_frame import GpuDecodedFrame

        # ``_use_high_precision_export`` is also true for native Topos 10-bit
        # output.  It describes RGB precision, while ``output_yuv422p10`` is
        # the encoder-native final format; these two flags are compatible.
        # T-P0-31: NV12/P010 VideoToolbox surfaces are the new explicit
        # output modes — 4:2:2 -> 4:2:0 downsampling happens on the GPU and
        # the surface is returned without any CPU readback.  Everything else
        # outside the planar 422p10 buffer contract still fails closed.
        surface_yuv420 = (
            output_yuv420p
            and not output_yuv422p10
            and output_surface is not None
            and str(output_surface.descriptor.pixel_format).lower()
            in ("nv12", "p010")
        )
        if not output_yuv422p10 and not surface_yuv420:
            self._fallback("unsupported_topos_metal_output_format")
            return None
        if surface_yuv420 and (out_height % 2 or out_width % 2):
            self._fallback("odd_topos_yuv420_surface_dimensions")
            return None
        if not isinstance(source, GpuDecodedFrame):
            self._fallback("unsupported_topos_metal_decoded_frame")
            return None
        if out_width <= 0 or out_height <= 0 or out_width % 2:
            self._fallback("invalid_topos_metal_output_dimensions")
            return None
        if str(source.pixel_format).lower() != "yuv422p10le":
            self._fallback("unsupported_topos_metal_pixel_format")
            return None
        if int(source.bit_depth) != 10:
            self._fallback("unsupported_topos_metal_bit_depth")
            return None

        components = {plane.component: plane for plane in source.planes}
        if any(name not in components for name in ("Y", "U", "V")):
            self._fallback("unsupported_topos_metal_decoded_planes")
            return None
        y_plane, u_plane, v_plane = (
            components["Y"], components["U"], components["V"])
        if any(
            plane.api != "metal" or plane.resource is None
            for plane in (y_plane, u_plane, v_plane)
        ):
            self._fallback("non_metal_topos_decoded_planes")
            return None
        y_texture, u_texture, v_texture = (
            y_plane.resolved_handle(),
            u_plane.resolved_handle(),
            v_plane.resolved_handle(),
        )
        width, height = int(source.width), int(source.height)
        chroma_width = (width + 1) // 2
        if (
            y_texture is None or u_texture is None or v_texture is None
            or width <= 0 or height <= 0
            or int(y_plane.width) != width or int(y_plane.height) != height
            or int(u_plane.width) != chroma_width
            or int(v_plane.width) != chroma_width
            or int(u_plane.height) != height
            or int(v_plane.height) != height
        ):
            self._fallback("invalid_topos_metal_decoded_dimensions")
            return None
        if not self._ensure_ready():
            if self.last_fallback_reason is None:
                self._fallback("metal_renderer_unavailable")
            return None

        matrix = str(source.color_matrix or "bt709").lower()
        if matrix in ("bt2020nc", "bt2020c", "bt2020"):
            kr, kb = 0.2627, 0.0593
        elif matrix in ("smpte170m", "bt470bg", "bt470m", "fcc"):
            kr, kb = 0.2990, 0.1140
        else:
            kr, kb = 0.2126, 0.0722
        full_range = str(source.color_range).lower() == "full"
        command_submitted = False

        try:
            params = self._make_params(
                width, height, int(out_width), int(out_height),
                chroma_width, height, chroma_width, height,
                10, 2, 1, True, full_range,
                float(opacity), bool(grade_uniforms), bool(surface_yuv420),
                kr, kb, grade_uniforms,
                output_yuv422p10=not surface_yuv420,
            )
            ctx = self._context
            assert ctx is not None
            if surface_yuv420:
                output_textures = self._resolve_surface_yuv420_textures(
                    output_surface, int(out_width), int(out_height))
                if output_textures is None:
                    return None
                param_buffer = self._upload_array(
                    "topos_texture_surface_params", params)
                ctx.dispatch(
                    self._kernel(
                        "timeline_fused_export_topos_texture_p010_surface"
                        if str(output_surface.descriptor.pixel_format).lower()
                        == "p010" else
                        "timeline_fused_export_topos_texture_yuv420_surface"),
                    buffers={0: param_buffer},
                    textures={
                        0: y_texture,
                        1: u_texture,
                        2: v_texture,
                        3: output_textures[0],
                        4: output_textures[1],
                    },
                    threads=(int(out_width), int(out_height), 1),
                )
                command_submitted = True
                ctx.finish()
                command_submitted = False
                self._record_render_completion(
                    ctx, 0, yuv_output=True, readback=False)
                self.frames_rendered += 1
                return output_surface
            y_size = int(out_width) * int(out_height)
            c_size = (int(out_width) // 2) * int(out_height)
            output_size = (y_size + 2 * c_size) * 2
            output = (
                self._buffer("topos_texture_fused_output", output_size)
                if output_surface is None else
                self._resolve_surface_buffer(output_surface, output_size)
            )
            if output is None:
                return None
            param_buffer = self._upload_array("topos_texture_fused_params", params)
            ctx.dispatch(
                self._kernel("timeline_fused_export_texture_planar_yuv422p10"),
                buffers={2: output, 3: param_buffer},
                textures={0: y_texture, 1: u_texture, 2: v_texture},
                threads=(int(out_width), int(out_height), 1),
            )
            command_submitted = True
            ctx.finish()
            command_submitted = False
            self._record_render_completion(
                ctx,
                output_size,
                yuv_output=True,
                readback=output_surface is None,
            )
            if output_surface is not None:
                self.frames_rendered += 1
                return output_surface
            data = ctx.read_buffer(output, output_size)
            values = data.view(np.uint16)
            result = GpuYuv422P10Output((
                values[:y_size].reshape(out_height, out_width).copy(),
                values[y_size:y_size + c_size].reshape(
                    out_height, out_width // 2).copy(),
                values[y_size + c_size:].reshape(
                    out_height, out_width // 2).copy(),
            ))
            self.frames_rendered += 1
            return result
        except Exception as error:  # noqa: BLE001 — caller uses CPU fallback
            self.metal_errors += 1
            self._record_fallback("metal_render_error")
            if self.metal_errors == 1:
                logger.warning(
                    "Metal Topos texture export failed; using CPU fallback: %s",
                    error,
                )
            return None
        finally:
            try:
                if command_submitted and self._context is not None:
                    self._context.finish()
            except Exception:
                logger.debug(
                    "Metal Topos texture cleanup failed", exc_info=True)

    @_serialize_renderer_call
    def render_metal_texture_frame(
        self,
        source: Any,
        grade_uniforms: Optional[Dict[str, Any]],
        out_width: int,
        out_height: int,
        opacity: float,
        output_u16: bool = False,
        output_yuv420p: bool = False,
        output_yuv422p10: bool = False,
        output_surface: Optional[GpuEncodeSurface] = None,
    ) -> Optional[Any]:
        """Fuse one VT or Topos Metal texture layer to encoder planes."""
        from src.features.edit.core.timeline_gpu_export_renderer import (
            GpuYuv420Output, GpuYuv422P10Output,
        )
        from src.shared.media.gpu_frame import GpuDecodedFrame

        self.last_fallback_reason = None
        # AUD-18602：每帧 CPU 上传字节计数在入口重置。
        self._frame_cpu_upload_bytes = 0
        if (isinstance(source, GpuDecodedFrame)
                and str(source.pixel_format).lower() == "yuv422p10le"):
            # AUD-18601：Topos 422 纹理源恒为 10-bit，而合成工作集是 8-bit
            # 码值域——高精度交付（u16/422P10）在该链路静默退化为 8-bit
            # 有效精度，fail-closed 回落 GL/CPU（普通 8-bit 输出不受影响）。
            if output_u16 or output_yuv422p10:
                self._fallback("metal_8bit_chain_high_depth_source")
                return None
            return self._render_topos_metal_texture_frame(
                source, grade_uniforms, out_width, out_height, opacity,
                output_u16, output_yuv420p, output_yuv422p10, output_surface,
            )
        if output_u16 and output_yuv420p:
            self._fallback("high_precision_yuv420_output")
            return None
        if output_yuv422p10 and (out_width % 2 or out_height <= 0):
            self._fallback("odd_yuv422p10_output_dimensions")
            return None
        if not isinstance(source, GpuDecodedFrame):
            self._fallback("unsupported_metal_decoded_frame")
            return None
        if out_width <= 0 or out_height <= 0:
            self._fallback("invalid_output_dimensions")
            return None
        if output_yuv420p and (out_width % 2 or out_height % 2):
            self._fallback("odd_yuv420p_output_dimensions")
            return None
        if str(source.pixel_format).lower() not in ("nv12", "p010"):
            self._fallback("unsupported_metal_decoded_frame")
            return None
        components = {plane.component: plane for plane in source.planes}
        if "Y" not in components or "UV" not in components:
            self._fallback("unsupported_metal_decoded_frame_planes")
            return None
        y_plane, uv_plane = components["Y"], components["UV"]
        if (y_plane.api != "metal" or uv_plane.api != "metal"
                or y_plane.resource is None or uv_plane.resource is None):
            self._fallback("non_metal_decoded_frame_planes")
            return None
        y_texture = y_plane.resolved_handle()
        uv_texture = uv_plane.resolved_handle()
        width, height = int(source.width), int(source.height)
        uv_width, uv_height = int(uv_plane.width), int(uv_plane.height)
        bit_depth = int(source.bit_depth)
        if (y_texture is None or uv_texture is None
                or width <= 0 or height <= 0
                or int(y_plane.width) != width
                or int(y_plane.height) != height
                or uv_width != width // 2 or uv_height != height // 2):
            self._fallback("invalid_metal_decoded_dimensions")
            return None
        if bit_depth not in (8, 10):
            self._fallback("unsupported_metal_decoded_bit_depth")
            return None
        # AUD-18601：P010（10-bit）源在 u16/422P10 交付下走 8-bit 码值域
        # 合成链会静默损失精度——fail-closed 回落 GL/CPU（NV12 8-bit 源
        # 与 8-bit 输出不受影响）。
        if (output_u16 or output_yuv422p10) and bit_depth > 8:
            self._fallback("metal_8bit_chain_high_depth_source")
            return None
        if not self._ensure_ready():
            if self.last_fallback_reason is None:
                self._fallback("metal_renderer_unavailable")
            return None

        matrix = str(source.color_matrix or "bt709").lower()
        if matrix in ("bt2020nc", "bt2020c", "bt2020"):
            kr, kb = 0.2627, 0.0593
        elif matrix in ("smpte170m", "bt470bg", "bt470m", "fcc"):
            kr, kb = 0.2990, 0.1140
        else:
            kr, kb = 0.2126, 0.0722
        full_range = str(source.color_range).lower() == "full"

        try:
            params = self._make_params(
                width, height, int(out_width), int(out_height),
                uv_width, uv_height, uv_width, uv_height,
                bit_depth, 2, 2, True, full_range,
                float(opacity), bool(grade_uniforms), output_yuv420p,
                kr, kb, grade_uniforms,
                output_yuv422p10=output_yuv422p10,
            )
            ctx = self._context
            assert ctx is not None
            if output_surface is not None and output_yuv420p:
                output_textures = self._resolve_surface_yuv420_textures(
                    output_surface, int(out_width), int(out_height))
                if output_textures is None:
                    return None
                param_buffer = self._upload_array(
                    "texture_fused_surface_params", params)
                ctx.dispatch(
                    self._kernel(
                        "timeline_fused_export_texture_p010_surface"
                        if str(output_surface.descriptor.pixel_format).lower()
                        == "p010" else
                        "timeline_fused_export_texture_yuv420_surface"),
                    buffers={0: param_buffer},
                    textures={
                        0: y_texture,
                        1: uv_texture,
                        2: output_textures[0],
                        3: output_textures[1],
                    },
                    threads=(int(out_width), int(out_height), 1),
                )
                ctx.finish()
                self._record_render_completion(
                    ctx, 0, yuv_output=True, readback=False)
                self.frames_rendered += 1
                return output_surface
            y_size = int(out_width) * int(out_height)
            c_size = y_size // 4
            y422_c_size = (int(out_width) // 2) * int(out_height)
            output_size = (
                (y_size + 2 * c_size)
                if output_yuv420p else
                (y_size + 2 * y422_c_size) * 2
                if output_yuv422p10 else y_size * 3 * (2 if output_u16 else 1)
            )
            output = (
                self._buffer("texture_fused_output", output_size)
                if output_surface is None else
                self._resolve_surface_buffer(output_surface, output_size)
            )
            if output is None:
                return None
            param_buffer = self._upload_array("texture_fused_params", params)
            ctx.dispatch(
                self._kernel(
                    "timeline_fused_export_texture_yuv422p10"
                    if output_yuv422p10 else
                    "timeline_fused_export_texture_u16"
                    if output_u16 else "timeline_fused_export_texture"),
                buffers={2: output, 3: param_buffer},
                textures={0: y_texture, 1: uv_texture},
                threads=(int(out_width), int(out_height), 1),
            )
            ctx.finish()
            self._record_render_completion(
                ctx,
                output_size,
                yuv_output=bool(output_yuv420p or output_yuv422p10),
                readback=output_surface is None,
            )
            if output_surface is not None:
                self.frames_rendered += 1
                return output_surface
            data = ctx.read_buffer(output, output_size)
            if output_yuv420p:
                result: Any = GpuYuv420Output((
                    data[:y_size].reshape(out_height, out_width).copy(),
                    data[y_size:y_size + c_size].reshape(
                        out_height // 2, out_width // 2).copy(),
                    data[y_size + c_size:].reshape(
                        out_height // 2, out_width // 2).copy(),
                ))
            elif output_yuv422p10:
                # ``data`` was read above from the final stack output.
                values = data.view(np.uint16)
                y422_c_size = (int(out_width) // 2) * int(out_height)
                result = GpuYuv422P10Output((
                    values[:y_size].reshape(out_height, out_width).copy(),
                    values[y_size:y_size + y422_c_size].reshape(
                        out_height, out_width // 2).copy(),
                    values[y_size + y422_c_size:].reshape(
                        out_height, out_width // 2).copy(),
                ))
            elif output_u16:
                result = data.view(np.uint16).reshape(
                    out_height, out_width, 3).copy()
            else:
                result = data.reshape(out_height, out_width, 3).copy()
            self.frames_rendered += 1
            return result
        except Exception as error:  # noqa: BLE001 — caller uses CPU fallback
            self.metal_errors += 1
            self._record_fallback("metal_render_error")
            if self.metal_errors == 1:
                logger.warning(
                    "Metal fused texture export failed; using CPU fallback: %s",
                    error,
                )
            return None
        finally:
            try:
                if self._context is not None:
                    self._context.finish()
            except Exception:
                logger.debug("Metal fused texture cleanup failed", exc_info=True)

    @_serialize_renderer_call
    def render_packed_bgr(
        self,
        frame_bgr_u8: np.ndarray,
        grade_uniforms: Optional[Dict[str, Any]],
        out_width: int,
        out_height: int,
        opacity: float,
        output_u16: bool = False,
        output_yuv420p: bool = False,
        output_yuv422p10: bool = False,
    ) -> Optional[Any]:
        return self._render(frame_bgr_u8, grade_uniforms, out_width, out_height,
                            opacity, output_u16, output_yuv420p,
                            output_yuv422p10)

    @staticmethod
    def _stack_source_bit_depth(source: Any) -> int:
        """层栈源的 luma 位深（AUD-18601 路由门用；打包 ndarray=8）。

        延迟导入避免与 timeline_gpu_export_renderer 的循环依赖；未知
        类型按 8 处理（后续既有校验会拒绝真正不支持的形态）。
        """
        from src.features.edit.core.timeline_gpu_export_renderer import (
            GpuYuvSource,
        )
        from src.shared.media.gpu_frame import GpuDecodedFrame

        if isinstance(source, GpuYuvSource):
            try:
                return int(source.luma_bits)
            except (TypeError, ValueError):
                return 8
        if isinstance(source, GpuDecodedFrame):
            try:
                return int(source.bit_depth)
            except (TypeError, ValueError):
                return 8
        return 8

    @_serialize_renderer_call
    def render_layer_stack(
        self,
        layers: list[Any],
        out_width: int,
        out_height: int,
        output_u16: bool = False,
        dip_color: Optional[tuple[float, float, float]] = None,
        dip_factor: float = 0.0,
        output_yuv420p: bool = False,
        output_yuv422p10: bool = False,
        output_surface: Optional[GpuEncodeSurface] = None,
    ) -> Optional[Any]:
        """Fuse timeline layers, including all GPU two-source transitions.

        When ``output_surface`` is supplied, the final 4:2:0 conversion writes
        directly to its VideoToolbox IOSurface-backed Metal textures. The
        existing packed/planar readback behavior remains unchanged otherwise.
        """
        from src.features.edit.core.timeline_gpu_export_renderer import (
            GpuWipeElement,
            GpuYuv420Output, GpuYuv422P10Output,
            GpuYuvSource,
        )
        from src.shared.media.gpu_frame import GpuDecodedFrame

        self.last_fallback_reason = None
        # AUD-18602：每帧 CPU 上传字节计数在入口重置。
        self._frame_cpu_upload_bytes = 0
        if output_u16 and output_yuv420p:
            self._fallback("high_precision_yuv420_output")
            return None
        if output_yuv422p10 and (out_width % 2 or out_height <= 0):
            self._fallback("odd_yuv422p10_output_dimensions")
            return None
        if not layers:
            self._fallback("empty_layer_stack")
            return None
        if out_width <= 0 or out_height <= 0:
            self._fallback("invalid_output_dimensions")
            return None
        if output_yuv420p and (out_width % 2 or out_height % 2):
            self._fallback("odd_yuv420p_output_dimensions")
            return None
        # AUD-18601：层栈合成工作集是 8-bit 码值域；>8-bit 源的高精度交付
        # （u16/422P10）不得在此链路静默降精度——fail-closed 回落
        # GL RGB16UI / CPU BGR16（8-bit 源与 8-bit 输出不受影响）。
        if output_u16 or output_yuv422p10:
            from src.features.edit.core.timeline_gpu_export_renderer import (
                GpuWipeElement,
            )
            for element in layers:
                if isinstance(element, GpuWipeElement):
                    stack_sources = (element.left, element.right)
                else:
                    stack_sources = (element[0] if element else None,)
                for source in stack_sources:
                    if self._stack_source_bit_depth(source) > 8:
                        self._fallback("metal_8bit_chain_high_depth_source")
                        return None
        if not self._ensure_ready():
            if self.last_fallback_reason is None:
                self._fallback("metal_renderer_unavailable")
            return None

        ctx = self._context
        assert ctx is not None
        output_surface_textures: Optional[tuple[Any, Any]] = None
        if output_surface is not None:
            if not output_yuv420p:
                self._fallback("encode_surface_requires_yuv420_output")
                return None
            output_surface_textures = self._resolve_surface_yuv420_textures(
                output_surface, int(out_width), int(out_height))
            if output_surface_textures is None:
                return None
        try:
            accumulator_bytes = int(out_width) * int(out_height) * 16
            accumulators = (
                self._private_buffer("stack_accumulator_0", accumulator_bytes),
                self._private_buffer("stack_accumulator_1", accumulator_bytes),
            )
            layer_plan: list[tuple[str, Any, Optional[str]]] = []
            # Allocate the CPU black placeholder only for a genuinely
            # one-sided transition. Normal two-source transitions never need
            # a host-side pixel buffer at this stage.
            black_side: Optional[np.ndarray] = None
            for element in layers:
                if isinstance(element, GpuWipeElement):
                    for side_name, source, grade in (
                        ("left", element.left, element.left_grade),
                        ("right", element.right, element.right_grade),
                    ):
                        if source is None:
                            if black_side is None:
                                black_side = np.zeros(
                                    (out_height, out_width, 3), dtype=np.uint8)
                            side_source = black_side
                        else:
                            side_source = source
                        if side_name == "left":
                            side_effects = element.left_effects
                            side_transform = element.left_transform
                            side_transform_dims = element.left_transform_dims
                            side_crop = element.left_crop
                        else:
                            side_effects = element.right_effects
                            side_transform = element.right_transform
                            side_transform_dims = element.right_transform_dims
                            side_crop = element.right_crop
                        side_element = (
                            side_source, grade, 1.0, 0, side_effects,
                            side_transform, side_transform_dims, side_crop,
                        )
                        layer_plan.append(("wipe_side", side_element, side_name))
                    layer_plan.append(("wipe", element, None))
                else:
                    layer_plan.append(("layer", element, None))

            for key in tuple(self._buffers):
                if key.startswith("stack_layer_"):
                    try:
                        old_index = int(key.split("_")[2])
                    except (IndexError, ValueError):
                        continue
                    if old_index >= len(layer_plan):
                        del self._buffers[key]

            source_slots: dict[int, str] = {}
            normalized_sources: dict[int, tuple[Any, ...]] = {}
            next_source_slot = 0
            main_accumulator = 0
            composited_layers = 0
            for layer_index, (element_kind, payload, side_name) in enumerate(
                    layer_plan):
                if element_kind == "wipe":
                    wipe = payload
                    params = np.zeros(1, dtype=_WIPE_BLEND_PARAMS_DTYPE)
                    params["dims"] = (out_width, out_height, 0, 0)
                    params["control"] = (
                        max(0.0, min(1.0, float(wipe.opacity))),
                        max(0.0, min(1.0, float(wipe.left_opacity))),
                        max(0.0, min(1.0, float(wipe.right_opacity))),
                        1.0 if composited_layers == 0 else 0.0,
                    )
                    spec = wipe.mask_spec
                    params["transition"] = (
                        float(getattr(spec, "transition_mode", 0))
                        if spec is not None else 0.0,
                        float(getattr(spec, "progress", 0.0))
                        if spec is not None else 0.0,
                        0.0,
                        0.0,
                    )
                    next_accumulator = 1 - main_accumulator
                    # AUD-18600：一帧内多个转场的 CPU 上传缓冲必须按
                    # layer_index 独立分配。dispatch 只编码不提交（整帧
                    # 共享一个 command buffer，循环结束才 finish 提交），
                    # 而 write_buffer 是对 Shared 存储的立即 CPU memcpy——
                    # 同名缓冲会被下一个转场在提交前复写，先前 dispatch
                    # 执行时读到串台的参数/掩码。stack_layer_ 前缀同时
                    # 挂进既有的按索引剪枝逻辑，plan 缩短时自动释放。
                    wipe_prefix = f"stack_layer_{layer_index}_"
                    wipe_left_buffer = self._private_buffer(
                        "stack_wipe_left", accumulator_bytes)
                    wipe_right_buffer = self._private_buffer(
                        "stack_wipe_right", accumulator_bytes)
                    if wipe.mask_spec is not None:
                        mask_buffer = self._private_buffer(
                            "stack_wipe_mask_gpu",
                            int(out_width) * int(out_height) * 4,
                        )
                        mask_params = self._make_transition_mask_params(
                            wipe.mask_spec, out_width, out_height)
                        mask_params_buffer = self._upload_array(
                            wipe_prefix + "wipe_mask_params", mask_params)
                        ctx.dispatch(
                            self._kernel("timeline_transition_mask"),
                            buffers={0: mask_buffer, 1: mask_params_buffer},
                            threads=(out_width, out_height, 1),
                        )
                        effect_mode = int(getattr(
                            wipe.mask_spec, "effect_mode", 0))
                        if effect_mode or int(getattr(
                                wipe.mask_spec, "kind", 0)) == 4:
                            effect_left_buffer = self._private_buffer(
                                "stack_wipe_effect_left", accumulator_bytes)
                            effect_right_buffer = self._private_buffer(
                                "stack_wipe_effect_right", accumulator_bytes)
                            ctx.dispatch(
                                self._kernel(
                                    "timeline_transition_source_effect"),
                                buffers={
                                    0: wipe_left_buffer,
                                    1: wipe_right_buffer,
                                    2: effect_left_buffer,
                                    3: effect_right_buffer,
                                    4: mask_params_buffer,
                                },
                                threads=(out_width, out_height, 1),
                            )
                            wipe_left_buffer = effect_left_buffer
                            wipe_right_buffer = effect_right_buffer
                    else:
                        mask = np.asarray(wipe.mask, dtype=np.float32)
                        if (mask.shape != (out_height, out_width)
                                or not np.isfinite(mask).all()):
                            self._fallback("invalid_wipe_mask")
                            return None
                        mask_buffer = self._upload_array(
                            wipe_prefix + "wipe_mask", np.ascontiguousarray(mask))
                    ctx.dispatch(
                        self._kernel("timeline_wipe_composite"),
                        buffers={
                            0: accumulators[main_accumulator],
                            1: wipe_left_buffer,
                            2: wipe_right_buffer,
                            3: mask_buffer,
                            4: accumulators[next_accumulator],
                            5: self._upload_array(
                                wipe_prefix + "wipe_params", params),
                        },
                        threads=(out_width, out_height, 1),
                    )
                    main_accumulator = next_accumulator
                    composited_layers += 1
                    continue

                is_wipe_side = element_kind == "wipe_side"
                element = payload
                if not isinstance(element, tuple) or len(element) < 3:
                    self._fallback("unsupported_stack_element")
                    return None
                source, grade_uniforms, opacity, *rest = element
                try:
                    blend_mode_code = int(rest[0]) if rest else 0
                except (TypeError, ValueError, OverflowError):
                    self._fallback("invalid_blend_mode")
                    return None
                if not 0 <= blend_mode_code <= 15:
                    self._fallback("invalid_blend_mode")
                    return None
                raw_effects = rest[1] if len(rest) > 1 else None
                if raw_effects is None:
                    effect_chain: tuple[Dict[str, Any], ...] = ()
                elif isinstance(raw_effects, dict):
                    effect_chain = (raw_effects,)
                elif isinstance(raw_effects, (tuple, list)) and all(
                        isinstance(item, dict) for item in raw_effects):
                    effect_chain = tuple(raw_effects)
                else:
                    self._fallback("invalid_effect_chain")
                    return None
                effect_params = effect_chain[0] if len(effect_chain) == 1 else None
                transform = rest[2] if len(rest) > 2 else None
                transform_dims = rest[3] if len(rest) > 3 else None
                crop_params = rest[4] if len(rest) > 4 else None
                opacity = float(opacity)
                if not np.isfinite(opacity):
                    self._fallback("invalid_layer_opacity")
                    return None
                opacity = max(0.0, min(1.0, opacity))
                is_metal_buffer = False
                metal_source_buffer = None
                source_id = id(source)
                source_info = normalized_sources.get(source_id)
                if source_info is None:
                    is_metal_texture = isinstance(source, GpuDecodedFrame)
                    metal_y_texture = metal_uv_texture = metal_v_texture = None
                    is_topos_metal_texture = False
                    if is_metal_texture:
                        components = {plane.component: plane
                                      for plane in source.planes}
                        pixel_format = str(source.pixel_format).lower()
                        is_topos_metal_texture = pixel_format == "yuv422p10le"
                        required_components = (
                            ("Y", "U", "V") if is_topos_metal_texture
                            else ("Y", "UV"))
                        if (pixel_format not in ("nv12", "p010", "yuv422p10le")
                                or any(name not in components
                                       for name in required_components)
                                or (source.bit_depth not in (8, 10)
                                    if not is_topos_metal_texture
                                    else source.bit_depth != 10)):
                            self._fallback("unsupported_metal_decoded_frame")
                            return None
                        y_plane = components["Y"]
                        uv_plane = components.get("UV")
                        u_plane = components.get("U")
                        v_plane = components.get("V")
                        metal_planes = (
                            (y_plane, u_plane, v_plane)
                            if is_topos_metal_texture
                            else (y_plane, uv_plane))
                        if any(plane is None or plane.api != "metal"
                               or plane.resource is None
                               for plane in metal_planes):
                            self._fallback("non_metal_decoded_frame_planes")
                            return None
                        metal_y_texture = y_plane.resolved_handle()
                        if is_topos_metal_texture:
                            assert u_plane is not None and v_plane is not None
                            metal_uv_texture = u_plane.resolved_handle()
                            metal_v_texture = v_plane.resolved_handle()
                        else:
                            assert uv_plane is not None
                            metal_uv_texture = uv_plane.resolved_handle()
                        if (metal_y_texture is None or metal_uv_texture is None
                                or (is_topos_metal_texture
                                    and metal_v_texture is None)):
                            self._fallback("invalid_metal_decoded_textures")
                            return None
                        source_width, source_height = int(source.width), int(source.height)
                        source_chroma_width = (
                            int(u_plane.width) if is_topos_metal_texture
                            and u_plane is not None else int(uv_plane.width))
                        source_chroma_height = (
                            int(u_plane.height) if is_topos_metal_texture
                            and u_plane is not None else int(uv_plane.height))
                        expected_chroma_width = (
                            (source_width + 1) // 2 if is_topos_metal_texture
                            else source_width // 2)
                        expected_chroma_height = (
                            source_height if is_topos_metal_texture
                            else source_height // 2)
                        chroma_planes_ok = (
                            source_chroma_width == expected_chroma_width
                            and source_chroma_height == expected_chroma_height
                            and (not is_topos_metal_texture
                                 or (u_plane is not None and v_plane is not None
                                     and int(v_plane.width)
                                     == expected_chroma_width
                                     and int(v_plane.height)
                                     == expected_chroma_height)))
                        if (source_width <= 0 or source_height <= 0
                                or int(y_plane.width) != source_width
                                or int(y_plane.height) != source_height
                                or not chroma_planes_ok):
                            self._fallback("invalid_metal_decoded_dimensions")
                            return None
                        bit_depth = int(source.bit_depth)
                        chroma_x, chroma_y = (2, 1) if is_topos_metal_texture else (2, 2)
                        full_range = str(source.color_range).lower() == "full"
                        matrix = str(source.color_matrix or "bt709").lower()
                        if matrix in ("bt2020nc", "bt2020c", "bt2020"):
                            kr, kb = 0.2627, 0.0593
                        elif matrix in ("smpte170m", "bt470bg", "bt470m", "fcc"):
                            kr, kb = 0.2990, 0.1140
                        else:
                            kr, kb = 0.2126, 0.0722
                        packed = np.zeros((1, 1, 3), dtype=np.uint8)
                        y = u = v = np.zeros((1, 1), dtype=np.uint16)
                        is_yuv = True
                    elif isinstance(source, GpuYuvSource):
                        y, u, v = (np.ascontiguousarray(p)
                                   for p in source.planes)
                        if any(p.ndim != 2 or p.dtype not in (np.uint8, np.uint16)
                               for p in (y, u, v)):
                            self._fallback("unsupported_planar_source_format")
                            return None
                        if not 8 <= int(source.luma_bits) <= 16:
                            self._fallback("unsupported_planar_bit_depth")
                            return None
                        if (source.kr <= 0.0 or source.kb <= 0.0
                                or source.kr + source.kb >= 1.0):
                            self._fallback("unsupported_yuv_matrix")
                            return None
                        source_height, source_width = y.shape
                        source_chroma_width, source_chroma_height = u.shape[1], u.shape[0]
                        if source_width <= 0 or source_height <= 0:
                            self._fallback("empty_planar_source")
                            return None
                        y, u, v = (np.ascontiguousarray(p, dtype=np.uint16)
                                   for p in (y, u, v))
                        bit_depth = int(source.luma_bits)
                        chroma_x = int(source.chroma_x_div)
                        chroma_y = int(source.chroma_y_div)
                        full_range = bool(source.full_range)
                        kr, kb = float(source.kr), float(source.kb)
                        packed = np.zeros((1, 1, 3), dtype=np.uint8)
                        is_yuv = True
                    else:
                        packed = np.asarray(source)
                        if (packed.dtype != np.uint8 or packed.ndim != 3
                                or packed.shape[2] != 3):
                            self._fallback("unsupported_packed_source_format")
                            return None
                        packed = np.ascontiguousarray(packed)
                        source_height, source_width = packed.shape[:2]
                        source_chroma_width = source_chroma_height = 1
                        if source_width <= 0 or source_height <= 0:
                            self._fallback("empty_packed_source")
                            return None
                        y = u = v = np.zeros((1, 1), dtype=np.uint16)
                        bit_depth, chroma_x, chroma_y = 8, 1, 1
                        full_range, kr, kb = False, 0.2126, 0.0722
                        is_yuv = False
                    source_info = (
                        packed, y, u, v, source_width, source_height,
                        bit_depth, chroma_x, chroma_y, is_yuv, full_range,
                        kr, kb, is_metal_texture, is_topos_metal_texture,
                        metal_y_texture,
                        metal_uv_texture, metal_v_texture,
                        source_chroma_width,
                        source_chroma_height,
                    )
                    normalized_sources[source_id] = source_info
                else:
                    (packed, y, u, v, source_width, source_height,
                     bit_depth, chroma_x, chroma_y, is_yuv, full_range,
                     kr, kb, is_metal_texture, is_topos_metal_texture,
                     metal_y_texture,
                     metal_uv_texture, metal_v_texture, source_chroma_width,
                     source_chroma_height) = source_info

                prefix = f"stack_layer_{layer_index}_"
                materialize_effect_chain = (
                    self._should_materialize_effect_chain(
                        effect_chain, is_metal_texture)
                )
                if materialize_effect_chain:
                    chain_source_params = self._make_params(
                        source_width, source_height, source_width, source_height,
                        source_chroma_width, source_chroma_height,
                        source_chroma_width, source_chroma_height,
                        bit_depth, chroma_x, chroma_y, is_yuv, full_range,
                        1.0, bool(grade_uniforms), False, kr, kb,
                        grade_uniforms,
                    )
                    chain_image = self._apply_effect_chain(
                        prefix=prefix,
                        source_width=source_width,
                        source_height=source_height,
                        packed_bgr=packed,
                        y_plane=y,
                        u_plane=u,
                        v_plane=v,
                        is_metal_texture=is_metal_texture,
                        metal_y_texture=metal_y_texture,
                        metal_uv_texture=metal_uv_texture,
                        metal_v_texture=metal_v_texture,
                        source_params=chain_source_params,
                        effects=effect_chain,
                    )
                    source = chain_image
                    source_id = id(source)
                    metal_source_buffer = chain_image.buffer
                    is_metal_buffer = True
                    is_metal_texture = False
                    packed = np.zeros((1, 1, 3), dtype=np.uint8)
                    y = u = v = np.zeros((1, 1), dtype=np.uint16)
                    bit_depth, chroma_x, chroma_y = 8, 1, 1
                    is_yuv, full_range = False, False
                    kr, kb = 0.2126, 0.0722
                    source_chroma_width = source_chroma_height = 1
                    grade_uniforms = None
                    effect_params = None

                source_params = self._make_params(
                    source_width, source_height, out_width, out_height,
                    source_chroma_width, source_chroma_height,
                    source_chroma_width, source_chroma_height,
                    bit_depth, chroma_x, chroma_y, is_yuv, full_range,
                    1.0, bool(grade_uniforms), False, kr, kb, grade_uniforms,
                    effect_params,
                )
                source_buffers: dict[int, Any] = {}
                texture_buffers: dict[int, Any] = {}
                if is_metal_texture:
                    texture_buffers = {
                        0: metal_y_texture,
                        1: metal_uv_texture,
                    }
                    if is_topos_metal_texture:
                        texture_buffers[2] = metal_v_texture
                elif is_metal_buffer:
                    dummy_plane = self._upload_array(
                        prefix + "chain_stack_dummy_plane",
                        np.zeros(1, dtype=np.uint16),
                    )
                    source_buffers = {
                        0: metal_source_buffer,
                        1: dummy_plane,
                        2: dummy_plane,
                        3: dummy_plane,
                    }
                else:
                    source_prefix = source_slots.get(source_id)
                    upload_source = source_prefix is None
                    if upload_source:
                        source_prefix = f"stack_source_{next_source_slot}_"
                        source_slots[source_id] = source_prefix
                        next_source_slot += 1
                    source_names = ("bgr", "y", "u", "v")
                    source_arrays = (packed, y, u, v)
                    for slot, (name, array) in enumerate(
                            zip(source_names, source_arrays)):
                        source_buffer_key = source_prefix + name
                        if upload_source:
                            source_buffers[slot] = self._upload_array(
                                source_buffer_key, array)
                        else:
                            source_buffers[slot] = self._buffers[source_buffer_key]
                blend_params = np.zeros(1, dtype=_LAYER_BLEND_PARAMS_DTYPE)
                blend_params["dims"] = (out_width, out_height, 0, 0)
                blend_params["options"] = (blend_mode_code, 0, 0, 0)
                inverse = None
                if transform is not None:
                    if (isinstance(transform_dims, (tuple, list))
                            and len(transform_dims) >= 2):
                        coordinate_width, coordinate_height = (
                            int(transform_dims[0]), int(transform_dims[1]))
                    else:
                        coordinate_width, coordinate_height = out_width, out_height
                    inverse = self._inverse_transform_affine(
                        transform, out_width, out_height,
                        coordinate_width, coordinate_height)
                crop_enabled = False
                if crop_params is not None:
                    from src.features.edit.core.crop_cpu import crop_is_identity
                    if not crop_is_identity(tuple(crop_params)):
                        try:
                            crop_values = tuple(float(value)
                                                for value in crop_params[:5])
                        except (TypeError, ValueError, IndexError):
                            self._fallback("invalid_crop_matte")
                            return None
                        if (len(crop_values) != 5
                                or not all(np.isfinite(value)
                                           for value in crop_values)):
                            self._fallback("invalid_crop_matte")
                            return None
                        if len(crop_params) < 5:
                            self._fallback("invalid_crop_matte")
                            return None
                        blend_params["crop"] = crop_values[:4]
                        blend_params["geometry"][0, 0] = max(
                            crop_values[4], 0.0)
                        crop_enabled = True
                blend_params["control"] = (
                    opacity, 1.0 if is_wipe_side or composited_layers == 0 else 0.0,
                    1.0 if inverse is not None else 0.0,
                    1.0 if crop_enabled else 0.0)
                if inverse is not None:
                    blend_params["inverse_x"] = (
                        inverse[0], inverse[1], inverse[2], 0.0)
                    blend_params["inverse_y"] = (
                        inverse[3], inverse[4], inverse[5], 0.0)
                target_index = 1 - main_accumulator
                output_buffer = (
                    self._private_buffer(
                        f"stack_wipe_{side_name}", accumulator_bytes)
                    if is_wipe_side else accumulators[target_index]
                )
                source_params_buffer = self._upload_array(
                    prefix + "source_params", source_params)
                blend_params_buffer = self._upload_array(
                    prefix + "blend_params", blend_params)
                buffers = {
                    **source_buffers,
                    4: accumulators[main_accumulator],
                    5: output_buffer,
                    6: source_params_buffer,
                    7: blend_params_buffer,
                }
                separable_effect = bool(
                    effect_params is not None
                    and (
                        (
                            int(round(float(effect_params.get("kind", 0.0))))
                            not in (6, 7, 8, 9, 10, 11)
                            and max(float(effect_params.get("half_x", 0.0)),
                                    float(effect_params.get("half_y", 0.0))) > 5.0
                        )
                        or (
                            int(round(float(effect_params.get("kind", 0.0)))) == 5
                            and max(float(effect_params.get("half_x", 0.0)),
                                    float(effect_params.get("half_y", 0.0))) > 1.0
                        )
                    )
                )
                if separable_effect:
                    horizontal_buffer = self._private_buffer(
                        "stack_effect_horizontal",
                        int(source_width) * int(source_height) * 8,
                    )
                    weights_x_buffer = self._upload_array(
                        prefix + "effect_weights_x",
                        self._effect_axis_weights(effect_params, "x"),
                    )
                    weights_y_buffer = self._upload_array(
                        prefix + "effect_weights_y",
                        self._effect_axis_weights(effect_params, "y"),
                    )
                    horizontal_buffers = {
                        **source_buffers,
                        4: horizontal_buffer,
                        5: source_params_buffer,
                        6: weights_x_buffer,
                    }
                    ctx.dispatch(
                        self._kernel(
                            "timeline_effect_horizontal_texture"
                            if is_metal_texture else "timeline_effect_horizontal"),
                        buffers=horizontal_buffers,
                        textures=texture_buffers,
                        threads=(source_width, source_height, 1),
                    )
                    buffers[8] = horizontal_buffer
                    buffers[9] = weights_y_buffer
                ctx.dispatch(
                    self._kernel(
                        ("timeline_stack_layer_texture_effect"
                         if is_metal_texture else "timeline_stack_layer_effect")
                        if separable_effect else
                        ("timeline_stack_layer_texture_planar"
                         if is_topos_metal_texture else
                         "timeline_stack_layer_texture")
                        if is_metal_texture else "timeline_stack_layer"),
                    buffers=buffers,
                    textures=texture_buffers,
                    threads=(out_width, out_height, 1),
                )
                if not is_wipe_side:
                    main_accumulator = target_index
                    composited_layers += 1

            for key in tuple(self._buffers):
                if key.startswith("stack_source_"):
                    try:
                        old_slot = int(key.split("_")[2])
                    except (IndexError, ValueError):
                        continue
                    if old_slot >= next_source_slot:
                        del self._buffers[key]

            if dip_color is None:
                normalized_dip = (0.0, 0.0, 0.0)
                dip_factor = 0.0
            else:
                if len(dip_color) != 3:
                    self._fallback("invalid_dip_color")
                    return None
                normalized_dip = tuple(float(c) for c in dip_color)
                if not all(np.isfinite(c) for c in normalized_dip):
                    self._fallback("invalid_dip_color")
                    return None
                dip_factor = max(0.0, min(1.0, float(dip_factor)))
            finalize_params = np.zeros(1, dtype=_STACK_FINALIZE_PARAMS_DTYPE)
            finalize_params["dims"] = (out_width, out_height, 0, 0)
            finalize_params["control"] = (
                dip_factor, 1.0 if output_yuv420p else 0.0, 0.0, 0.0)
            finalize_params["dip_color"][0, :3] = normalized_dip
            y422_c_size = (int(out_width) // 2) * int(out_height)
            output_size = (
                out_width * out_height * 3 * (2 if output_u16 else 1)
                if not output_yuv420p and not output_yuv422p10
                else (out_width * out_height + 2 * y422_c_size) * 2
                if output_yuv422p10
                else out_width * out_height * 3 // 2
            )
            finalize_params_buffer = self._upload_array(
                "stack_finalize_params", finalize_params)
            if output_surface_textures is not None:
                surface_format = str(
                    output_surface.descriptor.pixel_format  # type: ignore[union-attr]
                ).lower()
                ctx.dispatch(
                    self._kernel(
                        "timeline_finalize_stack_p010_surface"
                        if surface_format == "p010" else
                        "timeline_finalize_stack_yuv420_surface"),
                    buffers={
                        0: accumulators[main_accumulator],
                        1: finalize_params_buffer,
                    },
                    textures={
                        0: output_surface_textures[0],
                        1: output_surface_textures[1],
                    },
                    threads=(out_width, out_height, 1),
                )
                ctx.finish()
                self._record_render_completion(
                    ctx, 0, yuv_output=True, readback=False)
                self.frames_rendered += 1
                return output_surface
            final_output = self._buffer("stack_output", output_size)
            ctx.dispatch(
                self._kernel(
                    "timeline_finalize_stack_yuv422p10"
                    if output_yuv422p10 else
                    "timeline_finalize_stack_u16"
                    if output_u16 else "timeline_finalize_stack"),
                buffers={
                    0: accumulators[main_accumulator],
                    1: final_output,
                    2: finalize_params_buffer,
                },
                threads=(out_width, out_height, 1),
            )
            ctx.finish()
            self._record_render_completion(
                ctx,
                output_size,
                yuv_output=bool(output_yuv420p or output_yuv422p10),
            )
            data = ctx.read_buffer(final_output, output_size)
            if output_yuv420p:
                y_size = out_width * out_height
                chroma_size = y_size // 4
                result = GpuYuv420Output((
                    data[:y_size].reshape(out_height, out_width).copy(),
                    data[y_size:y_size + chroma_size].reshape(
                        out_height // 2, out_width // 2).copy(),
                    data[y_size + chroma_size:].reshape(
                        out_height // 2, out_width // 2).copy(),
                ))
            elif output_yuv422p10:
                values = data.view(np.uint16)
                result = GpuYuv422P10Output((
                    values[:out_width * out_height].reshape(
                        out_height, out_width).copy(),
                    values[out_width * out_height:
                           out_width * out_height + y422_c_size].reshape(
                        out_height, out_width // 2).copy(),
                    values[out_width * out_height + y422_c_size:].reshape(
                        out_height, out_width // 2).copy(),
                ))
            elif output_u16:
                result = data.view(np.uint16).reshape(
                    out_height, out_width, 3).copy()
            else:
                result = data.reshape(out_height, out_width, 3).copy()
            self.frames_rendered += 1
            return result
        except Exception as error:  # noqa: BLE001 — caller falls back to CPU
            self.metal_errors += 1
            self._record_fallback("metal_render_error")
            if self.metal_errors == 1:
                logger.warning(
                    "Metal timeline stack render failed; using CPU fallback: %s",
                    error,
                )
            return None
        finally:
            # Also submit/release any partially encoded work when a late input
            # validation failure returns None, before shared buffers are reused.
            try:
                ctx.finish()
            except Exception:
                logger.debug("Metal stack cleanup finish failed", exc_info=True)

    def _render(
        self,
        source: Any,
        grade_uniforms: Optional[Dict[str, Any]],
        out_width: int,
        out_height: int,
        opacity: float,
        output_u16: bool,
        output_yuv420p: bool,
        output_yuv422p10: bool = False,
    ) -> Optional[Any]:
        self.last_fallback_reason = None
        # AUD-18602：每帧 CPU 上传字节计数在入口重置。
        self._frame_cpu_upload_bytes = 0
        if output_u16 and output_yuv420p:
            self._fallback("high_precision_yuv420_output")
            return None
        if output_yuv422p10 and (out_width % 2 or out_height <= 0):
            self._fallback("odd_yuv422p10_output_dimensions")
            return None
        if out_width <= 0 or out_height <= 0:
            self._fallback("invalid_output_dimensions")
            return None
        if output_yuv420p and (out_width % 2 or out_height % 2):
            self._fallback("odd_yuv420p_output_dimensions")
            return None
        if not self._ensure_ready():
            if self.last_fallback_reason is None:
                self._fallback("metal_renderer_unavailable")
            return None
        try:
            from src.features.edit.core.timeline_gpu_export_renderer import (
                GpuYuv420Output, GpuYuv422P10Output, GpuYuvSource,
            )
            if isinstance(source, GpuYuvSource):
                y, u, v = (np.ascontiguousarray(p) for p in source.planes)
                if y.ndim != 2 or u.ndim != 2 or v.ndim != 2:
                    self._fallback("unsupported_planar_dimensions")
                    return None
                if any(p.dtype not in (np.uint8, np.uint16) for p in (y, u, v)):
                    self._fallback("unsupported_planar_dtype")
                    return None
                if source.luma_bits < 8 or source.luma_bits > 16:
                    self._fallback("unsupported_planar_bit_depth")
                    return None
                if source.kr <= 0.0 or source.kb <= 0.0 \
                        or source.kr + source.kb >= 1.0:
                    self._fallback("unsupported_yuv_matrix")
                    return None
                sh, sw = y.shape
                if sh <= 0 or sw <= 0:
                    self._fallback("empty_planar_source")
                    return None
                y = np.ascontiguousarray(y, dtype=np.uint16)
                u = np.ascontiguousarray(u, dtype=np.uint16)
                v = np.ascontiguousarray(v, dtype=np.uint16)
                packed = np.zeros((1, 1, 3), dtype=np.uint8)
                bit_depth = int(source.luma_bits)
                chroma_x = int(source.chroma_x_div)
                chroma_y = int(source.chroma_y_div)
                is_yuv = True
                full_range = bool(source.full_range)
                kr, kb = float(source.kr), float(source.kb)
                # AUD-18601：合成工作集是 8-bit 码值域（prepare_*_source_pixel
                # 出口 to_u8），>8-bit 源的高精度交付（u16/422P10）在此链路
                # 静默退化为 8-bit 有效精度——fail-closed 回落 GL RGB16UI /
                # CPU BGR16 路径（镜像 GL 侧 ">8-bit 源走全链" 契约）。
                if (output_u16 or output_yuv422p10) and bit_depth > 8:
                    self._fallback("metal_8bit_chain_high_depth_source")
                    return None
            else:
                packed = np.asarray(source)
                if packed.dtype != np.uint8 or packed.ndim != 3 \
                        or packed.shape[2] != 3:
                    self._fallback("unsupported_packed_source_format")
                    return None
                packed = np.ascontiguousarray(packed)
                sh, sw = packed.shape[:2]
                if sw <= 0 or sh <= 0:
                    self._fallback("empty_packed_source")
                    return None
                y = u = v = np.zeros((1, 1), dtype=np.uint16)
                bit_depth, chroma_x, chroma_y = 8, 1, 1
                is_yuv = False
                full_range = False
                kr, kb = 0.2126, 0.0722

            params = self._make_params(
                sw, sh, int(out_width), int(out_height),
                u.shape[1], u.shape[0], v.shape[1], v.shape[0],
                bit_depth, chroma_x, chroma_y, is_yuv, full_range,
                float(opacity), bool(grade_uniforms), output_yuv420p,
                kr, kb, grade_uniforms,
                output_yuv422p10=output_yuv422p10,
            )
            ctx = self._context
            assert ctx is not None
            bgr_buf = self._upload_array("bgr", packed)
            y_buf = self._upload_array("y", y)
            u_buf = self._upload_array("u", u)
            v_buf = self._upload_array("v", v)
            param_buf = self._upload_array("params", params)
            y_size = out_width * out_height
            c_size = y_size // 4
            y422_c_size = (int(out_width) // 2) * int(out_height)
            output_size = (
                (y_size + 2 * c_size)
                if output_yuv420p else
                (y_size + 2 * y422_c_size) * 2
                if output_yuv422p10 else y_size * 3 * (2 if output_u16 else 1)
            )
            output_buf = self._buffer("output", output_size)
            ctx.dispatch(
                self._kernel(
                    "timeline_fused_export_yuv422p10"
                    if output_yuv422p10 else
                    "timeline_fused_export_u16"
                    if output_u16 else "timeline_fused_export"),
                buffers={0: bgr_buf, 1: y_buf, 2: u_buf, 3: v_buf,
                         4: output_buf, 5: param_buf},
                threads=(out_width, out_height, 1),
            )
            if output_yuv420p:
                ctx.finish()
                self._record_render_completion(
                    ctx,
                    output_size,
                    yuv_output=True,
                    cpu_upload_bytes=(packed.nbytes + y.nbytes + u.nbytes + v.nbytes),
                )
                data = ctx.read_buffer(output_buf, output_size)
                y_plane = data[:y_size].reshape(out_height, out_width).copy()
                u_plane = data[y_size:y_size + c_size].reshape(
                    out_height // 2, out_width // 2).copy()
                v_plane = data[y_size + c_size:y_size + 2 * c_size].reshape(
                    out_height // 2, out_width // 2).copy()
                result = GpuYuv420Output((y_plane, u_plane, v_plane))
            elif output_yuv422p10:
                ctx.finish()
                self._record_render_completion(
                    ctx,
                    output_size,
                    yuv_output=True,
                    cpu_upload_bytes=(packed.nbytes + y.nbytes + u.nbytes + v.nbytes),
                )
                data = ctx.read_buffer(output_buf, output_size)
                values = data.view(np.uint16)
                result = GpuYuv422P10Output((
                    values[:y_size].reshape(out_height, out_width).copy(),
                    values[y_size:y_size + y422_c_size].reshape(
                        out_height, out_width // 2).copy(),
                    values[y_size + y422_c_size:].reshape(
                        out_height, out_width // 2).copy(),
                ))
            else:
                ctx.finish()
                self._record_render_completion(
                    ctx,
                    output_size,
                    yuv_output=False,
                    cpu_upload_bytes=(packed.nbytes + y.nbytes + u.nbytes + v.nbytes),
                )
                data = ctx.read_buffer(output_buf, output_size)
                if output_u16:
                    result = data.view(np.uint16).reshape(
                        out_height, out_width, 3).copy()
                else:
                    result = data.reshape(out_height, out_width, 3).copy()
            self.frames_rendered += 1
            return result
        except Exception as error:  # noqa: BLE001 — caller logs CPU fallback
            self.metal_errors += 1
            self._record_fallback("metal_render_error")
            if self.metal_errors == 1:
                logger.warning("Metal timeline render failed; using CPU fallback: %s", error)
            return None

    def _buffer(self, name: str, size: int) -> Any:
        """Return a reusable shared MTLBuffer with at least ``size`` bytes."""
        if self._context is None:
            raise RuntimeError("Metal context is not initialized")
        existing = self._buffers.get(name)
        if existing is None or int(existing.length()) < int(size):
            existing = self._context.create_buffer(int(size))
            self._buffers[name] = existing
        return existing

    def _private_buffer(self, name: str, size: int) -> Any:
        """Return reusable GPU-only storage for render-pass intermediates."""
        if self._context is None:
            raise RuntimeError("Metal context is not initialized")
        existing = self._buffers.get(name)
        if existing is None or int(existing.length()) < int(size):
            existing = self._context.create_private_buffer(int(size))
            self._buffers[name] = existing
        return existing

    def _upload_array(self, name: str, array: np.ndarray) -> Any:
        """Copy one CPU-decoded source/parameter array into a reusable buffer.

        AUD-18602：每次 write_buffer 的 CPU memcpy 字节数计入
        ``_frame_cpu_upload_bytes``（公开渲染入口重置），供
        ``_record_render_completion`` 的 full-GPU 证据使用。
        """
        data = np.ascontiguousarray(array)
        buffer = self._buffer(name, data.nbytes)
        if self._context is None:
            raise RuntimeError("Metal context is not initialized")
        # 参数/CPU staging 必须走显式写入 API。通过 read_buffer() 取得
        # ndarray 再原地修改会把上传伪装成一次 GPU→CPU 读回，且在
        # Managed storage 上没有明确的 didModifyRange 边界。
        self._context.write_buffer(buffer, data)
        self._frame_cpu_upload_bytes += int(data.nbytes)
        return buffer

    @staticmethod
    def _make_transition_mask_params(
        spec: Any, width: int, height: int,
    ) -> np.ndarray:
        """Pack a procedural mask spec without allocating a pixel mask."""
        params = np.zeros(1, dtype=_TRANSITION_MASK_PARAMS_DTYPE)
        vertices = tuple(getattr(spec, "vertices", ()) or ())[:48]
        params["dims"] = (int(width), int(height), len(vertices), 0)
        params["control"] = (
            float(getattr(spec, "progress", 0.0)),
            float(getattr(spec, "angle", 0.0)),
            float(getattr(spec, "softness", 0.02)),
            1.0 if bool(getattr(spec, "reverse", False)) else 0.0,
        )
        params["options"] = (
            int(getattr(spec, "kind", 0)),
            int(getattr(spec, "direction", 0)),
            len(vertices),
            int(getattr(spec, "effect_mode", 0)),
        )
        transition_mode = int(getattr(spec, "transition_mode", 0))
        effect_mode = int(getattr(spec, "effect_mode", 0))
        radius = float(getattr(
            spec, "blur_ratio" if transition_mode or not effect_mode
            else "blur_radius", 0.045))
        strength = float(getattr(
            spec, "split_amount" if effect_mode == 5 else "blur_strength",
            0.045 if effect_mode == 5 else 0.18))
        params["effect_control"] = (
            radius,
            float(getattr(spec, "blur_mix", 0.0)),
            float(getattr(spec, "shutter", 1.0)),
            strength,
        )
        params["effect_extra"] = (
            float(getattr(spec, "spin_angle", 18.0)),
            float(getattr(spec, "split_amount", 0.045)),
            float(getattr(spec, "split_mix", 0.96)),
            float(transition_mode),
        )
        for index, vertex in enumerate(vertices):
            params["vertices"][0, index] = (
                float(vertex[0]), float(vertex[1]))
        return params

    @staticmethod
    def _inverse_transform_affine(
        transform: Any,
        viewport_width: int,
        viewport_height: int,
        coordinate_width: int,
        coordinate_height: int,
    ) -> Optional[tuple[float, float, float, float, float, float]]:
        """Pack the inverse map used by OpenCV's viewport-space warpAffine."""
        from src.features.edit.core.adjustment_composite import (
            transform_is_identity,
        )

        if not transform or transform_is_identity(transform):
            return None
        try:
            if len(transform) != 8:
                return None
            tx, ty, sx, sy, rotation = (
                float(transform[index]) for index in range(5))
            pivot_mode = str(transform[5])
            pivot_x, pivot_y = float(transform[6]), float(transform[7])
        except (TypeError, ValueError, IndexError):
            # apply_transform_cpu treats malformed values as an identity.
            return None
        if not all(np.isfinite(value) for value in
                   (tx, ty, sx, sy, rotation, pivot_x, pivot_y)):
            return None
        coordinate_width = max(1, int(coordinate_width or viewport_width))
        coordinate_height = max(1, int(coordinate_height or viewport_height))
        scale_x = viewport_width / float(coordinate_width)
        scale_y = viewport_height / float(coordinate_height)
        if pivot_mode == "custom":
            anchor_x, anchor_y = pivot_x * scale_x, pivot_y * scale_y
        else:
            anchor_x, anchor_y = viewport_width / 2.0, viewport_height / 2.0
        radians = np.deg2rad(rotation)
        cos_r, sin_r = float(np.cos(radians)), float(np.sin(radians))
        m00, m01 = cos_r * sx, -sin_r * sy
        m10, m11 = sin_r * sx, cos_r * sy
        m02 = anchor_x - m00 * anchor_x - m01 * anchor_y + tx * scale_x
        m12 = anchor_y - m10 * anchor_x - m11 * anchor_y + ty * scale_y
        try:
            import cv2
            forward = np.asarray(
                ((m00, m01, m02), (m10, m11, m12)), dtype=np.float32)
            inverse = cv2.invertAffineTransform(forward)
        except Exception:
            forward = np.asarray(
                ((m00, m01, m02), (m10, m11, m12)), dtype=np.float64)
            try:
                inverse = np.linalg.inv(
                    np.vstack((forward, (0.0, 0.0, 1.0))))[:2]
            except np.linalg.LinAlgError:
                return None
        values = tuple(float(value) for value in inverse.reshape(-1))
        return values if all(np.isfinite(value) for value in values) else None

    @staticmethod
    def _make_params(
        source_width: int,
        source_height: int,
        output_width: int,
        output_height: int,
        u_width: int,
        u_height: int,
        v_width: int,
        v_height: int,
        bit_depth: int,
        chroma_x: int,
        chroma_y: int,
        is_yuv: bool,
        full_range: bool,
        opacity: float,
        has_grade: bool,
        output_yuv420p: bool,
        kr: float,
        kb: float,
        grade: Optional[Dict[str, Any]],
        effect: Optional[Dict[str, Any]] = None,
        output_yuv422p10: bool = False,
    ) -> np.ndarray:
        values = grade or {}
        params = np.zeros(1, dtype=_PARAM_DTYPE)
        params["source_output_dims"] = (
            source_width, source_height, output_width, output_height)
        params["chroma_dims"] = (u_width, u_height, v_width, v_height)
        params["source_flags"] = (
            bit_depth, chroma_x, chroma_y, 1 if is_yuv else 0)
        params["control"] = (
            opacity, 1.0 if has_grade else 0.0,
            2.0 if output_yuv422p10 else (1.0 if output_yuv420p else 0.0),
            1.0 if full_range else 0.0)
        for name, default in (("lift", (1.0, 1.0, 1.0)),
                              ("gamma", (1.0, 1.0, 1.0)),
                              ("gain", (1.0, 1.0, 1.0))):
            raw = np.asarray(values.get(f"u_{name}", default), dtype=np.float32).reshape(-1)
            if raw.size != 3:
                raise ValueError(f"Metal grade {name} must have 3 channels")
            params[name][0, :3] = raw
        scalar_names = (
            "u_exposure", "u_temp", "u_tint", "u_contrast", "u_shadow",
            "u_highlights", "u_saturation", "u_vibrance", "u_hue_shift",
        )
        scalars = [float(values.get(name, 0.0) or 0.0) for name in scalar_names]
        params["grade_a"] = (*scalars[:4],)
        params["grade_b"] = (*scalars[4:8],)
        params["grade_c"][0, 0] = scalars[8]
        params["yuv"] = (kr, kb, 0.0, 0.0)
        if effect is not None:
            effect_kind = int(round(float(effect["kind"])))
            params["effect"] = (
                float(effect["kind"]), float(effect["half_x"]),
                float(effect["half_y"]), float(effect["strength"]),
            )
            sigma_x = float(effect.get("sigma_x", 1.0))
            sigma_y = float(effect.get("sigma_y", 1.0))
            if effect_kind in (1, 2):
                params["effect_sigma"] = (
                    sigma_x, sigma_y,
                    float(effect.get("edge_policy", 0.0)), 0.0,
                )
                params["effect_extra"] = (0.0, 0.0, 0.0, 0.0)
            elif effect_kind == 3:
                params["effect_sigma"] = (sigma_x, sigma_y, 0.0, 0.0)
                params["effect_extra"] = (
                    0.0, 0.0, float(effect.get("threshold", 0.0)),
                    float(effect.get("channels", 0.0)),
                )
            elif effect_kind == 5:
                # Erode/dilate uses exact min/max with the selected border
                # policy encoded in effect_sigma.z.
                params["effect_sigma"] = (
                    sigma_x, sigma_y,
                    float(effect.get("edge_policy", 0.0)), 0.0,
                )
                params["effect_extra"] = (
                    float(effect.get("operation", 0.0)), 0.0, 0.0, 0.0,
                )
            elif effect_kind == 6:
                # Edge Detect's bounded Sobel path uses the threshold in x,
                # direction/output-mode indices in z/w, and invert in extra.x.
                params["effect_sigma"] = (
                    float(effect.get("sigma_x", 0.1)),
                    float(effect.get("sigma_y", 0.3)),
                    float(effect.get("direction", 0.0)),
                    float(effect.get("output_mode", 0.0)),
                )
                params["effect_extra"] = (
                    float(effect.get("invert", 0.0)),
                    float(effect.get("channel", 0.0)),
                    float(effect.get(
                        "method",
                        1.0 if effect.get("canny", 0.0) else 0.0,
                    )),
                    float(effect.get("preblur", 0.0)),
                )
            elif effect_kind in (7, 9):
                params["effect_sigma"] = (
                    float(effect.get("spatial_sigma", sigma_x)),
                    float(effect.get("range_sigma", sigma_y)),
                    0.0, 0.0,
                )
                if effect_kind == 9:
                    params["effect_extra"] = (
                        float(effect.get("channel_r", 1.0)),
                        float(effect.get("channel_g", 1.0)),
                        float(effect.get("channel_b", 1.0)), 0.0,
                    )
                else:
                    params["effect_extra"] = (
                        float(effect.get("luminance_only", 0.0)),
                        float(effect.get("chroma_only", 0.0)),
                        0.0, 0.0,
                    )
            elif effect_kind == 8:
                params["effect_sigma"] = (
                    float(effect.get("focal_x", 0.5)),
                    float(effect.get("focal_y", 0.5)),
                    float(effect.get("focal_size", 0.3)),
                    float(effect.get("max_radius", 20.0)),
                )
                params["effect_extra"] = (
                    float(effect.get("edge_policy", 0.0)),
                    float(effect.get("mode", 0.0)),
                    float(effect.get("process_alpha", 1.0)),
                    0.0,
                )
            elif effect_kind == 10:
                angle = float(effect.get("sigma_x", 0.0))
                params["effect_sigma"] = (
                    math.cos(angle),
                    math.sin(angle),
                    float(effect.get("edge_policy", 0.0)),
                    float(effect.get("sample_count", 16.0)),
                )
                params["effect_extra"] = (
                    float(effect.get("channel_r", 1.0)),
                    float(effect.get("channel_g", 1.0)),
                    float(effect.get("channel_b", 1.0)), 0.0,
                )
            elif effect_kind == 11:
                params["effect_sigma"] = (
                    float(effect.get("anamorphic", 0.0)),
                    float(effect.get("highlight_gain", 0.0)),
                    float(effect.get("edge_policy", 0.0)),
                    float(effect.get("sample_count", 16.0)),
                )
                params["effect_extra"] = (
                    float(effect.get("channel_r", 1.0)),
                    float(effect.get("channel_g", 1.0)),
                    float(effect.get("channel_b", 1.0)),
                    float(effect.get("aperture_blades", 0.0)),
                )
            else:
                params["effect_sigma"] = (
                    sigma_x, sigma_y,
                    float(effect.get("tint_r", 1.0)),
                    float(effect.get("tint_g", 0.85)),
                )
                params["effect_extra"] = (
                    float(effect.get("threshold", 0.0)),
                    float(effect.get("intensity", 0.0)),
                    float(effect.get("tint_b", 0.7)), 0.0,
                )
        return params

    @staticmethod
    def _effect_axis_weights(
        effect: Dict[str, Any], axis: str,
    ) -> np.ndarray:
        """Build normalized 1D convolution weights for a separable pass."""
        half_size = int(round(float(effect[f"half_{axis}"])))
        kind = int(round(float(effect["kind"])))
        weights = np.zeros(65, dtype=np.float32)
        if kind in (5, 10, 11):
            # These paths do not consume separable convolution coefficients.
            # Morphology can have a large schema radius; reserving the fixed
            # 65-tap table for it would truncate support above radius 32.
            return weights
        offsets = np.arange(-half_size, half_size + 1, dtype=np.float32)
        if kind in (6, 8):
            axis_weights = np.zeros(offsets.shape, dtype=np.float32)
            axis_weights[half_size] = 1.0
        elif kind in (7, 9):
            sigma = max(float(effect.get("spatial_sigma", 1.0)), 0.01)
            axis_weights = np.exp(
                -(offsets * offsets) / (2.0 * sigma * sigma)
            ).astype(np.float32)
        elif kind == 1:
            axis_weights = np.ones(offsets.shape, dtype=np.float32)
        elif kind == 2 and half_size <= 4:
            # Match OpenCV GaussianBlur(..., sigmaX=0), including its fixed
            # kernels for small radii. Metal's direct kernel uses the same
            # coefficients in effect_axis_weight().
            small_kernels = {
                0: (1.0,),
                1: (0.25, 0.5, 0.25),
                2: (0.0625, 0.25, 0.375, 0.25, 0.0625),
                3: (0.03125, 0.109375, 0.21875, 0.28125,
                    0.21875, 0.109375, 0.03125),
                4: (0.015625, 0.05078125, 0.1171875, 0.19921875,
                    0.234375, 0.19921875, 0.1171875, 0.05078125,
                    0.015625),
            }
            axis_weights = np.asarray(
                small_kernels[half_size], dtype=np.float32)
        else:
            sigma = max(float(effect[f"sigma_{axis}"]), 0.01)
            axis_weights = np.exp(
                -(offsets * offsets) / (2.0 * sigma * sigma)
            ).astype(np.float32)
        total = float(axis_weights.sum())
        if total <= 0.0 or not np.isfinite(total):
            raise ValueError("Metal effect weights must have a finite positive sum")
        axis_weights /= total
        weights[32 - half_size:33 + half_size] = axis_weights
        return weights

    @_serialize_renderer_call
    def release(self) -> None:
        if self._released:
            return
        if self._context is not None:
            try:
                self._context.finish()
            except Exception:
                logger.debug("Metal timeline context finish during release failed", exc_info=True)
        self._kernels.clear()
        self._buffers.clear()
        self._context = None
        self._released = True
