"""C21 frequency_separation —— 独立 CPU 数学参考（oracle，双输出）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
无随机流、防御回退。L00 台账 C21 行：无双彩色输出节点（mask_merge 的
result+matte 是彩色+遮罩先例，非 low/high 双彩色）——**首个多输出
子门**；**high 允许负值**（不 clamp）；**不是 Sharpen 预设**
（low+high 在声明域精确重建输入—— Sharpen 无重建语义）。

语义（钉定口径）：
    low  = separable_blur(RGB, radius, filter)   （Box/Gaussian）
    high = RGB − low                              （带符号，允许负值，
                                                   不 clamp——验收点）
    Box：窗半宽 k = int(round(radius))，权重 1/(2k+1)，索引钳
         [0, n−1]（边缘纹素重复计入分母）
    Gaussian：σ = max(radius/3, 1e-3)、半宽 k = ceil(3σ)、
              权重 exp(−i²/(2σ²)) 归一化（全窗和=1），索引钳同上
    radius ≤ 0 → low = RGB、high = 0（精确恒等锚）
    α 透传（随 low 输出）；high 的 α = 0（高频不带 matte——
    频率分离语义：retouch 消费者只改 low/high RGB）
    mode：
      LowHigh     → low=low、high=high
      Reconstruct → low=low+high（声明域重建=输入）、high=high
                    （round-trip 验证模式；端口语义见 README）

多输出签名扩展：evaluate 返回 {"low": ndarray, "high": ndarray}
（convention #7 单返回的显式双输出扩展，README/manifest 记录）。
"""

from __future__ import annotations

import math

from typing import Any, Mapping

import numpy as np

FILTERS = ("Box", "Gaussian")
MODES = ("LowHigh", "Reconstruct")

DEFAULTS: dict[str, Any] = {
    "radius": 4.0,     # 像素
    "filter": "Box",
    "mode": "LowHigh",
}


def _separable_blur(field: np.ndarray, radius: float, filt: str) -> np.ndarray:
    """可分离盒/高斯模糊（索引钳 [0, n−1]）。field (H,W)。"""
    h, w = field.shape
    if radius <= 0.0:
        return field.copy()
    if filt == "Box":
        k = int(round(radius))
        half = np.arange(-k, k + 1)                      # int 像素偏移
        weights = np.ones(2 * k + 1, dtype=np.float64) / (2.0 * k + 1.0)
    else:   # Gaussian
        sigma = max(radius / 3.0, 1e-3)
        k = max(1, int(math.ceil(3.0 * sigma)))
        half = np.arange(-k, k + 1)                      # int 像素偏移
        weights = np.exp(-(half.astype(np.float64) ** 2) / (2.0 * sigma * sigma))
        weights = weights / weights.sum()
    ys, xs = np.indices((h, w))
    acc = np.zeros((h, w), dtype=np.float64)
    for off, wt in zip(half, weights):
        yy = np.clip(ys + off, 0, h - 1)     # 垂直核
        acc += field[yy, xs] * wt
    acc_x = np.zeros((h, w), dtype=np.float64)
    for off, wt in zip(half, weights):
        xx = np.clip(xs + off, 0, w - 1)     # 水平核
        acc_x += acc[ys, xx] * wt
    return acc_x


def evaluate(image: np.ndarray,
             params: Mapping[str, Any] | None = None) -> dict[str, np.ndarray]:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    filt = str(p["filter"])
    mode = str(p["mode"])
    if filt not in FILTERS:
        raise ValueError(f"filter 必须是 {list(FILTERS)}，收到 {filt!r}")
    if mode not in MODES:
        raise ValueError(f"mode 必须是 {list(MODES)}，收到 {mode!r}")
    radius = float(p["radius"])

    img = np.asarray(image, dtype=np.float64)
    rgb = img[..., :3]

    if radius <= 0.0:
        low_rgb = rgb.copy()
    else:
        low_rgb = np.stack(
            [_separable_blur(rgb[..., c], radius, filt) for c in range(3)],
            axis=-1)
    high_rgb = rgb - low_rgb            # 带符号，允许负值（不 clamp）

    alpha = img[..., 3:4] if img.shape[-1] == 4 else None
    low = np.concatenate([low_rgb, alpha], axis=-1) if alpha is not None else low_rgb
    high_a = np.zeros_like(alpha) if alpha is not None else None
    high = (np.concatenate([high_rgb, high_a], axis=-1)
            if high_a is not None else high_rgb)

    if mode == "Reconstruct":
        low = low_rgb + high_rgb        # 声明域重建 = 输入（round-trip 模式）
        if alpha is not None:
            low = np.concatenate([low, alpha], axis=-1)
    return {"low": low.astype(np.float32), "high": high.astype(np.float32)}
