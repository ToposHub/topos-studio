"""page_curl 生产核（L14/L15；暂存区再导出本模块，函数体与暂存交付逐字一致）。"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from numpy import ndarray

DEFAULTS: dict[str, Any] = {
    "direction": "Left",   # Left | Right
    "radius": 0.25,
    "back_color": (0.85, 0.82, 0.78),   # scene-linear RGB
    "shadow": 0.5,
}
DIRECTIONS = ("Left", "Right")


def _safe_f(p: Mapping[str, Any], key: str, default: float,
            lo: float, hi: float) -> float:
    try:
        v = float(p[key])
    except (KeyError, TypeError, ValueError):
        return default
    if v != v or abs(v) == float("inf"):
        return default
    return min(hi, max(lo, v))


def _safe_color(p: Mapping[str, Any], key: str,
                default: tuple[float, float, float]) -> np.ndarray:
    raw = p.get(key, default)
    try:
        vals = [float(c) for c in raw]
    except (TypeError, ValueError):
        return np.asarray(default, dtype=np.float64)
    if len(vals) != 3 or any(v != v or abs(v) == float("inf") for v in vals):
        return np.asarray(default, dtype=np.float64)
    return np.clip(np.asarray(vals, dtype=np.float64), 0.0, 1.0)


def _bilinear(img: np.ndarray, u: np.ndarray) -> np.ndarray:
    """沿 x 的像素中心双线性（行原位），越界 clamp。"""
    n = img.shape[1]
    xf = np.clip(u * n - 0.5, 0.0, n - 1.0)
    x0 = np.floor(xf).astype(np.int64)
    x1 = np.minimum(x0 + 1, n - 1)
    fx = xf - x0
    if img.ndim == 3:
        fx = fx[..., None]
    take0 = np.take(img, x0, axis=1)
    take1 = np.take(img, x1, axis=1)
    return take0 * (1.0 - fx) + take1 * fx


def _finalize(out: np.ndarray, ref_dtype: Any) -> ndarray:
    if ref_dtype == np.uint8:
        return np.clip(np.round(out), 0, 255).astype(np.uint8)
    return out.astype(np.float32)


def evaluate(a_img: ndarray, b_img: ndarray, progress: float,
             params: Mapping[str, Any] | None = None) -> ndarray:
    """非线性卷页一帧（统一核签名；详见模块 docstring）。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    direction = str(p.get("direction", "Left"))
    if direction not in DIRECTIONS:
        raise ValueError(f"direction 必须是 {DIRECTIONS}，收到 {direction!r}")
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

    radius = _safe_f(p, "radius", 0.25, 0.05, 0.5)
    shadow = _safe_f(p, "shadow", 0.5, 0.0, 1.0)
    back = _safe_color(p, "back_color", tuple(DEFAULTS["back_color"]))

    w = a.shape[1]
    us = (np.arange(w, dtype=np.float64) + 0.5) / w
    t = us if direction == "Left" else (1.0 - us)   # 规范坐标（卷向左）

    x_c = 1.0 - progress * (1.0 + radius)
    in_curl = (t >= x_c - radius) & (t <= x_c)
    is_back = t > x_c   # 已剥离区（B）

    # 平铺区：A 原位（规范坐标 → 源坐标）
    src_flat = t if direction == "Left" else 1.0 - t

    # 卷曲区：圆弧参数/弧长/源材质/背面混合权重
    phi = np.clip((x_c - t) / radius, 0.0, 1.0) * (np.pi / 2.0)
    arc_s = radius * (2.0 / np.pi) * phi
    t_src = x_c + arc_s
    src_curl = t_src if direction == "Left" else 1.0 - t_src
    w_back = np.sin(phi)
    shade = 1.0 - shadow * w_back

    flat_rgb = _bilinear(a, src_flat)
    curl_rgb = _bilinear(a, src_curl)
    if a.ndim == 3:
        back3 = back.reshape(1, 1, 3)
        curl_val = curl_rgb * (1.0 - w_back[..., None]) + back3 * w_back[..., None]
        curl_val = curl_val * shade[..., None]
    else:
        back1 = float(np.dot(back, np.array([0.2126, 0.7152, 0.0722])))
        curl_val = (curl_rgb * (1.0 - w_back) + back1 * w_back) * shade
    b_rgb = _bilinear(b, src_flat)   # B 原位（镜像向时采样回源坐标）

    if a.ndim == 3:
        out = np.where(is_back[..., None], b_rgb,
                       np.where(in_curl[..., None], curl_val, flat_rgb))
    else:
        out = np.where(is_back, b_rgb, np.where(in_curl, curl_val, flat_rgb))
    return _finalize(out, a_in.dtype)
