"""N02-B：key 管线参数的单一权威换算表（GL/CPU 共用）。

数值语义与 ``async_renderer._apply_matte_postprocess`` 的 GL 实现
（morphology.glsl / blur.glsl / matte_denoise.glsl）逐项对应——GL 侧
换算改为引用本表（行为不变，来源唯一）；CPU 侧（qualifier_cpu）用同
表参数做 numpy 等价实现，跨后端对拍以本表为口径。

约定：slider 值域 clean_black/clean_white 0–20、blur 0–100、denoise
0–100；半径均以像素计，``ceil`` 进核窗。
"""
from __future__ import annotations

import math
from typing import Any, Mapping


def clean_radius(value: Any) -> int:
    """clean_black/clean_white slider → box min/max 半核半径（像素）。"""
    try:
        v = float(value or 0.0)
    except (TypeError, ValueError):
        return 0
    if v <= 0.01:
        return 0
    return int(math.ceil(max(1.0, v * 3.0)))


def blur_radius(value: Any) -> int:
    """blur slider → 截断高斯核半径（像素；σ = max(r·0.5, 0.1)）。"""
    try:
        v = float(value or 0.0)
    except (TypeError, ValueError):
        return 0
    if v <= 0.01:
        return 0
    return int(math.ceil(max(1.0, v * 1.0)))


def blur_sigma(radius: int) -> float:
    return max(radius * 0.5, 0.1)


def denoise_radius(value: Any) -> int:
    """denoise slider → 预模糊核半径（小半径，细节保留）。"""
    try:
        v = float(value or 0.0)
    except (TypeError, ValueError):
        return 0
    if v <= 0.5:
        return 0
    return int(math.ceil(max(1.0, v * 0.04)))


def denoise_strength(value: Any) -> float:
    """denoise slider → smoothstep 边缘恢复强度（0–1）。"""
    try:
        v = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return min(max(v / 100.0, 0.0), 1.0)


def denoise_edge_halfwidth(strength: float) -> float:
    """strength → smoothstep 半宽 k：0→0.45（近恒等），1→0.05（硬边）。"""
    return 0.45 + (0.05 - 0.45) * min(max(strength, 0.0), 1.0)


def postprocess_layers(qp: Mapping[str, Any]) -> tuple[str, ...]:
    """按 GL 顺序返回活跃的层级后处理阶段（声明/执行共用）。"""
    layers: list[str] = []
    if clean_radius(qp.get("clean_black")) > 0:
        layers.append("clean_black")
    if clean_radius(qp.get("clean_white")) > 0:
        layers.append("clean_white")
    if blur_radius(qp.get("blur")) > 0:
        layers.append("blur")
    if denoise_radius(qp.get("denoise")) > 0:
        layers.append("denoise")
    return tuple(layers)
