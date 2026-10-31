"""C10 kaleidoscope —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
无随机流。扇区折叠镜像采样（L00 台账 C10 行：Kaleido_Mirror 仅 2/4/8
重折叠，无扇区数旋转万花筒——本组件为差量新语义）：

- 中心：center 为 frame-normalized (u, v) ∈ [0,1]，映射到像素坐标
  cx = u·(W−1)、cy = v·(H−1)（像素中心 = 整数索引）；
- 方位角：θ = atan2(py − cy, px − cx) + radians(rotation)
  （rotation 逆时针旋转基准扇区，度）；
- 扇区折叠：扇区角 A = 2π/N（N=sectors，3–12）；a = θ mod A
  （正模，落 [0, A)）；镜像回基准扇区：f = a（a ≤ A/2）否则 f = A − a
  → f ∈ [0, A/2]。N 不整除 360 时折叠在接缝 a=0/a=A 处连续
  （两侧 f → 0），扇区边界 a=A/2 处亦连续（镜像 C0 对接）；
- 重采样：半径 r = hypot(dy, dx) 保持，采样位 = center + r·(cos f, sin f)；
  双线性 + clamp-to-edge（索引钳 [0, n−1]，对齐 manifest
  sampling_border=clamp-half-texel）；
- 混合：out = rgb + (sampled − rgb)·mix；mix=0 精确恒等（含 HDR）。
  基准扇区（θ ∈ [0, A/2]）内采样位即原位（浮点往返误差 ~1e-12，
  双线性权重 0 → 输出=输入）。

alpha（若存在）原样透传；不 clamp 内容色。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

DEFAULTS: dict[str, Any] = {
    "sectors": 6,          # 3–12
    "rotation": 0.0,       # −180–180 度
    "center": (0.5, 0.5),  # frame-normalized (u, v)
    "amount": 1.0,         # 0 恒等（mix 为 GLSL 保留字，F16/F19 先例改名）
}

_MIN_SECTORS = 3
_MAX_SECTORS = 12


def _bilinear(field: np.ndarray, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
    """clamp-to-edge 双线性采样。field (H,W) 或 (H,W,C)；像素中心=整数索引。"""
    h, w = field.shape[0], field.shape[1]
    ys = np.clip(ys, 0.0, float(h - 1))
    xs = np.clip(xs, 0.0, float(w - 1))
    y0 = np.floor(ys).astype(np.int64)
    x0 = np.floor(xs).astype(np.int64)
    y1 = np.minimum(y0 + 1, h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    fy = ys - y0
    fx = xs - x0
    if field.ndim == 3:
        fy = fy[..., None]
        fx = fx[..., None]
    return (field[y0, x0] * (1.0 - fx) * (1.0 - fy)
            + field[y0, x1] * fx * (1.0 - fy)
            + field[y1, x0] * (1.0 - fx) * fy
            + field[y1, x1] * fx * fy)


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    sectors = int(p["sectors"])
    sectors = max(_MIN_SECTORS, min(sectors, _MAX_SECTORS))
    rotation = float(p["rotation"])
    cu, cv = float(p["center"][0]), float(p["center"][1])
    amount = float(p["amount"])

    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]

    if amount < 0.001:
        return img.astype(np.float32)

    cx = cu * (w - 1)
    cy = cv * (h - 1)
    sector_angle = 2.0 * np.pi / sectors

    yy, xx = np.meshgrid(np.arange(h, dtype=np.float64),
                         np.arange(w, dtype=np.float64), indexing="ij")
    dy = yy - cy
    dx = xx - cx
    theta = np.arctan2(dy, dx) + np.deg2rad(rotation)

    a = theta - sector_angle * np.floor(theta / sector_angle)  # 正模 → [0, A)
    half = 0.5 * sector_angle
    folded = np.where(a <= half, a, sector_angle - a)          # 镜像回 [0, A/2]

    r = np.hypot(dx, dy)
    sx = cx + r * np.cos(folded)
    sy = cy + r * np.sin(folded)
    sampled = _bilinear(img[..., :3], sy, sx)

    out_rgb = img[..., :3] + (sampled - img[..., :3]) * amount
    if img.shape[-1] == 4:
        return np.concatenate([out_rgb, img[..., 3:4]], axis=-1).astype(np.float32)
    return out_rgb.astype(np.float32)
