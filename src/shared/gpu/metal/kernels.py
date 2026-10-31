"""大半径模糊 / 腐蚀膨胀的 MSL compute kernel 集（计划 P4-02）。

与 `src/features/composite/core/shaders.py` 的 GLSL compute 源
（`ComputeShaderManager._COMPUTE_SOURCES`）逐名对齐。buffer 布局为
RGBA16F 的线性数组（half4 = 4×float16），对应 GLSL 的 rgba16f
image2D；输入经 CPU staging 桥进入 MTLBuffer（macOS GL 4.1 与 Metal
无纹理共享通道，见 `metal_compute_bridge.py`）。

语义对齐要点（与 GLSL 逐条对应）：

- 高斯：sigma = max(radius, 0.1) * 0.5，kernel_size =
  min(ceil(sigma*3), 64)，权重 exp(-i²/2σ²)，按权重和归一（下限 1e-5）。
- 边缘策略：0=clamp、1=wrap。GLSL 版对 uv 做 clamp/fract 后经
  CLAMP_TO_EDGE 采样，等价于整数纹理索引的 clamp/wrap（uv=0 与
  uv=1 恰好落在首/末 texel）。GLSL 未实现 mirror（策略 2 会落入
  无分支路径，等效 clamp），MSL 版同样按 clamp 处理。
- 腐蚀/膨胀：方形邻域 half_size = min(size/2, 50)，operation
  0=erode(min)/1=dilate(max)，channels 0=仅 alpha / 1=全部通道。

与 GLSL shared 变体的关系：`blur_h_shared` / `blur_v_shared` 用
threadgroup 存量缓存实现真正的 tile 优化（GLSL 原版的协作加载按
16×16 工作组共享一行缓存，存在跨行覆写隐患，MSL 版重新设计为
每组一行、64 线程/组）；`erode_dilate_shared` 与 `erode_dilate`
同实现——GLSL 的 shared 优化针对 GL 纹理取数瓶颈，Metal 侧
buffer 直读经硬件 L2 缓存无此瓶颈，半径 50 的完整方阵 tile
（≥93KB）也超出 threadgroup 存储上限。同名同语义，画质一致。
"""

from __future__ import annotations

import numpy as np

# 与 ComputeShaderManager._COMPUTE_SOURCES 的名字对齐（histogram 归
# P4-03，见 kernels_histogram.py）。
METAL_BLUR_MSL = """
#include <metal_stdlib>
using namespace metal;

struct BlurParams {
    uint32_t width;
    uint32_t height;
    uint32_t edge_policy;   // 0=clamp, 1=wrap
    float    radius;
};

static inline int blur_sample_coord(int x, int width, uint32_t policy) {
    if (policy == 1u) {
        int m = x % (int)width;
        return m < 0 ? m + (int)width : m;
    }
    return clamp(x, 0, (int)width - 1);
}

static inline float gaussian_weight(float x, float sigma) {
    return exp(-(x * x) / (2.0 * sigma * sigma));
}

// 与 _BLUR_H_COMPUTE_SSBO 语义对齐。
kernel void blur_h(device const half4* in        [[buffer(0)]],
                   device half4*       out       [[buffer(1)]],
                   constant BlurParams& p        [[buffer(2)]],
                   uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= p.width || gid.y >= p.height) { return; }
    float radius = max(p.radius, 0.1);
    float sigma = radius * 0.5;
    int kernel_size = min((int)ceil(sigma * 3.0), 64);
    size_t row = gid.y * (size_t)p.width;
    float4 color = 0.0;
    float weight_sum = 0.0;
    for (int i = -64; i <= 64; i++) {
        if (abs(i) > kernel_size) { continue; }
        float w = gaussian_weight((float)i, sigma);
        int sx = blur_sample_coord((int)gid.x + i, (int)p.width, p.edge_policy);
        color += float4(in[row + (size_t)sx]) * w;
        weight_sum += w;
    }
    out[row + gid.x] = half4(color / max(weight_sum, 0.00001));
}

// 与 _BLUR_V_COMPUTE_SSBO 语义对齐。
kernel void blur_v(device const half4* in        [[buffer(0)]],
                   device half4*       out       [[buffer(1)]],
                   constant BlurParams& p        [[buffer(2)]],
                   uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= p.width || gid.y >= p.height) { return; }
    float radius = max(p.radius, 0.1);
    float sigma = radius * 0.5;
    int kernel_size = min((int)ceil(sigma * 3.0), 64);
    float4 color = 0.0;
    float weight_sum = 0.0;
    for (int j = -64; j <= 64; j++) {
        if (abs(j) > kernel_size) { continue; }
        float w = gaussian_weight((float)j, sigma);
        int sy = blur_sample_coord((int)gid.y + j, (int)p.height, p.edge_policy);
        color += float4(in[(size_t)sy * p.width + gid.x]) * w;
        weight_sum += w;
    }
    out[gid.y * (size_t)p.width + gid.x] = half4(color / max(weight_sum, 0.00001));
}
"""

# threadgroup tile 版本：每个工作组处理一行的 64 列，tile 覆盖
# [base-64, base+64+64) 共 192 个 texel（1536 B）。与 simple 版数学完全
# 一致，仅取数路径不同（对应 GLSL shared 变体的优化意图）。
METAL_BLUR_SHARED_MSL = """
#include <metal_stdlib>
using namespace metal;

struct BlurParams {
    uint32_t width;
    uint32_t height;
    uint32_t edge_policy;
    float    radius;
};

constant int TILE = 64;
constant int PAD = 64;   // MAX_KERNEL 上限

static inline int blur_sample_coord(int x, int width, uint32_t policy) {
    if (policy == 1u) {
        int m = x % (int)width;
        return m < 0 ? m + (int)width : m;
    }
    return clamp(x, 0, (int)width - 1);
}

static inline float gaussian_weight(float x, float sigma) {
    return exp(-(x * x) / (2.0 * sigma * sigma));
}

kernel void blur_h_shared(device const half4* in  [[buffer(0)]],
                          device half4*       out [[buffer(1)]],
                          constant BlurParams& p   [[buffer(2)]],
                          uint3 gid [[thread_position_in_grid]],
                          uint3 lid [[thread_position_in_threadgroup]]) {
    threadgroup half4 tile[TILE + 2 * PAD];
    int base_x = (int)(gid.x - lid.x);            // 本组起始列
    int row = (int)gid.y;
    // 64 线程协作加载 192 个 texel（左 pad + 本组 + 右 pad）
    for (int k = (int)lid.x; k < TILE + 2 * PAD; k += TILE) {
        int col = blur_sample_coord(base_x - PAD + k, (int)p.width, p.edge_policy);
        tile[k] = in[(size_t)row * (size_t)p.width + (size_t)col];
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);
    if (gid.x >= p.width) { return; }
    float radius = max(p.radius, 0.1);
    float sigma = radius * 0.5;
    int kernel_size = min((int)ceil(sigma * 3.0), 64);
    float4 color = 0.0;
    float weight_sum = 0.0;
    for (int i = -PAD; i <= PAD; i++) {
        if (abs(i) > kernel_size) { continue; }
        float w = gaussian_weight((float)i, sigma);
        color += float4(tile[(int)lid.x + PAD + i]) * w;
        weight_sum += w;
    }
    out[(size_t)row * p.width + gid.x] = half4(color / max(weight_sum, 0.00001));
}

kernel void blur_v_shared(device const half4* in  [[buffer(0)]],
                          device half4*       out [[buffer(1)]],
                          constant BlurParams& p   [[buffer(2)]],
                          uint3 gid [[thread_position_in_grid]],
                          uint3 lid [[thread_position_in_threadgroup]]) {
    threadgroup half4 tile[TILE + 2 * PAD];
    int base_y = (int)(gid.y - lid.y);
    int col = (int)gid.x;
    for (int k = (int)lid.y; k < TILE + 2 * PAD; k += TILE) {
        int row = blur_sample_coord(base_y - PAD + k, (int)p.height, p.edge_policy);
        tile[k] = in[(size_t)row * (size_t)p.width + (size_t)col];
    }
    threadgroup_barrier(mem_flags::mem_threadgroup);
    if (gid.y >= p.height) { return; }
    float radius = max(p.radius, 0.1);
    float sigma = radius * 0.5;
    int kernel_size = min((int)ceil(sigma * 3.0), 64);
    float4 color = 0.0;
    float weight_sum = 0.0;
    for (int i = -PAD; i <= PAD; i++) {
        if (abs(i) > kernel_size) { continue; }
        float w = gaussian_weight((float)i, sigma);
        color += float4(tile[(int)lid.y + PAD + i]) * w;
        weight_sum += w;
    }
    out[gid.y * (size_t)p.width + gid.x] = half4(color / max(weight_sum, 0.00001));
}
"""

METAL_ERODE_DILATE_MSL = """
#include <metal_stdlib>
using namespace metal;

struct ErodeDilateParams {
    uint32_t width;
    uint32_t height;
    uint32_t operation;   // 0=erode(min), 1=dilate(max)
    uint32_t size;
    uint32_t channels;    // 0=alpha only, 1=all
};

static inline int morph_sample_coord(int x, int limit) {
    return clamp(x, 0, limit - 1);
}

// 与 _ERODE_DILATE_COMPUTE 语义对齐。erode_dilate_shared 同实现
// （理由见模块 docstring）。
static inline float4 morph_impl(device const half4* in,
                                constant ErodeDilateParams& p,
                                uint2 gid) {
    int half_size = min((int)(p.size / 2u), 50);
    size_t idx = gid.y * (size_t)p.width + gid.x;
    float4 result = float4(in[idx]);
    for (int dy = -50; dy <= 50; dy++) {
        if (abs(dy) > half_size) { continue; }
        int sy = morph_sample_coord((int)gid.y + dy, (int)p.height);
        for (int dx = -50; dx <= 50; dx++) {
            if (abs(dx) > half_size) { continue; }
            int sx = morph_sample_coord((int)gid.x + dx, (int)p.width);
            float4 sample_val = float4(in[(size_t)sy * (size_t)p.width + (size_t)sx]);
            if (p.operation == 0u) {
                result = (p.channels == 0u)
                    ? float4(result.rgb, min(result.a, sample_val.a))
                    : min(result, sample_val);
            } else {
                result = (p.channels == 0u)
                    ? float4(result.rgb, max(result.a, sample_val.a))
                    : max(result, sample_val);
            }
        }
    }
    return result;
}

kernel void erode_dilate(device const half4* in  [[buffer(0)]],
                         device half4*       out [[buffer(1)]],
                         constant ErodeDilateParams& p [[buffer(2)]],
                         uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= p.width || gid.y >= p.height) { return; }
    out[gid.y * (size_t)p.width + gid.x] = half4(morph_impl(in, p, gid));
}

kernel void erode_dilate_shared(device const half4* in  [[buffer(0)]],
                                device half4*       out [[buffer(1)]],
                                constant ErodeDilateParams& p [[buffer(2)]],
                                uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= p.width || gid.y >= p.height) { return; }
    out[gid.y * (size_t)p.width + gid.x] = half4(morph_impl(in, p, gid));
}
"""

# 名字 → MSL 源（与 _COMPUTE_SOURCES 对齐；histogram 见 kernels_histogram）。
METAL_COMPUTE_SOURCES = {
    "blur_h": METAL_BLUR_MSL,
    "blur_v": METAL_BLUR_MSL,
    "blur_h_shared": METAL_BLUR_SHARED_MSL,
    "blur_v_shared": METAL_BLUR_SHARED_MSL,
    "erode_dilate": METAL_ERODE_DILATE_MSL,
    "erode_dilate_shared": METAL_ERODE_DILATE_MSL,
}

# MSL struct BlurParams / ErodeDilateParams 的内存布局（全部 4 字节
# 字段，无对齐空洞）。
BLUR_PARAMS_DTYPE = np.dtype([
    ("width", "<u4"), ("height", "<u4"),
    ("edge_policy", "<u4"), ("radius", "<f4"),
])

ERODE_DILATE_PARAMS_DTYPE = np.dtype([
    ("width", "<u4"), ("height", "<u4"),
    ("operation", "<u4"), ("size", "<u4"), ("channels", "<u4"),
])


def make_blur_params(w: int, h: int, radius: float, edge_policy: int = 0) -> np.ndarray:
    """打包 MSL ``BlurParams``（绑定在 buffer(2)，dtype 见
    ``BLUR_PARAMS_DTYPE``）。"""
    params = np.zeros(1, dtype=BLUR_PARAMS_DTYPE)
    params["width"] = w
    params["height"] = h
    params["edge_policy"] = edge_policy
    params["radius"] = radius
    return params


def make_erode_dilate_params(w: int, h: int, operation: str, size: int,
                             channels: int = 0) -> np.ndarray:
    """打包 MSL ``ErodeDilateParams``（绑定在 buffer(2)）。

    Args:
        operation: ``"erode"``（min）或其他（按 dilate/max 编码）。
        size: 方形邻域边长；kernel 侧钳到 half_size = min(size/2, 50)。
        channels: 0=仅 alpha，1=全部通道。
    """
    params = np.zeros(1, dtype=ERODE_DILATE_PARAMS_DTYPE)
    params["width"] = w
    params["height"] = h
    params["operation"] = 0 if operation == "erode" else 1
    params["size"] = size
    params["channels"] = channels
    return params


def gaussian_blur_reference(img: np.ndarray, radius: float, axis: int,
                            edge_policy: int = 0) -> np.ndarray:
    """GLSL/MSL 高斯语义的 numpy 参考（float32 计算，输入输出 half 精度）。

    Args:
        img: (h, w, 4) float16 数组
        axis: 0 = 水平（x 方向），1 = 垂直（y 方向）
        edge_policy: 0=clamp, 1=wrap
    """
    src = img.astype(np.float32)
    sigma = max(radius, 0.1) * 0.5
    kernel_size = min(int(np.ceil(sigma * 3.0)), 64)
    offsets = np.arange(-64, 65)
    offsets = offsets[np.abs(offsets) <= kernel_size].astype(np.int64)
    weights = np.exp(-(offsets.astype(np.float64) ** 2) / (2.0 * sigma * sigma))
    weights = weights.astype(np.float32)
    n = src.shape[1] if axis == 0 else src.shape[0]
    out = np.zeros_like(src)
    for i, off in enumerate(offsets):
        idx = np.arange(n) + off
        if edge_policy == 1:
            idx = idx % n
        else:
            idx = np.clip(idx, 0, n - 1)
        sampled = src[:, idx] if axis == 0 else src[idx]
        out += sampled * weights[i]
    total = weights.sum()
    result = out / max(float(total), 1e-5)
    return result.astype(np.float16)


def erode_dilate_reference(img: np.ndarray, operation: str, size: int,
                           channels: int = 0) -> np.ndarray:
    """GLSL/MSL 形态学语义的 numpy 参考（方形邻域，clamp 边缘）。

    初始化为中心像素（kernel: result = in[idx]），随后在整个方形邻域
    （含中心）上折叠 min/max。
    """
    src = img.astype(np.float32)
    height, width = src.shape[0], src.shape[1]
    half_size = min(size // 2, 50)
    padded = np.pad(
        src,
        ((half_size, half_size), (half_size, half_size), (0, 0)),
        mode="edge",
    )
    out = src.copy()
    fold = np.minimum if operation == "erode" else np.maximum
    for dy in range(-half_size, half_size + 1):
        for dx in range(-half_size, half_size + 1):
            if dy == 0 and dx == 0:
                continue  # 中心像素已作为初值
            window = padded[
                half_size + dy: half_size + dy + height,
                half_size + dx: half_size + dx + width,
            ]
            if channels == 0:
                out[..., 3] = fold(out[..., 3], window[..., 3])
            else:
                out = fold(out, window)
    return out.astype(np.float16)
