"""Normalize 百分位链路的 MSL histogram / CDF kernel（计划 P4-03）。

与 `src/features/composite/core/shaders.py` 的 `_HISTOGRAM_COMPUTE`
逐条对齐：

- 装箱：``bin = uint(clamp(c, 0, 1) * 255)``（HDR>1 截到 255，负值截到
  0），通道偏移 R=0 / G=256 / B=512 / A=768，共 1024 个 uint32 桶。
- 计数：``total_count`` 对全部像素无条件 +1（与 GLSL 版一致，被
  channel_mask 排除的通道不计入各自直方图但计入总像素）。
- 输入是 RGBA16F 的线性 half4 buffer（CPU staging 桥，同 kernels.py）。

`histogram_cdf` 在 GPU 上完成原 GLSL 链路留在 CPU 的后半段
（计划 §0.4 修正：CDF/百分位下移 GPU）：

- 每线程 (bin, ch) 计算 ``cdf = cumsum(0..bin) / channel_total``，
  写入 cdf 输出（各线程写各槽位，无竞争）。
- 低/高百分位桶归约：低桶 = 满足 ``cdf >= low_p`` 的最小 bin
  （``atomic_fetch_min``）；高桶 = 满足 ``cdf <= high_p`` 的最大 bin——
  atomic_max 无法用「合法上限」做初值（255 会吞掉一切更小值），故存储
  ``255 - bin`` 后同样取 min，读回时还原。两者初值均为哨兵
  0xFFFFFFFF：未被替换 = 该通道无满足桶，CPU 侧映射为
  `find_percentile_values` 的端点默认（low→0 / high→255）；
  channel_total==0 的通道跳过，CPU 侧按全零 CDF 的 (0.0, 1.0) 兜底。
"""

from __future__ import annotations

import numpy as np

# pct_bins 归约哨兵：0xFFFFFFFF = 未有任何 bin 满足阈值
PERCENTILE_BIN_SENTINEL = 0xFFFFFFFF

METAL_HISTOGRAM_MSL = """
#include <metal_stdlib>
using namespace metal;

struct PercentileParams {
    float    low_percentile;    // 0..1
    float    high_percentile;   // 0..1
    uint32_t channel_mask;      // 1=R, 2=G, 4=B, 8=A
};

// 与 _HISTOGRAM_COMPUTE 语义对齐。
kernel void histogram(device const half4*  in   [[buffer(0)]],
                      device atomic_uint* hist [[buffer(1)]],   // 1024
                      device atomic_uint* total [[buffer(2)]],  // 1
                      constant uint2& res       [[buffer(3)]],  // (w, h)
                      constant uint& channel_mask [[buffer(4)]],
                      uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= res.x || gid.y >= res.y) { return; }
    float4 color = float4(in[gid.y * (size_t)res.x + gid.x]);
    uint r_bin = (uint)(clamp(color.r, 0.0, 1.0) * 255.0);
    uint g_bin = (uint)(clamp(color.g, 0.0, 1.0) * 255.0);
    uint b_bin = (uint)(clamp(color.b, 0.0, 1.0) * 255.0);
    uint a_bin = (uint)(clamp(color.a, 0.0, 1.0) * 255.0);
    if ((channel_mask & 1u) != 0u) { atomic_fetch_add_explicit(&hist[r_bin], 1u, memory_order_relaxed); }
    if ((channel_mask & 2u) != 0u) { atomic_fetch_add_explicit(&hist[256u + g_bin], 1u, memory_order_relaxed); }
    if ((channel_mask & 4u) != 0u) { atomic_fetch_add_explicit(&hist[512u + b_bin], 1u, memory_order_relaxed); }
    if ((channel_mask & 8u) != 0u) { atomic_fetch_add_explicit(&hist[768u + a_bin], 1u, memory_order_relaxed); }
    atomic_fetch_add_explicit(total, 1u, memory_order_relaxed);
}

// CDF + 百分位桶归约（原 CPU 段下移 GPU，见模块 docstring）。
// 网格 (256, 4)：x = bin，y = 通道。pct_bins 布局：[ch*2]=low 原值，
// [ch*2+1]=high 的 (255-bin) 编码；两者 atomic_fetch_min，初值哨兵。
kernel void histogram_cdf(device const uint32_t* hist [[buffer(0)]],   // 1024
                          device float*       cdf [[buffer(1)]],      // 1024
                          device atomic_uint* pct_bins [[buffer(2)]], // 8
                          constant PercentileParams& p [[buffer(3)]],
                          uint2 gid [[thread_position_in_grid]]) {
    uint bin = gid.x;
    uint ch = gid.y;
    if (bin >= 256u || ch >= 4u) { return; }
    if ((p.channel_mask & (1u << ch)) == 0u) { return; }
    // AUD-1418: 原实现对全通道 256 桶做两次独立线性扫描（channel_total
    // 一次、cum ≤bin 一次），(256,4) 网格共 ~520k 次冗余桶读。合并为
    // 单次扫描，一次遍历同时累加 channel_total 与 bin 前缀和 cum。
    uint32_t channel_total = 0u;
    uint32_t cum = 0u;
    for (uint i = 0u; i < 256u; i++) {
        uint32_t count = hist[ch * 256u + i];
        channel_total += count;
        if (i <= bin) { cum += count; }
    }
    if (channel_total == 0u) { return; }
    float c = (float)cum / (float)channel_total;
    cdf[ch * 256u + bin] = c;
    if (c >= p.low_percentile) {
        atomic_fetch_min_explicit(&pct_bins[ch * 2u], bin, memory_order_relaxed);
    }
    if (c <= p.high_percentile) {
        atomic_fetch_min_explicit(&pct_bins[ch * 2u + 1u], 255u - bin, memory_order_relaxed);
    }
}
"""

HISTOGRAM_TOTAL_BINS = 1024
HISTOGRAM_BINS_PER_CHANNEL = 256

PERCENTILE_PARAMS_DTYPE = np.dtype([
    ("low_percentile", "<f4"),
    ("high_percentile", "<f4"),
    ("channel_mask", "<u4"),
])


def histogram_reference(img: np.ndarray, channel_mask: int) -> tuple[np.ndarray, int]:
    """GLSL 装箱语义的 numpy 参考：返回 (hist[1024] uint32, total)。"""
    src = img.astype(np.float32)
    flat = src.reshape(-1, 4)
    hist = np.zeros(HISTOGRAM_TOTAL_BINS, dtype=np.uint32)
    total = int(flat.shape[0])
    for ch in range(4):
        if (channel_mask & (1 << ch)) == 0:
            continue
        bins = np.clip(flat[:, ch], 0.0, 1.0)
        bins = (bins * 255.0).astype(np.uint32)  # uint() 截断语义
        np.add.at(hist, ch * 256 + bins, 1)
    return hist, total


def cdf_reference(hist: np.ndarray, channel_mask: int) -> np.ndarray:
    """`HistogramManager._compute_cdf` 语义（按通道总数归一）。"""
    cdf = np.zeros((4, HISTOGRAM_BINS_PER_CHANNEL), dtype=np.float32)
    for ch in range(4):
        if (channel_mask & (1 << ch)) == 0:
            continue
        channel_hist = hist[ch * 256:(ch + 1) * 256]
        channel_total = int(channel_hist.sum())
        if channel_total > 0:
            cdf[ch] = np.cumsum(channel_hist).astype(np.float32) / channel_total
    return cdf


def percentile_bins_reference(cdf: np.ndarray, channel: int,
                              low_percentile: float,
                              high_percentile: float) -> tuple[int, int]:
    """`find_percentile_values` 的正向/反向扫描语义（返回解码后的桶）。

    无满足桶时返回 CPU 端点默认（low→0 / high→255），与 kernel 哨兵
    解码规则一致。
    """
    channel_cdf = cdf[channel]
    if channel_cdf.sum() == 0:
        return (0, 255)
    low_bin = 0
    high_bin = 255
    for i in range(256):
        if channel_cdf[i] >= low_percentile:
            low_bin = i
            break
    for i in range(255, -1, -1):
        if channel_cdf[i] <= high_percentile:
            high_bin = i
            break
    return (low_bin, high_bin)


def decode_percentile_bins(raw: np.ndarray, channel: int) -> tuple[int, int]:
    """解码 GPU 归约的原始桶（哨兵 → CPU 端点默认）。"""
    low_raw = int(raw[channel * 2])
    high_raw = int(raw[channel * 2 + 1])
    low_bin = 0 if low_raw == PERCENTILE_BIN_SENTINEL else low_raw
    high_bin = 255 if high_raw == PERCENTILE_BIN_SENTINEL else 255 - high_raw
    return (low_bin, high_bin)
