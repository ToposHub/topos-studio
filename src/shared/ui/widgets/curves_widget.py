import logging

import numpy as np
from typing import Dict, Optional, Any
from PyQt6.QtWidgets import QHBoxLayout, QPushButton, QButtonGroup, QWidget
from PyQt6.QtCore import Qt, QEvent, QPointF, pyqtSignal, QRectF, QTimer
from PyQt6.QtGui import QPainter, QColor, QPen, QPainterPath
from src.shared.ui.widgets.gpu_scope_widget import GpuScopeWidget

try:
    from scipy.interpolate import PchipInterpolator
    HAS_SCIPY = True
except ModuleNotFoundError:
    HAS_SCIPY = False

from src.shared.i18n.i18n import I18N
from src.shared.assets.icon_manager import icon_color_picker, icon_reset
from src.shared.ui.style_sheet import (
    C,
    CURVE_BTN_CONTAINER_STYLE,
    CURVE_PICKER_BTN_STYLE,
    CURVE_RESET_BTN_STYLE,
    curve_channel_btn_style,
)

logger = logging.getLogger(__name__)

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
    # Emits the LUT array (256, 1, 3) BGR and the control points data
    changed = pyqtSignal(np.ndarray, dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    histogram_geometry_changed = pyqtSignal()
    # Color Picker signals
    picker_toggled = pyqtSignal(bool)  # True = activate picker, False = deactivate
    color_received = pyqtSignal(float, float, float)  # r, g, b (0-255)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet("background-color: #232326;")
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
        # AUD-38211: 左键 press 是否已发射 interaction_started（release 时
        # 据此配对 interaction_ended，避免点击空白处的孤儿 started 信号）
        self._press_interaction = False
        
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
        reset_btn.clicked.connect(lambda _checked=False: self.reset(emit_changed=True))
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

    def set_scope_frame(self, tex_id: Optional[int], width: int, height: int) -> None:
        """Present histogram inside the same bounds used by curve coordinates."""
        self._scope.set_scope_frame(tex_id, width, height)
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

        # Draw Inactive Curves first (Reference)
        for ch in self.channels:
            if ch == self.active_channel:
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
            painter.setBrush(QColor(35, 35, 38))
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

        # Draw diagonal reference line (from bottom-left to top-right)
        diagonal_pen = QPen(QColor(52, 52, 56), 0.3)
        painter.setPen(diagonal_pen)
        painter.drawLine(int(rect.left()), int(rect.bottom()), int(rect.right()), int(rect.top()))

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
        
        pts_sorted = sorted(points, key=lambda p: p.x())
        min_x = int(pts_sorted[0].x())
        max_x = int(pts_sorted[-1].x())
        
        # Calculate curve values for the full range (AUD-38219: 缓存复用)
        vals = self._cached_curve_values(ch, points)
        
        # Use 300+ sampling points for ultra-smooth curves
        num_samples = 300
        path = QPainterPath()
        
        # Start from the first control point's X position
        start_idx = min_x
        wx, wy = self.to_widget(start_idx, vals[start_idx])
        path.moveTo(wx, wy)
        
        # Calculate step for sub-pixel sampling
        x_range = max_x - min_x
        if x_range > 0:
            step = x_range / num_samples
            # Sample at sub-pixel positions for smoother curves
            for i in range(1, num_samples + 1):
                # Interpolate at sub-pixel positions
                sample_x = min_x + i * step
                if sample_x > max_x:
                    sample_x = max_x
                
                # Get interpolated Y value (use linear interpolation between integer samples)
                x_floor = int(sample_x)
                x_ceil = min(x_floor + 1, 255)
                t = sample_x - x_floor
                
                if x_floor >= 0 and x_ceil < 256:
                    y_interp = vals[x_floor] * (1 - t) + vals[x_ceil] * t
                elif x_floor < 0:
                    y_interp = vals[0]
                else:
                    y_interp = vals[255]
                
                wx, wy = self.to_widget(sample_x, y_interp)
                path.lineTo(wx, wy)
        
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
                painter.setBrush(QColor(35, 35, 38))  # Match background #232326
                painter.setPen(QPen(QColor(160, 160, 160), 1.5))
                painter.drawEllipse(QPointF(wx, wy), 4, 4)

    def calculate_curve(self, points):
        pts = sorted(points, key=lambda p: p.x())
        xs = np.array([p.x() for p in pts])
        ys = np.array([p.y() for p in pts])
        
        try:
            if len(xs) < 2:
                # Default case should not happen with 2+ points
                return np.full(256, ys[0] if len(ys) > 0 else 127)
            
            xi = np.arange(256)
            
            if len(xs) == 2:
                slope = (ys[1] - ys[0]) / (xs[1] - xs[0]) if xs[1] != xs[0] else 0
                return ys[0] + slope * (xi - xs[0])
            
            if HAS_SCIPY:
                mask_mid = (xi >= xs[0]) & (xi <= xs[-1])
                res = np.zeros(256)
                
                pchip = PchipInterpolator(xs, ys)
                res[mask_mid] = pchip(xi[mask_mid])
                
                mask_left = xi < xs[0]
                if np.any(mask_left):
                    dx = max(0.1, (xs[1] - xs[0]) * 0.1)
                    y_start = float(pchip(xs[0]))
                    y_next = float(pchip(xs[0] + dx))
                    slope = (y_next - y_start) / dx
                    res[mask_left] = ys[0] + slope * (xi[mask_left] - xs[0])
                    
                mask_right = xi > xs[-1]
                if np.any(mask_right):
                    dx = max(0.1, (xs[-1] - xs[-2]) * 0.1)
                    y_end = float(pchip(xs[-1]))
                    y_prev = float(pchip(xs[-1] - dx))
                    slope = (y_end - y_prev) / dx
                    res[mask_right] = ys[-1] + slope * (xi[mask_right] - xs[-1])
                    
                return res
            else:
                return np.interp(xi, xs, ys)
        except (ValueError, IndexError):
            # AUD-38215: 裸 except 违反 .clinerules 且会掩盖编程错误
            # （如 AttributeError）。只捕获插值域错误并降级到线性插值：
            # - ValueError: PCHIP 构造拒绝退化/非有限控制点
            # - IndexError: 控制点不足时的越界访问
            logger.warning(
                "AUD-38215: 曲线插值域错误，降级为线性插值（点数=%d）",
                len(points),
                exc_info=True,
            )
            return np.interp(np.arange(256), xs, ys)

    def mousePressEvent(self, event):
        pos = event.position()
        mx, my = self.from_widget(pos.x(), pos.y())
        
        pts = self.curves[self.active_channel]
        clicked_idx = -1
        
        # Precise distance calculation (Euclidean distance in screen pixels)
        min_dist = 12.0 # Threshold radius: 12 pixels
        for i, p in enumerate(pts):
            wx, wy = self.to_widget(p.x(), p.y())
            dist = np.sqrt((wx - pos.x())**2 + (wy - pos.y())**2)
            if dist < min_dist:
                min_dist = dist
                clicked_idx = i
        
        if event.button() == Qt.MouseButton.LeftButton:
            self.interaction_started.emit()
            # AUD-38211: 记录本次 press 发射过 started —— 无论随后是否
            # 命中/新增控制点，release 都必须配对 interaction_ended。
            self._press_interaction = True
            if clicked_idx != -1:
                self.selected_point_idx = clicked_idx
            else:
                # Only add new point if clicking inside or very close to the draw area
                rect = self.get_draw_rect()
                # Relaxed boundary check for adding points
                if rect.adjusted(-5, -5, 5, 5).contains(pos):
                    # Clamp to 0-255 range for internal representation
                    mx_clamped = max(0, min(255, mx))
                    my_clamped = max(0, min(255, my))
                    new_point = QPointF(mx_clamped, my_clamped)
                    pts.append(new_point)
                    pts.sort(key=lambda p: p.x())
                    self.selected_point_idx = pts.index(new_point)
                    # 链接编辑：新点同步到全部通道（同一 x/y）
                    if self.edit_linked:
                        for ch in self.channels:
                            if ch == self.active_channel:
                                continue
                            other = self.curves[ch]
                            other.append(QPointF(mx_clamped, my_clamped))
                            other.sort(key=lambda p: p.x())
                            self._lut_dirty[ch] = True
                    self.update_lut()
        elif event.button() == Qt.MouseButton.RightButton:
            if clicked_idx != -1 and len(pts) > 2:
                self.interaction_started.emit()
                removed_x = pts[clicked_idx].x()
                pts.pop(clicked_idx)
                self.selected_point_idx = -1
                # 标记当前通道为脏
                self._lut_dirty[self.active_channel] = True
                # 链接编辑：同步删除其余通道同 x 的控制点
                if self.edit_linked:
                    for ch in self.channels:
                        if ch == self.active_channel:
                            continue
                        for p in self.curves[ch]:
                            if abs(p.x() - removed_x) <= 0.5:
                                self.curves[ch].remove(p)
                                self._lut_dirty[ch] = True
                                break
                self.update_lut()
                self.interaction_ended.emit()
        
        self.update()

    def mouseMoveEvent(self, event):
        if self.selected_point_idx != -1:
            pos = event.position()
            mx, my = self.from_widget(pos.x(), pos.y())

            mx = max(0, min(255, mx))
            my = max(0, min(255, my))

            pts = self.curves[self.active_channel]

            # Special Constraints for Black (0) and White (-1) points
            # They MUST stay on the boundary box edges.
            if self.selected_point_idx == 0:
                # Black point: stays on left edge (X=0) or bottom edge (Y=255)
                # Determine which edge is closer to current mouse position
                if mx < (255 - my): # Closer to left edge
                    mx = 0
                else:               # Closer to bottom edge
                    my = 255
            elif self.selected_point_idx == len(pts) - 1:
                # White point: stays on right edge (X=255) or top edge (Y=0)
                if (255 - mx) < my: # Closer to right edge
                    mx = 255
                else:               # Closer to top edge
                    my = 0

            # Constraints: avoid swapping order
            if self.selected_point_idx > 0:
                mx = max(mx, pts[self.selected_point_idx-1].x() + 0.1)
            if self.selected_point_idx < len(pts) - 1:
                mx = min(mx, pts[self.selected_point_idx+1].x() - 0.1)

            prev_x = pts[self.selected_point_idx].x()
            prev_y = pts[self.selected_point_idx].y()
            pts[self.selected_point_idx] = QPointF(mx, my)
            # 链接编辑：把本帧的纵向增量镜像到其余通道同 x 的控制点
            # （只镜像 dy——各通道端点约束不同，横向位置保持独立）
            if self.edit_linked and self.selected_point_idx >= 0:
                dy = my - prev_y
                if dy != 0.0:
                    for ch in self.channels:
                        if ch == self.active_channel:
                            continue
                        for p in self.curves[ch]:
                            if abs(p.x() - prev_x) <= 0.5:
                                p.setY(max(0.0, min(255.0, p.y() + dy)))
                                break
            self._lut_dirty[self.active_channel] = True
            self._final_lut_dirty = True
            self._compute_lut()
            self.update()

    def mouseReleaseEvent(self, event):
        # AUD-38211: 左键 press 发出的 interaction_started 必须在 release
        # 配对 interaction_ended —— 此前仅在命中/新增控制点
        # （selected_point_idx != -1）时发射，左键点击空白处（未加点）时
        # started 成为孤儿信号，undo/空闲渲染状态被破坏。
        if event.button() == Qt.MouseButton.LeftButton and self._press_interaction:
            self._press_interaction = False
            self.interaction_ended.emit()
        self.selected_point_idx = -1
        self.update()

    def mouseDoubleClickEvent(self, event):
        # AUD-38213: 仅左键双击重置曲线 —— 此前未检查按钮，右键/中键
        # 双击会意外重置当前通道曲线（右键快速删除两个控制点时极易触发）。
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        self.interaction_started.emit()
        reset_targets = self.channels if self.edit_linked else [self.active_channel]
        for ch in reset_targets:
            self.curves[ch] = [QPointF(0, 255), QPointF(255, 0)]
            self._lut_dirty[ch] = True
        self.selected_point_idx = -1
        self.update_lut()
        self.update()
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
                curve_y = self.calculate_curve(self.curves[ch])
                self._lut_cache[ch] = (255.0 - curve_y) / 255.0
                self._lut_dirty[ch] = False
                self._final_lut_dirty = True

        if self._final_lut_dirty:
            # 合成走 layer.compose_curves_lut 权威实现（先 master 后通道 +
            # Edit 强度缩放），保证与工程重开/导出逐位一致。widget 的
            # 通道缓存不做 0-1 裁剪（GPU 采样 edge clamp，语义不变）。
            from src.features.color.core.layer import compose_curves_lut

            amounts = (self.edit_amounts
                       if any(v != 100.0 for v in self.edit_amounts.values())
                       else None)
            final_lut = compose_curves_lut(
                self._lut_cache.get('M'),
                {ch: self._lut_cache.get(ch) for ch in ('R', 'G', 'B')},
                amounts,
            )

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
            self.changed.emit(self._pending_lut, self._pending_data)
            self._pending_lut = None
            self._pending_data = None

    def reset(self, emit_changed: bool = True):
        for ch in self.channels:
            self.curves[ch] = [QPointF(0, 255), QPointF(255, 0)]
        self.selected_point_idx = -1
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
        if ch == 'M':
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

        # Get Y value at the calculated X position (seamless injection)
        x_int = int(round(x_coord))
        x_int = max(0, min(255, x_int))
        y_value = curve_values[x_int]

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

        # Insert new point
        pts.append(new_point)
        pts.sort(key=lambda p: p.x())

        # Update selected point index to the newly added point
        self.selected_point_idx = pts.index(new_point)

        # Mark LUT as dirty and update
        self._lut_dirty[ch] = True
        self.update_lut()
        self.update()

        # Emit interaction signals for proper render pipeline
        self.interaction_started.emit()
        self.interaction_ended.emit()

        return True
