"""ripple_refraction 生产核（L14/L15；暂存区再导出本模块，函数体与暂存交付逐字一致）。"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from numpy import ndarray

DEFAULTS: dict[str, Any] = {
    "center_x": 0.5,
    "center_y": 0.5,
    "frequency": 8.0,
    "amplitude": 0.1,
    "damping": 0.3,
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
    """径向波场折射一帧（统一核签名；详见模块 docstring）。"""
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

    cx = _safe_f(p, "center_x", 0.5, 0.0, 1.0)
    cy = _safe_f(p, "center_y", 0.5, 0.0, 1.0)
    freq = _safe_f(p, "frequency", 8.0, 1.0, 32.0)
    amp = _safe_f(p, "amplitude", 0.1, 0.0, 0.25)
    damping = _safe_f(p, "damping", 0.3, 0.0, 1.0)

    h, w = a.shape[0], a.shape[1]
    us = (np.arange(w, dtype=np.float64) + 0.5) / w
    vs = (np.arange(h, dtype=np.float64) + 0.5) / h
    uu, vv = np.meshgrid(us, vs)
    aspect = float(w) / float(h)

    rx = (uu - cx) * aspect
    ry = vv - cy
    r = np.hypot(rx, ry)
    # r_max：中心到四角的最大（宽高比修正）距离——波前 p∈[0,1] 穿到最远角
    r_max = max(float(np.hypot((corner_x - cx) * aspect, corner_y - cy))
                for corner_x in (0.0, 1.0) for corner_y in (0.0, 1.0))

    dr = amp * np.sin(freq * (r - progress * r_max)) * np.exp(-damping * r)
    safe_r = np.where(r > 1e-12, r, 1.0)
    disp_u = np.where(r > 1e-12, dr * rx / safe_r / aspect, 0.0)
    disp_v = np.where(r > 1e-12, dr * ry / safe_r, 0.0)

    win = 1.0 - abs(2.0 * progress - 1.0)   # 三角窗：两端 0、p=0.5 峰值 1
    layer_a = _bilinear(a, uu + (1.0 - progress) * win * disp_u,
                        vv + (1.0 - progress) * win * disp_v)
    layer_b = _bilinear(b, uu + progress * win * disp_u,
                        vv + progress * win * disp_v)
    out = (1.0 - progress) * layer_a + progress * layer_b
    return _finalize(out, a_in.dtype)
