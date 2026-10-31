"""F15 watercolor —— 独立 CPU 数学参考（oracle）。

与 filters/watercolor/watercolor.tglsl 逐式同构（references/oracle_conventions.md），
**单 pass 近似**（真多 pass 迭代扩散需原生 renderer，README 声明）：
1. Sobel 亮度梯度（Edge_Ink_Outline 同式：8 邻域 1/2/4 加权 /8）：
   mag = |g|/8，边缘带掩码 em = smoothstep(0.02, 0.20, mag)；
2. 沿梯度反向 8 tap 有界扩散：tap j 位移 = (j+1)·diffusion·em px（≤8px），
   权重 w_j = 1−(j+0.5)/8，与中心（权重 1）平均 → bleed；
   |g| < 1e-6 时方向为 0（平坦区精确不变）；diffusion=0 位移为 0（精确不变）；
3. 边缘颜料聚边（"饱和度/亮度提升 ∝ edge_pigment"字面实现）：
   pig = (lb + (bleed−lb)·(1+edge_pigment·em))·(1+0.5·edge_pigment·em)，
   lb = luma(bleed)——色度绕 luma 放大后整体乘性提亮，增益有界 ≤2x/≤1.5x；
4. 程序纸纹（调暗+去饱和）：整数格点 murmur3 风格 uint32 乘法散列
   （fixtures/_lattice_hash 的 uint32 同式缩减，无 seed 项 = 固定 seed 0）
   的双线性 value noise（线性插值，格距 paper_scale px），n ∈ [0,1)：
   desat = mix(vec3(luma(pig)), pig, 1−0.25n)；papered = desat·(1−0.25n)；
5. Delta 混合：out = img + (papered − base)·amount；amount=0 精确恒等。

边界：双线性采样 + clamp-to-edge；无随机流（hash 全确定）。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)

EDGE_LO = 0.02
EDGE_HI = 0.20
GRAD_EPS = 1e-6
N_TAPS = 8
PAPER_DARK = 0.25    # 纸纹调暗上限
PAPER_DESAT = 0.25   # 纸纹去饱和上限

DEFAULTS: dict[str, Any] = {
    "diffusion": 0.5,
    "edge_pigment": 0.5,
    "paper_scale": 8.0,
    "amount": 1.0,
}


def _bilinear(field: np.ndarray, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
    """clamp-to-edge 双线性采样（像素中心 = 整数索引）。field (H,W,C)。"""
    h, w = field.shape[0], field.shape[1]
    ys = np.clip(ys, 0.0, h - 1.0)
    xs = np.clip(xs, 0.0, w - 1.0)
    y0 = np.floor(ys).astype(np.int64)
    x0 = np.floor(xs).astype(np.int64)
    y1 = np.minimum(y0 + 1, h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    fy = (ys - y0)[..., None]
    fx = (xs - x0)[..., None]
    return (field[y0, x0] * (1.0 - fx) * (1.0 - fy)
            + field[y0, x1] * fx * (1.0 - fy)
            + field[y1, x0] * (1.0 - fx) * fy
            + field[y1, x1] * fx * fy)


def _hash_u32(ix: np.ndarray, iy: np.ndarray) -> np.ndarray:
    """整数格点 murmur3 风格 uint32 乘法散列 → [0,1)（与 GLSL wc_hash 同式）。"""
    mask = np.uint32(0xFFFFFFFF)
    x = ix.astype(np.uint32)
    y = iy.astype(np.uint32)
    h = (x * np.uint32(0x9E3779B1)) ^ (y * np.uint32(0x85EBCA77))
    h ^= h >> np.uint32(13)
    h = h * np.uint32(0xC2B2AE3D)
    h ^= h >> np.uint32(16)
    return h.astype(np.float64) / 4294967296.0


def _paper_noise(yy: np.ndarray, xx: np.ndarray, cell: float) -> np.ndarray:
    """格距 cell px 的双线性 value noise（线性插值，fixtures 同式）。"""
    gy = yy / max(cell, 2.0)
    gx = xx / max(cell, 2.0)
    y0 = np.floor(gy).astype(np.int64)
    x0 = np.floor(gx).astype(np.int64)
    fy = gy - y0
    fx = gx - x0
    n00 = _hash_u32(x0, y0)
    n10 = _hash_u32(x0 + 1, y0)
    n01 = _hash_u32(x0, y0 + 1)
    n11 = _hash_u32(x0 + 1, y0 + 1)
    return (n00 * (1.0 - fx) * (1.0 - fy) + n10 * fx * (1.0 - fy)
            + n01 * (1.0 - fx) * fy + n11 * fx * fy)


def _smoothstep(a: float, b: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]

    amount = float(p["amount"])
    if amount < 0.001:
        return img.astype(np.float32)

    diffusion = float(p["diffusion"])
    edge_pigment = float(p["edge_pigment"])
    paper_scale = float(p["paper_scale"])

    yy, xx = np.meshgrid(np.arange(h, dtype=np.float64),
                         np.arange(w, dtype=np.float64), indexing="ij")
    luma = img @ LUMA_W

    # ---- Sobel 亮度梯度（edge 边界 pad，/8 归一）----
    def shift(a: np.ndarray, dy: int, dx: int) -> np.ndarray:
        """out[y,x] = a[clamp(y+dy), clamp(x+dx)]（±1 邻域，edge pad）。"""
        padded = np.pad(a, ((1, 1), (1, 1)), mode="edge")
        return padded[1 + dy:1 + dy + a.shape[0], 1 + dx:1 + dx + a.shape[1]]

    tl, tm, tr = shift(luma, -1, -1), shift(luma, -1, 0), shift(luma, -1, 1)
    ml, mr = shift(luma, 0, -1), shift(luma, 0, 1)
    bl, bm, br = shift(luma, 1, -1), shift(luma, 1, 0), shift(luma, 1, 1)
    gx = (tr + 2.0 * mr + br) - (tl + 2.0 * ml + bl)
    gy = (bl + 2.0 * bm + br) - (tl + 2.0 * tm + tr)
    mag = np.sqrt(gx * gx + gy * gy) / 8.0

    em = _smoothstep(EDGE_LO, EDGE_HI, mag)
    norm = np.where(mag > GRAD_EPS, mag * 8.0, 1.0)
    dir_y = np.where(mag > GRAD_EPS, gy / norm, 0.0)
    dir_x = np.where(mag > GRAD_EPS, gx / norm, 0.0)

    # ---- 沿梯度反向 8 tap 有界扩散 ----
    acc = img.astype(np.float64).copy()
    wsum = np.full((h, w), 1.0)
    for j in range(N_TAPS):
        dpx = (j + 1.0) * diffusion * em
        s = _bilinear(img, yy - dir_y * dpx, xx - dir_x * dpx)
        w = 1.0 - (j + 0.5) / N_TAPS
        acc += s * w
        wsum += w
    bleed = acc / wsum[..., None]

    # ---- 边缘颜料聚边（饱和度/亮度提升，字面实现）----
    lb = bleed @ LUMA_W
    pig = (lb[..., None] + (bleed - lb[..., None]) * (1.0 + edge_pigment * em[..., None])) \
        * (1.0 + 0.5 * edge_pigment * em[..., None])

    # ---- 程序纸纹：调暗 + 去饱和 ----
    n = _paper_noise(yy + 0.5, xx + 0.5, paper_scale)
    lp = pig @ LUMA_W
    desat = lp[..., None] + (pig - lp[..., None]) * (1.0 - PAPER_DESAT * n[..., None])
    papered = desat * (1.0 - PAPER_DESAT * n[..., None])

    return (img + (papered - img) * amount).astype(np.float32)
