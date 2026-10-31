"""
画布 Tracker Overlay（阶段 4）。

独立 ``QGraphicsObject`` 图层，不向蒙版图形写入任何跟踪点
（保护蒙版编辑与撤销逻辑）。坐标约定：

- 对外接口全部使用**归一化区域/点**（0-1，与 ``MaskShape`` 一致）；
- 内部经 ``set_frame_size`` 提供的帧尺寸映射到场景像素坐标。

图层顺序（计划 §4.1，底→顶，由绘制顺序与 zValue 共同保证）：
    视频/蒙版边界(≤25) → search(26) → pattern(27) → 特征点(28) →
    路径(28.5) → 当前帧控制(29) → 错误标记(29.5) → hover(29.9)

交互约定（计划 §4.3）：拖动过程只更新 overlay，释放才发出提交信号。
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsRectItem,
    QStyleOptionGraphicsItem,
)

Region = Tuple[float, float, float, float]
Point = Tuple[float, float]


# ================================================================
# 颜色与样式常量（计划 §4.2，固定值，不随主题变化）
# ================================================================

ACTIVE_SHAPE_COLOR = QColor(0xFF, 0x8A, 0x00, 255)  # 当前形状
FEATURE_VALID_COLOR = QColor(0x36, 0xD7, 0xFF, 240)
FEATURE_WEAK_COLOR = QColor(0xFF, 0xB0, 0x20, 240)
FEATURE_FAILED_COLOR = QColor(0xFF, 0x4D, 0x5E, 240)
PATH_COLOR = QColor(0xFF, 0xFF, 0xFF, 210)
MANUAL_COLOR = QColor(0xC7, 0x7D, 0xFF, 255)
INVALID_COLOR = QColor(0xFF, 0x4D, 0x5E, 255)      # 包含性破坏时的红框
HOMOGRAPHY_COLOR = QColor(0xC7, 0x7D, 0xFF, 255)  # 透视当前平面
LOCKED_HANDLE_COLOR = QColor(0x88, 0x88, 0x88, 220)

HANDLE_SIZE = 8.0          # 角点控制点屏幕尺寸（8px，处于规范 7-11px 区间）
FEATURE_RADIUS = 2.5
PATH_MAX_DISPLAY = 2000    # 路径显示抽样上限（原始数据不丢失）
CONTAIN_EPS = 1e-6         # 包含性判定容差（归一化坐标）
HIT_TOLERANCE = 10.0       # 命中判定容差（场景像素）
DIRECTION_LINE_PX = 12.0   # 透视方向指示线长度（屏幕像素）

# 图层 zValue（底→顶，计划 §4.1）
Z_MASK_TOP = 25.0
Z_SEARCH = 26.0
Z_PATTERN = 27.0
Z_FEATURES = 28.0
Z_PATH = 28.5
Z_CURRENT = 29.0
Z_ERROR = 29.5
Z_HOVER = 29.9

LAYER_ORDER = (
    "search", "pattern", "features", "path", "current", "error", "hover",
)
LAYER_Z = {
    "search": Z_SEARCH,
    "pattern": Z_PATTERN,
    "features": Z_FEATURES,
    "path": Z_PATH,
    "current": Z_CURRENT,
    "error": Z_ERROR,
    "hover": Z_HOVER,
}


class OverlayState(Enum):
    """交互状态机（计划 §4.3 的画布侧子集）。"""

    IDLE = "idle"
    TRACKER_READY = "tracker_ready"
    PATTERN_EDITING = "pattern_editing"
    SEARCH_EDITING = "search_editing"
    TRACKING = "tracking"
    TRACKED = "tracked"
    MANUAL_CORRECTION = "manual_correction"


class FeaturePointStatus(Enum):
    VALID = "valid"
    WEAK = "weak"
    FAILED = "failed"


# ================================================================
# 纯函数几何辅助（无 Qt 场景依赖，便于单元测试）
# ================================================================

def search_contains_pattern(search: Region, pattern: Region,
                            eps: float = CONTAIN_EPS) -> bool:
    """search 区域是否完整包住 pattern 区域（归一化坐标）。"""
    sx, sy, sw, sh = search
    px, py, pw, ph = pattern
    return (
        sx - eps <= px
        and sy - eps <= py
        and sx + sw + eps >= px + pw
        and sy + sh + eps >= py + ph
    )


def sample_path(points: List[Point], max_display: int = PATH_MAX_DISPLAY) -> List[Point]:
    """路径显示抽样：超过上限时等步长抽样，首尾点必须保留。"""
    n = len(points)
    if n <= max_display:
        return list(points)
    stride = math.ceil(n / max_display)
    sampled = list(points[::stride])
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


def clamp_region(region: Region) -> Region:
    """把区域夹取到归一化单位正方形内（保持正宽高）。"""
    x, y, w, h = region
    x0 = max(0.0, min(1.0, x))
    y0 = max(0.0, min(1.0, y))
    x1 = max(0.0, min(1.0, x + w))
    y1 = max(0.0, min(1.0, y + h))
    return (x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))


def transform_point(matrix: np.ndarray, x: float, y: float) -> Point:
    """单应性变换单个归一化点。"""
    v = matrix @ np.array([x, y, 1.0], dtype=np.float64)
    w_ = v[2] if abs(v[2]) > 1e-12 else 1.0
    return (float(v[0] / w_), float(v[1] / w_))


def transform_region_corners(matrix: np.ndarray, region: Region) -> List[Point]:
    x, y, w, h = region
    corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
    return [transform_point(matrix, cx, cy) for cx, cy in corners]


def _cosmetic_pen(color: QColor, width: float, dash: bool = False) -> QPen:
    pen = QPen(color, width)
    pen.setCosmetic(True)
    if dash:
        pen.setStyle(Qt.PenStyle.DashLine)
    return pen


# ================================================================
# Overlay 主体
# ================================================================

class _HandleItem(QGraphicsRectItem):
    """角点控制点：屏幕固定尺寸（ItemIgnoresTransformations）。"""

    def __init__(self, parent: "TrackerOverlay") -> None:
        half = HANDLE_SIZE / 2.0
        super().__init__(-half, -half, HANDLE_SIZE, HANDLE_SIZE, parent)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)  # 事件交由 overlay 统一处理
        self.setZValue(Z_CURRENT)
        self.setPen(_cosmetic_pen(QColor(255, 255, 255, 200), 1))
        self.setBrush(QColor(0, 0, 0, 160))
        self.hide()


class TrackerOverlay(QGraphicsObject):
    """Tracker 画布覆盖层（独立图层，不污染蒙版图形）。"""

    # 拖动释放后的提交信号（归一化区域）
    pattern_region_committed = pyqtSignal(tuple)
    search_region_committed = pyqtSignal(tuple)
    # 手动修正拖动释放（归一化区域，当前帧）
    current_region_committed = pyqtSignal(tuple)
    # 透视模式手动修正：一次提交完整四角，顺序 TL/TR/BR/BL
    homography_corners_committed = pyqtSignal(list)
    # 失败标记点击 → 帧号（供跳转）
    failed_marker_clicked = pyqtSignal(int)
    state_changed = pyqtSignal(object)

    def __init__(self, parent: Optional[QGraphicsItem] = None) -> None:
        super().__init__(parent)
        self.setZValue(Z_SEARCH)  # 图层主体位于蒙版之上；内部按绘制顺序分层
        self.setAcceptedMouseButtons(
            Qt.MouseButton.LeftButton | Qt.MouseButton.RightButton
        )
        self.setAcceptHoverEvents(True)

        self._frame_w = 0.0
        self._frame_h = 0.0
        self._pattern: Optional[Region] = None
        self._search: Optional[Region] = None
        self._features: List[Tuple[float, float, FeaturePointStatus]] = []
        self._path: List[Point] = []               # 完整路径（归一化，不抽样）
        self._failed_markers: Dict[int, Point] = {}
        self._current_transform: Optional[np.ndarray] = None
        self._tracking_mode = "trs"
        self._homography_corners: Optional[List[Point]] = None
        self._locked_homography_corners: set[int] = set()
        self._manual_correction_active = False
        self._state = OverlayState.IDLE
        self._visible = False
        # AUD-25801：手动修正拖动中的实时预览区域（显示用，未提交模型）。
        # 任何外部状态刷新（新变换/新基准区域/新四角）都会清掉它。
        self._preview_region: Optional[Region] = None

        # 拖动状态
        self._drag: Optional[dict] = None

        # 角点控制点（0-3: pattern 四角，4-7: search 四角）
        self._handles: List[_HandleItem] = [
            _HandleItem(self) for _ in range(8)
        ]
        # 透视模式额外提供 4 个角点和 4 个边中点；这些控件不复用
        # pattern/search 的轴对齐句柄，避免拖动时改变分析区域。
        self._homography_handles: List[_HandleItem] = [
            _HandleItem(self) for _ in range(8)
        ]
        for handle in self._homography_handles:
            handle.setZValue(Z_CURRENT + 0.1)
            handle.setPen(_cosmetic_pen(HOMOGRAPHY_COLOR, 1.0))

    # ================================================================
    # 状态机
    # ================================================================

    @property
    def state(self) -> OverlayState:
        return self._state

    def set_state(self, state: OverlayState) -> None:
        if state == self._state:
            return
        self._state = state
        self.state_changed.emit(state)
        self.update()

    @property
    def can_start_tracking(self) -> bool:
        """包含性校验：search 必须包住 pattern 才允许开始跟踪。"""
        if self._pattern is None or self._search is None:
            return False
        return search_contains_pattern(self._search, self._pattern)

    @property
    def tracking_mode(self) -> str:
        return self._tracking_mode

    @property
    def is_homography_mode(self) -> bool:
        return self._tracking_mode in {"homography", "planar", "perspective"}

    def set_tracking_mode(self, mode: object) -> None:
        """设置 overlay 模式；接受字符串或 ``TrackingMode``。"""
        value = getattr(mode, "value", mode)
        self._tracking_mode = str(value).lower()
        self._sync_handles()
        self.update()

    def set_homography_corners(
        self, corners: Optional[List[Point] | Tuple[Point, ...]]
    ) -> None:
        """设置当前帧透视四角，使用归一化坐标 TL/TR/BR/BL。"""
        self._preview_region = None  # 显式四角覆盖拖动预览
        if corners is None:
            self._homography_corners = None
        else:
            if len(corners) != 4:
                raise ValueError("透视四角必须包含 4 个点")
            self._homography_corners = [
                (float(point[0]), float(point[1])) for point in corners
            ]
        self._sync_handles()
        self.update()

    def clear_homography_corners(self) -> None:
        """清除手动四角，恢复由当前变换矩阵计算。"""
        self.set_homography_corners(None)

    def homography_corners(self) -> Optional[List[Point]]:
        """返回当前透视四角的副本。"""
        corners = self._current_corners()
        return list(corners) if corners is not None else None

    def set_homography_corner_locked(self, index: int, locked: bool) -> None:
        if index < 0 or index >= 4:
            raise IndexError("透视角点索引必须在 0-3")
        if locked:
            self._locked_homography_corners.add(index)
        else:
            self._locked_homography_corners.discard(index)
        self._sync_handles()
        self.update()

    def locked_homography_corners(self) -> set[int]:
        return set(self._locked_homography_corners)

    # ================================================================
    # 数据输入（全部归一化坐标）
    # ================================================================

    def set_frame_size(self, width: float, height: float) -> None:
        self._frame_w = float(width)
        self._frame_h = float(height)
        self.prepareGeometryChange()
        self._sync_handles()
        self.update()

    def set_pattern_region(self, region: Optional[Region]) -> None:
        self._pattern = tuple(float(v) for v in region) if region else None
        self._preview_region = None  # 基准区域变化，拖动预览失效
        self._sync_handles()
        self.update()

    def set_search_region(self, region: Optional[Region]) -> None:
        self._search = tuple(float(v) for v in region) if region else None
        self._sync_handles()
        self.update()

    def set_features(
        self, points: List[Tuple[float, float, FeaturePointStatus]]
    ) -> None:
        self._features = list(points)
        self.update()

    def set_path(self, points: List[Point]) -> None:
        """设置完整跟踪路径（原始数据保留，绘制时才抽样）。"""
        self._path = list(points)
        self.update()

    def path_points(self) -> List[Point]:
        """完整路径数据（不因显示抽样丢失）。"""
        return list(self._path)

    def display_path_points(self) -> List[Point]:
        return sample_path(self._path)

    def set_failed_markers(self, markers: Dict[int, Point]) -> None:
        self._failed_markers = dict(markers)
        self.update()

    def set_current_transform(self, matrix: Optional[np.ndarray]) -> None:
        self._current_transform = (
            np.asarray(matrix, dtype=np.float64).reshape(3, 3)
            if matrix is not None else None
        )
        # 外部模型状态刷新（下一帧变换到达），拖动预览让位。
        self._preview_region = None
        self.update()

    def set_overlay_active(self, active: bool) -> None:
        """显示/隐藏整个图层（不影响数据）。"""
        self._visible = active
        if not active:
            self._preview_region = None
        self.setVisible(active)
        self._sync_handles()
        self.update()

    # ================================================================
    # 坐标映射
    # ================================================================

    def _to_scene(self, x: float, y: float) -> QPointF:
        return QPointF(x * self._frame_w, y * self._frame_h)

    def _to_norm(self, pos: QPointF) -> Point:
        if self._frame_w <= 0 or self._frame_h <= 0:
            return (0.0, 0.0)
        return (pos.x() / self._frame_w, pos.y() / self._frame_h)

    def _current_corners(self) -> Optional[List[Point]]:
        """当前帧四角：拖动预览优先，其次透视手动修正，最后矩阵实时求值。"""
        if self._pattern is None:
            return None
        if self._preview_region is not None:
            return self._region_corners(self._preview_region)
        if self._homography_corners is not None:
            return list(self._homography_corners)
        if self._current_transform is None:
            return None
        return transform_region_corners(self._current_transform, self._pattern)

    @staticmethod
    def _edge_midpoints(corners: List[Point]) -> List[Point]:
        return [
            ((corners[0][0] + corners[1][0]) / 2.0,
             (corners[0][1] + corners[1][1]) / 2.0),
            ((corners[1][0] + corners[2][0]) / 2.0,
             (corners[1][1] + corners[2][1]) / 2.0),
            ((corners[2][0] + corners[3][0]) / 2.0,
             (corners[2][1] + corners[3][1]) / 2.0),
            ((corners[3][0] + corners[0][0]) / 2.0,
             (corners[3][1] + corners[0][1]) / 2.0),
        ]

    # ================================================================
    # QGraphicsItem 接口
    # ================================================================

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(0, 0, self._frame_w, self._frame_h)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem,
              widget=None) -> None:
        if self._frame_w <= 0 or self._frame_h <= 0:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 注：pattern/search 区域框不再绘制——跟踪直接复用 Mask 窗口
        # 遮罩（形状由蒙版图层显示），画布上只叠加跟踪要素（特征点/
        # 路径/当前帧目标框/失败标记）。区域数据保留用于算法计算。

        # ---- 1. 特征点（三色；失败点为红色叉号） ----
        for x, y, status in self._features:
            p = self._to_scene(x, y)
            if status == FeaturePointStatus.VALID:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(FEATURE_VALID_COLOR)
                painter.drawEllipse(p, FEATURE_RADIUS, FEATURE_RADIUS)
            elif status == FeaturePointStatus.WEAK:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(FEATURE_WEAK_COLOR)
                painter.drawEllipse(p, FEATURE_RADIUS, FEATURE_RADIUS)
            else:
                painter.setPen(_cosmetic_pen(FEATURE_FAILED_COLOR, 1.0))
                r = FEATURE_RADIUS + 1.0
                painter.drawLine(
                    QPointF(p.x() - r, p.y() - r), QPointF(p.x() + r, p.y() + r))
                painter.drawLine(
                    QPointF(p.x() - r, p.y() + r), QPointF(p.x() + r, p.y() - r))

        # ---- 2. 跟踪路径折线（>2000 点抽样显示） ----
        display = self.display_path_points()
        if len(display) >= 2:
            painter.setPen(_cosmetic_pen(PATH_COLOR, 1.0))
            path = QPainterPath()
            first = self._to_scene(*display[0])
            path.moveTo(first)
            for x, y in display[1:]:
                path.lineTo(self._to_scene(x, y))
            painter.drawPath(path)

        # ---- 3. 当前帧：变换后的 pattern / 透视平面 + 中心十字 ----
        corners = self._current_corners()
        if corners is not None:
            color = (
                HOMOGRAPHY_COLOR
                if self.is_homography_mode
                else (MANUAL_COLOR if self._manual_correction_active else ACTIVE_SHAPE_COLOR)
            )
            painter.setPen(_cosmetic_pen(color, 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            poly = [self._to_scene(x, y) for x, y in corners]
            for i in range(4):
                painter.drawLine(poly[i], poly[(i + 1) % 4])
            # 目标中心十字
            cx = sum(point[0] for point in corners) / 4.0
            cy = sum(point[1] for point in corners) / 4.0
            c = self._to_scene(cx, cy)
            painter.setPen(_cosmetic_pen(color, 1.5))
            painter.drawLine(QPointF(c.x() - 8, c.y()), QPointF(c.x() + 8, c.y()))
            painter.drawLine(QPointF(c.x(), c.y() - 8), QPointF(c.x(), c.y() + 8))
            # 透视方向指示：每个角点指向质心的固定长度短线（透视模式下显示）
            if self.is_homography_mode:
                for x, y in corners:
                    p = self._to_scene(x, y)
                    vx, vy = c.x() - p.x(), c.y() - p.y()
                    length = math.hypot(vx, vy)
                    if length > 1e-6:
                        vx *= DIRECTION_LINE_PX / length
                        vy *= DIRECTION_LINE_PX / length
                        painter.drawLine(
                            p, QPointF(p.x() + vx, p.y() + vy))

        # ---- 4. 失败帧红色三角（可点击跳转） ----
        if self._failed_markers:
            painter.setPen(_cosmetic_pen(INVALID_COLOR, 1.5))
            painter.setBrush(QColor(0xFF, 0x4D, 0x5E, 160))
            for pos in self._failed_markers.values():
                p = self._to_scene(*pos)
                tri = QPainterPath()
                tri.moveTo(p.x(), p.y() - 6)
                tri.lineTo(p.x() - 6, p.y() + 5)
                tri.lineTo(p.x() + 6, p.y() + 5)
                tri.closeSubpath()
                painter.drawPath(tri)

        painter.restore()

    # ================================================================
    # 控制点同步
    # ================================================================

    @staticmethod
    def _region_corners(region: Region) -> List[Point]:
        x, y, w, h = region
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]

    def _sync_handles(self) -> None:
        visible = self._visible and self._frame_w > 0
        # pattern/search 手柄不再显示：跟踪区域由 Mask 窗口遮罩承担，
        # 画布上不存在可拖动的区域框（数据保留供算法计算）
        for handle in self._handles:
            handle.hide()

        corners = self._current_corners()
        show_homography = visible and self.is_homography_mode and corners is not None
        if not show_homography:
            for handle in self._homography_handles:
                handle.hide()
            return
        positions = list(corners) + self._edge_midpoints(corners)
        for i, handle in enumerate(self._homography_handles):
            handle.setPos(self._to_scene(*positions[i]))
            if i < 4 and i in self._locked_homography_corners:
                handle.setBrush(LOCKED_HANDLE_COLOR)
            else:
                handle.setBrush(QColor(0, 0, 0, 160))
            handle.show()

    # ================================================================
    # 命中与拖动（按下/拖动/释放三阶段；释放才提交）
    # ================================================================

    def _hit_part(self, scene_pos: QPointF) -> Optional[Tuple[str, int]]:
        """返回 (part, corner_index)：part ∈ homography_corner/homography_edge/
        current，corner_index=-1 表示内部拖动。

        pattern/search 区域框已移除（跟踪复用 Mask 窗口遮罩），不再命中；
        空白处返回 None 使鼠标事件穿透到蒙版图层，跟踪时仍可拖动窗口。
        """
        tol = HIT_TOLERANCE
        # 透视平面的角点/边中点优先于普通 pattern/search 控件。
        if self.is_homography_mode:
            for i, handle in enumerate(self._homography_handles):
                if not handle.isVisible():
                    continue
                dp = handle.scenePos() - scene_pos
                if math.hypot(dp.x(), dp.y()) <= tol:
                    if i < 4 and i in self._locked_homography_corners:
                        return None
                    return ("homography_corner" if i < 4 else "homography_edge", i % 4)
        norm = self._to_norm(scene_pos)
        # 当前帧变换后的 pattern（手动修正拖动）
        corners = self._current_corners()
        if corners is not None:
            if _point_in_polygon(norm, corners):
                return ("homography" if self.is_homography_mode else "current", -1)
        return None

    def _hit_failed_marker(self, scene_pos: QPointF) -> Optional[int]:
        for frame, pos in self._failed_markers.items():
            p = self._to_scene(*pos)
            dp = p - scene_pos
            if math.hypot(dp.x(), dp.y()) <= HIT_TOLERANCE:
                return frame
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        pos = event.scenePos()
        frame = self._hit_failed_marker(pos)
        if frame is not None and event.button() == Qt.MouseButton.LeftButton:
            self.failed_marker_clicked.emit(frame)
            event.accept()
            return
        part = self._hit_part(pos)
        if part is None or event.button() != Qt.MouseButton.LeftButton:
            event.ignore()  # 等价于 QGraphicsItem 默认行为：不拦截
            return
        kind, corner = part
        # 手动修正（current/homography）：以基准 pattern 区域为拖动起点
        base_region = self._pattern
        if kind in {"homography_corner", "homography_edge"}:
            corners = self._current_corners()
            if corners is None:
                event.ignore()
                return
            index = corner % 4
            self._drag = {
                "kind": kind,
                "corner": index,
                "start_corners": list(corners),
                "start_norm": self._to_norm(pos),
            }
            self.set_state(OverlayState.MANUAL_CORRECTION)
            self._manual_correction_active = True
            event.accept()
            return
        if base_region is None:
            event.ignore()
            return
        self._drag = {
            "kind": kind,
            "corner": corner,
            "start_region": tuple(base_region),
            "start_norm": self._to_norm(pos),
        }
        self.set_state(OverlayState.MANUAL_CORRECTION)
        self._manual_correction_active = True
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is None:
            event.ignore()
            return
        norm = self._to_norm(event.scenePos())
        dx = norm[0] - self._drag["start_norm"][0]
        dy = norm[1] - self._drag["start_norm"][1]
        kind = self._drag["kind"]
        if kind in {"homography_corner", "homography_edge"}:
            self._update_homography_drag(dx, dy)
            self._sync_handles()
            self.update()
            event.accept()
            return
        region = self._dragged_region(dx, dy)
        # AUD-25801：拖动过程把新区域写入显示预览（释放才提交模型），
        # 目标框/十字随鼠标实时移动；此前 region 是死存储，用户盲拖。
        self._preview_region = tuple(region)
        self._sync_handles()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag is None:
            event.ignore()
            return
        norm = self._to_norm(event.scenePos())
        dx = norm[0] - self._drag["start_norm"][0]
        dy = norm[1] - self._drag["start_norm"][1]
        kind = self._drag["kind"]
        if kind in {"homography_corner", "homography_edge"}:
            self._update_homography_drag(dx, dy)
            corners = self._current_corners() or []
            self._drag = None
            self.set_state(OverlayState.TRACKED)
            self._manual_correction_active = False
            self.homography_corners_committed.emit(list(corners))
            self._sync_handles()
            self.update()
            event.accept()
            return
        region = self._dragged_region(dx, dy)
        self._drag = None
        self.set_state(OverlayState.TRACKED)
        self._manual_correction_active = False
        # 保留最终预览：提交信号当前无下游消费者回填 overlay 显示，
        # 直接清掉会让目标框跳回拖动前位置；下一次外部状态刷新
        # （新帧变换/新基准区域/显式四角）会自然清掉预览。
        self._preview_region = tuple(region)
        self.current_region_committed.emit(tuple(region))
        self._sync_handles()
        self.update()
        event.accept()

    def _update_homography_drag(self, dx: float, dy: float) -> None:
        """只更新 overlay 中的透视四角，释放时才发出提交信号。"""
        start = self._drag.get("start_corners") if self._drag else None
        if not start:
            return
        index = int(self._drag["corner"])
        corners = list(start)
        if self._drag["kind"] == "homography_corner":
            if index in self._locked_homography_corners:
                return
            x, y = corners[index]
            corners[index] = (x + dx, y + dy)
        else:
            # 边中点拖动：移动该边两个端点，保持相邻角点关系。
            # AUD-25802：锁定角点保持锚定——位移只作用于未锁定的端点，
            # 两端都锁定时该边整体不可拖动（与 _hit_part 拒绝直接抓取
            # 锁定角点的语义一致）。
            a, b = index, (index + 1) % 4
            if a in self._locked_homography_corners and \
                    b in self._locked_homography_corners:
                return
            if a not in self._locked_homography_corners:
                corners[a] = (corners[a][0] + dx, corners[a][1] + dy)
            if b not in self._locked_homography_corners:
                corners[b] = (corners[b][0] + dx, corners[b][1] + dy)
        self._homography_corners = corners

    def _dragged_region(self, dx: float, dy: float) -> Region:
        """按拖动状态计算新区域（内部平移 / 角点缩放），夹取到画面内。"""
        x, y, w, h = self._drag["start_region"]
        corner = self._drag["corner"]
        if corner < 0:
            return clamp_region((x + dx, y + dy, w, h))
        # 角点拖动 = 缩放：固定对角，移动被拖角
        x1, y1 = x + w, y + h
        cx, cy = {
            0: (x, y), 1: (x1, y), 2: (x1, y1), 3: (x, y1),
        }[corner]
        nx, ny = cx + dx, cy + dy
        # 对角固定点
        fx, fy = {
            0: (x1, y1), 1: (x, y1), 2: (x, y), 3: (x1, y),
        }[corner]
        return clamp_region((
            min(nx, fx), min(ny, fy),
            abs(nx - fx), abs(ny - fy),
        ))


# ================================================================
# 命中辅助
# ================================================================

def _point_in_polygon(p: Point, corners: List[Point]) -> bool:
    """射线法点在四边形内判定。"""
    inside = False
    n = len(corners)
    j = n - 1
    for i in range(n):
        xi, yi = corners[i]
        xj, yj = corners[j]
        if (yi > p[1]) != (yj > p[1]) and (
            p[0] < (xj - xi) * (p[1] - yi) / (yj - yi + 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside
