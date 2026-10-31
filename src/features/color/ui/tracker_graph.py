"""Tracker 曲线图（阶段 5，计划 §5.6）。

自定义 ``QWidget`` + ``QPainter``（不用 QChart），保证视觉可控：
- 背景 ``#111216``；上方时间刻度区 22 px；左侧数值刻度 38 px
- 水平网格每 25% 一条细线；当前帧 1 px 红色竖线 + 8 px 红色三角标
- 五色曲线：平移X ``#00E0B0`` / 平移Y ``#4DA3FF`` / 缩放 ``#FF38A8`` /
  旋转 ``#FFD43B`` / 透视 ``#C77DFF``（3D 深度 ``#FF9F43`` 预留）
- 关键帧 6 px 菱形；手动修正紫色菱形；失败帧红色三角
- 片段模式（整段轨迹）/ 帧模式（参考帧附近）；滚轮缩放、F 适配、Home 回参考帧
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QPoint, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QCheckBox, QHBoxLayout, QVBoxLayout, QWidget
from src.shared.i18n.i18n import tr

SeriesPoint = Tuple[int, float]

GRAPH_BG = QColor(0x11, 0x12, 0x16)
GRID_COLOR = QColor(0x2A, 0x2C, 0x33)
AXIS_TEXT_COLOR = QColor(0x8C, 0x8C, 0x8C)
PLAYHEAD_COLOR = QColor(0xFF, 0x40, 0x40)
KEYFRAME_COLOR = QColor(0xD8, 0xD8, 0xD8)
MANUAL_KEY_COLOR = QColor(0xC7, 0x7D, 0xFF)
FAILED_KEY_COLOR = QColor(0xFF, 0x4D, 0x5E)

TIME_RULER_H = 22      # 上方时间刻度区高度
VALUE_AXIS_W = 38      # 左侧数值刻度宽度
SCROLLBAR_W = 10       # 右侧滚动条预留宽度
PLAYHEAD_TRI = 8       # 播放头三角标尺寸
KEYFRAME_SIZE = 6      # 关键帧菱形尺寸

# 曲线分量：key → (颜色, 中文标签)
CURVE_COMPONENTS = {
    "tx": (QColor(0x00, 0xE0, 0xB0), "平移 X"),
    "ty": (QColor(0x4D, 0xA3, 0xFF), "平移 Y"),
    "scale": (QColor(0xFF, 0x38, 0xA8), "缩放"),
    "rotation": (QColor(0xFF, 0xD4, 0x3B), "旋转"),
    "perspective": (QColor(0xC7, 0x7D, 0xFF), "透视"),
    "depth": (QColor(0xFF, 0x9F, 0x43), "3D 深度"),
}

# 复选框行显示的分量（计划：平移/竖移/缩放/旋转）
CHECKBOX_KEYS = ("tx", "ty", "scale", "rotation")

FRAME_MODE_WINDOW = 30  # 帧模式显示参考帧 ±30 帧


# ================================================================
# 纯函数辅助（可单测）
# ================================================================

def value_range(points_by_key: Dict[str, List[SeriesPoint]],
                keys: Optional[List[str]] = None) -> Tuple[float, float]:
    """所有可见序列的值域 (vmin, vmax)，空数据返回 (0, 1)。"""
    vmin, vmax = None, None
    use = keys if keys is not None else list(points_by_key.keys())
    for key in use:
        for _, v in points_by_key.get(key, []):
            if vmin is None or v < vmin:
                vmin = v
            if vmax is None or v > vmax:
                vmax = v
    if vmin is None:
        return (0.0, 1.0)
    if vmax - vmin < 1e-9:
        return (vmin - 0.5, vmax + 0.5)
    pad = (vmax - vmin) * 0.1
    return (vmin - pad, vmax + pad)


def frame_range(points_by_key: Dict[str, List[SeriesPoint]]) -> Tuple[int, int]:
    """所有序列覆盖的帧范围；空数据返回 (0, 0)。"""
    lo, hi = None, None
    for pts in points_by_key.values():
        for f, _ in pts:
            if lo is None or f < lo:
                lo = f
            if hi is None or f > hi:
                hi = f
    return (lo or 0, hi or 0)


def downsample(points: List[SeriesPoint], max_pts: int) -> List[SeriesPoint]:
    """显示降采样（保首尾），原始数据由调用方保留。"""
    n = len(points)
    if n <= max_pts:
        return list(points)
    stride = -(-n // max_pts)
    sampled = list(points[::stride])
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


# ================================================================
# 绘图视图
# ================================================================

class _GraphView(QWidget):
    """曲线绘制区（纯绘制 + 播放头定位与缩放交互）。"""

    frame_clicked = pyqtSignal(int)

    def __init__(self, parent: Optional["TrackerGraph"] = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        self._series: Dict[str, List[SeriesPoint]] = {}
        self._visible = {k: True for k in CURVE_COMPONENTS}
        self._current_frame = 0
        self._reference_frame = 0
        self._keyframes: List[int] = []
        self._manual_frames: List[int] = []
        self._failed_frames: List[int] = []
        self._mode = "clip"          # clip / frame
        self._hover: Optional[QPoint] = None

        # 视图范围（帧轴），None = 自动适配
        self._view_lo: Optional[float] = None
        self._view_hi: Optional[float] = None

    # ---------------- 数据输入 ----------------

    def set_series(self, series: Dict[str, List[SeriesPoint]]) -> None:
        self._series = {k: list(v) for k, v in series.items()}
        self.update()

    def set_component_visible(self, key: str, visible: bool) -> None:
        self._visible[key] = visible
        self.update()

    def set_current_frame(self, frame: int) -> None:
        self._current_frame = int(frame)
        self.update()

    def set_reference_frame(self, frame: int) -> None:
        self._reference_frame = int(frame)
        self.update()

    def set_markers(self, keyframes: List[int], manual: List[int],
                    failed: List[int]) -> None:
        self._keyframes = list(keyframes)
        self._manual_frames = list(manual)
        self._failed_frames = list(failed)
        self.update()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._view_lo = self._view_hi = None
        self.update()

    # ---------------- 坐标映射 ----------------

    def _plot_rect(self) -> QRectF:
        w = self.width() - VALUE_AXIS_W - SCROLLBAR_W
        h = self.height() - TIME_RULER_H
        return QRectF(VALUE_AXIS_W, TIME_RULER_H, max(10.0, w), max(10.0, h))

    def _frame_extent(self) -> Tuple[float, float]:
        if self._view_lo is not None and self._view_hi is not None:
            return (self._view_lo, self._view_hi)
        if self._mode == "frame":
            return (self._reference_frame - FRAME_MODE_WINDOW,
                    self._reference_frame + FRAME_MODE_WINDOW)
        lo, hi = frame_range(self._series)
        if hi <= lo:
            hi = lo + 1
        return (float(lo), float(hi))

    def frame_to_x(self, frame: float) -> float:
        lo, hi = self._frame_extent()
        rect = self._plot_rect()
        if hi <= lo:
            return rect.left()
        return rect.left() + (frame - lo) / (hi - lo) * rect.width()

    def x_to_frame(self, x: float) -> float:
        lo, hi = self._frame_extent()
        rect = self._plot_rect()
        if rect.width() <= 0:
            return lo
        return lo + (x - rect.left()) / rect.width() * (hi - lo)

    def value_to_y(self, value: float, vmin: float, vmax: float) -> float:
        rect = self._plot_rect()
        if vmax <= vmin:
            return rect.center().y()
        t = (value - vmin) / (vmax - vmin)
        return rect.bottom() - t * rect.height()

    def fit_all(self) -> None:
        self._view_lo = self._view_hi = None
        self.update()

    def go_to_reference(self) -> None:
        lo = self._reference_frame - FRAME_MODE_WINDOW
        self._view_lo, self._view_hi = float(lo), float(
            self._reference_frame + FRAME_MODE_WINDOW)
        self.update()

    # ---------------- 绘制 ----------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), GRAPH_BG)
        rect = self._plot_rect()
        visible_keys = [k for k in CURVE_COMPONENTS
                        if self._visible.get(k) and self._series.get(k)]
        vmin, vmax = value_range(self._series, visible_keys)

        self._draw_grid(painter, rect, vmin, vmax)
        self._draw_time_ruler(painter)
        for key in visible_keys:
            self._draw_curve(painter, key, vmin, vmax)
        self._draw_last_values(painter, visible_keys, vmin, vmax)
        self._draw_markers(painter, vmin, vmax)
        self._draw_playhead(painter, rect)
        if self._hover is not None:
            self._draw_hover(painter, vmin, vmax)

    def _draw_grid(self, painter: QPainter, rect: QRectF,
                   vmin: float, vmax: float) -> None:
        pen = QPen(GRID_COLOR, 1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        # 水平网格：每 25% 一条
        for i in range(1, 4):
            y = rect.top() + rect.height() * i / 4.0
            painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        # 左侧数值刻度
        painter.setPen(AXIS_TEXT_COLOR)
        font = QFont()
        font.setPixelSize(9)
        painter.setFont(font)
        for i in range(5):
            v = vmax - (vmax - vmin) * i / 4.0
            y = rect.top() + rect.height() * i / 4.0
            painter.drawText(
                QRectF(0, y - 7, VALUE_AXIS_W - 4, 14),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{v:.2f}",
            )

    def _draw_time_ruler(self, painter: QPainter) -> None:
        lo, hi = self._frame_extent()
        painter.setPen(AXIS_TEXT_COLOR)
        font = QFont()
        font.setPixelSize(9)
        painter.setFont(font)
        step = max(1, int((hi - lo) / 8))
        f = int(lo)
        while f <= hi:
            x = self.frame_to_x(f)
            painter.drawLine(QPointF(x, TIME_RULER_H - 5), QPointF(x, TIME_RULER_H))
            painter.drawText(QPointF(x + 2, TIME_RULER_H - 7), str(f))
            f += step

    def _draw_curve(self, painter: QPainter, key: str,
                    vmin: float, vmax: float) -> None:
        color, _ = CURVE_COMPONENTS[key]
        pen = QPen(color, 1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        pts = downsample(self._series[key], 1500)
        prev: Optional[QPointF] = None
        frames = {f for f in self._failed_frames}
        for f, v in pts:
            p = QPointF(self.frame_to_x(f), self.value_to_y(v, vmin, vmax))
            if prev is not None and f not in frames:
                painter.drawLine(prev, p)  # 失败帧处断开，不画假连续
            prev = p

    def _draw_last_values(self, painter: QPainter, keys: List[str],
                          vmin: float, vmax: float) -> None:
        font = QFont()
        font.setPixelSize(9)
        painter.setFont(font)
        for key in keys:
            pts = self._series[key]
            if not pts:
                continue
            f, v = pts[-1]
            color, _ = CURVE_COMPONENTS[key]
            painter.setPen(color)
            # 末值标签靠近图表右边缘时向左收，避免被裁掉。
            x = min(self.frame_to_x(f) + 3, self.width() - 34)
            painter.drawText(
                QPointF(x, self.value_to_y(v, vmin, vmax) + 3),
                f"{v:.2f}",
            )

    def _draw_markers(self, painter: QPainter, vmin: float, vmax: float) -> None:
        half = KEYFRAME_SIZE / 2.0
        bottom = self._plot_rect().bottom()
        for f in self._keyframes:
            if f in self._manual_frames:
                continue
            x = self.frame_to_x(f)
            self._draw_diamond(painter, x, bottom - half - 2, half, KEYFRAME_COLOR)
        for f in self._manual_frames:
            x = self.frame_to_x(f)
            self._draw_diamond(painter, x, bottom - half - 2, half, MANUAL_KEY_COLOR)
        for f in self._failed_frames:
            x = self.frame_to_x(f)
            self._draw_triangle(painter, x, bottom - 3, FAILED_KEY_COLOR)

    @staticmethod
    def _draw_diamond(painter: QPainter, x: float, y: float, half: float,
                      color: QColor) -> None:
        poly = QPolygonF([
            QPointF(x, y - half), QPointF(x + half, y),
            QPointF(x, y + half), QPointF(x - half, y),
        ])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(poly)

    @staticmethod
    def _draw_triangle(painter: QPainter, x: float, y: float,
                       color: QColor) -> None:
        poly = QPolygonF([
            QPointF(x, y - 7), QPointF(x - 5, y), QPointF(x + 5, y),
        ])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(poly)

    def _draw_playhead(self, painter: QPainter, rect: QRectF) -> None:
        x = self.frame_to_x(self._current_frame)
        pen = QPen(PLAYHEAD_COLOR, 1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawLine(QPointF(x, 0), QPointF(x, rect.bottom()))
        self._draw_triangle(painter, x, PLAYHEAD_TRI, PLAYHEAD_COLOR)

    def _draw_hover(self, painter: QPainter, vmin: float, vmax: float) -> None:
        assert self._hover is not None
        hx = self._hover.x()
        frame = int(round(self.x_to_frame(hx)))
        painter.setPen(QPen(QColor(0xFF, 0xFF, 0xFF, 100), 1))
        painter.drawLine(QPointF(hx, TIME_RULER_H),
                         QPointF(hx, self._plot_rect().bottom()))
        label = f"frame {frame}"
        best: Optional[Tuple[float, str]] = None
        for key in CURVE_COMPONENTS:
            if not (self._visible.get(key) and self._series.get(key)):
                continue
            for f, v in self._series[key]:
                if f == frame:
                    name = CURVE_COMPONENTS[key][1]
                    y = self.value_to_y(v, vmin, vmax)
                    dist = abs(y - self._hover.y())
                    if best is None or dist < best[0]:
                        best = (dist, f"{name}: {v:.2f}")
        if best is not None:
            label += f"  {best[1]}"
        painter.setPen(QColor(0xD8, 0xD8, 0xD8))
        painter.drawText(QPointF(hx + 6, TIME_RULER_H + 12), label)

    # ---------------- 交互 ----------------

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._hover = event.position().toPoint()
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = None
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            frame = int(round(self.x_to_frame(event.position().x())))
            self.frame_clicked.emit(frame)

    def wheelEvent(self, event) -> None:  # noqa: N802
        lo, hi = self._frame_extent()
        center = (self.x_to_frame(event.position().x())
                  if not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                  else (lo + hi) / 2.0)
        factor = 0.8 if event.angleDelta().y() > 0 else 1.25
        half = max(1.0, (hi - lo) * factor / 2.0)
        self._view_lo, self._view_hi = center - half, center + half
        self.update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_F:
            self.fit_all()
        elif event.key() == Qt.Key.Key_Home:
            self.go_to_reference()
        else:
            super().keyPressEvent(event)


# ================================================================
# 组合件：复选框行 + 绘图区
# ================================================================

class TrackerGraph(QWidget):
    """曲线图区：可选分量复选框行 + 绘图区。

    TrackerPanel 的主布局把分量开关放在曲线图上方，直接对应达芬奇的
    ``平移 / 竖移 / 缩放 / 旋转 / 3D`` 行。因此这里保留默认复选框行
    以兼容独立使用和旧测试，同时允许面板调用方隐藏该行。
    """

    frame_clicked = pyqtSignal(int)

    def __init__(self, parent=None, show_controls: bool = True) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._checkboxes: Dict[str, QCheckBox] = {}
        for key in CHECKBOX_KEYS:
            _, fallback_label = CURVE_COMPONENTS[key]
            box = QCheckBox(tr(f"tracker.curve_{key}", fallback_label), self)
            box.setChecked(True)
            box.setStyleSheet(
                f"QCheckBox {{ color: {CURVE_COMPONENTS[key][0].name()}; "
                "font-size: 10px; }"
            )
            box.toggled.connect(
                lambda checked, k=key: self.view.set_component_visible(k, checked)
            )
            self._checkboxes[key] = box
            row.addWidget(box)
        row.addStretch(1)
        self._controls_widget = QWidget(self)
        self._controls_widget.setLayout(row)
        self._controls_widget.setVisible(show_controls)
        layout.addWidget(self._controls_widget)

        self.view = _GraphView(self)
        self.view.frame_clicked.connect(self.frame_clicked.emit)
        layout.addWidget(self.view, 1)

    # 数据接口透传

    def set_series(self, series: Dict[str, List[SeriesPoint]]) -> None:
        self.view.set_series(series)

    def set_current_frame(self, frame: int) -> None:
        self.view.set_current_frame(frame)

    def set_reference_frame(self, frame: int) -> None:
        self.view.set_reference_frame(frame)

    def set_markers(self, keyframes, manual, failed) -> None:
        self.view.set_markers(keyframes, manual, failed)

    def set_mode(self, mode: str) -> None:
        self.view.set_mode(mode)

    def set_component_visible(self, key: str, visible: bool) -> None:
        """设置指定曲线分量是否绘制。"""
        self.view.set_component_visible(key, visible)

    def checkbox(self, key: str) -> QCheckBox:
        return self._checkboxes[key]

    def set_controls_visible(self, visible: bool) -> None:
        """显示/隐藏内置分量开关行。"""
        self._controls_widget.setVisible(bool(visible))
