"""F16 gradient_map —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
scene-linear、不 import 宿主渲染/GPU 代码（禁自证）、无随机流。

与 filters/gradient_map/gradient_map.tglsl 逐式同构：
    l = dot(color, (0.2126, 0.7152, 0.0722))
    t = clamp(l, 0, 1)                      # 查找坐标端点饱和（显式策略）
    分段（pos_0=0, pos_3=1 固定）：
      s = shape(clamp(t', 0, 1))，t' 为段内归一位置；等位置守护 max(width, 1e-4)
      shape: linear→t'；smooth→t'^2*(3-2t')
    mapped = mix(stop_i, stop_{i+1}, s)
    out = mix(color, mapped, amount)         # amount<0.001 早退恒等
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)

DEFAULTS: dict[str, Any] = {
    "stop_0": (0.02, 0.02, 0.05),
    "stop_1": (0.10, 0.20, 0.45),
    "stop_2": (0.90, 0.55, 0.25),
    "stop_3": (1.00, 0.98, 0.90),
    "pos_1": 0.33,
    "pos_2": 0.66,
    "interp_mode": "Linear",
    "amount": 1.0,
}


def _shape(t: np.ndarray | float, mode: str) -> np.ndarray | float:
    if mode == "Smooth":
        return t * t * (3.0 - 2.0 * t)
    return t


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    """对 (..., 3) RGB 数组应用梯度映射，返回 float32。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img64 = np.asarray(image, dtype=np.float64)
    luma = img64 @ LUMA_W
    t = np.clip(luma, 0.0, 1.0)

    s0 = np.asarray(p["stop_0"], dtype=np.float64)
    s1 = np.asarray(p["stop_1"], dtype=np.float64)
    s2 = np.asarray(p["stop_2"], dtype=np.float64)
    s3 = np.asarray(p["stop_3"], dtype=np.float64)
    pos1, pos2 = float(p["pos_1"]), float(p["pos_2"])
    mode = str(p["interp_mode"])

    seg1 = t <= pos1
    seg2 = (~seg1) & (t <= pos2)
    seg3 = ~(seg1 | seg2)

    mapped = np.empty_like(img64)
    if seg1.any():
        t1 = _shape(np.clip(t[seg1] / max(pos1, 1e-4), 0.0, 1.0), mode)
        mapped[seg1] = s0 + (s1 - s0) * np.asarray(t1)[:, None]
    if seg2.any():
        t2 = _shape(np.clip((t[seg2] - pos1) / max(pos2 - pos1, 1e-4), 0.0, 1.0), mode)
        mapped[seg2] = s1 + (s2 - s1) * np.asarray(t2)[:, None]
    if seg3.any():
        t3 = _shape(np.clip((t[seg3] - pos2) / max(1.0 - pos2, 1e-4), 0.0, 1.0), mode)
        mapped[seg3] = s2 + (s3 - s2) * np.asarray(t3)[:, None] if seg3.any() else mapped[seg3]

    amount = float(p["amount"])
    if amount < 0.001:
        return np.asarray(image, dtype=np.float32)
    out = img64 + (mapped - img64) * amount
    return out.astype(np.float32)
