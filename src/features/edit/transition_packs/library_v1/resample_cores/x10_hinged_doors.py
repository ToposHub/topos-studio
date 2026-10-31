"""hinged_doors 生产核（L14/L15；暂存区再导出本模块，函数体与暂存交付逐字一致）。"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from numpy import ndarray

DEFAULTS: dict[str, Any] = {
    "hinge": "Both",   # Left | Both
    "perspective": 0.5,
    "shadow": 0.5,
}
HINGES = ("Left", "Both")

_WIDTH_EPS = 0.999


def _safe_f(p: Mapping[str, Any], key: str, default: float,
            lo: float, hi: float) -> float:
    try:
        v = float(p[key])
    except (KeyError, TypeError, ValueError):
        return default
    if v != v or abs(v) == float("inf"):
        return default
    return min(hi, max(lo, v))


def _bilinear(img: np.ndarray, u: np.ndarray) -> np.ndarray:
    """沿 x 的像素中心双线性（v 恒为原位行，无重采样），越界 clamp。"""
    w = img.shape[1]
    xf = np.clip(u * w - 0.5, 0.0, w - 1.0)
    x0 = np.floor(xf).astype(np.int64)
    x1 = np.minimum(x0 + 1, w - 1)
    fx = xf - x0
    if img.ndim == 3:
        fx = fx[..., None]
        take0 = np.take(img, x0, axis=1)
        take1 = np.take(img, x1, axis=1)
        return take0 * (1.0 - fx) + take1 * fx
    take0 = np.take(img, x0, axis=1)
    take1 = np.take(img, x1, axis=1)
    return take0 * (1.0 - fx) + take1 * fx


def _finalize(out: np.ndarray, ref_dtype: Any) -> ndarray:
    if ref_dtype == np.uint8:
        return np.clip(np.round(out), 0, 255).astype(np.uint8)
    return out.astype(np.float32)


def _door_source(m: np.ndarray, persp: float) -> np.ndarray:
    """门内归一坐标 m∈[0,1] → 源归一坐标：persp=0 精确线性 m；persp>0
    自由边（m=1）压缩加速（s(1)=1、s(0)=0 恒成立）。"""
    if persp <= 0.0:
        return m
    return m * (1.0 + persp * m) / (1.0 + persp)


def evaluate(a_img: ndarray, b_img: ndarray, progress: float,
             params: Mapping[str, Any] | None = None) -> ndarray:
    """双门 2D 投影压扁退出 + B 原位露出（统一核签名；详见模块 docstring）。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    hinge = str(p.get("hinge", "Both"))
    if hinge not in HINGES:
        raise ValueError(f"hinge 必须是 {HINGES}，收到 {hinge!r}")
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

    persp = _safe_f(p, "perspective", 0.5, 0.0, 1.0)
    shadow = _safe_f(p, "shadow", 0.5, 0.0, 1.0)
    shade = 1.0 - shadow * progress

    w = a.shape[1]
    us = (np.arange(w, dtype=np.float64) + 0.5) / w
    door_w = 1.0 - _WIDTH_EPS * progress  # 门剩余宽度（归一）

    if hinge == "Left":
        on_door = us <= door_w
        m = np.clip(us / door_w, 0.0, 1.0)
        src = _door_source(m, persp)
    else:
        half = 0.5 * door_w
        on_left = us <= half
        on_right = us >= 1.0 - half
        on_door = on_left | on_right
        m_left = np.clip(us / half, 0.0, 1.0)
        m_right = np.clip((1.0 - us) / half, 0.0, 1.0)
        src = np.where(on_left, _door_source(m_left, persp) * 0.5,
                       1.0 - _door_source(m_right, persp) * 0.5)

    a_door = _bilinear(a, src) * shade
    b_behind = _bilinear(b, us)
    if a.ndim == 3:
        sel = on_door[..., None]
    else:
        sel = on_door
    out = np.where(sel, a_door, b_behind)
    return _finalize(out, a_in.dtype)
