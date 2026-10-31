// ================================================================
// video_color_decode.glsl - HDR Video Color Decode Shader Library
// ================================================================
//
// 提供 HDR 视频色彩解码完整管线，包括：
// - PQ EOTF (SMPTE ST 2084) 解码
// - HLG OETF^-1 (ARIB STD-B67) 解码
// - HDR → SDR Tone Mapping (Reinhard, ACES)
// - Scene-linear display transform
// - 支持 PQ/HLG/SDR 输入的完整 pipeline
//
// 技术规范：
// - PQ: SMPTE ST 2084, 峰值 10,000 nits
// - HLG: ARIB STD-B67 / ITU-R BT.2100
// - Tone Mapping: Reinhard, ACES Filmic
//
// 注意：所有数字必须带 .0 后缀（GLSL 严格要求）
//       for 循环边界必须是常量
//
// 参考：
// - SMPTE ST 2084: https://ieeexplore.ieee.org/document/7291452
// - ARIB STD-B67: https://www.arib.or.jp/english/std_tr/
// - ACES: https://acescentral.com/
// ================================================================

#version 330

// ================================================================
// Part 1: Constants
// ================================================================

// ---------------------------------------------------------------
// PQ (SMPTE ST 2084) Constants
// ---------------------------------------------------------------
// 峰值亮度: 10,000 nits
// PQ EOTF 公式中的常量

const float PQ_M1 = 2610.0 / 16384.0;     // 0.1593017578125
const float PQ_M2 = 2523.0 / 4096.0 * 128.0;  // 78.84375
const float PQ_C1 = 3424.0 / 4096.0;      // 0.8359375 (corrected)
const float PQ_C2 = 2413.0 / 4096.0 * 32.0;   // 18.8515625
const float PQ_C3 = 2392.0 / 4096.0 * 32.0;   // 18.6875
const float PQ_LMAX = 10000.0;            // Peak luminance (nits)

// ---------------------------------------------------------------
// HLG (ARIB STD-B67) Constants
// ---------------------------------------------------------------
// Hybrid Log-Gamma OETF^-1 参数

const float HLG_A = 0.17883277;
const float HLG_B = 0.28466892;
const float HLG_C = 0.55991073;

// HLG OETF 分段点
const float HLG_THRESHOLD = 0.5;          // 分段点
const float HLG_INV_A = 1.0 / HLG_A;      // 1/a 预计算
const float HLG_SQRT3 = 1.73205080757;    // sqrt(3)

// ---------------------------------------------------------------
// sRGB / Display Gamma Constants
// ---------------------------------------------------------------

const float SRGB_THRESHOLD = 0.04045;
const float SRGB_LINEAR_SCALE = 12.92;
const float SRGB_GAMMA_SCALE = 1.055;
const float SRGB_GAMMA_OFFSET = 0.055;
const float SRGB_GAMMA = 2.4;
const float SRGB_GAMMA_INV = 1.0 / 2.4;

// ---------------------------------------------------------------
// Tone Mapping Constants
// ---------------------------------------------------------------

// Reinhard extended 参数
const float REINHARD_WHITE_POINT = 1.0;  // 默认白点

// ACES Filmic approximation 参数
const float ACES_A = 2.51;
const float ACES_B = 0.03;
const float ACES_C = 2.43;
const float ACES_D = 0.59;
const float ACES_E = 0.14;

// ================================================================
// Part 2: PQ EOTF (SMPTE ST 2084)
// ================================================================

// PQ to Linear (EOTF - Electro-Optical Transfer Function)
// 输入: PQ encoded value [0, 1]
// 输出: Linear light value (nits, up to 10,000)
// 
// PQ EOTF 公式:
// L = 10000 * (max(E'^(1/m2) - c1, 0) / (c2 - c3 * E'^(1/m2)))^(1/m1)
//
// 参考: SMPTE ST 2084-2014

float pq_eotf(float x) {
    // 安全 clamp
    x = max(x, 0.0);
    x = min(x, 1.0);
    
    // Step 1: E'^(1/m2)
    float v = pow(x, 1.0 / PQ_M2);
    
    // Step 2: 计算分子和分母
    float num = max(v - PQ_C1, 0.0);
    float den = max(PQ_C2 - PQ_C3 * v, 0.0001);
    
    // Step 3: ^(1/m1) 并缩放到 nits
    float linear = pow(num / den, 1.0 / PQ_M1);
    
    return linear * PQ_LMAX;
}

// PQ EOTF (vec3 版本)
vec3 pq_eotf(vec3 x) {
    return vec3(
        pq_eotf(x.r),
        pq_eotf(x.g),
        pq_eotf(x.b)
    );
}

// Linear to PQ (inverse EOTF - OETF)
// 输入: Linear light value (nits)
// 输出: PQ encoded value [0, 1]
float pq_oetf(float linear_nits) {
    // 归一化到 10,000 nits
    float normalized = linear_nits / PQ_LMAX;
    normalized = max(normalized, 0.0);
    normalized = min(normalized, 1.0);
    
    // Step 1: E^(m1)
    float v = pow(normalized, PQ_M1);
    
    // Step 2: PQ 公式
    float num = PQ_C1 + PQ_C2 * v;
    float den = 1.0 + PQ_C3 * v;
    
    // Step 3: ^(m2)
    return pow(num / den, PQ_M2);
}

// PQ OETF (vec3 版本)
vec3 pq_oetf(vec3 linear_nits) {
    return vec3(
        pq_oetf(linear_nits.r),
        pq_oetf(linear_nits.g),
        pq_oetf(linear_nits.b)
    );
}

// ================================================================
// Part 3: HLG OETF Inverse (ARIB STD-B67)
// ================================================================

// HLG OETF^-1 (inverse of OETF)
// 输入: HLG encoded signal E' [0, 1]
// 输出: Linear light value E (relative, scene-referred)
//
// HLG OETF 定义:
// - E' = sqrt(3 * E)                     for 0 <= E <= 1
// - E' = a * ln(E - b) + c               for E > 1
//
// HLG OETF^-1:
// - E = E'^2 / 3                          for 0 <= E' <= 0.5
// - E = exp((E' - c) / a) + b            for E' > 0.5

float hlg_oetf_inverse(float x) {
    // 安全 clamp
    x = max(x, 0.0);
    x = min(x, 1.0);
    
    // 分段函数
    if (x <= HLG_THRESHOLD) {
        // 低亮度区域: E = E'^2 / 3
        return (x * x) / 3.0;
    } else {
        // 高亮度区域: E = exp((E' - c) / a) + b
        return exp((x - HLG_C) / HLG_A) + HLG_B;
    }
}

// HLG OETF^-1 (vec3 版本)
vec3 hlg_oetf_inverse(vec3 x) {
    return vec3(
        hlg_oetf_inverse(x.r),
        hlg_oetf_inverse(x.g),
        hlg_oetf_inverse(x.b)
    );
}

// HLG OETF (forward)
// 输入: Linear light value E (relative, scene-referred)
// 输出: HLG encoded signal E' [0, 1]
float hlg_oetf(float linear) {
    linear = max(linear, 0.0);
    
    // 分段点: 1/3
    const float hlg_threshold_linear = 1.0 / 3.0;
    
    if (linear <= hlg_threshold_linear) {
        // 低亮度区域: E' = sqrt(3 * E)
        return sqrt(3.0 * linear);
    } else {
        // 高亮度区域: E' = a * ln(E - b) + c
        // 注意: 需要 E > b
        linear = max(linear, HLG_B + 0.0001);
        return HLG_A * log(linear - HLG_B) + HLG_C;
    }
}

// HLG OETF (vec3 版本)
vec3 hlg_oetf(vec3 linear) {
    return vec3(
        hlg_oetf(linear.r),
        hlg_oetf(linear.g),
        hlg_oetf(linear.b)
    );
}

// HLG OOTF (Opto-Optical Transfer Function)
// 将 scene-linear 转换为 display light
// 输入: scene-linear E (relative)
// 输出: display light L (relative)
// 
// HLG OOTF: L = α * E^γ
// 其中 γ 是 display gamma (通常 1.2), α 是系统增益
float hlg_ootf(float e, float gamma, float alpha) {
    return alpha * pow(max(e, 0.0), gamma);
}

// HLG inverse OOTF
float hlg_ootf_inverse(float l, float gamma, float alpha) {
    l = max(l, 0.0);
    alpha = max(alpha, 0.0001);
    return pow(l / alpha, 1.0 / gamma);
}

// ================================================================
// Part 4: HDR Tone Mapping
// ================================================================

// ---------------------------------------------------------------
// Reinhard Tone Mapping
// ---------------------------------------------------------------

// 简单 Reinhard tone mapping
// Ld = L / (1 + L)
// 输入: HDR linear value (can exceed 1.0)
// 输出: Tone mapped value [0, 1)
float reinhard_tonemap(float hdr_linear) {
    return hdr_linear / (1.0 + hdr_linear);
}

// Reinhard (vec3 版本)
vec3 reinhard_tonemap(vec3 hdr_linear) {
    return vec3(
        reinhard_tonemap(hdr_linear.r),
        reinhard_tonemap(hdr_linear.g),
        reinhard_tonemap(hdr_linear.b)
    );
}

// Reinhard Extended (带白点控制)
// Ld = L * (1 + L / L_white^2) / (1 + L)
// 输入: 
//   hdr_linear: HDR linear value
//   white_point: 白点参考值 (default 1.0 for normalized)
// 输出: Tone mapped value [0, 1)
float reinhard_extended_tonemap(float hdr_linear, float white_point) {
    float scale = white_point * white_point;
    return (hdr_linear * (1.0 + hdr_linear / scale)) / (1.0 + hdr_linear);
}

// Reinhard Extended (vec3 版本)
vec3 reinhard_extended_tonemap(vec3 hdr_linear, float white_point) {
    return vec3(
        reinhard_extended_tonemap(hdr_linear.r, white_point),
        reinhard_extended_tonemap(hdr_linear.g, white_point),
        reinhard_extended_tonemap(hdr_linear.b, white_point)
    );
}

// Reinhard with max_nits 参数
// 用于 HDR → SDR 转换
vec3 hdr_tonemap_reinhard(vec3 hdr_color, float max_nits) {
    // 相对亮度
    float L = max_nits / 10000.0;
    
    // Reinhard tone curve
    float Ld = L / (1.0 + L);
    
    // 应用 tone mapping
    return hdr_color * Ld / L;
}

// ---------------------------------------------------------------
// ACES Filmic Tone Mapping (Approximation)
// ---------------------------------------------------------------

// ACES Filmic tone mapping approximation
// 参考: https://knarkowicz.wordpress.com/2016/01/06/aces-filmic-tone-mapping-curve/
// 输入: HDR linear value
// 输出: Tone mapped value [0, 1)
//
// 公式:
// numerator = x * (a * x + b)
// denominator = x * (c * x + d) + e
// output = numerator / denominator

float aces_tonemap(float x) {
    x = max(x, 0.0);
    
    float num = x * (ACES_A * x + ACES_B);
    float den = x * (ACES_C * x + ACES_D) + ACES_E;
    
    return num / den;
}

// ACES (vec3 版本)
vec3 aces_tonemap(vec3 x) {
    return vec3(
        aces_tonemap(x.r),
        aces_tonemap(x.g),
        aces_tonemap(x.b)
    );
}

// ACES with exposure adjustment
vec3 aces_tonemap_exposure(vec3 x, float exposure) {
    // 先应用 exposure
    x = x * exposure;
    
    // ACES tone map
    return aces_tonemap(x);
}

// ACES approximation for HDR to SDR
vec3 hdr_tonemap_aces(vec3 hdr_color, float max_nits) {
    // 归一化
    float normalized_peak = max_nits / 10000.0;
    
    // ACES tone mapping
    vec3 mapped = aces_tonemap(hdr_color * normalized_peak);
    
    return mapped;
}

// ---------------------------------------------------------------
// Luminance-based Tone Mapping
// ---------------------------------------------------------------

// 计算亮度 (BT.709)
float luminance_bt709(vec3 rgb) {
    return 0.2126 * rgb.r + 0.7152 * rgb.g + 0.0722 * rgb.b;
}

// 计算亮度 (BT.2020)
float luminance_bt2020(vec3 rgb) {
    return 0.2627 * rgb.r + 0.6780 * rgb.g + 0.0593 * rgb.b;
}

// Luminance-preserving Reinhard
// 保持色度，只对亮度做 tone mapping
vec3 luminance_preserving_tonemap(vec3 hdr_color, float white_point) {
    float lum = luminance_bt709(hdr_color);
    lum = max(lum, 0.0001);
    
    float lum_mapped = reinhard_extended_tonemap(lum, white_point);
    
    return hdr_color * (lum_mapped / lum);
}

// ================================================================
// Part 5: Display Transform (Gamma)
// ================================================================

// Scene-linear to display gamma
// 输入: Scene-linear value [0, 1]
// 输出: Gamma encoded value
vec3 scene_linear_to_display(vec3 linear, float gamma) {
    return pow(max(linear, vec3(0.0)), vec3(1.0 / gamma));
}

// Display gamma to scene-linear (inverse)
vec3 display_to_scene_linear(vec3 gamma_encoded, float gamma) {
    return pow(max(gamma_encoded, vec3(0.0)), vec3(gamma));
}

// Linear to sRGB (EOTF^-1)
float linear_to_srgb(float linear) {
    linear = max(linear, 0.0);
    
    if (linear <= 0.0031308) {
        return linear * SRGB_LINEAR_SCALE;
    } else {
        return SRGB_GAMMA_SCALE * pow(linear, SRGB_GAMMA_INV) - SRGB_GAMMA_OFFSET;
    }
}

// sRGB to Linear (EOTF)
float srgb_to_linear(float srgb) {
    srgb = max(srgb, 0.0);
    srgb = min(srgb, 1.0);
    
    if (srgb <= SRGB_THRESHOLD) {
        return srgb / SRGB_LINEAR_SCALE;
    } else {
        return pow((srgb + SRGB_GAMMA_OFFSET) / SRGB_GAMMA_SCALE, SRGB_GAMMA);
    }
}

// Linear to sRGB (vec3 版本)
vec3 linear_to_srgb(vec3 linear) {
    return vec3(
        linear_to_srgb(linear.r),
        linear_to_srgb(linear.g),
        linear_to_srgb(linear.b)
    );
}

// sRGB to Linear (vec3 版本)
vec3 srgb_to_linear(vec3 srgb) {
    return vec3(
        srgb_to_linear(srgb.r),
        srgb_to_linear(srgb.g),
        srgb_to_linear(srgb.b)
    );
}

// Gamma 2.2 (简化版，用于显示器)
vec3 gamma_2_2_encode(vec3 linear) {
    return pow(max(linear, vec3(0.0)), vec3(1.0 / 2.2));
}

vec3 gamma_2_2_decode(vec3 gamma_encoded) {
    return pow(max(gamma_encoded, vec3(0.0)), vec3(2.2));
}

// Gamma 2.4 (BT.2020)
vec3 gamma_2_4_encode(vec3 linear) {
    return pow(max(linear, vec3(0.0)), vec3(1.0 / 2.4));
}

vec3 gamma_2_4_decode(vec3 gamma_encoded) {
    return pow(max(gamma_encoded, vec3(0.0)), vec3(2.4));
}

// ================================================================
// Part 6: Complete Processing Pipeline
// ================================================================

// PQ HDR to Display Pipeline
// 完整流程: PQ → Linear (nits) → Normalize → Tone Map → Display Gamma
// 
// 参数:
//   pq_rgb: PQ encoded RGB [0, 1]
//   target_peak_nits: 目标显示峰值亮度 (nits, e.g., 100.0 for SDR, 1000.0 for HDR)
//   tonemap_mode: 0=Reinhard, 1=Reinhard Extended, 2=ACES
//   display_gamma: 显示 gamma (2.2, 2.4, 或 0.0 for sRGB)
vec3 pq_hdr_to_display(vec3 pq_rgb, float target_peak_nits, int tonemap_mode, float display_gamma) {
    // Step 1: PQ to Linear (nits)
    vec3 linear_nits = pq_eotf(pq_rgb);
    
    // Step 2: 归一化到目标峰值
    vec3 normalized = linear_nits / target_peak_nits;
    
    // Step 3: Tone Mapping
    vec3 mapped;
    if (tonemap_mode == 0) {
        // Reinhard simple
        mapped = reinhard_tonemap(normalized);
    } else if (tonemap_mode == 1) {
        // Reinhard Extended
        mapped = reinhard_extended_tonemap(normalized, 1.0);
    } else {
        // ACES Filmic
        mapped = aces_tonemap(normalized);
    }
    
    // Step 4: Display Gamma
    vec3 display;
    if (display_gamma <= 0.0) {
        // sRGB
        display = linear_to_srgb(mapped);
    } else {
        // Custom gamma
        display = scene_linear_to_display(mapped, display_gamma);
    }
    
    return display;
}

// HLG HDR to Display Pipeline
// 完整流程: HLG → Linear (relative) → OOTF → Normalize → Tone Map → Display Gamma
//
// 参数:
//   hlg_rgb: HLG encoded RGB [0, 1]
//   display_peak_nits: 显示器峰值亮度 (nits, e.g., 100.0 for SDR, 1000.0 for HDR)
//   hlg_gamma: HLG system gamma (usually 1.2)
//   exposure_gain: 曝光增益
//   tonemap_mode: 0=Reinhard, 1=Reinhard Extended, 2=ACES
//   display_gamma: 显示 gamma
vec3 hlg_hdr_to_display(vec3 hlg_rgb, float display_peak_nits, float hlg_gamma, 
                        float exposure_gain, int tonemap_mode, float display_gamma) {
    // Step 1: HLG OETF^-1 to scene-linear
    vec3 scene_linear = hlg_oetf_inverse(hlg_rgb);
    
    // Step 2: Apply exposure gain
    scene_linear = scene_linear * exposure_gain;
    
    // Step 3: HLG OOTF (scene to display)
    // Note: HLG OOTF is implicitly applied in display
    // For simplicity, we skip explicit OOTF here
    
    // Step 4: Normalize to display peak
    // HLG reference white is 203 nits, peak is 1000 nits for 1000-nit display
    float reference_white = 203.0;
    vec3 normalized = scene_linear * (reference_white / display_peak_nits);
    
    // Step 5: Tone Mapping
    vec3 mapped;
    if (tonemap_mode == 0) {
        mapped = reinhard_tonemap(normalized);
    } else if (tonemap_mode == 1) {
        mapped = reinhard_extended_tonemap(normalized, 1.0);
    } else {
        mapped = aces_tonemap(normalized);
    }
    
    // Step 6: Display Gamma
    vec3 display;
    if (display_gamma <= 0.0) {
        display = linear_to_srgb(mapped);
    } else {
        display = scene_linear_to_display(mapped, display_gamma);
    }
    
    return display;
}

// SDR to Display Pipeline
// 简单流程: SDR (gamma encoded) → Linear → Display Gamma
//
// 参数:
//   sdr_rgb: SDR gamma encoded RGB [0, 1]
//   input_gamma: 输入 gamma (2.2, 2.4, 或 0.0 for sRGB)
//   display_gamma: 显示 gamma
vec3 sdr_to_display(vec3 sdr_rgb, float input_gamma, float display_gamma) {
    // Step 1: Input gamma decode to linear
    vec3 linear;
    if (input_gamma <= 0.0) {
        // sRGB input
        linear = srgb_to_linear(sdr_rgb);
    } else {
        // Custom gamma input
        linear = display_to_scene_linear(sdr_rgb, input_gamma);
    }
    
    // Step 2: Display gamma encode
    vec3 display;
    if (display_gamma <= 0.0) {
        // sRGB output
        display = linear_to_srgb(linear);
    } else {
        // Custom gamma output
        display = scene_linear_to_display(linear, display_gamma);
    }
    
    return display;
}

// ================================================================
// Part 7: Utility Functions
// ================================================================

// Safe clamp for HDR (preserve highlights above max_value)
// 将超过 max_value 的值软裁剪到 [0, max_value]
vec3 soft_clamp_hdr(vec3 rgb, float max_value) {
    // Soft clamp using Reinhard
    vec3 over = max(rgb - vec3(max_value), vec3(0.0));
    vec3 clamped = min(rgb, vec3(max_value));
    return clamped + over / (1.0 + over / max_value);
}

// Hard clamp for HDR
vec3 hard_clamp_hdr(vec3 rgb, float max_value) {
    return clamp(rgb, vec3(0.0), vec3(max_value));
}

// Normalize by peak luminance
vec3 normalize_by_peak(vec3 linear_nits, float peak_nits) {
    return linear_nits / peak_nits;
}

// Apply white balance
vec3 white_balance(vec3 rgb, vec3 white_point) {
    // white_point 应该是归一化的 RGB white (e.g., vec3(1.0, 1.0, 1.0))
    return rgb / max(white_point, vec3(0.0001));
}

// Saturation adjustment
vec3 adjust_saturation(vec3 rgb, float saturation) {
    float lum = luminance_bt709(rgb);
    return mix(vec3(lum), rgb, saturation);
}

// Contrast adjustment
vec3 adjust_contrast(vec3 rgb, float contrast) {
    return (rgb - vec3(0.5)) * contrast + vec3(0.5);
}

// ================================================================
// VERTEX SHADER - Fullscreen Quad
// ================================================================

// VERTEX SHADER

#version 330

in vec2 in_vert;      // Vertex position (-1 to 1)
in vec2 in_texcoord;  // Texture coordinate (0 to 1)

out vec2 v_texcoord;   // Pass to fragment shader

void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_texcoord = in_texcoord;
}

// ================================================================
// FRAGMENT SHADER - HDR Video Color Decode
// ================================================================

// FRAGMENT SHADER

#version 330

// 输入纹理
uniform sampler2D input_texture;  // 输入视频帧 (PQ/HLG/SDR encoded)

// 处理参数
uniform int transfer_function;    // 0=SDR, 1=PQ, 2=HLG
uniform int tonemap_mode;         // 0=Reinhard, 1=Reinhard Extended, 2=ACES
uniform float target_peak_nits;   // 目标显示峰值 (nits)
uniform float display_gamma;      // 显示 gamma (0.0 = sRGB)
uniform float exposure_gain;      // 曝光增益 (HLG 用)
uniform float hlg_gamma;          // HLG system gamma (usually 1.2)

// 输入
in vec2 v_texcoord;

// 输出
out vec4 f_color;

void main() {
    // 读取输入纹理
    vec3 input_color = texture(input_texture, v_texcoord).rgb;
    
    // 根据传输函数选择处理管线
    vec3 output_color;
    
    if (transfer_function == 0) {
        // SDR (gamma encoded)
        output_color = sdr_to_display(input_color, 0.0, display_gamma);
    } else if (transfer_function == 1) {
        // PQ HDR
        output_color = pq_hdr_to_display(input_color, target_peak_nits, 
                                         tonemap_mode, display_gamma);
    } else {
        // HLG HDR
        output_color = hlg_hdr_to_display(input_color, target_peak_nits, 
                                          hlg_gamma, exposure_gain, 
                                          tonemap_mode, display_gamma);
    }
    
    // 输出
    f_color = vec4(output_color, 1.0);
}

// ================================================================
// FRAGMENT SHADER - HDR to SDR Conversion (Optimized)
// ================================================================

// FRAGMENT SHADER HDR_TO_SDR

#version 330

// 输入纹理
uniform sampler2D hdr_texture;   // HDR 输入 (PQ or HLG)
uniform int hdr_format;           // 0=PQ, 1=HLG
uniform float target_peak;        // SDR 峰值 (usually 100.0)
uniform int tone_mode;            // 0=Reinhard, 1=ACES

in vec2 v_texcoord;
out vec4 f_color;

void main() {
    vec3 hdr_input = texture(hdr_texture, v_texcoord).rgb;
    
    vec3 sdr_output;
    if (hdr_format == 0) {
        // PQ → SDR
        sdr_output = pq_hdr_to_display(hdr_input, target_peak, tone_mode, 0.0);
    } else {
        // HLG → SDR (simplified)
        sdr_output = hlg_hdr_to_display(hdr_input, target_peak, 1.2, 1.0, tone_mode, 0.0);
    }
    
    // 输出 SDR (sRGB)
    f_color = vec4(sdr_output, 1.0);
}

// ================================================================
// FRAGMENT SHADER - YUV + HDR Decode Pipeline
// ================================================================

// FRAGMENT SHADER YUV_HDR

#version 330

// YUV 平面输入
uniform sampler2D y_plane;
uniform sampler2D u_plane;
uniform sampler2D v_plane;

// YUV 参数
uniform int yuv_matrix;           // 0=BT.709, 1=BT.2020, 2=BT.601
uniform int color_range;          // 0=limited, 1=full
uniform vec2 chroma_uv_scale;     // Chroma UV coordinate scale (default 1.0, 1.0 for independent textures)
uniform vec2 chroma_uv_offset;    // Chroma UV coordinate offset (default 0.0, 0.0)

// HDR 参数
uniform int transfer_function;    // 0=SDR, 1=PQ, 2=HLG
uniform int tonemap_mode;         // 0=Reinhard, 1=Reinhard Extended, 2=ACES
uniform float target_peak_nits;   // Target display peak
uniform float display_gamma;      // Display gamma

in vec2 v_texcoord;
out vec4 f_color;

// YUV → RGB 矩阵
const mat3 bt709 = mat3(
    1.0, 1.0, 1.0,
    0.0, -0.1873, 1.8556,
    1.5748, -0.4681, 0.0
);

const mat3 bt2020 = mat3(
    1.0, 1.0, 1.0,
    0.0, -0.1646, 1.8814,
    1.4746, -0.5714, 0.0
);

// BT.601 数字 YCbCr 系数（AUD-R2-010，AUD-39800 同族）：色度归一化为
// limited-range 数字 YCbCr（±0.5），标准系数由 Kr=0.299/Kb=0.114 推导：
// R:+1.402  G:-0.3441*U-0.7141*V  B:+1.772（旧 PAL 模拟系数已废弃）
const mat3 bt601 = mat3(
    1.0, 1.0, 1.0,
    0.0, -0.3441, 1.7720,
    1.4020, -0.7141, 0.0
);

// Range 常量
const float Y_OFF_LIM = 16.0 / 255.0;
const float Y_SCL_LIM = 255.0 / 219.0;
const float C_OFF_LIM = 128.0 / 255.0;
const float C_SCL_LIM = 255.0 / 224.0;  // limited 色度码值跨度 224/255
const float C_OFF_FULL = 0.5;

void main() {
    // 读取 YUV
    float y = texture(y_plane, v_texcoord).r;
    // 独立纹理的 UV 坐标已归一化，直接使用 v_texcoord
    vec2 chroma_uv = v_texcoord * chroma_uv_scale + chroma_uv_offset;
    float u = texture(u_plane, chroma_uv).r;
    float v = texture(v_plane, chroma_uv).r;
    
    // Range 转换
    if (color_range == 0) {
        y = (y - Y_OFF_LIM) * Y_SCL_LIM;
        u = (u - C_OFF_LIM) * C_SCL_LIM;
        v = (v - C_OFF_LIM) * C_SCL_LIM;
    } else {
        u = u - C_OFF_FULL;
        v = v - C_OFF_FULL;
    }
    
    // YUV → RGB
    mat3 matrix = bt709;
    if (yuv_matrix == 1) matrix = bt2020;
    else if (yuv_matrix == 2) matrix = bt601;
    
    vec3 rgb = matrix * vec3(y, u, v);
    
    // HDR 处理
    vec3 display;
    if (transfer_function == 0) {
        // SDR
        if (display_gamma <= 0.0) {
            display = linear_to_srgb(rgb);
        } else {
            display = scene_linear_to_display(rgb, display_gamma);
        }
    } else if (transfer_function == 1) {
        // PQ
        display = pq_hdr_to_display(rgb, target_peak_nits, tonemap_mode, display_gamma);
    } else {
        // HLG
        display = hlg_hdr_to_display(rgb, target_peak_nits, 1.2, 1.0, 
                                      tonemap_mode, display_gamma);
    }
    
    f_color = vec4(display, 1.0);
}

// ================================================================
// End of video_color_decode.glsl
// ================================================================