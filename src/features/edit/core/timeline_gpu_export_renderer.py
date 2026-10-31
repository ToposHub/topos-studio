"""时间线导出 GPU 合成渲染器（P0-GPU-1：单 video/image 层直通）。

复用 ``ExportVideoDenoiseBridge`` 的生命周期模式（渲染线程惰性创建
standalone moderngl context、失败直通、幂等 release），把
``TimelineExporter._composite_layers`` 的 CPU 热链折叠为三段 GPU pass：

    pass1  planar YUV → RGB u8 @ 源分辨率
           （usampler + texelFetch，色度手动双线性上采样对齐
           ``_planes_to_bgr_float`` 的 cv2.INTER_LINEAR 口径；位深/range/
           matrix 系数在 CPU 侧按同一公式计算后以 uniform 传入）
    pass2  ColorStack primary grade @ 源分辨率
           （shader 与 ``EditGpuPreviewRenderer._grade_program`` 同源，
           预览/导出同像素；仅在层挂有调色时执行）
    pass2.5 clip 效果链 @ 源分辨率（2026-09-17 导出提速：blur 先行）
           （复用 ``EffectPassRunner``（预览同款 GLSL/参数打包），在
           grade 之后、缩放之前执行；u8 f1 链与 deep-u16 桥
           （u16→f2→EPR→u16，T-P1-21）均支持。效果契约由
           ``TimelineExporter._gpu_opengl_effect_chain`` fail-closed 把关）
    pass3  LINEAR 双线性缩放到输出分辨率 + opacity → BGR u8 回读

阶段 1 适用条件由调用方（``TimelineExporter._gpu_render_composite``）
把关：单层、无转场/嵌套序列/adjustment、无 crop/denoise、非 alpha
导出、非高精度导出、planar YUV（422/444/420 的 8/10/12-bit）或打包
BGR u8 源。任一不满足或运行失败 → 返回 None，调用方原样回落 CPU 链，
导出永不因 GL 中断。

线程契约：standalone context 绑定创建线程；``release()`` 必须由创建
线程调用（集成层保证：流水线模式在生产者线程 finally 释放，串行模式
在导出线程 finally 释放）。非线程安全——每个导出 worker 持有独立实例。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from src.features.edit.core.gpu_transition_shaders import (
    GPU_TRANSITION_FRAGMENT_SHADER_SOURCE,
)

from src.features.edit.core.primary_grade_shader import (
    PRIMARY_GRADE_BODY as _GRADE_BODY,
    PRIMARY_GRADE_UNIFORMS as _GRADE_UNIFORMS,
    PRIMARY_GRADE_FRAGMENT,
)

logger = logging.getLogger(__name__)

def _load_blend_glsl() -> str:
    """A1: 读取共享 blend.glsl 公式（与 CPU blend_modes.py 一致）。"""
    import os as _os
    path = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), "shaders", "blend.glsl")
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


_BLEND_GLSL = _load_blend_glsl()

_VERT_SRC = """
#version 330
in vec2 in_pos;
out vec2 v_texcoord;
void main() {
    v_texcoord = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

# pass1：planar YUV（usampler 整数纹理）→ RGB u8。
# 色度上采样复刻 cv2.resize INTER_LINEAR 的半像素中心约定：
# dst(x) ← src((x + 0.5) / div - 0.5) 双线性；luma 1:1 直取。
_FRAG_YUV_SRC = """
#version 330
in vec2 v_texcoord;
uniform usampler2D u_y_plane;
uniform usampler2D u_u_plane;
uniform usampler2D u_v_plane;
uniform float u_y_scale;
uniform float u_y_offset;
uniform float u_c_scale;
uniform float u_c_offset;
uniform float u_r_from_cr;
uniform float u_b_from_cb;
uniform float u_g_from_cb;
uniform float u_g_from_cr;
uniform float u_chroma_x_div;
uniform float u_chroma_y_div;
uniform int u_chroma_round_even;
out vec4 f_color;

// 镜像 cv2.resize 整数输出的舍入：8U 平面 half-up，16U 平面 half-even
float half_even(float v) {
    float fl = floor(v);
    float fr = v - fl;
    if (fr > 0.5) { return fl + 1.0; }
    if (fr < 0.5) { return fl; }
    return fl + mod(fl, 2.0);
}
vec3 half_even3(vec3 v) {
    return vec3(half_even(v.r), half_even(v.g), half_even(v.b));
}

float round_chroma(float v) {
    if (u_chroma_round_even != 0) { return half_even(v); }
    return floor(v + 0.5);
}

float fetch_bilinear_texel(usampler2D tex, vec2 pos) {
    ivec2 sz = textureSize(tex, 0);
    ivec2 p0 = ivec2(floor(pos));
    vec2 frac = pos - vec2(p0);
    ivec2 pa = clamp(p0, ivec2(0), sz - ivec2(1));
    ivec2 pb = clamp(p0 + ivec2(1), ivec2(0), sz - ivec2(1));
    float c00 = float(texelFetch(tex, pa, 0).r);
    float c10 = float(texelFetch(tex, ivec2(pb.x, pa.y), 0).r);
    float c01 = float(texelFetch(tex, ivec2(pa.x, pb.y), 0).r);
    float c11 = float(texelFetch(tex, pb, 0).r);
    return round_chroma(mix(mix(c00, c10, frac.x), mix(c01, c11, frac.x), frac.y));
}

void main() {
    ivec2 ysz = textureSize(u_y_plane, 0);
    ivec2 luma_p = clamp(ivec2(gl_FragCoord.xy), ivec2(0), ysz - ivec2(1));
    float y_code = float(texelFetch(u_y_plane, luma_p, 0).r);
    // gl_FragCoord 已在像素中心 (r + 0.5)，直接映射 cv2 半像素中心约定：
    // dst r ← src((r + 0.5) / div - 0.5)；再 +0.5 会引入半像素相位误差
    vec2 chroma_pos = vec2(gl_FragCoord.xy)
        / vec2(u_chroma_x_div, u_chroma_y_div) - vec2(0.5);
    float cb_code = fetch_bilinear_texel(u_u_plane, chroma_pos);
    float cr_code = fetch_bilinear_texel(u_v_plane, chroma_pos);
    float y = y_code * u_y_scale + u_y_offset;
    float cb = cb_code * u_c_scale + u_c_offset;
    float cr = cr_code * u_c_scale + u_c_offset;
    float r = y + u_r_from_cr * cr;
    float b = y + u_b_from_cb * cb;
    float g = y + u_g_from_cb * cb + u_g_from_cr * cr;
    // 信号域合法性裁剪，与 _planes_to_bgr_float 的 np.clip 同语义
    f_color = vec4(clamp(vec3(r, g, b), 0.0, 1.0), 1.0);
}
"""

# ColorStack primary grade——与 EditGpuPreviewRenderer._grade_program
# 同源（含 rgb2hsv/hsv2rgb 与 apply_primary_grade 逐行一致），保证导出
# GPU 路径与实时预览像素一致。u8/u16 两条管线共用此主体。
# pass2（u8 管线）：f1 采样 → grade → f1 输出
_FRAG_GRADE_SRC = PRIMARY_GRADE_FRAGMENT

# round-half-even 标量/向量工具（镜像 CPU np.rint）
_HALF_EVEN_GLSL = """
float half_even(float v) {
    float fl = floor(v);
    float fr = v - fl;
    if (fr > 0.5) { return fl + 1.0; }
    if (fr < 0.5) { return fl; }
    return fl + mod(fl, 2.0);
}
vec3 half_even3(vec3 v) {
    return vec3(half_even(v.r), half_even(v.g), half_even(v.b));
}
"""

# pass2（u16 管线）：RGB16UI texelFetch → /65535 → grade → u16 舍入输出。
# 舍入为 half-even（镜像 CPU np.rint）
_FRAG_GRADE_U16_SRC = (
    "#version 330\n"
    "uniform usampler2D u_input_tex;\n"
    + _GRADE_UNIFORMS
    + "in vec2 v_texcoord;\nout uvec4 f_color;\n"
    + _GRADE_BODY
    + _HALF_EVEN_GLSL
    + """
void main() {
    ivec2 sz = textureSize(u_input_tex, 0);
    ivec2 p = clamp(ivec2(gl_FragCoord.xy), ivec2(0), sz - ivec2(1));
    vec3 rgb = vec3(texelFetch(u_input_tex, p, 0).rgb) / 65535.0;
    vec3 graded = clamp(apply_primary_grade(rgb), 0.0, 1.0);
    f_color = uvec4(half_even3(graded * 65535.0), 65535u);
}
"""
)

# pass3：LINEAR 双线性缩放 + opacity，输出 BGR 字节序（回读即 bgr24，
# 与 _encode_frame 的输入契约一致）。
_FRAG_SCALE_SRC = """
#version 330
uniform sampler2D u_input_tex;
uniform float u_opacity;
in vec2 v_texcoord;
out vec4 f_color;

void main() {
    vec3 rgb = texture(u_input_tex, v_texcoord).rgb;
    vec3 scaled = clamp(rgb, 0.0, 1.0) * u_opacity;
    f_color = vec4(scaled.b, scaled.g, scaled.r, 1.0);
}
"""

# Final RGB8 → limited-range BT.709 YUV420 conversion.  The application keeps
# the working image on the GPU through grading/compositing/scaling, then reads
# back only the three encoder input planes.  The RGB texture passed here is
# already 8-bit quantized to match the established BGR→swscale path.
_FRAG_SCALE_RGB_SRC = """
#version 330
uniform sampler2D u_input_tex;
uniform float u_opacity;
in vec2 v_texcoord;
out vec4 f_color;
void main() {
    vec3 rgb = clamp(texture(u_input_tex, v_texcoord).rgb, 0.0, 1.0)
        * u_opacity;
    f_color = vec4(rgb, 1.0);
}
"""

_FRAG_QUANT_RGB_SRC = (
    "#version 330\n"
    "uniform sampler2D u_acc_tex;\n"
    "in vec2 v_texcoord;\n"
    "out vec4 f_color;\n"
    + _HALF_EVEN_GLSL
    + """
void main() {
    vec4 acc = texelFetch(u_acc_tex, ivec2(gl_FragCoord.xy), 0);
    vec3 rgb = half_even3(clamp(acc.rgb, 0.0, 1.0) * 255.0) / 255.0;
    f_color = vec4(rgb, 1.0);
}
"""
)

_FRAG_YUV420_LUMA_SRC = """
#version 330
uniform sampler2D u_rgb_tex;
in vec2 v_texcoord;
out vec4 f_color;
float half_even(float v) {
    float fl = floor(v);
    float fr = v - fl;
    if (fr > 0.5) return fl + 1.0;
    if (fr < 0.5) return fl;
    return fl + mod(fl, 2.0);
}
void main() {
    vec3 rgb = texelFetch(u_rgb_tex, ivec2(gl_FragCoord.xy), 0).rgb;
    float y = dot(rgb, vec3(0.2126, 0.7152, 0.0722));
    float code = clamp(16.0 + 219.0 * y, 16.0, 235.0);
    f_color = vec4(half_even(code) / 255.0, 0.0, 0.0, 1.0);
}
"""

_FRAG_YUV420_CHROMA_SRC = """
#version 330
uniform sampler2D u_rgb_tex;
in vec2 v_texcoord;
out vec4 f_color;
float half_even(float v) {
    float fl = floor(v);
    float fr = v - fl;
    if (fr > 0.5) return fl + 1.0;
    if (fr < 0.5) return fl;
    return fl + mod(fl, 2.0);
}
vec3 fetch_rgb(ivec2 p, ivec2 size) {
    return texelFetch(u_rgb_tex, clamp(p, ivec2(0), size - ivec2(1)), 0).rgb;
}
void main() {
    ivec2 size = textureSize(u_rgb_tex, 0);
    ivec2 p = ivec2(gl_FragCoord.xy) * 2;
    // Average each 2x2 block before the matrix, matching 4:2:0 box
    // subsampling and keeping luma/chroma in the same Rec.709 signal domain.
    vec3 rgb = 0.25 * (fetch_rgb(p, size) + fetch_rgb(p + ivec2(1, 0), size)
        + fetch_rgb(p + ivec2(0, 1), size)
        + fetch_rgb(p + ivec2(1, 1), size));
    float y = dot(rgb, vec3(0.2126, 0.7152, 0.0722));
    float cb = (rgb.b - y) / 1.8556;
    float cr = (rgb.r - y) / 1.5748;
    float u = half_even(clamp(128.0 + 224.0 * cb, 16.0, 240.0));
    float v = half_even(clamp(128.0 + 224.0 * cr, 16.0, 240.0));
    f_color = vec4(u / 255.0, v / 255.0, 0.0, 1.0);
}
"""

# Common export case fast path: an ungraded 8-bit BT.709 4:2:0 source can be
# resized directly in the Y/U/V domains.  The previous path rendered the
# complete source frame to RGB, scaled RGB, quantized RGB, converted RGB back
# to YUV, and only then read the encoder planes.  That is needlessly expensive
# for the identity color contract used by ordinary H.264 delivery.
#
# This path deliberately keeps a narrow gate in the caller: 8-bit, limited
# range, BT.709, 4:2:0, no grade/effects.  Keeping the shader narrow preserves
# the existing RGB reference path for HDR, alternate matrices, and unusual
# source ranges where YUV-domain resizing would not be equivalent.
_FRAG_YUV420_DIRECT_LUMA_SRC = """
#version 330
uniform usampler2D u_y_plane;
uniform vec2 u_scale;
uniform float u_opacity;
in vec2 v_texcoord;
out vec4 f_color;

float sample_plane(usampler2D tex, vec2 pos) {
    ivec2 size = textureSize(tex, 0);
    ivec2 p0 = ivec2(floor(pos));
    vec2 frac = pos - vec2(p0);
    ivec2 a = clamp(p0, ivec2(0), size - ivec2(1));
    ivec2 b = clamp(p0 + ivec2(1), ivec2(0), size - ivec2(1));
    float c00 = float(texelFetch(tex, a, 0).r);
    float c10 = float(texelFetch(tex, ivec2(b.x, a.y), 0).r);
    float c01 = float(texelFetch(tex, ivec2(a.x, b.y), 0).r);
    float c11 = float(texelFetch(tex, b, 0).r);
    return mix(mix(c00, c10, frac.x), mix(c01, c11, frac.x), frac.y);
}

float half_even(float v) {
    float fl = floor(v);
    float fr = v - fl;
    if (fr > 0.5) return fl + 1.0;
    if (fr < 0.5) return fl;
    return fl + mod(fl, 2.0);
}

void main() {
    vec2 source_pos = gl_FragCoord.xy * u_scale - vec2(0.5);
    float y = sample_plane(u_y_plane, source_pos);
    // Limited BT.709 luma is already in the encoder's code domain.  Opacity
    // is a fade to black, so only the distance from black is scaled.
    float code = 16.0 + (y - 16.0) * u_opacity;
    f_color = vec4(half_even(clamp(code, 16.0, 235.0)) / 255.0,
                   0.0, 0.0, 1.0);
}
"""

_FRAG_YUV420_DIRECT_CHROMA_SRC = """
#version 330
uniform usampler2D u_u_plane;
uniform usampler2D u_v_plane;
uniform vec2 u_scale;
uniform float u_opacity;
in vec2 v_texcoord;
out vec4 f_color;

float sample_plane(usampler2D tex, vec2 pos) {
    ivec2 size = textureSize(tex, 0);
    ivec2 p0 = ivec2(floor(pos));
    vec2 frac = pos - vec2(p0);
    ivec2 a = clamp(p0, ivec2(0), size - ivec2(1));
    ivec2 b = clamp(p0 + ivec2(1), ivec2(0), size - ivec2(1));
    float c00 = float(texelFetch(tex, a, 0).r);
    float c10 = float(texelFetch(tex, ivec2(b.x, a.y), 0).r);
    float c01 = float(texelFetch(tex, ivec2(a.x, b.y), 0).r);
    float c11 = float(texelFetch(tex, b, 0).r);
    return mix(mix(c00, c10, frac.x), mix(c01, c11, frac.x), frac.y);
}

float half_even(float v) {
    float fl = floor(v);
    float fr = v - fl;
    if (fr > 0.5) return fl + 1.0;
    if (fr < 0.5) return fl;
    return fl + mod(fl, 2.0);
}

void main() {
    vec2 source_pos = gl_FragCoord.xy * u_scale - vec2(0.5);
    float u = sample_plane(u_u_plane, source_pos);
    float v = sample_plane(u_v_plane, source_pos);
    // Chroma fades toward neutral (128) when an opaque source is faded to
    // black.  This is the YUV equivalent of RGB * opacity.
    float u_code = 128.0 + (u - 128.0) * u_opacity;
    float v_code = 128.0 + (v - 128.0) * u_opacity;
    f_color = vec4(half_even(clamp(u_code, 16.0, 240.0)) / 255.0,
                   half_even(clamp(v_code, 16.0, 240.0)) / 255.0,
                   0.0, 1.0);
}
"""

# pass1（u16 管线）：planar YUV → RGB16UI 全幅码值（镜像
# _convert_yuv_planes_to_bgr16 的 rint(clip*65535) 量化点）
_FRAG_YUV_U16_SRC = """
#version 330
in vec2 v_texcoord;
uniform usampler2D u_y_plane;
uniform usampler2D u_u_plane;
uniform usampler2D u_v_plane;
uniform float u_y_scale;
uniform float u_y_offset;
uniform float u_c_scale;
uniform float u_c_offset;
uniform float u_r_from_cr;
uniform float u_b_from_cb;
uniform float u_g_from_cb;
uniform float u_g_from_cr;
uniform float u_chroma_x_div;
uniform float u_chroma_y_div;
uniform int u_chroma_round_even;
out uvec4 f_color;

float half_even(float v) {
    float fl = floor(v);
    float fr = v - fl;
    if (fr > 0.5) { return fl + 1.0; }
    if (fr < 0.5) { return fl; }
    return fl + mod(fl, 2.0);
}
vec3 half_even3(vec3 v) {
    return vec3(half_even(v.r), half_even(v.g), half_even(v.b));
}

float round_chroma(float v) {
    if (u_chroma_round_even != 0) { return half_even(v); }
    return floor(v + 0.5);
}

float fetch_bilinear_texel(usampler2D tex, vec2 pos) {
    ivec2 sz = textureSize(tex, 0);
    ivec2 p0 = ivec2(floor(pos));
    vec2 frac = pos - vec2(p0);
    ivec2 pa = clamp(p0, ivec2(0), sz - ivec2(1));
    ivec2 pb = clamp(p0 + ivec2(1), ivec2(0), sz - ivec2(1));
    float c00 = float(texelFetch(tex, pa, 0).r);
    float c10 = float(texelFetch(tex, ivec2(pb.x, pa.y), 0).r);
    float c01 = float(texelFetch(tex, ivec2(pa.x, pb.y), 0).r);
    float c11 = float(texelFetch(tex, pb, 0).r);
    return round_chroma(mix(mix(c00, c10, frac.x), mix(c01, c11, frac.x), frac.y));
}

void main() {
    ivec2 ysz = textureSize(u_y_plane, 0);
    ivec2 luma_p = clamp(ivec2(gl_FragCoord.xy), ivec2(0), ysz - ivec2(1));
    float y_code = float(texelFetch(u_y_plane, luma_p, 0).r);
    // gl_FragCoord 已在像素中心 (r + 0.5)，直接映射 cv2 半像素中心约定：
    // dst r ← src((r + 0.5) / div - 0.5)；再 +0.5 会引入半像素相位误差
    vec2 chroma_pos = vec2(gl_FragCoord.xy)
        / vec2(u_chroma_x_div, u_chroma_y_div) - vec2(0.5);
    float cb_code = fetch_bilinear_texel(u_u_plane, chroma_pos);
    float cr_code = fetch_bilinear_texel(u_v_plane, chroma_pos);
    float y = y_code * u_y_scale + u_y_offset;
    float cb = cb_code * u_c_scale + u_c_offset;
    float cr = cr_code * u_c_scale + u_c_offset;
    float r = y + u_r_from_cr * cr;
    float b = y + u_b_from_cb * cb;
    float g = y + u_g_from_cb * cb + u_g_from_cr * cr;
    vec3 rgb = clamp(vec3(r, g, b), 0.0, 1.0);
    f_color = uvec4(half_even3(rgb * 65535.0), 65535u);
}
"""

# pass3（u16 管线）：RGB16UI 手动双线性（整数纹理不可 LINEAR 采样）→
# opacity → BGR u16 输出。双线性在码值域进行（与 cv2.resize uint16 同域，
# 线性缩放与归一化可交换）
_FRAG_SCALE_U16_SRC = (
    "#version 330\n"
    "uniform usampler2D u_input_tex;\n"
    "uniform float u_opacity;\n"
    "in vec2 v_texcoord;\n"
    "out uvec4 f_color;\n"
    + _HALF_EVEN_GLSL
    + """
void main() {
    ivec2 sz = textureSize(u_input_tex, 0);
    vec2 pos = v_texcoord * vec2(sz) - vec2(0.5);
    ivec2 p0 = ivec2(floor(pos));
    vec2 frac = pos - vec2(p0);
    ivec2 pa = clamp(p0, ivec2(0), sz - ivec2(1));
    ivec2 pb = clamp(p0 + ivec2(1), ivec2(0), sz - ivec2(1));
    vec3 c00 = vec3(texelFetch(u_input_tex, pa, 0).rgb);
    vec3 c10 = vec3(texelFetch(u_input_tex, ivec2(pb.x, pa.y), 0).rgb);
    vec3 c01 = vec3(texelFetch(u_input_tex, ivec2(pa.x, pb.y), 0).rgb);
    vec3 c11 = vec3(texelFetch(u_input_tex, pb, 0).rgb);
    vec3 rgb = mix(mix(c00, c10, frac.x), mix(c01, c11, frac.x), frac.y)
        / 65535.0;
    vec3 scaled = clamp(rgb, 0.0, 1.0) * u_opacity;
    f_color = uvec4(half_even3(vec3(scaled.b, scaled.g, scaled.r) * 65535.0), 65535u);
}
"""
)

# pass3（packed/图片层 + u16 交付）：u8 域 f1 LINEAR 采样（镜像 CPU 对
# packed/图片源在 u8 域 resize 后才量化 u16 的顺序——u8 码值 ×65535/255
# 恰为 ×257 整数倍）→ round 到 u8 → ×257 展开 u16
_FRAG_SCALE_U16_FROM_F1_SRC = """
#version 330
uniform sampler2D u_input_tex;
uniform float u_opacity;
in vec2 v_texcoord;
out uvec4 f_color;

void main() {
    vec3 rgb = texture(u_input_tex, v_texcoord).rgb;
    vec3 scaled = clamp(rgb, 0.0, 1.0) * u_opacity;
    vec3 u8 = floor(scaled * 255.0 + vec3(0.5));
    f_color = uvec4(vec4(u8.b, u8.g, u8.r, 255.0) * 257.0 + vec4(0.5));
}
"""

# ── 多层混合（P0-GPU-3）：RGBA32F 累积 ping-pong，精确镜像 CPU 的
# "逐层 lerp 进 float32 累积缓冲 + 末端一次量化" ──

# 混合（u8 域层）：双线性后 round 回 u8 域（镜像 cv2.resize(8U) 的
# half-up 整数输出），再与累积缓冲 lerp
_FRAG_BLEND_F1_SRC = """
#version 330
uniform sampler2D u_layer_tex;
uniform sampler2D u_acc_tex;
uniform float u_opacity;
uniform int u_blend_mode;
in vec2 v_texcoord;
out vec4 f_color;

void main() {
    vec3 rgb = texture(u_layer_tex, v_texcoord).rgb;
    vec3 u8 = floor(clamp(rgb, 0.0, 1.0) * 255.0 + vec3(0.5)) / 255.0;
    vec4 acc = texelFetch(u_acc_tex, ivec2(gl_FragCoord.xy), 0);
    vec3 mixed = blend_channel(u8, acc.rgb, u_blend_mode);
    f_color = vec4(acc.rgb * (1.0 - u_opacity) + mixed * u_opacity, 1.0);
}
"""

# 混合（u16 码值域层）：手动双线性（整数纹理不可 LINEAR）→ half-even
# round 回 u16 码值（镜像 cv2.resize(16U)）→ 归一化 lerp
_FRAG_BLEND_U16_SRC = (
    "#version 330\n"
    "uniform usampler2D u_layer_tex;\n"
    "uniform sampler2D u_acc_tex;\n"
    "uniform float u_opacity;\n"
    "uniform int u_blend_mode;\n"
    "in vec2 v_texcoord;\n"
    "out vec4 f_color;\n"
    + _HALF_EVEN_GLSL
    + """
void main() {
    ivec2 sz = textureSize(u_layer_tex, 0);
    vec2 pos = v_texcoord * vec2(sz) - vec2(0.5);
    ivec2 p0 = ivec2(floor(pos));
    vec2 frac = pos - vec2(p0);
    ivec2 pa = clamp(p0, ivec2(0), sz - ivec2(1));
    ivec2 pb = clamp(p0 + ivec2(1), ivec2(0), sz - ivec2(1));
    vec3 c00 = vec3(texelFetch(u_layer_tex, pa, 0).rgb);
    vec3 c10 = vec3(texelFetch(u_layer_tex, ivec2(pb.x, pa.y), 0).rgb);
    vec3 c01 = vec3(texelFetch(u_layer_tex, ivec2(pa.x, pb.y), 0).rgb);
    vec3 c11 = vec3(texelFetch(u_layer_tex, pb, 0).rgb);
    vec3 rgb = half_even3(mix(mix(c00, c10, frac.x),
                              mix(c01, c11, frac.x), frac.y)) / 65535.0;
    vec4 acc = texelFetch(u_acc_tex, ivec2(gl_FragCoord.xy), 0);
    vec3 mixed = blend_channel(rgb, acc.rgb, u_blend_mode);
    f_color = vec4(acc.rgb * (1.0 - u_opacity) + mixed * u_opacity, 1.0);
}
"""
)

# 末端量化（u8 交付）：累积 float → half-even（镜像 np.rint）
_FRAG_QUANT_U8_SRC = (
    "#version 330\n"
    "uniform sampler2D u_acc_tex;\n"
    "in vec2 v_texcoord;\n"
    "out vec4 f_color;\n"
    + _HALF_EVEN_GLSL
    + """
void main() {
    vec4 acc = texelFetch(u_acc_tex, ivec2(gl_FragCoord.xy), 0);
    vec3 c = clamp(acc.rgb, 0.0, 1.0) * 255.0;
    // 出口换序 BGR（累积缓冲全程 RGB 序，与单层 scale 路径同约定）
    f_color = vec4(half_even3(vec3(c.b, c.g, c.r)) / 255.0, 1.0);
}
"""
)

# 末端量化（u16 交付）
_FRAG_QUANT_U16_SRC = (
    "#version 330\n"
    "uniform sampler2D u_acc_tex;\n"
    "in vec2 v_texcoord;\n"
    "out uvec4 f_color;\n"
    + _HALF_EVEN_GLSL
    + """
void main() {
    vec4 acc = texelFetch(u_acc_tex, ivec2(gl_FragCoord.xy), 0);
    vec3 c = clamp(acc.rgb, 0.0, 1.0) * 65535.0;
    f_color = uvec4(half_even3(vec3(c.b, c.g, c.r)), 65535u);
}
"""
)

# ── T-P1-21：深度（u16）效果链桥 ──
# EPR scratch 只有 f1/f2（RGBA8/16F），RGB16UI 深度纹理不能直接进效果链
# （此前 deep_u16+effects 直接 fail-closed 回落 CPU，TOPOS 10-bit 导出的
# blur/sharpen/glow 等全部走 CPU float 管线）。桥接：u16 →(f2)→ EPR 链
# →(f2)→ u16，全程 GPU；通道序与 grade_u16 一致为 RGB。
_FRAG_U16_TO_F2_SRC = """
#version 330
uniform usampler2D u_input_tex;
in vec2 v_texcoord;
out vec4 f_color;

void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    vec3 rgb = vec3(texelFetch(u_input_tex, p, 0).rgb) / 65535.0;
    f_color = vec4(rgb, 1.0);
}
"""

_FRAG_F2_TO_U16_SRC = """
#version 330
uniform sampler2D u_input_tex;
in vec2 v_texcoord;
out uvec4 f_color;
""" + _HALF_EVEN_GLSL + """
void main() {
    vec3 rgb = clamp(texture(u_input_tex, v_texcoord).rgb, 0.0, 1.0);
    f_color = uvec4(half_even3(rgb * 65535.0), 65535u);
}
"""

# ── P0-GPU-4 转场：所有双源转场统一使用 procedural GPU shader ──
_FRAG_WIPE_SRC = GPU_TRANSITION_FRAGMENT_SHADER_SOURCE

# wipe 结果注入累积：镜像 CPU 注入规则——首个元素直接拷贝（不乘
# opacity，CPU `composite = layer_float.copy()` 的既有语义），其余
# acc*(1-op) + wipe*op
_FRAG_BLEND_F4_SRC = """
#version 330
uniform sampler2D u_layer_tex;
uniform sampler2D u_acc_tex;
uniform float u_opacity;
uniform int u_blend_mode;
uniform int u_first;
in vec2 v_texcoord;
out vec4 f_color;

void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    vec3 layer_rgb = texelFetch(u_layer_tex, p, 0).rgb;
    if (u_first != 0) {
        f_color = vec4(layer_rgb, 1.0);
        return;
    }
    vec4 acc = texelFetch(u_acc_tex, p, 0);
    vec3 mixed = blend_channel(layer_rgb, acc.rgb, u_blend_mode);
    f_color = vec4(acc.rgb * (1.0 - u_opacity) + mixed * u_opacity, 1.0);
}
"""

# dip_to_color：合成后颜色 over（CPU：composite*(1-cf) + color*cf）
_FRAG_DIP_SRC = """
#version 330
uniform sampler2D u_acc_tex;
uniform vec3 u_dip_color;
uniform float u_dip_factor;
in vec2 v_texcoord;
out vec4 f_color;

void main() {
    vec4 acc = texelFetch(u_acc_tex, ivec2(gl_FragCoord.xy), 0);
    f_color = vec4(acc.rgb * (1.0 - u_dip_factor) + u_dip_color * u_dip_factor, 1.0);
}
"""


def grade_params_to_uniforms(grading_params: Dict[str, Any]) -> Dict[str, Any]:
    """ColorStack primary grade 参数 → shader uniform 值。

    镜像 ``EditGpuPreviewRenderer._grade_params_to_uniforms`` 的取值口径
    （轮盘 lift/gamma/gain 经 ``GradingMath.params_to_rgb`` 求值），保证
    导出 GPU 路径与预览 shader 消费同一组 uniform。
    """
    from src.features.color.core.the_decompiler import GradingMath

    def scalar(name: str) -> float:
        try:
            return float(grading_params.get(name, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def wheel(name: str) -> Tuple[float, float, float]:
        raw = grading_params.get(name, (0.0, 0.0, 0.0)) or (0.0, 0.0, 0.0)
        try:
            hue, saturation, brightness = (
                float(item) for item in tuple(raw)[:3])
        except (TypeError, ValueError):
            hue, saturation, brightness = (0.0, 0.0, 0.0)
        values = GradingMath.params_to_rgb(hue, saturation, brightness)
        return (float(values[0]), float(values[1]), float(values[2]))

    return {
        "u_lift": wheel("lift"),
        "u_gamma": wheel("gamma"),
        "u_gain": wheel("gain"),
        "u_exposure": scalar("exposure"),
        "u_temp": scalar("temp"),
        "u_tint": scalar("tint"),
        "u_contrast": scalar("contrast"),
        "u_shadow": scalar("shadow"),
        "u_highlights": scalar("highlights"),
        "u_saturation": scalar("saturation"),
        "u_vibrance": scalar("vibrance"),
        "u_hue_shift": scalar("hue_shift"),
    }


@dataclass(frozen=True)
class GpuYuvSource:
    """planar YUV 源描述（系数在 CPU 侧按 _planes_to_bgr_float 同一
    公式预计算，GPU 只做采样/矩阵/量化）。

    Attributes:
        planes: (Y, U, V) 平面，uint8/uint16，二维 C 连续可转
        luma_bits: luma 位深（chroma 归一化同样按此深度，镜像产品内核）
        chroma_x_div / chroma_y_div: 色度抽样因子（422 → 2/1 等）
        full_range: True=满刻度，False=limited-range
        kr / kb: Y'CbCr→RGB 基色系数（bt709/bt2020/smpte170m 家族）
    """

    planes: Tuple[np.ndarray, np.ndarray, np.ndarray]
    luma_bits: int
    chroma_x_div: int
    chroma_y_div: int
    full_range: bool
    kr: float
    kb: float


@dataclass(frozen=True)
class GpuYuv420Output:
    """Final GPU-converted, limited-range BT.709 8-bit encoder planes."""

    planes: Tuple[np.ndarray, np.ndarray, np.ndarray]


@dataclass(frozen=True)
class GpuYuv422P10Output:
    """Final GPU-converted 10-bit 4:2:2 encoder planes.

    The planes use the Topos/FFmpeg ``yuv422p10le`` convention: uint16
    containers carrying nominal 10-bit code values (0..1023), matching the
    native encoder's planar input contract.
    """

    planes: Tuple[np.ndarray, np.ndarray, np.ndarray]


@dataclass(frozen=True)
class GpuWipeElement:
    """GPU 双源转场层栈元素。

    镜像 _composite_layers 的 wipe 预合成：两侧各自执行 Metal 支持的
    clip effects、grade、transform、crop 和 resize 后按掩码混合，再按
    注入层 opacity 进累积缓冲。``mask_spec`` 是 pixel-free 控制数据，
    由统一 GPU shader 计算几何 mask 和转场源效果；旧 ``mask`` 字段仅
    为兼容直接构造旧测试/调用方保留。side 为 None 表示该侧缺失（黑）。
    """

    left: Any  # GpuYuvSource | BGR ndarray | None
    left_grade: Optional[Dict[str, Any]]
    right: Any
    right_grade: Optional[Dict[str, Any]]
    mask: Optional[np.ndarray]
    left_opacity: float
    right_opacity: float
    opacity: float  # 注入层（left clip 位置）自身 opacity
    left_effects: Optional[Tuple[Dict[str, Any], ...]] = None
    right_effects: Optional[Tuple[Dict[str, Any], ...]] = None
    left_transform: Optional[Tuple[Any, ...]] = None
    right_transform: Optional[Tuple[Any, ...]] = None
    left_transform_dims: Optional[Tuple[int, int]] = None
    right_transform_dims: Optional[Tuple[int, int]] = None
    left_crop: Optional[Tuple[float, ...]] = None
    right_crop: Optional[Tuple[float, ...]] = None
    mask_spec: Optional[Any] = None
    transition_type: str = "wipe"


class TimelineGpuExportRenderer:
    """导出线程 GPU 合成渲染器（BGR/YUV 源 → GPU 链 → BGR 或 YUV420P）。

    生命周期由 TimelineExporter 管理：首个适用帧惰性初始化，导出结束在
    **创建线程** ``release()``（幂等）。GL 不可用或渲染异常时返回 None，
    调用方回落 CPU，导出永不中断。
    """

    def __init__(self) -> None:
        self._ctx = None
        self._failed = False
        self._released = False
        self.gl_errors = 0
        self._owner_thread: Optional[int] = None
        self._warned_render_error = False
        self.frames_rendered = 0
        # 惰性分配/尺寸复用的 GL 资源
        self._tex_y = None
        self._tex_u = None
        self._tex_v = None
        self._tex_y_u8 = None
        self._tex_u_u8 = None
        self._tex_v_u8 = None
        self._tex_packed = None
        self._tex_mid: Tuple[Any, Any] = (None, None)  # RGB8 源分辨率 ping-pong
        self._fbo_mid: Tuple[Any, Any] = (None, None)
        self._tex_out = None
        self._fbo_out = None
        self._tex_y_out = None
        self._fbo_y_out = None
        self._tex_uv_out = None
        self._fbo_uv_out = None
        # u16 管线（高精度交付：>8-bit 源走 RGB16UI 全链，8-bit 源仅出口 u16）
        self._tex_mid_u16: Tuple[Any, Any] = (None, None)
        self._fbo_mid_u16: Tuple[Any, Any] = (None, None)
        # T-P1-21：深度效果链桥 scratch（RGBA16F ping-pong）
        self._tex_f2: Tuple[Any, Any] = (None, None)
        self._fbo_f2: Tuple[Any, Any] = (None, None)
        self._f2_unavailable = False
        self._tex_out_u16 = None
        self._fbo_out_u16 = None
        self._prog_yuv = None
        self._prog_grade = None
        self._prog_scale = None
        self._prog_scale_rgb = None
        self._prog_quant_rgb = None
        self._prog_yuv420_luma = None
        self._prog_yuv420_chroma = None
        self._prog_yuv420_direct_luma = None
        self._prog_yuv420_direct_chroma = None
        self._prog_yuv_u16 = None
        self._prog_grade_u16 = None
        self._prog_scale_u16 = None
        self._prog_scale_u16_from_f1 = None
        # 多层混合（P0-GPU-3）
        self._prog_blend_f1 = None
        self._prog_blend_u16 = None
        self._prog_quant_u8 = None
        self._prog_quant_u16 = None
        # 转场（P0-GPU-4）
        self._prog_wipe = None
        self._prog_blend_f4 = None
        self._prog_dip = None
        # RGBA32F 累积 ping-pong（镜像 CPU float32 累积语义）
        self._tex_acc: Tuple[Any, Any] = (None, None)
        self._fbo_acc: Tuple[Any, Any] = (None, None)
        self._acc_unavailable = False
        # wipe 支撑资源（f4 side 对 / wipe 结果 / 掩码纹理）
        self._tex_side: Tuple[Any, Any] = (None, None)
        self._fbo_side: Tuple[Any, Any] = (None, None)
        self._tex_wipe = None
        self._fbo_wipe = None
        self._tex_mask = None
        self._vao: Dict[str, Any] = {}
        self._quad: Any = None
        self.direct_yuv420_frames = 0
        # clip 效果链执行器（pass2.5）；False = 创建失败不再重试
        self._effect_runner: Any = None

    @property
    def failed(self) -> bool:
        """GL 初始化失败后不再重试（调用方据此短路）。"""
        return self._failed

    def _ensure_ready(self) -> bool:
        """惰性创建 standalone context + 程序/全屏四边形；失败后不再重试。"""
        if self._ctx is not None:
            return True
        if self._failed:
            return False
        try:
            import moderngl

            ctx = moderngl.create_standalone_context(require=330)
            self._ctx = ctx
            self._owner_thread = threading.get_ident()
            self._quad = ctx.buffer(
                np.array([-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0],
                         dtype=np.float32).tobytes())
            self._prog_yuv = ctx.program(
                vertex_shader=_VERT_SRC, fragment_shader=_FRAG_YUV_SRC)
            self._prog_grade = ctx.program(
                vertex_shader=_VERT_SRC, fragment_shader=_FRAG_GRADE_SRC)
            self._prog_scale = ctx.program(
                vertex_shader=_VERT_SRC, fragment_shader=_FRAG_SCALE_SRC)
            _blend_gsl = _BLEND_GLSL
            _augmented = {
                name: frag.replace("out vec4 f_color;",
                                   _blend_gsl + "\nout vec4 f_color;", 1)
                for name, frag in (
                    ("blend_f1", _FRAG_BLEND_F1_SRC),
                    ("blend_u16", _FRAG_BLEND_U16_SRC),
                    ("blend_f4", _FRAG_BLEND_F4_SRC),
                )
            }
            for key, src in (
                ("yuv_u16", _FRAG_YUV_U16_SRC),
                ("grade_u16", _FRAG_GRADE_U16_SRC),
                ("scale_u16", _FRAG_SCALE_U16_SRC),
                ("scale_u16_from_f1", _FRAG_SCALE_U16_FROM_F1_SRC),
                ("scale_rgb", _FRAG_SCALE_RGB_SRC),
                ("quant_rgb", _FRAG_QUANT_RGB_SRC),
                ("yuv420_luma", _FRAG_YUV420_LUMA_SRC),
                ("yuv420_chroma", _FRAG_YUV420_CHROMA_SRC),
                ("yuv420_direct_luma", _FRAG_YUV420_DIRECT_LUMA_SRC),
                ("yuv420_direct_chroma", _FRAG_YUV420_DIRECT_CHROMA_SRC),
                ("blend_f1", _augmented["blend_f1"]),
                ("blend_u16", _augmented["blend_u16"]),
                ("quant_u8", _FRAG_QUANT_U8_SRC),
                ("quant_u16", _FRAG_QUANT_U16_SRC),
                ("u16_to_f2", _FRAG_U16_TO_F2_SRC),
                ("f2_to_u16", _FRAG_F2_TO_U16_SRC),
                ("wipe", _FRAG_WIPE_SRC),
                ("blend_f4", _augmented["blend_f4"]),
                ("dip", _FRAG_DIP_SRC),
            ):
                try:
                    prog = ctx.program(vertex_shader=_VERT_SRC, fragment_shader=src)
                except Exception as error:
                    # u16/多层管线整组可选——驱动不支持时仅禁用对应 GPU
                    # 路径（回落 CPU），不影响 u8 单层主链
                    logger.warning(
                        "TimelineGpuExportRenderer: %s pipeline disabled "
                        f"({error}); affected exports fall back to CPU", key)
                    prog = None
                setattr(self, f"_prog_{key}", prog)
            for key, prog in (
                ("yuv", self._prog_yuv),
                ("grade", self._prog_grade),
                ("scale", self._prog_scale),
                ("scale_rgb", self._prog_scale_rgb),
                ("quant_rgb", self._prog_quant_rgb),
                ("yuv420_luma", self._prog_yuv420_luma),
                ("yuv420_chroma", self._prog_yuv420_chroma),
                ("yuv420_direct_luma", self._prog_yuv420_direct_luma),
                ("yuv420_direct_chroma", self._prog_yuv420_direct_chroma),
                ("yuv_u16", self._prog_yuv_u16),
                ("grade_u16", self._prog_grade_u16),
                ("scale_u16", self._prog_scale_u16),
                ("scale_u16_from_f1", self._prog_scale_u16_from_f1),
                ("blend_f1", self._prog_blend_f1),
                ("blend_u16", self._prog_blend_u16),
                ("quant_u8", self._prog_quant_u8),
                ("quant_u16", self._prog_quant_u16),
                ("u16_to_f2", self._prog_u16_to_f2),
                ("f2_to_u16", self._prog_f2_to_u16),
                ("wipe", self._prog_wipe),
                ("blend_f4", self._prog_blend_f4),
                ("dip", self._prog_dip),
            ):
                if prog is not None:
                    self._vao[key] = ctx.vertex_array(
                        prog, [(self._quad, "2f", "in_pos")])
            logger.info(
                "TimelineGpuExportRenderer: headless GL ready "
                f"(renderer={ctx.info.get('GL_RENDERER', '?')})")
            if self._effect_runner is None:
                # 与 standalone context 同线程惰性创建（满足
                # EffectPassRunner 的线程契约）；context 自愈重建后
                # _ctx 置 None → 下次 _ensure_ready 在新 ctx 上重建。
                from src.features.shared.effects.effect_pass_runner import (
                    EffectPassRunner,
                )
                try:
                    self._effect_runner = EffectPassRunner(ctx)
                except Exception as error:
                    self._effect_runner = False
                    logger.warning(
                        "TimelineGpuExportRenderer: EffectPassRunner "
                        f"unavailable ({error}); effects fall back to CPU")
            return True
        except Exception as error:
            self._failed = True
            logger.warning(
                "TimelineGpuExportRenderer: headless GL unavailable "
                f"({error}); export GPU composite passthrough")
            return False

    # ------------------------------------------------------------------
    # 资源尺寸管理（denoise 桥同款：尺寸变化即重建，否则复用）
    # ------------------------------------------------------------------

    def _ensure_int_texture(self, tex: Any, plane: np.ndarray) -> Any:
        h, w = plane.shape
        if tex is None or tex.size != (w, h):
            if tex is not None:
                tex.release()
            tex = self._ctx.texture((w, h), 1, dtype="u2")
            tex.filter = (self._ctx.NEAREST, self._ctx.NEAREST)
            tex.repeat_x = tex.repeat_y = False
        # u2 纹理统一按 uint16 上传（u8 平面升位深拷贝，源分辨率代价小）
        data = plane if plane.dtype == np.uint16 else plane.astype(np.uint16)
        tex.write(np.ascontiguousarray(data))
        return tex

    def _ensure_byte_texture(self, tex: Any, plane: np.ndarray) -> Any:
        """Upload an 8-bit plane without the u8→u16 staging copy."""
        h, w = plane.shape
        if tex is None or tex.size != (w, h):
            if tex is not None:
                tex.release()
            tex = self._ctx.texture((w, h), 1, dtype="u1")
            tex.filter = (self._ctx.NEAREST, self._ctx.NEAREST)
            tex.repeat_x = tex.repeat_y = False
        tex.write(np.ascontiguousarray(plane, dtype=np.uint8))
        return tex

    def _ensure_rgb_fbo_pair(self, width: int, height: int) -> None:
        for i in (0, 1):
            tex, fbo = self._tex_mid[i], self._fbo_mid[i]
            if tex is not None and fbo is not None \
                    and tex.size == (width, height):
                continue
            if fbo is not None:
                fbo.release()
            if tex is not None:
                tex.release()
            tex = self._ctx.texture((width, height), 3, dtype="f1")
            tex.filter = (self._ctx.LINEAR, self._ctx.LINEAR)
            tex.repeat_x = tex.repeat_y = False
            fbo = self._ctx.framebuffer(tex)
            tex_list = list(self._tex_mid)
            fbo_list = list(self._fbo_mid)
            tex_list[i] = tex
            fbo_list[i] = fbo
            self._tex_mid = (tex_list[0], tex_list[1])
            self._fbo_mid = (fbo_list[0], fbo_list[1])

    def _ensure_out_fbo(self, width: int, height: int) -> None:
        if self._fbo_out is None or self._tex_out is None \
                or self._tex_out.size != (width, height):
            if self._fbo_out is not None:
                self._fbo_out.release()
            if self._tex_out is not None:
                self._tex_out.release()
            self._tex_out = self._ctx.texture((width, height), 3, dtype="f1")
            self._fbo_out = self._ctx.framebuffer(self._tex_out)

    def _ensure_yuv420_output_fbos(self, width: int, height: int) -> bool:
        """Allocate reusable R8 luma and RG8 chroma targets."""
        if width % 2 or height % 2:
            return False
        if self._tex_y_out is None or self._tex_y_out.size != (width, height):
            if self._fbo_y_out is not None:
                self._fbo_y_out.release()
            if self._tex_y_out is not None:
                self._tex_y_out.release()
            self._tex_y_out = self._ctx.texture((width, height), 1, dtype="f1")
            self._tex_y_out.filter = (self._ctx.NEAREST, self._ctx.NEAREST)
            self._fbo_y_out = self._ctx.framebuffer(self._tex_y_out)
        chroma_size = (width // 2, height // 2)
        if self._tex_uv_out is None or self._tex_uv_out.size != chroma_size:
            if self._fbo_uv_out is not None:
                self._fbo_uv_out.release()
            if self._tex_uv_out is not None:
                self._tex_uv_out.release()
            self._tex_uv_out = self._ctx.texture(
                chroma_size, 2, dtype="f1")
            self._tex_uv_out.filter = (self._ctx.NEAREST, self._ctx.NEAREST)
            self._fbo_uv_out = self._ctx.framebuffer(self._tex_uv_out)
        return True

    def _convert_rgb_fbo_to_yuv420p(
        self, rgb_tex: Any, width: int, height: int,
    ) -> Optional[GpuYuv420Output]:
        """Run BT.709 limited-range conversion and read back only Y/U/V."""
        if not self._ensure_yuv420_output_fbos(width, height):
            return None
        if self._prog_yuv420_luma is None \
                or self._prog_yuv420_chroma is None:
            return None
        import moderngl

        rgb_tex.use(0)
        luma = self._prog_yuv420_luma
        luma["u_rgb_tex"].value = 0
        self._fbo_y_out.use()
        self._vao["yuv420_luma"].render(mode=moderngl.TRIANGLE_STRIP)

        rgb_tex.use(0)
        chroma = self._prog_yuv420_chroma
        chroma["u_rgb_tex"].value = 0
        self._fbo_uv_out.use()
        self._vao["yuv420_chroma"].render(mode=moderngl.TRIANGLE_STRIP)

        y_data = self._fbo_y_out.read(components=1, dtype="f1", alignment=1)
        uv_data = self._fbo_uv_out.read(components=2, dtype="f1", alignment=1)
        y = np.frombuffer(y_data, dtype=np.uint8).reshape(height, width).copy()
        uv = np.frombuffer(uv_data, dtype=np.uint8).reshape(
            height // 2, width // 2, 2)
        return GpuYuv420Output((y, uv[..., 0].copy(), uv[..., 1].copy()))

    @staticmethod
    def _direct_yuv420_allowed(
        source: Any,
        grade_uniforms: Optional[Dict[str, Any]],
        output_u16: bool,
    ) -> bool:
        """Return whether the lossless-in-contract YUV delivery path applies."""
        if output_u16 or grade_uniforms or not isinstance(source, GpuYuvSource):
            return False
        if source.luma_bits != 8 or source.chroma_x_div != 2 \
                or source.chroma_y_div != 2 or source.full_range:
            return False
        return abs(source.kr - 0.2126) < 1e-5 \
            and abs(source.kb - 0.0722) < 1e-5

    def _render_direct_yuv420p(
        self,
        source: GpuYuvSource,
        out_width: int,
        out_height: int,
        opacity: float,
    ) -> Optional[GpuYuv420Output]:
        """Resize limited BT.709 8-bit planes directly on the GPU.

        This avoids the full-resolution RGB intermediate and the RGB scale/
        conversion passes used by the general renderer.  The output targets
        remain the same reusable R8/RG8 readback buffers as the reference path.
        """
        import moderngl

        if self._prog_yuv420_direct_luma is None \
                or self._prog_yuv420_direct_chroma is None:
            return None
        if "yuv420_direct_luma" not in self._vao \
                or "yuv420_direct_chroma" not in self._vao:
            return None
        if out_width <= 0 or out_height <= 0 \
                or not self._ensure_yuv420_output_fbos(out_width, out_height):
            return None

        y_plane, u_plane, v_plane = source.planes
        if any(plane.ndim != 2 or plane.dtype != np.uint8
               for plane in (y_plane, u_plane, v_plane)):
            return None
        chroma_width, chroma_height = out_width // 2, out_height // 2
        self._tex_y_u8 = self._ensure_byte_texture(self._tex_y_u8, y_plane)
        self._tex_u_u8 = self._ensure_byte_texture(self._tex_u_u8, u_plane)
        self._tex_v_u8 = self._ensure_byte_texture(self._tex_v_u8, v_plane)

        self._tex_y_u8.use(0)
        luma = self._prog_yuv420_direct_luma
        luma["u_y_plane"].value = 0
        luma["u_scale"].value = (
            y_plane.shape[1] / float(out_width),
            y_plane.shape[0] / float(out_height),
        )
        luma["u_opacity"].value = float(np.clip(opacity, 0.0, 1.0))
        self._fbo_y_out.use()
        self._vao["yuv420_direct_luma"].render(
            mode=moderngl.TRIANGLE_STRIP)

        self._tex_u_u8.use(0)
        self._tex_v_u8.use(1)
        chroma = self._prog_yuv420_direct_chroma
        chroma["u_u_plane"].value = 0
        chroma["u_v_plane"].value = 1
        chroma["u_scale"].value = (
            u_plane.shape[1] / float(chroma_width),
            u_plane.shape[0] / float(chroma_height),
        )
        chroma["u_opacity"].value = float(np.clip(opacity, 0.0, 1.0))
        self._fbo_uv_out.use()
        self._vao["yuv420_direct_chroma"].render(
            mode=moderngl.TRIANGLE_STRIP)

        y_data = self._fbo_y_out.read(components=1, dtype="f1", alignment=1)
        uv_data = self._fbo_uv_out.read(components=2, dtype="f1", alignment=1)
        y = np.frombuffer(y_data, dtype=np.uint8).reshape(
            out_height, out_width).copy()
        uv = np.frombuffer(uv_data, dtype=np.uint8).reshape(
            chroma_height, chroma_width, 2)
        self.frames_rendered += 1
        self.direct_yuv420_frames += 1
        return GpuYuv420Output((y, uv[..., 0].copy(), uv[..., 1].copy()))

    def _ensure_u16_fbo_pair(self, width: int, height: int) -> bool:
        """RGB16UI ping-pong（NEAREST；整数纹理不可线性过滤）。"""
        for i in (0, 1):
            tex, fbo = self._tex_mid_u16[i], self._fbo_mid_u16[i]
            if tex is not None and fbo is not None \
                    and tex.size == (width, height):
                continue
            if fbo is not None:
                fbo.release()
            if tex is not None:
                tex.release()
            try:
                tex = self._ctx.texture((width, height), 3, dtype="u2")
                tex.filter = (self._ctx.NEAREST, self._ctx.NEAREST)
                tex.repeat_x = tex.repeat_y = False
                fbo = self._ctx.framebuffer(tex)
            except Exception as error:
                logger.warning(
                    "TimelineGpuExportRenderer: RGB16UI FBO unavailable "
                    f"({error}); high-precision export falls back to CPU")
                return False
            tex_list = list(self._tex_mid_u16)
            fbo_list = list(self._fbo_mid_u16)
            tex_list[i] = tex
            fbo_list[i] = fbo
            self._tex_mid_u16 = (tex_list[0], tex_list[1])
            self._fbo_mid_u16 = (fbo_list[0], fbo_list[1])
        return True

    def _ensure_f2_pair(self, width: int, height: int) -> bool:
        """RGBA16F ping-pong（深度效果链桥 scratch；不可用一次性禁用）。"""
        if self._f2_unavailable:
            return False
        for i in (0, 1):
            tex, fbo = self._tex_f2[i], self._fbo_f2[i]
            if tex is not None and fbo is not None \
                    and tex.size == (width, height):
                continue
            if fbo is not None:
                fbo.release()
            if tex is not None:
                tex.release()
            try:
                tex = self._ctx.texture((width, height), 4, dtype="f2")
                tex.filter = (self._ctx.NEAREST, self._ctx.NEAREST)
                tex.repeat_x = tex.repeat_y = False
                fbo = self._ctx.framebuffer(tex)
            except Exception as error:
                logger.warning(
                    "TimelineGpuExportRenderer: RGBA16F bridge FBO "
                    f"unavailable ({error}); deep effect chain falls back "
                    "to CPU")
                self._f2_unavailable = True
                return False
            tex_list = list(self._tex_f2)
            fbo_list = list(self._fbo_f2)
            tex_list[i] = tex
            fbo_list[i] = fbo
            self._tex_f2 = (tex_list[0], tex_list[1])
            self._fbo_f2 = (fbo_list[0], fbo_list[1])
        return True

    def _note_effect_chain_failure(self, stage: str, error: BaseException) -> None:
        """AUD-18501：效果链桥失败必须可观测——告警一次 + 计入 gl_errors。

        此前 4 处 ``except Exception: return None`` 静默吞掉一切异常（无
        日志、无计数），正是 AUD-18500 的死路径长期不可见的直接原因：
        导出静默全量回落 CPU 时，性能回退无法从日志定位。调用方仍按
        None 整帧回落 CPU（行为不变），只是不再掩盖根因。
        """
        self.gl_errors += 1
        if not self._warned_render_error:
            self._warned_render_error = True
            logger.warning(
                "TimelineGpuExportRenderer: effect chain stage '%s' failed "
                "(%s: %s); frame falls back to CPU",
                stage, type(error).__name__, error,
            )
        else:
            logger.debug(
                "TimelineGpuExportRenderer: effect chain stage '%s' failed "
                "again (%s: %s)",
                stage, type(error).__name__, error,
            )

    def _apply_effect_chain_deep_u16(
        self,
        src_tex: Any,
        effect_chain: Any,
        width: int,
        height: int,
        out_slot: int,
    ) -> Optional[Any]:
        """u16 码值域纹理 → f2 效果链（EPR）→ u16（全 GPU 桥）。

        通道序 RGB 与 grade_u16 一致；量化 half-even 与 u16 管线一致。
        链中断/桥不可用返回 None（调用方整帧回落 CPU）。
        """
        runner = self._effect_runner
        if runner is None or self._prog_u16_to_f2 is None \
                or self._prog_f2_to_u16 is None:
            return None
        if not self._ensure_f2_pair(width, height):
            return None
        try:
            src_tex.use(0)
            self._prog_u16_to_f2["u_input_tex"].value = 0
            self._fbo_f2[0].use()
            self._vao["u16_to_f2"].render(
                mode=self._ctx.TRIANGLE_STRIP)
        except Exception as error:  # noqa: BLE001 — 见 _note_effect_chain_failure
            self._note_effect_chain_failure("u16_to_f2_bridge", error)
            return None
        current = self._tex_f2[0]
        for spec, params in effect_chain:
            try:
                out_glo = runner.run(
                    spec, current.glo, params, width, height, dtype="f2")
            except Exception as error:  # noqa: BLE001 — 见 _note_effect_chain_failure
                self._note_effect_chain_failure(
                    f"deep_u16:{getattr(spec, 'effect_type', '?')}", error)
                return None
            if out_glo == current.glo:
                continue
            out_tex = runner.output_texture()
            if out_tex is None:
                return None
            current = out_tex
        try:
            current.use(0)
            self._prog_f2_to_u16["u_input_tex"].value = 0
            self._fbo_mid_u16[out_slot].use()
            self._vao["f2_to_u16"].render(
                mode=self._ctx.TRIANGLE_STRIP)
        except Exception as error:  # noqa: BLE001 — 见 _note_effect_chain_failure
            self._note_effect_chain_failure("f2_to_u16_bridge", error)
            return None
        return self._tex_mid_u16[out_slot]

    def _ensure_out_fbo_u16(self, width: int, height: int) -> bool:
        if self._fbo_out_u16 is not None and self._tex_out_u16 is not None \
                and self._tex_out_u16.size == (width, height):
            return True
        if self._fbo_out_u16 is not None:
            self._fbo_out_u16.release()
        if self._tex_out_u16 is not None:
            self._tex_out_u16.release()
        try:
            self._tex_out_u16 = self._ctx.texture((width, height), 3,
                                                  dtype="u2")
            self._fbo_out_u16 = self._ctx.framebuffer(self._tex_out_u16)
        except Exception as error:
            logger.warning(
                "TimelineGpuExportRenderer: RGB16UI output FBO unavailable "
                f"({error}); high-precision export falls back to CPU")
            return False
        return True

    def _ensure_acc_pair(self, width: int, height: int) -> bool:
        """RGBA32F 累积 ping-pong（多层混合；镜像 CPU float32 累积）。"""
        if self._acc_unavailable:
            return False
        for i in (0, 1):
            tex, fbo = self._tex_acc[i], self._fbo_acc[i]
            if tex is not None and fbo is not None \
                    and tex.size == (width, height):
                continue
            if fbo is not None:
                fbo.release()
            if tex is not None:
                tex.release()
            try:
                tex = self._ctx.texture((width, height), 4, dtype="f4")
                tex.filter = (self._ctx.NEAREST, self._ctx.NEAREST)
                tex.repeat_x = tex.repeat_y = False
                fbo = self._ctx.framebuffer(tex)
            except Exception as error:
                logger.warning(
                    "TimelineGpuExportRenderer: RGBA32F accumulation "
                    f"unavailable ({error}); multi-layer export falls back "
                    "to CPU")
                self._acc_unavailable = True
                return False
            tex_list = list(self._tex_acc)
            fbo_list = list(self._fbo_acc)
            tex_list[i] = tex
            fbo_list[i] = fbo
            self._tex_acc = (tex_list[0], tex_list[1])
            self._fbo_acc = (fbo_list[0], fbo_list[1])
        return True

    def _ensure_f4_fbo_pair(self, attr_tex: str, attr_fbo: str,
                            width: int, height: int) -> bool:
        """通用 RGBA32F ping-pong 分配（双源转场 side 对）。"""
        tex_pair = getattr(self, attr_tex)
        fbo_pair = getattr(self, attr_fbo)
        for i in (0, 1):
            tex, fbo = tex_pair[i], fbo_pair[i]
            if tex is not None and fbo is not None \
                    and tex.size == (width, height):
                continue
            if fbo is not None:
                fbo.release()
            if tex is not None:
                tex.release()
            try:
                tex = self._ctx.texture((width, height), 4, dtype="f4")
                tex.filter = (self._ctx.NEAREST, self._ctx.NEAREST)
                tex.repeat_x = tex.repeat_y = False
                fbo = self._ctx.framebuffer(tex)
            except Exception as error:
                logger.warning(
                    "TimelineGpuExportRenderer: RGBA32F FBO unavailable "
                    f"({error}); transition export falls back to CPU")
                return False
            tex_list = list(tex_pair)
            fbo_list = list(fbo_pair)
            tex_list[i] = tex
            fbo_list[i] = fbo
            setattr(self, attr_tex, (tex_list[0], tex_list[1]))
            setattr(self, attr_fbo, (fbo_list[0], fbo_list[1]))
            tex_pair = getattr(self, attr_tex)
            fbo_pair = getattr(self, attr_fbo)
        return True

    def _ensure_wipe_resources(self, width: int, height: int) -> bool:
        """双源转场结果 FBO；mask 纹理只服务旧调用方兼容路径。"""
        if not self._ensure_f4_fbo_pair("_tex_side", "_fbo_side",
                                        width, height):
            return False
        if self._fbo_wipe is None or self._tex_wipe is None \
                or self._tex_wipe.size != (width, height):
            if self._fbo_wipe is not None:
                self._fbo_wipe.release()
            if self._tex_wipe is not None:
                self._tex_wipe.release()
            try:
                self._tex_wipe = self._ctx.texture((width, height), 4,
                                                   dtype="f4")
                self._tex_wipe.filter = (self._ctx.NEAREST, self._ctx.NEAREST)
                self._tex_wipe.repeat_x = self._tex_wipe.repeat_y = False
                self._fbo_wipe = self._ctx.framebuffer(self._tex_wipe)
            except Exception as error:
                logger.warning(
                    "TimelineGpuExportRenderer: wipe FBO unavailable "
                    f"({error}); transition export falls back to CPU")
                return False
        if self._tex_mask is None or self._tex_mask.size != (width, height):
            if self._tex_mask is not None:
                self._tex_mask.release()
            try:
                self._tex_mask = self._ctx.texture((width, height), 1,
                                                   dtype="f4")
                self._tex_mask.filter = (self._ctx.NEAREST, self._ctx.NEAREST)
                self._tex_mask.repeat_x = self._tex_mask.repeat_y = False
            except Exception as error:
                logger.warning(
                    "TimelineGpuExportRenderer: mask texture unavailable "
                    f"({error}); transition export falls back to CPU")
                return False
        return True

    # ------------------------------------------------------------------
    # 渲染入口
    # ------------------------------------------------------------------

    def _apply_effect_chain(
        self,
        tex: Any,
        effect_chain: Any,
        width: int,
        height: int,
    ) -> Optional[Any]:
        """pass2.5：在源分辨率上按序执行 clip 效果链（u8 f1 域）。

        effect_chain 元素为 (EffectSpec, 原始采样参数 dict)，参数打包由
        EffectPassRunner 内部完成（与预览同一套 GLSL 与 packer）。
        runner.run 返回裸 glo；输出 Texture 对象经 output_texture() 取回。
        no-op（半径~0 等，runner 原样返回输入 glo）跳过该效果继续。
        runner 不可用或链中断 → None（调用方整帧回落 CPU）。
        """
        runner = self._effect_runner
        if not runner:
            return None
        current = tex
        for spec, params in effect_chain:
            try:
                out_glo = runner.run(
                    spec, current.glo, params, width, height, dtype="f1")
            except Exception as error:  # noqa: BLE001 — 见 _note_effect_chain_failure
                self._note_effect_chain_failure(
                    f"f1:{getattr(spec, 'effect_type', '?')}", error)
                return None
            if out_glo == current.glo:
                continue
            out_tex = runner.output_texture()
            if out_tex is None:
                return None
            current = out_tex
        return current

    def _prepare_layer_texture(
        self,
        source: Any,
        grade_uniforms: Optional[Dict[str, Any]],
        deep_u16: bool,
        effect_chain: Any = None,
    ) -> Optional[Tuple[Any, bool]]:
        """上传源帧并执行 pass1（YUV→RGB/打包直上）、grade 与可选效果链。

        返回 (graded_tex, is_int)：is_int=True 为 u16 码值域纹理
        （deep-u16 链）；失败返回 None。
        """
        import moderngl

        # F4.1: grade_uniforms 兼容单 dict（旧调用）或链序列 [主, 链段...]
        # （_gpu_grade_uniforms 产出）。链段顺序 pre→trim→post 逐段过
        # grade pass（mid FBO 乒乓），与 CPU 顺序应用逐段一致。
        if isinstance(grade_uniforms, (list, tuple)):
            grade_list = [g for g in grade_uniforms if g]
        else:
            grade_list = [grade_uniforms] if grade_uniforms else []
        grade_uniforms = grade_list[0] if grade_list else None
        chain_tail = grade_list[1:]

        def _grade_pass(tex_in, prog, fbo):
            tex_in.use(0)
            prog["u_input_tex"].value = 0
            for name, value in (grade_uniforms or {}).items():
                prog[name].value = value
            fbo.use()
            self._vao[
                "grade_u16" if prog is self._prog_grade_u16 else "grade"
            ].render(mode=moderngl.TRIANGLE_STRIP)

        if isinstance(source, GpuYuvSource):
            y_plane, u_plane, v_plane = source.planes
            height, width = y_plane.shape
            self._tex_y = self._ensure_int_texture(self._tex_y, y_plane)
            self._tex_u = self._ensure_int_texture(self._tex_u, u_plane)
            self._tex_v = self._ensure_int_texture(self._tex_v, v_plane)

            # 系数镜像 _planes_to_bgr_float（色度归一化按 luma 深度）
            code_max = float((1 << source.luma_bits) - 1)
            if source.full_range:
                y_scale, y_offset = 1.0 / code_max, 0.0
                c_scale, c_offset = 1.0 / code_max, -0.5
            else:
                scale_8 = float(1 << max(0, source.luma_bits - 8))
                y_scale = 1.0 / (219.0 * scale_8)
                y_offset = -(16.0 * scale_8) / (219.0 * scale_8)
                c_scale = 1.0 / (224.0 * scale_8)
                c_offset = -(128.0 * scale_8) / (224.0 * scale_8)
            kg = 1.0 - source.kr - source.kb
            uniforms = {
                "u_y_scale": y_scale, "u_y_offset": y_offset,
                "u_c_scale": c_scale, "u_c_offset": c_offset,
                "u_r_from_cr": 2.0 * (1.0 - source.kr),
                "u_b_from_cb": 2.0 * (1.0 - source.kb),
                "u_g_from_cb": -2.0 * source.kb * (1.0 - source.kb) / kg,
                "u_g_from_cr": -2.0 * source.kr * (1.0 - source.kr) / kg,
                "u_chroma_x_div": float(source.chroma_x_div),
                "u_chroma_y_div": float(source.chroma_y_div),
                # cv2 整数输出舍入随 dtype：16U=half-even，8U=half-up
                "u_chroma_round_even":
                    1 if u_plane.dtype == np.uint16 else 0,
            }

            if deep_u16:
                # T-P1-21：deep + 效果链经 u16→f2→EPR→u16 桥（全 GPU）。
                # 桥程序缺失或链失败仍 fail-closed 回落 CPU。
                if effect_chain and (
                        self._prog_u16_to_f2 is None
                        or self._prog_f2_to_u16 is None
                        or self._effect_runner is None):
                    return None
                if self._prog_yuv_u16 is None \
                        or (grade_uniforms and self._prog_grade_u16 is None):
                    return None
                if not self._ensure_u16_fbo_pair(width, height):
                    return None
                self._tex_y.use(0)
                self._tex_u.use(1)
                self._tex_v.use(2)
                prog = self._prog_yuv_u16
                prog["u_y_plane"].value = 0
                prog["u_u_plane"].value = 1
                prog["u_v_plane"].value = 2
                for name, value in uniforms.items():
                    prog[name].value = value
                self._fbo_mid_u16[0].use()
                self._vao["yuv_u16"].render(mode=moderngl.TRIANGLE_STRIP)
                if not grade_uniforms:
                    # AUD-18500：本分支曾提前 return，使下方的 deep 桥
                    # 代码块脱出 if deep_u16: 后对 8-bit 路径执行——
                    # _tex_mid_u16 在非 deep 导出恒为 None，桥吞掉
                    # AttributeError 后整帧回落 CPU（8-bit planar 效果链
                    # 全灭）；deep 无 grade 时效果链又被静默丢弃。
                    # 正确语义：无 grade 且无效果链才直接交付槽 0；
                    # 带效果链则桥接（输入槽 0，输出写槽 1，避免读写
                    # 同纹理形成反馈回路）。
                    if not effect_chain:
                        return self._tex_mid_u16[0], True
                    merged = self._apply_effect_chain_deep_u16(
                        self._tex_mid_u16[0], effect_chain, width, height,
                        out_slot=1)
                    if merged is None:
                        return None
                    return merged, True
                gprog = self._prog_grade_u16
                saved_main = grade_uniforms
                _grade_pass(self._tex_mid_u16[0], gprog, self._fbo_mid_u16[1])
                slot = 1
                for extra in chain_tail:
                    grade_uniforms = extra
                    _grade_pass(
                        self._tex_mid_u16[slot], gprog,
                        self._fbo_mid_u16[1 - slot])
                    slot = 1 - slot
                grade_uniforms = saved_main
                if effect_chain:
                    merged = self._apply_effect_chain_deep_u16(
                        self._tex_mid_u16[1], effect_chain, width, height,
                        out_slot=0)
                    if merged is None:
                        return None
                    return merged, True
                return self._tex_mid_u16[1], True

            if self._prog_yuv is None \
                    or (grade_uniforms and self._prog_grade is None):
                return None
            self._ensure_rgb_fbo_pair(width, height)
            self._tex_y.use(0)
            self._tex_u.use(1)
            self._tex_v.use(2)
            prog = self._prog_yuv
            prog["u_y_plane"].value = 0
            prog["u_u_plane"].value = 1
            prog["u_v_plane"].value = 2
            for name, value in uniforms.items():
                prog[name].value = value
            self._fbo_mid[0].use()
            self._vao["yuv"].render(mode=moderngl.TRIANGLE_STRIP)
            result_tex = self._tex_mid[0]
            if grade_uniforms:
                gprog = self._prog_grade
                saved_main = grade_uniforms
                _grade_pass(self._tex_mid[0], gprog, self._fbo_mid[1])
                slot = 1
                for extra in chain_tail:
                    grade_uniforms = extra
                    _grade_pass(
                        self._tex_mid[slot], gprog, self._fbo_mid[1 - slot])
                    slot = 1 - slot
                grade_uniforms = saved_main
                result_tex = self._tex_mid[slot]
            if effect_chain:
                result_tex = self._apply_effect_chain(
                    result_tex, effect_chain, width, height)
                if result_tex is None:
                    return None
            return result_tex, False

        # 打包 BGR u8（图片层/打包解码帧）
        height, width = source.shape[:2]
        self._ensure_rgb_fbo_pair(width, height)
        tex = self._tex_packed
        if tex is None or tex.size != (width, height):
            if tex is not None:
                tex.release()
            tex = self._ctx.texture((width, height), 3, dtype="f1")
            tex.filter = (self._ctx.LINEAR, self._ctx.LINEAR)
            tex.repeat_x = tex.repeat_y = False
            # 存储字节序 BGR，采样经 swizzle 还原为 RGB
            tex.swizzle = "BGR"
            self._tex_packed = tex
        tex.write(np.ascontiguousarray(source[..., :3]))
        result_tex = tex
        if grade_uniforms:
            gprog = self._prog_grade
            saved_main = grade_uniforms
            _grade_pass(tex, gprog, self._fbo_mid[0])
            slot = 0
            for extra in chain_tail:
                grade_uniforms = extra
                _grade_pass(
                    self._tex_mid[slot], gprog, self._fbo_mid[1 - slot])
                slot = 1 - slot
            grade_uniforms = saved_main
            result_tex = self._tex_mid[slot]
        if effect_chain:
            result_tex = self._apply_effect_chain(
                result_tex, effect_chain, width, height)
            if result_tex is None:
                return None
        return result_tex, False

    def render_planar(
        self,
        source: GpuYuvSource,
        grade_uniforms: Optional[Dict[str, Any]],
        out_width: int,
        out_height: int,
        opacity: float,
        output_u16: bool = False,
        output_yuv420p: bool = False,
    ) -> Optional[Any]:
        """planar YUV 源 → GPU 全链 → BGR u8/u16 或 YUV420P encoder planes。

        u16 交付（高精度导出）：planar 源一律走 RGB16UI 全链（镜像
        _extract_frame_data 在高精度下对任意位深 planar 源都转 bgr16
        的语义——8-bit 码值经 limited-range 归一化后并非 ×257 倍数）。
        """
        if not self._ensure_ready():
            return None
        if output_yuv420p and self._direct_yuv420_allowed(
                source, grade_uniforms, output_u16):
            try:
                direct = self._render_direct_yuv420p(
                    source, out_width, out_height, opacity)
                if direct is not None:
                    return direct
            except Exception as error:
                self.gl_errors += 1
                if not self._warned_render_error:
                    self._warned_render_error = True
                    logger.warning(
                        "TimelineGpuExportRenderer: direct YUV420 render "
                        f"failed ({error}); trying general GPU path")
        deep_u16 = bool(output_u16)
        try:
            prepared = self._prepare_layer_texture(
                source, grade_uniforms, deep_u16)
            if prepared is None:
                return None
            graded_tex, is_int = prepared
            if output_yuv420p:
                if output_u16:
                    return None
                return self._scale_and_read_yuv420p(
                    graded_tex, out_width, out_height, opacity)
            if output_u16:
                return self._scale_and_read_u16(
                    graded_tex, out_width, out_height, opacity,
                    from_integer=is_int)
            return self._scale_and_read(
                graded_tex, out_width, out_height, opacity)
        except Exception as error:
            self.gl_errors += 1
            if not self._warned_render_error:
                self._warned_render_error = True
                logger.warning(
                    "TimelineGpuExportRenderer: planar render failed "
                    f"({error}); CPU fallback for this export")
            return None

    def render_packed_bgr(
        self,
        frame_bgr_u8: np.ndarray,
        grade_uniforms: Optional[Dict[str, Any]],
        out_width: int,
        out_height: int,
        opacity: float,
        output_u16: bool = False,
        output_yuv420p: bool = False,
    ) -> Optional[Any]:
        """打包 BGR u8 源 → GPU grade+缩放 → BGR u8/u16 或 YUV420P。"""
        if not self._ensure_ready():
            return None
        try:
            prepared = self._prepare_layer_texture(
                frame_bgr_u8, grade_uniforms, False)
            if prepared is None:
                return None
            graded_tex, is_int = prepared
            if output_yuv420p:
                if output_u16:
                    return None
                return self._scale_and_read_yuv420p(
                    graded_tex, out_width, out_height, opacity)
            if output_u16:
                return self._scale_and_read_u16(
                    graded_tex, out_width, out_height, opacity,
                    from_integer=is_int)
            return self._scale_and_read(
                graded_tex, out_width, out_height, opacity)
        except Exception as error:
            self.gl_errors += 1
            if not self._warned_render_error:
                self._warned_render_error = True
                logger.warning(
                    "TimelineGpuExportRenderer: packed render failed "
                    f"({error}); CPU fallback for this export")
            return None

    def render_layer_stack(
        self,
        layers: List[Any],
        out_width: int,
        out_height: int,
        output_u16: bool = False,
        dip_color: Optional[Tuple[float, float, float]] = None,
        dip_factor: float = 0.0,
        output_yuv420p: bool = False,
    ) -> Optional[Any]:
        """多层 GPU 合成 → BGR u8/u16，或直接输出 YUV420P 编码平面。

        P0-GPU-3/4：镜像 _composite_layers 的多层语义——每层
        extract→grade（各自域）→ resize→opacity lerp 进 float 累积缓冲，
        末端一次量化。layers 元素 = (GpuYuvSource|BGR u8 ndarray,
        grade_uniforms|None, opacity) 或 GpuWipeElement（空间转场预合成
        注入），顺序为 track 自下而上。dip_color/dip_factor 为
        dip_to_color 的末端颜色 over（CPU：composite*(1-cf)+color*cf）。
        """
        if not self._ensure_ready():
            return None
        if not self._ensure_acc_pair(out_width, out_height):
            return None
        has_wipe = any(isinstance(element, GpuWipeElement)
                       for element in layers)
        if has_wipe and not self._ensure_wipe_resources(
                out_width, out_height):
            return None
        try:
            import moderngl

            self._fbo_acc[0].use()
            self._ctx.clear(0.0, 0.0, 0.0, 0.0)
            current = 0
            first_element = True
            for element in layers:
                if isinstance(element, GpuWipeElement):
                    result = self._render_wipe_element(
                        element, out_width, out_height, current,
                        first_element, output_u16)
                    if result is None:
                        return None
                    current, _ = result
                    first_element = False
                    continue
                source, grade_uniforms, opacity, *rest = element
                blend_code = int(rest[0]) if rest else 0
                # OpenGL 效果链：(EffectSpec, 原始参数 dict) 元组序列
                # （Metal 元素该槽位是 MSL uniform dict——两种后端各自
                # 解释自己 resolve 出的契约）。
                effect_chain = rest[1] if len(rest) > 1 else None
                deep_u16 = (
                    bool(output_u16)
                    and isinstance(source, GpuYuvSource)
                )
                prepared = self._prepare_layer_texture(
                    source, grade_uniforms, deep_u16, effect_chain)
                if prepared is None:
                    return None
                graded_tex, is_int = prepared
                vao_key = "blend_u16" if is_int else "blend_f1"
                prog = getattr(self, f"_prog_{vao_key}")
                if prog is None or vao_key not in self._vao:
                    return None
                graded_tex.use(0)
                self._tex_acc[current].use(1)
                prog["u_layer_tex"].value = 0
                prog["u_acc_tex"].value = 1
                prog["u_opacity"].value = float(opacity)
                # A1: 图层混合模式（0 = normal 纯 alpha over）
                try:
                    prog["u_blend_mode"].value = blend_code
                except KeyError:
                    pass
                self._fbo_acc[1 - current].use()
                self._vao[vao_key].render(mode=moderngl.TRIANGLE_STRIP)
                current = 1 - current
                first_element = False

            if dip_color is not None and dip_factor > 0.0 \
                    and self._prog_dip is not None:
                self._tex_acc[current].use(0)
                dprog = self._prog_dip
                dprog["u_acc_tex"].value = 0
                dprog["u_dip_color"].value = tuple(
                    float(c) for c in dip_color)
                dprog["u_dip_factor"].value = float(dip_factor)
                self._fbo_acc[1 - current].use()
                self._vao["dip"].render(mode=moderngl.TRIANGLE_STRIP)
                current = 1 - current

            if output_yuv420p:
                if output_u16 or self._prog_quant_rgb is None:
                    return None
                self._ensure_out_fbo(out_width, out_height)
                self._tex_acc[current].use(0)
                qrgb = self._prog_quant_rgb
                qrgb["u_acc_tex"].value = 0
                self._fbo_out.use()
                self._vao["quant_rgb"].render(
                    mode=moderngl.TRIANGLE_STRIP)
                return self._convert_rgb_fbo_to_yuv420p(
                    self._tex_out, out_width, out_height)

            quant_key = "quant_u16" if output_u16 else "quant_u8"
            qprog = getattr(self, f"_prog_{quant_key}")
            if qprog is None or quant_key not in self._vao:
                return None
            self._tex_acc[current].use(0)
            qprog["u_acc_tex"].value = 0
            if output_u16:
                if not self._ensure_out_fbo_u16(out_width, out_height):
                    return None
                self._fbo_out_u16.use()
                self._vao[quant_key].render(mode=moderngl.TRIANGLE_STRIP)
                data = self._fbo_out_u16.read(
                    components=3, dtype="u2", alignment=2)
                self.frames_rendered += 1
                return np.frombuffer(data, dtype=np.uint16).reshape(
                    out_height, out_width, 3).copy()
            self._ensure_out_fbo(out_width, out_height)
            self._fbo_out.use()
            self._vao[quant_key].render(mode=moderngl.TRIANGLE_STRIP)
            data = self._fbo_out.read(components=3, dtype="f1", alignment=1)
            self.frames_rendered += 1
            return np.frombuffer(data, dtype=np.uint8).reshape(
                out_height, out_width, 3).copy()
        except Exception as error:
            self.gl_errors += 1
            if not self._warned_render_error:
                self._warned_render_error = True
                logger.warning(
                    "TimelineGpuExportRenderer: layer stack render failed "
                    f"({error}); CPU fallback for this export")
            return None

    def _render_wipe_element(
        self,
        element: GpuWipeElement,
        out_width: int,
        out_height: int,
        acc_current: int,
        first_element: bool,
        output_u16: bool,
    ) -> Optional[Tuple[int, bool]]:
        """渲染一个双源转场元素并注入累积缓冲，返回 (新 acc 槽位, ok)。

        side 准备复用 blend shader（opacity=1 即"仅归一化层值"）：
        u8 域 bilinear→round→/255，u16 域手动双线性→half-even→/65535，
        与 CPU wipe 分支的 resize→normalise 量化点一致。
        """
        import moderngl

        for side_index, (side, side_grade) in enumerate((
                (element.left, element.left_grade),
                (element.right, element.right_grade))):
            if side is None:
                # CPU 语义：缺失侧 = 黑
                self._fbo_side[side_index].use()
                self._ctx.clear(0.0, 0.0, 0.0, 0.0)
                continue
            deep_u16 = bool(output_u16) and isinstance(side, GpuYuvSource)
            prepared = self._prepare_layer_texture(
                side, side_grade, deep_u16)
            if prepared is None:
                return None
            graded_tex, is_int = prepared
            vao_key = "blend_u16" if is_int else "blend_f1"
            prog = getattr(self, f"_prog_{vao_key}")
            if prog is None or vao_key not in self._vao:
                return None
            graded_tex.use(0)
            self._tex_acc[0].use(1)  # opacity=1 时 acc 项为 0，仅占位
            prog["u_layer_tex"].value = 0
            prog["u_acc_tex"].value = 1
            prog["u_opacity"].value = 1.0
            # prep 内部会切换绑定的渲染目标（pass1/grade 写 mid FBO），
            # side 混合前必须重新绑定 side 目标
            self._fbo_side[side_index].use()
            self._vao[vao_key].render(mode=moderngl.TRIANGLE_STRIP)

        if self._prog_wipe is None or "wipe" not in self._vao \
                or self._prog_blend_f4 is None or "blend_f4" not in self._vao:
            return None
        self._tex_side[0].use(0)
        self._tex_side[1].use(1)
        wprog = self._prog_wipe
        wprog["u_left_tex"].value = 0
        wprog["u_right_tex"].value = 1
        wprog["u_left_opacity"].value = float(
            max(0.0, min(1.0, element.left_opacity)))
        wprog["u_right_opacity"].value = float(
            max(0.0, min(1.0, element.right_opacity)))
        spec = element.mask_spec
        if spec is None:
            # Compatibility for old direct callers. The production timeline
            # path always supplies mask_spec and never uploads this pixel mask.
            if element.mask is None:
                return None
            mask = np.ascontiguousarray(element.mask, dtype=np.float32)
            if mask.shape != (out_height, out_width):
                return None
            self._tex_mask.write(mask)
            self._tex_mask.use(2)
            wprog["u_mask_tex"].value = 2
            wprog["u_use_procedural"].value = 0
            # Uniforms are retained by a ModernGL program between draws, so
            # explicitly clear the procedural branch after a spec-backed
            # transition has been rendered on the same renderer.
            wprog["u_transition_kind"].value = 0
            wprog["u_transition_mode"].value = 0
            wprog["u_effect_mode"].value = 0
        else:
            self._set_transition_uniforms(wprog, element, spec)
        self._fbo_wipe.use()
        self._vao["wipe"].render(mode=moderngl.TRIANGLE_STRIP)

        # 注入累积（镜像 CPU：首元素 copy 不乘 opacity，其余 lerp）
        self._tex_wipe.use(0)
        self._tex_acc[acc_current].use(1)
        iprog = self._prog_blend_f4
        iprog["u_layer_tex"].value = 0
        iprog["u_acc_tex"].value = 1
        iprog["u_opacity"].value = float(element.opacity)
        iprog["u_first"].value = 1 if first_element else 0
        self._fbo_acc[1 - acc_current].use()
        self._vao["blend_f4"].render(mode=moderngl.TRIANGLE_STRIP)
        return 1 - acc_current, True

    @staticmethod
    def _set_transition_uniforms(program: Any, element: GpuWipeElement,
                                 spec: Any) -> None:
        """Upload a pixel-free transition control block to the GL shader."""
        vertices = np.zeros((48, 2), dtype=np.float32)
        raw_vertices = tuple(getattr(spec, "vertices", ()) or ())[:48]
        if raw_vertices:
            vertices[:len(raw_vertices)] = np.asarray(
                raw_vertices, dtype=np.float32)
        program["u_left_tex"].value = 0
        program["u_right_tex"].value = 1
        program["u_progress"].value = float(getattr(spec, "progress", 0.0))
        program["u_left_opacity"].value = float(element.left_opacity)
        program["u_right_opacity"].value = float(element.right_opacity)
        program["u_blur_ratio"].value = float(
            getattr(spec, "blur_ratio", 0.045))
        program["u_transition_mode"].value = int(
            getattr(spec, "transition_mode", 0))
        program["u_use_procedural"].value = 1
        program["u_transition_kind"].value = int(getattr(spec, "kind", 0))
        program["u_direction"].value = int(getattr(spec, "direction", 0))
        program["u_reverse"].value = 1 if bool(
            getattr(spec, "reverse", False)) else 0
        program["u_angle"].value = float(getattr(spec, "angle", 0.0))
        program["u_softness"].value = float(
            getattr(spec, "softness", 0.02))
        program["u_vertex_count"].value = len(raw_vertices)
        program["u_vertices"].write(vertices.tobytes())
        program["u_effect_mode"].value = int(getattr(spec, "effect_mode", 0))
        program["u_blur_radius"].value = float(
            getattr(spec, "blur_radius", 0.0))
        program["u_blur_mix"].value = float(getattr(spec, "blur_mix", 0.0))
        program["u_shutter"].value = float(getattr(spec, "shutter", 1.0))
        program["u_blur_strength"].value = float(
            getattr(spec, "blur_strength", 0.18))
        program["u_spin_angle"].value = float(
            getattr(spec, "spin_angle", 18.0))
        program["u_split_amount"].value = float(
            getattr(spec, "split_amount", 0.045))
        program["u_split_mix"].value = float(
            getattr(spec, "split_mix", 0.96))
        # LIB1（L03）：新增 uniform 未被旧 spec 使用时由链接器剔除，
        # 以成员存在性守护（缺省值=旧路径语义）。
        if "u_center" in program:
            center = getattr(spec, "center", (0.5, 0.5)) or (0.5, 0.5)
            program["u_center"].value = (float(center[0]), float(center[1]))
        if "u_resample_mode" in program:
            program["u_resample_mode"].value = int(
                getattr(spec, "resample_mode", 0))
        if "u_push_dir" in program:
            program["u_push_dir"].value = int(
                getattr(spec, "push_direction", 0))
        if "u_mask_p0" in program:
            import numpy as _np
            mask_params = tuple(getattr(spec, "mask_params", ()) or ())[:8]
            packed = _np.zeros((2, 4), dtype=_np.float32)
            if mask_params:
                packed[0, :len(mask_params)] = mask_params[:4]
                if len(mask_params) > 4:
                    packed[1, :len(mask_params) - 4] = mask_params[4:8]
            program["u_mask_p0"].write(packed[0].tobytes())
            program["u_mask_p1"].write(packed[1].tobytes())

    def _scale_and_read(
        self,
        source_tex: Any,
        out_width: int,
        out_height: int,
        opacity: float,
    ) -> np.ndarray:
        import moderngl

        self._ensure_out_fbo(out_width, out_height)
        source_tex.use(0)
        prog = self._prog_scale
        prog["u_input_tex"].value = 0
        prog["u_opacity"].value = float(opacity)
        self._fbo_out.use()
        self._vao["scale"].render(mode=moderngl.TRIANGLE_STRIP)
        data = self._fbo_out.read(components=3, dtype="f1", alignment=1)
        self.frames_rendered += 1
        # GL 行序自底向上；与上传/采样约定一致，直接 reshape 即为顶左原点。
        # frombuffer 视图只读——copy 对齐 CPU 路径"可写帧"语义，防止下游
        # 字幕烧录/遮版/编码的未来原地写炸掉导出（4K ~25MB 拷贝 ≈2ms）
        return np.frombuffer(data, dtype=np.uint8).reshape(
            out_height, out_width, 3).copy()

    def _scale_and_read_yuv420p(
        self,
        source_tex: Any,
        out_width: int,
        out_height: int,
        opacity: float,
    ) -> Optional[GpuYuv420Output]:
        """Scale on GPU, then convert to encoder-ready BT.709 YUV420P."""
        import moderngl

        if self._prog_scale_rgb is None:
            return None
        self._ensure_out_fbo(out_width, out_height)
        source_tex.use(0)
        prog = self._prog_scale_rgb
        prog["u_input_tex"].value = 0
        prog["u_opacity"].value = float(opacity)
        self._fbo_out.use()
        self._vao["scale_rgb"].render(mode=moderngl.TRIANGLE_STRIP)
        self.frames_rendered += 1
        return self._convert_rgb_fbo_to_yuv420p(
            self._tex_out, out_width, out_height)

    def _scale_and_read_u16(
        self,
        source_tex: Any,
        out_width: int,
        out_height: int,
        opacity: float,
        from_integer: bool,
    ) -> Optional[np.ndarray]:
        """末端缩放 → RGB16UI → u16 回读（高精度交付出口）。"""
        import moderngl

        vao_key = "scale_u16" if from_integer else "scale_u16_from_f1"
        prog = getattr(self, f"_prog_{vao_key}")
        if prog is None or vao_key not in self._vao:
            return None
        if not self._ensure_out_fbo_u16(out_width, out_height):
            return None
        source_tex.use(0)
        prog["u_input_tex"].value = 0
        prog["u_opacity"].value = float(opacity)
        self._fbo_out_u16.use()
        self._vao[vao_key].render(mode=moderngl.TRIANGLE_STRIP)
        data = self._fbo_out_u16.read(components=3, dtype="u2", alignment=2)
        self.frames_rendered += 1
        return np.frombuffer(data, dtype=np.uint16).reshape(
            out_height, out_width, 3).copy()

    def release(self) -> None:
        """释放全部 GL 资源（幂等；必须在创建线程调用）。

        AUD-18502：``_released`` 只在资源真正释放完毕后闩死。跨线程误调
        分支仅告警返回、不置位——否则一次误调（如异常时序下其他线程先于
        生产者线程的 finally 调用）会让创建线程随后的合法释放 no-op，
        整个 standalone context 永久泄漏且不可恢复。
        """
        if self._released:
            return
        if self._ctx is None:
            self._released = True
            return
        if self._owner_thread is not None \
                and threading.get_ident() != self._owner_thread:
            # 集成层契约违背（创建线程未释放）——跨线程删除对象未定义，
            # 只告警并交由进程退出回收，绝不冒险崩掉导出线程；不置位
            # ``_released``，创建线程稍后的合法 finally 仍可重试释放。
            logger.warning(
                "TimelineGpuExportRenderer: release() called off the "
                "creating thread; leaking GL context intentionally until "
                "the owner thread releases it")
            return
        resources = [
            self._tex_y, self._tex_u, self._tex_v, self._tex_packed,
            self._tex_y_u8, self._tex_u_u8, self._tex_v_u8,
            self._fbo_mid[0], self._tex_mid[0],
            self._fbo_mid[1], self._tex_mid[1],
            self._fbo_out, self._tex_out,
            self._fbo_y_out, self._tex_y_out,
            self._fbo_uv_out, self._tex_uv_out,
            self._fbo_mid_u16[0], self._tex_mid_u16[0],
            self._fbo_mid_u16[1], self._tex_mid_u16[1],
            self._fbo_out_u16, self._tex_out_u16,
            self._fbo_acc[0], self._tex_acc[0],
            self._fbo_acc[1], self._tex_acc[1],
            self._fbo_side[0], self._tex_side[0],
            self._fbo_side[1], self._tex_side[1],
            self._fbo_wipe, self._tex_wipe, self._tex_mask,
        ]
        for vao in self._vao.values():
            try:
                vao.release()
            except Exception:
                pass
        for prog in (self._prog_yuv, self._prog_grade, self._prog_scale,
                     self._prog_scale_rgb, self._prog_quant_rgb,
                     self._prog_yuv420_luma, self._prog_yuv420_chroma,
                     self._prog_yuv420_direct_luma,
                     self._prog_yuv420_direct_chroma,
                     self._prog_yuv_u16, self._prog_grade_u16,
                     self._prog_scale_u16, self._prog_scale_u16_from_f1,
                     self._prog_blend_f1, self._prog_blend_u16,
                     self._prog_quant_u8, self._prog_quant_u16,
                     self._prog_wipe, self._prog_blend_f4, self._prog_dip):
            try:
                prog.release()
            except Exception:
                pass
        for obj in resources:
            if obj is not None:
                try:
                    obj.release()
                except Exception:
                    pass
        try:
            self._quad.release()
        except Exception:
            pass
        if self._effect_runner:
            try:
                self._effect_runner.release()
            except Exception:
                pass
            self._effect_runner = None
        try:
            self._ctx.release()
        except Exception:
            pass
        self._ctx = None
        # AUD-18502：资源已全部释放，此刻才闩死幂等标志。
        self._released = True
        if self.frames_rendered:
            logger.info(
                "TimelineGpuExportRenderer: released "
                f"({self.frames_rendered} frames rendered)")
