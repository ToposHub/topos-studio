"""F12 palette_dither —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
scene-linear、不 import 宿主渲染/GPU 代码（禁自证）、无随机流。

与 filters/palette_dither/palette_dither.tglsl 逐式同构：
    probe = clamp(color, 0, 1)                    # 显示域策略（显式声明）
    dither: phase = (bayer[y%4][x%4]+0.5)/16 - 0.5
            Δ = (max4(p)-min4(p)) / 3             # 逐通道
            probe += phase·strength·Δ
    best = argmin_i ‖probe - palette_i‖²          # 平局取低索引
    out = color + (best - color)·strength         # strength<0.001 早退
位置相关（Bayer 相位）：evaluate 接受可选 pixel_xy（(H,W,2) 像素坐标），
缺省为网格索引 (y, x)——与 GLSL 的 uv·u_tex_size 口径一致。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

import numpy as np

BAYER4 = np.array([
    [0, 8, 2, 10],
    [12, 4, 14, 6],
    [3, 11, 1, 9],
    [15, 7, 13, 5],
], dtype=np.int64)

DEFAULTS: dict[str, Any] = {
    "palette_0": (0.03, 0.03, 0.06),
    "palette_1": (0.35, 0.12, 0.16),
    "palette_2": (0.80, 0.48, 0.22),
    "palette_3": (0.96, 0.95, 0.90),
    "dither_mode": "None",
    "strength": 1.0,
}


def bayer_phase(pixel_xy: np.ndarray) -> np.ndarray:
    """像素坐标 → Bayer 4x4 相位 φ=(b+0.5)/16−0.5。"""
    ix = np.mod(np.floor(pixel_xy[..., 1]).astype(np.int64), 4)  # x 列
    iy = np.mod(np.floor(pixel_xy[..., 0]).astype(np.int64), 4)  # y 行
    return (BAYER4[iy, ix].astype(np.float64) + 0.5) / 16.0 - 0.5


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None,
             pixel_xy: Optional[np.ndarray] = None) -> np.ndarray:
    """对 RGB 数组应用调色板量化，返回 float32。

    image: (H, W, 3) 图像或 (N, 3) 像素列表（后者内部按单行处理）。
    pixel_xy: (H, W, 2) 像素坐标网格（行 y、列 x）、(N, 2) 像素列表或
    单像素 (2,)；缺省为 np.indices 网格。与 GLSL 的 uv·u_tex_size 口径一致。
    """
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img64 = np.asarray(image, dtype=np.float64)
    squeeze = img64.ndim == 2
    if squeeze:
        img64 = img64[None, ...]  # (1, N, 3)
    strength = float(p["strength"])
    if strength < 0.001:
        out = np.asarray(image, dtype=np.float32)
        return out

    h, w = img64.shape[:2]
    if pixel_xy is None:
        yy, xx = np.indices((h, w))
        pixel_xy = np.stack([yy, xx], axis=-1)
    else:
        pixel_xy = np.asarray(pixel_xy, dtype=np.float64)
        if pixel_xy.ndim == 1:                      # 单像素 (y, x)
            pixel_xy = pixel_xy[None, None, :]
        elif pixel_xy.ndim == 2 and pixel_xy.shape == (h, w):
            pixel_xy = np.stack([pixel_xy, np.zeros_like(pixel_xy)], axis=-1)
        elif pixel_xy.ndim == 2 and pixel_xy.shape[-1] == 2:
            pixel_xy = pixel_xy[None, ...]          # (N,2) → (1,N,2)
    if pixel_xy.shape != (h, w, 2):
        raise ValueError(f"pixel_xy 形状 {pixel_xy.shape} 与图像 {(h, w)} 不匹配")

    palette = np.stack([np.asarray(p[f"palette_{i}"], dtype=np.float64)
                        for i in range(4)], axis=0)  # (4, 3)

    probe = np.clip(img64, 0.0, 1.0)
    if str(p["dither_mode"]) == "Ordered 4x4":
        lo = palette.min(axis=0)
        hi = palette.max(axis=0)
        delta = (hi - lo) / 3.0
        phase = bayer_phase(pixel_xy)  # (H, W)
        probe = probe + (phase * strength)[..., None] * delta

    # 最近色（平方欧氏距离），平局取低索引（np.argmin 语义）
    dists = ((probe[..., None, :] - palette[None, None, :, :]) ** 2).sum(axis=-1)
    best_idx = np.argmin(dists, axis=-1)              # (H, W)
    best = palette[best_idx]                          # (H, W, 3)

    out = img64 + (best - img64) * strength
    if squeeze:
        out = out[0]
    return out.astype(np.float32)
