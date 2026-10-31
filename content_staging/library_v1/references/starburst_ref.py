"""F10 starburst —— 独立 CPU 数学参考（oracle）。

与 filters/starburst/starburst.tglsl 逐式同构（references/oracle_conventions.md），
门控/衰减/归一化沿用 plugins/Anamorphic_Streak.tglsl 惯例：
- N 条过中心的角度线：φ_k = angle + k·180/N（N = blades 档 2/4/6）；
- 每条线双向各 7 tap：t_i = (i+0.5)/7，偏移 = ±dir·span_px·t_i，
  权重 w_i = (1−t_i)²，门控 e = max(luma(s) − threshold, 0)（线性超出量）；
- 归一化按全部 2N 方向总权重 Σ 2·w_i（星芒能量有界）；
- out = color + star·intensity；intensity=0 或 ray_length=0 精确恒等；
- span_px = ray_length · 0.25 · W（Anamorphic_Streak streak_reach 同口径）；
- 角度在方形像素域（像素序偏移）；双线性采样 + clamp-to-edge。

单 pass 语义：全部 tap 直接采样输入纹理（无中间场），单 pass 天然成立。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)

TAPS_PER_DIR = 7

DEFAULTS: dict[str, Any] = {
    "blades": "4",
    "angle": 0.0,
    "ray_length": 0.5,
    "threshold": 0.55,
    "intensity": 0.4,
}


def _bilinear(field: np.ndarray, ys: np.ndarray, xs: np.ndarray) -> np.ndarray:
    """clamp-to-edge 双线性采样（像素中心 = 整数索引）。field (H,W,C)。"""
    h, w = field.shape[0], field.shape[1]
    ys = np.clip(ys, 0.0, h - 1.0)
    xs = np.clip(xs, 0.0, w - 1.0)
    y0 = np.floor(ys).astype(np.int64)
    x0 = np.floor(xs).astype(np.int64)
    y1 = np.minimum(y0 + 1, h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    fy = (ys - y0)[..., None]
    fx = (xs - x0)[..., None]
    return (field[y0, x0] * (1.0 - fx) * (1.0 - fy)
            + field[y0, x1] * fx * (1.0 - fy)
            + field[y1, x0] * (1.0 - fx) * fy
            + field[y1, x1] * fx * fy)


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]

    intensity = float(p["intensity"])
    ray_length = float(p["ray_length"])
    if intensity < 0.001 or ray_length < 0.001:
        return img.astype(np.float32)

    blades = int(p["blades"])
    angle_deg = float(p["angle"])
    threshold = float(p["threshold"])

    span_px = ray_length * 0.25 * w
    # N 条线的基方向（angle + k*180/N，方形像素域）
    base_angles = np.arange(blades, dtype=np.float64) * (180.0 / blades)
    phi = np.deg2rad(angle_deg + base_angles)
    dirs = np.stack([np.cos(phi), np.sin(phi)], axis=-1)          # (N, 2)

    ts = [(i + 0.5) / TAPS_PER_DIR for i in range(TAPS_PER_DIR)]
    ws = [(1.0 - t) ** 2 for t in ts]

    yy, xx = np.meshgrid(np.arange(h, dtype=np.float64),
                         np.arange(w, dtype=np.float64), indexing="ij")

    acc = np.zeros((h, w, 3), dtype=np.float64)
    wsum = 0.0
    for k in range(blades):
        dy, dx = dirs[k]
        for i in range(TAPS_PER_DIR):
            d = span_px * ts[i]
            s_pos = _bilinear(img, yy + dy * d, xx + dx * d)
            s_neg = _bilinear(img, yy - dy * d, xx - dx * d)
            # 门控按各 tap 采样位的 luma 线性超出量（Anamorphic 惯例）
            e_pos = np.maximum(s_pos @ LUMA_W - threshold, 0.0)
            e_neg = np.maximum(s_neg @ LUMA_W - threshold, 0.0)
            acc += (s_pos * e_pos[..., None] + s_neg * e_neg[..., None]) * ws[i]
            wsum += 2.0 * ws[i]

    star = acc / max(wsum, 0.001)
    return (img + star * intensity).astype(np.float32)
