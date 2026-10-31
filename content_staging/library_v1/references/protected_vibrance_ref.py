"""F03 protected_vibrance —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
scene-linear、不 import 宿主渲染/GPU 代码（禁自证）、无随机流。

与 filters/protected_vibrance/protected_vibrance.tglsl 逐式同构：
    luma = dot(color, W);  chroma = color - luma
    hue = hsv_hue_deg(color)            # max/min 比值法；无彩色 = -1
    d   = |mod(hue - center + 180, 360) - 180|
    protect = 1 - smoothstep(0.5w, w, d)   # w = max(width, 5)
    gain = 1 + saturation + vibrance·(1 - skin_protection·protect)
    out = luma + chroma·gain            # 亮度按构造精确保持
双滑杆 <0.001 早退恒等。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)

DEFAULTS: dict[str, Any] = {
    "saturation": 0.0,
    "vibrance": 0.0,
    "skin_hue_center": 20.0,
    "skin_hue_width": 25.0,
    "skin_protection": 1.0,
}


def hsv_hue_deg(rgb: np.ndarray) -> np.ndarray:
    """max/min 比值法 HSV hue（度）；无彩色（delta<1e-6）= -1。向量化。"""
    rgb = np.asarray(rgb, dtype=np.float64)
    cmax = rgb.max(axis=-1)
    cmin = rgb.min(axis=-1)
    delta = cmax - cmin
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    hue = np.full(r.shape, -1.0, dtype=np.float64)
    achromatic = delta < 1e-6
    # 分支互斥（cmax 与单通道相等时取第一分支；并列时与 GLSL 同序）
    m_r = (~achromatic) & (cmax == r)
    m_g = (~achromatic) & (cmax != r) & (cmax == g)
    m_b = (~achromatic) & (cmax != r) & (cmax != g)
    hue[m_r] = 60.0 * np.mod((g[m_r] - b[m_r]) / delta[m_r], 6.0)
    hue[m_g] = 60.0 * ((b[m_g] - r[m_g]) / delta[m_g] + 2.0)
    hue[m_b] = 60.0 * ((r[m_b] - g[m_b]) / delta[m_b] + 4.0)
    return hue


def protection_weight(hue: np.ndarray, center: float, width: float) -> np.ndarray:
    """肤色 hue 软窗权重（无彩色 hue<0 → 0）。"""
    weight = np.zeros(np.shape(hue), dtype=np.float64)
    valid = hue >= 0.0
    if valid.any():
        d = np.abs(np.mod(hue[valid] - center + 180.0, 360.0) - 180.0)
        w = max(width, 5.0)
        t = np.clip((d - 0.5 * w) / (w - 0.5 * w), 0.0, 1.0)
        weight[valid] = 1.0 - (t * t * (3.0 - 2.0 * t))
    return weight


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    """对 (..., 3) RGB 数组应用肤色保护自然饱和，返回 float32。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img64 = np.asarray(image, dtype=np.float64)

    saturation = float(p["saturation"])
    vibrance = float(p["vibrance"])
    if abs(saturation) < 0.001 and vibrance < 0.001:
        return np.asarray(image, dtype=np.float32)

    luma = img64 @ LUMA_W
    chroma = img64 - luma[..., None]

    hue = hsv_hue_deg(img64)
    protect = protection_weight(hue, float(p["skin_hue_center"]), float(p["skin_hue_width"]))

    gain = 1.0 + saturation + vibrance * (1.0 - float(p["skin_protection"]) * protect)
    out = luma[..., None] + chroma * gain[..., None]
    return out.astype(np.float32)
