"""C05 mosaic —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
无随机流。算法参照 plugins/Pixel_Blocks.tglsl 逐式同构（两段式）：

1) 马赛克离散（块中心采样）：像素中心坐标 px = (x+0.5, y+0.5)，
   块 id bid = floor(px / block)；采样位 = 块几何中心 bid·block +
   block/2（与 .tglsl 的 suv = (bid*block + 0.5*block)/safe_size 同值）。
   oracle 钉 **NEAREST** 口径：texel = floor(采样位)，钳到 [0, n−1]
   （GPU 侧输入若为 LINEAR 滤波，偶数块取相邻 texel 均值——对拍差异
   由 L02 GPU 实测时声明，此处显式记录）。
2) scene-linear 均匀阶梯量化：q = (floor(c·L + d) + 0.5) / L，对任意
   实数 c 单调、无界（HDR 负值/超白照常外推，不 clamp 内容色）；
   d 为 Bayer 4x4 有序抖动相位 (b+0.5)/16 − 0.5 ∈ [−0.5, 0.4375)，
   块内像素坐标 inner = px − bid·block，下标 int(min(inner, 3)) 钳制。

恒等语义（README 同步声明）：量化阶宽 1/L——block_px=1、dither=off
时 |out − in| ≤ 0.5/L，仅当 levels→∞ 才精确恒等；参数域 levels ≤ 32
（对齐 .tglsl 上限），故 neutral 取 levels=32 的**近似恒等**
（|out−in| ≤ 1/64），非精确恒等。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

# 4x4 Bayer 矩阵（与 Pixel_Blocks.tglsl PB_BAYER4 逐值同构）
BAYER4: np.ndarray = np.array([
    [0, 8, 2, 10],
    [12, 4, 14, 6],
    [3, 11, 1, 9],
    [15, 7, 13, 5],
], dtype=np.int64)

DEFAULTS: dict[str, Any] = {
    "block_px": 8.0,    # 1–64 px
    "levels": 8.0,      # 2–32 级
    "dither": False,    # Bayer 4x4 开关
}


def _block_center_index(px: np.ndarray, block: float, n: int) -> np.ndarray:
    """块中心采样下标：texel = floor(bid·block + block/2)，钳 [0, n−1]。"""
    bid = np.floor(px / block)
    center = bid * block + 0.5 * block
    return np.clip(np.floor(center).astype(np.int64), 0, n - 1)


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    block = max(float(p["block_px"]), 1.0)
    levels = max(float(p["levels"]), 2.0)
    dither = bool(p["dither"])

    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]
    yy = np.arange(h, dtype=np.float64) + 0.5
    xx = np.arange(w, dtype=np.float64) + 0.5

    sy = _block_center_index(yy, block, h)[:, None] * np.ones((1, w), dtype=np.int64)
    sx = _block_center_index(xx, block, w)[None, :] * np.ones((h, 1), dtype=np.int64)
    # 只取 RGB 参与量化：alpha 是透传通道（初版 img[sy, sx, :] 把第 4 通道
    # 一并量化，alpha 0.6 → 0.75 且输出变 5 通道——oracle 缺陷，已修）
    sampled = img[sy, sx, :3]

    dith = 0.0
    if dither:
        py = np.repeat(yy[:, None], w, axis=1)
        px = np.repeat(xx[None, :], h, axis=0)
        # .tglsl 取 PB_BAYER4[iy*4+ix]（行=inner.y、列=inner.x），下标 min(·,3) 钳制
        iy = np.minimum(py - np.floor(py / block) * block, 3.0).astype(np.int64)
        ix = np.minimum(px - np.floor(px / block) * block, 3.0).astype(np.int64)
        # (H,W) → (H,W,1)：与 (H,W,3) RGB 广播（初版 2D dith 与 3D 采样
        # 形状不兼容直接 ValueError——oracle 缺陷，已修）
        dith = ((BAYER4[iy, ix] + 0.5) / 16.0 - 0.5)[..., None]

    out_rgb = (np.floor(sampled * levels + dith) + 0.5) / levels
    if img.shape[-1] == 4:
        return np.concatenate([out_rgb, img[..., 3:4]], axis=-1).astype(np.float32)
    return out_rgb.astype(np.float32)
