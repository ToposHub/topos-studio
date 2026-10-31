"""M06.4 · MaskShape 组合的 CPU 模型层（qualifier/导出侧可消费）。

GPU 管线（image_engine）的蒙版合成此前是唯一实现（QPainter 栅格化 +
递归布尔组合）。本模块提供**纯 CPU 同语义**组合：

- **组合语义逐条一致**：first_visible 首形状直取；add=max、
  subtract=clip(m−s)、intersect=乘；MaskGroup 递归 + 组级 opacity/
  invert；后处理链序 feather→blur→clean_black(腐蚀)→clean_white
  (膨胀)→opacity→invert，公式与 engine 相同（blur 半径/核尺寸）。
- **栅格化子集与声明**：circle/rect/gradient 为解析实现（cpu_analytic
  ——边缘为解析阶跃+同源模糊，与 QPainter 抗锯齿边缘**声明近似**，
  不冒充逐像素一致）；curve（QPainterPath/SDF 逐点羽化）/bitmap/
  brush 显式 ``unsupported``，跳过并进报告——绝不静默丢形状。
- 报告（``MaskCombineReport``）：evaluated / approximate /
  unsupported 三口径，消费方（预检/交付矩阵/UI）如实呈现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from src.features.color.core.layer import MaskGroup, MaskShape


@dataclass
class MaskCombineReport:
    """组合结果的三口径报告。"""

    evaluated: List[str] = field(default_factory=list)      # 完整求值的形状 id
    approximate: List[str] = field(default_factory=list)    # 解析近似（边缘 AA 差异）
    unsupported: List[Tuple[str, str, str]] = field(
        default_factory=list)                               # (id, type, 原因)
    empty: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated": list(self.evaluated),
            "approximate": list(self.approximate),
            "unsupported": [list(u) for u in self.unsupported],
            "empty": self.empty,
        }


def _feather_blur(arr: np.ndarray, radius: int) -> np.ndarray:
    """与 engine._fast_feather_blur 同式（≤31 走 Gaussian；大半径不进
    模型层小画布场景，同式简化为 Gaussian——半径域内一致）。"""
    if radius <= 0:
        return arr
    if radius % 2 == 0:
        radius += 1
    return cv2.GaussianBlur(arr, (radius, radius), 0)


def _apply_postprocess(arr: np.ndarray, shape: MaskShape,
                       target_w: int) -> np.ndarray:
    """后处理链（与 engine._render_single_shape 尾段同式同序）。"""
    out = arr
    if shape.feather > 0 and shape.type != MaskShape.BITMAP:
        radius = int((shape.feather / 100.0) * 4.0 * (target_w / 5.0))
        if radius > 0:
            out = _feather_blur(out, radius)
    if shape.blur > 0:
        radius = int((shape.blur / 100.0) * (target_w / 5.0))
        if radius > 0:
            out = _feather_blur(out, radius)
    if shape.clean_black > 0:
        k = max(3, int(shape.clean_black * 3.0))
        if k % 2 == 0:
            k += 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        out = cv2.erode(out, kernel, iterations=1)
    if shape.clean_white > 0:
        k = max(3, int(shape.clean_white * 3.0))
        if k % 2 == 0:
            k += 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        out = cv2.dilate(out, kernel, iterations=1)
    out = out * float(shape.opacity)
    if shape.invert:
        out = 1.0 - out
    return out


def rasterize_shape_cpu(shape: MaskShape, w: int, h: int
                        ) -> Tuple[Optional[np.ndarray], Optional[str]]:
    """单个形状的解析栅格化（float32 0-1）。

    Returns:
        (arr, None)：circle/rect/gradient 求值成功（解析近似）；
        (None, reason)：curve/bitmap/brush 不支持（显式原因）。
    """
    if shape.type == MaskShape.CURVE:
        return None, "curve 形状依赖 QPainterPath/SDF 逐点羽化（GPU 管线专属）"
    if shape.type == MaskShape.BITMAP:
        return None, "bitmap/AI 蒙版数据不由形状解析栅格化"
    if shape.type == MaskShape.BRUSH:
        return None, "brush 笔刷由 GPU 管线渲染（笔画栅格化专属）"

    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    xn = xx / max(1, w - 1)
    yn = yy / max(1, h - 1)
    cx, cy = float(shape.center[0]), float(shape.center[1])
    sw, sh = float(shape.size[0]), float(shape.size[1])

    if shape.type == MaskShape.CIRCLE:
        # 椭圆：归一化半径内 1（feather 由后处理模糊链负责边缘过渡）
        rx = max(1e-6, sw / 2.0)
        ry = max(1e-6, sh / 2.0)
        d = ((xn - cx) / rx) ** 2 + ((yn - cy) / ry) ** 2
        arr = (d <= 1.0).astype(np.float32)
    elif shape.type == MaskShape.RECT:
        # 轴对齐矩形（旋转经 warp 同 engine 路径，见 combine 内处理）
        hw = max(1e-6, sw / 2.0)
        hh = max(1e-6, sh / 2.0)
        arr = ((np.abs(xn - cx) <= hw) & (np.abs(yn - cy) <= hh)
               ).astype(np.float32)
    elif shape.type == MaskShape.GRADIENT:
        (x0, y0), (x1, y1) = shape.grad_points[:2]
        gx, gy = float(x1) - float(x0), float(y1) - float(y0)
        norm2 = max(1e-9, gx * gx + gy * gy)
        t = (((xn - float(x0)) * gx + (yn - float(y0)) * gy)
             / norm2)
        soft = max(1e-6, float(shape.softness))
        arr = np.clip((t + soft / 2.0) / soft, 0.0, 1.0).astype(np.float32)
        arr = 1.0 - arr  # 起点为 1（engine 梯度语义：start 侧不透明）
    else:
        return None, f"未知形状类型：{shape.type}"

    if abs(float(shape.rotation)) > 0.01:
        arr = _rotate_mask(arr, float(shape.rotation), w, h)
    return _apply_postprocess(arr, shape, w), None


def _rotate_mask(arr: np.ndarray, rotation_deg: float,
                 w: int, h: int) -> np.ndarray:
    """绕画布中心旋转（同 engine 的 warpAffine 常规路径）。"""
    pad = max(w, h) * 3 // 4 + 4
    padded = np.zeros((h + 2 * pad, w + 2 * pad), dtype=np.float32)
    padded[pad:pad + h, pad:pad + w] = arr
    ccx, ccy = 0.5 * w + pad, 0.5 * h + pad
    matrix = cv2.getRotationMatrix2D((ccx, ccy), -rotation_deg, 1.0)
    rotated = cv2.warpAffine(
        padded, matrix, (w + 2 * pad, h + 2 * pad),
        flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
        borderValue=0.0)
    return rotated[pad:pad + h, pad:pad + w]


def combine_masks_cpu(items: List[Any], w: int, h: int
                      ) -> Tuple[np.ndarray, MaskCombineReport]:
    """递归组合（MaskShape/MaskGroup），语义与 engine 同源。

    Returns:
        (mask, report)：mask 为 float32 (h, w) 0-1；不可评估形状
        （curve/bitmap/brush）跳过并进 report.unsupported——不静默。
    """
    report = MaskCombineReport()
    final = _combine_items(items, w, h, report)
    report.empty = not bool(report.evaluated)
    return final, report


def _combine_items(items: List[Any], w: int, h: int,
                   report: MaskCombineReport) -> np.ndarray:
    final_mask = np.zeros((h, w), dtype=np.float32)
    first_visible = True
    for item in items:
        if not isinstance(item, (MaskGroup, MaskShape)):
            continue
        if not item.visible:
            continue
        if isinstance(item, MaskGroup):
            group_mask = _combine_items(item.children, w, h, report)
            group_mask = group_mask * float(item.opacity)
            if item.invert:
                group_mask = 1.0 - group_mask
            shape_arr = group_mask
            operation = item.operation
            if report.evaluated or report.approximate:
                report.evaluated.append(f"group:{item.name}")
        else:
            shape_arr, reason = rasterize_shape_cpu(item, w, h)
            if shape_arr is None:
                report.unsupported.append(
                    (item.id, item.type, reason or "unknown"))
                continue
            # circle/rect/gradient：解析近似（边缘与 QPainter AA 差异）
            report.approximate.append(item.id)
            report.evaluated.append(item.id)
            operation = item.operation

        if first_visible:
            final_mask = shape_arr
            first_visible = False
        else:
            if operation == MaskShape.OP_ADD:
                final_mask = np.maximum(final_mask, shape_arr)
            elif operation == MaskShape.OP_SUBTRACT:
                final_mask = np.clip(final_mask - shape_arr, 0.0, 1.0)
            elif operation == MaskShape.OP_INTERSECT:
                final_mask = final_mask * shape_arr
    return final_mask


__all__ = [
    "MaskCombineReport",
    "combine_masks_cpu",
    "rasterize_shape_cpu",
]
