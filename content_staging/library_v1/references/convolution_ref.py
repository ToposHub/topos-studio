"""C16 convolution —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
无随机流、防御回退。3×3 固定形状自定义卷积（L00 台账 C16 行：
Composite 无自定义核节点；edge_detect/sharpen 专用核不算）：

- 核布局：k00..k22 行主序 3×3（k00=左上、k11=中心、k22=右下）；
  out[y,x] = Σ_{i,j} K[i,j]·src[y+i−1, x+j−1]（edge 边界钳制）；
- 归一化：normalize=True 时除以 D = Σ|K|（绝对值和——负权重核如
  Laplacian 的能量归一）；**防御回退**：D < 1e-6（如全零核）时 D=1
  （不除零、不发散，README/known_limits 声明）；
- 偏置：bias 在归一化**之后**加：out = conv/D + bias；
- 恒等：中心核（k11=1 其余 0）+ normalize 任意 + bias=0 → 精确恒等
  （含 HDR）。注意恒等核是**中心 tap k11**（k00=1 是 (−1,−1) 平移核，
  非恒等——任务口径按行列网格语义修正为中心 tap）；
- 线性卷积无 clamp——HDR 负值/超白透传；alpha 原样透传。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

_KERNEL_KEYS = (
    ("k00", "k01", "k02"),
    ("k10", "k11", "k12"),
    ("k20", "k21", "k22"),
)

DEFAULTS: dict[str, Any] = {
    "k00": 0.0, "k01": 0.0, "k02": 0.0,
    "k10": 0.0, "k11": 1.0, "k12": 0.0,
    "k20": 0.0, "k21": 0.0, "k22": 0.0,
    "normalize": False,
    "bias": 0.0,
}

_NORM_EPS = 1e-6


def build_kernel(params: Mapping[str, Any]) -> np.ndarray:
    """从参数快照构造 3×3 核（行主序，float64）。"""
    return np.array(
        [[float(params[k]) for k in row] for row in _KERNEL_KEYS], dtype=np.float64)


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    kernel = build_kernel(p)
    normalize = bool(p["normalize"])
    bias = float(p["bias"])

    img = np.asarray(image, dtype=np.float64)
    h, w = img.shape[0], img.shape[1]

    acc = np.zeros((h, w, 3), dtype=np.float64)
    padded = np.pad(img[..., :3], ((1, 1), (1, 1), (0, 0)), mode="edge")
    for i in range(3):
        for j in range(3):
            acc += kernel[i, j] * padded[i:i + h, j:j + w, :]

    if normalize:
        denom = float(np.sum(np.abs(kernel)))
        if denom < _NORM_EPS:
            denom = 1.0  # 防御回退：全零核不除零
        acc = acc / denom

    out_rgb = acc + bias
    if img.shape[-1] == 4:
        return np.concatenate([out_rgb, img[..., 3:4]], axis=-1).astype(np.float32)
    return out_rgb.astype(np.float32)
