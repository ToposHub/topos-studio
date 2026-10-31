"""cube_faces 生产核（L14/L15；暂存区再导出本模块，函数体与暂存交付逐字一致）。"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from numpy import ndarray

DEFAULTS: dict[str, Any] = {
    "axis": "Horizontal",   # Horizontal | Vertical
    "perspective": 0.5,
    "shadow": 0.5,
}
AXES = ("Horizontal", "Vertical")

_MIN_FACE = 1e-6  # cosθ/sinθ 下限（p=1 端点走 early return，仅防御）


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
    """沿单轴的像素中心双线性（另一轴原位），越界 clamp（X09 惯例）。"""
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


def _persp_a(m: np.ndarray, persp: float) -> np.ndarray:
    """A 面重参数化：外边 m=1 压缩加速（m=0 共棱侧零压缩；persp=0 线性）。"""
    if persp <= 0.0:
        return m
    return m * (1.0 + persp * m) / (1.0 + persp)


def _persp_b(m: np.ndarray, persp: float) -> np.ndarray:
    """B 面重参数化（镜像）：外边 m=0 压缩加速（m=1 共棱侧零压缩）。"""
    if persp <= 0.0:
        return m
    return 1.0 - (1.0 - m) * (1.0 + persp * (1.0 - m)) / (1.0 + persp)


def evaluate(a_img: ndarray, b_img: ndarray, progress: float,
             params: Mapping[str, Any] | None = None) -> ndarray:
    """两面旋转一帧（统一核签名；详见模块 docstring）。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    axis = str(p.get("axis", "Horizontal"))
    if axis not in AXES:
        raise ValueError(f"axis 必须是 {AXES}，收到 {axis!r}")
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

    theta = progress * (np.pi / 2.0)
    cos_t = max(float(np.cos(theta)), _MIN_FACE)
    sin_t = max(float(np.sin(theta)), _MIN_FACE)
    shade_a = 1.0 - shadow * (1.0 - cos_t)
    shade_b = 1.0 - shadow * (1.0 - sin_t)

    n = a.shape[1] if axis == "Horizontal" else a.shape[0]
    cs = (np.arange(n, dtype=np.float64) + 0.5) / n  # 轴向像素中心坐标

    m_a = np.clip((cs - progress) / cos_t, 0.0, 1.0)
    m_b = np.clip((cs - progress + sin_t) / sin_t, 0.0, 1.0)
    src_a = _persp_a(m_a, persp)
    src_b = _persp_b(m_b, persp)
    use_a = cs >= progress

    if axis == "Vertical":
        # 转置后沿 x 处理（转置的列 = 原行轴），再转回
        a_t = np.swapaxes(a, 0, 1)
        b_t = np.swapaxes(b, 0, 1)
        face_a = np.swapaxes(_bilinear(a_t, src_a), 0, 1) * shade_a
        face_b = np.swapaxes(_bilinear(b_t, src_b), 0, 1) * shade_b
        sel = use_a.reshape(-1, 1) if a.ndim == 2 else use_a.reshape(-1, 1, 1)
    else:
        face_a = _bilinear(a, src_a) * shade_a
        face_b = _bilinear(b, src_b) * shade_b
        sel = use_a if a.ndim == 2 else use_a[..., None]
    out = np.where(sel, face_a, face_b)
    return _finalize(out, a_in.dtype)
