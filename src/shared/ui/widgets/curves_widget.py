import numpy as np
from typing import Dict, Optional, Any
from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QButtonGroup, QWidget
from PyQt6.QtCore import Qt, QEvent, QPointF, pyqtSignal, QRectF, QTimer
from PyQt6.QtGui import QPainter, QColor, QPen, QPainterPath, QMouseEvent
from src.shared.ui.widgets.gpu_scope_widget import GpuScopeWidget
from src.shared.gpu.scope_publication import ScopePublication

from src.shared.i18n.i18n import I18N
from src.shared.assets.icon_manager import icon_color_picker, icon_reset
from src.shared.ui.style_sheet import (
    C,
    CURVE_BTN_CONTAINER_STYLE,
    CURVE_PICKER_BTN_STYLE,
    CURVE_RESET_BTN_STYLE,
    curve_channel_btn_style,
)

class _CurveOverlay(QWidget):
    """Raster UI chrome stays visible even without a GPU scope frame."""

    def __init__(self, canvas: "CurvesWidget") -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setStyleSheet("background: transparent;")

    def paintEvent(self, event) -> None:
        self.canvas._paint_curve_overlay(self)


class CurvesWidget(QWidget):
    # Emits the sampled curve LUT and the control points.
    changed = pyqtSignal(np.ndarray, dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    histogram_geometry_changed = pyqtSignal()
    # Color Picker signals
    picker_toggled = pyqtSignal(bool)  # True = activate picker, False = deactivate
    color_received = pyqtSignal(float, float, float)  # r, g, b (0-255)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background-color: {C.COLOR_PANEL_INSET};")
        self._scope = GpuScopeWidget(self, preserve_aspect=False, show_value_axis=False)
        self._scope.hide()
        self._has_histogram = False
        self._overlay = _CurveOverlay(self)
        self._scope_resize_timer = QTimer(self)
        self._scope_resize_timer.setSingleShot(True)
        self._scope_resize_timer.setInterval(150)
        self._scope_resize_timer.timeout.connect(self.histogram_geometry_changed)
        self.setMinimumSize(300, 220)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        
        # Channel Data: 'M' (Master), 'R', 'G', 'B'
        self.channels = ['M', 'R', 'G', 'B']
        self.active_channel = 'M'
        self.curves = {
            'M': [QPointF(0, 255), QPointF(255, 0)],
            'R': [QPointF(0, 255), QPointF(255, 0)],
            'G': [QPointF(0, 255), QPointF(255, 0)],
            'B': [QPointF(0, 255), QPointF(255, 0)]
        }
        
        self.selected_point_idx = -1
        self.margin = 7
        # 曲线面板侧栏状态（语义见 layer.compose_curves_lut）：
        # - edit_amounts：YRGB 强度 0-100，100 = 曲线全额生效；
        # - edit_linked：链接编辑（拖动/加点/删点同步到全部通道）。
        # 默认值 = 既有单通道编辑行为，行为零回归。
        self.edit_amounts = {"Y": 100.0, "R": 100.0, "G": 100.0, "B": 100.0}
        self.edit_linked = False
        self.calibrated = False
        self.calibration_version = 0
        self._press_interaction = False
        self._drag_position: QPointF | None = None
        self._drag_points: dict[str, tuple[int, QPointF]] = {}
        
        # Color Picker state
        self._picker_active = False
        
        # === 缓存机制 ===
        self._lut_cache: Dict[str, np.ndarray] = {}
        self._lut_dirty: Dict[str, bool] = {ch: True for ch in self.channels}
        self._final_lut_cache: Optional[np.ndarray] = None
        self._final_lut_dirty: bool = True
        # AUD-38219: 绘制曲线采样按通道缓存，key 为该通道控制点坐标快照；
        # 任何点位变动都会使 key 失配从而重算，未脏通道不再每帧全量重算。
        self._curve_cache: Dict[str, Any] = {}
        
        # 节流定时器：限制信号发射频率为 60FPS (16ms)
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(16)
        self._update_timer.timeout.connect(self._emit_changed_signal)
        # 暂存数据：用于节流发射的数据
        self._pending_lut: Optional[np.ndarray] = None
        self._pending_data: Optional[Dict[str, Any]] = None
        
        self.init_ui()

    def init_ui(self):
        # We don't use a main layout for buttons anymore to allow them to float
        self.btn_container = QWidget(self)
        self.btn_container.setStyleSheet(CURVE_BTN_CONTAINER_STYLE)
        btn_layout = QHBoxLayout(self.btn_container)
        btn_layout.setContentsMargins(5, 5, 5, 5)
        btn_layout.setSpacing(4)
        
        self.group = QButtonGroup(self)
        colors = {'M': C.CHANNEL_M, 'R': C.CHANNEL_R, 'G': C.CHANNEL_G, 'B': C.CHANNEL_B}
        
        for ch in self.channels:
            btn = QPushButton(ch)
            btn.setCheckable(True)
            btn.setFixedSize(28, 24)
            if ch == 'M': btn.setChecked(True)
            
            btn.setStyleSheet(curve_channel_btn_style(colors[ch]))
            
            self.group.addButton(btn)
            btn_layout.addWidget(btn)
            btn.clicked.connect(lambda checked, c=ch: self.set_active_channel(c))
        
        # Color Picker Button (Eyedropper)
        self.picker_btn = QPushButton()
        self.picker_btn.setIcon(icon_color_picker())
        self.picker_btn.setFixedSize(28, 24)
        self.picker_btn.setCheckable(True)
        self.picker_btn.setToolTip(I18N.tr("tooltip.color_picker", "Pick color from image to add control point"))
        self.picker_btn.setStyleSheet(CURVE_PICKER_BTN_STYLE)
        self.picker_btn.toggled.connect(self._on_picker_toggled)
        btn_layout.addWidget(self.picker_btn)
        
        # Add Reset Button
        reset_btn = QPushButton()
        reset_btn.setIcon(icon_reset())
        reset_btn.setFixedSize(28, 24)
        reset_btn.setToolTip(I18N.tr("tooltip.reset_curves", "重置所有曲线"))
        reset_btn.setStyleSheet(CURVE_RESET_BTN_STYLE)
        reset_btn.clicked.connect(self._reset_from_toolbar)
        btn_layout.addWidget(reset_btn)
            
        self.btn_container.adjustSize()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scope.setGeometry(self.get_draw_rect().toAlignedRect())
        self._overlay.setGeometry(self.rect())
        self._overlay.raise_()
        # Position buttons at top-left, floating over the canvas
        self.btn_container.move(self.margin + 5, self.margin + 5)
        self.btn_container.raise_()
        self._scope_resize_timer.start()

    def histogram_display_metrics(self) -> tuple[tuple[int, int], float]:
        """Request a one-to-one device-pixel texture and one logical pixel stroke."""
        rect = self.get_draw_rect().toAlignedRect()
        dpr = self.devicePixelRatioF()
        return (max(1, round(rect.width() * dpr)), max(1, round(rect.height() * dpr))), dpr

    def event(self, event: QEvent) -> bool:
        if (event.type() == QEvent.Type.DevicePixelRatioChange
                and hasattr(self, '_scope_resize_timer')):
            self._scope_resize_timer.start()
        return super().event(event)

    def set_active_channel(self, ch):
        self.active_channel = ch
        self.selected_point_idx = -1
        self.update()

    def get_draw_rect(self):
        w, h = self.width(), self.height()
        return QRectF(self.margin, self.margin, w - 2 * self.margin, h - 2 * self.margin)

    def to_widget(self, x_img, y_img):
        rect = self.get_draw_rect()
        wx = rect.left() + (x_img / 255.0) * rect.width()
        wy = rect.top() + (y_img / 255.0) * rect.height()
        return wx, wy

    def from_widget(self, wx, wy):
        rect = self.get_draw_rect()
        mx = (wx - rect.left()) / rect.width() * 255
        my = (wy - rect.top()) / rect.height() * 255
        return mx, my

    def set_scope_frame(
        self, tex_id: Optional[int], width: int, height: int, *,
        publication: Optional[ScopePublication] = None,
    ) -> None:
        """Present histogram inside the same bounds used by curve coordinates."""
        self._scope.set_scope_frame(tex_id, width, height, publication=publication)
        self._has_histogram = tex_id is not None
        self._scope.setVisible(tex_id is not None)
        self._overlay.raise_()
        self.btn_container.raise_()
        self.update()

    def clear(self) -> None:
        self._scope.clear()
        self._has_histogram = False
        self._scope.hide()
        self.update()

    def _paint_curve_overlay(self, target: QWidget) -> None:
        painter = QPainter(target)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        rect = self.get_draw_rect()
        
        # Draw background
        self._draw_background(painter, rect)
        
        # Draw dual-layer grid
        self._draw_grid(painter, rect)

        # Define curve colors (soft but clear)
        inactive_colors = {
            'M': QColor(200, 200, 200, 60),
            'R': QColor(255, 100, 100, 60),
            'G': QColor(100, 255, 100, 60),
            'B': QColor(100, 150, 255, 60)
        }
        active_colors = {
            'M': QColor(255, 255, 255),
            'R': QColor(255, 100, 100),
            'G': QColor(100, 255, 100),
            'B': QColor(100, 150, 255)
        }

        # Draw edited channels underneath the active one.
        default_points = [QPointF(0, 255), QPointF(255, 0)]
        for ch in self.channels:
            if ch == self.active_channel or self.curves[ch] == default_points:
                continue
            self._draw_curve(painter, ch, inactive_colors[ch], rect, thickness=1.0)

        # Draw Active Curve with thicker line
        self._draw_curve(painter, self.active_channel, active_colors[self.active_channel], rect, thickness=1.2)

        # Draw Control Points for Active Channel
        self._draw_control_points(painter, active_colors[self.active_channel], rect)
        painter.end()

    def _draw_background(self, painter: QPainter, rect: QRectF):
        """Draw the curve surface with a fine black outer frame."""
        if not self._has_histogram:
            painter.setBrush(QColor(C.COLOR_PANEL_INSET))
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(7, 7, 8), 0.8))
        painter.drawRect(rect)

    def _draw_grid(self, painter: QPainter, rect: QRectF):
        """Draw fine continuous grid lines with a subtly stronger major grid."""
        x_divisions = max(4, round(rect.width() / 27.0))
        y_divisions = max(4, round(rect.height() / 14.0))
        major_every = 5

        minor_pen = QPen(QColor(75, 75, 80, 52), 0.55)
        major_pen = QPen(QColor(92, 92, 97, 65), 0.75)
        for divisions, vertical in ((x_divisions, True), (y_divisions, False)):
            for i in range(1, divisions):
                x = rect.left() + rect.width() * i / divisions
                y = rect.top() + rect.height() * i / divisions
                painter.setPen(major_pen if i % major_every == 0 else minor_pen)
                if vertical:
                    painter.drawLine(round(x), round(rect.top()), round(x), round(rect.bottom()))
                else:
                    painter.drawLine(round(rect.left()), round(y), round(rect.right()), round(y))

    def _cached_curve_values(self, ch: str, points) -> np.ndarray:
        """AUD-38219: 按控制点坐标快照缓存 calculate_curve 的 256 点采样。

        key 含通道全部控制点的 (x, y)——任何增删/拖动都会导致 key 失配
        而重算，未脏通道在重绘时直接复用上一次结果。
        """
        key = tuple((p.x(), p.y()) for p in points)
        cached = self._curve_cache.get(ch)
        if cached is not None and cached[0] == key:
            return cached[1]
        vals = self.calculate_curve(points)
        self._curve_cache[ch] = (key, vals)
        return vals

    def _draw_curve(self, painter: QPainter, ch: str, color: QColor, rect: QRectF, thickness: float = 1.5):
        """Draw smooth curve with high-density sampling for ultra-smooth rendering."""
        points = self.curves[ch]
        if len(points) < 2:
            return
        
        vals = self._cached_curve_values(ch, points)
        path = QPainterPath()
        path.moveTo(*self.to_widget(0, vals[0]))
        for x in range(1, len(vals)):
            path.lineTo(*self.to_widget(x, vals[x]))
        
        painter.setPen(QPen(color, thickness))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    def _draw_control_points(self, painter: QPainter, active_color: QColor, rect: QRectF):
        """Draw control points with professional styling (hollow/filled + halo effect)."""
        points = self.curves[self.active_channel]
        
        for i, p in enumerate(points):
            wx, wy = self.to_widget(p.x(), p.y())
            is_selected = (i == self.selected_point_idx)
            
            if is_selected:
                # Selected point: filled circle with halo effect
                # Draw halo (glow) first
                halo_color = QColor(active_color)
                halo_color.setAlpha(60)
                painter.setBrush(halo_color)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(QPointF(wx, wy), 9, 9)
                
                # Draw filled circle
                painter.setBrush(active_color)
                painter.setPen(QPen(QColor(0, 0, 0), 1.5))
                painter.drawEllipse(QPointF(wx, wy), 5, 5)
            else:
                # Unselected point: hollow circle with background fill
                painter.setBrush(QColor(C.COLOR_PANEL_INSET))
                painter.setPen(QPen(QColor(160, 160, 160), 1.5))
                painter.drawEllipse(QPointF(wx, wy), 4, 4)

    def calculate_curve(self, points: list[QPointF]) -> np.ndarray:
        xs = np.array([point.x() for point in points])
        ys = np.array([point.y() for point in points])
        if self.calibrated:
            from src.features.color.core.curves_calibration import calibrated_curve_samples

            mapped = calibrated_curve_samples(xs, ys, version=self.calibration_version)
            return 255.0 * (1.0 - np.clip(mapped, 0.0, 1.0))
        from src.features.color.core.layer import _interpolate_curve_255

        sampled = _interpolate_curve_255(list(zip(xs, ys)))
        return 255.0 - np.arange(256) if sampled is None else 255.0 * (1.0 - sampled)

    def _begin_point_edit(self) -> None:
        if not self._press_interaction:
            self._press_interaction = True
            self.interaction_started.emit()

    def _add_curve_anchor(self, channel: str, x: float, y: float) -> int:
        points = self.curves[channel]
        for index, point in enumerate(points):
            if abs(point.x() - x) <= 0.5:
                return index
        anchor = QPointF(x, y)
        points.append(anchor)
        points.sort(key=lambda point: point.x())
        self._lut_dirty[channel] = True
        return points.index(anchor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        pos = event.position()
        x, y = self.from_widget(pos.x(), pos.y())
        points = self.curves[self.active_channel]
        offsets = [(QPointF(*self.to_widget(p.x(), p.y())) - pos, i) for i, p in enumerate(points)]
        distance_squared, index = min(
            ((offset.x() ** 2 + offset.y() ** 2, i) for offset, i in offsets),
            default=(float("inf"), -1),
        )
        clicked = index if distance_squared <= 12.0 ** 2 else -1

        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_position = None
            self._drag_points.clear()
            self.selected_point_idx = -1
            if clicked < 0:
                rect = self.get_draw_rect()
                if not rect.adjusted(-5, -5, 5, 5).contains(pos):
                    return
                x = min(max(x, 0.0), 255.0)
                y = min(max(y, 0.0), 255.0)
                if any(abs(point.x() - x) <= 0.5 for point in points):
                    return
                if self.calibrated:
                    samples = self._cached_curve_values(self.active_channel, points)
                    axis = np.arange(len(samples))
                    dx = rect.left() + axis * rect.width() / 255.0 - pos.x()
                    dy = rect.top() + samples * rect.height() / 255.0 - pos.y()
                    if np.min(dx * dx + dy * dy) > 9.0 ** 2:
                        return
                    if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                        y = float(np.interp(x, axis, samples))
                self._begin_point_edit()
                clicked = self._add_curve_anchor(self.active_channel, x, y)
                if self.edit_linked:
                    for channel in self.channels:
                        if channel != self.active_channel:
                            self._add_curve_anchor(channel, x, y)
                self.update_lut()
            self.selected_point_idx = clicked
            anchor = points[clicked]
            self._drag_position = pos
            self._drag_points[self.active_channel] = (clicked, QPointF(anchor))
            if self.edit_linked:
                for channel in self.channels:
                    if channel == self.active_channel:
                        continue
                    for index, peer in enumerate(self.curves[channel]):
                        if abs(peer.x() - anchor.x()) <= 0.5:
                            self._drag_points[channel] = (index, QPointF(peer))
                            break
        elif event.button() == Qt.MouseButton.RightButton and clicked >= 0 and len(points) > 2:
            self.interaction_started.emit()
            removed_x = points.pop(clicked).x()
            self.selected_point_idx = -1
            self._lut_dirty[self.active_channel] = True
            if self.edit_linked:
                for channel in self.channels:
                    if channel == self.active_channel or len(self.curves[channel]) <= 2:
                        continue
                    for peer in self.curves[channel]:
                        if abs(peer.x() - removed_x) <= 0.5:
                            self.curves[channel].remove(peer)
                            self._lut_dirty[channel] = True
                            break
            self.update_lut()
            self.flush()
            self.interaction_ended.emit()
        self.update()

    def _constrain_anchor(self, points: list[QPointF], index: int, x: float, y: float) -> QPointF:
        x = min(max(x, 0.0), 255.0)
        y = min(max(y, 0.0), 255.0)
        if index == 0:
            if x < 255.0 - y:
                x = 0.0
            else:
                y = 255.0
        elif index == len(points) - 1:
            if 255.0 - x < y:
                x = 255.0
            else:
                y = 0.0
        lower = points[index - 1].x() + 0.1 if index else 0.0
        upper = points[index + 1].x() - 0.1 if index + 1 < len(points) else 255.0
        return QPointF(min(max(x, lower), upper), y)

    def _move_curve_anchors(self, position: QPointF) -> None:
        if (self._drag_position is None or position == self._drag_position
                or self.active_channel not in self._drag_points):
            return
        self._drag_position = position
        x, y = self.from_widget(position.x(), position.y())
        index, anchor = self._drag_points[self.active_channel]
        active = self._constrain_anchor(self.curves[self.active_channel], index, x, y)
        changes = {self.active_channel: (index, active)}
        for channel, (index, peer) in self._drag_points.items():
            if channel != self.active_channel:
                x = active.x() + (peer.x() - anchor.x()) if self.calibrated else peer.x()
                y = active.y() + (peer.y() - anchor.y())
                changes[channel] = (index, self._constrain_anchor(self.curves[channel], index, x, y))
        updates = [(channel, index, point) for channel, (index, point) in changes.items()
                   if point != self.curves[channel][index]]
        if updates:
            self._begin_point_edit()
            for channel, index, point in updates:
                self.curves[channel][index] = point
                self._lut_dirty[channel] = True
            self.update_lut()
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._move_curve_anchors(event.position())

    def point_drag_active(self) -> bool:
        """Whether a pressed curve anchor is awaiting its mouse release."""
        return self._drag_position is not None

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._move_curve_anchors(event.position())
        self._drag_position = None
        self._drag_points.clear()
        if self._press_interaction:
            self._press_interaction = False
            self._update_timer.stop()
            self._emit_changed_signal()
            self.interaction_ended.emit()
        self.update()

    def mouseDoubleClickEvent(self, event):
        # AUD-38213: 仅左键双击重置曲线 —— 此前未检查按钮，右键/中键
        # 双击会意外重置当前通道曲线（右键快速删除两个控制点时极易触发）。
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        self.interaction_started.emit()
        self._drag_position = None
        self._drag_points.clear()
        reset_targets = self.channels if self.edit_linked else [self.active_channel]
        for ch in reset_targets:
            self.curves[ch] = [QPointF(0, 255), QPointF(255, 0)]
            self._lut_dirty[ch] = True
        self.selected_point_idx = -1
        self.update_lut()
        self.update()
        self.flush()
        self.interaction_ended.emit()

    def _reset_from_toolbar(self) -> None:
        if not self.calibrated:
            self.reset()
            return
        self.interaction_started.emit()
        self.reset()
        self.flush()
        self.interaction_ended.emit()

    def update_lut(self, emit_changed: bool = True) -> None:
        """
        立即计算 LUT 并暂存结果，通过节流定时器发射信号。
        """
        self._compute_lut()
        if not emit_changed:
            self._pending_lut = None
            self._pending_data = None
            self._update_timer.stop()

    def _compute_lut(self) -> None:
        """
        从缓存中计算最终 LUT（向量化版本）。
        
        使用线性外推处理超出范围的值，避免裁切色彩信息。
        当 Master 曲线调整黑点或白点时，原本超出范围的输入值
        会通过斜率外推正确映射，而不是被裁切。
        """
        # 仅对脏通道执行计算，结果存入缓存
        for ch in self.channels:
            if self._lut_dirty.get(ch, True):
                if not self.calibrated:
                    curve_y = self.calculate_curve(self.curves[ch])
                    self._lut_cache[ch] = (255.0 - curve_y) / 255.0
                self._lut_dirty[ch] = False
                self._final_lut_dirty = True

        if self._final_lut_dirty:
            # 合成走 layer.compose_curves_lut 权威实现（先 master 后通道 +
            # Edit 强度缩放），保证与工程重开/导出逐位一致。widget 的
            # 通道缓存不做 0-1 裁剪（GPU 采样 edge clamp，语义不变）。
            from src.features.color.core.layer import build_curves_lut_from_data, compose_curves_lut

            amounts = (self.edit_amounts
                       if any(v != 100.0 for v in self.edit_amounts.values())
                       else None)
            if self.calibrated:
                final_lut = build_curves_lut_from_data(
                    self.get_curves_data(), amounts, calibration_version=self.calibration_version)
            else:
                final_lut = np.clip(compose_curves_lut(
                    self._lut_cache.get('M'),
                    {ch: self._lut_cache.get(ch) for ch in ('R', 'G', 'B')},
                    amounts,
                ), 0.0, 1.0)

            self._final_lut_cache = final_lut
            self._final_lut_dirty = False

        if self._final_lut_cache is not None:
            self._pending_lut = self._final_lut_cache.copy()
            self._pending_data = self.get_curves_data()

            if not self._update_timer.isActive():
                self._update_timer.start()

    def _emit_changed_signal(self) -> None:
        """节流定时器回调：发射真正的 changed 信号"""
        if self._pending_lut is not None:
            lut, points = self._pending_lut, self._pending_data
            self._pending_lut = None
            self._pending_data = None
            self.changed.emit(lut, points)

    def reset(self, emit_changed: bool = True):
        for ch in self.channels:
            self.curves[ch] = [QPointF(0, 255), QPointF(255, 0)]
        self.selected_point_idx = -1
        self._drag_position = None
        self._drag_points.clear()
        # 所有通道数据都变了，标记所有通道为脏
        for ch in self.channels:
            self._lut_dirty[ch] = True
        self._final_lut_dirty = True
        self._final_lut_cache = None
        self.update_lut(emit_changed=emit_changed)
        self.update()

    def get_curves_data(self):
        """Returns the current control points as a dictionary of lists of tuples."""
        data = {}
        for ch, points in self.curves.items():
            data[ch] = [(p.x(), p.y()) for p in points]
        return data

    def set_curves_data(self, data, emit_changed: bool = False):
        """Restores control points from data dictionary.
        
        Args:
            data: Dictionary of control points per channel
            emit_changed: If True, emit changed signal after update (default False)
                          When False, only updates UI cache without triggering timer
        """
        if not data:
            self.reset(emit_changed=emit_changed)
            return

        for ch in self.channels:
            if ch in data:
                self.curves[ch] = [QPointF(x, y) for x, y in data[ch]]
            else:
                self.curves[ch] = [QPointF(0, 255), QPointF(255, 0)]

        self.selected_point_idx = -1
        self._drag_position = None
        self._drag_points.clear()
        # 所有通道数据都变了，标记所有通道为脏
        for ch in self.channels:
            self._lut_dirty[ch] = True
        self._final_lut_dirty = True
        self._final_lut_cache = None
        
        # Rebuild LUT cache but don't start timer unless emit_changed
        self.update_lut(emit_changed=emit_changed)
        self.update()
        
        if emit_changed:
            self._pending_lut = self._final_lut_cache.copy() if self._final_lut_cache is not None else None
            self._pending_data = self.get_curves_data()
            if self._pending_lut is not None and not self._update_timer.isActive():
                self._update_timer.start()

    def set_lut(self, lut, emit_changed: bool = False):
        """Restore curve control points from LUT array.

        Args:
            lut: LUT array of shape (256, 1, 3), (256, 3), or (256, 1)
            emit_changed: If True, emit changed signal after update (default False)

        Note: This method generates approximate control points by sampling
        key positions in the LUT. It's used for legacy project restore when
        only LUT is available but no control points data.
        """
        if lut is None:
            return

        lut = np.asarray(lut, dtype=np.float32)

        sample_positions = [0, 32, 64, 96, 128, 160, 192, 224, 255]

        was_blocked = self.blockSignals(True)

        try:
            # AUD-38214: 统一归一为 (256, 3) 二维视图 —— 文档一直声明支持
            # (256, 3) 形状，但旧代码只匹配 ndim==3 的 (256,1,3) 与
            # ndim==2 的 (256,1)：(256, 3) 静默落入无分支，旧工程恢复时
            # 曲线全部丢失。此处显式接受两种三维/二维形状。
            if lut.ndim == 3 and lut.shape[1] == 1 and lut.shape[2] == 3:
                lut_2d = lut[:, 0, :]
            elif lut.ndim == 2 and lut.shape[1] == 3:
                lut_2d = lut
            else:
                lut_2d = None

            if lut.ndim == 2 and lut.shape[1] == 1:
                lut_1d = lut[:, 0]
                points = []
                for x in sample_positions:
                    if x < len(lut_1d):
                        y_ui = 255.0 - lut_1d[x] * 255.0
                        y_ui = max(0.0, min(255.0, y_ui))
                        points.append(QPointF(float(x), y_ui))
                if len(points) >= 2:
                    self.curves['M'] = points
                    for ch in ['R', 'G', 'B']:
                        self.curves[ch] = [QPointF(0.0, 255.0), QPointF(255.0, 0.0)]

            elif lut_2d is not None:
                lut_master = lut_2d.mean(axis=1)

                master_points = []
                for x in sample_positions:
                    if x < len(lut_master):
                        y_ui = 255.0 - lut_master[x] * 255.0
                        y_ui = max(0.0, min(255.0, y_ui))
                        master_points.append(QPointF(float(x), y_ui))
                if len(master_points) >= 2:
                    self.curves['M'] = master_points

                for ch_idx, ch in enumerate(['R', 'G', 'B']):
                    channel_lut = lut_2d[:, ch_idx]
                    points = []
                    for x in sample_positions:
                        if x < len(channel_lut):
                            y_ui = 255.0 - channel_lut[x] * 255.0
                            y_ui = max(0.0, min(255.0, y_ui))
                            points.append(QPointF(float(x), y_ui))
                    if len(points) >= 2:
                        self.curves[ch] = points

            for ch in self.channels:
                self._lut_dirty[ch] = True
            self._final_lut_dirty = True
            self._final_lut_cache = None
            self.update_lut(emit_changed=emit_changed)
            self.update()
        finally:
            self.blockSignals(was_blocked)

        # AUD-38212: changed 信号必须在 blockSignals 恢复之后发射。
        # 旧实现在 blocked 区内调用 _emit_changed_signal()：
        # 1) Qt 级 blockSignals 直接吞掉 self.changed.emit；
        # 2) _pending_lut 已被置 None，16ms 节流定时器重放时也无事可做。
        # 结果 emit_changed=True 时信号永不送达接收方。
        if emit_changed and self._pending_lut is not None:
            self._update_timer.stop()
            self._emit_changed_signal()

    def flush(self):
        """立即发射待处理的 LUT 变更信号（跳过节流延迟）。
        用于 apply grade 等需要同步应用曲线的场景。
        """
        self._compute_lut()
        if self._pending_lut is not None:
            self._update_timer.stop()
            self._emit_changed_signal()

    # ============================================================
    # Edit 强度 / 链接编辑（曲线面板侧栏状态）
    # ============================================================

    def set_calibrated(self, enabled: bool, *, version: int = 1) -> None:
        version = version if enabled else 0
        if self.calibration_version != version:
            self.calibrated = enabled
            self.calibration_version = version
            self._lut_dirty = dict.fromkeys(self.channels, True)
            self._curve_cache.clear()
            self.update_lut(emit_changed=False)
            self.update()

    def set_edit_amounts(self, amounts: Dict[str, Any],
                         emit_changed: bool = False) -> None:
        """设置 YRGB 侧栏强度（0-100，100 = 全额生效）并重算最终 LUT。

        强度只影响合成阶段（compose_curves_lut），不改变控制点——通道
        缓存保持有效，仅最终 LUT 置脏。默认不发射 ``changed``（侧栏
        编辑经面板自身的参数信号走渲染，不与画布控制点信号混用）。
        """
        if not isinstance(amounts, dict):
            return
        changed = False
        for ch in ('Y', 'R', 'G', 'B'):
            value = amounts.get(ch, 100.0)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                value = 100.0
            value = float(value)
            if value != self.edit_amounts.get(ch):
                self.edit_amounts[ch] = value
                changed = True
        if changed:
            self._final_lut_dirty = True
            self._final_lut_cache = None
            self.update_lut(emit_changed=emit_changed)
            self.update()

    def get_edit_amounts(self) -> Dict[str, float]:
        """返回当前 YRGB 强度的副本。"""
        return dict(self.edit_amounts)

    # ============================================================
    # Color Picker (Eyedropper) Methods
    # ============================================================

    def _on_picker_toggled(self, checked: bool) -> None:
        """Handle color picker button toggle."""
        self._picker_active = checked
        self.picker_toggled.emit(checked)

    def deactivate_picker(self) -> None:
        """Deactivate the color picker (called when color is picked or externally)."""
        self._picker_active = False
        self.picker_btn.setChecked(False)

    def is_picker_active(self) -> bool:
        """Check if color picker is currently active."""
        return self._picker_active

    def add_point_from_color(self, r: float, g: float, b: float) -> bool:
        """
        Add a control point on the active curve based on RGB color picked from image.

        For RGB Curves:
        - Master (M): X coordinate based on luma (BT.601 formula)
        - R: X coordinate based on R channel value
        - G: X coordinate based on G channel value
        - B: X coordinate based on B channel value

        Args:
            r, g, b: RGB values in 0-255 range

        Returns:
            True if point was added successfully, False otherwise
        """

        ch = self.active_channel

        # Calculate X coordinate based on active channel
        if ch == 'M' and self.calibrated:
            x_coord = 0.2126 * r + 0.7152 * g + 0.0722 * b
        elif ch == 'M':
            # Master: use luminance (BT.601 formula)
            # Y = 0.299*R + 0.587*G + 0.114*B
            x_coord = 0.299 * r + 0.587 * g + 0.114 * b
        elif ch == 'R':
            # R channel: X = R value
            x_coord = r
        elif ch == 'G':
            # G channel: X = G value
            x_coord = g
        elif ch == 'B':
            # B channel: X = B value
            x_coord = b
        else:
            return False

        # Clamp X to valid range
        x_coord = max(0.0, min(255.0, x_coord))

        # Get current curve points
        pts = self.curves[ch]

        # Calculate current curve values using PCHIP
        curve_values = self.calculate_curve(pts)

        y_value = float(np.interp(x_coord, np.arange(256), curve_values))

        # Create new point
        new_point = QPointF(x_coord, y_value)

        # Check if a point already exists very close to this position
        min_distance = 3.0  # Minimum distance threshold in X
        for p in pts:
            if abs(p.x() - x_coord) < min_distance:
                # Point already exists nearby, just select it instead
                pts.sort(key=lambda p: p.x())
                self.selected_point_idx = pts.index(p)
                self.update()
                return True

        self.interaction_started.emit()
        pts.append(new_point)
        pts.sort(key=lambda p: p.x())

        # Update selected point index to the newly added point
        self.selected_point_idx = pts.index(new_point)

        # Mark LUT as dirty and update
        self._lut_dirty[ch] = True
        self.update_lut()
        self.update()

        self.flush()
        self.interaction_ended.emit()

        return True
