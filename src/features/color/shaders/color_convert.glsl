// ================================================================
// color_convert.glsl - Color Space Conversion Shader Library
// ================================================================
//
// 提供完整的色彩空间转换功能，包括：
// - YUV → RGB（BT.709, BT.2020, BT.601）
// - Range 转换（Limited/Full）
// - Transfer function 转换（PQ, HLG, sRGB, Linear）
// - HDR tone mapping 预研
//
// 注意：所有数字必须带 .0（GLSL 严格要求）
// ================================================================

#version 330

// ================================================================
// Part 1: YUV → RGB Conversion Matrices
// ================================================================

// BT.709 matrix (HD video, Rec. 709)
// Y = 0.2126*R + 0.7152*G + 0.0722*B
const mat3 BT709_YUV_TO_RGB = mat3(
    1.0, 1.0, 1.0,            // Y column
    0.0, -0.1873, 1.8556,     // U column (Cb)
    1.5748, -0.4681, 0.0      // V column (Cr)
);

// BT.2020 NCL matrix (UHD/HDR, Rec. 2020 non-constant luminance)
// Y = 0.2627*R + 0.6780*G + 0.0593*B
const mat3 BT2020_YUV_TO_RGB = mat3(
    1.0, 1.0, 1.0,
    0.0, -0.1646, 1.8814,
    1.4746, -0.5714, 0.0
);

// BT.601 NTSC matrix (SD video, SMPTE 170M)
// Y = 0.299*R + 0.587*G + 0.114*B
// 数字 YCbCr 系数（AUD-R2-010，AUD-39800 同族）：色度归一化为
// limited-range 数字 YCbCr（±0.5），标准系数由 Kr=0.299/Kb=0.114 推导：
// R:+1.402  G:-0.3441*U-0.7141*V  B:+1.772（旧 PAL 模拟系数已废弃）
const mat3 BT601_YUV_TO_RGB = mat3(
    1.0, 1.0, 1.0,
    0.0, -0.3441, 1.7720,
    1.4020, -0.7141, 0.0
);

// BT.601 PAL matrix (SD video, BT.470BG)
// Same coefficients as NTSC（数字域 PAL/NTSC 同为 BT.601 Kr/Kb）
const mat3 BT601_PAL_YUV_TO_RGB = mat3(
    1.0, 1.0, 1.0,
    0.0, -0.3441, 1.7720,
    1.4020, -0.7141, 0.0
);

// ================================================================
// Part 2: Range Conversion Constants
// ================================================================

// Limited range (TV legal range):
// Y: [16, 235] mapped to [0, 1] -> 16/255 offset, scale = 255/219
// C: [16, 240] centered at 128 -> offset = 128/255
const float Y_OFFSET_LIMITED_8BIT = 16.0 / 255.0;
const float Y_SCALE_LIMITED_8BIT = 255.0 / 219.0;  // 235 - 16 = 219
const float C_OFFSET_LIMITED_8BIT = 128.0 / 255.0;

// 10-bit limited range:
// Y: [64, 940] -> offset = 64/1023, scale = 1023/876
const float Y_OFFSET_LIMITED_10BIT = 64.0 / 1023.0;
const float Y_SCALE_LIMITED_10BIT = 1023.0 / 876.0;  // 940 - 64 = 876
const float C_OFFSET_LIMITED_10BIT = 512.0 / 1023.0;

// Full range (PC range):
// C offset = 0.5 (UV centered at 0.5)
const float C_OFFSET_FULL = 0.5;

// ================================================================
// Part 3: Transfer Function Constants (HDR)
// ================================================================

// PQ (SMPTE ST 2084) constants
// Peak luminance: 10,000 nits
const float PQ_M1 = 2610.0 / 16384.0;  // 0.1593017578125
const float PQ_M2 = 2523.0 / 4096.0 * 128.0;  // 78.84375
const float PQ_C1 = 3443.0 / 4096.0;  // 0.8359375
const float PQ_C2 = 15.0 / 4096.0;  // 0.003671875 (corrected: 18/4096)
const float PQ_C3 = 1305.0 / 4096.0;  // 0.31884765625 (corrected: 36/4096)
const float PQ_LMAX = 10000.0;  // Peak luminance (nits)

// HLG (ARIB STD-B67) constants
const float HLG_A = 0.17883277;
const float HLG_B = 0.28466892;
const float HLG_C = 0.55991073;

// sRGB constants
const float SRGB_THRESHOLD = 0.04045;
const float SRGB_LINEAR_SCALE = 12.92;
const float SRGB_GAMMA_SCALE = 1.055;
const float SRGB_GAMMA_OFFSET = 0.055;
const float SRGB_GAMMA_INV = 1.0 / 2.4;
const float SRGB_GAMMA = 2.4;

// ================================================================
// Part 4: Range Conversion Functions
// ================================================================

// Convert limited range Y to normalized [0, 1] (8-bit)
float limited_y_to_normalized_8bit(float y) {
    return (y - Y_OFFSET_LIMITED_8BIT) * Y_SCALE_LIMITED_8BIT;
}

// Convert limited range Y to normalized [0, 1] (10-bit)
float limited_y_to_normalized_10bit(float y) {
    return (y - Y_OFFSET_LIMITED_10BIT) * Y_SCALE_LIMITED_10BIT;
}

// Convert limited range C (U/V) to normalized [-0.5, 0.5] (8-bit)
float limited_c_to_normalized_8bit(float c) {
    return c - C_OFFSET_LIMITED_8BIT;
}

// Convert limited range C (U/V) to normalized [-0.5, 0.5] (10-bit)
float limited_c_to_normalized_10bit(float c) {
    return c - C_OFFSET_LIMITED_10BIT;
}

// Convert full range Y to normalized [0, 1]
// (already normalized, just pass through)
float full_y_to_normalized(float y) {
    return y;
}

// Convert full range C (U/V) to normalized [-0.5, 0.5]
float full_c_to_normalized(float c) {
    return c - C_OFFSET_FULL;
}

// ================================================================
// Part 5: YUV → RGB Conversion Functions
// ================================================================

// Convert YUV to RGB with specified matrix (8-bit limited range)
vec3 yuv_to_rgb_bt709_limited_8bit(float y, float u, float v) {
    // Range conversion
    float y_norm = limited_y_to_normalized_8bit(y);
    float u_norm = limited_c_to_normalized_8bit(u);
    float v_norm = limited_c_to_normalized_8bit(v);

    // Matrix conversion
    return BT709_YUV_TO_RGB * vec3(y_norm, u_norm, v_norm);
}

// Convert YUV to RGB with BT.709 matrix (full range)
vec3 yuv_to_rgb_bt709_full(float y, float u, float v) {
    // Range conversion
    float y_norm = full_y_to_normalized(y);
    float u_norm = full_c_to_normalized(u);
    float v_norm = full_c_to_normalized(v);

    // Matrix conversion
    return BT709_YUV_TO_RGB * vec3(y_norm, u_norm, v_norm);
}

// Convert YUV to RGB with BT.2020 matrix
vec3 yuv_to_rgb_bt2020_limited_8bit(float y, float u, float v) {
    float y_norm = limited_y_to_normalized_8bit(y);
    float u_norm = limited_c_to_normalized_8bit(u);
    float v_norm = limited_c_to_normalized_8bit(v);
    return BT2020_YUV_TO_RGB * vec3(y_norm, u_norm, v_norm);
}

// Convert YUV to RGB with BT.601 matrix
vec3 yuv_to_rgb_bt601_limited_8bit(float y, float u, float v) {
    float y_norm = limited_y_to_normalized_8bit(y);
    float u_norm = limited_c_to_normalized_8bit(u);
    float v_norm = limited_c_to_normalized_8bit(v);
    return BT601_YUV_TO_RGB * vec3(y_norm, u_norm, v_norm);
}

// Generic YUV to RGB function with matrix selection
// matrix_id: 0=BT709, 1=BT2020, 2=BT601
// range_id: 0=limited, 1=full
vec3 yuv_to_rgb(float y, float u, float v, int matrix_id, int range_id) {
    // Range conversion
    float y_norm;
    float u_norm;
    float v_norm;

    if (range_id == 0) {
        // Limited range
        y_norm = limited_y_to_normalized_8bit(y);
        u_norm = limited_c_to_normalized_8bit(u);
        v_norm = limited_c_to_normalized_8bit(v);
    } else {
        // Full range
        y_norm = full_y_to_normalized(y);
        u_norm = full_c_to_normalized(u);
        v_norm = full_c_to_normalized(v);
    }

    // Matrix selection
    mat3 conv_matrix = BT709_YUV_TO_RGB;
    if (matrix_id == 1) {
        conv_matrix = BT2020_YUV_TO_RGB;
    } else if (matrix_id == 2) {
        conv_matrix = BT601_YUV_TO_RGB;
    }

    return conv_matrix * vec3(y_norm, u_norm, v_norm);
}

// ================================================================
// Part 6: Transfer Function Conversion (HDR)
// ================================================================

// PQ to Linear (inverse EOTF)
// Input: PQ encoded value [0, 1]
// Output: Linear light value (nits, up to 10,000)
float pq_to_linear(float pq) {
    // Clamp input
    pq = max(pq, 0.0);
    pq = min(pq, 1.0);

    // Step 1: E'^(1/m2)
    float v = pow(pq, 1.0 / PQ_M2);

    // Step 2: Apply inverse formula
    float numerator = max(v - PQ_C1, 0.0);
    float denominator = max(PQ_C2 - PQ_C3 * v, 0.0001);

    // Step 3: ^(1/m1)
    float linear = pow(numerator / denominator, 1.0 / PQ_M1);

    // Scale to nits
    return linear * PQ_LMAX;
}

// Linear to PQ (forward EOTF)
// Input: Linear light value (nits)
// Output: PQ encoded value [0, 1]
float linear_to_pq(float linear_nits) {
    // Normalize to 10,000 nits
    float normalized = linear_nits / PQ_LMAX;
    normalized = max(normalized, 0.0);
    normalized = min(normalized, 1.0);

    // Step 1: E^(m1)
    float v = pow(normalized, PQ_M1);

    // Step 2: Apply formula
    float numerator = PQ_C1 + PQ_C2 * v;
    float denominator = 1.0 + PQ_C3 * v;

    // Step 3: ^(m2)
    return pow(numerator / denominator, PQ_M2);
}

// HLG to Linear (inverse OETF)
// Input: HLG encoded value [0, 1]
// Output: Linear light value (relative)
float hlg_to_linear(float hlg) {
    hlg = max(hlg, 0.0);
    hlg = min(hlg, 1.0);

    // Piecewise function
    if (hlg <= 0.5) {
        // Low region: simple square
        return (hlg * hlg) / 3.0;
    } else {
        // High region: exponential
        return exp((hlg - HLG_C) / HLG_A) + HLG_B;
    }
}

// Linear to HLG (forward OETF)
// Input: Linear light value (relative)
// Output: HLG encoded value [0, 1]
float linear_to_hlg(float linear) {
    linear = max(linear, 0.0);

    // Piecewise function
    if (linear <= 1.0) {
        // Low region: sqrt
        return sqrt(3.0 * linear);
    } else {
        // High region: log
        return HLG_A * log(linear - HLG_B) + HLG_C;
    }
}

// sRGB to Linear (inverse EOTF)
// Input: sRGB encoded value [0, 1]
// Output: Linear light value [0, 1]
float srgb_to_linear(float srgb) {
    srgb = max(srgb, 0.0);
    srgb = min(srgb, 1.0);

    // Piecewise function
    if (srgb <= SRGB_THRESHOLD) {
        // Low region: linear
        return srgb / SRGB_LINEAR_SCALE;
    } else {
        // High region: gamma 2.4
        return pow((srgb + SRGB_GAMMA_OFFSET) / SRGB_GAMMA_SCALE, SRGB_GAMMA);
    }
}

// Linear to sRGB (forward EOTF)
// Input: Linear light value [0, 1]
// Output: sRGB encoded value [0, 1]
float linear_to_srgb(float linear) {
    linear = max(linear, 0.0);
    linear = min(linear, 1.0);

    // Piecewise function
    if (linear <= 0.0031308) {
        // Low region: linear
        return linear * SRGB_LINEAR_SCALE;
    } else {
        // High region: gamma 1/2.4
        return SRGB_GAMMA_SCALE * pow(linear, SRGB_GAMMA_INV) - SRGB_GAMMA_OFFSET;
    }
}

// ================================================================
// Part 7: Tone Mapping (HDR → SDR)
// ================================================================

// Simple Reinhard tone mapping
// Input: Linear HDR value (can exceed 1.0)
// Output: Tone mapped value [0, 1]
float reinhard_tonemap(float hdr_linear) {
    return hdr_linear / (1.0 + hdr_linear);
}

// Reinhard with adjustable white point
// white_point: Reference white level (default 1.0 for normalized)
float reinhard_extended_tonemap(float hdr_linear, float white_point) {
    float scale = white_point * white_point;
    return (hdr_linear * (1.0 + hdr_linear / scale)) / (1.0 + hdr_linear);
}

// ACES Filmic tone mapping (approximation)
// More cinematic look, preserves highlights better
float aces_tonemap(float hdr_linear) {
    // ACES approximation parameters
    const float a = 2.51;
    const float b = 0.03;
    const float c = 2.43;
    const float d = 0.59;
    const float e = 0.14;

    float numerator = hdr_linear * (a * hdr_linear + b);
    float denominator = hdr_linear * (c * hdr_linear + d) + e;

    return numerator / denominator;
}

// PQ to sRGB tone mapping
// Complete pipeline: PQ → Linear → Tone Map → sRGB
float pq_to_srgb_tonemap(float pq, float target_peak_nits) {
    // PQ to linear (nits)
    float linear_nits = pq_to_linear(pq);

    // Normalize to target peak
    float normalized = linear_nits / target_peak_nits;

    // Apply tone mapping (Reinhard)
    float mapped = reinhard_tonemap(normalized);

    // Apply gamma for display (sRGB)
    return linear_to_srgb(mapped);
}

// HLG to sRGB tone mapping
float hlg_to_srgb_tonemap(float hlg, float exposure_gain) {
    // HLG to linear
    float linear = hlg_to_linear(hlg);

    // Apply exposure gain
    linear = linear * exposure_gain;

    // Tone map and gamma
    float mapped = reinhard_tonemap(linear);

    return linear_to_srgb(mapped);
}

// ================================================================
// Part 8: Complete HDR Processing Pipeline
// ================================================================

// Process HDR frame: PQ → Linear → Tone Map → Display Gamma
vec3 process_pq_hdr_frame(vec3 pq_rgb, float target_peak_nits, int tonemap_mode) {
    // PQ to linear (nits)
    vec3 linear_nits = vec3(
        pq_to_linear(pq_rgb.r),
        pq_to_linear(pq_rgb.g),
        pq_to_linear(pq_rgb.b)
    );

    // Normalize to target peak
    vec3 normalized = linear_nits / target_peak_nits;

    // Tone mapping
    vec3 mapped;
    if (tonemap_mode == 0) {
        // Reinhard
        mapped = vec3(
            reinhard_tonemap(normalized.r),
            reinhard_tonemap(normalized.g),
            reinhard_tonemap(normalized.b)
        );
    } else if (tonemap_mode == 1) {
        // Reinhard Extended
        mapped = vec3(
            reinhard_extended_tonemap(normalized.r, 1.0),
            reinhard_extended_tonemap(normalized.g, 1.0),
            reinhard_extended_tonemap(normalized.b, 1.0)
        );
    } else {
        // ACES Filmic
        mapped = vec3(
            aces_tonemap(normalized.r),
            aces_tonemap(normalized.g),
            aces_tonemap(normalized.b)
        );
    }

    // Apply display gamma (sRGB)
    return vec3(
        linear_to_srgb(mapped.r),
        linear_to_srgb(mapped.g),
        linear_to_srgb(mapped.b)
    );
}

// Process HLG frame
vec3 process_hlg_hdr_frame(vec3 hlg_rgb, float exposure_gain, int tonemap_mode) {
    // HLG to linear
    vec3 linear = vec3(
        hlg_to_linear(hlg_rgb.r),
        hlg_to_linear(hlg_rgb.g),
        hlg_to_linear(hlg_rgb.b)
    );

    // Apply exposure gain
    linear = linear * exposure_gain;

    // Tone mapping
    vec3 mapped;
    if (tonemap_mode == 0) {
        mapped = vec3(
            reinhard_tonemap(linear.r),
            reinhard_tonemap(linear.g),
            reinhard_tonemap(linear.b)
        );
    } else {
        mapped = vec3(
            aces_tonemap(linear.r),
            aces_tonemap(linear.g),
            aces_tonemap(linear.b)
        );
    }

    // Apply display gamma
    return vec3(
        linear_to_srgb(mapped.r),
        linear_to_srgb(mapped.g),
        linear_to_srgb(mapped.b)
    );
}

// ================================================================
// Part 9: Utility Functions
// ================================================================

// Safe clamp for HDR (preserve highlights)
vec3 safe_clamp_hdr(vec3 rgb, float max_value) {
    return min(rgb, vec3(max_value));
}

// Luminance calculation (BT.709 coefficients)
float luminance_bt709(vec3 rgb) {
    return 0.2126 * rgb.r + 0.7152 * rgb.g + 0.0722 * rgb.b;
}

// Luminance calculation (BT.2020 coefficients)
float luminance_bt2020(vec3 rgb) {
    return 0.2627 * rgb.r + 0.6780 * rgb.g + 0.0593 * rgb.b;
}

// Chroma subsampling-aware UV coordinate scaling
vec2 scale_uv_for_subsampling(vec2 uv, int subsampling_mode) {
    // subsampling_mode:
    // 0 = 4:4:4 (no scaling)
    // 1 = 4:2:2 (horizontal 0.5)
    // 2 = 4:2:0 (horizontal + vertical 0.5)

    if (subsampling_mode == 0) {
        return uv;  // No scaling
    } else if (subsampling_mode == 1) {
        return vec2(uv.x * 0.5, uv.y);  // 4:2:2
    } else {
        return vec2(uv.x * 0.5, uv.y * 0.5);  // 4:2:0
    }
}

// ================================================================
// End of color_convert.glsl
// ================================================================