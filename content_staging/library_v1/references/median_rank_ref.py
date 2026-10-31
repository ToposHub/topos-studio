"""F19 median_rank —— 独立 CPU 数学参考（oracle）。

与 filters/median_rank/median_rank.tglsl 逐式同构（references/oracle_conventions.md）：
- 邻域索引 clamp 到 [0, n-1]（edge 边界）；
- 每通道对窗口值排序取第 k 个，k = round(percentile*(n-1))
  （并列值占连续秩 → 与 GLSL 秩计数选择的**值**口径一致）；
- Luma 档：luma 窗口取 k 值 med_l，out = color * (med_l / max(l, 1e-6))，
  l ≤ 1e-6 时 out = vec3(med_l)；
- Delta 混合：out = color + (med - base) * mix，mix<0.001 恒等；
  CPU 侧 color 与 base 同源（无上游调色分离），结果即 med 混合。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)

DEFAULTS: dict[str, Any] = {
    "kernel_size": "3x3",
    "percentile": 0.5,
    "channel_mode": "RGB",
    "amount": 1.0,
}


def _rank_pick(window: np.ndarray, k: int) -> np.ndarray:
    """window: (..., n) → 排序取第 k 小（值口径，与 GLSL 秩计数一致）。"""
    return np.sort(window, axis=-1)[..., k]


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]
    radius = 1 if p["kernel_size"] == "3x3" else 2
    n = (2 * radius + 1) ** 2
    k = int(round(float(p["percentile"]) * (n - 1)))

    padded = np.pad(img, ((radius, radius), (radius, radius), (0, 0)), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, (2 * radius + 1, 2 * radius + 1), axis=(0, 1))
    # windows: (h, w, 3, s, s) → 展平空间维为样本维
    samples = windows.reshape(h, w, img.shape[2], n)

    if p["channel_mode"] == "Luma":
        luma_samples = np.einsum("...cn,c->...n", samples, LUMA_W)  # (h, w, n)
        med_l = _rank_pick(luma_samples, k)        # (h, w)
        luma = img @ LUMA_W
        scale = np.where(luma > 1e-6, med_l / np.maximum(luma, 1e-6), 0.0)
        med = np.where(luma[..., None] > 1e-6, img * scale[..., None], med_l[..., None])
    else:
        med = _rank_pick(samples, k)               # (h, w, 3)

    amount = float(p["amount"])
    out = img + (med - img) * amount
    return out.astype(np.float32)
