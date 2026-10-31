"""C11 polar_coordinates —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
无随机流、防御回退（缺通道按 0 填充）。L00 台账 C11 行：全库无极坐标
变换——本组件为新算法差量（Cartesian↔polar UV 重映射）。

坐标约定（与 C10 kaleidoscope_ref 同构）：像素中心 = 整数索引；
center 为 frame-normalized (u, v)，映射 cx = u·(W−1)、cy = v·(H−1)。
半径归一化 r_max = radius_scale · min(W, H) / 2。

两方向为**同一映射 F 的正/逆**（voluntary involutive pair）：
    F:  (θ, r)  ↔  (u, v)，θ = atan2(dy, dx)，r = hypot(dx, dy)，
        u = 0.5 + θ/span_rad，v = r / r_max        （span_rad = radians(angle_span)）

- CartesianToPolar（正向）：输出像素 (x,y) 的极坐标 (θ, r) → 采样输入
  (u, v)（输入 x 轴=角度、y 轴=半径；矩形图卷成极坐标视图）；
- PolarToCartesian（逆向，F⁻¹）：输出像素的 frame 归一化坐标 (uq, vq)
  → θ = span_rad·(uq − 0.5)、r = vq·r_max → 采样输入位于
  center + r·(cos θ, sin θ)（极坐标图展开回矩形）。
  连续域上 F⁻¹∘F = 恒等 **on 开域 v > 0**；v = 0 行（半径零奇点：
  uq ≠ 0.5 的点在 F 下无原像）按公式塌缩到 center——中心奇点的
  固有退化，非缺陷（测试如实钉住）。

角度跨度 angle_span（度）：输入 x 轴覆盖的角域宽度；span < 360 时
|θ| > span/2 的输出落在 [0,1] 外 → 由采样边界钳制（clamp）取边缘。
接缝：θ = ±π 处 u 跳变（Nearest 硬缝 / Linear 在钳制边缘插值衰减）；
中心奇点：r → 0 时 v → 0（采样 y 轴顶行）。α 通道同样参与重映射
（整图重定位）；域外/越界由 clamp-half-texel（索引钳 [0, n−1]）兜底，
不 clamp 内容色（HDR 透传）。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

MODES = ("CartesianToPolar", "PolarToCartesian")
FILTERS = ("Nearest", "Linear")

DEFAULTS: dict[str, Any] = {
    "mode": "CartesianToPolar",
    "center": (0.5, 0.5),   # frame-normalized (u, v)
    "angle_span": 360.0,    # 度；输入 x 轴覆盖的角域宽度
    "radius_scale": 1.0,    # r_max = radius_scale · min(W,H)/2
    "filter": "Nearest",    # oracle 钉 Nearest；Linear 双线性同构实现
}


def _sample(field: np.ndarray, ys: np.ndarray, xs: np.ndarray,
            filt: str) -> np.ndarray:
    """clamp-to-edge 采样（像素中心 = 整数索引）。field (H,W[,C])。"""
    h, w = field.shape[0], field.shape[1]
    ys = np.clip(ys, 0.0, float(h - 1))
    xs = np.clip(xs, 0.0, float(w - 1))
    if filt == "Nearest":
        yi = np.floor(ys + 0.5).astype(np.int64)
        xi = np.floor(xs + 0.5).astype(np.int64)
        return field[yi, xi]
    # Linear：clamp 双线性
    y0 = np.floor(ys).astype(np.int64)
    x0 = np.floor(xs).astype(np.int64)
    y1 = np.minimum(y0 + 1, h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    fy = (ys - y0)[..., None] if field.ndim == 3 else (ys - y0)
    fx = (xs - x0)[..., None] if field.ndim == 3 else (xs - x0)
    return (field[y0, x0] * (1.0 - fx) * (1.0 - fy)
            + field[y0, x1] * fx * (1.0 - fy)
            + field[y1, x0] * (1.0 - fx) * fy
            + field[y1, x1] * fx * fy)


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    mode = str(p["mode"])
    if mode not in MODES:
        raise ValueError(f"mode 必须是 {list(MODES)}，收到 {mode!r}")
    filt = str(p["filter"])
    if filt not in FILTERS:
        raise ValueError(f"filter 必须是 {list(FILTERS)}，收到 {filt!r}")
    cu, cv = float(p["center"][0]), float(p["center"][1])
    span_rad = np.deg2rad(float(p["angle_span"]))

    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]
    r_max = max(float(p["radius_scale"]), 1e-9) * 0.5 * min(w, h)
    if span_rad <= 0.0:
        span_rad = np.deg2rad(360.0)

    cx = cu * (w - 1)
    cy = cv * (h - 1)
    yy, xx = np.meshgrid(np.arange(h, dtype=np.float64),
                         np.arange(w, dtype=np.float64), indexing="ij")

    if mode == "CartesianToPolar":
        dx = xx - cx
        dy = yy - cy
        theta = np.arctan2(dy, dx)
        u = 0.5 + theta / span_rad
        v = np.hypot(dx, dy) / r_max
        sx = u * (w - 1)
        sy = v * (h - 1)
    else:  # PolarToCartesian：F⁻¹
        uq = xx / (w - 1) if w > 1 else np.zeros_like(xx, dtype=np.float64)
        vq = yy / (h - 1) if h > 1 else np.zeros_like(yy, dtype=np.float64)
        theta = span_rad * (uq - 0.5)
        rad = vq * r_max
        sx = cx + rad * np.cos(theta)
        sy = cy + rad * np.sin(theta)

    out = _sample(img, sy, sx, filt)
    return out.astype(np.float32)
