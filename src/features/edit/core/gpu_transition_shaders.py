"""GPU-only transition shader sources shared by preview and export.

The transition stage receives two textures that are already resident in the
active graphics API.  Geometry, motion blur and source-channel offsets are
evaluated per pixel by the shader; callers only upload a small uniform block
describing the transition.  No per-frame CPU pixel mask or intermediate image
is part of this contract.
"""

from __future__ import annotations


GPU_TRANSITION_VERTEX_SHADER_SOURCE = """
#version 330
in vec2 in_pos;
out vec2 v_texcoord;
void main() {
    v_texcoord = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""


GPU_TRANSITION_FRAGMENT_SHADER_SOURCE = """
#version 330
uniform sampler2D u_left_tex;
uniform sampler2D u_right_tex;
uniform sampler2D u_mask_tex; // legacy compatibility only
uniform float u_left_opacity;
uniform float u_right_opacity;
uniform float u_progress;
uniform float u_blur_ratio;
uniform int u_transition_mode; // 0=blur, 1=defocus, 2=film, 3=smooth cut

uniform int u_use_procedural;
uniform int u_transition_kind; // 0=wipe, 1=circle, 2=ellipse, 3=polygon, 4=full frame
uniform int u_direction;       // 0=left, 1=right, 2=up, 3=down, 4=angle
uniform int u_reverse;
uniform float u_angle;
uniform float u_softness;
uniform int u_vertex_count;
uniform vec2 u_vertices[48];

uniform int u_effect_mode; // 0=none, 1=motion blur, 2=whip, 3=zoom, 4=spin, 5=RGB split
uniform float u_blur_radius;
uniform float u_blur_mix;
uniform float u_shutter;
uniform float u_blur_strength;
uniform float u_spin_angle;
uniform float u_split_amount;
uniform float u_split_mix;

// LIB1（L03）：kind=5 时钟极角中心（归一）；resample_mode=1 双源平移。
uniform vec2 u_center;
uniform int u_resample_mode; // 0=mask composite, 1=dual-source push
uniform int u_push_dir;      // 0=left, 1=right, 2=up, 3=down
uniform vec4 u_mask_p0;      // LIB1（L13）mask 家族参数 0-3（kind 6-10）
uniform vec4 u_mask_p1;      // LIB1（L13）mask 家族参数 4-7（预留）

in vec2 v_texcoord;
out vec4 f_color;

const float PI = 3.14159265358979323846;

vec3 blur9(sampler2D tex, vec2 uv, vec2 texel, vec2 delta) {
    vec3 result = texture(tex, uv).rgb * 0.25;
    result += texture(tex, uv + delta).rgb * 0.125;
    result += texture(tex, uv - delta).rgb * 0.125;
    result += texture(tex, uv + vec2(delta.x, -delta.y)).rgb * 0.0625;
    result += texture(tex, uv + vec2(-delta.x, delta.y)).rgb * 0.0625;
    result += texture(tex, uv + vec2(delta.x, 0.0)).rgb * 0.125;
    result += texture(tex, uv - vec2(delta.x, 0.0)).rgb * 0.125;
    result += texture(tex, uv + vec2(0.0, delta.y)).rgb * 0.0625;
    result += texture(tex, uv - vec2(0.0, delta.y)).rgb * 0.0625;
    return result;
}

vec3 directional_blur(sampler2D tex, vec2 uv, vec2 axis, float radius) {
    vec2 texel = 1.0 / vec2(textureSize(tex, 0));
    vec2 delta = axis * texel * radius;
    return blur9(tex, uv, texel, delta);
}

vec2 rotate_point(vec2 point, float radians) {
    float c = cos(radians);
    float s = sin(radians);
    return vec2(point.x * c - point.y * s, point.x * s + point.y * c);
}

vec3 radial_blur(sampler2D tex, vec2 uv, bool spin, float strength) {
    vec2 centre = vec2(0.5);
    vec2 delta = uv - centre;
    vec3 result = vec3(0.0);
    // Fixed bounds are intentional: this is a bounded shader cost on every
    // driver and does not turn a user parameter into a dynamic loop bound.
    for (int i = 0; i < 7; ++i) {
        float amount = (float(i) - 3.0) / 3.0;
        vec2 sample_uv;
        if (spin) {
            sample_uv = centre + rotate_point(delta, amount * strength);
        } else {
            sample_uv = centre + delta * (1.0 + amount * strength);
        }
        result += texture(tex, clamp(sample_uv, 0.0, 1.0)).rgb / 7.0;
    }
    return result;
}

vec3 sample_effect(sampler2D tex, vec2 uv) {
    float progress = clamp(u_progress, 0.0, 1.0);
    float pulse = sin(PI * progress);
    if (u_effect_mode == 1 || u_effect_mode == 2) {
        vec2 axis = vec2(1.0, 0.0);
        if (u_direction == 2 || u_direction == 3) {
            axis = vec2(0.0, 1.0);
        }
        float radius = max(0.0, u_blur_radius)
            * pow(max(pulse, 0.0), u_effect_mode == 2 ? 0.72 : 0.90)
            * clamp(u_shutter, 0.0, 2.0);
        float mix_amount = clamp(u_blur_mix, 0.0, 1.0)
            * pow(max(pulse, 0.0), u_effect_mode == 2 ? 0.35 : 0.35);
        vec2 shifted_uv = uv;
        if (u_effect_mode == 2) {
            float travel = 0.06 * pulse * pow(max(pulse, 0.0), 0.65);
            if (u_direction == 0 || u_direction == 2) travel = -travel;
            shifted_uv = clamp(uv + axis * travel, 0.0, 1.0);
        }
        vec3 raw = texture(tex, shifted_uv).rgb;
        vec3 filtered = directional_blur(tex, shifted_uv, axis, radius);
        return mix(raw, filtered, mix_amount);
    }
    if (u_effect_mode == 3 || u_effect_mode == 4) {
        float strength = u_effect_mode == 3
            ? max(0.0, u_blur_strength) * pow(max(pulse, 0.0), 0.72)
            : radians(max(0.0, u_spin_angle)) * pow(max(pulse, 0.0), 0.72);
        if (u_direction == 1 || u_direction == 3) strength = -strength;
        vec3 raw = texture(tex, uv).rgb;
        vec3 filtered = radial_blur(tex, uv, u_effect_mode == 4, strength);
        float mix_amount = clamp(u_blur_mix, 0.0, 1.0)
            * pow(max(pulse, 0.0), 0.35);
        return mix(raw, filtered, mix_amount);
    }
    if (u_effect_mode == 5) {
        vec2 axis = (u_direction == 2 || u_direction == 3)
            ? vec2(0.0, 1.0) : vec2(1.0, 0.0);
        float sign = (u_direction == 0 || u_direction == 2) ? -1.0 : 1.0;
        float amount = max(0.0, u_split_amount)
            * pow(max(pulse, 0.0), 0.7);
        vec3 raw = texture(tex, uv).rgb;
        vec3 shifted = raw;
        shifted.r = texture(tex, clamp(uv + axis * amount * sign,
                                       0.0, 1.0)).r;
        shifted.b = texture(tex, clamp(uv - axis * amount * sign,
                                       0.0, 1.0)).b;
        return mix(raw, shifted, clamp(u_split_mix, 0.0, 1.0)
                   * pow(max(pulse, 0.0), 0.25));
    }
    return texture(tex, uv).rgb;
}

uint lib1_hash_u32(uint x, uint y, uint s) {
    // LIB1/X06（L13）：uint32 murmur3 风格雪崩，与 CPU
    // noise_lattice_hash_u32（uint64 截 32 位）逐位一致。
    uint h = (x * 0x9E3779B1u) ^ (y * 0x85EBCA77u) ^ (s * 0xC2B2AE3Du);
    h ^= h >> 15u;
    h *= 0x2545F491u;
    h ^= h >> 13u;
    h *= 0x9E3779B1u;
    h ^= h >> 16u;
    return h;
}

float lib1_hash01(uint x, uint y, uint s) {
    return float(lib1_hash_u32(x, y, s)) / 4294967296.0;
}

float polygon_inside(vec2 point) {
    bool inside = false;
    for (int i = 0; i < 48; ++i) {
        if (i >= u_vertex_count) break;
        int next_index = (i + 1) % max(u_vertex_count, 1);
        vec2 a = u_vertices[i] * clamp(u_progress, 0.0, 1.0);
        vec2 b = u_vertices[next_index] * clamp(u_progress, 0.0, 1.0);
        bool crosses = (a.y > point.y) != (b.y > point.y);
        float denominator = b.y - a.y;
        float safe_denominator = abs(denominator) > 1.0e-8
            ? denominator : 1.0e-8;
        float edge_x = (b.x - a.x) * (point.y - a.y)
            / safe_denominator + a.x;
        if (crosses && point.x < edge_x) inside = !inside;
    }
    return inside ? 1.0 : 0.0;
}

float procedural_mask() {
    float progress = clamp(u_progress, 0.0, 1.0);
    if (u_transition_kind == 4) return 1.0;
    if (progress <= 0.0) return 0.0;
    if (progress >= 1.0) return 1.0;

    vec2 size = vec2(textureSize(u_left_tex, 0));
    float aspect = size.x / max(size.y, 1.0);
    vec2 point = v_texcoord * 2.0 - 1.0;
    point.x *= aspect;
    if (u_transition_kind == 1 || u_transition_kind == 2) {
        // AUD-16900：point.x 已乘 aspect。旧常数半径 1.55 在宽画幅下
        // 永远盖不住左右边缘中点（|x|=aspect>1.55）与四角，这些区域
        // 一直显示 outgoing 画面，直到 progress=1.0 端点早退一次性翻
        // 转为全 1（硬跳变）。改为按画幅自适应：圆的半径上界取对角距
        // sqrt(aspect^2+1)，椭圆保持 0.62 的纵横比并对对角距做尺度补
        // 偿，保证 progress→1 时全帧被 iris 覆盖、无终点跳变。
        if (u_transition_kind == 2) {
            float ellipse_scale = progress
                * sqrt(aspect * aspect + 1.0 / (0.62 * 0.62));
            return dot(vec2(point.x / max(ellipse_scale, 1.0e-6),
                            point.y / max(ellipse_scale * 0.62, 1.0e-6)),
                       vec2(point.x / max(ellipse_scale, 1.0e-6),
                            point.y / max(ellipse_scale * 0.62, 1.0e-6))) <= 1.0
                ? 1.0 : 0.0;
        }
        float radius = progress * sqrt(aspect * aspect + 1.0);
        return dot(point, point) <= radius * radius ? 1.0 : 0.0;
    }
    if (u_transition_kind == 3) return polygon_inside(point);

    if (u_transition_kind == 5) {
        // LIB1/X01 时钟极角（L03）：与 transition_packs clock_wipe_mask 同构。
        // v_texcoord y 向上（quad uv 左下原点）：北（画面上方）= +y。
        float dx = (v_texcoord.x - u_center.x) * aspect;
        float dy = v_texcoord.y - u_center.y;
        float theta = atan(dx, dy);            // 北 0，顺时针正
        float delta = theta - radians(u_angle); // u_angle 复用为 start_angle（度）
        if (u_reverse != 0) delta = -delta;
        float rel = mod(delta, 2.0 * PI);
        float sweep = progress * 2.0 * PI;
        // u_softness 为归一角宽（×2π；与 kind=0 wipe 的长边比例口径不同）
        float feather = max(u_softness * 2.0 * PI, 1.0e-6);
        return clamp((sweep - rel) / feather, 0.0, 1.0);
    }

    if (u_transition_kind == 6) {
        // LIB1/X02 周期百叶（L13）：带内相位 t=mod(s·count+phase,1)，全带
        // 同时扫掠；CPU 口径 y 向下（v_texcoord y 向上 → y_down 翻转）。
        float y_down6 = 1.0 - v_texcoord.y;
        float a6 = radians(u_mask_p0.y);
        float s6 = v_texcoord.x * cos(a6)
            + (y_down6 / max(aspect, 1.0e-6)) * sin(a6);
        float t6 = mod(s6 * u_mask_p0.x + u_mask_p0.z, 1.0);
        float feather6 = max(u_mask_p0.w, 1.0e-6);
        return clamp((progress - t6) / feather6, 0.0, 1.0);
    }
    if (u_transition_kind == 7) {
        // LIB1/X03 棋盘奇偶两波（L13）：(r+c)%2 错相 0.5，相邻格恒错相。
        float y_down7 = 1.0 - v_texcoord.y;
        float c7 = clamp(floor(v_texcoord.x * u_mask_p0.y),
                         0.0, u_mask_p0.y - 1.0);
        float r7 = clamp(floor(y_down7 * u_mask_p0.x),
                         0.0, u_mask_p0.x - 1.0);
        float trig7 = mod(mod(r7 + c7, 2.0) * 0.5 + u_mask_p0.z, 1.0);
        float feather7 = max(u_mask_p0.w, 1.0e-6);
        return clamp((progress - trig7) / feather7, 0.0, 1.0);
    }
    if (u_transition_kind == 8) {
        // LIB1/X05 内容驱动亮度揭示（L13）：mask 采样源图 luma（首个
        // 非几何 mask 家族）；source=1 时用 u_right_tex（B 图排序）。
        // 直接采样 v_texcoord：纹理行 r = 数据首行起（行序与回读帧缓冲
        // 行一致，numpy 行序对拍无需翻转——几何家族的 y_down 只用于
        // 屏幕坐标，与纹理内容行序无关）。
        vec3 rgb8 = (u_mask_p0.w < 0.5)
            ? texture(u_left_tex, v_texcoord).rgb
            : texture(u_right_tex, v_texcoord).rgb;
        float luma8 = dot(rgb8, vec3(0.2126, 0.7152, 0.0722));
        luma8 = clamp(luma8 + (0.5 - u_mask_p0.x), 0.0, 1.0);
        if (u_mask_p0.z > 0.5) luma8 = 1.0 - luma8;
        float span8 = u_mask_p0.y;
        float thr8 = -span8 + progress * (1.0 + 2.0 * span8);
        return clamp((thr8 + 0.5 * span8 - luma8) / span8, 0.0, 1.0);
    }
    if (u_transition_kind == 9) {
        // LIB1/X06 uint32 格点散列 rank 场（L13）：mode=1 Cell 最近格点 /
        // 0 Value 双线性 value noise；与 CPU 核逐位同式。
        float scale9 = u_mask_p0.x;
        float seed9 = u_mask_p0.y;
        vec2 gp9 = vec2(v_texcoord.x, 1.0 - v_texcoord.y) * scale9;
        float rank9;
        if (u_mask_p0.w > 0.5) {
            ivec2 cell9 = ivec2(floor(gp9 + 0.5));
            rank9 = lib1_hash01(uint(cell9.x), uint(cell9.y), uint(seed9));
        } else {
            vec2 f9 = fract(gp9);
            vec2 fade9 = f9 * f9 * (3.0 - 2.0 * f9);
            ivec2 i9 = ivec2(floor(gp9));
            float n00 = lib1_hash01(uint(i9.x), uint(i9.y), uint(seed9));
            float n10 = lib1_hash01(uint(i9.x) + 1u, uint(i9.y), uint(seed9));
            float n01 = lib1_hash01(uint(i9.x), uint(i9.y) + 1u, uint(seed9));
            float n11 = lib1_hash01(uint(i9.x) + 1u, uint(i9.y) + 1u, uint(seed9));
            float a9 = n00 + (n10 - n00) * fade9.x;
            float b9 = n01 + (n11 - n01) * fade9.x;
            rank9 = a9 + (b9 - a9) * fade9.y;
        }
        float feather9 = max(u_mask_p0.z, 1.0e-6);
        return clamp((progress - rank9) / feather9, 0.0, 1.0);
    }
    if (u_transition_kind == 10) {
        // LIB1/X08 网点扩张（L13）：像素中心坐标旋转格点，Dot=径向距 /
        // Square=切比雪夫距；p=1 半径 √2 覆盖胞元角+最大羽化。
        vec2 size10 = vec2(textureSize(u_left_tex, 0));
        vec2 px10 = vec2(v_texcoord.x * size10.x,
                         (1.0 - v_texcoord.y) * size10.y);
        float a10 = radians(u_mask_p0.y);
        float s10 = px10.x * cos(a10) - px10.y * sin(a10);
        float t10 = px10.x * sin(a10) + px10.y * cos(a10);
        float sp10 = max(u_mask_p0.x, 4.0);
        float ds10 = s10 - (floor(s10 / sp10) + 0.5) * sp10;
        float dt10 = t10 - (floor(t10 / sp10) + 0.5) * sp10;
        float d10 = (u_mask_p0.w > 0.5)
            ? max(abs(ds10), abs(dt10)) / sp10
            : sqrt(ds10 * ds10 + dt10 * dt10) / sp10;
        float r10 = progress * 1.4142135623730951;
        float feather10 = 2.0 * max(u_mask_p0.z, 1.0e-6);
        return clamp((r10 + u_mask_p0.z - d10) / feather10, 0.0, 1.0);
    }
    if (u_transition_kind == 11) {
        // LIB1/X07 voronoi 晶胞推进（L14）：n=ceil(sqrt(count)) 网格 hash
        // 扰动种子，最近种子晶胞（宽高比修正距离），出生 t=rank·t_span
        // （rank=hash 计数名次，与 CPU stable argsort 等价）；mask=
        // clamp((p−t)·speed−d/D)。常量循环界 6 + break（GLSL 330 纪律）。
        float edge11 = max(u_mask_p0.z, 0.0);
        float n11f = ceil(sqrt(max(u_mask_p0.x, 1.0)));
        int n11 = int(n11f);
        int m11 = n11 * n11;
        float t_span11 = max(0.0, 1.0 - 2.0 * edge11);
        float speed11 = 1.0 / (edge11 + 1.0e-6);
        float diag11 = sqrt(aspect * aspect + 1.0);
        float y_down11 = 1.0 - v_texcoord.y;
        uint seed11 = uint(u_mask_p0.y);
        float best_d2 = 1.0e30;
        float best_t = 0.0;
        for (int jy = 0; jy < 6; ++jy) {
            if (jy >= n11) break;
            for (int jx = 0; jx < 6; ++jx) {
                if (jx >= n11) break;
                float h1 = lib1_hash01(uint(jx), uint(jy), seed11);
                float h2 = lib1_hash01(uint(jx), uint(jy), seed11 + 1001u);
                float h3 = lib1_hash01(uint(jx), uint(jy), seed11 + 2002u);
                float sx11 = (float(jx) + h1) / n11f;
                float sy11 = (float(jy) + h2) / n11f;
                float dx11 = (v_texcoord.x - sx11) * aspect;
                float dy11 = y_down11 - sy11;
                float d211 = dx11 * dx11 + dy11 * dy11;
                if (d211 < best_d2) {
                    best_d2 = d211;
                    float rank11 = 0.0;
                    for (int ky = 0; ky < 6; ++ky) {
                        if (ky >= n11) break;
                        for (int kx = 0; kx < 6; ++kx) {
                            if (kx >= n11) break;
                            float t_k = lib1_hash01(uint(kx), uint(ky),
                                                    seed11 + 2002u);
                            if (t_k < h3) rank11 += 1.0;
                        }
                    }
                    best_t = (rank11 / float(m11)) * t_span11;
                }
            }
        }
        float d_norm11 = sqrt(max(best_d2, 0.0)) / diag11;
        float reveal11 = (progress - best_t) * speed11 - d_norm11;
        return clamp(reveal11, 0.0, 1.0);
    }

    float coord;
    if (u_direction == 0) coord = v_texcoord.x;
    else if (u_direction == 1) coord = 1.0 - v_texcoord.x;
    else if (u_direction == 2) coord = v_texcoord.y;
    else if (u_direction == 3) coord = 1.0 - v_texcoord.y;
    else {
        float theta = radians(u_angle);
        vec2 direction = vec2(cos(theta), sin(theta));
        float projection = dot(point, direction);
        float span = 0.5 * (abs(direction.x) * aspect
                            + abs(direction.y));
        coord = (projection + span) / max(2.0 * span, 1.0e-6);
    }
    if (u_reverse != 0) progress = 1.0 - progress;
    float feather = max(1.0 / max(size.x, size.y), u_softness);
    float t = (progress + feather - coord) / max(2.0 * feather, 1.0e-6);
    t = clamp(t, 0.0, 1.0);
    return t * t * (3.0 - 2.0 * t);
}

vec3 to_linear(vec3 value) {
    return pow(max(value, vec3(0.0)), vec3(2.2));
}

vec3 from_linear(vec3 value) {
    return pow(max(value, vec3(0.0)), vec3(1.0 / 2.2));
}

void main() {
    float progress = clamp(u_progress, 0.0, 1.0);
    if (u_transition_kind == 4) {
        float shaped = progress * progress * (3.0 - 2.0 * progress);
        float pulse = sin(PI * progress);
        float radius = max(0.0, u_blur_ratio) * min(
            float(textureSize(u_left_tex, 0).x),
            float(textureSize(u_left_tex, 0).y))
            * pow(max(pulse, 0.0), 0.72);
        float blur_mix = pow(max(pulse, 0.0), 0.65);
        float defocus_scale = u_transition_mode == 1 ? 1.65
            : (u_transition_mode == 3 ? 1.25 : 1.0);
        vec3 left_raw = texture(u_left_tex, v_texcoord).rgb
            * u_left_opacity;
        vec3 right_raw = texture(u_right_tex, v_texcoord).rgb
            * u_right_opacity;
        vec2 texel = 1.0 / vec2(textureSize(u_left_tex, 0));
        vec3 left_blurred = blur9(u_left_tex, v_texcoord, texel,
                                  texel * radius);
        vec3 right_blurred = blur9(u_right_tex, v_texcoord, texel,
                                   texel * radius);
        vec3 left = mix(left_raw, left_blurred * u_left_opacity,
                        min(1.0, blur_mix * defocus_scale));
        vec3 right = mix(right_raw, right_blurred * u_right_opacity,
                         min(1.0, blur_mix * defocus_scale));
        if (u_transition_mode == 2) {
            f_color = vec4(from_linear(mix(
                to_linear(left), to_linear(right), shaped)), 1.0);
        } else {
            f_color = vec4(mix(left, right, shaped), 1.0);
        }
        return;
    }
    // LIB1/X09 双源平移（L03）：两源采样坐标都随 progress 连续移动
    // （slide 是 mask 揭示双源不动）。A 段 u+p<=1、B 段 u+p-1>0——
    // 恒在界内，无越界采样；端点由 progress clamp 保证纯 A/B。
    if (u_resample_mode == 2) {
        // LIB1 复合家族（L14b）：双源 UV 重采样，按 kind 分派。
        if (u_transition_kind == 12) {
            // X16 scanline_slices：条带 hash 方向 ±1 水平位移（UV 域），
            // A 按 (1−p) 份额位移、B 按 p 份额，线性混合；
            // win = Linear 三角窗 / Ease 平方（CPU x16 同构，y CPU 行序）。
            vec2 size = vec2(textureSize(u_left_tex, 0));
            float band = max(u_mask_p0.x, 2.0);
            float seed12 = u_mask_p0.z;
            float y_px = (1.0 - v_texcoord.y) * size.y;
            float band_k = floor(y_px / band);
            float h1 = lib1_hash01(uint(band_k), 53u, uint(seed12));
            float dir = h1 >= 0.5 ? 1.0 : -1.0;
            float w_lin = 1.0 - abs(2.0 * progress - 1.0);
            float win = u_mask_p0.w > 0.5 ? w_lin * w_lin : w_lin;
            float shift = dir * u_mask_p0.y * win;
            vec2 uv_a = vec2(v_texcoord.x + (1.0 - progress) * shift,
                             v_texcoord.y);
            vec2 uv_b = vec2(v_texcoord.x + progress * shift, v_texcoord.y);
            vec3 la = texture(u_left_tex, clamp(uv_a, vec2(0.0), vec2(1.0))).rgb;
            vec3 lb = texture(u_right_tex, clamp(uv_b, vec2(0.0), vec2(1.0))).rgb;
            f_color = vec4((1.0 - progress) * la * u_left_opacity
                           + progress * lb * u_right_opacity, 1.0);
            return;
        }
        if (u_transition_kind == 13) {
            // X14 displacement_bridge：噪声位移桥——A 采样 u+(1−p)d、
            // B 采样 u+p·d（d=scale·win·noise，三角窗端点 0）；Value=
            // 双线性 value noise / Cell=最近格点常量（lib1_hash01 同族）。
            // y 场用 y_down（CPU vv 行序）；位移在 v 轴取负（v 轴 y-up）。
            float freq13 = u_mask_p0.y;
            float seed13 = u_mask_p0.z;
            vec2 gp13 = vec2(v_texcoord.x, 1.0 - v_texcoord.y) * freq13;
            vec2 n13;
            if (u_mask_p0.w > 0.5) {
                ivec2 cell13 = ivec2(floor(gp13));
                n13 = vec2(lib1_hash01(uint(cell13.x), uint(cell13.y), uint(seed13)),
                           lib1_hash01(uint(cell13.x), uint(cell13.y), uint(seed13) + 1001u));
            } else {
                vec2 f13 = fract(gp13);
                vec2 fade13 = f13 * f13 * (3.0 - 2.0 * f13);
                ivec2 i13 = ivec2(floor(gp13));
                float a00 = lib1_hash01(uint(i13.x), uint(i13.y), uint(seed13));
                float a10 = lib1_hash01(uint(i13.x) + 1u, uint(i13.y), uint(seed13));
                float a01 = lib1_hash01(uint(i13.x), uint(i13.y) + 1u, uint(seed13));
                float a11 = lib1_hash01(uint(i13.x) + 1u, uint(i13.y) + 1u, uint(seed13));
                float b0 = a00 + (a10 - a00) * fade13.x;
                float b1 = a01 + (a11 - a01) * fade13.x;
                n13.x = b0 + (b1 - b0) * fade13.y;
                float c00 = lib1_hash01(uint(i13.x), uint(i13.y), uint(seed13) + 1001u);
                float c10 = lib1_hash01(uint(i13.x) + 1u, uint(i13.y), uint(seed13) + 1001u);
                float c01 = lib1_hash01(uint(i13.x), uint(i13.y) + 1u, uint(seed13) + 1001u);
                float c11 = lib1_hash01(uint(i13.x) + 1u, uint(i13.y) + 1u, uint(seed13) + 1001u);
                float d0 = c00 + (c10 - c00) * fade13.x;
                float d1 = c01 + (c11 - c01) * fade13.x;
                n13.y = d0 + (d1 - d0) * fade13.y;
            }
            n13 = n13 * 2.0 - 1.0;
            float win13 = 1.0 - abs(2.0 * progress - 1.0);
            vec2 disp13 = u_mask_p0.x * win13 * n13;
            vec2 uv_a13 = v_texcoord + (1.0 - progress) * vec2(disp13.x, -disp13.y);
            vec2 uv_b13 = v_texcoord + progress * vec2(disp13.x, -disp13.y);
            vec3 la13 = texture(u_left_tex, clamp(uv_a13, vec2(0.0), vec2(1.0))).rgb;
            vec3 lb13 = texture(u_right_tex, clamp(uv_b13, vec2(0.0), vec2(1.0))).rgb;
            f_color = vec4((1.0 - progress) * la13 * u_left_opacity
                           + progress * lb13 * u_right_opacity, 1.0);
            return;
        }
        if (u_transition_kind == 16) {
            // X18 burn_reveal：rank 场揭示 + 前沿带发热（orange→white 随
            // band 二次增益），out = mix(A,B,mask) + emission·heat；
            // rank 场与 X06 同族（×8 帧归一格点，Field=双线性/Edge=最近）。
            vec2 size16 = vec2(textureSize(u_left_tex, 0));
            float ramp16 = max(u_mask_p0.x, 1.0e-6);
            float seed16 = u_mask_p0.z;
            vec2 gp16 = vec2(v_texcoord.x, 1.0 - v_texcoord.y) * 8.0;
            float rank16;
            if (u_mask_p0.w > 0.5) {
                ivec2 cell16 = ivec2(floor(gp16 + 0.5));
                rank16 = lib1_hash01(uint(cell16.x), uint(cell16.y), uint(seed16));
            } else {
                vec2 f16 = fract(gp16);
                vec2 fade16 = f16 * f16 * (3.0 - 2.0 * f16);
                ivec2 i16 = ivec2(floor(gp16));
                float n00 = lib1_hash01(uint(i16.x), uint(i16.y), uint(seed16));
                float n10 = lib1_hash01(uint(i16.x) + 1u, uint(i16.y), uint(seed16));
                float n01 = lib1_hash01(uint(i16.x), uint(i16.y) + 1u, uint(seed16));
                float n11 = lib1_hash01(uint(i16.x) + 1u, uint(i16.y) + 1u, uint(seed16));
                float a0 = n00 + (n10 - n00) * fade16.x;
                float b0 = n01 + (n11 - n01) * fade16.x;
                rank16 = a0 + (b0 - a0) * fade16.y;
            }
            float mask16 = clamp((progress - rank16) / ramp16, 0.0, 1.0);
            float band16 = clamp(1.0 - abs(progress - rank16) / ramp16, 0.0, 1.0);
            vec3 la16 = texture(u_left_tex, v_texcoord).rgb;
            vec3 lb16 = texture(u_right_tex, v_texcoord).rgb;
            vec3 base16 = la16 * u_left_opacity * (1.0 - mask16)
                + lb16 * u_right_opacity * mask16;
            vec3 heat16 = band16 * (vec3(1.0, 0.3, 0.05)
                + (vec3(1.0) - vec3(1.0, 0.3, 0.05)) * band16);
            f_color = vec4(base16 + u_mask_p0.y * heat16, 1.0);
            return;
        }
        if (u_transition_kind == 21) {
            // X04 staggered_tiles：g×g 块错峰（Row 行序 rank=k/(m−1) /
            // Random hash 名次 rank=计数/m），pi=clamp((p−delay_i)/denom)；
            // A 窗向块中心收缩 s_a=1−scale_amt·pi、B 自宽窗 1/(1−scale_amt(1−pi))
            // 收缩到 1；scale_amt 走 u_mask_p1.x。
            float g21 = u_mask_p0.x;
            float seed21 = u_mask_p0.z;
            bool random21 = u_mask_p0.w > 0.5;
            int m21 = int(g21 * g21);
            vec2 uvf21 = vec2(v_texcoord.x, 1.0 - v_texcoord.y);
            float ix21 = clamp(floor(uvf21.x * g21), 0.0, g21 - 1.0);
            float iy21 = clamp(floor(uvf21.y * g21), 0.0, g21 - 1.0);
            float k21 = iy21 * g21 + ix21;
            float rank21;
            if (random21) {
                float hk21 = lib1_hash01(uint(k21), 41u, uint(seed21));
                float cnt21 = 0.0;
                for (int jy = 0; jy < 16; ++jy) {
                    if (float(jy) >= g21) break;
                    for (int jx = 0; jx < 16; ++jx) {
                        if (float(jx) >= g21) break;
                        float hj = lib1_hash01(uint(jy * int(g21) + jx), 41u,
                                               uint(seed21));
                        if (hj < hk21) cnt21 += 1.0;
                    }
                }
                rank21 = cnt21 / float(m21);
            } else {
                rank21 = k21 / max(g21 * g21 - 1.0, 1.0);
            }
            float max_rank21 = random21 ? (float(m21 - 1) / float(m21)) : 1.0;
            float denom21 = max(1.0 - u_mask_p0.y * max_rank21, 1.0e-6);
            float pi21 = clamp((progress - u_mask_p0.y * rank21) / denom21,
                               0.0, 1.0);
            float scale21 = u_mask_p1.x;
            float lx21 = uvf21.x * g21 - ix21;
            float ly21 = uvf21.y * g21 - iy21;
            float s_a21 = 1.0 - scale21 * pi21;
            float s_b21 = 1.0 / (1.0 - scale21 * (1.0 - pi21));
            vec2 uv_a21 = vec2((ix21 + 0.5 + (lx21 - 0.5) * s_a21) / g21,
                               (iy21 + 0.5 + (ly21 - 0.5) * s_a21) / g21);
            vec2 uv_b21 = vec2((ix21 + 0.5 + (lx21 - 0.5) * s_b21) / g21,
                               (iy21 + 0.5 + (ly21 - 0.5) * s_b21) / g21);
            vec3 la21 = texture(u_left_tex, clamp(uv_a21, vec2(0.0), vec2(1.0))).rgb;
            vec3 lb21 = texture(u_right_tex, clamp(uv_b21, vec2(0.0), vec2(1.0))).rgb;
            f_color = vec4((1.0 - pi21) * la21 * u_left_opacity
                           + pi21 * lb21 * u_right_opacity, 1.0);
            return;
        }
        if (u_transition_kind == 20) {
            // X11 cube_faces：共棱 x=p 线性扫，A 面 [p, p+cosθ]（视宽 cosθ）
            // / B 面 [p−sinθ, p]（视宽 sinθ），θ=p·90°；面内透视同 X10 式
            // （A 外边压缩 / B 镜像），shade_a=1−shadow(1−cosθ)、
            // shade_b=1−shadow(1−sinθ)；面外 B 原位... 无——面外区：
            // use_a = coord ≥ p（A 在棱右、B 在棱左+面外 B 原位由 sel 兜底）。
            float persp20 = u_mask_p0.x;
            float theta20 = progress * 1.5707963267948966;
            float cos_t20 = max(cos(theta20), 1.0e-6);
            float sin_t20 = max(sin(theta20), 1.0e-6);
            float shade_a20 = 1.0 - u_mask_p0.y * (1.0 - cos_t20);
            float shade_b20 = 1.0 - u_mask_p0.y * (1.0 - sin_t20);
            // Vertical 轴向坐标 = CPU 行序 cs（flipud 约定下 = 1−v.y）
            float coord20 = u_mask_p0.z > 0.5 ? 1.0 - v_texcoord.y
                                              : v_texcoord.x;
            vec2 uv20 = u_mask_p0.z > 0.5
                ? vec2(v_texcoord.x, v_texcoord.y)
                : vec2(v_texcoord.x, v_texcoord.y);
            float m_a20 = clamp((coord20 - progress) / cos_t20, 0.0, 1.0);
            float m_b20 = clamp((coord20 - progress + sin_t20) / sin_t20, 0.0, 1.0);
            float s_a20 = persp20 <= 0.0 ? m_a20
                : m_a20 * (1.0 + persp20 * m_a20) / (1.0 + persp20);
            float s_b20 = persp20 <= 0.0 ? m_b20
                : 1.0 - (1.0 - m_b20) * (1.0 + persp20 * (1.0 - m_b20)) / (1.0 + persp20);
            // 面内源坐标：src_a = persp_a(m_a)、src_b = persp_b(m_b)——
            // 整幅 A/B 图各自铺满自己的可见面（CPU _bilinear(a, src_a) 同构，
            // m 出面外被 clip 0/1 贴边）；无 face 外兜底区（cs<p 全走 B 面）。
            vec2 uv_a20, uv_b20;
            if (u_mask_p0.z > 0.5) {
                uv_a20 = vec2(v_texcoord.x, s_a20);
                uv_b20 = vec2(v_texcoord.x, s_b20);
            } else {
                uv_a20 = vec2(s_a20, v_texcoord.y);
                uv_b20 = vec2(s_b20, v_texcoord.y);
            }
            bool use_a20 = coord20 >= progress;
            vec3 face_a20 = texture(u_left_tex, clamp(uv_a20, vec2(0.0), vec2(1.0))).rgb
                * shade_a20 * u_left_opacity;
            vec3 face_b20 = texture(u_right_tex, clamp(uv_b20, vec2(0.0), vec2(1.0))).rgb
                * shade_b20 * u_right_opacity;
            f_color = vec4(use_a20 ? face_a20 : face_b20, 1.0);
            return;
        }
        if (u_transition_kind == 19) {
            // X10 hinged_doors：门宽 W=1−0.999p（Left 单门外铰链 /
            // Both 双门外铰链），门内 s=m(1+persp·m)/(1+persp)（m=门内
            // 归一坐标），shade=(1−shadow·p) 门均匀乘；门外 B 原位。
            float persp19 = u_mask_p0.x;
            float shade19 = 1.0 - u_mask_p0.y * progress;
            bool left_hinge19 = u_mask_p0.z > 0.5;
            float door_w19 = 1.0 - 0.999 * progress;
            float src19;
            bool on_door19;
            if (left_hinge19) {
                on_door19 = v_texcoord.x <= door_w19;
                float m19 = clamp(v_texcoord.x / max(door_w19, 1.0e-6), 0.0, 1.0);
                src19 = persp19 <= 0.0 ? m19
                    : m19 * (1.0 + persp19 * m19) / (1.0 + persp19);
            } else {
                float half19 = 0.5 * door_w19;
                bool on_left19 = v_texcoord.x <= half19;
                bool on_right19 = v_texcoord.x >= 1.0 - half19;
                on_door19 = on_left19 || on_right19;
                float ml19 = clamp(v_texcoord.x / max(half19, 1.0e-6), 0.0, 1.0);
                float mr19 = clamp((1.0 - v_texcoord.x) / max(half19, 1.0e-6), 0.0, 1.0);
                float s_l = persp19 <= 0.0 ? ml19
                    : ml19 * (1.0 + persp19 * ml19) / (1.0 + persp19);
                float s_r = persp19 <= 0.0 ? mr19
                    : mr19 * (1.0 + persp19 * mr19) / (1.0 + persp19);
                src19 = on_left19 ? s_l * 0.5 : 1.0 - s_r * 0.5;
            }
            vec3 a_door19 = texture(u_left_tex, vec2(src19, v_texcoord.y)).rgb
                * shade19 * u_left_opacity;
            vec3 b_behind19 = texture(u_right_tex, v_texcoord).rgb
                * u_right_opacity;
            f_color = vec4(on_door19 ? a_door19 : b_behind19, 1.0);
            return;
        }
        if (u_transition_kind == 18) {
            // X12 page_curl：规范坐标 t（Left=uv.x，Right=1−uv.x）；
            // x_c=1−p(1+r)；t<x_c−r 平铺 A、[x_c−r,x_c] 卷区（φ→弧长→
            // 源 x_c+s 镜像采样，mix(A,back,sinφ)，shade=1−shadow·sinφ）、
            // t>x_c 显 B。y 场无关（行向不变）。
            float radius18 = u_mask_p0.x;
            float shadow18 = u_mask_p0.y;
            bool right18 = u_mask_p0.z > 0.5;
            vec3 back18 = vec3(u_vertices[0].x, u_vertices[0].y, u_vertices[1].x);
            float t18 = right18 ? (1.0 - v_texcoord.x) : v_texcoord.x;
            float x_c18 = 1.0 - progress * (1.0 + radius18);
            vec3 flat18 = texture(u_left_tex, vec2(
                right18 ? (1.0 - t18) : t18, v_texcoord.y)).rgb;
            vec3 out18;
            if (t18 > x_c18) {
                out18 = texture(u_right_tex, vec2(
                    right18 ? (1.0 - t18) : t18, v_texcoord.y)).rgb
                    * u_right_opacity;
            } else if (t18 >= x_c18 - radius18) {
                float phi18 = clamp((x_c18 - t18) / radius18, 0.0, 1.0)
                    * (3.14159265358979 / 2.0);
                float arc_s18 = radius18 * (2.0 / 3.14159265358979) * phi18;
                float t_src18 = x_c18 + arc_s18;
                float src18 = right18 ? (1.0 - t_src18) : t_src18;
                vec3 curl18 = texture(u_left_tex, vec2(src18, v_texcoord.y)).rgb;
                float w_back18 = sin(phi18);
                curl18 = mix(curl18, back18, w_back18)
                    * (1.0 - shadow18 * w_back18);
                out18 = curl18;
            } else {
                out18 = flat18;
            }
            f_color = vec4(out18 * u_left_opacity, 1.0);
            return;
        }
        if (u_transition_kind == 17) {
            // X17 kaleido_bridge：θ 折叠 m=mod(θ−rot, 2π/N)、a=min(m, 2π/N−m)
            // →折叠点；s_eff=strength·(1−|2p−1|)；layer=(1−s_eff)X(u)+s_eff·X(u_f)；
            // w_b=clamp(2p−1,0,1)（B 仅后半程淡入）。y_down 场与 v 位移取负。
            vec2 size17 = vec2(textureSize(u_left_tex, 0));
            float asp17 = size17.x / max(size17.y, 1.0);
            float n17 = u_mask_p0.x;
            float rx17 = (v_texcoord.x - u_center.x) * asp17;
            float ry17 = (1.0 - v_texcoord.y) - u_center.y;
            float r17 = length(vec2(rx17, ry17));
            float theta17 = atan(ry17, rx17);
            float rot17 = radians(u_mask_p0.y);
            float sector17 = 2.0 * 3.14159265358979 / n17;
            float m17 = mod(theta17 - rot17, 2.0 * sector17);
            float a17 = min(m17, 2.0 * sector17 - m17);
            float theta_f17 = rot17 + a17;
            vec2 fold17 = vec2(u_center.x + r17 * cos(theta_f17) / asp17,
                               u_center.y + r17 * sin(theta_f17));
            // 场坐标 (uu, vv) 与 v_texcoord 同值域（都是 (i+0.5)/n 归一），
            // 折叠采样直接用 fold（y_down 语义由 flipud 对拍承载）
            vec2 uv_id17 = v_texcoord;
            vec2 uv_f17 = fold17;
            float win17 = 1.0 - abs(2.0 * progress - 1.0);
            float s_eff17 = u_mask_p0.z * win17;
            vec3 la17 = (1.0 - s_eff17) * texture(u_left_tex, uv_id17).rgb
                + s_eff17 * texture(u_left_tex, clamp(uv_f17, vec2(0.0), vec2(1.0))).rgb;
            vec3 lb17 = (1.0 - s_eff17) * texture(u_right_tex, uv_id17).rgb
                + s_eff17 * texture(u_right_tex, clamp(uv_f17, vec2(0.0), vec2(1.0))).rgb;
            float w_b17 = clamp(2.0 * progress - 1.0, 0.0, 1.0);
            f_color = vec4((1.0 - w_b17) * la17 * u_left_opacity
                           + w_b17 * lb17 * u_right_opacity, 1.0);
            return;
        }
        if (u_transition_kind == 14) {
            // X15 ripple_refraction：径向正弦折射（dr=amp·sin(freq·(r−p·r_max))
            // ·exp(−damping·r)，位移 dr·r̂ 三角窗；r=0 置 0 防除零）。
            float aspect14 = u_center.x >= 0.0 ? 1.0 : 1.0;  // aspect 由下方统一
            vec2 size14 = vec2(textureSize(u_left_tex, 0));
            float asp14 = size14.x / max(size14.y, 1.0);
            float cx14 = u_center.x, cy14 = u_center.y;
            float rx14 = (v_texcoord.x - cx14) * asp14;
            float ry14 = (1.0 - v_texcoord.y) - cy14;   // y_down 行序
            float r14 = length(vec2(rx14, ry14));
            float r_max14 = max(
                max(length(vec2((0.0 - cx14) * asp14, 0.0 - cy14)),
                    length(vec2((1.0 - cx14) * asp14, 0.0 - cy14))),
                max(length(vec2((0.0 - cx14) * asp14, 1.0 - cy14)),
                    length(vec2((1.0 - cx14) * asp14, 1.0 - cy14))));
            float dr14 = u_mask_p0.y
                * sin(u_mask_p0.x * (r14 - progress * r_max14))
                * exp(-u_mask_p0.z * r14);
            vec2 rhat = r14 > 1.0e-9 ? vec2(rx14, ry14) / r14 : vec2(0.0);
            vec2 disp14 = vec2(dr14 * rhat.x / asp14, dr14 * rhat.y);
            float win14 = 1.0 - abs(2.0 * progress - 1.0);
            vec2 uv_a14 = v_texcoord + (1.0 - progress) * win14
                * vec2(disp14.x, -disp14.y);
            vec2 uv_b14 = v_texcoord + progress * win14
                * vec2(disp14.x, -disp14.y);
            vec3 la14 = texture(u_left_tex, clamp(uv_a14, vec2(0.0), vec2(1.0))).rgb;
            vec3 lb14 = texture(u_right_tex, clamp(uv_b14, vec2(0.0), vec2(1.0))).rgb;
            f_color = vec4((1.0 - progress) * la14 * u_left_opacity
                           + progress * lb14 * u_right_opacity, 1.0);
            return;
        }
        if (u_transition_kind == 15) {
            // X13 pixelate_bridge：A 粗化→switch 换源→B 细化（块中心
            // texelFetch 精确 NEAREST，In/Out 剖面 cell_eff 分段）。
            vec2 size15 = vec2(textureSize(u_left_tex, 0));
            float cell15 = u_mask_p0.x;
            float switch15 = u_mask_p0.y;
            bool before15 = progress < switch15;
            float frac15 = before15
                ? progress / switch15
                : (progress - switch15) / (1.0 - switch15);
            bool in15 = u_mask_p0.z < 0.5;
            float cell_eff15 = before15
                ? (in15 ? cell15 * frac15 : cell15 * (1.0 - frac15))
                : (in15 ? cell15 * (1.0 - frac15) : cell15 * frac15);
            cell_eff15 = max(cell_eff15, 1.0);
            vec2 px15 = vec2(v_texcoord.x * size15.x,
                             (1.0 - v_texcoord.y) * size15.y);
            // CPU _block_center_index：center = floor((bid+0.5)·cell − 0.5)
            //（块中心像素索引，非 bid·c+c/2——初版差一 texel）
            vec2 bid15 = floor(px15 / cell_eff15);
            vec2 center15 = floor((bid15 + 0.5) * cell_eff15 - 0.5);
            ivec2 tc15 = ivec2(clamp(center15, vec2(0.0), size15 - 1.0));
            vec3 sampled15 = before15
                ? texelFetch(u_left_tex, tc15, 0).rgb
                : texelFetch(u_right_tex, tc15, 0).rgb;
            f_color = vec4(sampled15 * 1.0, 1.0);
            return;
        }
    }
    if (u_resample_mode == 1) {
        float off = clamp(u_progress, 0.0, 1.0);
        float coord;
        vec2 off_vec;
        if (u_push_dir == 0) { coord = v_texcoord.x; off_vec = vec2(off, 0.0); }
        else if (u_push_dir == 1) { coord = v_texcoord.x; off_vec = vec2(-off, 0.0); }
        else if (u_push_dir == 2) { coord = v_texcoord.y; off_vec = vec2(0.0, off); }
        else { coord = v_texcoord.y; off_vec = vec2(0.0, -off); }
        vec2 one = (u_push_dir == 0 || u_push_dir == 1)
            ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
        bool left_push = (u_push_dir == 0 || u_push_dir == 2);
        float seam = left_push ? (1.0 - off) : off;
        // u_softness 复用为归一接缝半宽（resample 语义；0=硬接缝）
        float soft = max(u_softness, 0.0);
        vec3 left_rgb = texture(u_left_tex, v_texcoord + off_vec).rgb;
        vec2 b_off = left_push ? off_vec - one : off_vec + one;
        vec3 right_rgb = texture(u_right_tex, v_texcoord + b_off).rgb;
        vec3 outgoing;
        if (soft <= 0.0) {
            bool use_left = left_push ? (coord + off <= 1.0)
                                      : (coord - off >= 0.0);
            outgoing = use_left ? left_rgb * u_left_opacity
                                : right_rgb * u_right_opacity;
        } else {
            // 接缝处 50/50；距接缝 ≥ soft 处回到硬选择（与 CPU 核同式）
            float b_sign = left_push ? 1.0 : -1.0;
            float w_b = clamp(
                0.5 + b_sign * (coord - seam) / max(2.0 * soft, 1.0e-6),
                0.0, 1.0);
            outgoing = left_rgb * u_left_opacity * (1.0 - w_b)
                + right_rgb * u_right_opacity * w_b;
        }
        f_color = vec4(outgoing, 1.0);
        return;
    }
    vec3 left = sample_effect(u_left_tex, v_texcoord)
        * u_left_opacity;
    vec3 right = sample_effect(u_right_tex, v_texcoord)
        * u_right_opacity;
    float mask = u_use_procedural != 0
        ? procedural_mask() : texture(u_mask_tex, v_texcoord).r;
    f_color = vec4(left * (1.0 - mask) + right * mask, 1.0);
}
"""


# Both preview and headless OpenGL export use the same unified program.  Keep
# the historical symbol so existing focused shader tests and imports remain
# stable while the implementation is now one full transition pipeline.
GPU_FULL_FRAME_TRANSITION_FRAGMENT_SHADER_SOURCE = (
    GPU_TRANSITION_FRAGMENT_SHADER_SOURCE
)


__all__ = [
    "GPU_TRANSITION_VERTEX_SHADER_SOURCE",
    "GPU_TRANSITION_FRAGMENT_SHADER_SOURCE",
    "GPU_FULL_FRAME_TRANSITION_FRAGMENT_SHADER_SOURCE",
]
