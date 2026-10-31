"""F01 wb_adaptation —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
scene-linear、不 import 宿主渲染/GPU 代码（禁自证）、无随机流。

与 filters/wb_adaptation/wb_adaptation.tglsl 逐式同构：
    ws = white_xyz(source_white)   # color → XYZ → xy → Y=1 白点
    wt = white_xyz(target_white)   # 非物理参数回退 D65 xy=(0.3127,0.3290)
    strength<0.001 或 distance(ws,wt)<1e-6 → 恒等
    Bradford:  M = Ainv · diag(rt/rs) · A    （A=Bradford 锥响应）
    XYZScaling: M = diag(wt/ws)
    T = XYZ2RGB @ M @ RGB2XYZ
    out = color + (T @ color - color) * strength
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

RGB2XYZ = np.array([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
], dtype=np.float64)

XYZ2RGB = np.array([
    [3.2404542, -1.5371385, -0.4985314],
    [-0.9692660, 1.8760108, 0.0415560],
    [0.0556434, -0.2040259, 1.0572252],
], dtype=np.float64)

BRADFORD = np.array([
    [0.8951, 0.2664, -0.1614],
    [-0.7502, 1.7135, 0.0367],
    [0.0389, -0.0685, 1.0296],
], dtype=np.float64)

BRADFORD_INV = np.array([
    [0.9869929, -0.1470543, 0.1599627],
    [0.4323053, 0.5183603, 0.0492912],
    [-0.0085287, 0.0400428, 0.9684867],
], dtype=np.float64)

D65_XY = (0.3127, 0.3290)

DEFAULTS: dict[str, Any] = {
    "source_white": (1.0, 1.0, 1.0),
    "target_white": (1.0, 1.0, 1.0),
    "adapt_model": "Bradford",
    "strength": 1.0,
}


def white_xyz(rgb: Any) -> np.ndarray:
    """color 白点参数 → XYZ → xy 色度 → Y=1 白点 XYZ（非物理回退 D65）。"""
    xyz = RGB2XYZ @ np.asarray(rgb, dtype=np.float64)
    s = float(xyz[0] + xyz[1] + xyz[2])
    if s < 1e-6 or xyz[1] < 1e-6:
        x, y = D65_XY
        return np.array([x / y, 1.0, (1.0 - x - y) / y], dtype=np.float64)
    x, y = xyz[0] / s, xyz[1] / s
    return np.array([x / y, 1.0, (1.0 - x - y) / y], dtype=np.float64)


def adaptation_matrix(ws: np.ndarray, wt: np.ndarray, model: str) -> np.ndarray:
    if model == "Bradford":
        rs = BRADFORD @ ws
        rt = BRADFORD @ wt
        return BRADFORD_INV @ np.diag(rt / rs) @ BRADFORD
    return np.diag(wt / ws)


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    """对 (..., 3) RGB 数组应用白点色适应，返回 float32。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img64 = np.asarray(image, dtype=np.float64)

    strength = float(p["strength"])
    if strength < 0.001:
        return np.asarray(image, dtype=np.float32)

    ws = white_xyz(p["source_white"])
    wt = white_xyz(p["target_white"])
    if float(np.linalg.norm(wt - ws)) < 1e-6:
        return np.asarray(image, dtype=np.float32)

    adapt = adaptation_matrix(ws, wt, str(p["adapt_model"]))
    transform = XYZ2RGB @ adapt @ RGB2XYZ
    adapted = img64 @ transform.T
    out = img64 + (adapted - img64) * strength
    return out.astype(np.float32)
