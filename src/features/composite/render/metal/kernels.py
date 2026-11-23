"""合成域 Metal compute kernel 集（迁移计划阶段 5.1/5.2 部分）。

与 GL 路径（`src/features/composite/core/shaders.py` 的
`_BLEND_FRAGMENT` / `_TRANSFORM_FRAGMENT` / `_CROP_FRAGMENT` /
`_MASK_FRAGMENT` / `_PREMULT_FRAGMENT` / `_UNPREMULT_FRAGMENT` /
`_CONSTANT_GRADIENT_FRAGMENT` / Output 节点的 `_ALPHA_FRAGMENT` 与
`_BG_COMPOSITE_FRAGMENT`）逐条对齐，混合模式索引与
`nodes/blend_node.py::BLEND_MODES` 一致。

坐标与排版约定（与 GL 路径的差异在此声明）：

- 纹理行序为图像 top-left（numpy 行序）：row 0 是画面顶部。GL FBO 是
  bottom-up；GLSL 里以 ``v_uv`` 直接寻址的图案（constant 渐变、mask
  SDF）的 Y 对应关系由 CPU 侧参数换算保证（见各 kernel 注释）。
- 所有 kernel 以「区域」为执行单位：dispatch 网格 = 区域尺寸，参数携带
  区域在画布上的绝对原点与写回输出纹理的 texel 偏移。这是 tile 渲染
  （5.3）的基础——同一 kernel 既跑全帧也跑 tile，无需变体。
- 参数结构体只使用 4 字节标量（uint32_t/int32_t/float），与 numpy
  structured dtype 逐字段对齐，规避 MSL 的 float4 对齐规则。每个 struct
  以 ``Region region`` 开头，numpy dtype 以 ``REGION_FIELDS`` 开头。
- 采样：linear 走硬件双线性（clamp_to_edge），nearest 走整数 texel。
  GLSL 版在采样前显式检查 uv ∈ [0,1]，越界返回透明；本文在采样前做
  整数矩形包含检查（数据窗口语义），等价且对 tile 边界更稳。
"""

from __future__ import annotations

import numpy as np

# ── 参数 buffer 的 numpy dtype（与 MSL struct 逐字段对齐） ────────────────

REGION_FIELDS = [
    ("origin_x", "<u4"), ("origin_y", "<u4"),
    ("size_x", "<u4"), ("size_y", "<u4"),
    # 区域首 texel 在输出纹理内的偏移（packed 数据窗口时 = 区域原点 -
    # 窗口原点；canvas 尺寸纹理时 = 区域原点）。
    ("write_x", "<u4"), ("write_y", "<u4"),
]

CONSTANT_PARAMS_DT = np.dtype([
    *REGION_FIELDS,
    ("mode", "<u4"),            # 0 solid, 1 checker, 2 gradient, 3 radial
    ("color1", "<f4", 4),
    ("color2", "<f4", 4),
    ("checker_size", "<u4"),
    ("start_x", "<f4"), ("start_y", "<f4"),
    ("end_x", "<f4"), ("end_y", "<f4"),
    ("center_x", "<f4"), ("center_y", "<f4"),
    ("radial_radius", "<f4"),
    ("feather", "<f4"),
    ("canvas_w", "<u4"), ("canvas_h", "<u4"),
])

TRANSFORM_PARAMS_DT = np.dtype([
    *REGION_FIELDS,
    # 逆仿射（out_abs → in_abs，top-down 像素空间）：
    # in_x = i00*out_x + i01*out_y + i02 ; in_y = i10*out_x + i11*out_y + i12
    ("i00", "<f4"), ("i01", "<f4"), ("i02", "<f4"),
    ("i10", "<f4"), ("i11", "<f4"), ("i12", "<f4"),
    ("in_rect_x", "<i4"), ("in_rect_y", "<i4"),
    ("in_w", "<u4"), ("in_h", "<u4"),
    ("filter", "<u4"),          # 0 linear, 1 nearest（FILTER_MODES）
])

MERGE_PARAMS_DT = np.dtype([
    *REGION_FIELDS,
    ("canvas_w", "<u4"), ("canvas_h", "<u4"),
    ("rect_a", "<i4", 4),       # 输入 A（前景）数据窗口，top-down 半开（绝对）
    ("rect_b", "<i4", 4),
    ("rect_mask", "<i4", 4),
    # 输入纹理的打包区域（绝对原点 + 尺寸）：uv = (abs - pack.xy + .5)/pack.wh
    ("pack_a", "<i4", 4),
    ("pack_b", "<i4", 4),
    ("pack_mask", "<i4", 4),
    ("has_mask", "<u4"),
    ("blend_mode", "<u4"),      # BLEND_MODES 索引
    ("opacity", "<f4"),
    ("mix", "<f4"),
    ("a_straight", "<u4"),
    ("b_straight", "<u4"),
    ("alpha_clamp", "<u4"),
    ("output_unpremult", "<u4"),
    ("mask_channel", "<u4"),    # 0 luma, 1 R, 2 G, 3 B, 4 A
    ("mask_invert", "<u4"),
    ("transparent_background", "<u4"),
    ("layer_composite", "<u4"),
    ("mask_straight", "<u4"),
])

ALPHA_PARAMS_DT = np.dtype([
    *REGION_FIELDS,
    ("mode", "<u4"),            # 0 premult, 1 unpremult
])

CROP_PARAMS_DT = np.dtype([
    *REGION_FIELDS,
    # 采样原点（源画布绝对坐标，已含 crop 偏移；CPU 侧从节点 Y-up 参数换算）。
    ("abs_x0", "<i4"), ("abs_y0", "<i4"),
    # 输入纹理打包区域（绝对原点 + 尺寸）。
    ("pack_x", "<i4"), ("pack_y", "<i4"),
    ("pack_w", "<u4"), ("pack_h", "<u4"),
    ("src_w", "<u4"), ("src_h", "<u4"),   # 源画布尺寸（越界策略判定）
    ("outside", "<u4"),         # 0 black, 1 transparent, 2 repeat, 3 mirror
])

MASK_PARAMS_DT = np.dtype([
    *REGION_FIELDS,
    ("has_input", "<u4"),
    ("canvas_w", "<u4"), ("canvas_h", "<u4"),
    # 归一化（0-1，图像 top-down；CPU 侧已做 1-center_y 换算）。
    ("center_x", "<f4"), ("center_y", "<f4"),
    ("mask_size_x", "<f4"), ("mask_size_y", "<f4"),
    ("rotation_deg", "<f4"),
    ("feather", "<f4"),
    ("invert", "<u4"),
    ("output_mode", "<u4"),     # 0 matte, 1 result(白), 2 输入乘 mask
])

OUTPUT_PARAMS_DT = np.dtype([
    *REGION_FIELDS,
    # 输入数据窗口（绝对，含检查用）与输入纹理打包区域（uv 换算用）。
    ("in_rect_x", "<i4"), ("in_rect_y", "<i4"),
    ("in_w", "<u4"), ("in_h", "<u4"),
    ("pack_x", "<i4"), ("pack_y", "<i4"),
    ("pack_w", "<u4"), ("pack_h", "<u4"),
    ("alpha_mode", "<u4"),      # 0 keep, 1 premult, 2 straight, 3 discard
    ("input_premult", "<u4"),
    ("bg_r", "<f4"), ("bg_g", "<f4"), ("bg_b", "<f4"), ("bg_a", "<f4"),
    ("encode_srgb", "<u4"),
])

COPY_PARAMS_DT = np.dtype([
    *REGION_FIELDS,             # 目标区域
    ("src_x", "<i4"), ("src_y", "<i4"),
])


def region_dict(origin_x: int, origin_y: int, size_x: int, size_y: int,
                write_x: int = 0, write_y: int = 0) -> dict:
    """构造参数 dict 的区域六元组（供各 kernel 的参数组装复用）。"""
    return {
        "origin_x": int(origin_x), "origin_y": int(origin_y),
        "size_x": int(size_x), "size_y": int(size_y),
        "write_x": int(write_x), "write_y": int(write_y),
    }


# ── MSL 源 ────────────────────────────────────────────────────────────────

COMMON_MSL = """
#include <metal_stdlib>
using namespace metal;

struct Region {
    uint32_t origin_x; uint32_t origin_y;
    uint32_t size_x; uint32_t size_y;
    uint32_t write_x; uint32_t write_y;
};

static inline bool region_guard(uint2 gid, constant Region& r) {
    return gid.x >= r.size_x || gid.y >= r.size_y;
}

static inline uint2 region_write_pos(uint2 gid, constant Region& r) {
    return gid + uint2(r.write_x, r.write_y);
}

// 常量采样器（linear / nearest）。tile 与全帧共用。
static inline float4 sample_linear(texture2d<float, access::sample> tex,
                                   float2 uv) {
    constexpr sampler s(filter::linear, address::clamp_to_edge,
                        coord::normalized);
    return tex.sample(s, uv);
}

static inline float4 sample_nearest(texture2d<float, access::sample> tex,
                                    float2 uv) {
    constexpr sampler s(filter::nearest, address::clamp_to_edge,
                        coord::normalized);
    return tex.sample(s, uv);
}

// 以整数矩形包含检查 + 打包区域 uv 采样：等价于 GLSL sampleLayer 的
// 越界透明语义（bbox 包含检查），uv 按输入纹理的打包区域换算。
// data_rect 与 pack 均为半开矩形 (x0, y0, x1, y1)。
static inline float4 sample_window_linear(
        texture2d<float, access::sample> tex,
        float2 abs_pos, int4 data_rect, int4 pack) {
    float2 local = abs_pos - (float2)data_rect.xy;
    if (local.x < 0.0 || local.y < 0.0 ||
        local.x >= (float)(data_rect.z - data_rect.x) ||
        local.y >= (float)(data_rect.w - data_rect.y)) {
        return float4(0.0);
    }
    float2 pack_origin((float)pack[0], (float)pack[1]);
    float2 pack_size((float)(pack[2] - pack[0]), (float)(pack[3] - pack[1]));
    return sample_linear(tex, (abs_pos - pack_origin + 0.5) / pack_size);
}

// Transform 专用：包含检查用输入纹理的打包区域（GLSL _TRANSFORM_FRAGMENT
// 检查的是纹理边界而非 bbox）。
static inline float4 sample_packed_linear(
        texture2d<float, access::sample> tex, float2 abs_pos, int4 pack) {
    float2 local = abs_pos - (float2)pack.xy;
    if (local.x < 0.0 || local.y < 0.0 ||
        local.x >= (float)pack.z || local.y >= (float)pack.w) {
        return float4(0.0);
    }
    float2 pack_size((float)pack.z, (float)pack.w);
    return sample_linear(tex, (local + 0.5) / pack_size);
}

static inline float4 sample_packed_nearest(
        texture2d<float, access::sample> tex, float2 abs_pos, int4 pack) {
    float2 local = abs_pos - (float2)pack.xy;
    if (local.x < 0.0 || local.y < 0.0 ||
        local.x >= (float)pack.z || local.y >= (float)pack.w) {
        return float4(0.0);
    }
    float2 pack_size((float)pack.z, (float)pack.w);
    return sample_nearest(tex, (local + 0.5) / pack_size);
}
"""

CONSTANT_MSL = COMMON_MSL + """
struct ConstantParams {
    Region region;
    uint32_t mode;
    float color1[4];
    float color2[4];
    uint32_t checker_size;
    float start_x; float start_y;
    float end_x; float end_y;
    float center_x; float center_y;
    float radial_radius;
    float feather;
    uint32_t canvas_w; uint32_t canvas_h;
};

// 与 _CONSTANT_GRADIENT_FRAGMENT::apply_feather 逐字对齐。
static inline float constant_apply_feather(float t, float f) {
    float tc = clamp(t, 0.0, 1.0);
    if (fabs(f) <= 0.001) { return tc; }
    if (f > 0.0) {
        float pad = min(f, 0.999) * 0.5;
        if (tc <= pad) { return 0.0; }
        if (tc >= 1.0 - pad) { return 1.0; }
        float x = (tc - pad) / (1.0 - 2.0 * pad);
        return x * x * (3.0 - 2.0 * x);
    }
    float a = min(-f, 0.999) * 0.5;
    float s = tc * tc * (3.0 - 2.0 * tc);
    return a + s * (1.0 - 2.0 * a);
}

kernel void comp_constant(texture2d<float, access::write> out [[texture(0)]],
                          constant ConstantParams& p [[buffer(0)]],
                          uint2 gid [[thread_position_in_grid]]) {
    if (region_guard(gid, p.region)) { return; }
    int px = (int)p.region.origin_x + (int)gid.x;
    int py = (int)p.region.origin_y + (int)gid.y;
    if (px < 0 || py < 0 || px >= (int)p.canvas_w || py >= (int)p.canvas_h) {
        out.write(float4(0.0), region_write_pos(gid, p.region));
        return;
    }
    float fx = (float)px;
    float fy = (float)py;
    float w = (float)p.canvas_w;
    float h = (float)p.canvas_h;
    float4 c1 = float4(p.color1[0], p.color1[1], p.color1[2], p.color1[3]);
    float4 c2 = float4(p.color2[0], p.color2[1], p.color2[2], p.color2[3]);
    float4 result;
    if (p.mode == 0u) {
        result = c1;
    } else if (p.mode == 1u) {
        // 与 ConstantNode._make_checker 一致：((y/size)+(x/size))%2==0 → color1。
        int cell = ((int)py / (int)p.checker_size)
                 + ((int)px / (int)p.checker_size);
        result = ((cell % 2) + 2) % 2 == 0 ? c1 : c2;
    } else {
        // 图像 top-down uv（与 GLSL 内翻转后的 uv 一致），**像素中心**
        // 采样：(px + 0.5) / size——GL 参考的 v_uv 来自像素中心插值，
        // 角点采样会让 gradient/radial 相对 GL 偏移半个像素（5.6 对拍
        // 发现，逐像素 diff ≈ 1/w 量级）。
        float2 uv = (float2(fx, fy) + 0.5) / float2(w, h);
        float t;
        if (p.mode == 3u) {
            float2 delta = float2(fx + 0.5 - p.center_x * w,
                                  fy + 0.5 - p.center_y * h);
            float short_edge = min(w, h);
            t = length(delta) / max(p.radial_radius * short_edge, 0.0001);
        } else {
            float2 dir = float2(p.end_x - p.start_x, p.end_y - p.start_y);
            float len2 = dot(dir, dir);
            if (len2 < 0.0001) {
                t = 0.0;
            } else {
                t = dot(uv - float2(p.start_x, p.start_y), dir) / len2;
            }
        }
        t = constant_apply_feather(t, p.feather);
        result = mix(c1, c2, t);
    }
    out.write(result, region_write_pos(gid, p.region));
}
"""

TRANSFORM_MSL = COMMON_MSL + """
struct TransformParams {
    Region region;
    float i00; float i01; float i02;
    float i10; float i11; float i12;
    int in_rect_x; int in_rect_y;
    uint32_t in_w; uint32_t in_h;
    uint32_t filter;
};

// 语义对齐 _TRANSFORM_FRAGMENT：逆矩阵映射输出 → 输入，越界透明。
kernel void comp_transform(
        texture2d<float, access::sample> src [[texture(0)]],
        texture2d<float, access::write> out [[texture(1)]],
        constant TransformParams& p [[buffer(0)]],
        uint2 gid [[thread_position_in_grid]]) {
    if (region_guard(gid, p.region)) { return; }
    float ox = (float)p.region.origin_x + (float)gid.x;
    float oy = (float)p.region.origin_y + (float)gid.y;
    float ix = p.i00 * ox + p.i01 * oy + p.i02;
    float iy = p.i10 * ox + p.i11 * oy + p.i12;
    int4 in_pack = int4(p.in_rect_x, p.in_rect_y,
                        (int)p.in_w, (int)p.in_h);
    float4 value;
    if (p.filter == 1u) {
        value = sample_packed_nearest(src, float2(ix, iy), in_pack);
    } else {
        value = sample_packed_linear(src, float2(ix, iy), in_pack);
    }
    out.write(value, region_write_pos(gid, p.region));
}
"""

MERGE_MSL = COMMON_MSL + """
struct MergeParams {
    Region region;
    uint32_t canvas_w; uint32_t canvas_h;
    int rect_a[4];
    int rect_b[4];
    int rect_mask[4];
    int pack_a[4];
    int pack_b[4];
    int pack_mask[4];
    uint32_t has_mask;
    uint32_t blend_mode;
    float opacity;
    float mix;
    uint32_t a_straight;
    uint32_t b_straight;
    uint32_t alpha_clamp;
    uint32_t output_unpremult;
    uint32_t mask_channel;
    uint32_t mask_invert;
    uint32_t transparent_background;
    uint32_t layer_composite;
    uint32_t mask_straight;
};

// 与 _BLEND_FRAGMENT 的 rgb2hsv/hsv2rgb 逐分量对齐。
static inline float3 rgb2hsv(float3 c) {
    float4 K = float4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    float4 p = mix(float4(c.z, c.y, K.w, K.z), float4(c.y, c.z, K.x, K.y),
                   step(c.z, c.y));
    float4 q = mix(float4(p.x, p.y, p.w, c.x), float4(c.x, p.y, p.z, p.x),
                   step(p.x, c.x));
    float d = q.x - min(q.w, q.y);
    float e = 1.0e-10;
    return float3(fabs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}

static inline float3 hsv2rgb(float3 c) {
    float4 K = float4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
    float3 p = fabs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}

static inline float4 merge_blend(uint32_t mode, float4 fg, float4 b,
                                 float3 fgc, float3 bgc) {
    float3 blended_rgb;
    float out_alpha;
    switch (mode) {
        case 0u:
            blended_rgb = fg.rgb + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break;
        case 1u:
            blended_rgb = b.rgb + fg.rgb * (1.0 - b.a);
            out_alpha = b.a + fg.a * (1.0 - b.a);
            break;
        case 2u:
            blended_rgb = b.rgb + fg.rgb;
            out_alpha = b.a + fg.a;
            break;
        case 3u:
            blended_rgb = b.rgb - fg.rgb;
            out_alpha = b.a;
            break;
        case 4u: {
            float3 col = fgc * bgc;
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 5u: {
            float3 col = 1.0 - (1.0 - fgc) * (1.0 - bgc);
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 6u: {
            float3 col = mix(2.0 * bgc * fgc,
                             1.0 - 2.0 * (1.0 - bgc) * (1.0 - fgc),
                             step(0.5, bgc));
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 7u: {
            float3 col = fabs(bgc - fgc);
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 8u: {
            float3 col = bgc / max(fgc, float3(0.00001));
            col = min(col, float3(65504.0));
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 9u: {
            float3 col = min(fgc, bgc);
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 10u: {
            float3 col = max(fgc, bgc);
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 11u: {
            float3 col = (fgc + bgc) * 0.5;
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 12u:
            blended_rgb = fg.rgb;
            out_alpha = fg.a;
            break;
        case 13u:
            blended_rgb = b.rgb * fg.a;
            out_alpha = b.a * fg.a;
            break;
        case 14u:
            blended_rgb = fg.rgb * b.a;
            out_alpha = fg.a * b.a;
            break;
        case 15u:
            blended_rgb = fg.rgb * b.a;
            out_alpha = fg.a * b.a;
            break;
        case 16u:
            blended_rgb = fg.rgb * (1.0 - b.a);
            out_alpha = fg.a * (1.0 - b.a);
            break;
        case 17u:
            blended_rgb = fg.rgb * b.a + b.rgb * (1.0 - fg.a);
            out_alpha = b.a;
            break;
        case 18u:
            blended_rgb = fg.rgb * (1.0 - b.a) + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a * (1.0 - b.a) + b.a * (1.0 - fg.a);
            break;
        case 19u: {
            float3 col = bgc / max(1.0 - fgc, float3(0.00001));
            col = min(col, float3(65504.0));
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 20u: {
            float3 col = 1.0 - (1.0 - bgc) / max(fgc, float3(0.00001));
            col = max(col, float3(0.0));
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 21u: {
            float3 col = mix(bgc - (1.0 - 2.0 * fgc) * bgc * (1.0 - bgc),
                             bgc + (2.0 * fgc - 1.0)
                                 * (sqrt(max(bgc, 0.0)) - bgc),
                             step(0.5, fgc));
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 22u: {
            float3 col = mix(2.0 * fgc * bgc,
                             1.0 - 2.0 * (1.0 - fgc) * (1.0 - bgc),
                             step(0.5, fgc));
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 23u: {
            float3 col = bgc + 2.0 * fgc - 1.0;
            col = clamp(col, float3(0.0), float3(65504.0));
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 24u: {
            float3 fg_hsv = rgb2hsv(fgc);
            float3 bg_hsv = rgb2hsv(bgc);
            float3 col = hsv2rgb(float3(fg_hsv.x, bg_hsv.y, bg_hsv.z));
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 25u: {
            float3 fg_hsv = rgb2hsv(fgc);
            float3 bg_hsv = rgb2hsv(bgc);
            float3 col = hsv2rgb(float3(bg_hsv.x, fg_hsv.y, bg_hsv.z));
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 26u: {
            float3 fg_hsv = rgb2hsv(fgc);
            float3 bg_hsv = rgb2hsv(bgc);
            float3 col = hsv2rgb(float3(fg_hsv.x, fg_hsv.y, bg_hsv.z));
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 27u: {
            float3 fg_hsv = rgb2hsv(fgc);
            float3 bg_hsv = rgb2hsv(bgc);
            float3 col = hsv2rgb(float3(bg_hsv.x, bg_hsv.y, fg_hsv.z));
            blended_rgb = col * fg.a + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break; }
        case 28u:
            blended_rgb = fg.rgb - b.rgb;
            out_alpha = clamp(fg.a, 0.0, 1.0);
            break;
        case 29u:
            blended_rgb = fg.rgb + b.rgb - 2.0 * fg.rgb * b.rgb;
            out_alpha = fg.a + b.a * (1.0 - fg.a);
            break;
        case 30u: {
            float a_sum = fg.a + b.a;
            float a_out = min(a_sum, 1.0);
            blended_rgb = fg.rgb + b.rgb
                        * clamp((a_out - fg.a) / max(b.a, 1e-6), 0.0, 1.0);
            out_alpha = a_out;
            break; }
        case 31u: {
            float a_sum2 = fg.a + b.a;
            if (a_sum2 <= 1.0) {
                blended_rgb = fg.rgb + b.rgb;
                out_alpha = a_sum2;
            } else {
                blended_rgb = fg.rgb + b.rgb
                            * clamp((1.0 - fg.a) / max(b.a, 1e-6), 0.0, 1.0);
                out_alpha = max(fg.a, b.a);
            }
            break; }
        default:
            blended_rgb = fg.rgb + b.rgb * (1.0 - fg.a);
            out_alpha = fg.a + b.a * (1.0 - fg.a);
    }
    return float4(blended_rgb, out_alpha);
}

kernel void comp_merge(
        texture2d<float, access::sample> tex_a [[texture(0)]],
        texture2d<float, access::sample> tex_b [[texture(1)]],
        texture2d<float, access::sample> tex_mask [[texture(2)]],
        texture2d<float, access::write> out [[texture(3)]],
        constant MergeParams& p [[buffer(0)]],
        uint2 gid [[thread_position_in_grid]]) {
    if (region_guard(gid, p.region)) { return; }
    float2 abs_pos = float2((float)p.region.origin_x + (float)gid.x,
                            (float)p.region.origin_y + (float)gid.y);
    int4 rect_a = int4(p.rect_a[0], p.rect_a[1], p.rect_a[2], p.rect_a[3]);
    int4 rect_b = int4(p.rect_b[0], p.rect_b[1], p.rect_b[2], p.rect_b[3]);
    int4 rect_m = int4(p.rect_mask[0], p.rect_mask[1], p.rect_mask[2],
                       p.rect_mask[3]);
    int4 pack_a = int4(p.pack_a[0], p.pack_a[1], p.pack_a[2], p.pack_a[3]);
    int4 pack_b = int4(p.pack_b[0], p.pack_b[1], p.pack_b[2], p.pack_b[3]);
    int4 pack_m = int4(p.pack_mask[0], p.pack_mask[1], p.pack_mask[2],
                       p.pack_mask[3]);

    float4 a = sample_window_linear(tex_a, abs_pos, rect_a, pack_a);
    float4 b = p.transparent_background != 0u
        ? float4(0.0)
        : sample_window_linear(tex_b, abs_pos, rect_b, pack_b);

    if (p.a_straight != 0u) { a.rgb *= a.a; }
    if (p.b_straight != 0u) { b.rgb *= b.a; }

    float mask_val = 1.0;
    if (p.has_mask != 0u) {
        float4 m = sample_window_linear(tex_mask, abs_pos, rect_m, pack_m);
        if (p.mask_channel == 0u) {
            mask_val = dot(m.rgb, float3(0.2126, 0.7152, 0.0722));
            if (p.layer_composite != 0u && p.mask_straight != 0u) {
                mask_val *= m.a;
            }
        } else if (p.mask_channel == 1u) {
            mask_val = m.r;
        } else if (p.mask_channel == 2u) {
            mask_val = m.g;
        } else if (p.mask_channel == 3u) {
            mask_val = m.b;
        } else {
            mask_val = m.a;
        }
        if (p.mask_invert != 0u) {
            mask_val = 1.0 - mask_val;
        }
    }
    if (p.layer_composite != 0u) {
        mask_val = clamp(mask_val, 0.0, 1.0);
    }
    float effect = p.opacity * mask_val;

    float4 fg;
    if (p.blend_mode == 12u || p.blend_mode == 13u || p.blend_mode == 14u) {
        fg = float4(a.rgb, a.a * effect);
    } else {
        fg = float4(a.rgb * effect, a.a * effect);
    }

    float3 fg_col = fg.a > 0.00001 ? fg.rgb / fg.a : float3(0.0);
    float3 bg_col = b.a > 0.00001 ? b.rgb / b.a : float3(0.0);

    float4 blended = merge_blend(p.blend_mode, fg, b, fg_col, bg_col);

    if (p.layer_composite != 0u) {
        if (b.a <= 0.0) {
            blended.rgb = fg.rgb;
            blended.a = fg.a;
        } else if ((p.blend_mode >= 4u && p.blend_mode <= 11u)
                   || (p.blend_mode >= 19u && p.blend_mode <= 27u)) {
            float3 backdrop = b.rgb * (1.0 - fg.a);
            blended.rgb = backdrop
                + mix(fg.rgb, blended.rgb - backdrop, min(b.a, 1.0));
        }
    }

    if (p.alpha_clamp != 0u) {
        blended.a = clamp(blended.a, 0.0, 1.0);
    }

    float mix_amount = clamp(
        p.mix * (p.layer_composite != 0u ? 1.0 : mask_val), 0.0, 1.0);
    blended.rgb = mix(b.rgb, blended.rgb, mix_amount);
    blended.a = mix(b.a, blended.a, mix_amount);

    if (p.output_unpremult != 0u && blended.a > 0.00001) {
        blended.rgb = blended.rgb / blended.a;
    }

    out.write(blended, region_write_pos(gid, p.region));
}
"""

ALPHA_MSL = COMMON_MSL + """
struct AlphaParams {
    Region region;
    uint32_t mode;   // 0 premult, 1 unpremult
};

// 与 _PREMULT_FRAGMENT / _UNPREMULT_FRAGMENT 对齐；alpha=0 时保持 RGB=0
// （除法守卫 1e-5 与 GLSL 相同）。
kernel void comp_alpha_op(
        texture2d<float, access::sample> src [[texture(0)]],
        texture2d<float, access::write> out [[texture(1)]],
        constant AlphaParams& p [[buffer(0)]],
        uint2 gid [[thread_position_in_grid]]) {
    if (region_guard(gid, p.region)) { return; }
    float2 tex_size((float)src.get_width(), (float)src.get_height());
    float4 color = sample_linear(src, (float2(gid) + 0.5) / tex_size);
    float4 result;
    if (p.mode == 0u) {
        result = float4(color.rgb * color.a, color.a);
    } else {
        float inv_a = 1.0 / max(color.a, 0.00001);
        float3 rgb = color.a > 0.00001 ? color.rgb * inv_a : float3(0.0);
        result = float4(rgb, color.a);
    }
    out.write(result, region_write_pos(gid, p.region));
}
"""

CROP_MSL = COMMON_MSL + """
struct CropParams {
    Region region;
    int abs_x0; int abs_y0;      // 采样原点（源画布绝对坐标）
    int pack_x; int pack_y;      // 输入纹理打包区域
    uint32_t pack_w; uint32_t pack_h;
    uint32_t src_w; uint32_t src_h;   // 源画布尺寸（越界策略判定）
    uint32_t outside;  // 0 black, 1 transparent, 2 repeat, 3 mirror
};

static inline float2 crop_mirror_uv(float2 uv) {
    float2 m = fract(uv * 0.5) * 2.0;
    return mix(m, 2.0 - m, step(1.0, m));
}

// 与 _CROP_FRAGMENT 对齐：输出像素 → 源像素（越界按 outside 策略；
// 越界判定基于源画布，采样 uv 按输入纹理打包区域换算）。
kernel void comp_crop(
        texture2d<float, access::sample> src [[texture(0)]],
        texture2d<float, access::write> out [[texture(1)]],
        constant CropParams& p [[buffer(0)]],
        uint2 gid [[thread_position_in_grid]]) {
    if (region_guard(gid, p.region)) { return; }
    // abs_pos 已含像素中心 +0.5（与硬件 texel 中心约定一致）；后续采样
    // 不得再次 +0.5。
    float2 abs_pos = float2((float)p.abs_x0 + (float)gid.x,
                            (float)p.abs_y0 + (float)gid.y) + 0.5;
    float2 src_size = float2((float)p.src_w, (float)p.src_h);
    float2 uv = abs_pos / src_size;
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
        if (p.outside == 2u) {
            uv = fract(uv);
        } else if (p.outside == 3u) {
            uv = crop_mirror_uv(uv);
        } else {
            out.write(p.outside == 1u ? float4(0.0)
                                      : float4(0.0, 0.0, 0.0, 1.0),
                      region_write_pos(gid, p.region));
            return;
        }
        abs_pos = uv * src_size;
    }
    float2 pack_origin((float)p.pack_x, (float)p.pack_y);
    float2 pack_size((float)p.pack_w, (float)p.pack_h);
    out.write(sample_linear(src, (abs_pos - pack_origin) / pack_size),
              region_write_pos(gid, p.region));
}
"""

MASK_MSL = COMMON_MSL + """
struct MaskParams {
    Region region;
    uint32_t has_input;
    uint32_t canvas_w; uint32_t canvas_h;
    float center_x; float center_y;
    float mask_size_x; float mask_size_y;
    float rotation_deg;
    float feather;
    uint32_t invert;
    uint32_t output_mode;  // 0 matte, 1 result, 2 input*mask
};

// 与 _MASK_FRAGMENT 的 rectangle 分支对齐（SDF + smoothstep 羽化 +
// 旋转 + 反相 + 三种输出模式）。
kernel void comp_mask_rect(
        texture2d<float, access::sample> src [[texture(0)]],
        texture2d<float, access::write> out [[texture(1)]],
        constant MaskParams& p [[buffer(0)]],
        uint2 gid [[thread_position_in_grid]]) {
    if (region_guard(gid, p.region)) { return; }
    float w = (float)p.canvas_w;
    float h = (float)p.canvas_h;
    float2 uv = float2(((float)p.region.origin_x + (float)gid.x) / w,
                       ((float)p.region.origin_y + (float)gid.y) / h);
    float2 center = float2(p.center_x, p.center_y);
    if (fabs(p.rotation_deg) > 0.1) {
        float angle = p.rotation_deg * 0.01745329;
        float c = cos(angle);
        float s = sin(angle);
        float2 offset = uv - center;
        uv = float2(offset.x * c - offset.y * s,
                    offset.x * s + offset.y * c) + center;
    }
    float2 half_size = float2(p.mask_size_x, p.mask_size_y) * 0.5;
    float2 d = fabs(uv - center) - half_size;
    float dist = max(d.x, d.y);

    float alpha;
    if (p.feather < 0.001) {
        alpha = step(0.0, dist);
    } else {
        alpha = smoothstep(-p.feather, p.feather, dist);
    }
    if (p.invert != 0u) {
        alpha = 1.0 - alpha;
    }

    float4 result;
    if (p.output_mode == 0u) {
        result = float4(0.0, 0.0, 0.0, alpha);
    } else if (p.output_mode == 1u) {
        result = float4(alpha, alpha, alpha, alpha);
    } else {
        float2 tex_size((float)src.get_width(), (float)src.get_height());
        float4 source = p.has_input != 0u
            ? sample_linear(src, (float2(gid) + 0.5) / tex_size)
            : float4(0.0);
        result = float4(source.rgb * alpha, source.a * alpha);
    }
    out.write(result, region_write_pos(gid, p.region));
}
"""

OUTPUT_MSL = COMMON_MSL + """
struct OutputParams {
    Region region;
    int in_rect_x; int in_rect_y;      // 输入数据窗口（绝对）
    uint32_t in_w; uint32_t in_h;
    int pack_x; int pack_y;            // 输入纹理打包区域
    uint32_t pack_w; uint32_t pack_h;
    uint32_t alpha_mode;   // 0 keep, 1 premult, 2 straight, 3 discard
    uint32_t input_premult;
    float bg_r; float bg_g; float bg_b; float bg_a;
    uint32_t encode_srgb;
};

// sRGB OETF（IEC 61966-2-1），与 cs_transform 的编码方向一致。
static inline float linear_to_srgb(float x) {
    if (x <= 0.0031308) { return 12.92 * x; }
    return 1.055 * pow(max(x, 0.0), 1.0 / 2.4) - 0.055;
}

// Output 节点的画布放置 + alpha 策略 + 背景合成，语义对齐
// _place_on_canvas / _ALPHA_FRAGMENT / _BG_COMPOSITE_FRAGMENT。
kernel void comp_output(
        texture2d<float, access::sample> src [[texture(0)]],
        texture2d<float, access::write> out [[texture(1)]],
        constant OutputParams& p [[buffer(0)]],
        uint2 gid [[thread_position_in_grid]]) {
    if (region_guard(gid, p.region)) { return; }
    float2 abs_pos = float2((float)p.region.origin_x + (float)gid.x,
                            (float)p.region.origin_y + (float)gid.y);
    int4 in_rect = int4(p.in_rect_x, p.in_rect_y,
                        p.in_rect_x + (int)p.in_w,
                        p.in_rect_y + (int)p.in_h);
    // pack_w/pack_h 是尺寸；换算成半开矩形喂给 sample_window_linear。
    int4 in_pack = int4(p.pack_x, p.pack_y,
                        p.pack_x + (int)p.pack_w,
                        p.pack_y + (int)p.pack_h);
    float4 c = sample_window_linear(src, abs_pos, in_rect, in_pack);

    if (p.bg_a > 0.0) {
        float alpha = c.a;
        float3 fg_premult = p.input_premult != 0u ? c.rgb : c.rgb * alpha;
        float4 bg = float4(p.bg_r, p.bg_g, p.bg_b, p.bg_a);
        c = float4(fg_premult + bg.rgb * bg.a * (1.0 - alpha),
                   alpha + bg.a * (1.0 - alpha));
    }

    float3 straight_rgb = c.rgb;
    if (p.input_premult != 0u && c.a > 0.001) {
        straight_rgb /= c.a;
    }
    float4 result = c;
    if (p.alpha_mode == 1u) {
        result = float4(straight_rgb * c.a, c.a);
    } else if (p.alpha_mode == 2u) {
        result = float4(straight_rgb, c.a);
    } else if (p.alpha_mode == 3u) {
        result = float4(straight_rgb, 1.0);
    }

    if (p.encode_srgb != 0u) {
        result.rgb = float3(linear_to_srgb(result.r),
                            linear_to_srgb(result.g),
                            linear_to_srgb(result.b));
    }
    out.write(result, region_write_pos(gid, p.region));
}
"""

COPY_MSL = COMMON_MSL + """
struct CopyParams {
    Region region;
    int src_x; int src_y;
};

// tile 裁回中心：把源纹理的一个子矩形拷贝到目标区域（tile 拼接用）。
// nearest 采样保证 1:1 像素搬运，不做重采样。
kernel void comp_copy_region(
        texture2d<float, access::sample> src [[texture(0)]],
        texture2d<float, access::write> out [[texture(1)]],
        constant CopyParams& p [[buffer(0)]],
        uint2 gid [[thread_position_in_grid]]) {
    if (region_guard(gid, p.region)) { return; }
    float2 tex_size((float)src.get_width(), (float)src.get_height());
    float2 src_pos = float2((float)p.src_x + (float)gid.x,
                            (float)p.src_y + (float)gid.y) + 0.5;
    out.write(sample_nearest(src, src_pos / tex_size),
              region_write_pos(gid, p.region));
}
"""

# kernel 名 → MSL 源（MetalComputeContext.compile_kernel 的缓存键是
# kernel 名，因此每个 kernel 一段独立源，公共头重复内联）。
METAL_COMPOSITE_KERNEL_SOURCES: dict = {
    "comp_constant": CONSTANT_MSL,
    "comp_transform": TRANSFORM_MSL,
    "comp_merge": MERGE_MSL,
    "comp_alpha_op": ALPHA_MSL,
    "comp_crop": CROP_MSL,
    "comp_mask_rect": MASK_MSL,
    "comp_output": OUTPUT_MSL,
    "comp_copy_region": COPY_MSL,
}
