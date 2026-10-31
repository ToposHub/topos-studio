"""F04 zoned_tint —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
scene-linear、不 import 宿主渲染/GPU 代码（禁自证）、无随机流。

与 filters/zoned_tint/zoned_tint.tglsl 逐式同构：
    luma = max(dot(color, W), 0);  t = luma / (luma + 0.18)
    s_mid  = smoothstep(sp - sw/2, sp + sw/2, t)     # sw = max(shadow_width, 1e-4)
    s_high = smoothstep(hp - hw/2, hp + hw/2, t)     # hw 同式
    w_shadow = 1 - s_mid;  w_high = s_high;  w_mid = clamp(1 - w_shadow - w_high, 0, 1)
    gain = Π mix(1, tint·2, w_zone·strength_zone)
    out = color · gain
三强度 <0.001 早退恒等；tint 全 0.5 → gain=1 精确恒等。
"""

from __future__ import annotations

from typing import Any, Mapping, Tuple

import numpy as np

LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)
TONE_PIVOT = 0.18

DEFAULTS: dict[str, Any] = {
    "shadow_color": (0.5, 0.5, 0.5),
    "midtone_color": (0.5, 0.5, 0.5),
    "highlight_color": (0.5, 0.5, 0.5),
    "shadow_strength": 0.35,
    "midtone_strength": 0.30,
    "highlight_strength": 0.35,
    "shadow_pivot": 0.25,
    "highlight_pivot": 0.75,
    "shadow_width": 0.30,
    "highlight_width": 0.30,
}


def smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def tone_coordinate(luma: np.ndarray) -> np.ndarray:
    luma = np.maximum(luma, 0.0)
    return luma / (luma + TONE_PIVOT)


def zone_weights(t: np.ndarray, shadow_pivot: float, highlight_pivot: float,
                 shadow_width: float, highlight_width: float
                 ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """三区权重 (w_shadow, w_midtone, w_highlight)。"""
    sw = max(shadow_width, 1e-4)
    hw = max(highlight_width, 1e-4)
    s_mid = smoothstep(shadow_pivot - 0.5 * sw, shadow_pivot + 0.5 * sw, t)
    s_high = smoothstep(highlight_pivot - 0.5 * hw, highlight_pivot + 0.5 * hw, t)
    w_shadow = 1.0 - s_mid
    w_highlight = s_high
    w_midtone = np.clip(1.0 - w_shadow - w_highlight, 0.0, 1.0)
    return w_shadow, w_midtone, w_highlight


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    """对 (..., 3) RGB 数组应用三区着色，返回 float32。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img64 = np.asarray(image, dtype=np.float64)

    if (float(p["shadow_strength"]) < 0.001 and float(p["midtone_strength"]) < 0.001
            and float(p["highlight_strength"]) < 0.001):
        return np.asarray(image, dtype=np.float32)

    luma = img64 @ LUMA_W
    t = tone_coordinate(luma)
    w_s, w_m, w_h = zone_weights(t, float(p["shadow_pivot"]), float(p["highlight_pivot"]),
                                 float(p["shadow_width"]), float(p["highlight_width"]))

    tint_s = np.asarray(p["shadow_color"], dtype=np.float64)
    tint_m = np.asarray(p["midtone_color"], dtype=np.float64)
    tint_h = np.asarray(p["highlight_color"], dtype=np.float64)

    # 逐区 mix(1, tint*2, w*strength) 连乘（mix 展开：1 + (tint*2-1)·w·s）
    gain = np.ones_like(img64)
    for tint, w, s in ((tint_s, w_s, float(p["shadow_strength"])),
                       (tint_m, w_m, float(p["midtone_strength"])),
                       (tint_h, w_h, float(p["highlight_strength"]))):
        gain *= 1.0 + (tint * 2.0 - 1.0) * (w * s)[..., None]

    out = img64 * gain
    return out.astype(np.float32)
