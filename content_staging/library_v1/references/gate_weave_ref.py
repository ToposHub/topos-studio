"""F08 gate_weave —— 独立 CPU 数学参考（oracle），静态版。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
scene-linear、不 import 宿主渲染/GPU 代码（禁自证）、无随机流。

与 filters/gate_weave/gate_weave.tglsl 逐式同构：
- dx = amplitude_h·(h32((11,7), seed) - 0.5)；dy = amplitude_v·(h32((23,5), seed) - 0.5)
  （uint32 murmur3 风格格点散列，同 F07 常量族；静态：同一 seed 恒同位移）；
- 像素 (x, y) 中心 p=(x+0.5, y+0.5)；v = p - 0.5·(W,H) - (dx,dy)·(W,H)；
  q = R(-θ)·v（θ=radians(rotation)，绕中心各向同性微旋转）；
  suv = (q + 0.5·(W,H)) / (W,H)；
- 双线性采样（像素中心制，索引钳 [0, n-1]，clamp-half-texel 同构）：
  Freeze：钳制采样；Black：suv 出 [0,1]² → 0；
- Delta 惯例 out = color + (sampled - base)；CPU 侧 color 与 base 同源
  （无上游调色分离），base = 自身像素 → out = sampled（Black 边界 = 0）。
- GLSL 精确零值早退（amplitude/rotation 全 0）在此镜像。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

_U64 = np.uint64
_MASK32 = _U64(0xFFFFFFFF)
_C1, _C2, _C3 = _U64(0x9E3779B1), _U64(0x85EBCA77), _U64(0xC2B2AE3D)
_C4, _C5 = _U64(0x2545F491), _U64(0x9E3779B1)

DEFAULTS: dict[str, Any] = {
    "amplitude_h": 0.004,
    "amplitude_v": 0.002,
    "rotation": 0.05,
    "seed": 7,
    "border_mode": "Freeze",
}


def h32_pair_consts(x: int, y: int, seed: int) -> float:
    """uint32 murmur3 风格散列 → [0,1)（与 tglsl gw_hash 逐位同构）。"""
    h = ((x * 0x9E3779B1) ^ (y * 0x85EBCA77) ^ (seed * 0xC2B2AE3D)) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 0x2545F491) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0x9E3779B1) & 0xFFFFFFFF
    h ^= h >> 16
    return h / 4294967296.0


def frame_offset(amplitude_h: float, amplitude_v: float, seed: int) -> tuple[float, float]:
    """整帧常量位移（静态：seed 决定，每帧相同）。"""
    dx = amplitude_h * (h32_pair_consts(11, 7, int(seed)) - 0.5)
    dy = amplitude_v * (h32_pair_consts(23, 5, int(seed)) - 0.5)
    return dx, dy


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
    """对 (H, W, 3) RGB 数组应用静态门片位移+微旋转，返回 float32。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img64 = np.asarray(image, dtype=np.float64)
    h, w = img64.shape[0], img64.shape[1]

    amp_h = float(p["amplitude_h"])
    amp_v = float(p["amplitude_v"])
    rot_deg = float(p["rotation"])
    if amp_h == 0.0 and amp_v == 0.0 and rot_deg == 0.0:
        return np.asarray(image, dtype=np.float32)

    dx, dy = frame_offset(amp_h, amp_v, int(float(p["seed"])))
    theta = np.deg2rad(rot_deg)
    cs = np.cos(theta)
    sn = np.sin(theta)

    ys, xs = np.meshgrid(np.arange(h, dtype=np.float64) + 0.5,
                         np.arange(w, dtype=np.float64) + 0.5, indexing="ij")
    px = xs
    py = ys
    cx, cy = w * 0.5, h * 0.5
    vx = px - cx - dx * w
    vy = py - cy - dy * h
    qx = vx * cs + vy * sn
    qy = -vx * sn + vy * cs
    suv_x = (qx + cx) / w
    suv_y = (qy + cy) / h

    black = str(p["border_mode"]) == "Black"
    sampled = _bilinear(img64, suv_x, suv_y)
    if black:
        outside = (suv_x < 0.0) | (suv_x > 1.0) | (suv_y < 0.0) | (suv_y > 1.0)
        if outside.any():
            sampled = np.where(outside[..., None], 0.0, sampled)
    # Delta 惯例：CPU 侧 base = 自身像素 → out = sampled
    return sampled.astype(np.float32)
