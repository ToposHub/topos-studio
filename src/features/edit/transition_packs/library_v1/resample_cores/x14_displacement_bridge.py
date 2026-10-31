"""displacement_bridge 生产核（L14/L15；暂存区再导出本模块，函数体与暂存交付逐字一致）。"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from numpy import ndarray

DEFAULTS: dict[str, Any] = {
    "scale": 0.1,
    "frequency": 4.0,
    "seed": 0,
    "mode": "Wave",   # Wave | Cell
}
MODES = ("Wave", "Cell")

_U64 = np.uint64
_MASK32 = _U64(0xFFFFFFFF)
_C1, _C2, _C3 = _U64(0x9E3779B1), _U64(0x85EBCA77), _U64(0xC2B2AE3D)
_C4, _C5 = _U64(0x2545F491), _U64(0x9E3779B1)


def _lattice_u32(ix: np.ndarray, iy: np.ndarray, seed: int) -> np.ndarray:
    """整数格点 → [0,1)（uint32 murmur3 风格，同 F07/fixtures 家族）。"""
    x = ix.astype(np.int64).astype(_U64) & _MASK32
    y = iy.astype(np.int64).astype(_U64) & _MASK32
    s = _U64(int(seed) & 0xFFFFFFFF)
    h = (x * _C1) ^ (y * _C2) ^ (s * _C3)
    h &= _MASK32
    h ^= h >> _U64(15)
    h = (h * _C4) & _MASK32
    h ^= h >> _U64(13)
    h = (h * _C5) & _MASK32
    h ^= h >> _U64(16)
    return h.astype(np.float64) / 4294967296.0


def _value_noise(gx: np.ndarray, gy: np.ndarray, seed: int) -> np.ndarray:
    """4-tap 平滑 value noise ∈ [0,1)（F07 同式）。"""
    x0 = np.floor(gx)
    y0 = np.floor(gy)
    fx = gx - x0
    fy = gy - y0
    sx = fx * fx * (3.0 - 2.0 * fx)
    sy = fy * fy * (3.0 - 2.0 * fy)
    xi = x0.astype(np.int64)
    yi = y0.astype(np.int64)
    n00 = _lattice_u32(xi, yi, seed)
    n10 = _lattice_u32(xi + 1, yi, seed)
    n01 = _lattice_u32(xi, yi + 1, seed)
    n11 = _lattice_u32(xi + 1, yi + 1, seed)
    a = n00 + (n10 - n00) * sx
    b = n01 + (n11 - n01) * sx
    return a + (b - a) * sy


def _safe_f(p: Mapping[str, Any], key: str, default: float,
            lo: float, hi: float) -> float:
    try:
        v = float(p[key])
    except (KeyError, TypeError, ValueError):
        return default
    if v != v or abs(v) == float("inf"):
        return default
    return min(hi, max(lo, v))


def _safe_i(p: Mapping[str, Any], key: str, default: int,
            lo: int, hi: int) -> int:
    try:
        v = int(round(float(p[key])))
    except (KeyError, TypeError, ValueError, OverflowError):
        return default
    return min(hi, max(lo, v))


def _bilinear(img: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """像素中心制双线性，越界 clamp（X09 惯例）。"""
    h, w = img.shape[0], img.shape[1]
    xf = np.clip(u * w - 0.5, 0.0, w - 1.0)
    yf = np.clip(v * h - 0.5, 0.0, h - 1.0)
    x0 = np.floor(xf).astype(np.int64)
    y0 = np.floor(yf).astype(np.int64)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = xf - x0
    fy = yf - y0
    if img.ndim == 3:
        fx = fx[..., None]
        fy = fy[..., None]
    top = img[y0, x0] * (1.0 - fx) + img[y0, x1] * fx
    bot = img[y1, x0] * (1.0 - fx) + img[y1, x1] * fx
    return top * (1.0 - fy) + bot * fy


def _finalize(out: np.ndarray, ref_dtype: Any) -> ndarray:
    if ref_dtype == np.uint8:
        return np.clip(np.round(out), 0, 255).astype(np.uint8)
    return out.astype(np.float32)


def evaluate(a_img: ndarray, b_img: ndarray, progress: float,
             params: Mapping[str, Any] | None = None) -> ndarray:
    """双源程序噪声位移桥接一帧（统一核签名；详见模块 docstring）。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    mode = str(p.get("mode", "Wave"))
    if mode not in MODES:
        raise ValueError(f"mode 必须是 {MODES}，收到 {mode!r}")
    a_in = np.asarray(a_img)
    b_in = np.asarray(b_img)
    if a_in.shape != b_in.shape:
        raise ValueError(
            f"双源尺寸不一致: {a_in.shape} vs {b_in.shape}")
    if a_in.ndim not in (2, 3):
        raise ValueError(f"输入须为 (H,W) 或 (H,W,C)，收到 ndim={a_in.ndim}")
    a = a_in.astype(np.float64)
    b = b_in.astype(np.float64)
    try:
        progress = float(progress)
    except (TypeError, ValueError):
        progress = 0.0
    if progress != progress:
        progress = 0.0
    progress = max(0.0, min(1.0, progress))
    if progress <= 0.0:
        return _finalize(a, a_in.dtype)
    if progress >= 1.0:
        return _finalize(b, b_in.dtype)

    scale = _safe_f(p, "scale", 0.1, 0.0, 0.25)
    freq = _safe_f(p, "frequency", 4.0, 1.0, 16.0)
    seed = _safe_i(p, "seed", 0, 0, 100)

    h, w = a.shape[0], a.shape[1]
    us = (np.arange(w, dtype=np.float64) + 0.5) / w
    vs = (np.arange(h, dtype=np.float64) + 0.5) / h
    uu, vv = np.meshgrid(us, vs)

    if mode == "Cell":
        gx = np.floor(uu * freq).astype(np.int64)
        gy = np.floor(vv * freq).astype(np.int64)
        nx = _lattice_u32(gx, gy, seed) * 2.0 - 1.0
        ny = _lattice_u32(gx, gy, seed + 1001) * 2.0 - 1.0
    else:
        nx = _value_noise(uu * freq, vv * freq, seed) * 2.0 - 1.0
        ny = _value_noise(uu * freq, vv * freq, seed + 1001) * 2.0 - 1.0

    win = 1.0 - abs(2.0 * progress - 1.0)   # 三角窗：两端 0、p=0.5 峰值 1
    dx = scale * win * nx
    dy = scale * win * ny

    layer_a = _bilinear(a, uu + (1.0 - progress) * dx, vv + (1.0 - progress) * dy)
    layer_b = _bilinear(b, uu + progress * dx, vv + progress * dy)
    if a.ndim == 3:
        out = (1.0 - progress) * layer_a + progress * layer_b
    else:
        out = (1.0 - progress) * layer_a + progress * layer_b
    return _finalize(out, a_in.dtype)
