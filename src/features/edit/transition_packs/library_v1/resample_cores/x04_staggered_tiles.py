"""staggered_tiles 生产核（L14/L15；暂存区再导出本模块，函数体与暂存交付逐字一致）。"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from numpy import ndarray

DEFAULTS: dict[str, Any] = {
    "grid": 4,
    "delay": 0.2,
    "order": "Row",   # Row | Random
    "seed": 0,
    "scale_amt": 0.25,
}
ORDERS = ("Row", "Random")

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
    """AUD-19210 家族防御：坏值（NaN/Inf/非数）回退默认，越界钳到 [lo,hi]。"""
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


def _tile_ranks(g: int, order: str, seed: int) -> np.ndarray:
    """块排名 r∈[0,1)（Random 档取 <1 上确界，delay_max 按实际最大值取）。"""
    m = g * g
    if m <= 1:
        return np.zeros(1, dtype=np.float64)
    if order == "Random":
        h = [_hash01(k, 41, seed) for k in range(m)]
        perm = sorted(range(m), key=lambda k: (h[k], k))
        rank = np.empty(m, dtype=np.float64)
        for pos, k in enumerate(perm):
            rank[k] = pos / m
        return rank
    return np.arange(m, dtype=np.float64) / (m - 1)


def _bilinear(img: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """像素中心制双线性（u,v 归一），越界 clamp 到边纹素（X09 惯例）。"""
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
    """双源分块错峰缩放一帧（统一核签名；详见模块 docstring）。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    order = str(p.get("order", "Row"))
    if order not in ORDERS:
        raise ValueError(f"order 必须是 {ORDERS}，收到 {order!r}")
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

    g = _safe_i(p, "grid", 4, 2, 8)
    delay = _safe_f(p, "delay", 0.2, 0.0, 0.5)
    seed = _safe_i(p, "seed", 0, 0, 100)
    scale_amt = _safe_f(p, "scale_amt", 0.25, 0.0, 0.5)

    h, w = a.shape[0], a.shape[1]
    us = (np.arange(w, dtype=np.float64) + 0.5) / w
    vs = (np.arange(h, dtype=np.float64) + 0.5) / h
    uu, vv = np.meshgrid(us, vs)

    ix = np.clip((uu * g).astype(np.int64), 0, g - 1)
    iy = np.clip((vv * g).astype(np.int64), 0, g - 1)
    k = iy * g + ix

    ranks = _tile_ranks(g, order, seed)
    delays = delay * ranks
    delay_max = float(delays.max()) if delays.size else 0.0
    denom = max(1.0 - delay_max, 1e-6)
    pi = np.clip((progress - delays[k]) / denom, 0.0, 1.0)

    lx = uu * g - ix  # 块内局部坐标 ∈ [0,1)
    ly = vv * g - iy

    # A 层：采样窗向块中心收缩（s≤1 → 恒在块内/界内）
    s_a = 1.0 - scale_amt * pi
    src_ax = (ix + 0.5 + (lx - 0.5) * s_a) / g
    src_ay = (iy + 0.5 + (ly - 0.5) * s_a) / g
    # B 层：自宽窗 1/(1-scale_amt·(1-pi)) 收缩到 1（可越块 → clamp）
    s_b = 1.0 / (1.0 - scale_amt * (1.0 - pi))
    src_bx = (ix + 0.5 + (lx - 0.5) * s_b) / g
    src_by = (iy + 0.5 + (ly - 0.5) * s_b) / g

    layer_a = _bilinear(a, src_ax, src_ay)
    layer_b = _bilinear(b, src_bx, src_by)
    pi3 = pi[..., None] if a.ndim == 3 else pi
    out = (1.0 - pi3) * layer_a + pi3 * layer_b
    return _finalize(out, a_in.dtype)
