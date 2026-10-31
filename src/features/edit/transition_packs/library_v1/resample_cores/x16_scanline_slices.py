"""scanline_slices 生产核（L14/L15；暂存区再导出本模块，函数体与暂存交付逐字一致）。"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from numpy import ndarray

DEFAULTS: dict[str, Any] = {
    "band_px": 8,
    "amount": 0.1,
    "seed": 0,
    "curve": "Linear",   # Linear | Ease
}
CURVES = ("Linear", "Ease")

_U64 = np.uint64
_MASK32 = _U64(0xFFFFFFFF)
_C1, _C2, _C3 = _U64(0x9E3779B1), _U64(0x85EBCA77), _U64(0xC2B2AE3D)
_C4, _C5 = _U64(0x2545F491), _U64(0x9E3779B1)


def _hash01(ix: int, iy: int, seed: int) -> float:
    """整数格点 → [0,1)（uint32 murmur3 风格，同 F07/fixtures 家族）。"""
    x = _U64(int(ix) & 0xFFFFFFFF)
    y = _U64(int(iy) & 0xFFFFFFFF)
    s = _U64(int(seed) & 0xFFFFFFFF)
    h = (x * _C1) ^ (y * _C2) ^ (s * _C3)
    h &= _MASK32
    h ^= h >> _U64(15)
    h = (h * _C4) & _MASK32
    h ^= h >> _U64(13)
    h = (h * _C5) & _MASK32
    h ^= h >> _U64(16)
    return float(h) / 4294967296.0


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


def _bilinear(img: np.ndarray, u: np.ndarray) -> np.ndarray:
    """沿 x 的像素中心双线性（每行独立位移，u 为 (H,W)），越界 clamp。"""
    h, w = img.shape[0], img.shape[1]
    xf = np.clip(u * w - 0.5, 0.0, w - 1.0)
    x0 = np.floor(xf).astype(np.int64)
    x1 = np.minimum(x0 + 1, w - 1)
    fx = xf - x0
    rows = np.arange(h, dtype=np.int64)[:, None]
    if img.ndim == 3:
        fx = fx[..., None]
    return img[rows, x0] * (1.0 - fx) + img[rows, x1] * fx


def _finalize(out: np.ndarray, ref_dtype: Any) -> ndarray:
    if ref_dtype == np.uint8:
        return np.clip(np.round(out), 0, 255).astype(np.uint8)
    return out.astype(np.float32)


def evaluate(a_img: ndarray, b_img: ndarray, progress: float,
             params: Mapping[str, Any] | None = None) -> ndarray:
    """分行带 UV 错位一帧（统一核签名；详见模块 docstring）。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    curve = str(p.get("curve", "Linear"))
    if curve not in CURVES:
        raise ValueError(f"curve 必须是 {CURVES}，收到 {curve!r}")
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

    band_px = _safe_i(p, "band_px", 8, 2, 64)
    amount = _safe_f(p, "amount", 0.1, 0.0, 0.25)
    seed = _safe_i(p, "seed", 0, 0, 100)

    h, w = a.shape[0], a.shape[1]
    rows = np.arange(h, dtype=np.int64)
    band_k = rows // band_px
    dir_k = np.array([1.0 if _hash01(int(k), 53, seed) >= 0.5 else -1.0
                      for k in band_k])

    w_lin = 1.0 - abs(2.0 * progress - 1.0)
    win = w_lin if curve == "Linear" else w_lin * w_lin
    shift = (dir_k * amount * win)[:, None]   # (H,1) 每行常量

    us = (np.arange(w, dtype=np.float64) + 0.5) / w
    u_grid = np.broadcast_to(us, (h, w)).copy()

    layer_a = _bilinear(a, u_grid + (1.0 - progress) * shift)
    layer_b = _bilinear(b, u_grid + progress * shift)
    out = (1.0 - progress) * layer_a + progress * layer_b
    return _finalize(out, a_in.dtype)
