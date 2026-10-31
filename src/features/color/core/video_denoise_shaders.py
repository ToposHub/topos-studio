"""视频降噪 GLSL 着色器集合（时域 + 空域 + 运动估计）。

所有 shader 在渲染线程的 ModernGL 上下文中编译（#version 330 core）。
设计约束（docs/video_denoise_implementation_plan.md M1.4 / M6）：
- 避免逐采样 exp()：空间/值域权重使用有理近似 1/(1+k*x*x)
- Fast 模式单 pass 小半径；Better 模式可分离两 pass；Enhanced 多尺度
- 运动估计在 1/4 分辨率亮度图上做块匹配
- 时域累积使用 ping-pong 历史纹理 + 运动补偿 warp
- 亮度/色度在 YCbCr (BT.709 full-range) 域独立处理
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------

QUAD_VERTEX_SHADER = """
#version 330
in vec2 in_pos;
out vec2 v_uv;
void main() {
    v_uv = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

# 纯拷贝 pass（历史纹理初始化 / 直通）
COPY_FRAGMENT = """
#version 330
in vec2 v_uv;
out vec4 out_color;
uniform sampler2D u_texture;
void main() {
    out_color = texture(u_texture, v_uv);
}
"""

# 公共 GLSL 片段：YCbCr 转换与有理权重近似
_COMMON_GLSL = """
const vec3 LUMA_COEF = vec3(0.2126, 0.7152, 0.0722);

vec3 rgb_to_ycbcr(vec3 c) {
    float y = dot(c, LUMA_COEF);
    float cb = (c.b - y) / 1.8556 + 0.5;
    float cr = (c.r - y) / 1.5748 + 0.5;
    return vec3(y, cb, cr);
}

vec3 ycbcr_to_rgb(vec3 ycc) {
    float y = ycc.x;
    float b = y + (ycc.y - 0.5) * 1.8556;
    float r = y + (ycc.z - 0.5) * 1.5748;
    float g = (y - 0.2126 * r - 0.0722 * b) / 0.7152;
    return vec3(r, g, b);
}

// 有理近似权重，替代 exp(-x*k)：单调递减、无超越函数
float rat_w(float d2, float k) {
    return 1.0 / (1.0 + k * d2);
}
"""

# ---------------------------------------------------------------------------
# M1 空域降噪
# ---------------------------------------------------------------------------

# Fast：单 pass 小半径联合双边（亮度/色度独立权重），MAX_FAST_RADIUS=4
SPATIAL_FAST_FRAGMENT = """
#version 330
in vec2 v_uv;
out vec4 out_color;

uniform sampler2D u_texture;
uniform vec2 u_texel;          // 1.0 / resolution
uniform float u_luma_strength;   // 0-1
uniform float u_chroma_strength; // 0-1
uniform float u_edge_threshold;  // 0-1，越大越保边
uniform int u_radius;            // 1-4

const int MAX_FAST_RADIUS = 4;

""" + _COMMON_GLSL + """
void main() {
    vec3 center = texture(u_texture, v_uv).rgb;
    vec3 c_ycc = rgb_to_ycbcr(center);

    // 值域 sigma 随强度增大（邻居权重更高→更强降噪），
    // edge_threshold 越大 → k 越大 → 权重衰减越快 → 越保边
    float sigma_l = mix(0.015, 0.13, u_luma_strength);
    float sigma_c = mix(0.015, 0.15, u_chroma_strength);
    float edge_k = mix(0.6, 3.0, u_edge_threshold);
    float k_luma = edge_k / (sigma_l * sigma_l);
    float k_chroma = edge_k / (sigma_c * sigma_c);

    float w_sum_l = 0.0;
    float acc_l = 0.0;
    float w_sum_c = 0.0;
    vec2 acc_c = vec2(0.0);

    for (int dy = -MAX_FAST_RADIUS; dy <= MAX_FAST_RADIUS; dy++) {
        if (dy < -u_radius || dy > u_radius) { continue; }
        for (int dx = -MAX_FAST_RADIUS; dx <= MAX_FAST_RADIUS; dx++) {
            if (dx < -u_radius || dx > u_radius) { continue; }
            vec2 off = vec2(float(dx), float(dy)) * u_texel;
            vec3 s = texture(u_texture, v_uv + off).rgb;
            vec3 s_ycc = rgb_to_ycbcr(s);

            float d2 = float(dx * dx + dy * dy);
            float w_spatial = rat_w(d2, 0.35);

            float dy_l = (s_ycc.x - c_ycc.x) * (s_ycc.x - c_ycc.x);
            float w_l = w_spatial * rat_w(dy_l, k_luma);

            vec2 dc = s_ycc.yz - c_ycc.yz;
            float dc2 = dot(dc, dc);
            float w_c = w_spatial * rat_w(dc2, k_chroma);

            w_sum_l += w_l;
            acc_l += w_l * s_ycc.x;
            w_sum_c += w_c;
            acc_c += w_c * s_ycc.yz;
        }
    }

    float f_l = w_sum_l > 0.0001 ? acc_l / w_sum_l : c_ycc.x;
    vec2 f_c = w_sum_c > 0.0001 ? acc_c / w_sum_c : c_ycc.yz;

    vec3 filtered = vec3(mix(c_ycc.x, f_l, u_luma_strength),
                         mix(c_ycc.y, f_c.x, u_chroma_strength),
                         mix(c_ycc.z, f_c.y, u_chroma_strength));
    // Keep the internal float pipeline HDR-safe.  Display/output conversion is
    // responsible for clipping to the target transfer range, not denoising.
    vec3 result = ycbcr_to_rgb(filtered);
    out_color = vec4(result, texture(u_texture, v_uv).a);
}
"""

# Better：可分离双边（水平/垂直各一 pass），u_dir = (1,0) 或 (0,1)，半径最大 6
SPATIAL_SEPARABLE_FRAGMENT = """
#version 330
in vec2 v_uv;
out vec4 out_color;

uniform sampler2D u_texture;
uniform vec2 u_texel;
uniform vec2 u_dir;              // (1,0) 水平 pass / (0,1) 垂直 pass
uniform float u_luma_strength;
uniform float u_chroma_strength;
uniform float u_edge_threshold;
uniform int u_radius;            // 1-6
uniform int u_pass_index;        // 0=水平 1=垂直（色度只在垂直 pass 混合输出）

const int MAX_SEP_RADIUS = 6;

""" + _COMMON_GLSL + """
void main() {
    vec4 src = texture(u_texture, v_uv);
    vec3 center = src.rgb;
    vec3 c_ycc = rgb_to_ycbcr(center);

    float sigma_l = mix(0.015, 0.13, u_luma_strength);
    float sigma_c = mix(0.015, 0.15, u_chroma_strength);
    float edge_k = mix(0.6, 3.0, u_edge_threshold);
    float k_luma = edge_k / (sigma_l * sigma_l);
    float k_chroma = edge_k / (sigma_c * sigma_c);

    float w_sum_l = 0.0;
    float acc_l = 0.0;
    float w_sum_c = 0.0;
    vec2 acc_c = vec2(0.0);

    for (int i = -MAX_SEP_RADIUS; i <= MAX_SEP_RADIUS; i++) {
        if (i < -u_radius || i > u_radius) { continue; }
        vec2 off = u_dir * float(i) * u_texel;
        vec3 s = texture(u_texture, v_uv + off).rgb;
        vec3 s_ycc = rgb_to_ycbcr(s);

        float d2 = float(i * i);
        float w_spatial = rat_w(d2, 0.18);

        float dy_l = (s_ycc.x - c_ycc.x) * (s_ycc.x - c_ycc.x);
        float w_l = w_spatial * rat_w(dy_l, k_luma);

        vec2 dc = s_ycc.yz - c_ycc.yz;
        float dc2 = dot(dc, dc);
        float w_c = w_spatial * rat_w(dc2, k_chroma);

        w_sum_l += w_l;
        acc_l += w_l * s_ycc.x;
        w_sum_c += w_c;
        acc_c += w_c * s_ycc.yz;
    }

    float f_l = w_sum_l > 0.0001 ? acc_l / w_sum_l : c_ycc.x;
    vec2 f_c = w_sum_c > 0.0001 ? acc_c / w_sum_c : c_ycc.yz;

    // 亮度：两个 pass 都要过滤
    float out_l = mix(c_ycc.x, f_l, u_luma_strength);
    // 色度：只在第二 pass（垂直）过滤，避免两次衰减导致色度过度平滑
    float chroma_mix = u_pass_index == 1 ? u_chroma_strength : 0.0;
    vec2 out_c = mix(c_ycc.yz, f_c, chroma_mix);

    vec3 filtered = vec3(out_l, out_c);
    vec3 result = ycbcr_to_rgb(filtered);
    out_color = vec4(result, src.a);
}
"""

# Enhanced 多尺度 - 1/2 降采样（4 tap box）
SPATIAL_DOWNSAMPLE_FRAGMENT = """
#version 330
in vec2 v_uv;
out vec4 out_color;

uniform sampler2D u_texture;
uniform vec2 u_texel;   // 源纹理 texel

""" + _COMMON_GLSL + """
void main() {
    vec4 acc = vec4(0.0);
    acc += texture(u_texture, v_uv + u_texel * vec2(-0.5, -0.5));
    acc += texture(u_texture, v_uv + u_texel * vec2(0.5, -0.5));
    acc += texture(u_texture, v_uv + u_texel * vec2(-0.5, 0.5));
    acc += texture(u_texture, v_uv + u_texel * vec2(0.5, 0.5));
    out_color = acc * 0.25;
}
"""

# Enhanced 多尺度 - 上采样 + 细节残差恢复
SPATIAL_UPSAMPLE_DETAIL_FRAGMENT = """
#version 330
in vec2 v_uv;
out vec4 out_color;

uniform sampler2D u_original;   // 原图（全分辨率）
uniform sampler2D u_denoised;   // 半分辨率降噪结果
uniform sampler2D u_lowfreq;    // 半分辨率未降噪（用于计算残差）
uniform float u_detail_recovery;  // 0-1

""" + _COMMON_GLSL + """
void main() {
    vec4 orig = texture(u_original, v_uv);
    vec3 denoised = texture(u_denoised, v_uv).rgb;
    vec3 lowfreq = texture(u_lowfreq, v_uv).rgb;

    // 高频残差 = 原图 - 上采样后的低频
    vec3 residual = orig.rgb - lowfreq;

    // 局部 3x3 残差能量：小残差≈噪声（丢弃），大残差≈真实细节（保留）
    float mag = dot(abs(residual), vec3(0.3333));
    float low = mix(0.10, 0.035, u_detail_recovery);
    float high = mix(0.30, 0.12, u_detail_recovery);
    float keep = smoothstep(low, high, mag);  // recovery 越大越容易保留细节

    // 细节恢复只作用于亮度方向（残差在低能量处≈噪声）
    float y_orig = dot(orig.rgb, LUMA_COEF);
    float y_den = dot(denoised, LUMA_COEF);
    float y_low = dot(lowfreq, LUMA_COEF);
    float y_res = y_orig - y_low;
    float y_keep = smoothstep(low, high, abs(y_res));

    float y_out = y_den + y_res * y_keep;
    // RGB 整体平移保持色度：用亮度增量平移所有通道，色度不变
    vec3 result = denoised + vec3(y_out - y_den);
    result += residual * keep * 0.0;  // 色度残差不恢复（色度高频≈噪声）
    out_color = vec4(result, orig.a);
}
"""

# 细节恢复独立 pass（Fast/Better 模式复用）：按残差能量选择性回加亮度细节
DETAIL_RECOVERY_FRAGMENT = """
#version 330
in vec2 v_uv;
out vec4 out_color;

uniform sampler2D u_original;
uniform sampler2D u_denoised;
uniform float u_detail_recovery;  // 0-1

""" + _COMMON_GLSL + """
void main() {
    vec4 orig = texture(u_original, v_uv);
    vec3 denoised = texture(u_denoised, v_uv).rgb;

    float y_res = dot(orig.rgb, LUMA_COEF) - dot(denoised, LUMA_COEF);
    float low = mix(0.10, 0.035, u_detail_recovery);
    float high = mix(0.30, 0.12, u_detail_recovery);
    float keep = smoothstep(low, high, abs(y_res));

    float delta = y_res * keep;
    vec3 result = denoised + vec3(delta);
    out_color = vec4(result, orig.a);
}
"""

# ---------------------------------------------------------------------------
# M3 运动估计（1/4 分辨率亮度图块匹配）
# ---------------------------------------------------------------------------

# 亮度降采样到 1/4（16 tap box，输出 RGBA16F，R 通道为亮度）
LUMA_DOWNSAMPLE_QUARTER_FRAGMENT = """
#version 330
in vec2 v_uv;
out vec4 out_color;

uniform sampler2D u_texture;
uniform vec2 u_texel;   // 源纹理 texel

""" + _COMMON_GLSL + """
void main() {
    float acc = 0.0;
    for (int dy = -2; dy <= 1; dy++) {
        for (int dx = -2; dx <= 1; dx++) {
            vec3 c = texture(u_texture, v_uv + u_texel * vec2(float(dx) + 0.5, float(dy) + 0.5)).rgb;
            acc += dot(c, LUMA_COEF);
        }
    }
    float y = acc / 16.0;
    out_color = vec4(y, y, y, 1.0);
}
"""

# 块匹配：为每个 1/4 分辨率 texel 搜索最优匹配偏移
# 输出 RG = 运动矢量（UV 单位，current -> previous），B = 置信度 0-1
# MAX_SEARCH = 5（1/4 分辨率像素，相当于全分辨率 ±20px）
MOTION_BLOCK_MATCH_FRAGMENT = """
#version 330
in vec2 v_uv;
out vec4 out_color;

uniform sampler2D u_current_luma;   // 1/4 res
uniform sampler2D u_previous_luma;  // 1/4 res
uniform vec2 u_luma_texel;          // 1/4 res texel
uniform int u_search_radius;        // 1-5（1/4 res 像素）
uniform float u_match_scale;        // 噪声底标定：9 tap SAD 在纯噪声下的期望值

const int MAX_SEARCH = 5;

// G-P1: 5x5 支撑（= 20 全分辨率像素块）。3x3 支撑下 SAD 曲面在相邻
// 整数位间摇摆，运动场逐像素分裂（一半选 0 一半选 ±1）且置信度虚高，
// 是缓慢平移涂抹拖影的主因之一。
float block_sad(vec2 center_uv, vec2 off) {
    float sad = 0.0;
    for (int by = -2; by <= 2; by++) {
        for (int bx = -2; bx <= 2; bx++) {
            vec2 p = center_uv + u_luma_texel * vec2(float(bx), float(by));
            float c = texture(u_current_luma, p).r;
            float q = texture(u_previous_luma, p + off).r;
            sad += abs(c - q);
        }
    }
    return sad;
}

void main() {
    float best_sad = 1e9;
    vec2 best_off = vec2(0.0);

    for (int oy = -MAX_SEARCH; oy <= MAX_SEARCH; oy++) {
        if (oy < -u_search_radius || oy > u_search_radius) { continue; }
        for (int ox = -MAX_SEARCH; ox <= MAX_SEARCH; ox++) {
            if (ox < -u_search_radius || ox > u_search_radius) { continue; }
            float sad = block_sad(v_uv, u_luma_texel * vec2(float(ox), float(oy)));
            if (sad < best_sad) {
                best_sad = sad;
                best_off = vec2(float(ox), float(oy));
            }
        }
    }

    // G-P1 亚像素细化（抛物线插值）。整数搜索的量化步长 = 4 全分辨率像素：
    // 缓慢平移（1/4 res 上 <1px/帧）会被量化到 0 或 ±1 → 系统性错位
    // 2-4px，置信度却因 SAD 小而偏高 → 半错位历史被高强度融合 →
    // 纹理涂抹/拖影。细化后精度 ~1/4 res 0.25px（全分辨率 1px）。
    float sad_px = block_sad(v_uv, u_luma_texel * (best_off + vec2(1.0, 0.0)));
    float sad_mx = block_sad(v_uv, u_luma_texel * (best_off + vec2(-1.0, 0.0)));
    float sad_py = block_sad(v_uv, u_luma_texel * (best_off + vec2(0.0, 1.0)));
    float sad_my = block_sad(v_uv, u_luma_texel * (best_off + vec2(0.0, -1.0)));
    vec2 refine = vec2(0.0);
    float den_x = sad_mx - 2.0 * best_sad + sad_px;
    float den_y = sad_my - 2.0 * best_sad + sad_py;
    if (abs(den_x) > 1e-6) {
        refine.x = clamp((sad_mx - sad_px) / (2.0 * den_x), -0.5, 0.5);
    }
    if (abs(den_y) > 1e-6) {
        refine.y = clamp((sad_my - sad_py) / (2.0 * den_y), -0.5, 0.5);
    }
    vec2 refined_off = best_off + refine;

    // 置信度：SAD 接近噪声底（norm<=0.55）→ 高置信；
    // SAD 远超噪声底（norm>=1.25，遮挡/运动出搜索窗/场景切换）→ 0
    float norm = best_sad / max(u_match_scale, 0.0001);
    float confidence = clamp((1.25 - norm) / 0.7, 0.0, 1.0);
    confidence *= confidence;

    // 运动矢量转 UV 单位（指向上一帧对应位置）
    vec2 motion_uv = refined_off * u_luma_texel;
    out_color = vec4(motion_uv, confidence, 1.0);
}
"""

# G-P1 运动场平滑：3x3 均值矢量 + 最小置信度。逐像素块匹配的矢量场在
# 缓慢平移时分裂（相邻像素在两个整数位置间跳变），平滑把分裂收敛到
# 一致矢量；置信度取 9 tap 最小值，分裂（=不确定）区域自动降权。
MOTION_SMOOTH_FRAGMENT = """
#version 330
in vec2 v_uv;
out vec4 out_color;

uniform sampler2D u_motion;
uniform vec2 u_texel;

void main() {
    vec2 mv_sum = vec2(0.0);
    float conf_min = 1.0;
    for (int oy = -1; oy <= 1; oy++) {
        for (int ox = -1; ox <= 1; ox++) {
            vec4 m = texture(u_motion, v_uv + u_texel * vec2(float(ox), float(oy)));
            mv_sum += m.rg;
            conf_min = min(conf_min, m.b);
        }
    }
    out_color = vec4(mv_sum / 9.0, conf_min, 1.0);
}
"""

# ---------------------------------------------------------------------------
# M4 时域累积（ping-pong 历史 + 运动补偿 warp）
# ---------------------------------------------------------------------------

TEMPORAL_ACCUM_FRAGMENT = """
#version 330
in vec2 v_uv;
out vec4 out_color;

uniform sampler2D u_current;    // 全分辨率当前帧
uniform sampler2D u_history;    // 全分辨率历史（上一帧输出）
uniform sampler2D u_motion;     // 1/4 res 运动场 RG=motion_uv B=confidence

uniform float u_luma_strength;     // 0-1
uniform float u_chroma_strength;   // 0-1
uniform float u_threshold;         // 0-1 颜色相似度阈值
uniform float u_blend;             // 0-1 历史混合上限
uniform float u_scene_cut_threshold;  // 亮度差超过此值判定为切换/遮挡
uniform float u_history_frames;    // 已累积帧数（首帧=1，用于权重爬坡）
uniform float u_noise_sigma;       // 亮度噪声σ估计（噪声自适应相似度）

""" + _COMMON_GLSL + """
void main() {
    vec4 cur4 = texture(u_current, v_uv);
    vec3 cur = cur4.rgb;

    vec4 m = texture(u_motion, v_uv);
    vec2 motion_uv = m.rg;
    float confidence = m.b;

    // motion_match stores previous_coord - current_coord.  Therefore the
    // previous-frame sample is current_uv + motion, not current_uv - motion.
    vec2 hist_uv = clamp(v_uv + motion_uv, vec2(0.0), vec2(1.0));
    vec3 hist = texture(u_history, hist_uv).rgb;

    vec3 cur_ycc = rgb_to_ycbcr(cur);
    vec3 hist_ycc = rgb_to_ycbcr(hist);

    float d_y = abs(cur_ycc.x - hist_ycc.x);
    vec2 d_c = abs(cur_ycc.yz - hist_ycc.yz);

    // 场景切换 / 遮挡：亮度或色度差过大直接放弃历史。仅检查亮度会
    // 让“同亮度换色”的硬切仍然污染递归历史。
    if (max(d_y, max(d_c.x, d_c.y)) > u_scene_cut_threshold) {
        out_color = cur4;
        return;
    }

    // G-P1 噪声自适应相似度：半衰减点在 m_tol·σ（阈值越大越宽容）。
    // 匹配正确时 warp 残差 ≈ √2·σ；残差显著超出噪声水平的区域
    // （错位/低对比度纹理错融合）权重快速衰减，杜绝系统性涂抹。
    float m_tol = mix(2.0, 5.0, u_threshold);
    float sigma = max(u_noise_sigma, 0.008);
    float k_sim = 1.0 / (m_tol * sigma * m_tol * sigma);
    float sim_l = rat_w(d_y * d_y, k_sim);
    float sim_c = rat_w(dot(d_c, d_c), k_sim * 0.25);

    // 历史权重爬坡：前几帧逐步累积，避免首帧后立刻全权重
    float ramp = clamp(u_history_frames / 3.0, 0.0, 1.0);

    float w_l = u_luma_strength * u_blend * confidence * sim_l * ramp;
    float w_c = u_chroma_strength * u_blend * confidence * sim_c * ramp;
    w_l = clamp(w_l, 0.0, 0.85);   // 保留当前帧占比，限制递归拖影
    w_c = clamp(w_c, 0.0, 0.90);

    float out_y = mix(cur_ycc.x, hist_ycc.x, w_l);

    vec2 out_c = mix(cur_ycc.yz, hist_ycc.yz, w_c);

    vec3 result = ycbcr_to_rgb(vec3(out_y, out_c));
    out_color = vec4(result, cur4.a);
}
"""

# 邻帧 warp 混合（多帧窗口模式：当前帧与指定邻帧直接比较混合，无递归历史）
TEMPORAL_NEIGHBOR_BLEND_FRAGMENT = """
#version 330
in vec2 v_uv;
out vec4 out_color;

uniform sampler2D u_current;
uniform sampler2D u_neighbor;   // 已 warp 到当前帧坐标的邻帧
uniform float u_weight;         // 该邻帧的基础权重 0-1

""" + _COMMON_GLSL + """
void main() {
    vec4 cur4 = texture(u_current, v_uv);
    vec3 nb = texture(u_neighbor, v_uv).rgb;

    vec3 cur_ycc = rgb_to_ycbcr(cur4.rgb);
    vec3 nb_ycc = rgb_to_ycbcr(nb);

    float d_y = abs(cur_ycc.x - nb_ycc.x);
    vec2 d_c = abs(cur_ycc.yz - nb_ycc.yz);

    float sim_l = rat_w(d_y * d_y, 60.0);
    float sim_c = rat_w(dot(d_c, d_c), 30.0);

    float w_l = clamp(u_weight * sim_l, 0.0, 0.5);
    float w_c = clamp(u_weight * sim_c, 0.0, 0.5);

    float out_y = mix(cur_ycc.x, nb_ycc.x, w_l);
    vec2 out_c = mix(cur_ycc.yz, nb_ycc.yz, w_c);

    out_color = vec4(ycbcr_to_rgb(vec3(out_y, out_c)), cur4.a);
}
"""

# 调试叠加：运动场可视化（video_denoise_debug_overlay）
# 红=低置信度（运动/遮挡，历史被拒），青=高置信度（静止，正在累积历史），
# 黄色增量随运动矢量模长增大（快速运动区域更亮）。
DEBUG_OVERLAY_FRAGMENT = """
#version 330
in vec2 v_uv;
out vec4 out_color;

uniform sampler2D u_texture;   // 降噪后帧
uniform sampler2D u_motion;    // 1/4 res 运动场 RG=motion_uv B=confidence

void main() {
    vec4 frame = texture(u_texture, v_uv);
    vec4 m = texture(u_motion, v_uv);
    float confidence = m.b;
    float speed = length(m.rg);

    vec3 tint = mix(vec3(1.0, 0.25, 0.15), vec3(0.15, 1.0, 0.85), confidence);
    vec3 result = mix(frame.rgb, tint, 0.35);
    result += vec3(0.35, 0.35, 0.0)
        * clamp(speed * 40.0, 0.0, 0.5) * (1.0 - confidence);
    out_color = vec4(result, frame.a);
}
"""
