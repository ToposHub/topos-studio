// YUV GPU Convert Shader
// YUV 平面直接 GPU 上传后的 RGB 转换
//
// 支持格式：
// - YUV420P (三个独立平面)
// - YUV422P (三个独立平面)
// - YUV444P (三个独立平面)
// - NV12 (Y + interleaved UV)
// - P010 (10-bit NV12)
//
// 支持色彩空间：
// - BT.709 (HD video, Rec. 709)
// - BT.2020 (HDR, Rec. 2020)
// - BT.601 (SD video, Rec. 601)
//
// 支持颜色范围：
// - Limited (TV legal range)
// - Full (PC range)
//
// 使用方式：
// 1. 上传 Y/U/V 平面到 GPU 纹理
// 2. 绑定纹理到 shader uniform
// 3. 设置 yuv_matrix, color_range, chroma_scale
// 4. 渲染全屏四边形
//
// 注意：
// - 所有数字必须带 .0 后缀（GLSL 规范）
// - for 循环边界必须是常量

// ================================================================
// VERTEX SHADER - 全屏四边形
// ================================================================

#version 330

in vec2 in_vert;      // 顶点位置 (-1 to 1)
in vec2 in_texcoord;  // 纹理坐标 (0 to 1)

out vec2 v_texcoord;  // 传递给 fragment shader

void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_texcoord = in_texcoord;
}

// ================================================================
// FRAGMENT SHADER - YUV420/422/444 Planar 转 RGB
// ================================================================

// FRAGMENT SHADER PLANAR

#version 330

// 纹理输入
uniform sampler2D y_plane;   // Y 平面 (亮度)
uniform sampler2D u_plane;   // U 平面 (蓝色色度)
uniform sampler2D v_plane;   // V 平面 (红色色度)

// 转换参数
uniform int yuv_matrix;      // 0=BT.709, 1=BT.2020, 2=BT.601
uniform int color_range;     // 0=limited (TV), 1=full (PC)
uniform vec2 chroma_uv_scale;  // Chroma UV coordinate scale (default 1.0, 1.0 for independent textures)
uniform vec2 chroma_uv_offset; // Chroma UV coordinate offset (default 0.0, 0.0)

// 输入
in vec2 v_texcoord;

// 输出
out vec4 f_color;

// ================================================================
// 色彩空间转换矩阵
// ================================================================

// BT.709 矩阵 (HD video, Rec. 709)
// 用于 720p, 1080p, 4K SDR 内容
// Y = 0.2126*R + 0.7152*G + 0.0722*B
const mat3 bt709_matrix = mat3(
    1.0, 1.0, 1.0,           // Y 列
    0.0, -0.1873, 1.8556,    // U 列 (Cb)
    1.5748, -0.4681, 0.0     // V 列 (Cr)
);

// BT.2020 矩阵 (UHD/HDR, Rec. 2020)
// 用于 HDR10, Dolby Vision, HLG 内容
// Y = 0.2627*R + 0.6780*G + 0.0593*B
const mat3 bt2020_matrix = mat3(
    1.0, 1.0, 1.0,
    0.0, -0.1646, 1.8814,
    1.4746, -0.5714, 0.0
);

// BT.601 矩阵 (SD video, Rec. 601)
// 用于 PAL/NTSC 标清内容
// Y = 0.299*R + 0.587*G + 0.114*B
// 数字 YCbCr 系数（AUD-R2-010，AUD-39800 同族）：色度归一化为
// limited-range 数字 YCbCr（±0.5），标准系数由 Kr=0.299/Kb=0.114 推导：
// R:+1.402  G:-0.3441*U-0.7141*V  B:+1.772（旧 PAL 模拟系数已废弃）
const mat3 bt601_matrix = mat3(
    1.0, 1.0, 1.0,
    0.0, -0.3441, 1.7720,
    1.4020, -0.7141, 0.0
);

// ================================================================
// 颜色范围常量
// ================================================================

// Limited range (TV legal range):
// Y: [16, 235] 映射到 [0, 1]
// C: [16, 240] 映射到 [-0.5, 0.5]
// 注意：实际 UV 以 128 为中心（8-bit）

const float Y_OFFSET_LIM = 16.0 / 255.0;
const float Y_SCALE_LIM = 255.0 / 219.0;   // 235 - 16 = 219
const float C_OFFSET_LIM = 128.0 / 255.0;
const float C_SCALE_LIM = 255.0 / 224.0;   // 240 - 16 = 224

// Full range (PC range):
// Y: [0, 255] 映射到 [0, 1]
// C: [0, 255] 映射到 [-0.5, 0.5]

const float C_OFFSET_FULL = 0.5;

// ================================================================
// 主函数
// ================================================================

void main() {
    // 读取 Y 平面（全分辨率）
    float y = texture(y_plane, v_texcoord).r;

    // 读取 U/V 平面（独立纹理，纹理坐标已归一化到 [0, 1]）
    // 对于 YUV420/YUV422/YUV444 的独立 U/V 纹理，纹理坐标范围本身就是 [0, 1]
    // 直接使用 v_texcoord 即可，无需乘以 chroma subsampling 比例
    vec2 chroma_uv = v_texcoord * chroma_uv_scale + chroma_uv_offset;
    float u = texture(u_plane, chroma_uv).r;
    float v = texture(v_plane, chroma_uv).r;

    // ================================================================
    // 颜色范围转换
    // ================================================================

    if (color_range == 0) {
        // Limited range (TV legal)
        // Y: [16/255, 235/255] -> [0, 1]
        y = (y - Y_OFFSET_LIM) * Y_SCALE_LIM;

        // U/V: [16/255, 240/255] -> [-0.5, 0.5]
        // Limited range UV 以 128/255 为中心，码值跨度 224/255，
        // 归一化需乘 255/224（与 YUVGPUUploader / color_decode CPU 参考一致）
        u = (u - C_OFFSET_LIM) * C_SCALE_LIM;
        v = (v - C_OFFSET_LIM) * C_SCALE_LIM;
    } else {
        // Full range (PC)
        // Y: [0, 1] -> [0, 1] (无偏移)
        // U/V: [0, 1] -> [-0.5, 0.5]
        u = u - C_OFFSET_FULL;
        v = v - C_OFFSET_FULL;
    }

    // ================================================================
    // 色彩空间转换
    // ================================================================

    // 选择适当的转换矩阵
    mat3 conv_matrix = bt709_matrix;
    if (yuv_matrix == 1) {
        conv_matrix = bt2020_matrix;
    } else if (yuv_matrix == 2) {
        conv_matrix = bt601_matrix;
    }

    // YUV -> RGB 转换
    // RGB = matrix * vec3(Y, U, V)
    vec3 rgb = conv_matrix * vec3(y, u, v);

    // ================================================================
    // 输出
    // ================================================================

    // 输出 RGB，不做 clamp（保留 HDR 高光）
    // 下游管线处理 tone mapping
    f_color = vec4(rgb, 1.0);
}

// ================================================================
// FRAGMENT SHADER - NV12/P010 转 RGB (Interleaved UV)
// ================================================================

// FRAGMENT SHADER NV12

#version 330

// 纹理输入
uniform sampler2D y_plane;    // Y 平面 (亮度)
uniform sampler2D uv_plane;   // Interleaved UV 平面 (RG 格式)
                               // R = U, G = V

// 转换参数
uniform int yuv_matrix;
uniform int color_range;
uniform vec2 chroma_uv_scale;  // Chroma UV coordinate scale (default 1.0, 1.0)
uniform vec2 chroma_uv_offset; // Chroma UV coordinate offset (default 0.0, 0.0)

in vec2 v_texcoord;
out vec4 f_color;

// 转换矩阵（同上）
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

// BT.601 数字 YCbCr 系数（AUD-R2-010，AUD-39800 同族；推导见上文
// bt601_matrix 处注释）
const mat3 bt601 = mat3(
    1.0, 1.0, 1.0,
    0.0, -0.3441, 1.7720,
    1.4020, -0.7141, 0.0
);

void main() {
    // 读取 Y 平面
    float y = texture(y_plane, v_texcoord).r;

    // 读取 interleaved UV 平面（独立纹理，纹理坐标已归一化）
    // NV12: UV 交织存储，U 在 R 通道，V 在 G 通道
    // NV12 UV 纹理是独立的 half-size 纹理，坐标范围 [0, 1]
    vec2 chroma_uv = v_texcoord * chroma_uv_scale + chroma_uv_offset;
    vec2 uv_rg = texture(uv_plane, chroma_uv).rg;
    float u = uv_rg.r;  // U 在 R 通道
    float v = uv_rg.g;  // V 在 G 通道

    // Range 处理
    if (color_range == 0) {
        y = (y - 16.0 / 255.0) * 255.0 / 219.0;
        u = (u - 128.0 / 255.0) * (255.0 / 224.0);
        v = (v - 128.0 / 255.0) * (255.0 / 224.0);
    } else {
        u = u - 0.5;
        v = v - 0.5;
    }

    // 转换矩阵
    mat3 matrix = bt709;
    if (yuv_matrix == 1) {
        matrix = bt2020;
    } else if (yuv_matrix == 2) {
        matrix = bt601;
    }

    vec3 rgb = matrix * vec3(y, u, v);

    f_color = vec4(rgb, 1.0);
}

// ================================================================
// FRAGMENT SHADER - 10-bit/12-bit High Bit Depth
// ================================================================

// FRAGMENT SHADER HIGH_BIT_DEPTH

#version 330

uniform sampler2D y_plane;
uniform sampler2D u_plane;
uniform sampler2D v_plane;
uniform int yuv_matrix;
uniform int color_range;
uniform vec2 chroma_uv_scale;  // Chroma UV coordinate scale (default 1.0, 1.0)
uniform vec2 chroma_uv_offset; // Chroma UV coordinate offset (default 0.0, 0.0)
uniform int bit_depth;       // 位深度 (10, 12, 16)

in vec2 v_texcoord;
out vec4 f_color;

// 转换矩阵
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

// BT.601 数字 YCbCr 系数（AUD-R2-010，AUD-39800 同族；推导见上文
// bt601_matrix 处注释）
const mat3 bt601 = mat3(
    1.0, 1.0, 1.0,
    0.0, -0.3441, 1.7720,
    1.4020, -0.7141, 0.0
);

void main() {
    float y = texture(y_plane, v_texcoord).r;
    // 独立纹理的 UV 坐标已归一化，直接使用 v_texcoord
    vec2 chroma_uv = v_texcoord * chroma_uv_scale + chroma_uv_offset;
    float u = texture(u_plane, chroma_uv).r;
    float v = texture(v_plane, chroma_uv).r;

    // 高位深 Limited range:
    // 10-bit: Y [64, 940], UV [64, 960] (以 512 为中心)
    // 12-bit: Y [256, 3760], UV [256, 3840] (以 2048 为中心)
    // 但 shader 使用统一的 8-bit offset/scale

    if (color_range == 0) {
        // 使用与 8-bit 相同的 offset/scale
        // 因为纹理数据已归一化到 [0, 1]
        y = (y - 16.0 / 255.0) * 255.0 / 219.0;
        u = (u - 128.0 / 255.0) * (255.0 / 224.0);
        v = (v - 128.0 / 255.0) * (255.0 / 224.0);
    } else {
        u = u - 0.5;
        v = v - 0.5;
    }

    mat3 matrix = bt709;
    if (yuv_matrix == 1) {
        matrix = bt2020;
    } else if (yuv_matrix == 2) {
        matrix = bt601;
    }

    vec3 rgb = matrix * vec3(y, u, v);

    // HDR 输出，保留高位深精度
    f_color = vec4(rgb, 1.0);
}