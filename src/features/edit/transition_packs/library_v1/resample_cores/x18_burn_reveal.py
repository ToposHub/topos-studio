"""burn_reveal 生产核（L14/L15；暂存区再导出本模块，函数体与暂存交付逐字一致）。"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from numpy import ndarray

BURN_DEFAULTS: dict[str, Any] = {
    "pattern": "Field",   # "Edge"/"Field"；非法回退 "Field"
    "char_width": 0.08,   # 前沿带宽（rank 域；兼作揭示 ramp 宽）
    "emission": 0.5,      # 发光强度
    "seed": 0,            # 固定种子（静态：无 seed 动画）
}

_SCALE = 8.0            # rank-field 格距（帧归一 ×8；常量）
_ORANGE = (1.0, 0.3, 0.05)  # 发热橙（scene-linear 近似）
_WHITE = (1.0, 1.0, 1.0)

_U64 = np.uint64
_MASK32 = _U64(0xFFFFFFFF)
_C1, _C2, _C3 = _U64(0x9E3779B1), _U64(0x85EBCA77), _U64(0xC2B2AE3D)
_C4, _C5 = _U64(0x2545F491), _U64(0x9E3779B1)


def _safe_float(value: Any, fallback: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if v != v or v == float("inf") or v == float("-inf"):
        return float(fallback)
    return v


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def lattice_hash_u32(ix: ndarray, iy: ndarray, seed: int) -> ndarray:
    """整数格点 → [0,1)。uint32 murmur3 风格（与 X06 同式，文件内自带）。"""
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


def _rank_field(width: int, height: int, pattern: str, seed: int) -> ndarray:
    """帧归一 ×_SCALE 格点上的 rank 场（Field=双线性 value，Edge=最近格点）。"""
    xs = (np.arange(width, dtype=np.float64) + 0.5) / width * _SCALE
    ys = (np.arange(height, dtype=np.float64) + 0.5) / height * _SCALE
    gp_x, gp_y = np.meshgrid(xs, ys)
    if pattern == "Edge":
        xi = np.floor(gp_x + 0.5).astype(np.int64)
        yi = np.floor(gp_y + 0.5).astype(np.int64)
        return lattice_hash_u32(xi, yi, seed)
    x0 = np.floor(gp_x)
    y0 = np.floor(gp_y)
    fx = gp_x - x0
    fy = gp_y - y0
    fade_x = fx * fx * (3.0 - 2.0 * fx)
    fade_y = fy * fy * (3.0 - 2.0 * fy)
    xi = x0.astype(np.int64)
    yi = y0.astype(np.int64)
    n00 = lattice_hash_u32(xi, yi, seed)
    n10 = lattice_hash_u32(xi + 1, yi, seed)
    n01 = lattice_hash_u32(xi, yi + 1, seed)
    n11 = lattice_hash_u32(xi + 1, yi + 1, seed)
    a = n00 + (n10 - n00) * fade_x
    b = n01 + (n11 - n01) * fade_x
    return a + (b - a) * fade_y


def evaluate(a_img: ndarray, b_img: ndarray, progress: float,
             params: Mapping[str, Any] | None = None) -> ndarray:
    """burn reveal 一帧：base = mix(A,B,mask)，前沿带叠加发热色。"""
    a = np.asarray(a_img, dtype=np.float64)
    b = np.asarray(b_img, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(
            f"双源尺寸不一致: {np.asarray(a_img).shape} vs {np.asarray(b_img).shape}")
    p = dict(BURN_DEFAULTS)
    if params:
        p.update(params)

    pattern = str(p.get("pattern", "Field"))
    if pattern not in ("Edge", "Field"):
        pattern = "Field"
    cw = _clamp(_safe_float(p["char_width"], 0.08), 0.0, 0.5)
    emission = _clamp(_safe_float(p["emission"], 0.5), 0.0, 1.0)
    seed = int(round(_safe_float(p["seed"], 0)))
    seed = int(_clamp(seed, 0, 100))

    progress = _clamp(_safe_float(progress, 0.0), 0.0, 1.0)
    height, width = a.shape[0], a.shape[1]
    if progress <= 0.0:
        out = a
        return _cast_like(out, a_img)
    if progress >= 1.0:
        out = b
        return _cast_like(out, a_img)

    rank = _rank_field(width, height, pattern, seed)
    ramp = max(cw, 1e-6)
    mask = np.clip((progress - rank) / ramp, 0.0, 1.0)
    band = np.clip(1.0 - np.abs(progress - rank) / ramp, 0.0, 1.0)

    # base = A·(1−mask) + B·mask（mask 沿通道广播）
    if a.ndim == 3:
        m3 = mask[..., None]
        base = a * (1.0 - m3) + b * m3
        heat_rgb = np.empty_like(base)
        for ch in range(3):
            orange = _ORANGE[ch]
            heat_rgb[..., ch] = band * (orange + (_WHITE[ch] - orange) * band)
        out = base + emission * heat_rgb
    else:
        base = a * (1.0 - mask) + b * mask
        orange = _ORANGE[0]
        heat = band * (orange + (_WHITE[0] - orange) * band)
        out = base + emission * heat
    return _cast_like(out, a_img)


def _cast_like(out: ndarray, ref_img: Any) -> ndarray:
    """uint8 输入 → round-trip uint8（镜像 push_frame 约定）；否则 float32。"""
    if np.asarray(ref_img).dtype == np.uint8:
        return np.clip(np.round(out), 0, 255).astype(np.uint8)
    return out.astype(np.float32)
