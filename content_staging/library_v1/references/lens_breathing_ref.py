"""F11 lens_breathing —— 独立 CPU 数学参考（oracle），静态版。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
scene-linear、不 import 宿主渲染/GPU 代码（禁自证）、无随机流。

与 filters/lens_breathing/lens_breathing.tglsl 逐式同构：
- s = 1 + amount·(scale - 1)；suv = pivot + (uv - pivot)/s（统一缩放，
  归一坐标等比 → 内容几何保持，无需宽高比校正；旋转 0）；
- 双线性采样（像素中心制，索引钳 [0, n-1]，clamp-half-texel 同构）：
  Freeze：钳制采样；Black：suv 出 [0,1]² → 0；
- Delta 惯例 out = color + (sampled - base)；CPU 侧 color 与 base 同源
  （无上游调色分离）→ out = sampled（Black 边界 = 0）。
- GLSL 精确零值早退（amount==0 或 scale==1）在此镜像。
动画走宿主既有 keyframe（参数可关键帧），oracle 只定义单帧静态语义。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

DEFAULTS: dict[str, Any] = {
    "amount": 1.0,
    "pivot_x": 0.5,
    "pivot_y": 0.5,
    "scale": 1.1,
    "border_mode": "Freeze",
}


def _bilinear(img: np.ndarray, sx: np.ndarray, sy: np.ndarray) -> np.ndarray:
    """像素中心制双线性，索引钳 [0, n-1]（clamp-half-texel 同构）。"""
    h, w = img.shape[0], img.shape[1]
    xf = np.clip(sx * w - 0.5, 0.0, w - 1.0)
    yf = np.clip(sy * h - 0.5, 0.0, h - 1.0)
    x0 = np.floor(xf).astype(np.int64)
    y0 = np.floor(yf).astype(np.int64)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (xf - x0)[..., None]
    fy = (yf - y0)[..., None]
    top = img[y0, x0] * (1.0 - fx) + img[y0, x1] * fx
    bot = img[y1, x0] * (1.0 - fx) + img[y1, x1] * fx
    return top * (1.0 - fy) + bot * fy


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    """对 (H, W, 3) RGB 数组应用呼吸缩放重采样，返回 float32。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img64 = np.asarray(image, dtype=np.float64)
    h, w = img64.shape[0], img64.shape[1]

    amount = float(p["amount"])
    scale = float(p["scale"])
    if amount == 0.0 or scale == 1.0:
        return np.asarray(image, dtype=np.float32)

    s = 1.0 + amount * (scale - 1.0)
    pv_x, pv_y = float(p["pivot_x"]), float(p["pivot_y"])

    ys, xs = np.meshgrid((np.arange(h, dtype=np.float64) + 0.5) / h,
                         (np.arange(w, dtype=np.float64) + 0.5) / w, indexing="ij")
    suv_x = pv_x + (xs - pv_x) / s
    suv_y = pv_y + (ys - pv_y) / s

    black = str(p["border_mode"]) == "Black"
    sampled = _bilinear(img64, suv_x, suv_y)
    if black:
        outside = (suv_x < 0.0) | (suv_x > 1.0) | (suv_y < 0.0) | (suv_y > 1.0)
        if outside.any():
            sampled = np.where(outside[..., None], 0.0, sampled)
    # Delta 惯例：CPU 侧 base = 自身像素 → out = sampled
    return sampled.astype(np.float32)
