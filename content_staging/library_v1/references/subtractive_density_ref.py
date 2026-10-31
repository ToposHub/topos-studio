"""F02 subtractive_density —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
scene-linear、不 import 宿主渲染/GPU 代码（禁自证）、无随机流。

与 filters/subtractive_density/subtractive_density.tglsl 逐式同构：
    δ = density · σ, σ = (0.85, 0.95, 1.10)
    A = (1-k)·I + k·C, C 行和 1（染料吸收串扰）
    D = A·δ, τ = exp(-D)
    raw = color · τ
    raw += W·(luma_in - dot(raw,W))/dot(W,W) · luma_protection   # 精确亮度补偿
    out = color + (raw - color) · amount                          # amount<0.001 早退
density=0 早退恒等。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)
LUMA_W_DOT = float(LUMA_W @ LUMA_W)  # ≈0.5619，精确补偿的分母
SIGMA = np.array([0.85, 0.95, 1.10], dtype=np.float64)

# 染料串扰矩阵（行和 1；行=颜色通道，列=染料层）
CROSS = np.array([
    [0.88, 0.08, 0.04],
    [0.10, 0.84, 0.06],
    [0.05, 0.15, 0.80],
], dtype=np.float64)

DEFAULTS: dict[str, Any] = {
    "density": 0.0,
    "dye_coupling": 0.35,
    "luma_protection": 0.0,
    "amount": 1.0,
}


def absorption_matrix(k: float) -> np.ndarray:
    return (1.0 - k) * np.eye(3) + k * CROSS


def transmission(density: float, k: float) -> np.ndarray:
    dye = density * SIGMA
    return np.exp(-(absorption_matrix(k) @ dye))


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    """对 (..., 3) RGB 数组应用减色密度，返回 float32。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img64 = np.asarray(image, dtype=np.float64)

    amount = float(p["amount"])
    density = float(p["density"])
    if amount < 0.001 or abs(density) < 0.001:
        return np.asarray(image, dtype=np.float32)

    k = min(max(float(p["dye_coupling"]), 0.0), 1.0)
    tau = transmission(density, k)
    raw = img64 * tau

    protection = float(p["luma_protection"])
    if protection > 0.001:
        luma_in = img64 @ LUMA_W
        luma_raw = raw @ LUMA_W
        raw = raw + LUMA_W * ((luma_in - luma_raw) / LUMA_W_DOT)[..., None] * protection

    out = img64 + (raw - img64) * amount
    return out.astype(np.float32)
