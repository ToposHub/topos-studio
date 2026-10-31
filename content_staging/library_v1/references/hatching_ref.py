"""F14 hatching —— 独立 CPU 数学参考（oracle）。

与 filters/hatching/hatching.tglsl 逐式同构（references/oracle_conventions.md）：
- 目标墨密度 D = 1 − clamp(luma, 0, 1)（clamp 仅几何参数域）；
- 像素序坐标 pq = (x+0.5, y+0.5)；层 1 带坐标 b1 = (pq·dir1)/spacing，
  dir1 = (cos φ, sin φ)，φ = radians(angle)；层 2 正交、半密度 D/2；
- 线半宽 h = −AA + (0.5+2·AA)·D（AA=0.06：D=0 全露白 / D=1 满幅墨）；
  cov = 1 − smoothstep(h−AA, h+AA, |fract(b)−0.5|)；
- layers=2：总覆盖率 = 1 − (1−cov1)(1−cov2)；
- hatched = mix(vec3(paper), ink_color, cov_total)；
  out = color + (hatched − color)·amount；amount=0 精确恒等。

零采样、零循环（程序几何）；位置驱动（不可 LUT 烘出）。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)

AA = 0.06   # 条带软边宽度（胞元单位）

DEFAULTS: dict[str, Any] = {
    "line_spacing": 8.0,
    "angle": 45.0,
    "layers": "2",
    "ink_color": (0.05, 0.05, 0.08),
    "paper": 0.9,
    "amount": 1.0,
}


def _band_cov(density: np.ndarray, b: np.ndarray) -> np.ndarray:
    cell = np.abs(b - np.floor(b) - 0.5)
    h = -AA + (0.5 + 2.0 * AA) * density
    t = np.clip((cell - (h - AA)) / (2.0 * AA), 0.0, 1.0)
    s = t * t * (3.0 - 2.0 * t)
    return 1.0 - s


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]

    amount = float(p["amount"])
    if amount < 0.001:
        return img.astype(np.float32)

    spacing = max(float(p["line_spacing"]), 1.0)
    phi = np.deg2rad(float(p["angle"]))
    ink_color = np.asarray(p["ink_color"], dtype=np.float64)
    paper = float(p["paper"])
    two_layers = (p["layers"] == "2")

    yy, xx = np.meshgrid(np.arange(h, dtype=np.float64) + 0.5,
                         np.arange(w, dtype=np.float64) + 0.5, indexing="ij")

    luma = img @ LUMA_W
    d_in = 1.0 - np.clip(luma, 0.0, 1.0)

    b1 = (xx * np.cos(phi) + yy * np.sin(phi)) / spacing
    cov = _band_cov(d_in, b1)
    if two_layers:
        b2 = (-xx * np.sin(phi) + yy * np.cos(phi)) / spacing
        cov2 = _band_cov(d_in * 0.5, b2)
        cov = 1.0 - (1.0 - cov) * (1.0 - cov2)

    hatched = (1.0 - cov[..., None]) * paper + cov[..., None] * ink_color
    return (img + (hatched - img) * amount).astype(np.float32)
