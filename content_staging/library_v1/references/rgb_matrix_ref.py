"""C02 rgb_matrix —— 独立 CPU 数学参考（oracle）。

遵守 references/oracle_conventions.md：float64 内部、float32 接口、
scene-linear、不 import 宿主渲染/GPU 代码（禁自证）、无随机流。

算法（3×3 通道矩阵 + 偏置 + 强度混合）：
    out_rgb = M · rgb + bias
    final   = rgb + (out_rgb − rgb) · strength
M 的行由 row{0,1,2}_{r,g,b} 9 个系数组成（out.r = row0_r·r + row0_g·g
+ row0_b·b + bias.r，依此类推）。默认 M=I、bias=0 → **任意 strength 下
恒等**；strength=0 → 任意 M/bias 下恒等。线性变换无 clamp——HDR 负值/
超白透传（L00 台账 C02 行：Composite 无 3×3 串扰系数节点，差量登记）。
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

DEFAULTS: dict[str, Any] = {
    "row0_r": 1.0, "row0_g": 0.0, "row0_b": 0.0,
    "row1_r": 0.0, "row1_g": 1.0, "row1_b": 0.0,
    "row2_r": 0.0, "row2_g": 0.0, "row2_b": 1.0,
    "bias": (0.0, 0.0, 0.0),
    "strength": 1.0,
}

_ROW_KEYS = (("row0_r", "row0_g", "row0_b"),
             ("row1_r", "row1_g", "row1_b"),
             ("row2_r", "row2_g", "row2_b"))


def build_matrix(params: Mapping[str, Any]) -> np.ndarray:
    """从参数快照构造 3×3 矩阵（行主序，float64）。"""
    return np.array(
        [[float(params[k]) for k in row] for row in _ROW_KEYS], dtype=np.float64)


def evaluate(image: np.ndarray, params: Mapping[str, Any] | None = None) -> np.ndarray:
    """对 (..., 3) 或 (..., 4) 数组应用 M·rgb + bias 与 strength 混合。

    alpha（若存在）原样透传；输出 float32。
    """
    p = dict(DEFAULTS)
    if params:
        p.update(params)
    img = np.asarray(image, dtype=np.float64)
    rgb = img[..., :3]

    m = build_matrix(p)
    bias = np.asarray(p["bias"], dtype=np.float64)
    strength = float(p["strength"])

    transformed = rgb @ m.T + bias
    out_rgb = rgb + (transformed - rgb) * strength

    if img.shape[-1] == 4:
        return np.concatenate([out_rgb, img[..., 3:4]], axis=-1).astype(np.float32)
    return out_rgb.astype(np.float32)
