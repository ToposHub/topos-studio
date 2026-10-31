"""X07 voronoi_reveal CPU 核（暂存组件，L14 / mask 类）。

晶胞距离推进揭示 mask（本批唯一 mask 类：签名 evaluate_mask(width,
height, progress, params)，与 X01 clock_wipe 同口径，float32 (H,W) 输出）。

- 胞元种子点：n=ceil(sqrt(count)) 网格（8→3×3、16→4×4、32→6×6），种子在
  格内 hash 扰动位置；出生时间 t_i = rank_i·t_span（rank = hash 排序名次/m，
  t_span = clamp(1-2·edge_width, 0, 1) —— 时间跨度压缩是端点 p=1 全揭示
  的充要条件，见 README 冲突记录）。
- 每像素取**最近**种子（非全种子并集，README 记录取舍）：mask =
  clamp((progress - t_i)·speed - d/D, 0, 1)；speed = 1/(edge_width+1e-6)，
  d = 宽高比修正欧氏距离，D = 画幅对角线 → d/D ∈ [0,1]。
- 晶胞语义：同一胞元共享 t_i 与距离场 → 胞元中心（d 小）先揭示、胞界
  （d 大）后揭示；edge_width 增大 → 揭示带（0<mask<1）变宽。
- 端点：p≤0 → 全 0、p≥1 → 全 1（early return，与 X01 一致）；p∈(0,1) 的
  公式在 p=1 处亦满足 mask≡1（(1-t_max)·speed ≥ d_max/D + 1，README 推导）。

hash 家族与 F07 seeded_grain / fixtures _lattice_hash 同式 uint32 murmur3。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from numpy import ndarray

DEFAULTS: dict[str, Any] = {
    "cell_count": "16",   # "8" | "16" | "32"（目标数，实际网格 n²）
    "seed": 7,
    "edge_width": 0.1,    # 归一距离（相对画幅对角线 D）
}
CELL_COUNTS = ("8", "16", "32")

_U64 = np.uint64
_MASK32 = _U64(0xFFFFFFFF)
_C1, _C2, _C3 = _U64(0x9E3779B1), _U64(0x85EBCA77), _U64(0xC2B2AE3D)
_C4, _C5 = _U64(0x2545F491), _U64(0x9E3779B1)

_EPS_SPEED = 1e-6


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


def _seeds(n: int, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (sx, sy, t)：种子归一位置与出生时间（行序展开，m=n²个）。"""
    m = n * n
    sx = np.empty(m, dtype=np.float64)
    sy = np.empty(m, dtype=np.float64)
    t_raw = np.empty(m, dtype=np.float64)
    for jy in range(n):
        for jx in range(n):
            i = jy * n + jx
            sx[i] = (jx + _hash01(jx, jy, seed)) / n
            sy[i] = (jy + _hash01(jx, jy, seed + 1001)) / n
            t_raw[i] = _hash01(jx, jy, seed + 2002)
    rank_pos = np.empty(m, dtype=np.float64)
    order = np.argsort(t_raw, kind="stable")
    for pos, i in enumerate(order):
        rank_pos[i] = pos / m
    return sx, sy, rank_pos


def evaluate_mask(width: int, height: int, progress: float,
                  params: Mapping[str, Any] | None = None) -> ndarray:
    """晶胞距离推进揭示 mask：(H,W) float32，p=0 全 0 / p=1 全 1。"""
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    cell = str(p.get("cell_count", "16"))
    if cell not in CELL_COUNTS:
        raise ValueError(f"cell_count 必须是 {CELL_COUNTS}，收到 {cell!r}")
    try:
        progress = float(progress)
    except (TypeError, ValueError):
        progress = 0.0
    if progress != progress:
        progress = 0.0
    progress = max(0.0, min(1.0, progress))
    if progress <= 0.0:
        return np.zeros((height, width), dtype=np.float32)
    if progress >= 1.0:
        return np.ones((height, width), dtype=np.float32)

    seed = _safe_i(p, "seed", 7, 0, 100)
    edge_width = _safe_f(p, "edge_width", 0.1, 0.0, 0.5)
    n = int(np.ceil(np.sqrt(float(cell))))
    m = n * n

    t_span = max(0.0, 1.0 - 2.0 * edge_width)
    speed = 1.0 / (edge_width + _EPS_SPEED)
    aspect = float(width) / float(height)
    diag = np.sqrt(aspect * aspect + 1.0)

    sx, sy, rank = _seeds(n, seed)
    t_birth = rank * t_span

    us = (np.arange(width, dtype=np.float64) + 0.5) / width
    vs = (np.arange(height, dtype=np.float64) + 0.5) / height
    uu, vv = np.meshgrid(us, vs)

    # 每像素最近种子（宽高比修正距离场）
    d2_min = np.full((height, width), np.inf, dtype=np.float64)
    best = np.zeros((height, width), dtype=np.int64)
    for i in range(m):
        dx = (uu - sx[i]) * aspect
        dy = vv - sy[i]
        d2 = dx * dx + dy * dy
        take = d2 < d2_min
        d2_min = np.where(take, d2, d2_min)
        best = np.where(take, np.int64(i), best)
    d_norm = np.sqrt(d2_min) / diag

    reveal = (progress - t_birth[best]) * speed - d_norm
    return np.clip(reveal, 0.0, 1.0).astype(np.float32)
