"""pixelate_bridge 生产核（L14/L15；暂存区再导出本模块，函数体与暂存交付逐字一致）。"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from numpy import ndarray

PIXELATE_DEFAULTS: dict[str, Any] = {
    "cell": 16,          # 峰值块尺寸（像素）
    "source_switch": 0.5,  # 换源相对进度位置（clamp 到 [0.01,0.99] 防除零）
    "direction": "In",   # "In"/"Out"；非法回退 "In"
}


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


def _block_center_index(n: int, cell_eff: float) -> ndarray:
    """块中心采样索引：像素 i 取其所在块中心最近像素（cell=1 → 恒等）。"""
    x = np.arange(n, dtype=np.float64) + 0.5
    block = np.floor(x / cell_eff)
    center = np.floor((block + 0.5) * cell_eff - 0.5)
    return np.clip(center, 0.0, float(n - 1)).astype(np.int64)


def evaluate(a_img: ndarray, b_img: ndarray, progress: float,
             params: Mapping[str, Any] | None = None) -> ndarray:
    """pixelate bridge 一帧：A 粗化 → switch 换源 → B 细化。"""
    a = np.asarray(a_img)
    b = np.asarray(b_img)
    if a.shape != b.shape:
        raise ValueError(
            f"双源尺寸不一致: {a.shape} vs {b.shape}")
    p = dict(PIXELATE_DEFAULTS)
    if params:
        p.update(params)

    cell = int(round(_safe_float(p["cell"], PIXELATE_DEFAULTS["cell"])))
    cell = int(_clamp(cell, 4, 64))
    switch = _clamp(_safe_float(p["source_switch"], 0.5), 0.01, 0.99)
    direction = str(p.get("direction", "In"))
    if direction not in ("In", "Out"):
        direction = "In"

    progress = _clamp(_safe_float(progress, 0.0), 0.0, 1.0)
    if progress <= 0.0:
        src, cell_eff = a, 1.0
    elif progress >= 1.0:
        src, cell_eff = b, 1.0
    else:
        before = progress < switch
        src = a if before else b
        if before:
            frac = progress / switch  # 0→1 靠近 switch
            cell_eff = cell * (frac if direction == "In" else 1.0 - frac)
        else:
            frac = (progress - switch) / (1.0 - switch)  # 0→1 远离 switch
            cell_eff = cell * ((1.0 - frac) if direction == "In" else frac)
    cell_eff = max(cell_eff, 1.0)  # <1 块退化 → 恒等采样

    h, w = src.shape[0], src.shape[1]
    rows = _block_center_index(h, cell_eff)
    cols = _block_center_index(w, cell_eff)
    out = src[rows][:, cols]
    if a.dtype == np.uint8:
        return out.astype(np.uint8)
    return out.astype(np.float32)
