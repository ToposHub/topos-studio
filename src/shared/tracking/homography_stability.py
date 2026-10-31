"""
单应性稳定性检查（阶段 8，L2 平面/透视跟踪）。

对 RANSAC 求解出的单应性做稳定性判定，极端透视与退化四边形
**停止并标记失败**，绝不输出爆炸矩阵：

- 矩阵条件数过大 → 数值不稳定；
- pattern 四角变换后面积比过小（接近侧面）或过大（爆炸）→ 不可信；
- 变换后四边形自交/非凸（顶点顺序反转）→ 退化；
- 顶点出现非有限值或齐次除零 → 退化。

纯函数模块，无 Qt 依赖；同时提供跟踪诊断汇总（内点数、
重投影误差、条件数），供面板诊断显示。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np

# ---- 默认阈值（可由 TrackerConfig 覆盖） ----
DEFAULT_MAX_CONDITION = 1.0e4          # 矩阵条件数上限
DEFAULT_MIN_AREA_RATIO = 0.10          # 变换后面积 / 参考面积 下限（低于≈接近侧面）
DEFAULT_MAX_AREA_RATIO = 10.0          # 面积比上限（爆炸矩阵防护）
DEFAULT_SIDE_VIEW_RATIO = 0.18         # 低于此面积比视为接近侧面（诊断标记）


@dataclass(frozen=True)
class HomographyDiagnostics:
    """单应性稳定性诊断结果。"""

    stable: bool
    condition_number: float        # 矩阵条件数（不可计算时为 inf）
    area_ratio: float              # 变换后四边形面积 / 参考面积
    convex: bool                   # 变换后四边形是否凸且顶点顺序未反转
    side_view: bool                # 是否接近侧面视角（面积比过小）
    reason: str = ""               # 不稳定原因（供面板/日志显示）


def _polygon_area(points: np.ndarray) -> float:
    """鞋带公式多边形面积（正值表示给定顶点顺序未反转）。"""
    x = points[:, 0]
    y = points[:, 1]
    return float(0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _is_convex_ccw(points: np.ndarray, eps: float = 1e-9) -> bool:
    """严格检查四边形凸性；有向面积为正还不足以排除凹四边形。"""
    if points.shape != (4, 2) or not np.all(np.isfinite(points)):
        return False
    crosses = []
    for i in range(4):
        a = points[(i + 1) % 4] - points[i]
        b = points[(i + 2) % 4] - points[(i + 1) % 4]
        crosses.append(float(a[0] * b[1] - a[1] * b[0]))
    return all(cross > eps for cross in crosses)


def transform_quad(
    matrix: np.ndarray, rect_px: Tuple[float, float, float, float]
) -> Optional[np.ndarray]:
    """矩形四角 (TL, TR, BR, BL) 经单应性变换；出现非有限值返回 None。"""
    x, y, w, h = rect_px
    corners = np.array(
        [[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.float64
    )
    homog = np.column_stack([corners, np.ones(4)])
    proj = (np.asarray(matrix, dtype=np.float64) @ homog.T).T
    if not np.all(np.isfinite(proj)):
        return None
    w_ = proj[:, 2]
    if np.any(np.abs(w_) < 1e-9):
        return None
    return proj[:, :2] / w_[:, None]


def check_homography_stability(
    matrix: np.ndarray,
    pattern_rect_px: Tuple[float, float, float, float],
    max_condition: float = DEFAULT_MAX_CONDITION,
    min_area_ratio: float = DEFAULT_MIN_AREA_RATIO,
    max_area_ratio: float = DEFAULT_MAX_AREA_RATIO,
    side_view_ratio: float = DEFAULT_SIDE_VIEW_RATIO,
) -> HomographyDiagnostics:
    """判定单应性是否稳定可用。

    Args:
        matrix: 3x3 单应性（与 pattern 同坐标系）。
        pattern_rect_px: pattern 区域（像素 (x, y, w, h)）。
    """
    m = np.asarray(matrix, dtype=np.float64).reshape(3, 3)
    if not np.all(np.isfinite(m)):
        return HomographyDiagnostics(
            False, math.inf, 0.0, False, False, "矩阵含非有限值"
        )

    try:
        cond = float(np.linalg.cond(m))
    except np.linalg.LinAlgError:
        cond = math.inf

    ref_area = abs(_polygon_area(
        np.array([
            [pattern_rect_px[0], pattern_rect_px[1]],
            [pattern_rect_px[0] + pattern_rect_px[2], pattern_rect_px[1]],
            [pattern_rect_px[0] + pattern_rect_px[2],
             pattern_rect_px[1] + pattern_rect_px[3]],
            [pattern_rect_px[0], pattern_rect_px[1] + pattern_rect_px[3]],
        ], dtype=np.float64)
    ))
    if ref_area < 1.0:
        return HomographyDiagnostics(
            False, cond, 0.0, False, False, "pattern 区域过小"
        )

    quad = transform_quad(m, pattern_rect_px)
    if quad is None:
        return HomographyDiagnostics(
            False, cond, 0.0, False, False, "四角变换出现非有限值（爆炸矩阵）"
        )

    signed = _polygon_area(quad)
    area_ratio = abs(signed) / ref_area
    convex = _is_convex_ccw(quad)

    side_view = area_ratio < side_view_ratio
    if not convex:
        return HomographyDiagnostics(
            False, cond, area_ratio, False, side_view,
            "退化四边形（自交或顶点顺序反转）",
        )
    if area_ratio < min_area_ratio:
        return HomographyDiagnostics(
            False, cond, area_ratio, True, True,
            "极端透视：接近侧面视角，结果不可靠",
        )
    if area_ratio > max_area_ratio:
        return HomographyDiagnostics(
            False, cond, area_ratio, True, side_view,
            "面积爆炸：单应性数值不稳定",
        )
    if cond > max_condition:
        return HomographyDiagnostics(
            False, cond, area_ratio, True, side_view,
            f"矩阵条件数过大（{cond:.3g}）",
        )
    return HomographyDiagnostics(
        True, cond, area_ratio, True, side_view, ""
    )


# ================================================================
# 诊断汇总（内点数 / 重投影误差 / 条件数）
# ================================================================


def summarize_diagnostics(diagnostics: Dict[int, object]) -> dict:
    """逐帧诊断汇总，供面板显示：内点数、重投影误差、条件数等。"""
    values = list(diagnostics.values())
    if not values:
        return {
            "frame_count": 0,
            "avg_inlier_count": 0.0,
            "min_inlier_count": 0,
            "avg_reproj_error": 0.0,
            "max_reproj_error": 0.0,
            "avg_confidence": 0.0,
            "avg_condition_number": 0.0,
            "max_condition_number": 0.0,
        }
    inliers = [float(getattr(d, "inlier_count", 0)) for d in values]
    errors = [
        float(getattr(d, "reproj_error", 0.0)) for d in values
        if math.isfinite(float(getattr(d, "reproj_error", 0.0)))
    ]
    confs = [float(getattr(d, "confidence", 0.0)) for d in values]
    conds = [
        float(getattr(d, "condition_number", 0.0)) for d in values
        if math.isfinite(float(getattr(d, "condition_number", 0.0)))
    ]
    return {
        "frame_count": len(values),
        "avg_inlier_count": float(np.mean(inliers)) if inliers else 0.0,
        "min_inlier_count": int(min(inliers)) if inliers else 0,
        "avg_reproj_error": float(np.mean(errors)) if errors else 0.0,
        "max_reproj_error": float(np.max(errors)) if errors else 0.0,
        "avg_confidence": float(np.mean(confs)) if confs else 0.0,
        "avg_condition_number": float(np.mean(conds)) if conds else 0.0,
        "max_condition_number": float(np.max(conds)) if conds else 0.0,
    }
