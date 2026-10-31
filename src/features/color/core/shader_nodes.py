# GLSL Code Snippets for Pipeline Fusion

COMMON_DEFINITIONS = """
// Convert RGB to HSV
vec3 rgb2hsv(vec3 c) {
    vec4 K = vec4(0.0, -1.0 / 3.0, 2.0 / 3.0, -1.0);
    vec4 p = mix(vec4(c.bg, K.wz), vec4(c.gb, K.xy), step(c.b, c.g));
    vec4 q = mix(vec4(p.xyw, c.r), vec4(c.r, p.yzx), step(p.x, c.r));

    float d = q.x - min(q.w, q.y);
    float e = 1.0e-10;
    return vec3(abs(q.z + (q.w - q.y) / (6.0 * d + e)), d / (q.x + e), q.x);
}

// Convert HSV to RGB
vec3 hsv2rgb(vec3 c) {
    vec4 K = vec4(1.0, 2.0 / 3.0, 1.0 / 3.0, 3.0);
    vec3 p = abs(fract(c.xxx + K.xyz) * 6.0 - K.www);
    return c.z * mix(K.xxx, clamp(p - K.xxx, 0.0, 1.0), c.y);
}
"""

GRADING_SNIPPET = """
uniform vec2 u_tex_size;

vec3 apply_grading(vec3 color, vec3 lift, vec3 gamma, vec3 gain, float exposure, float temp, float tint, float contrast, float saturation, float vibrance, float shadow, float highlights, float mid_detail, float sharpness, float hue_shift, float lift_saturation, float gamma_saturation, float gain_saturation) {
    // 1. Exposure - Reversible linear multiplier
    float exp_stops = (exposure / 100.0) * (exposure / 100.0) * 6.0;
    if (exposure < 0.0) exp_stops = -exp_stops;
    color *= pow(2.0, exp_stops);
    
    // 2. White Balance (Simple Gain)
    vec3 wb = vec3(1.0);
    wb.r = 1.0 + temp / 200.0;
    wb.b = 1.0 - temp / 200.0;
    wb.g = 1.0 - tint / 200.0;
    color *= wb;

    // 3. Contrast - Pivot around 0.18 (Physical Mid-Grey)
    if (contrast != 0.0) {
        // Map -100..100 to factor 0..2 (roughly)
        float c_factor = 1.0 + contrast / 100.0;
        // Use natural power curve for contrast centered at 0.18
        color = sign(color) * pow(abs(color) / 0.18, vec3(c_factor)) * 0.18;
    }

    // 4. Lift / Gamma / Gain
    vec3 l = (lift - 1.0) * 0.2;
    color = color * gain * (1.0 - l) + l;
    
    // Gamma (HDR & Negative Safe)
    vec3 safe_gamma = max(gamma, vec3(0.01)); 
    vec3 gm = vec3(pow(2.0, -log2(safe_gamma.r)), pow(2.0, -log2(safe_gamma.g)), pow(2.0, -log2(safe_gamma.b)));
    
    // Apply Gamma using "SDR Curve + HDR/Negative Linear Extension"
    vec3 res;
    for(int i=0; i<3; i++) {
        float x = color[i];
        if (x > 0.0 && x <= 1.0) {
            res[i] = pow(x, gm[i]);
        } else if (x > 1.0) {
            res[i] = gm[i] * (x - 1.0) + 1.0;
        } else {
            // Negative values: linear extension to allow recovery
            res[i] = gm[i] * x;
        }
    }
    color = res;
    
    // === 安全修复：防止 Lift/Gamma 导致暗部颜色溢出 ===
    // 1. 暗部负值软性保护：对极端负值进行压缩，防止色块
    float luma_post_grading = dot(color, vec3(0.299, 0.587, 0.114));
    // 如果暗部（luma < 0）且某个通道极端负值，进行保护
    if (luma_post_grading < 0.0) {
        for(int i=0; i<3; i++) {
            float x = color[i];
            // 暗部负值软性压缩（保留 -0.5 范围）
            if (x < -0.5) {
                x = -0.5 - (abs(x + 0.5) * 0.1);
            }
            color[i] = x;
        }
        // RGB 通道平衡检查：防止 Gamma 线性外推导致的通道失衡
        float min_channel = min(min(color.r, color.g), color.b);
        float max_channel = max(max(color.r, color.g), color.b);
        if (max_channel - min_channel > 0.15 && min_channel < 0.0) {
            // 向灰度方向平衡，减少色偏
            float balance_factor = clamp(abs(min_channel) * 0.25, 0.0, 0.15);
            color = mix(color, vec3(luma_post_grading), balance_factor);
        }
    }

    // 5. Shadows - Power Curve (Natural Lifting)
    if (shadow != 0.0) {
        float shadow_factor = 1.0 + (shadow / 100.0);
        if (shadow_factor > 0.0) {
            // Apply power curve to [0, 1] range, keeping 0 and 1 fixed
            vec3 clamped_c = clamp(color, 0.0, 1.0);
            color = pow(clamped_c, vec3(1.0 / shadow_factor)) + (color - clamped_c);
        }
    }

    // 6. Highlights - Reinhard Tone Mapping (Soft Compression)
    if (highlights != 0.0) {
        float max_c = max(max(color.r, color.g), color.b);
        if (max_c > 0.1) {
            float mapped_max = max_c;
            if (highlights < 0.0) {
                // Compression
                float k = abs(highlights / 100.0) * 2.0;
                mapped_max = 0.1 + (max_c - 0.1) / (1.0 + k * (max_c - 0.1));
            } else {
                // Boost
                float k = (highlights / 100.0) * 0.5;
                mapped_max = 0.1 + (max_c - 0.1) * (1.0 + k);
            }
            color *= (mapped_max / max_c);
        }
    }

    // 6.5 色调区间饱和度（Lift/Gamma/Gain 区间各自的饱和度偏置）。
    // 与移动端 mega.frag 的 tonal_saturation_adjust_g 同款数学：
    // 按亮度把像素划入暗部/中间调/亮部三个权重区，再按区间施加
    // 对应的饱和度偏置（-100..100）。插在 highlights 与 vibrance
    // 之间，与移动端渲染顺序一致。
    if (lift_saturation != 0.0 || gamma_saturation != 0.0 || gain_saturation != 0.0) {
        float tonal_lum = clamp(dot(color, vec3(0.299, 0.587, 0.114)), 0.0, 1.0);
        float tonal_shadow_w = 1.0 - smoothstep(0.0, 0.5, tonal_lum);
        float tonal_mid_w = smoothstep(0.0, 0.5, tonal_lum) *
                            (1.0 - smoothstep(0.5, 1.0, tonal_lum));
        float tonal_high_w = smoothstep(0.5, 1.0, tonal_lum);
        float tonal_amount = lift_saturation * tonal_shadow_w +
                             gamma_saturation * tonal_mid_w +
                             gain_saturation * tonal_high_w;
        if (tonal_amount != 0.0) {
            float tonal_gray = dot(color, vec3(0.299, 0.587, 0.114));
            color = mix(vec3(tonal_gray), color, 1.0 + tonal_amount / 100.0);
        }
    }

    // 7. Vibrance
    if (vibrance != 0.0) {
        float gray = dot(color, vec3(0.299, 0.587, 0.114));
        float max_c = max(color.r, max(color.g, color.b));
        float sat = max_c - gray;
        float amt = vibrance / 100.0;
        float boost = amt * (1.0 - clamp(sat, 0.0, 1.0));
        color = mix(color, vec3(gray), -boost);
    }

    // 8. Midtone Detail & Sharpness (3px Radius sampling for Mid Detail)
    if (mid_detail != 0.0 || sharpness != 0.0) {
        // 分辨率缩放补偿：基于 2048px 基准宽度
        float scale_factor = u_tex_size.x / 2048.0;
        vec2 tex_offset_mid = 3.0 * scale_factor / u_tex_size; 
        vec2 tex_offset_sharp = 1.0 * scale_factor / u_tex_size;

        vec3 avg_mid = vec3(0.0);
        avg_mid += texture(u_input_tex, v_texcoord + vec2(tex_offset_mid.x, 0.0)).rgb;
        avg_mid += texture(u_input_tex, v_texcoord + vec2(-tex_offset_mid.x, 0.0)).rgb;
        avg_mid += texture(u_input_tex, v_texcoord + vec2(0.0, tex_offset_mid.y)).rgb;
        avg_mid += texture(u_input_tex, v_texcoord + vec2(0.0, -tex_offset_mid.y)).rgb;
        avg_mid *= 0.25;

        vec3 avg_sharp = vec3(0.0);
        avg_sharp += texture(u_input_tex, v_texcoord + vec2(tex_offset_sharp.x, 0.0)).rgb;
        avg_sharp += texture(u_input_tex, v_texcoord + vec2(-tex_offset_sharp.x, 0.0)).rgb;
        avg_sharp += texture(u_input_tex, v_texcoord + vec2(0.0, tex_offset_sharp.y)).rgb;
        avg_sharp += texture(u_input_tex, v_texcoord + vec2(0.0, -tex_offset_sharp.y)).rgb;
        avg_sharp *= 0.25;
        
        float lum = dot(color, vec3(0.299, 0.587, 0.114));
        
        // Mid Detail (Clarity)
        if (mid_detail != 0.0) {
            float mid_mask = smoothstep(0.0, 0.5, lum) * (1.0 - smoothstep(0.5, 1.0, lum)) * 4.0;
            color += (color - avg_mid) * (mid_detail / 100.0) * mid_mask;
        }

        // Sharpness (Edge Enhancement)
        if (sharpness != 0.0) {
            // Correct sharpening: color + (color - blurred) * amount
            color += (color - avg_sharp) * (sharpness / 100.0);
        }
    }

    // 9. Hue Shift
    // AUD-0840：负值 HDR 颜色（lift/gamma 之后可达）直接进 rgb2hsv 会得到
    // 负饱和度与失真色相——先取绝对值旋转、再按符号还原（与 MIXER/WARPER
    // 同防护）；非负输入 abs 为恒等，行为不变。
    if (hue_shift != 0.0) {
        vec3 sign_c = sign(color);
        vec3 hsv = rgb2hsv(abs(color));
        hsv.x = fract(hsv.x + hue_shift / 360.0);
        color = sign_c * hsv2rgb(hsv);
    }

    // 10. Saturation
    if (saturation != 0.0) {
        float gray = dot(color, vec3(0.299, 0.587, 0.114));
        color = mix(vec3(gray), color, 1.0 + saturation / 100.0);
    }

    return color;
}
"""

CURVES_SNIPPET = """
vec3 apply_curves(vec3 color, sampler2D curves_tex) {
    // Advanced Curves with Linear Extrapolation to prevent clipping.
    // Instead of clamping input to [0, 1], we extrapolate the curve's slope
    // for values outside the range. This allows "finding back" colors.

    // Sample points near boundaries to calculate slopes
    vec3 v0 = texture(curves_tex, vec2(0.5 / 256.0, 0.5)).rgb;
    vec3 v1 = texture(curves_tex, vec2(1.5 / 256.0, 0.5)).rgb;
    vec3 v254 = texture(curves_tex, vec2(254.5 / 256.0, 0.5)).rgb;
    vec3 v255 = texture(curves_tex, vec2(255.5 / 256.0, 0.5)).rgb;

    vec3 slope_low = (v1 - v0) * 255.0;
    vec3 slope_high = (v255 - v254) * 255.0;

    vec3 res;
    for(int i=0; i<3; i++) {
        float x = color[i];
        if (x < 0.0) {
            res[i] = v0[i] + x * slope_low[i];
        } else if (x > 1.0) {
            res[i] = v255[i] + (x - 1.0) * slope_high[i];
        } else {
            res[i] = texture(curves_tex, vec2((x * 255.0 + 0.5) / 256.0, 0.5))[i];
        }
    }
    
    // === 安全修复：防止暗部颜色溢出 ===
    // 1. 暗部软性保护：对极端负值进行软性压缩，防止色块
    for(int i=0; i<3; i++) {
        float x = res[i];
        // 暗部负值软性压缩（保留 -0.5 范围，超出部分衰减）
        if (x < -0.5) {
            // 软性衰减：超过 -0.5 的部分按 0.1 比例保留
            x = -0.5 - (abs(x + 0.5) * 0.1);
        }
        // 高光软性保护（保留 2.0 范围，超出部分衰减）
        if (x > 2.0) {
            x = 2.0 + (x - 2.0) * 0.1;
        }
        res[i] = x;
    }
    
    // 2. RGB 通道平衡检查：防止三通道失衡导致色偏
    float min_channel = min(min(res.r, res.g), res.b);
    float max_channel = max(max(res.r, res.g), res.b);
    // 如果暗部 RGB 差异过大（超过 0.15），且最小通道为负值，进行平衡校正
    if (max_channel - min_channel > 0.15 && min_channel < 0.0) {
        // 计算各通道与最小通道的差异
        vec3 diff = res - vec3(min_channel);
        // 对负值区域进行软性平衡：减少差异，但不完全消除
        float balance_factor = clamp(abs(min_channel) * 0.3, 0.0, 0.2);
        res = res - diff * balance_factor;
    }
    
    return res;
}

// HSL Curves: 6 professional curve modes
// Uses a 2D R32F texture (6 x 256) where:
// - Width (x=0..5): curve type (LumSat, HueHue, HueSat, HueLum, SatSat, SatLum)
// - Height (y=0..255): sample points
// Column 0: LumSat (Luminance → Saturation multiplier)
// Column 1: HueHue (Hue → Hue shift)
// Column 2: HueSat (Hue → Saturation multiplier)
// Column 3: HueLum (Hue → Luminance multiplier)
// Column 4: SatSat (Saturation → Saturation multiplier)
// Column 5: SatLum (Saturation → Luminance multiplier)

// Helper: Sample from specific curve column (0-5)
float sample_hsl_curve(sampler2D lut_tex, float sample_normalized, int curve_idx) {
    // Texture is (6, 256), so:
    // - x coordinate: curve_idx / 5.5 to center on the curve column (0-5)
    // - y coordinate: sample_normalized maps to 256 sample points (0-1)
    float x_coord = (float(curve_idx) + 0.5) / 6.0;
    return texture(lut_tex, vec2(x_coord, sample_normalized)).r;
}

vec3 apply_hsl_curves(vec3 color, sampler2D hsl_lut_tex) {
    // AUD-0840：负值 HDR 颜色（curves 线性外推可产生）直接进 rgb2hsv 会
    // 得到负饱和度与失真色相——整条 HSV 链以 |color| 为基准，出口按符号
    // 还原（与 MIXER/WARPER 同防护）。非负输入 abs 恒等，行为不变。
    vec3 sign_c = sign(color);
    vec3 abs_color = abs(color);

    // Step 1: Convert RGB to HSV (we use HSV for better hue handling)
    vec3 hsv = rgb2hsv(abs_color);

    // Calculate luminance (Rec.709 luma coefficients)
    float luma = dot(abs_color, vec3(0.299, 0.587, 0.114));

    // Normalize values for texture sampling
    float luma_norm = clamp(luma, 0.0, 1.0);      // For Lum-based curves
    float hue_norm = hsv.x;                        // Already 0-1 (0-360° normalized)
    float sat_norm = clamp(hsv.y, 0.0, 1.0);      // For Sat-based curves

    // Step 2: Sample all 6 curves
    // Row 0: Lum vs Sat - saturation multiplier based on luminance
    float lum_sat_mult = sample_hsl_curve(hsl_lut_tex, luma_norm, 0);
    lum_sat_mult = clamp(lum_sat_mult, 0.0, 2.0);  // Clamp to prevent negative/overflow

    // Row 1: Hue vs Hue - hue shift based on hue (range -0.5 to +0.5)
    float hue_hue_shift = sample_hsl_curve(hsl_lut_tex, hue_norm, 1);
    hue_hue_shift = clamp(hue_hue_shift, -0.5, 0.5);  // Safety clamp

    // Row 2: Hue vs Sat - saturation multiplier based on hue
    float hue_sat_mult = sample_hsl_curve(hsl_lut_tex, hue_norm, 2);
    hue_sat_mult = clamp(hue_sat_mult, 0.0, 2.0);  // Clamp to prevent negative/overflow

    // Row 3: Hue vs Lum - luminance multiplier based on hue
    float hue_lum_mult = sample_hsl_curve(hsl_lut_tex, hue_norm, 3);
    hue_lum_mult = clamp(hue_lum_mult, 0.0, 2.0);  // Clamp to prevent negative/overflow

    // Row 4: Sat vs Sat - saturation multiplier based on saturation
    float sat_sat_mult = sample_hsl_curve(hsl_lut_tex, sat_norm, 4);
    sat_sat_mult = clamp(sat_sat_mult, 0.0, 2.0);  // Clamp to prevent negative/overflow

    // Row 5: Sat vs Lum - luminance multiplier based on saturation
    float sat_lum_mult = sample_hsl_curve(hsl_lut_tex, sat_norm, 5);
    sat_lum_mult = clamp(sat_lum_mult, 0.0, 2.0);  // Clamp to prevent negative/overflow

    // Step 3: Apply all modifications in sequence

    // 3.1: Apply Hue vs Hue shift first (before other hue-based modifications)
    // This allows hue remapping before saturation/luminance adjustments
    float shifted_hue = fract(hsv.x + hue_hue_shift);

    // 3.2: Apply saturation multipliers (cumulative)
    // Start with Lum vs Sat
    float final_sat_mult = lum_sat_mult;

    // Add Hue vs Sat multiplier (multiply together for cumulative effect)
    final_sat_mult *= hue_sat_mult;

    // Add Sat vs Sat multiplier
    final_sat_mult *= sat_sat_mult;

    // Clamp final saturation multiplier to prevent overflow/artifacts
    final_sat_mult = clamp(final_sat_mult, 0.0, 2.0);

    // Apply final saturation
    // sat_mult = 1.0 means no change
    // sat_mult = 0.0 means fully desaturated
    // sat_mult = 2.0 means double saturation
    vec3 gray = vec3(luma);
    vec3 sat_adjusted = mix(gray, abs_color, final_sat_mult);

    // Recalculate HSV after saturation adjustment
    hsv = rgb2hsv(sat_adjusted);
    // Restore the shifted hue (recalculating HSV overwrites hsv.x, so we restore it)
    hsv.x = shifted_hue;

    // 3.3: Apply luminance multipliers (cumulative)
    float final_lum_mult = 1.0;

    // Hue vs Lum multiplier
    final_lum_mult *= hue_lum_mult;

    // Sat vs Lum multiplier
    final_lum_mult *= sat_lum_mult;

    // Clamp final luminance multiplier to prevent overflow/artifacts
    final_lum_mult = clamp(final_lum_mult, 0.0, 2.0);

    // Apply luminance adjustment
    // lum_mult = 1.0 means no change
    // lum_mult = 0.5 means half brightness
    // lum_mult = 2.0 means double brightness
    // Note: Don't clamp to 1.0 to preserve HDR values > 1.0
    hsv.z = max(hsv.z * final_lum_mult, 0.0);

    // Step 4: Convert back to RGB（AUD-0840：按入口符号还原）
    vec3 result = sign_c * hsv2rgb(hsv);

    return result;
}

// Legacy function name for backward compatibility
vec3 apply_hsl_curve(vec3 color, sampler2D hsl_lut_tex) {
    return apply_hsl_curves(color, hsl_lut_tex);
}
"""

BLUR_SHARPEN_SNIPPET = """
// ============================================
// Blur Helpers & Dithering
// ============================================
float blur_rand(vec2 co) {
    return fract(sin(dot(co.xy ,vec2(12.9898,78.233))) * 43758.5453);
}

vec3 spiral_blur(sampler2D tex, vec2 uv, float radius, vec2 tex_size) {
    if (radius <= 0.0 || tex_size.x <= 0.0 || tex_size.y <= 0.0) return texture(tex, uv).rgb;
    vec3 acc = texture(tex, uv).rgb * 2.0;
    float w = 2.0;
    for (float i = 1.0; i <= 8.0; i++) {
        float r = (i / 8.0) * radius;
        float a = i * 2.39996;
        acc += texture(tex, uv + vec2(cos(a), sin(a)) * r / tex_size).rgb;
        w += 1.0;
    }
    return acc / w;
}

// ============================================
// 1. Bilateral Filter (Texture Soften)
// Improved: Range-adaptive sampling with dithering
// ============================================
vec3 bilateral_blur(sampler2D tex, vec2 uv, float intensity, float detail_preserve, vec2 tex_size) {
    if (intensity <= 0.0) return texture(tex, uv).rgb;
    
    // 分辨率缩放补偿：基于 2048px 基准宽度
    float scale = tex_size.x / 2048.0;
    float sigma_s = max(intensity * scale / 10.0, 0.1); 
    float sigma_r = max(1.0 - detail_preserve / 100.0, 0.01) * 0.5;
    
    vec3 center = texture(tex, uv).rgb;
    vec3 result = vec3(0.0);
    float total_weight = 0.0;
    
    // Dithering for smoother results with fewer samples
    float rnd = blur_rand(uv) * 6.28;
    float cos_r = cos(rnd);
    float sin_r = sin(rnd);
    mat2 rot = mat2(cos_r, -sin_r, sin_r, cos_r);

    // 5x5 dithered grid (25 taps) covering the same sigma_s window as the
    // original 7x7 kernel: step doubles to sigma_s/2 so the outermost taps
    // still reach sigma_s pixels, and the spatial falloff is tightened to
    // keep the effective gaussian width.
    for (int i = -2; i <= 2; i++) {
        for (int j = -2; j <= 2; j++) {
            vec2 offset = rot * vec2(float(i), float(j)) * (sigma_s / 2.0);
            vec3 sample_c = texture(tex, uv + offset / tex_size).rgb;

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

// ============================================
// 2. Bokeh Blur (Optical Blur / Lens)
// Improved: Dithered Golden Spiral for high-quality bokeh
// ============================================
vec3 bokeh_blur(sampler2D tex, vec2 uv, float radius, float bokeh, vec2 tex_size) {
    if (radius <= 0.0) return texture(tex, uv).rgb;
    
    // 分辨率缩放补偿：基于 2048px 基准宽度
    float scale = tex_size.x / 2048.0;
    float r = radius * scale / 2.0;
    float highlight_boost = 1.0 + (bokeh / 100.0) * 2.0;
    
    vec3 center = texture(tex, uv).rgb;
    vec3 result = vec3(0.0);
    float total_weight = 0.0;
    
    // 28 dithered spiral taps: with LINEAR filtering + per-pixel rotation the
    // disk integral stays visually equivalent to the previous 48-tap version
    // at half the texture-fetch cost.
    const int samples = 28;
    float golden_angle = 2.39996;
    float noise = blur_rand(uv + vec2(0.123, 0.456)) * 6.28; // Random rotation per pixel

    for (int i = 0; i < samples; i++) {
        float fi = float(i);
        float theta = fi * golden_angle + noise;
        float dist = sqrt(fi / float(samples)) * r;
        
        vec2 offset = vec2(cos(theta), sin(theta)) * dist / tex_size;
        vec3 sample_c = texture(tex, uv + offset).rgb;
        
        // Highlight boost for cinematic bokeh
        float lum = dot(sample_c, vec3(0.299, 0.587, 0.114));
        float highlight_factor = pow(max(lum, 0.001), 2.0) * highlight_boost;
        sample_c *= (1.0 + highlight_factor);
        
        float weight = 1.0; // Flat disk for bokeh
        result += sample_c * weight;
        total_weight += weight;
    }
    
    return result / total_weight;
}

// ============================================
// 3. Motion Blur (Directional Convolution)
// Improved: Jittered sampling to remove banding
// ============================================
vec3 motion_blur(sampler2D tex, vec2 uv, float intensity, float angle_degrees, vec2 tex_size) {
    if (intensity <= 0.0) return texture(tex, uv).rgb;
    
    // 分辨率缩放补偿：基于 2048px 基准宽度
    float scale = tex_size.x / 2048.0;
    float angle_rad = radians(angle_degrees);
    vec2 direction = vec2(cos(angle_rad), sin(angle_rad));
    float blur_length = intensity * scale * 1.5;
    const int num_samples = 32;
    float jitter = blur_rand(uv) - 0.5;
    
    vec3 result = vec3(0.0);
    for (int i = 0; i < num_samples; i++) {
        float t = (float(i) + jitter) / float(num_samples - 1);
        float offset = (t - 0.5) * blur_length;
        vec2 tex_offset = direction * offset / tex_size;
        result += texture(tex, uv + tex_offset).rgb;
    }
    
    return result / float(num_samples);
}

// ============================================
// 4. Fast Blur (Approximated Gaussian)
// Improved: Dithered multi-ring spiral (much smoother)
// ============================================
vec3 fast_blur(sampler2D tex, vec2 uv, float intensity, float radius, vec2 tex_size) {
    // 分辨率缩放补偿：基于 2048px 基准宽度
    float scale = tex_size.x / 2048.0;
    float r = (intensity * radius * scale) / 50.0;
    if (r <= 0.0) return texture(tex, uv).rgb;
    
    vec3 result = vec3(0.0);
    float total_weight = 0.0;
    float noise = blur_rand(uv) * 6.28;
    
    // 12 samples in a dithered spiral pattern (was 16): LINEAR filtering plus
    // per-pixel jitter keeps the smoothed disk visually equivalent.
    for (int i = 0; i < 12; i++) {
        float theta = float(i) * 2.39996 + noise;
        float dist = sqrt(float(i) / 12.0) * r;
        vec2 offset = vec2(cos(theta), sin(theta)) * dist / tex_size;
        result += texture(tex, uv + offset).rgb;
        total_weight += 1.0;
    }
    
    return result / total_weight;
}

// ============================================
// Main Blur/Sharpen Function
// ============================================
vec3 apply_blur_sharpen(vec3 color, sampler2D tex, vec2 uv, int mode, float sharpen, float texture_amt, float halo, float radius, int iterations, vec2 tex_size, bool enabled,
                        bool blur_enabled, int blur_mode, float blur_intensity, float blur_detail, float blur_radius, float blur_bokeh, float blur_angle) {
    if (tex_size.x <= 0.0 || tex_size.y <= 0.0) return color;
    
    vec3 res = color;
    vec3 blur_result = color;

    // 1. Sharpening (USM)
    if (enabled && (sharpen > 0.0 || texture_amt > 0.0)) {
        float r = (mode == 1) ? max(radius, 0.5) : 1.0;
        int iters = (mode == 1) ? clamp(iterations, 1, 10) : 1;

        // Center tap is loop-invariant; sample it once outside the iteration
        // loop (each iteration only changes the blur radius, not the pixel).
        vec3 raw_pix = texture(tex, uv).rgb;
        for(int i = 0; i < iters; i++) {
            // Use fast_blur to create the 'blurred' version for USM
            // Scale radius by 50.0 because fast_blur expects 0-100 range values, while USM radius is typically 0.5-10.0 px
            vec3 blurred = fast_blur(tex, uv, 1.0, r * 50.0 * (1.0 + float(i)*0.5), tex_size);
            vec3 detail = raw_pix - blurred;
            
            // Halo suppression (optional clamping)
            if (halo > 0.0) {
                float h_limit = 1.0 - (halo / 100.0) * 0.5;
                detail = clamp(detail, vec3(-h_limit), vec3(h_limit));
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
            if (blur_mode == 0) blur_result = bilateral_blur(tex, uv, blur_intensity, blur_detail, tex_size);
            else if (blur_mode == 1) blur_result = bokeh_blur(tex, uv, blur_radius, blur_bokeh, tex_size);
            else if (blur_mode == 2) blur_result = motion_blur(tex, uv, blur_intensity, blur_angle, tex_size);
            else if (blur_mode == 3) blur_result = fast_blur(tex, uv, blur_intensity, blur_radius, tex_size);
            
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

MIXER_SNIPPET = """
vec3 apply_mixer(vec3 color, vec3 red_p, vec3 orange_p, vec3 yellow_p, vec3 green_p, vec3 aqua_p, vec3 blue_p, vec3 purple_p, vec3 magenta_p) {
    vec3 sign_c = sign(color);
    vec3 hsv = rgb2hsv(abs(color));
    float h = hsv.x * 360.0;
    const float centers[8] = float[](0.0, 30.0, 60.0, 120.0, 180.0, 240.0, 270.0, 300.0);
    vec3 params[8] = vec3[](red_p, orange_p, yellow_p, green_p, aqua_p, blue_p, purple_p, magenta_p);
    float total_hue_shift = 0.0;
    for(int i=0; i<8; i++) {
        float dist = min(abs(h - centers[i]), 360.0 - abs(h - centers[i]));
        float mask = clamp(1.0 - (dist / 30.0), 0.0, 1.0);
        if (params[i].x != 0.0) total_hue_shift += params[i].x * 0.3 * mask;
        if (params[i].y != 0.0) hsv.y *= (1.0 + (params[i].y / 100.0) * mask);
        if (params[i].z != 0.0) hsv.z *= (1.0 + (params[i].z / 100.0) * mask);
    }
    if (total_hue_shift != 0.0) hsv.x = fract((h + total_hue_shift) / 360.0);
    return sign_c * hsv2rgb(hsv);
}
"""

LUT3D_SNIPPET = """
vec3 apply_3dlut(vec3 color, sampler3D lut_tex, float intensity) {
    vec3 sdr_part = clamp(color, 0.0, 1.0);
    // LUT 网格点 i 对应输入 i/(n-1)，而 GL texel i 中心位于 (i+0.5)/n。
    // 映射到 texel 中心坐标后再采样，消除边缘半 texel 拉伸（与
    // lut_exporter._apply_3dlut_cpu 的网格点对齐语义一致）。
    float lut_n = float(textureSize(lut_tex, 0).x);
    vec3 lut_result = texture(lut_tex, (sdr_part * (lut_n - 1.0) + 0.5) / lut_n).rgb;
    vec3 blended_sdr = mix(sdr_part, lut_result, intensity);

    // 比例缩放：保留 HDR 高光的纯正色彩比例
    float max_orig = max(max(color.r, color.g), color.b);
    if (max_orig > 1.0) {
        float max_sdr = max(max(sdr_part.r, sdr_part.g), sdr_part.b);
        if (max_sdr > 1e-5) {
            blended_sdr *= (max_orig / max_sdr);
        }
    }
    
    // 加法偏移：仅用于保留负数暗部不发生色彩反转
    vec3 negative_excess = color - clamp(color, 0.0, 99999.0);
    return blended_sdr + negative_excess;
}
"""

WARPER_SNIPPET = """
vec3 apply_warper(vec3 color, sampler2D warper_tex, float grid_size) {
    vec3 sign_c = sign(color);
    vec3 hsv = rgb2hsv(abs(color));

    // Only warp pixels with meaningful saturation.
    if (hsv.y < 0.02) return color;

    // Sample the warper texture: stores (delta_hue, delta_sat) offsets.
    vec2 coord = vec2(hsv.x, hsv.y);
    vec2 delta = texture(warper_tex, coord).rg;

    // Smooth fade for low-saturation pixels to prevent noisy hue shifts
    float sat_fade = smoothstep(0.02, 0.15, hsv.y);

    float out_hue = fract(hsv.x + delta.x * sat_fade);
    float out_sat = clamp(hsv.y + delta.y * sat_fade, 0.0, 1.0);

    vec3 warped_hsv = vec3(out_hue, out_sat, hsv.z);
    return sign_c * hsv2rgb(warped_hsv);
}
"""

BLEND_MODES_SNIPPET = """
float blend_overlay(float b, float a) { return b < 0.5 ? (2.0 * b * a) : (1.0 - 2.0 * (1.0 - b) * (1.0 - a)); }
float blend_soft_light(float b, float a) { return (a < 0.5) ? (2.0 * b * a + b * b * (1.0 - 2.0 * a)) : (sqrt(b) * (2.0 * a - 1.0) + 2.0 * b * (1.0 - a)); }
vec3 apply_blend_mode(vec3 base, vec3 blend_layer, int mode) {
    if (mode <= 0) return blend_layer;
    if (mode == 1) return base * blend_layer;
    if (mode == 2) return 1.0 - (1.0 - base) * (1.0 - blend_layer);
    if (mode == 3) return vec3(blend_overlay(base.r, blend_layer.r), blend_overlay(base.g, blend_layer.g), blend_overlay(base.b, blend_layer.b));
    if (mode == 4) return vec3(blend_soft_light(base.r, blend_layer.r), blend_soft_light(base.g, blend_layer.g), blend_soft_light(base.b, blend_layer.b));
    if (mode == 5) return vec3(blend_overlay(blend_layer.r, base.r), blend_overlay(blend_layer.g, base.g), blend_overlay(blend_layer.b, base.b));
    if (mode == 6) return base / max(vec3(0.001), 1.0 - blend_layer);
    if (mode == 7) return 1.0 - (1.0 - base) / max(vec3(0.001), blend_layer);
    if (mode == 8) return min(base, blend_layer);
    if (mode == 9) return max(base, blend_layer);
    if (mode == 10) return abs(base - blend_layer);
    if (mode == 11) return base + blend_layer - 2.0 * base * blend_layer;
    if (mode >= 12 && mode <= 15) {
        vec3 b_hsv = rgb2hsv(base); vec3 a_hsv = rgb2hsv(blend_layer);
        if (mode == 12) return hsv2rgb(vec3(a_hsv.x, b_hsv.y, b_hsv.z));
        if (mode == 13) return hsv2rgb(vec3(b_hsv.x, a_hsv.y, b_hsv.z));
        // AUD-0910：Color = 上层色相+饱和度 + 下层明度（a.x, a.y, b.z）。
        // 旧实现 (a.x, a.y, a.z) 是 HSV 恒等往返，退化为 Normal 且丢掉
        // base 亮度；CPU 参照（lut_exporter Color 分支）一直是 b.z。
        if (mode == 14) return hsv2rgb(vec3(a_hsv.x, a_hsv.y, b_hsv.z));
        if (mode == 15) return hsv2rgb(vec3(b_hsv.x, b_hsv.y, a_hsv.z));
    }
    if (mode == 16) return base + blend_layer;
    if (mode == 17) return max(base - blend_layer, 0.0);
    if (mode == 18) return base / max(vec3(0.001), blend_layer);
    return blend_layer;
}
"""

QUALIFIER_SNIPPET = """
// Phase 3: Qualifier uses HSV (Hue, Saturation, Value) semantics for HDR handling.
// The legacy lum_* params control HSV Value (max(r,g,b)), not HSL Lightness.
// See docs/color_tools_acceptance_residual_plan_2026-05-24.md Phase 3.
vec3 rgb_to_qualifier_hsv(vec3 color) {
    vec3 c = max(color, vec3(0.0));
    float c_max = max(max(c.r, c.g), c.b);
    float c_min = min(min(c.r, c.g), c.b);
    float delta = c_max - c_min;
    
    float h = 0.0;
    float s = 0.0;
    
    // 【关键修复】：将超出范围的 RAW 高光亮度安全钳制在 1.0
    // 这样 UI 的 0.0-1.0 滑块才能捕获它，色彩分离交给 H 和 S 负责
    float v = min(c_max, 1.0); 
    
    if (c_max > 0.00001) {
        // 使用 HSV 的饱和度公式 (delta / max)，它永远完美映射在 0-1 之间
        s = delta / c_max;
    }
    
    if (delta > 0.00001) {
        if (c.r == c_max) h = (c.g - c.b) / delta + (c.g < c.b ? 6.0 : 0.0);
        else if (c.g == c_max) h = (c.b - c.r) / delta + 2.0;
        else h = (c.r - c.g) / delta + 4.0;
        h /= 6.0;
    }
    
    return vec3(h, s, v);
}
vec3 rgb_to_hsl(vec3 color) {
    return rgb_to_qualifier_hsv(color);
}
float calculate_qualifier_hsv(vec3 hsv, bool h_enabled, float h_center, float h_width, float h_soft, bool s_enabled, float s_low, float s_high, float s_s_low, float s_s_high, bool l_enabled, float l_low, float l_high, float l_s_low, float l_s_high, bool invert) {
    float matte = 1.0;
    if (h_enabled) {
        float h = fract(hsv.x + 0.5); float dist = abs(h - h_center); if (dist > 0.5) dist = 1.0 - dist;
        matte *= 1.0 - smoothstep(h_width * 0.5, h_width * 0.5 + max(h_soft, 0.0001), dist);
    }
    if (s_enabled) {
        if (s_low > 0.0 || s_s_low > 0.0) matte *= smoothstep(s_low - max(s_s_low, 0.0001), s_low, hsv.y);
        if (s_high < 1.0 || s_s_high > 0.0) matte *= (1.0 - smoothstep(s_high, s_high + max(s_s_high, 0.0001), hsv.y));
    }
    if (l_enabled) {
        if (l_low > 0.0 || l_s_low > 0.0) matte *= smoothstep(l_low - max(l_s_low, 0.0001), l_low, hsv.z);
        if (l_high < 1.0 || l_s_high > 0.0) matte *= (1.0 - smoothstep(l_high, l_high + max(l_s_high, 0.0001), hsv.z));
    }
    return invert ? 1.0 - matte : matte;
}
float calculate_qualifier_hsl(vec3 hsl, bool h_enabled, float h_center, float h_width, float h_soft, bool s_enabled, float s_low, float s_high, float s_s_low, float s_s_high, bool l_enabled, float l_low, float l_high, float l_s_low, float l_s_high, bool invert) {
    return calculate_qualifier_hsv(hsl, h_enabled, h_center, h_width, h_soft, s_enabled, s_low, s_high, s_s_low, s_s_high, l_enabled, l_low, l_high, l_s_low, l_s_high, invert);
}
float calculate_qualifier(vec3 color, bool h_enabled, float h_center, float h_width, float h_soft, bool s_enabled, float s_low, float s_high, float s_s_low, float s_s_high, bool l_enabled, float l_low, float l_high, float l_s_low, float l_s_high, bool invert) {
    return calculate_qualifier_hsv(rgb_to_qualifier_hsv(color), h_enabled, h_center, h_width, h_soft, s_enabled, s_low, s_high, s_s_low, s_s_high, l_enabled, l_low, l_high, l_s_low, l_s_high, invert);
}
"""

FILM_GRAIN_SNIPPET = """
uniform vec4 u_visible_rect; // 视口在原图上的 x, y, width, height (图像空间绝对物理像素坐标)
// 注意：u_tex_size 由 BLUR_SHARPEN_SNIPPET 声明，这里直接使用

// 基础随机哈希
float hash_grain(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * .1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

// 平滑插值的有机值噪声 (Value Noise) - 模拟真实胶片银盐晶体的有机团块感
float value_noise(vec2 p) {
    vec2 i = floor(p);
    vec2 f = fract(p);
    // Cubic Hermine interpolation for smooth organic feel
    vec2 u = f * f * (3.0 - 2.0 * f);

    // Sample four corners of the grid cell
    float a = hash_grain(i + vec2(0.0, 0.0));
    float b = hash_grain(i + vec2(1.0, 0.0));
    float c = hash_grain(i + vec2(0.0, 1.0));
    float d = hash_grain(i + vec2(1.0, 1.0));

    // Bilinear interpolation
    return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

// 多层叠加噪声 - 增加胶片颗粒的自然复杂度
float fbm_grain(vec2 p, float seed) {
    float value = 0.0;
    float amplitude = 0.5;
    vec2 p_shifted = p + seed * 100.0;
    
    // 3 octaves of noise for natural film grain complexity
    for (int i = 0; i < 3; i++) {
        value += amplitude * value_noise(p_shifted);
        p_shifted *= 2.0;
        amplitude *= 0.5;
    }
    return value;
}

vec3 apply_film_grain(vec3 color, vec2 uv, float intensity, float size, bool monochrome, float shadows, float midtones, float highlights, float seed, float aspect_ratio, vec2 tex_size) {
    if (intensity <= 0.0) return color;

    // 1. 尺度补偿 (以 2048px 为基准准星，保证不同分辨率下颗粒物理面积的视觉占比一致)
    // 重要：tex_size 必须是原始图像的全分辨率，用于正确的尺度计算
    float scale_factor = max(1.0, tex_size.x / 2048.0);
    float physical_size = max(size * scale_factor, 1.0);  // 确保最小为 1.0

    // 2. 坐标系映射到真实的绝对物理像素
    // 将视口局部 UV 映射回原图的绝对物理像素坐标
    // 这样噪点种子绑定到图像空间，平移/缩放时颗粒"焊死"在图像纹理上
    vec2 grain_uv;
    if (u_visible_rect.z > 0.0 && u_visible_rect.w > 0.0) {
        // u_visible_rect 已经是原始图像坐标空间的像素值
        vec2 abs_pixel = u_visible_rect.xy + uv * u_visible_rect.zw;
        grain_uv = abs_pixel / physical_size;
    } else {
        // 回退：使用纹理尺寸作为物理像素坐标
        grain_uv = (uv * tex_size) / physical_size;
    }
    
    // 3. 生成有机银盐颗粒 (支持单色和彩色胶片特性)
    vec3 noise;
    if (monochrome) {
        // 单色胶片：所有通道共享相同的颗粒图案
        float n = fbm_grain(grain_uv, seed);
        noise = vec3(n);
    } else {
        // 彩色胶片：模拟各感光层的颗粒错位 (红、绿、蓝三层乳剂略有偏移)
        noise.r = fbm_grain(grain_uv, seed);
        noise.g = fbm_grain(grain_uv + vec2(12.3, 45.6), seed);
        noise.b = fbm_grain(grain_uv + vec2(78.9, 12.3), seed);
    }
    
    // 4. 提取原图亮度计算响应蒙版
    // 真实胶片颗粒在不同亮度区域的响应不同
    float luma = dot(color, vec3(0.299, 0.587, 0.114));
    float mask = luma < 0.5 
        ? mix(shadows, midtones, smoothstep(0.0, 0.5, luma)) 
        : mix(midtones, highlights, smoothstep(0.5, 1.0, luma));
    
    // 5. 光学柔光混合 (Soft Light Blending) 而不是死板的加法
    // 保持画面的反差与通透度，暗部不发灰，亮部不曝光过度
    vec3 grain_offset = (noise - 0.5) * intensity * mask;
    
    // 对比度保护算法：根据原始亮度调整颗粒影响强度
    // 中间调受影响最大，暗部和亮部受影响较小
    float contrast_protection = 1.0 - abs(luma - 0.5) * 1.5;
    contrast_protection = max(contrast_protection, 0.2); // 保证最小影响量
    
    vec3 result = color + grain_offset * contrast_protection;
    
    // 保留负值空间，允许暗部细节恢复
    return result;
}
"""

SOFT_CLIP_SNIPPET = """
// Filmic Soft Clip (Hybrid: Per-channel Toe, Ratio-preserving Shoulder)
vec3 apply_soft_clip(vec3 color, float high, float white_point, float shoulder_strength, float low, float black_point, float toe_strength, float exposure, float contrast) {
    vec3 res = color;
    
    // 1. Exposure and Contrast Pre-adjustments
    res *= pow(2.0, exposure);
    if (contrast != 0.0) {
        float factor = (contrast >= 0.0) ? (1.0 + contrast) : (1.0 / (1.0 + abs(contrast)));
        // 修复对比度炸弹：使用 sign 和 abs，让 S 曲线完美延展到负数空间，不裁切任何信息
        res = sign(res) * pow(abs(res) / 0.18, vec3(factor)) * 0.18;
    }

    // --- 核心进化 1：暗部 (Toe) 逐通道压缩 ---
    // 强行把所有负数值"拽"回黑场以上，再深的颜色也能被挤压出来
    for (int i = 0; i < 3; i++) {
        float c = res[i];
        if (c < low && black_point < low) {
            float T = low;
            float B = black_point;
            float k = max(toe_strength, 0.01);
            float diff = T - c;
            float range = T - B;
            res[i] = B + range * pow(1.0 + k * diff / range, -1.0 / k);
        } else if (c < black_point) {
            res[i] = black_point;
        }
    }
    
    // === 安全修复：防止暗部 RGB 通道失衡导致色块 ===
    // Toe Compression 对各通道独立处理，可能导致 RGB 比例失衡
    // 检查并校正暗部区域的通道平衡
    float toe_min = min(min(res.r, res.g), res.b);
    float toe_max = max(max(res.r, res.g), res.b);
    // 如果暗部 RGB 差异过大（超过 0.2），且最小通道接近黑场，进行平衡校正
    if (toe_max - toe_min > 0.2 && toe_min < low) {
        // 计算各通道的差异比例
        vec3 toe_diff = res - vec3(toe_min);
        // 对差异较大的通道进行软性压缩，保持 RGB 相对比例
        float balance_factor = clamp((toe_max - toe_min) * 0.25, 0.0, 0.15);
        // 使用 luma 作为基准，向灰度方向平衡（防止色偏）
        float toe_luma = dot(res, vec3(0.299, 0.587, 0.114));
        res = mix(res, vec3(toe_luma), balance_factor * abs(toe_min));
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
