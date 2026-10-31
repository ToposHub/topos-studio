"""把 MaskShape 基准几何光栅化为特征检测掩码。

跟踪直接复用 Mask 窗口形状：初始特征点只落在遮罩形状内部，
而不是包围盒矩形——圆形窗口不会把四角的背景特征也拿来跟踪
（DaVinci 式窗口跟踪）。

仅依赖 numpy / PIL，无 Qt 依赖，可在跟踪工作线程直接调用。
"""

from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw

# 每个 cubic bezier 段的采样数（pos 之间的折线展开粒度）
_CURVE_SEGMENTS = 16
# 掩码有效像素低于该值（约 16 px）视为形状过小/出画，退化 None
_MIN_MASK_PIXELS = 16


def rasterize_shape_mask(
    base_geometry: Optional[Dict],
    width: int,
    height: int,
) -> Optional[np.ndarray]:
    """归一化基准几何 → uint8 掩码（255 = 形状内部）。

    支持 circle / rect / curve；其他类型（gradient / bitmap / brush）
    或几何无效时返回 None，调用方退化为包围盒矩形检测。

    Args:
        base_geometry: ``capture_base_geometry`` 生成的几何快照（归一化坐标）。
        width: 掩码宽度（像素，须与检测灰度图一致）。
        height: 掩码高度（像素）。
    """
    if not base_geometry or width <= 0 or height <= 0:
        return None
    shape_type = str(base_geometry.get("type", ""))
    if shape_type not in ("circle", "rect", "curve"):
        return None

    img = Image.new("L", (int(width), int(height)), 0)
    draw = ImageDraw.Draw(img)

    if shape_type == "circle":
        if not _draw_ellipse(draw, base_geometry, width, height):
            return None
    elif shape_type == "rect":
        corners = _rect_corners(base_geometry, width, height)
        if corners is None:
            return None
        draw.polygon(corners, fill=255)
    else:
        poly = _curve_polygon(base_geometry, width, height)
        if poly is None:
            return None
        draw.polygon(poly, fill=255)

    mask = np.asarray(img, dtype=np.uint8)
    if int((mask > 0).sum()) < _MIN_MASK_PIXELS:
        return None
    return mask


def _draw_ellipse(draw: ImageDraw.ImageDraw, geometry: Dict,
                   width: int, height: int) -> bool:
    center = geometry.get("center") or [0.5, 0.5]
    size = geometry.get("size") or [0.0, 0.0]
    cx = float(center[0]) * width
    cy = float(center[1]) * height
    rx = float(size[0]) * width / 2.0
    ry = float(size[1]) * height / 2.0
    if rx <= 1.0 or ry <= 1.0:
        return False
    draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=255)
    return True


def _rect_corners(geometry: Dict, width: int, height: int
                   ) -> Optional[List[Tuple[float, float]]]:
    """含旋转的矩形四角（归一化 → 像素）。"""
    center = geometry.get("center") or [0.5, 0.5]
    size = geometry.get("size") or [0.0, 0.0]
    cx = float(center[0]) * width
    cy = float(center[1]) * height
    hw = float(size[0]) * width / 2.0
    hh = float(size[1]) * height / 2.0
    if hw <= 1.0 or hh <= 1.0:
        return None
    import math
    rot = float(geometry.get("rotation", 0.0))
    cos_r, sin_r = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    corners = []
    for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        corners.append((cx + dx * cos_r - dy * sin_r,
                        cy + dx * sin_r + dy * cos_r))
    return corners


def _curve_polygon(geometry: Dict, width: int, height: int
                   ) -> Optional[List[Tuple[float, float]]]:
    """贝塞尔锚点序列 → 闭合折线（每段 cubic 采样展开）。"""
    points = geometry.get("points") or []
    if len(points) < 3:
        return None
    anchors = [p.get("pos") or [0.0, 0.0] for p in points]
    poly: List[Tuple[float, float]] = []
    n = len(anchors)
    for i in range(n):
        p0 = (float(anchors[i][0]) * width, float(anchors[i][1]) * height)
        p1_raw = anchors[(i + 1) % n]
        p1 = (float(p1_raw[0]) * width, float(p1_raw[1]) * height)
        c2 = points[i].get("c2") or anchors[i]
        c1_next = points[(i + 1) % n].get("c1") or p1_raw
        c2_px = (float(c2[0]) * width, float(c2[1]) * height)
        c1_px = (float(c1_next[0]) * width, float(c1_next[1]) * height)
        for t in range(_CURVE_SEGMENTS):
            u = t / _CURVE_SEGMENTS
            poly.append(_cubic_point(p0, c2_px, c1_px, p1, u))
    return poly if len(poly) >= 3 else None


def _cubic_point(p0: Tuple[float, float], c2: Tuple[float, float],
                 c1: Tuple[float, float], p1: Tuple[float, float],
                 u: float) -> Tuple[float, float]:
    """cubic bezier 求值（De Casteljau 展开式）。"""
    v = 1.0 - u
    b0 = v * v * v
    b1 = 3.0 * v * v * u
    b2 = 3.0 * v * u * u
    b3 = u * u * u
    return (
        b0 * p0[0] + b1 * c2[0] + b2 * c1[0] + b3 * p1[0],
        b0 * p0[1] + b1 * c2[1] + b2 * c1[1] + b3 * p1[1],
    )
