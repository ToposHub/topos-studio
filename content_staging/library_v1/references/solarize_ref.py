"""F17 solarize —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
scene-linear、不 import 宿主渲染/GPU 代码（禁自证）、无随机流。

与 filters/solarize/solarize.tglsl 逐式同构：
    thr = max(threshold, 1e-4);  h = max(softness·thr, 1e-4)
    fold(v) = v·(1-m) + (2·thr−v)·m,  m = smoothstep(thr−h, thr+h, v)
    Luma 档: v = dot(color, W);  out = color + (fold(v) − v)
    RGB  档: 逐通道 fold
    out = color + (out_mode − color)·amount          # amount<0.001 早退
线性折返无 clamp（HDR 可折出负值，显式声明）。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)

DEFAULTS: dict[str, Any] = {
    "threshold": 1.0,
    "softness": 0.0,
    "channel_mode": "Luma",
    "amount": 1.0,
}


def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def fold(v: np.ndarray, thr: float, h: float) -> np.ndarray:
    """折返：m=smoothstep(thr-h, thr+h, v)；fold = mix(v, 2thr-v, m)。"""
    m = _smoothstep(thr - h, thr + h, v)
    return v * (1.0 - m) + (2.0 * thr - v) * m


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    """对 (..., 3) RGB 数组应用太阳化折返，返回 float32。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img64 = np.asarray(image, dtype=np.float64)

    amount = float(p["amount"])
    if amount < 0.001:
        return np.asarray(image, dtype=np.float32)

    thr = max(float(p["threshold"]), 1e-4)
    h = max(float(p["softness"]) * thr, 1e-4)

    if str(p["channel_mode"]) == "Luma":
        luma = img64 @ LUMA_W
        folded = fold(luma, thr, h)
        result = img64 + (folded - luma)[..., None]
    else:
        result = fold(img64, thr, h)

    out = img64 + (result - img64) * amount
    return out.astype(np.float32)
