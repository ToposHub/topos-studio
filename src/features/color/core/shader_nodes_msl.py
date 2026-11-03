"""shader_nodes.py 各 SNIPPET 的 MSL 翻译（计划 P7-03，ADR 0004 决策 3）。

翻译契约（ADR 0004 Consequences）：
- ``shader_nodes.py`` 的 GLSL SNIPPET 是「GLSL 权威文本」，本模块的
  MSL 版本是其**翻译**而非并行演化——任何语义变更必须两侧同步 +
  G2 复验（per-effect 对拍门禁）。
- 命名规则：``msl_<glsl 函数名>``，_GLSL 形参/全局 uniform 改为显式
  传参（``constant CompositionParams&`` + 纹理 + sampler）——MSL 无
  全局 uniform 语义，struct 布局由 ``composition_uniforms.py`` 的
  manifest 承载（P7-02）。
- 与 P6-06 的边界：film grain / soft clip compute 内核（kernels_color.py）
  有位级哈希链契约（禁内建随机原语、contract(off)）；grading 等调色
  翻译走 **G2 容差门禁**（SDR ≤1/255，HDR ≤2e-3 rel），允许使用
  dot/mix/smoothstep 等内建——逐效果按台账登记的门禁等级执行。
- 翻译保持 GLSL 的语句顺序与分支形状（含死代码语义，如 P6-06 先例）；
  GLSL 的 ``for(i=0..3) color[i]=x`` 分量循环展开为逐分量 helper。

状态（台账 0004a 状态机）：composition 11 SNIPPET 全部收口——grading /
curves / hsl_curve / lut3d / mixer / warper / blur_sharpen / blend_modes /
soft_clip / film_grain = **msl_verified(fragment)**（G2 实测通过：SDR
用例 ≤1/255 + HDR 用例 ≤2e-3 rel；blur/sharpen 抖动路径按台账登记的噪
声门、film grain 走 P6-06 参考门 ≤2 fp16 ulp；MASK_SNIPPET 死代码删
除、qualifier 归属 matte 生成侧；真机 AMD 6800 Metal fragment vs GL
同负载逐像素；G3 正式基线待 A′ 主机器）。
"""

from __future__ import annotations

# ── COMMON_DEFINITIONS 的 MSL 翻译（rgb2hsv / hsv2rgb）───────────────────

MSL_HSV_FUNCS = """
// —— COMMON_DEFINITIONS.rgb2hsv 逐条翻译
float3 msl_rgb2hsv(float3 c) {
    float4 K = float4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    float4 p = mix(float4(c.bg, K.wz), float4(c.gb, K.xy), step(c.b, c.g));
    float4 q = mix(float4(p.xyw, c.r), float4(c.r, p.yzx), step(p.x, c.r));

    float d = q.x - min(q.w, q.y);
    float e = 1.0e-10;
    return float3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}

// —— COMMON_DEFINITIONS.hsv2rgb 逐条翻译
float3 msl_hsv2rgb(float3 c) {
    float4 K = float4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
    float3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}
"""


# ── GRADING_SNIPPET 的 MSL 翻译 ──────────────────────────────────────────

# GLSL `for(i=0..3)` 分量循环的逐分量展开（Gamma 三分支 / 暗部保护）
MSL_GRADING_HELPERS = """
// GLSL gamma 循环分支的逐分量展开（SDR 曲线 + HDR/负值线性外推）
float _grading_gamma(float x, float g) {
    if (x > 0.0 && x <= 1.0) return pow(x, g);
    if (x > 1.0) return g * (x - 1.0) + 1.0;
    return g * x;
}

// GLSL 暗部负值软性压缩的逐分量展开（保留 -0.5 范围）
float _grading_shadow_guard(float x) {
    if (x < -0.5) return -0.5 - (abs(x + 0.5) * 0.1);
    return x;
}
"""

MSL_GRADING = """
// —— GRADING_SNIPPET.apply_grading 逐条翻译。
// GLSL 全局 uniform（u_lift 等）与 u_tex_size/u_input_tex/v_texcoord
// 改为显式传参；u_matte/blend 语义属于 composition main（后续迁移位）。
float3 msl_apply_grading(float3 color, float2 v_texcoord,
                         constant CompositionParams& p,
                         texture2d<float> u_input_tex, sampler smp) {
    float3 lift = float3(p.u_lift_x, p.u_lift_y, p.u_lift_z);
    float3 gamma = float3(p.u_gamma_x, p.u_gamma_y, p.u_gamma_z);
    float3 gain = float3(p.u_gain_x, p.u_gain_y, p.u_gain_z);
    float exposure = p.u_exposure;
    float temp = p.u_temp;
    float tint = p.u_tint;
    float contrast = p.u_contrast;
    float saturation = p.u_saturation;
    float vibrance = p.u_vibrance;
    float shadow = p.u_shadow;
    float highlights = p.u_highlights;
    float mid_detail = p.u_mid_detail;
    float sharpness = p.u_sharpness;
    float hue_shift = p.u_hue_shift;
    // 色调区间饱和度（与 GLSL apply_grading 同款数学，与移动端
    // mega.frag tonal_saturation_adjust_g 对齐）。
    float lift_saturation = p.u_lift_saturation;
    float gamma_saturation = p.u_gamma_saturation;
    float gain_saturation = p.u_gain_saturation;
    float2 u_tex_size = float2(p.u_tex_size_x, p.u_tex_size_y);

    // 1. Exposure - Reversible linear multiplier
    float exp_stops = (exposure / 100.0) * (exposure / 100.0) * 6.0;
    if (exposure < 0.0) exp_stops = -exp_stops;
    color *= pow(2.0, exp_stops);

    // 2. White Balance (Simple Gain)
    float3 wb = float3(1.0);
    wb.r = 1.0 + temp / 200.0;
    wb.b = 1.0 - temp / 200.0;
    wb.g = 1.0 - tint / 200.0;
    color *= wb;

    // 3. Contrast - Pivot around 0.18 (Physical Mid-Grey)
    if (contrast != 0.0) {
        float c_factor = 1.0 + contrast / 100.0;
        color = sign(color) * pow(abs(color) / 0.18, float3(c_factor)) * 0.18;
    }

    // 4. Lift / Gamma / Gain
    float3 l = (lift - 1.0) * 0.2;
    color = color * gain * (1.0 - l) + l;

    // Gamma (HDR & Negative Safe)
    float3 safe_gamma = max(gamma, float3(0.01));
    float3 gm = float3(pow(2.0, -log2(safe_gamma.r)),
                       pow(2.0, -log2(safe_gamma.g)),
                       pow(2.0, -log2(safe_gamma.b)));
    color = float3(_grading_gamma(color.r, gm.r),
                   _grading_gamma(color.g, gm.g),
                   _grading_gamma(color.b, gm.b));

    // === 暗部保护（Lift/Gamma 暗部溢出）===
    float luma_post_grading = dot(color, float3(0.299, 0.587, 0.114));
    if (luma_post_grading < 0.0) {
        color.r = _grading_shadow_guard(color.r);
        color.g = _grading_shadow_guard(color.g);
        color.b = _grading_shadow_guard(color.b);
        float min_channel = min(min(color.r, color.g), color.b);
        float max_channel = max(max(color.r, color.g), color.b);
        if (max_channel - min_channel > 0.15 && min_channel < 0.0) {
            float balance_factor = clamp(abs(min_channel) * 0.25, 0.0, 0.15);
            color = mix(color, float3(luma_post_grading), balance_factor);
        }
    }

    // 5. Shadows - Power Curve (Natural Lifting)
    if (shadow != 0.0) {
        float shadow_factor = 1.0 + (shadow / 100.0);
        if (shadow_factor > 0.0) {
            float3 clamped_c = clamp(color, 0.0, 1.0);
            color = pow(clamped_c, float3(1.0 / shadow_factor)) + (color - clamped_c);
        }
    }

    // 6. Highlights - Reinhard Tone Mapping (Soft Compression)
    if (highlights != 0.0) {
        float max_c = max(max(color.r, color.g), color.b);
        if (max_c > 0.1) {
            float mapped_max = max_c;
            if (highlights < 0.0) {
                float k = abs(highlights / 100.0) * 2.0;
                mapped_max = 0.1 + (max_c - 0.1) / (1.0 + k * (max_c - 0.1));
            } else {
                float k = (highlights / 100.0) * 0.5;
                mapped_max = 0.1 + (max_c - 0.1) * (1.0 + k);
            }
            color *= (mapped_max / max_c);
        }
    }

    // 6.5 色调区间饱和度（Lift/Gamma/Gain 区间各自的饱和度偏置）。
    // 插在 highlights 与 vibrance 之间，与 GLSL/移动端渲染顺序一致。
    if (lift_saturation != 0.0 || gamma_saturation != 0.0 || gain_saturation != 0.0) {
        float tonal_lum = clamp(dot(color, float3(0.299, 0.587, 0.114)), 0.0, 1.0);
        float tonal_shadow_w = 1.0 - smoothstep(0.0, 0.5, tonal_lum);
        float tonal_mid_w = smoothstep(0.0, 0.5, tonal_lum) *
                            (1.0 - smoothstep(0.5, 1.0, tonal_lum));
        float tonal_high_w = smoothstep(0.5, 1.0, tonal_lum);
        float tonal_amount = lift_saturation * tonal_shadow_w +
                             gamma_saturation * tonal_mid_w +
                             gain_saturation * tonal_high_w;
        if (tonal_amount != 0.0) {
            float tonal_gray = dot(color, float3(0.299, 0.587, 0.114));
            color = mix(float3(tonal_gray), color, 1.0 + tonal_amount / 100.0);
        }
    }

    // 7. Vibrance
    if (vibrance != 0.0) {
        float gray = dot(color, float3(0.299, 0.587, 0.114));
        float max_c = max(color.r, max(color.g, color.b));
        float sat = max_c - gray;
        float amt = vibrance / 100.0;
        float boost = amt * (1.0 - clamp(sat, 0.0, 1.0));
        color = mix(color, float3(gray), -boost);
    }

    // 8. Midtone Detail & Sharpness（邻域采样：u_input_tex ± texel 偏移）
    if (mid_detail != 0.0 || sharpness != 0.0) {
        float scale_factor = u_tex_size.x / 2048.0;
        float2 tex_offset_mid = 3.0 * scale_factor / u_tex_size;
        float2 tex_offset_sharp = 1.0 * scale_factor / u_tex_size;

        float3 avg_mid = float3(0.0);
        avg_mid += u_input_tex.sample(smp, v_texcoord + float2(tex_offset_mid.x, 0.0)).rgb;
        avg_mid += u_input_tex.sample(smp, v_texcoord + float2(-tex_offset_mid.x, 0.0)).rgb;
        avg_mid += u_input_tex.sample(smp, v_texcoord + float2(0.0, tex_offset_mid.y)).rgb;
        avg_mid += u_input_tex.sample(smp, v_texcoord + float2(0.0, -tex_offset_mid.y)).rgb;
        avg_mid *= 0.25;

        float3 avg_sharp = float3(0.0);
        avg_sharp += u_input_tex.sample(smp, v_texcoord + float2(tex_offset_sharp.x, 0.0)).rgb;
        avg_sharp += u_input_tex.sample(smp, v_texcoord + float2(-tex_offset_sharp.x, 0.0)).rgb;
        avg_sharp += u_input_tex.sample(smp, v_texcoord + float2(0.0, tex_offset_sharp.y)).rgb;
        avg_sharp += u_input_tex.sample(smp, v_texcoord + float2(0.0, -tex_offset_sharp.y)).rgb;
        avg_sharp *= 0.25;

        float lum = dot(color, float3(0.299, 0.587, 0.114));

        if (mid_detail != 0.0) {
            float mid_mask = smoothstep(0.0, 0.5, lum) * (1.0 - smoothstep(0.5, 1.0, lum)) * 4.0;
            color += (color - avg_mid) * (mid_detail / 100.0) * mid_mask;
        }
        if (sharpness != 0.0) {
            color += (color - avg_sharp) * (sharpness / 100.0);
        }
    }

    // 9. Hue Shift（AUD-0840：负值 HDR 颜色按 MIXER 同防护——abs 进 HSV
    // 旋转后按符号还原；非负输入 abs 恒等，与 GLSL 权威逐条一致）
    if (hue_shift != 0.0) {
        float3 sign_c = sign(color);
        float3 hsv = msl_rgb2hsv(abs(color));
        hsv.x = fract(hsv.x + hue_shift / 360.0);
        color = sign_c * msl_hsv2rgb(hsv);
    }

    // 10. Saturation
    if (saturation != 0.0) {
        float gray = dot(color, float3(0.299, 0.587, 0.114));
        color = mix(float3(gray), color, 1.0 + saturation / 100.0);
    }

    return color;
}
"""


# ── CURVES_SNIPPET 的 MSL 翻译 ───────────────────────────────────────────

# GLSL `for(i=0..3)` 分量循环的逐分量展开。注意采样坐标随各通道的 x
# 而异（x = color[i]），且结果取该 texel 的**第 i 分量**（GLSL
# `texture(...)[i]`）——helper 以 comp 下标选择分量（MSL 向量支持运行
# 期下标）；外推分支的边界样本按通道以标量传入（斜率与 GLSL 先算
# vec3 再取分量逐通道等价）。
MSL_CURVES_HELPERS = """
// apply_curves 主循环（线性外推 + 区间采样）的逐分量展开
float _curves_channel(float x, int comp, float v0, float v1, float v254, float v255,
                      texture2d<float> curves_tex, sampler smp) {
    float slope_low = (v1 - v0) * 255.0;
    float slope_high = (v255 - v254) * 255.0;
    if (x < 0.0) return v0 + x * slope_low;
    if (x > 1.0) return v255 + (x - 1.0) * slope_high;
    return curves_tex.sample(smp, float2((x * 255.0 + 0.5) / 256.0, 0.5))[comp];
}

// 暗部/高光软性保护的逐分量展开（保留 -0.5 / 2.0 范围，超出按 0.1 衰减）
float _curves_guard(float x) {
    if (x < -0.5) x = -0.5 - (abs(x + 0.5) * 0.1);
    if (x > 2.0) x = 2.0 + (x - 2.0) * 0.1;
    return x;
}
"""

MSL_CURVES = """
// —— CURVES_SNIPPET.apply_curves 逐条翻译
float3 msl_apply_curves(float3 color, texture2d<float> curves_tex, sampler smp) {
    // Sample points near boundaries to calculate slopes
    float3 v0 = curves_tex.sample(smp, float2(0.5 / 256.0, 0.5)).rgb;
    float3 v1 = curves_tex.sample(smp, float2(1.5 / 256.0, 0.5)).rgb;
    float3 v254 = curves_tex.sample(smp, float2(254.5 / 256.0, 0.5)).rgb;
    float3 v255 = curves_tex.sample(smp, float2(255.5 / 256.0, 0.5)).rgb;

    float3 res;
    res.r = _curves_channel(color.r, 0, v0.r, v1.r, v254.r, v255.r, curves_tex, smp);
    res.g = _curves_channel(color.g, 1, v0.g, v1.g, v254.g, v255.g, curves_tex, smp);
    res.b = _curves_channel(color.b, 2, v0.b, v1.b, v254.b, v255.b, curves_tex, smp);

    // === 安全修复：防止暗部颜色溢出 ===
    res.r = _curves_guard(res.r);
    res.g = _curves_guard(res.g);
    res.b = _curves_guard(res.b);

    // 2. RGB 通道平衡检查：防止三通道失衡导致色偏
    float min_channel = min(min(res.r, res.g), res.b);
    float max_channel = max(max(res.r, res.g), res.b);
    if (max_channel - min_channel > 0.15 && min_channel < 0.0) {
        float3 diff = res - float3(min_channel);
        float balance_factor = clamp(abs(min_channel) * 0.3, 0.0, 0.2);
        res = res - diff * balance_factor;
    }

    return res;
}

// HSL Curves: 6 professional curve modes（纹理布局与 GLSL 版一致：
// 宽 6 列 = 曲线类型，高 256 行 = 样本点）

// Helper: Sample from specific curve column (0-5)
float msl_sample_hsl_curve(texture2d<float> lut_tex, sampler smp,
                           float sample_normalized, int curve_idx) {
    float x_coord = (float(curve_idx) + 0.5) / 6.0;
    return lut_tex.sample(smp, float2(x_coord, sample_normalized)).r;
}

// —— CURVES_SNIPPET.apply_hsl_curves 逐条翻译
float3 msl_apply_hsl_curves(float3 color, texture2d<float> hsl_lut_tex, sampler smp) {
    // AUD-0840：负值 HDR 颜色按 MIXER 同防护——整条 HSV 链以 |color|
    // 为基准，出口按符号还原（与 GLSL 权威逐条一致；非负输入恒等）。
    float3 sign_c = sign(color);
    float3 abs_color = abs(color);

    // Step 1: Convert RGB to HSV (we use HSV for better hue handling)
    float3 hsv = msl_rgb2hsv(abs_color);

    // Calculate luminance (Rec.709 luma coefficients)
    float luma = dot(abs_color, float3(0.299, 0.587, 0.114));

    // Normalize values for texture sampling
    float luma_norm = clamp(luma, 0.0, 1.0);      // For Lum-based curves
    float hue_norm = hsv.x;                        // Already 0-1 (0-360° normalized)
    float sat_norm = clamp(hsv.y, 0.0, 1.0);      // For Sat-based curves

    // Step 2: Sample all 6 curves
    float lum_sat_mult = msl_sample_hsl_curve(hsl_lut_tex, smp, luma_norm, 0);
    lum_sat_mult = clamp(lum_sat_mult, 0.0, 2.0);  // Clamp to prevent negative/overflow

    float hue_hue_shift = msl_sample_hsl_curve(hsl_lut_tex, smp, hue_norm, 1);
    hue_hue_shift = clamp(hue_hue_shift, -0.5, 0.5);  // Safety clamp

    float hue_sat_mult = msl_sample_hsl_curve(hsl_lut_tex, smp, hue_norm, 2);
    hue_sat_mult = clamp(hue_sat_mult, 0.0, 2.0);

    float hue_lum_mult = msl_sample_hsl_curve(hsl_lut_tex, smp, hue_norm, 3);
    hue_lum_mult = clamp(hue_lum_mult, 0.0, 2.0);

    float sat_sat_mult = msl_sample_hsl_curve(hsl_lut_tex, smp, sat_norm, 4);
    sat_sat_mult = clamp(sat_sat_mult, 0.0, 2.0);

    float sat_lum_mult = msl_sample_hsl_curve(hsl_lut_tex, smp, sat_norm, 5);
    sat_lum_mult = clamp(sat_lum_mult, 0.0, 2.0);

    // Step 3: Apply all modifications in sequence
    // 3.1: Apply Hue vs Hue shift first
    float shifted_hue = fract(hsv.x + hue_hue_shift);

    // 3.2: Apply saturation multipliers (cumulative)
    float final_sat_mult = lum_sat_mult;
    final_sat_mult *= hue_sat_mult;
    final_sat_mult *= sat_sat_mult;
    final_sat_mult = clamp(final_sat_mult, 0.0, 2.0);

    float3 gray = float3(luma);
    float3 sat_adjusted = mix(gray, abs_color, final_sat_mult);

    // Recalculate HSV after saturation adjustment, restore the shifted hue
    hsv = msl_rgb2hsv(sat_adjusted);
    hsv.x = shifted_hue;

    // 3.3: Apply luminance multipliers (cumulative)
    float final_lum_mult = 1.0;
    final_lum_mult *= hue_lum_mult;
    final_lum_mult *= sat_lum_mult;
    final_lum_mult = clamp(final_lum_mult, 0.0, 2.0);
    hsv.z = max(hsv.z * final_lum_mult, 0.0);

    // Step 4: Convert back to RGB（AUD-0840：按入口符号还原）
    return sign_c * msl_hsv2rgb(hsv);
}

// Legacy function name for backward compatibility
float3 msl_apply_hsl_curve(float3 color, texture2d<float> hsl_lut_tex, sampler smp) {
    return msl_apply_hsl_curves(color, hsl_lut_tex, smp);
}
"""


# ── LUT3D_SNIPPET 的 MSL 翻译 ────────────────────────────────────────────

MSL_LUT3D = """
// —— LUT3D_SNIPPET.apply_3dlut 逐条翻译（R05 强度混合规格 v1）
// GLSL textureSize(lut_tex, 0).x → MSL lut_tex.get_width()（同语义）。
float3 msl_apply_3dlut(float3 color, texture3d<float> lut_tex, sampler smp,
                       float intensity) {
    float3 sdr_part = clamp(color, 0.0, 1.0);
    // LUT 网格点 i 对应输入 i/(n-1)，texel i 中心位于 (i+0.5)/n：
    // 映射到 texel 中心坐标后再采样，消除边缘半 texel 拉伸。
    float lut_n = float(lut_tex.get_width());
    float3 lut_result = lut_tex.sample(smp, (sdr_part * (lut_n - 1.0) + 0.5) / lut_n).rgb;

    // base 保持完整输入（intensity=0 严格恒等，含 HDR/负值）；LUT 分支按
    // 原通道最大值比例重建 HDR；负偏移按强度加权加法保留。
    float max_orig = max(max(color.r, color.g), color.b);
    float max_sdr = max(max(sdr_part.r, sdr_part.g), sdr_part.b);
    float3 lut_hdr = lut_result;
    if (max_orig > 1.0 && max_sdr > 1.0e-5) {
        lut_hdr *= (max_orig / max_sdr);
    }
    float3 negative_excess = color - clamp(color, 0.0, 99999.0);
    return mix(color, lut_hdr, intensity) + negative_excess * intensity;
}
"""


# ── MIXER_SNIPPET 的 MSL 翻译 ────────────────────────────────────────────

MSL_MIXER = """
// —— MIXER_SNIPPET.apply_mixer 逐条翻译。
// 8 通道 vec3 uniform 由调用点从 struct 分量重建（见 msl_composition
// 的管线调用）；GLSL const 数组 + 局部数组在 MSL 同语义。
float3 msl_apply_mixer(float3 color,
                       float3 red_p, float3 orange_p, float3 yellow_p,
                       float3 green_p, float3 aqua_p, float3 blue_p,
                       float3 purple_p, float3 magenta_p) {
    float3 sign_c = sign(color);
    float3 hsv = msl_rgb2hsv(abs(color));
    float h = hsv.x * 360.0;
    const float centers[8] = {0.0, 30.0, 60.0, 120.0, 180.0, 240.0, 270.0, 300.0};
    float3 params[8] = {red_p, orange_p, yellow_p, green_p,
                        aqua_p, blue_p, purple_p, magenta_p};
    float total_hue_shift = 0.0;
    for (int i = 0; i < 8; i++) {
        float dist = min(abs(h - centers[i]), 360.0 - abs(h - centers[i]));
        float mask = clamp(1.0 - (dist / 30.0), 0.0, 1.0);
        if (params[i].x != 0.0) total_hue_shift += params[i].x * 0.3 * mask;
        if (params[i].y != 0.0) hsv.y *= (1.0 + (params[i].y / 100.0) * mask);
        if (params[i].z != 0.0) hsv.z *= (1.0 + (params[i].z / 100.0) * mask);
    }
    if (total_hue_shift != 0.0) hsv.x = fract((h + total_hue_shift) / 360.0);
    return sign_c * msl_hsv2rgb(hsv);
}
"""


# ── WARPER_SNIPPET 的 MSL 翻译 ───────────────────────────────────────────

MSL_WARPER = """
// —— WARPER_SNIPPET.apply_warper 逐条翻译。
// grid_size 在 GLSL 中同样未被使用（保留形参 ABI 一致）。
float3 msl_apply_warper(float3 color, texture2d<float> warper_tex,
                        sampler smp, float grid_size) {
    float3 sign_c = sign(color);
    float3 hsv = msl_rgb2hsv(abs(color));

    // Only warp pixels with meaningful saturation.
    if (hsv.y < 0.02) return color;

    // Sample the warper texture: stores (delta_hue, delta_sat) offsets.
    float2 coord = float2(hsv.x, hsv.y);
    float2 delta = warper_tex.sample(smp, coord).rg;

    // Smooth fade for low-saturation pixels to prevent noisy hue shifts
    float sat_fade = smoothstep(0.02, 0.15, hsv.y);

    float out_hue = fract(hsv.x + delta.x * sat_fade);
    float out_sat = clamp(hsv.y + delta.y * sat_fade, 0.0, 1.0);

    float3 warped_hsv = float3(out_hue, out_sat, hsv.z);
    return sign_c * msl_hsv2rgb(warped_hsv);
}
"""


# ── BLUR_SHARPEN_SNIPPET 的 MSL 翻译 ─────────────────────────────────────

# 注意：blur_rand 是 sin-hash 抖动（fract(sin(dot(co,vec2(12.9898,78.233)))
# * 43758.5453)）——公式逐字翻译，但 sin 大参数下的 ulp 差异会被 hash 放
# 大，抖动旋转角在两种后端上**允许不同**（抖动只改变采样相位，不改变均
# 值语义）。G2 门禁对模糊/锐化用例以实测为准（平域收敛、边缘为噪声级差
# 异则登记噪声豁免，结构性偏差则阻断）。
MSL_BLUR_SHARPEN = """
// —— BLUR_SHARPEN_SNIPPET 逐条翻译（blur_rand / spiral_blur /
// bilateral_blur / bokeh_blur / motion_blur / fast_blur /
// apply_blur_sharpen）。纹理与 sampler 显式传参；mat2 → float2x2
// （同列主序）；spiral_blur 在 GLSL 中同样无调用方，忠实保留。

float msl_blur_rand(float2 co) {
    return fract(sin(dot(co.xy, float2(12.9898, 78.233))) * 43758.5453);
}

float3 msl_spiral_blur(texture2d<float> tex, sampler smp, float2 uv,
                       float radius, float2 tex_size) {
    if (radius <= 0.0 || tex_size.x <= 0.0 || tex_size.y <= 0.0) return tex.sample(smp, uv).rgb;
    float3 acc = tex.sample(smp, uv).rgb * 2.0;
    float w = 2.0;
    for (float i = 1.0; i <= 8.0; i++) {
        float r = (i / 8.0) * radius;
        float a = i * 2.39996;
        acc += tex.sample(smp, uv + float2(cos(a), sin(a)) * r / tex_size).rgb;
        w += 1.0;
    }
    return acc / w;
}

float3 msl_bilateral_blur(texture2d<float> tex, sampler smp, float2 uv,
                          float intensity, float detail_preserve, float2 tex_size) {
    if (intensity <= 0.0) return tex.sample(smp, uv).rgb;

    // 分辨率缩放补偿：基于 2048px 基准宽度
    float scale = tex_size.x / 2048.0;
    float sigma_s = max(intensity * scale / 10.0, 0.1);
    float sigma_r = max(1.0 - detail_preserve / 100.0, 0.01) * 0.5;

    float3 center = tex.sample(smp, uv).rgb;
    float3 result = float3(0.0);
    float total_weight = 0.0;

    // Dithering for smoother results with fewer samples
    float rnd = msl_blur_rand(uv) * 6.28;
    float cos_r = cos(rnd);
    float sin_r = sin(rnd);
    float2x2 rot = float2x2(cos_r, -sin_r, sin_r, cos_r);

    // 5x5 dithered grid (25 taps) covering the same sigma_s window as the
    // original 7x7 kernel: step doubles to sigma_s/2 so the outermost taps
    // still reach sigma_s pixels, and the spatial falloff is tightened to
    // keep the effective gaussian width.
    for (int i = -2; i <= 2; i++) {
        for (int j = -2; j <= 2; j++) {
            float2 offset = rot * float2(float(i), float(j)) * (sigma_s / 2.0);
            float3 sample_c = tex.sample(smp, uv + offset / tex_size).rgb;

            float spatial_dist = float(i * i + j * j);
            float spatial_weight = exp(-spatial_dist / 4.0);

            float range_dist = distance(center, sample_c);
            float range_weight = exp(-range_dist * range_dist / (2.0 * sigma_r * sigma_r));

            float weight = spatial_weight * range_weight;
            result += sample_c * weight;
            total_weight += weight;
        }
    }

    return total_weight > 0.0 ? result / total_weight : center;
}

float3 msl_bokeh_blur(texture2d<float> tex, sampler smp, float2 uv,
                      float radius, float bokeh, float2 tex_size) {
    if (radius <= 0.0) return tex.sample(smp, uv).rgb;

    // 分辨率缩放补偿：基于 2048px 基准宽度
    float scale = tex_size.x / 2048.0;
    float r = radius * scale / 2.0;
    float highlight_boost = 1.0 + (bokeh / 100.0) * 2.0;

    float3 center = tex.sample(smp, uv).rgb;
    float3 result = float3(0.0);
    float total_weight = 0.0;

    // 28 dithered spiral taps: with LINEAR filtering + per-pixel rotation the
    // disk integral stays visually equivalent to the previous 48-tap version
    // at half the texture-fetch cost.
    const int samples = 28;
    float golden_angle = 2.39996;
    float noise = msl_blur_rand(uv + float2(0.123, 0.456)) * 6.28; // Random rotation per pixel

    for (int i = 0; i < samples; i++) {
        float fi = float(i);
        float theta = fi * golden_angle + noise;
        float dist = sqrt(fi / float(samples)) * r;

        float2 offset = float2(cos(theta), sin(theta)) * dist / tex_size;
        float3 sample_c = tex.sample(smp, uv + offset).rgb;

        // Highlight boost for cinematic bokeh
        float lum = dot(sample_c, float3(0.299, 0.587, 0.114));
        float highlight_factor = pow(max(lum, 0.001), 2.0) * highlight_boost;
        sample_c *= (1.0 + highlight_factor);

        float weight = 1.0; // Flat disk for bokeh
        result += sample_c * weight;
        total_weight += weight;
    }

    return result / total_weight;
}

float3 msl_motion_blur(texture2d<float> tex, sampler smp, float2 uv,
                       float intensity, float angle_degrees, float2 tex_size) {
    if (intensity <= 0.0) return tex.sample(smp, uv).rgb;

    // 分辨率缩放补偿：基于 2048px 基准宽度
    float scale = tex_size.x / 2048.0;
    // GLSL radians(angle_degrees) 的等值展开（MSL 无 radians 内建）
    float angle_rad = angle_degrees * 0.017453292519943295;
    float2 direction = float2(cos(angle_rad), sin(angle_rad));
    float blur_length = intensity * scale * 1.5;
    const int num_samples = 32;
    float jitter = msl_blur_rand(uv) - 0.5;

    float3 result = float3(0.0);
    for (int i = 0; i < num_samples; i++) {
        float t = (float(i) + jitter) / float(num_samples - 1);
        float offset = (t - 0.5) * blur_length;
        float2 tex_offset = direction * offset / tex_size;
        result += tex.sample(smp, uv + tex_offset).rgb;
    }

    return result / float(num_samples);
}

float3 msl_fast_blur(texture2d<float> tex, sampler smp, float2 uv,
                     float intensity, float radius, float2 tex_size) {
    // 分辨率缩放补偿：基于 2048px 基准宽度
    float scale = tex_size.x / 2048.0;
    float r = (intensity * radius * scale) / 50.0;
    if (r <= 0.0) return tex.sample(smp, uv).rgb;

    float3 result = float3(0.0);
    float total_weight = 0.0;
    float noise = msl_blur_rand(uv) * 6.28;

    // 12 samples in a dithered spiral pattern (was 16): LINEAR filtering plus
    // per-pixel jitter keeps the smoothed disk visually equivalent.
    for (int i = 0; i < 12; i++) {
        float theta = float(i) * 2.39996 + noise;
        float dist = sqrt(float(i) / 12.0) * r;
        float2 offset = float2(cos(theta), sin(theta)) * dist / tex_size;
        result += tex.sample(smp, uv + offset).rgb;
        total_weight += 1.0;
    }

    return result / total_weight;
}

float3 msl_apply_blur_sharpen(float3 color, texture2d<float> tex, sampler smp,
                              float2 uv, int mode, float sharpen,
                              float texture_amt, float halo, float radius,
                              int iterations, float2 tex_size, bool enabled,
                              bool blur_enabled, int blur_mode,
                              float blur_intensity, float blur_detail,
                              float blur_radius, float blur_bokeh,
                              float blur_angle) {
    if (tex_size.x <= 0.0 || tex_size.y <= 0.0) return color;

    float3 res = color;
    float3 blur_result = color;

    // 1. Sharpening (USM)
    if (enabled && (sharpen > 0.0 || texture_amt > 0.0)) {
        float r = (mode == 1) ? max(radius, 0.5) : 1.0;
        int iters = (mode == 1) ? clamp(iterations, 1, 10) : 1;

        // Center tap is loop-invariant; sample it once outside the iteration
        // loop (each iteration only changes the blur radius, not the pixel).
        float3 raw_pix = tex.sample(smp, uv).rgb;
        for (int i = 0; i < iters; i++) {
            // Use fast_blur to create the 'blurred' version for USM
            // Scale radius by 50.0 because fast_blur expects 0-100 range values, while USM radius is typically 0.5-10.0 px
            float3 blurred = msl_fast_blur(tex, smp, uv, 1.0, r * 50.0 * (1.0 + float(i) * 0.5), tex_size);
            float3 detail = raw_pix - blurred;

            // Halo suppression (optional clamping)
            if (halo > 0.0) {
                float h_limit = 1.0 - (halo / 100.0) * 0.5;
                detail = clamp(detail, float3(-h_limit), float3(h_limit));
            }

            // Apply detail to the current graded color
            // Scaling: map 0-100 to roughly 0-2.0 multiplier
            float amt = (sharpen / 50.0) + (texture_amt / 100.0);
            res += detail * amt;
        }
    }

    // 2. Blur (separate result, then mix)
    if (blur_enabled) {
        if (blur_intensity > 0.0 || blur_radius > 0.0) {
            if (blur_mode == 0) blur_result = msl_bilateral_blur(tex, smp, uv, blur_intensity, blur_detail, tex_size);
            else if (blur_mode == 1) blur_result = msl_bokeh_blur(tex, smp, uv, blur_radius, blur_bokeh, tex_size);
            else if (blur_mode == 2) blur_result = msl_motion_blur(tex, smp, uv, blur_intensity, blur_angle, tex_size);
            else if (blur_mode == 3) blur_result = msl_fast_blur(tex, smp, uv, blur_intensity, blur_radius, tex_size);

            // Mix blur result with sharpened result to preserve sharpening effect
            // Use blur_intensity/100 as blend factor
            float blur_mix = max(blur_intensity / 100.0, blur_radius / 100.0);
            blur_mix = clamp(blur_mix, 0.0, 1.0);
            res = mix(res, blur_result, blur_mix);
        }
    }

    return res;
}
"""


# ── BLEND_MODES_SNIPPET 的 MSL 翻译 ──────────────────────────────────────

MSL_BLEND_MODES = """
// —— BLEND_MODES_SNIPPET 逐条翻译（mode 0~18；12~15 需 HSV 函数）。
// base = 原始输入色，blend_layer = 管线处理结果（GL composition main
// 语义）；mix(mask * opacity) 属于 composition main 尾部（emitter 的
// blend epilog），不在本函数内。
float msl_blend_overlay(float b, float a) {
    return b < 0.5 ? (2.0 * b * a) : (1.0 - 2.0 * (1.0 - b) * (1.0 - a));
}

float msl_blend_soft_light(float b, float a) {
    return (a < 0.5) ? (2.0 * b * a + b * b * (1.0 - 2.0 * a))
                     : (sqrt(b) * (2.0 * a - 1.0) + 2.0 * b * (1.0 - a));
}

float3 msl_apply_blend_mode(float3 base, float3 blend_layer, int mode) {
    if (mode <= 0) return blend_layer;
    if (mode == 1) return base * blend_layer;
    if (mode == 2) return 1.0 - (1.0 - base) * (1.0 - blend_layer);
    if (mode == 3) return float3(msl_blend_overlay(base.r, blend_layer.r),
                                 msl_blend_overlay(base.g, blend_layer.g),
                                 msl_blend_overlay(base.b, blend_layer.b));
    if (mode == 4) return float3(msl_blend_soft_light(base.r, blend_layer.r),
                                 msl_blend_soft_light(base.g, blend_layer.g),
                                 msl_blend_soft_light(base.b, blend_layer.b));
    if (mode == 5) return float3(msl_blend_overlay(blend_layer.r, base.r),
                                 msl_blend_overlay(blend_layer.g, base.g),
                                 msl_blend_overlay(blend_layer.b, base.b));
    if (mode == 6) return base / max(float3(0.001), 1.0 - blend_layer);
    if (mode == 7) return 1.0 - (1.0 - base) / max(float3(0.001), blend_layer);
    if (mode == 8) return min(base, blend_layer);
    if (mode == 9) return max(base, blend_layer);
    if (mode == 10) return abs(base - blend_layer);
    if (mode == 11) return base + blend_layer - 2.0 * base * blend_layer;
    if (mode >= 12 && mode <= 15) {
        float3 b_hsv = msl_rgb2hsv(base);
        float3 a_hsv = msl_rgb2hsv(blend_layer);
        if (mode == 12) return msl_hsv2rgb(float3(a_hsv.x, b_hsv.y, b_hsv.z));
        if (mode == 13) return msl_hsv2rgb(float3(b_hsv.x, a_hsv.y, b_hsv.z));
        // AUD-0910：Color = 上层色相+饱和度 + 下层明度（a.x, a.y, b.z），
        // 与 GLSL 权威（shader_nodes.py blend mode 14）及 CPU 参照同步。
        if (mode == 14) return msl_hsv2rgb(float3(a_hsv.x, a_hsv.y, b_hsv.z));
        if (mode == 15) return msl_hsv2rgb(float3(b_hsv.x, b_hsv.y, a_hsv.z));
    }
    if (mode == 16) return base + blend_layer;
    if (mode == 17) return max(base - blend_layer, 0.0);
    if (mode == 18) return base / max(float3(0.001), blend_layer);
    return blend_layer;
}
"""


# ── SOFT_CLIP_SNIPPET 的 MSL 翻译 ────────────────────────────────────────

# GLSL `for(i=0..3) res[i]=x` 分量循环的逐分量展开（Toe 双分支）
MSL_SOFT_CLIP_HELPERS = """
// Toe 逐通道压缩（黑场拽回 + elif 钳制分支——原实现语义，如实移植）
float _soft_clip_toe(float c, float low, float black_point, float toe_strength) {
    if (c < low && black_point < low) {
        float T = low;
        float B = black_point;
        float k = max(toe_strength, 0.01);
        float diff = T - c;
        float range = T - B;
        return B + range * pow(1.0 + k * diff / range, -1.0 / k);
    } else if (c < black_point) {
        return black_point;
    }
    return c;
}
"""

MSL_SOFT_CLIP = """
// —— SOFT_CLIP_SNIPPET.apply_soft_clip 逐条翻译
float3 msl_apply_soft_clip(float3 color, float high, float white_point,
                           float shoulder_strength, float low,
                           float black_point, float toe_strength,
                           float exposure, float contrast) {
    float3 res = color;

    // 1. Exposure and Contrast Pre-adjustments
    res *= pow(2.0, exposure);
    if (contrast != 0.0) {
        float factor = (contrast >= 0.0) ? (1.0 + contrast) : (1.0 / (1.0 + abs(contrast)));
        // 修复对比度炸弹：使用 sign 和 abs，让 S 曲线完美延展到负数空间，不裁切任何信息
        res = sign(res) * pow(abs(res) / 0.18, float3(factor)) * 0.18;
    }

    // --- 核心进化 1：暗部 (Toe) 逐通道压缩 ---
    res.r = _soft_clip_toe(res.r, low, black_point, toe_strength);
    res.g = _soft_clip_toe(res.g, low, black_point, toe_strength);
    res.b = _soft_clip_toe(res.b, low, black_point, toe_strength);

    // === 安全修复：防止暗部 RGB 通道失衡导致色块 ===
    float toe_min = min(min(res.r, res.g), res.b);
    float toe_max = max(max(res.r, res.g), res.b);
    // 如果暗部 RGB 差异过大（超过 0.2），且最小通道接近黑场，进行平衡校正
    if (toe_max - toe_min > 0.2 && toe_min < low) {
        // 计算各通道的差异比例
        float3 toe_diff = res - float3(toe_min);
        // 对差异较大的通道进行软性压缩，保持 RGB 相对比例
        float balance_factor = clamp((toe_max - toe_min) * 0.25, 0.0, 0.15);
        // 使用 luma 作为基准，向灰度方向平衡（防止色偏）
        float toe_luma = dot(res, float3(0.299, 0.587, 0.114));
        res = mix(res, float3(toe_luma), balance_factor * abs(toe_min));
    }

    // --- 核心进化 2：亮部 (Shoulder) 比例缩放保护 ---
    // 提取压缩暗部后的新极值，按比例压暗高光，死守色彩饱和度
    float max_c = max(max(res.r, res.g), res.b);
    if (max_c > high && white_point > high) {
        float mapped_c = max_c;
        float T = high;
        float W = white_point;
        float k = max(shoulder_strength, 0.01);
        float diff = mapped_c - T;
        float range = W - T;
        mapped_c = W - range * pow(1.0 + k * diff / range, -1.0 / k);

        float scale = mapped_c / max_c;
        res *= scale;
    } else if (max_c > white_point) {
        float scale = white_point / max_c;
        res *= scale;
    }

    return res;
}
"""


# ── CURVES_SOFT_CLIP_SNIPPET 的 MSL 翻译 ────────────────────────────────

# 曲线面板四参数柔化裁切（指数软膝）——与 GLSL CURVES_SOFT_CLIP_SNIPPET
# 及移动端 soft_clip.frag 逐式同形；默认参数下严格恒等。修改必须三端同步。
MSL_CURVES_SOFT_CLIP = """
// —— CURVES_SOFT_CLIP_SNIPPET 逐条翻译（每通道指数软膝，移动端同式）
float msl_apply_soft_clip_knee(float value, float low_clip, float low_soft,
                               float high_clip, float high_soft) {
    float gap = max(high_clip - low_clip, 0.0);
    if (gap == 0.0) return low_clip;
    float low_knee = clamp(low_soft, 0.0, gap);
    float high_knee = clamp(high_soft, 0.0, max(gap - low_knee, 0.0));

    if (low_knee > 0.0) {
        float low_start = low_clip + low_knee;
        if (value < low_start) {
            value = low_clip + low_knee * exp((value - low_start) / low_knee);
        }
    } else if (low_clip != 0.0 && value < low_clip) {
        value = low_clip;
    }

    if (high_knee > 0.0) {
        float high_start = high_clip - high_knee;
        if (value > high_start) {
            value = high_clip - high_knee * exp(-(value - high_start) / high_knee);
        }
    } else if (high_clip != 1.0 && value > high_clip) {
        value = high_clip;
    }
    return value;
}

float3 msl_apply_curves_soft_clip(float3 color, float3 low_clip, float3 low_soft,
                                  float3 high_clip, float3 high_soft) {
    return float3(
        msl_apply_soft_clip_knee(color.r, low_clip.r, low_soft.r, high_clip.r, high_soft.r),
        msl_apply_soft_clip_knee(color.g, low_clip.g, low_soft.g, high_clip.g, high_soft.g),
        msl_apply_soft_clip_knee(color.b, low_clip.b, low_soft.b, high_clip.b, high_soft.b)
    );
}
"""


# ── FILM_GRAIN_SNIPPET 的 MSL 翻译 ───────────────────────────────────────

# hash_grain 为无 sin 的 fract-hash（纯 +/* 运算）——与 P6-06 compute
# 内核（kernels_color.py）同一公式同一参考实现，门禁沿用其 ≤1 fp16 ulp
# 实测约定；GL 侧噪声相位不同属预期（同分布），位级一致性以
# 「同设备两次渲染逐位相同」为准。dot/mix 内建必须展开为显式同序四则
# （AIR 的 FMA 收缩/代数重写会翻转哈希链，见 kernels_color 顺序敏感区
# 注记）——本翻译与内核逐项同形。
MSL_FILM_GRAIN = """
// —— FILM_GRAIN_SNIPPET 逐条翻译（hash_grain / value_noise / fbm_grain /
// apply_film_grain）。u_visible_rect / u_tex_size 由调用点从 struct 重建。

// 基础随机哈希（dot/fract 显式展开为同序四则——哈希链对求值顺序与
// FMA 收缩极端敏感，P6-06 实测：内建原语形态会使整格翻转；与
// kernels_color.METAL_FILM_GRAIN_MSL 逐项同形，不得单侧修改）
float msl_hash_grain(float px, float py) {
    float vx = px * 0.1031;
    float vy = py * 0.1031;
    float p3x = vx - floor(vx);
    float p3y = vy - floor(vy);
    float p3z = p3x;
    // GLSL: p3 += dot(p3, p3.yzx + 33.33) —— dot 展开为显式同序求和
    float d = p3x*(p3y + 33.33) + p3y*(p3z + 33.33) + p3z*(p3x + 33.33);
    p3x = p3x + d;
    p3y = p3y + d;
    p3z = p3z + d;
    float s = (p3x + p3y) * p3z;
    return s - floor(s);
}

// 平滑插值的有机值噪声 (Value Noise) - 模拟真实胶片银盐晶体的有机团块感
float msl_value_noise(float px, float py) {
    float ix = floor(px);
    float iy = floor(py);
    float fx = px - ix;
    float fy = py - iy;
    // Cubic Hermine interpolation for smooth organic feel
    float ux = fx*fx*(3.0 - 2.0*fx);
    float uy = fy*fy*(3.0 - 2.0*fy);

    // Sample four corners of the grid cell
    float a = msl_hash_grain(ix + 0.0, iy + 0.0);
    float b = msl_hash_grain(ix + 1.0, iy + 0.0);
    float c = msl_hash_grain(ix + 0.0, iy + 1.0);
    float d = msl_hash_grain(ix + 1.0, iy + 1.0);

    // Bilinear interpolation（mix(x,y,t) = x*(1-t) + y*t，显式展开）
    float m1 = a*(1.0 - ux) + b*ux;
    float m2 = c*(1.0 - ux) + d*ux;
    return m1*(1.0 - uy) + m2*uy;
}

// 多层叠加噪声 - 增加胶片颗粒的自然复杂度
float msl_fbm_grain(float2 p, float seed) {
    float value = 0.0;
    float amplitude = 0.5;
    float2 p_shifted = p + seed * 100.0;

    // 3 octaves of noise for natural film grain complexity
    for (int i = 0; i < 3; i++) {
        value += amplitude * msl_value_noise(p_shifted.x, p_shifted.y);
        p_shifted *= 2.0;
        amplitude *= 0.5;
    }
    return value;
}

float3 msl_apply_film_grain(float3 color, float2 uv, float intensity,
                            float size, bool monochrome, float shadows,
                            float midtones, float highlights, float seed,
                            float aspect_ratio, float2 tex_size,
                            float4 visible_rect) {
    if (intensity <= 0.0) return color;

    // 1. 尺度补偿 (以 2048px 为基准准星，保证不同分辨率下颗粒物理面积的视觉占比一致)
    float scale_factor = max(1.0, tex_size.x / 2048.0);
    float physical_size = max(size * scale_factor, 1.0);  // 确保最小为 1.0

    // 2. 坐标系映射到真实的绝对物理像素（噪点种子绑定到图像空间）
    float2 grain_uv;
    if (visible_rect.z > 0.0 && visible_rect.w > 0.0) {
        float2 abs_pixel = visible_rect.xy + uv * visible_rect.zw;
        grain_uv = abs_pixel / physical_size;
    } else {
        // 回退：使用纹理尺寸作为物理像素坐标
        grain_uv = (uv * tex_size) / physical_size;
    }

    // 3. 生成有机银盐颗粒 (支持单色和彩色胶片特性)
    float3 noise;
    if (monochrome) {
        float n = msl_fbm_grain(grain_uv, seed);
        noise = float3(n);
    } else {
        // 彩色胶片：模拟各感光层的颗粒错位 (红、绿、蓝三层乳剂略有偏移)
        noise.r = msl_fbm_grain(grain_uv, seed);
        noise.g = msl_fbm_grain(grain_uv + float2(12.3, 45.6), seed);
        noise.b = msl_fbm_grain(grain_uv + float2(78.9, 12.3), seed);
    }

    // 4. 提取原图亮度计算响应蒙版
    float luma = dot(color, float3(0.299, 0.587, 0.114));
    float mask = luma < 0.5
        ? mix(shadows, midtones, smoothstep(0.0, 0.5, luma))
        : mix(midtones, highlights, smoothstep(0.5, 1.0, luma));

    // 5. 光学柔光混合 (Soft Light Blending) 而不是死板的加法
    float3 grain_offset = (noise - 0.5) * intensity * mask;

    // 对比度保护算法：根据原始亮度调整颗粒影响强度
    float contrast_protection = 1.0 - abs(luma - 0.5) * 1.5;
    contrast_protection = max(contrast_protection, 0.2); // 保证最小影响量

    float3 result = color + grain_offset * contrast_protection;

    // 保留负值空间，允许暗部细节恢复
    return result;
}
"""
