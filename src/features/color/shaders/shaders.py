# Basic shaders for image rendering

# Rotation preview vertex shader for crop tool
# Implements physical space rotation with viewport aspect ratio correction
# Key insight: NDC is square [-1,1] but viewport is rectangular, so we must
# transform to physical space before rotating to avoid distortion
ROTATION_PREVIEW_VERTEX = """
#version 330
uniform float u_rotation_angle;
uniform float u_viewport_aspect; // Viewport Width / Height - critical for correct rotation

in vec2 in_vert;
in vec2 in_texcoord;
out vec2 v_texcoord;

void main() {
    vec2 pos = in_vert;
    
    if (abs(u_rotation_angle) > 0.001) {
        // Step 1: Transform NDC coordinates to physical space
        // NDC is square, but we need uniform physical coordinates
        pos.x *= u_viewport_aspect;
        
        // Step 2: Perform rotation in physical space
        float radians = -u_rotation_angle * 3.14159265359 / 180.0;
        float cos_a = cos(radians);
        float sin_a = sin(radians);
        mat2 rot_mat = mat2(cos_a, -sin_a, sin_a, cos_a);
        pos = rot_mat * pos;
        
        // Step 3: Transform back to NDC space
        pos.x /= u_viewport_aspect;
    }
    
    gl_Position = vec4(pos, 0.0, 1.0);
    v_texcoord = in_texcoord;
}
"""

PASSTHROUGH_VERTEX = """
#version 330
in vec2 in_vert;
in vec2 in_texcoord;
out vec2 v_texcoord;
void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_texcoord = in_texcoord;
}
"""

PASSTHROUGH_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
in vec2 v_texcoord;
out vec4 f_color;
void main() {
    f_color = texture(u_texture, v_texcoord);
}
"""

ROTATION_PREVIEW_FRAGMENT = """
#version 330
uniform sampler2D u_texture;
in vec2 v_texcoord;
out vec4 f_color;
void main() {
    // Check if UV is outside texture bounds (rotation reveals background)
    if (v_texcoord.x < 0.0 || v_texcoord.x > 1.0 || v_texcoord.y < 0.0 || v_texcoord.y > 1.0) {
        // 输出 50% 中灰，与画布背景无缝融合
        f_color = vec4(0.5, 0.5, 0.5, 1.0);
    } else {
        f_color = texture(u_texture, v_texcoord);
    }
}
"""

SPLIT_VIEW_FRAGMENT = """
#version 330
uniform sampler2D u_tex_current;
uniform sampler2D u_tex_original;
uniform float u_split_pos;
uniform vec4 u_uv_rect; // [offset_x, offset_y, scale_x, scale_y]
// F4.3: 对比模式（0=split wipe, 1=difference 全屏 |current-reference|）
uniform int u_compare_mode;
in vec2 v_texcoord;
out vec4 f_color;
void main() {
    vec4 current = texture(u_tex_current, v_texcoord);
    
    // Map viewport texture coordinates to original image coordinates
    vec2 original_uv = u_uv_rect.xy + v_texcoord * u_uv_rect.zw;
    
    // Check if original_uv is within [0, 1] range to handle viewport background
    vec4 original;
    if (original_uv.x < 0.0 || original_uv.x > 1.0 || original_uv.y < 0.0 || original_uv.y > 1.0) {
        original = vec4(0.15, 0.15, 0.18, 1.0); // Match viewport background
    } else {
        original = texture(u_tex_original, original_uv);
    }
    
    // F4.3: difference 模式——全屏 |current - reference|（同域比较：
    // 参考与当前同为显示域 float，差的绝对值即像素差；黑=一致）
    if (u_compare_mode == 1) {
        f_color = vec4(abs(current.rgb - original.rgb), 1.0);
        return;
    }
    
    // Split logic: Original on left, Current on right
    if (v_texcoord.x < u_split_pos) {
        f_color = original;
    } else {
        f_color = current;
    }
    
    // Draw a thin white line at the split position
    float line_width = 0.0015;
    if (abs(v_texcoord.x - u_split_pos) < line_width) {
        f_color = vec4(1.0, 1.0, 1.0, 1.0);
    }
}
"""

# ── LUT3D Shader for LutNode ──
LUT3D_FRAGMENT = """
#version 330
uniform sampler2D u_input_tex;
uniform sampler3D u_lut_tex;
uniform float u_intensity;
uniform float u_input_min;
uniform float u_input_max;
uniform float u_output_min;
uniform float u_output_max;
uniform float u_mix;
uniform vec2 u_tex_size;

in vec2 v_texcoord;
out vec4 f_color;

// HDR-safe LUT application
// Values within [0,1] are transformed by the LUT
// Values outside are linearly extrapolated to preserve HDR data
vec3 apply_lut_hdr_safe(vec3 color, sampler3D lut_tex, float intensity) {
    // SDR part: apply LUT with trilinear interpolation
    vec3 sdr_part = clamp(color, 0.0, 1.0);
    // LUT 网格点 i 对应输入 i/(n-1)，而 GL texel i 中心位于 (i+0.5)/n：
    // 先映射到 texel 中心坐标，消除边缘半 texel 拉伸。
    float lut_n = float(textureSize(lut_tex, 0).x);
    vec3 lut_result = texture(lut_tex, (sdr_part * (lut_n - 1.0) + 0.5) / lut_n).rgb;
    vec3 blended_sdr = mix(sdr_part, lut_result, intensity);

    // Preserve HDR highlights: scale proportionally
    float max_orig = max(max(color.r, color.g), color.b);
    if (max_orig > 1.0) {
        float max_sdr = max(max(sdr_part.r, sdr_part.g), sdr_part.b);
        if (max_sdr > 1e-5) {
            blended_sdr *= (max_orig / max_sdr);
        }
    }

    // Preserve negative values (shadow detail)
    vec3 negative_excess = min(color, vec3(0.0));
    return blended_sdr + negative_excess;
}

void main() {
    vec4 tex_color = texture(u_input_tex, v_texcoord);
    vec3 color = tex_color.rgb;

    // Input range mapping
    // Map [input_min, input_max] to [0, 1] for LUT sampling
    if (u_input_min != 0.0 || u_input_max != 1.0) {
        float range = u_input_max - u_input_min;
        if (range > 1e-5) {
            color = (color - u_input_min) / range;
        }
    }

    // Apply LUT
    vec3 lut_result = apply_lut_hdr_safe(color, u_lut_tex, u_intensity);

    // Output range mapping
    // Map [0, 1] to [output_min, output_max]
    if (u_output_min != 0.0 || u_output_max != 1.0) {
        lut_result = lut_result * (u_output_max - u_output_min) + u_output_min;
    }

    // Mix control: blend between original and LUT result
    if (u_mix < 1.0) {
        vec3 original = tex_color.rgb;
        lut_result = mix(original, lut_result, u_mix);
    }

    f_color = vec4(lut_result, tex_color.a);
}
"""

RAW_VIEW_FRAGMENT = """
        #version 330
        uniform sampler2D u_texture;
        uniform sampler3D u_lut_tex;
        uniform bool u_lut_enabled;
        uniform float u_raw_prev_exposure;
        uniform vec3 u_raw_prev_gains;
        uniform float u_raw_lift;
        uniform float u_raw_gain;
        uniform float u_raw_contrast;
        uniform float u_raw_saturation;
        uniform float u_raw_shadows;
        uniform float u_raw_highlights;
        uniform float u_raw_color_boost;
        uniform float u_raw_mid_detail;
        uniform float u_raw_sharpness;
        uniform int u_raw_color_space;
        uniform int u_raw_gamma;
        uniform bool u_is_srgb_source;
        uniform bool u_bgr_swizzle;  // P3-1: GPU端 BGR->RGB swizzle (避免CPU memcpy)

        in vec2 v_texcoord;
        out vec4 f_color;

        // Convert RGB to HSV for Vibrance/Saturation
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

        // Helper to apply common linear transforms to any pixel
        vec3 get_linear_color(vec3 c) {
            c *= pow(2.0, u_raw_prev_exposure);
            c *= u_raw_prev_gains;
            c = c * u_raw_gain + u_raw_lift;
            if (u_raw_contrast != 1.0) {
                c = (c - 0.18) * u_raw_contrast + 0.18;
            }
            return c;
        }

        vec3 linear_to_srgb(vec3 c) {
            return mix(12.92 * c, 1.055 * pow(max(c, vec3(0.0)), vec3(1.0/2.4)) - 0.055, step(0.0031308, c));
        }

        vec3 linear_to_rec709(vec3 c) {
            return mix(4.5 * c, 1.099 * pow(max(c, vec3(0.0)), vec3(0.45)) - 0.099, step(0.018, c));
        }

        vec3 linear_to_slog3(vec3 c) {
            // Sony 官方标准常数，向量化无分支运算
            vec3 log_part = (420.0 + 261.5 * log((max(c, 0.0) + 0.01) / 0.19) / log(10.0)) / 1023.0;
            vec3 lin_part = (c * 6774.248 + 95.0) / 1023.0;
            // 如果 c < 0.01125，使用 lin_part；否则使用 log_part
            return mix(lin_part, log_part, step(0.01125, c));
        }

        vec3 linear_to_logc3(vec3 c) {
            float cut = 0.010591;
            float a = 5.555556;
            float b = 0.052272;
            float c_val = 0.247190;
            float d = 0.385537;
            float e = 5.367655;
            float f = 0.092809;
            
            // max() 防止负数导致的对数域爆炸 (NaN)
            vec3 log_part = c_val * log(max(a * c + vec3(b), vec3(1e-10))) / log(10.0) + d;
            vec3 lin_part = e * c + f;
            return mix(lin_part, log_part, step(cut, c));
        }

        vec3 linear_to_acescct(vec3 c) {
            float X_BRK = 0.0078125;
            float A = 10.5402377416545;
            float B = 0.0729055341958355;
            vec3 log_part = (log2(max(c, vec3(1e-10))) + 9.72) / 17.52;
            vec3 lin_part = A * c + B;
            return mix(lin_part, log_part, step(X_BRK, c));
        }

        vec3 linear_to_davinci_int(vec3 c) {
            float A = 0.00262409;
            float B = 0.00567202;
            float C = 7.5671; // Fixed for continuity and 0.18 -> 0.336 mapping
            float M = 15.2913;
            vec3 log_part = (log2(max(c, 0.0) + B) + C) / M;
            vec3 lin_part = c * 16.291741;
            return mix(lin_part, log_part, step(A, c));
        }

        vec3 linear_to_redlog3g10(vec3 c) {
            float a = 0.224282;
            float b = 155.9753;
            float c_val = 1.0;
            float d = 0.0;
            // Standard RED Log3G10: y = a * log10(b * x + c) + d
            // Where 0.18 -> 1/3
            return a * (log(max(c * b + c_val, 1e-6)) / log(10.0)) + d;
        }

        vec3 linear_to_clog2(vec3 c) {
            return mix(10.125 * c + 0.0630735, (log(max(c, 1e-5)) / log(10.0) + 3.3244) / 7.3704, step(0.001923, c));
        }

        vec3 linear_to_clog3(vec3 c) {
            return mix(10.125 * c + 0.0630735, (log(max(c, 1e-5)) / log(10.0) + 2.8561) / 5.0272, step(0.012484, c));
        }

        vec3 linear_to_flog(vec3 c) {
            float a = 0.555556;
            float b = 0.009468;
            float c_val = 0.344676; // Official Fuji Constants
            float d = 0.790453;
            float e = 8.799461;
            float f = 0.092864;
            return mix(e * c + f, c_val * (log(max(a * c + b, 1e-5)) / log(10.0)) + d, step(0.00089, c));
        }

        vec3 linear_to_flog2(vec3 c) {
            float a = 0.754958;
            float b = 0.005120;
            float c_val = 0.201399;
            float d = 0.443315;
            float e = 6.273033;
            float f = 0.106821;
            float cut = 0.00089;
            
            // Official Fujifilm F-Log2 formula
            vec3 log_part = c_val * (log(max(a * c + b, 1e-9)) / log(10.0)) + d;
            vec3 lin_part = e * c + f;
            return mix(lin_part, log_part, step(cut, c));
        }

        vec3 linear_to_applelog(vec3 c) {
            // Apple Log is designed to be compatible with ARRI LogC3
            float a = 5.555556;
            float b = 0.052272;
            float c_val = 0.247190;
            float d = 0.385537;
            float e = 5.367655;
            float f = 0.092809;
            float cut = 0.010591;
            vec3 log_part = c_val * log(max(a * c + vec3(b), vec3(1e-10))) / log(10.0) + d;
            vec3 lin_part = e * c + f;
            return mix(lin_part, log_part, step(cut, c));
        }

        vec3 linear_to_hlg(vec3 c) {
            float a = 0.17883277;
            float b = 0.28466892;
            float c_val = 0.55991073;
            return mix(sqrt(3.0 * c), a * log(max(12.0 * c - b, 1e-6)) + c_val, step(1.0/12.0, c));
        }

        vec3 linear_to_vlog(vec3 c) {
            float cut = 0.01;
            float a = 5.6;
            float b = 0.125;
            float c_val = 0.241514;
            float d = 0.00876;
            float e = 0.598206;
            // 下限 1e-6 而非 0.0：c <= -d 时 log(0) = -inf，mix 按
            // x*(1-a)+y*a 展开会得到 0 * inf = NaN 污染整帧（与其余
            // transfer 函数的防护下限对齐）
            return mix(a * c + b, c_val * (log(max(c + d, 1e-6)) / log(10.0)) + e, step(cut, c));
        }

        vec3 apply_cmyk_simulation(vec3 rgb) {
            // Simplified RGB -> CMYK -> RGB gamut mapping
            float k = 1.0 - max(max(rgb.r, rgb.g), rgb.b);
            float c = (1.0 - rgb.r - k) / (1.0 - k + 1e-6);
            float m = (1.0 - rgb.g - k) / (1.0 - k + 1e-6);
            float y = (1.0 - rgb.b - k) / (1.0 - k + 1e-6);
            
            // Apply desaturation and shift common in printing (CMYK is duller)
            c = pow(clamp(c, 0.0, 1.0), 1.1);
            m = pow(clamp(m, 0.0, 1.0), 1.1);
            y = pow(clamp(y, 0.0, 1.0), 1.1);
            k = pow(clamp(k, 0.0, 1.0), 1.05);
            
            return vec3(
                (1.0 - c) * (1.0 - k),
                (1.0 - m) * (1.0 - k),
                (1.0 - y) * (1.0 - k)
            );
        }

        vec3 srgb_to_linear(vec3 c) {
            return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c));
        }

        // Linear sRGB to Linear ProPhoto (D65 -> D50)
        // GLSL mat3 is column-major, so values are arranged in columns
        const mat3 SRGB_TO_PROPHOTO = mat3(
            0.5292930, 0.0983601, 0.0168761,  // Column 0
            0.3301161, 0.8734699, 0.1176587,  // Column 1
            0.1405909, 0.0281700, 0.8654651   // Column 2
        );

        // AUD-24701: 统一的采样预处理（BGR swizzle → sRGB 去马 → ProPhoto）。
        // 中心像素与锐化/中间细节的邻域采样必须处于同一色彩空间，
        // 否则 Laplacian 会混合线性域中心值与 gamma 域邻域值（或 R/B
        // 颠倒的邻域），均匀区域出现整体压暗/彩边。
        vec3 prepare_color_space(vec3 c) {
            if (u_bgr_swizzle) {
                c = c.bgr;
            }
            if (u_is_srgb_source) {
                c = SRGB_TO_PROPHOTO * srgb_to_linear(c);
            }
            return c;
        }

        // 邻域采样 + 预处理：与中心像素走完全相同的色彩空间路径
        vec3 fetch_prepared(vec2 uv) {
            return prepare_color_space(texture(u_texture, uv).rgb);
        }

        void main() {
            vec4 tex_color = texture(u_texture, v_texcoord);

            // P3-1 + AUD-24701: BGR swizzle 与色彩空间转换统一在
            // prepare_color_space 中完成，中心/邻域共用同一入口
            vec3 color = prepare_color_space(tex_color.rgb);

            // 1 & 2. Exposure, White Balance, Lift, Gain, Contrast
            color = get_linear_color(color);
            
            // 3. Sharpness & Mid Detail (Applied in Linear/Contrast Space)
            if (u_raw_sharpness > 0.0 || abs(u_raw_mid_detail) > 0.0) {
                vec2 off = 1.0 / textureSize(u_texture, 0);
                
                // Sharpness - Radius 1 Laplacian
                if (u_raw_sharpness > 0.0) {
                    vec3 l = get_linear_color(fetch_prepared(v_texcoord - vec2(off.x, 0.0)));
                    vec3 r = get_linear_color(fetch_prepared(v_texcoord + vec2(off.x, 0.0)));
                    vec3 u = get_linear_color(fetch_prepared(v_texcoord - vec2(0.0, off.y)));
                    vec3 d = get_linear_color(fetch_prepared(v_texcoord + vec2(0.0, off.y)));
                    vec3 laplacian = color * 4.0 - (l + r + u + d);
                    color += laplacian * (u_raw_sharpness / 100.0); 
                }
                
                // Mid Detail - Radius 2 Laplacian
                if (abs(u_raw_mid_detail) > 0.0) {
                    float radius = 2.0;
                    vec3 l = get_linear_color(fetch_prepared(v_texcoord - vec2(off.x * radius, 0.0)));
                    vec3 r = get_linear_color(fetch_prepared(v_texcoord + vec2(off.x * radius, 0.0)));
                    vec3 u = get_linear_color(fetch_prepared(v_texcoord - vec2(0.0, off.y * radius)));
                    vec3 d = get_linear_color(fetch_prepared(v_texcoord + vec2(0.0, off.y * radius)));
                    vec3 mid_laplacian = color * 4.0 - (l + r + u + d);
                    color += mid_laplacian * (u_raw_mid_detail / 100.0);
                }
            }
            
            // 4. Shadows - Power Curve (Natural Lifting)
            if (u_raw_shadows != 0.0) {
                float shadow_factor = 1.0 + (u_raw_shadows / 100.0);
                if (shadow_factor > 0.0) {
                    vec3 clamped_c = clamp(color, 0.0, 1.0);
                    color = pow(clamped_c, vec3(1.0 / shadow_factor)) + (color - clamped_c);
                }
            }

            // 5. Highlights - Reinhard Tone Mapping (Soft Compression)
            if (u_raw_highlights != 0.0) {
                float max_c = max(max(color.r, color.g), color.b);
                if (max_c > 0.1) {
                    float mapped_max = max_c;
                    if (u_raw_highlights < 0.0) {
                        float k = abs(u_raw_highlights / 100.0) * 2.0;
                        mapped_max = 0.1 + (max_c - 0.1) / (1.0 + k * (max_c - 0.1));
                    } else {
                        float k = (u_raw_highlights / 100.0) * 0.5;
                        mapped_max = 0.1 + (max_c - 0.1) * (1.0 + k);
                    }
                    color *= (mapped_max / max_c);
                }
            }
            
            // 6. Saturation & Vibrance (Color Boost) — HDR-safe
            // AUD-24700: 禁止 clamp(0.0, 1.0) 后做 HSV 往返——那会永久截断
            // >1 的高光并抹掉负值。rgb2hsv 的 V 通道就是 max 分量，hsv2rgb
            // 按 V 等比恢复，往返对任意幅度的非负颜色是恒等变换，高光按
            // 原幅度保留；负值（lift 等线性操作可产生）HSV 无法表示，
            // 分离出来调整后原样加回。
            if (u_raw_saturation != 1.0 || u_raw_color_boost != 0.0) {
                vec3 negative_excess = min(color, vec3(0.0));
                vec3 hsv = rgb2hsv(color - negative_excess);
                if (u_raw_saturation != 1.0) {
                    hsv.y *= u_raw_saturation;
                }
                if (u_raw_color_boost != 0.0) {
                    float v_mask = 1.0 - hsv.y;
                    hsv.y += hsv.y * v_mask * (u_raw_color_boost / 100.0);
                }
                color = hsv2rgb(hsv) + negative_excess;
            }

            // 7. Color Space Conversion (Linear ProPhoto D50 to Target)
            // Matrices are pre-calculated using Bradford Chromatic Adaptation (D50 -> Target WP)
            // Note: GLSL mat3 is Column-Major, so we transpose standard row-major matrices.
            
            if (u_raw_color_space == 0 || u_raw_color_space == 1) {
                // Target: sRGB / Rec.709 (Linear D65)
                mat3 prophoto_to_srgb = mat3(
                     2.0342892, -0.2288020, -0.0085623,
                    -0.7275078,  1.2317255, -0.1532653,
                    -0.3067813, -0.0029236,  1.1618276
                );
                color = prophoto_to_srgb * color;
            }
            else if (u_raw_color_space == 2) {
                // Target: Rec.2020 (Linear D65)
                mat3 prophoto_to_2020 = mat3(
                     1.2006095, -0.0699261,  0.0055390,
                    -0.0574933,  1.0806111, -0.0407803,
                    -0.1431162, -0.0106850,  1.0352414
                );
                color = prophoto_to_2020 * color;
            }
            else if (u_raw_color_space == 3) {
                // Target: Adobe RGB (Linear D65)
                mat3 prophoto_to_adobe = mat3(
                     1.3895924, -0.2288020, -0.0176278,
                    -0.1693724,  1.2317255, -0.0962564,
                    -0.2202200, -0.0029236,  1.1138841
                );
                color = prophoto_to_adobe * color;
            }
            else if (u_raw_color_space == 4 || u_raw_color_space == 5) {
                // Target: DCI-P3 / Display P3 (Linear D65)
                mat3 prophoto_to_p3 = mat3(
                     1.6325044, -0.1536805,  0.0103902,
                    -0.3796694,  1.1666904, -0.0628051,
                    -0.2528350, -0.0130099,  1.0524149
                );
                color = prophoto_to_p3 * color;
            }
            else if (u_raw_color_space == 6) {
                // Target: ACEScg (Linear, AP1 Primaries, ~D60)
                mat3 prophoto_to_acescg = mat3(
                     1.1691282, -0.0669844,  0.0094207,
                    -0.0350954,  1.0755682, -0.0133505,
                    -0.1340327, -0.0085838,  1.0039298
                );
                color = prophoto_to_acescg * color;
            }
            else if (u_raw_color_space == 7) {
                // Target: ARRI Wide Gamut 3 (Linear D65)
                mat3 prophoto_to_arri = mat3(
                     1.2214917, -0.1080029, -0.0058478,
                    -0.1407401,  0.9239401,  0.0428319,
                    -0.0807516,  0.1840628,  0.9630159
                );
                color = prophoto_to_arri * color;
            }
            else if (u_raw_color_space == 8) {
                // Target: Sony S-Gamut3 (Linear D65)
                mat3 prophoto_to_sony = mat3(
                     1.0723019, -0.0273251,  0.0131766,
                    -0.0035910,  0.9092234, -0.0156734,
                    -0.0687108,  0.1181018,  1.0024968
                );
                color = prophoto_to_sony * color;
            }
            else if (u_raw_color_space == 9) {
                // Target: Panasonic V-Gamut (Linear D65)
                mat3 prophoto_to_vgamut = mat3(
                     1.1158480, -0.0285319,  0.0128478,
                    -0.0424541,  0.9367819, -0.0081652,
                    -0.0733939,  0.0917501,  0.9953173
                );
                color = prophoto_to_vgamut * color;
            }
            else if (u_raw_color_space == 10) {
                // Target: DaVinci Wide Gamut (Linear D65)
                mat3 prophoto_to_dwg = mat3(
                     1.1763040, -0.0963309,  0.0095457,
                    -0.0463205,  1.1346271, -0.0120490,
                    -0.1299835, -0.0382962,  1.0025033
                );
                color = prophoto_to_dwg * color;
            }
            else if (u_raw_color_space == 11) {
                // Target: REDWideGamutRGB (Linear D65)
                mat3 prophoto_to_rwg = mat3(
                     1.0192343, -0.0204216,  0.0514708,
                     0.0343281,  0.8661532,  0.1917153,
                    -0.0535624,  0.1542684,  0.7568139
                );
                color = prophoto_to_rwg * color;
            }
            else if (u_raw_color_space == 12) {
                // Target: Canon Cinema Gamut (Linear D65)
                mat3 prophoto_to_canon = mat3(
                     1.0909034, -0.0387532,  0.0128448,
                    -0.0257664,  1.1701934, -0.0080968,
                    -0.0651369, -0.1314402,  0.9952519
                );
                color = prophoto_to_canon * color;
            }
            else if (u_raw_color_space == 13) {
                // Target: Fujifilm F-Gamut (Linear D65)
                // AUD-24702: 此前九个系数是 ProPhoto->BT.2020 矩阵的逐字节
                // 复制（两个不同色域产生 7 位小数全同的矩阵在数学上不可能）。
                // 按 Fujifilm F-Gamut 公布基色（R 0.7347/0.2653, G 0.1424/
                // 0.8576, B 0.0991/-0.0040, D65）经 Bradford CAT
                // (ProPhoto D50 -> D65) 重新生成，方法与本文件已验证的
                // sRGB/Rec.2020 矩阵一致（列主序书写）。
                mat3 prophoto_to_fgamut = mat3(
                     1.0696275, -0.0061746,  0.0035957,
                    -0.0069129,  0.9562600, -0.0118759,
                    -0.0627145,  0.0499146,  1.0082802
                );
                color = prophoto_to_fgamut * color;
            }
            else if (u_raw_color_space == 14) {
                // Target: BMD Film Gen5 (Linear D65)
                mat3 prophoto_to_bmd = mat3(
                     1.1216969,  0.0154464,  0.0216934,
                    -0.0787515,  0.8730074, -0.0586091,
                    -0.0429454,  0.1115462,  1.0369157
                );
                color = prophoto_to_bmd * color;
            }
            else if (u_raw_color_space == 15) {
                // Target: CMYK Simulation
                // First convert to Rec.709
                mat3 prophoto_to_srgb = mat3(
                     2.0342892, -0.2288020, -0.0085623,
                    -0.7275078,  1.2317255, -0.1532653,
                    -0.3067813, -0.0029236,  1.1618276
                );
                color = prophoto_to_srgb * color;
                color = apply_cmyk_simulation(clamp(color, 0.0, 1.0));
            }

            // 8. Gamma Encoding
            if (u_raw_gamma == 1) {
                color = linear_to_srgb(color);
            } else if (u_raw_gamma == 2) {
                color = linear_to_rec709(color);
            } else if (u_raw_gamma == 3) {
                color = pow(max(color, 0.0), vec3(1.0/2.2));
            } else if (u_raw_gamma == 4) {
                color = pow(max(color, 0.0), vec3(1.0/2.4));
            } else if (u_raw_gamma == 5) {
                color = pow(max(color, 0.0), vec3(1.0/2.6));
            } else if (u_raw_gamma == 6) {
                color = linear_to_hlg(color);
            } else if (u_raw_gamma == 7) {
                color = linear_to_slog3(color);
            } else if (u_raw_gamma == 8) {
                color = linear_to_logc3(color);
            } else if (u_raw_gamma == 9) {
                color = linear_to_acescct(color);
            } else if (u_raw_gamma == 10) {
                color = linear_to_vlog(color);
            } else if (u_raw_gamma == 11) {
                color = linear_to_davinci_int(color);
            } else if (u_raw_gamma == 12) {
                color = linear_to_redlog3g10(color);
            } else if (u_raw_gamma == 13) {
                color = linear_to_clog2(color);
            } else if (u_raw_gamma == 14) {
                color = linear_to_clog3(color);
            } else if (u_raw_gamma == 15) {
                color = linear_to_flog(color);
            } else if (u_raw_gamma == 16) {
                color = linear_to_flog2(color);
            } else if (u_raw_gamma == 17) {
                color = linear_to_applelog(color);
            }
            
            // 9. Input LUT (Applied before final output, after potential log/gamma encoding)
            // AUD-24703: LUT 纹理均以 LINEAR 过滤创建，网格点 i 的 texel
            // 中心位于 (i+0.5)/n；直接用 [0,1] 坐标采样会让 LUT 两端各
            // 拉伸半 texel（33 点 LUT 端点偏移约 1.5%）。与本文件
            // apply_lut_hdr_safe（LUT3D）已修复的映射保持一致。
            if (u_lut_enabled) {
                float lut_n = float(textureSize(u_lut_tex, 0).x);
                vec3 lut_input = clamp(color, 0.0, 1.0);
                color = texture(u_lut_tex, (lut_input * (lut_n - 1.0) + 0.5) / lut_n).rgb;
            }
            
            f_color = vec4(color, tex_color.a);
        }
"""
