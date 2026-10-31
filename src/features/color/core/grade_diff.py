"""M22 · GradeDiff——before/after 调色差异度量（镜头匹配预览用）。

对参考帧与匹配预览帧给出可解释差异（曝光档/白平衡/饱和比）——与
shot_match 的 match_shot 估计同域，供 UI 在 before/after 实际帧旁
呈现"差多少"（不只是看图）。
"""

from __future__ import annotations

from typing import Any, Dict

import numpy as np


def frame_mean_stats(rgb: np.ndarray) -> Dict[str, float]:
    """帧级统计（RGB 加权亮度 + 通道均值 + 饱和度均值）。"""
    flat = rgb[..., :3].reshape(-1, 3).astype(np.float64)
    r, g, b = flat[:, 0], flat[:, 1], flat[:, 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    sat = np.where(maxc > 1e-10, (maxc - minc) / np.maximum(maxc, 1e-10),
                   0.0)
    return {
        "luma_mean": float(luma.mean()),
        "r_mean": float(r.mean()),
        "g_mean": float(g.mean()),
        "b_mean": float(b.mean()),
        "sat_mean": float(sat.mean()),
    }


def grade_diff(
    reference: np.ndarray, preview: np.ndarray,
) -> Dict[str, Any]:
    """参考帧 vs 匹配预览帧的差异度量。

    Returns:
        {"exposure_stops": float,   # 亮度比 → 档（正=预览更亮）
         "temp_delta": float,       # R/B 均值差（正=预览偏暖）
         "tint_delta": float,       # G 均值差（正=预览偏绿）
         "sat_ratio": float,        # 饱和度比（>1=预览更饱和）
         "summary": str}            # 人类可读一行
    """
    ref = frame_mean_stats(reference)
    pre = frame_mean_stats(preview)
    luma_ratio = max(pre["luma_mean"], 1e-6) / max(ref["luma_mean"], 1e-6)
    import math
    exposure_stops = math.log2(max(luma_ratio, 1e-9))
    temp_delta = (pre["r_mean"] - ref["r_mean"]) \
        - (pre["b_mean"] - ref["b_mean"])
    tint_delta = pre["g_mean"] - ref["g_mean"]
    sat_ratio = max(pre["sat_mean"], 1e-6) / max(ref["sat_mean"], 1e-6)
    summary = (f"曝光 {exposure_stops:+.2f} 档｜"
               f"色温 {temp_delta:+.3f}｜色调 {tint_delta:+.3f}｜"
               f"饱和 ×{sat_ratio:.2f}")
    return {
        "exposure_stops": exposure_stops,
        "temp_delta": float(temp_delta),
        "tint_delta": float(tint_delta),
        "sat_ratio": float(sat_ratio),
        "summary": summary,
    }


__all__ = ["frame_mean_stats", "grade_diff"]
