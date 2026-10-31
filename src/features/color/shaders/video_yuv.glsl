// Vertex Shader - YUV to RGB conversion
// Standard passthrough vertex shader for full-screen quad rendering

#version 330

in vec2 in_vert;
in vec2 in_texcoord;

out vec2 v_texcoord;

void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_texcoord = in_texcoord;
}

// FRAGMENT SHADER

// YUV to RGB conversion shader
// Supports BT.709, BT.2020, and BT.601 color spaces
// Supports limited and full range

#version 330

uniform sampler2D y_plane;   // Y plane (full resolution)
uniform sampler2D u_plane;   // U plane (chroma subsampled)
uniform sampler2D v_plane;   // V plane (chroma subsampled)
uniform int yuv_matrix;      // 0=BT.709, 1=BT.2020, 2=BT.601
uniform int color_range;     // 0=limited (TV), 1=full (PC)
uniform vec2 chroma_uv_scale;  // Chroma UV coordinate scale (default 1.0, 1.0 for independent textures)
uniform vec2 chroma_uv_offset; // Chroma UV coordinate offset (default 0.0, 0.0)

in vec2 v_texcoord;
out vec4 f_color;

// ================================================================
// Color Space Conversion Matrices
// ================================================================

// BT.709 matrix (HD video, Rec. 709)
// Used for most HD content (720p, 1080p, 4K)
// Y = 0.2126*R + 0.7152*G + 0.0722*B
const mat3 bt709_matrix = mat3(
    1.0, 1.0, 1.0,           // Y column
    0.0, -0.1873, 1.8556,    // U column (Cb)
    1.5748, -0.4681, 0.0     // V column (Cr)
);

// BT.2020 matrix (UHD/HDR, Rec. 2020)
// Used for HDR content (HDR10, Dolby Vision)
// Y = 0.2627*R + 0.6780*G + 0.0593*B
const mat3 bt2020_matrix = mat3(
    1.0, 1.0, 1.0,
    0.0, -0.1646, 1.8814,
    1.4746, -0.5714, 0.0
);

// BT.601 matrix (SD video, Rec. 601)
// Used for older SD content (PAL, NTSC)
// Y = 0.299*R + 0.587*G + 0.114*B
// 数字 YCbCr 系数（AUD-R2-010，AUD-39800 同族）：本 shader 的色度归一化
// 是 limited-range 数字 YCbCr（(C - 128/255) * (255/224) → ±0.5），该约定
// 下的标准系数由 Kr=0.299/Kb=0.114 推导：
// R:+1.402  G:-0.3441*U-0.7141*V  B:+1.772（旧 PAL 模拟系数已废弃）
const mat3 bt601_matrix = mat3(
    1.0, 1.0, 1.0,
    0.0, -0.3441, 1.7720,
    1.4020, -0.7141, 0.0
);

// ================================================================
// Range Conversion Constants
// ================================================================

// Limited range (TV legal range):
// Y: [16, 235] mapped to [0, 1]
// C: [16, 240] mapped to [-0.5, 0.5]
// Note: actual UV is centered at 128 (for 8-bit)

const float Y_OFFSET_LIM = 16.0 / 255.0;
const float Y_SCALE_LIM = 255.0 / 219.0;   // 235 - 16 = 219
const float C_OFFSET_LIM = 128.0 / 255.0;
const float C_SCALE_LIM = 255.0 / 224.0;   // 240 - 16 = 224 (required)

// Full range (PC range):
// Y: [0, 255] mapped to [0, 1]
// C: [0, 255] mapped to [-0.5, 0.5]
const float C_OFFSET_FULL = 0.5;

// ================================================================
// Main Fragment Shader
// ================================================================

void main() {
    // Read Y plane at full resolution
    float y = texture(y_plane, v_texcoord).r;

    // Read U/V planes（独立纹理，纹理坐标已归一化）
    // 对于 YUV420/YUV422/YUV444 的独立 U/V 纹理，纹理坐标范围本身就是 [0, 1]
    // 直接使用 v_texcoord 即可，无需乘以 chroma subsampling 比例
    vec2 chroma_uv = v_texcoord * chroma_uv_scale + chroma_uv_offset;
    float u = texture(u_plane, chroma_uv).r;
    float v = texture(v_plane, chroma_uv).r;

    // ================================================================
    // Range Conversion
    // ================================================================

    if (color_range == 0) {
        // Limited range (TV legal)
        // Y: [16/255, 235/255] -> [0, 1]
        y = (y - Y_OFFSET_LIM) * Y_SCALE_LIM;

        // U/V: [16/255, 240/255] -> [-0.5, 0.5]
        // For limited range, UV is centered at 128/255, code span 224/255,
        // so normalization requires * 255/224 (matches YUVGPUUploader and
        // color_decode CPU reference). Missing it desaturates the image.
        u = (u - C_OFFSET_LIM) * C_SCALE_LIM;
        v = (v - C_OFFSET_LIM) * C_SCALE_LIM;
    } else {
        // Full range (PC)
        // Y: [0, 1] -> [0, 1] (no offset)
        // U/V: [0, 1] -> [-0.5, 0.5]
        u = u - C_OFFSET_FULL;
        v = v - C_OFFSET_FULL;
    }

    // ================================================================
    // Color Space Conversion
    // ================================================================

    // Select appropriate matrix
    mat3 conv_matrix = bt709_matrix;
    if (yuv_matrix == 1) {
        conv_matrix = bt2020_matrix;
    } else if (yuv_matrix == 2) {
        conv_matrix = bt601_matrix;
    }

    // YUV -> RGB conversion
    // RGB = matrix * vec3(Y, U, V)
    vec3 rgb = conv_matrix * vec3(y, u, v);

    // ================================================================
    // Output
    // ================================================================

    // Output RGB without clamping (preserve HDR highlights)
    // The downstream pipeline handles tone mapping if needed
    f_color = vec4(rgb, 1.0);
}