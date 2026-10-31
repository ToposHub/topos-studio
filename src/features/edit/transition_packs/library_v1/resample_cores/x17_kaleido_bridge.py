"""kaleido_bridge 生产核（L14/L15；暂存区再导出本模块，函数体与暂存交付逐字一致）。"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from numpy import ndarray

DEFAULTS: dict[str, Any] = {
    "sectors": 6,
    "rotation": 0.0,     # 度
    "center_x": 0.5,
    "center_y": 0.5,
    "strength": 0.8,
}


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
    """角域折叠桥接一帧（统一核签名；详见模块 docstring）。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
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

    sectors = _safe_i(p, "sectors", 6, 3, 12)
    rot_deg = _safe_f(p, "rotation", 0.0, -180.0, 180.0)
    cx = _safe_f(p, "center_x", 0.5, 0.0, 1.0)
    cy = _safe_f(p, "center_y", 0.5, 0.0, 1.0)
    strength = _safe_f(p, "strength", 0.8, 0.0, 1.0)

    h, w = a.shape[0], a.shape[1]
    us = (np.arange(w, dtype=np.float64) + 0.5) / w
    vs = (np.arange(h, dtype=np.float64) + 0.5) / h
    uu, vv = np.meshgrid(us, vs)
    aspect = float(w) / float(h)

    rx = (uu - cx) * aspect
    ry = vv - cy
    r = np.hypot(rx, ry)
    theta = np.arctan2(ry, rx)
    rot = np.deg2rad(rot_deg)
    sector = 2.0 * np.pi / sectors
    m = np.mod(theta - rot, 2.0 * sector)
    a_fold = np.minimum(m, 2.0 * sector - m)
    theta_f = rot + a_fold
    fold_u = cx + r * np.cos(theta_f) / aspect
    fold_v = cy + r * np.sin(theta_f)

    win = 1.0 - abs(2.0 * progress - 1.0)
    s_eff = strength * win
    layer_a = ((1.0 - s_eff) * _bilinear(a, uu, vv)
               + s_eff * _bilinear(a, fold_u, fold_v))
    layer_b = ((1.0 - s_eff) * _bilinear(b, uu, vv)
               + s_eff * _bilinear(b, fold_u, fold_v))
    w_b = min(1.0, max(0.0, 2.0 * progress - 1.0))
    out = (1.0 - w_b) * layer_a + w_b * layer_b
    return _finalize(out, a_in.dtype)
