"""
HSL Curves Widget for Topos Color.
Provides 6 professional HSL curve editing modes:

1. Lum vs Sat (Luminance → Saturation mapping)
2. Hue vs Hue (Hue shift based on hue)
3. Hue vs Sat (Hue → Saturation mapping)
4. Hue vs Lum (Hue → Luminance mapping)
5. Sat vs Sat (Saturation → Saturation mapping)
6. Sat vs Lum (Saturation → Luminance mapping)

Visual design inspired by DaVinci Resolve professional color grading UI.
"""
import numpy as np
from typing import Dict, Optional, Any, List
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QPushButton, QComboBox, QLabel
from PyQt6.QtCore import Qt, QEvent, QPointF, pyqtSignal, QRectF, QTimer
from PyQt6.QtGui import QPainter, QColor, QPen, QPainterPath, QLinearGradient

try:
    from scipy.interpolate import PchipInterpolator
    HAS_SCIPY = True
except ModuleNotFoundError:
    HAS_SCIPY = False

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import (
    apply_combo_dropdown_style,
    CURVE_BTN_CONTAINER_STYLE,
    HSL_MODE_COMBO_STYLE,
    CURVE_PICKER_BTN_STYLE,
    CURVE_RESET_BTN_STYLE,
    HSL_INFO_LABEL_STYLE,
)
from src.shared.assets.icon_manager import icon_color_picker, icon_reset
from src.shared.ui.widgets.gpu_scope_widget import GpuScopeWidget


class _HslCurveOverlay(QWidget):
    def __init__(self, canvas: 'HSLCurvesWidget') -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setStyleSheet('background: transparent;')

    def paintEvent(self, event) -> None:
        self.canvas._paint_editor(self)


# Curve mode definitions with metadata
# NOTE: 'name' key stores the i18n translation key (without 'hsl_curves.' prefix)
# The actual translated name is retrieved via I18N.tr() in init_ui()
CURVE_MODES = {
    'LumSat': {
        'name': 'lum_vs_sat',  # i18n key: hsl_curves.lum_vs_sat
        'x_axis': 'lum',       # X-axis: Luminance
        'y_axis': 'sat',       # Y-axis: Saturation multiplier
        'y_type': 'mult',      # Y output type: multiplier (0.0-2.0)
    },
    'HueHue': {
        'name': 'hue_vs_hue',  # i18n key: hsl_curves.hue_vs_hue
        'x_axis': 'hue',       # X-axis: Hue (0-360°)
        'y_axis': 'hue',       # Y-axis: Hue shift
        'y_type': 'shift',     # Y output type: shift (-0.5 to +0.5)
    },
    'HueSat': {
        'name': 'hue_vs_sat',  # i18n key: hsl_curves.hue_vs_sat
        'x_axis': 'hue',
        'y_axis': 'sat',
        'y_type': 'mult',
    },
    'HueLum': {
        'name': 'hue_vs_lum',  # i18n key: hsl_curves.hue_vs_lum
        'x_axis': 'hue',
        'y_axis': 'lum',
        'y_type': 'mult',
    },
    'SatSat': {
        'name': 'sat_vs_sat',  # i18n key: hsl_curves.sat_vs_sat
        'x_axis': 'sat',       # X-axis: Saturation
        'y_axis': 'sat',
        'y_type': 'mult',
    },
    'SatLum': {
        'name': 'sat_vs_lum',  # i18n key: hsl_curves.sat_vs_lum
        'x_axis': 'sat',
        'y_axis': 'lum',
        'y_type': 'mult',
    },
}

# Mode order for UI display
MODE_ORDER = ['LumSat', 'HueHue', 'HueSat', 'HueLum', 'SatSat', 'SatLum']


class HSLCurvesWidget(QWidget):
    """
    HSL Curves Widget - 6 professional curve modes for color grading.

    Each curve mode maps an input (X-axis) to an output (Y-axis):
    - Lum vs Sat: Adjust saturation based on luminance
    - Hue vs Hue: Shift hue based on hue (hue mapping)
    - Hue vs Sat: Adjust saturation based on hue
    - Hue vs Lum: Adjust luminance based on hue
    - Sat vs Sat: Adjust saturation based on saturation
    - Sat vs Lum: Adjust luminance based on saturation

    **Y-axis conventions:**
    - Multiplier types (sat/lum): Y=127.5 is 1.0 (no change), range 0.0-2.0
    - Shift type (hue): Y=127.5 is 0.0 (no shift), range -0.5 to +0.5

    **Endpoint constraints:**
    - First point (X=0) and last point (X=255) are locked horizontally
    - They can only move vertically and cannot be deleted
    """
    # Emits the LUT array (6, 256) float32 and control points data
    changed = pyqtSignal(np.ndarray, dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    # HSL Color Picker signals
    hsl_picker_toggled = pyqtSignal(bool)  # True = activate picker, False = deactivate
    hsl_color_received = pyqtSignal(float, float, float)  # r, g, b (0-255)
    mode_changed = pyqtSignal(str)
    point_state_changed = pyqtSignal()
    histogram_geometry_changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet('background-color: #232326;')
        self._scope = GpuScopeWidget(self, show_value_axis=False)
        self._scope.hide()
        self._has_histogram = False
        self._overlay = _HslCurveOverlay(self)
        self._scope_resize_timer = QTimer(self)
        self._scope_resize_timer.setSingleShot(True)
        self._scope_resize_timer.setInterval(150)
        self._scope_resize_timer.timeout.connect(self.histogram_geometry_changed)
        self.setMinimumSize(300, 220)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        # Current active curve mode
        self._current_mode = 'LumSat'

        # HSL Color Picker state
        self._hsl_picker_active = False

        # Initialize all 6 curves with default horizontal line at center
        self.curves: Dict[str, List[QPointF]] = {}
        for mode in MODE_ORDER:
            self.curves[mode] = [QPointF(0, 127.5), QPointF(255, 127.5)]

        self.selected_point_idx = -1
        self.padding = 7
        self._inspected_point_idx = 0

        # === Cache and Throttling ===
        self._lut_cache: Optional[np.ndarray] = None  # Shape: (6, 256)
        self._lut_dirty: bool = True
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(16)
        self._update_timer.timeout.connect(self._emit_changed_signal)
        self._pending_lut: Optional[np.ndarray] = None
        self._pending_data: Optional[Dict[str, Any]] = None
        # 左键 press 是否构成一次交互（release 据此对称发 ended）
        self._press_interaction: bool = False

        self.init_ui()

    def init_ui(self):
        # Button container (floating over canvas)
        self.btn_container = QWidget(self)
        self.btn_container.setStyleSheet(CURVE_BTN_CONTAINER_STYLE)
        btn_layout = QHBoxLayout(self.btn_container)
        btn_layout.setContentsMargins(5, 5, 5, 5)
        btn_layout.setSpacing(4)

        # Mode selector dropdown
        self.mode_combo = QComboBox()
        self.mode_combo.setFixedHeight(24)
        for mode in MODE_ORDER:
            # Get translated name from i18n system
            i18n_key = f"hsl_curves.{CURVE_MODES[mode]['name']}"
            translated_name = I18N.tr(i18n_key, CURVE_MODES[mode]['name'])
            self.mode_combo.addItem(translated_name, mode)
        self.mode_combo.setStyleSheet(HSL_MODE_COMBO_STYLE)
        apply_combo_dropdown_style(self.mode_combo)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        btn_layout.addWidget(self.mode_combo)

        # HSL Color Picker Button (Eyedropper)
        self.picker_btn = QPushButton()
        self.picker_btn.setIcon(icon_color_picker())
        self.picker_btn.setFixedSize(28, 24)
        self.picker_btn.setCheckable(True)
        self.picker_btn.setToolTip(I18N.tr("tooltip.hsl_picker", "Pick color from image to add control point"))
        self.picker_btn.setStyleSheet(CURVE_PICKER_BTN_STYLE)
        self.picker_btn.toggled.connect(self._on_picker_toggled)
        btn_layout.addWidget(self.picker_btn)

        # Reset Button
        reset_btn = QPushButton()
        self.reset_btn = reset_btn
        reset_btn.setIcon(icon_reset())
        reset_btn.setFixedSize(28, 24)
        reset_btn.setToolTip(I18N.tr("tooltip.reset_hsl_curves", "Reset Current Curve"))
        reset_btn.setStyleSheet(CURVE_RESET_BTN_STYLE)
        reset_btn.clicked.connect(lambda _checked=False: self.reset_current_curve(emit_changed=True))
        btn_layout.addWidget(reset_btn)

        # Info label showing current multiplier value
        self.info_label = QLabel("1.00x")
        self.info_label.setStyleSheet(HSL_INFO_LABEL_STYLE)
        btn_layout.addWidget(self.info_label)

        self.btn_container.adjustSize()

    @property
    def current_mode(self) -> str:
        """Get the current active curve mode."""
        return self._current_mode

    @current_mode.setter
    def current_mode(self, mode: str) -> None:
        """Set the current active curve mode."""
        if mode in CURVE_MODES and mode != self._current_mode:
            self._current_mode = mode
            self.selected_point_idx = -1
            self._inspected_point_idx = 0
            self.clear_scope()
            # Update combo box without triggering signal
            idx = MODE_ORDER.index(mode)
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(idx)
            self.mode_combo.blockSignals(False)
            self._update_info_label()
            self.mode_changed.emit(mode)
            self.update()

    def _on_mode_changed(self, index: int) -> None:
        """Handle mode combo box selection change."""
        mode = MODE_ORDER[index]
        if mode != self._current_mode:
            self.current_mode = mode

    def _on_picker_toggled(self, checked: bool) -> None:
        """Handle HSL color picker button toggle."""
        self._hsl_picker_active = checked
        self.hsl_picker_toggled.emit(checked)

    def deactivate_picker(self) -> None:
        """Deactivate the HSL picker (called when color is picked or externally)."""
        self._hsl_picker_active = False
        self.picker_btn.setChecked(False)

    def is_picker_active(self) -> bool:
        """Check if HSL picker is currently active."""
        return self._hsl_picker_active

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scope.setGeometry(self.get_draw_rect().toAlignedRect())
        self._overlay.setGeometry(self.rect())
        self._overlay.raise_()
        # Position buttons at top-left, floating over the canvas
        self.btn_container.move(self.padding + 5, self.padding + 5)
        self.btn_container.raise_()
        self._scope_resize_timer.start()

    def histogram_display_metrics(self) -> tuple[tuple[int, int], float]:
        rect = self.get_draw_rect().toAlignedRect()
        dpr = self.devicePixelRatioF()
        return (max(1, round(rect.width() * dpr)), max(1, round(rect.height() * dpr))), dpr

    def event(self, event: QEvent) -> bool:
        if (event.type() == QEvent.Type.DevicePixelRatioChange
                and hasattr(self, '_scope_resize_timer')):
            self._scope_resize_timer.start()
        return super().event(event)

    def set_scope_frame(self, tex_id: Optional[int], width: int, height: int) -> None:
        self._scope.set_scope_frame(tex_id, width, height)
        self._has_histogram = tex_id is not None
        self._scope.setVisible(self._has_histogram)
        self._overlay.raise_()
        self.btn_container.raise_()
        self.update()

    def clear_scope(self) -> None:
        self._has_histogram = False
        self._scope.clear()
        self._scope.hide()
        self.update()

    def get_draw_rect(self):
        """
        Calculate the safe drawing area with padding.
        All visual elements must be drawn within this rectangle.
        """
        w, h = self.width(), self.height()
        return QRectF(self.padding, self.padding, w - 2 * self.padding, h - 2 * self.padding)

    def to_widget(self, x_img, y_img):
        """Convert image coordinates (0-255) to widget pixel coordinates within draw_rect."""
        rect = self.get_draw_rect()
        wx = rect.left() + (x_img / 255.0) * rect.width()
        wy = rect.top() + (y_img / 255.0) * rect.height()
        return wx, wy

    def from_widget(self, wx, wy):
        """Convert widget pixel coordinates to image coordinates (0-255) within draw_rect."""
        rect = self.get_draw_rect()
        mx = (wx - rect.left()) / rect.width() * 255
        my = (wy - rect.top()) / rect.height() * 255
        return mx, my

    def _paint_editor(self, target: QWidget) -> None:
        painter = QPainter(target)
        # Global antialiasing for smooth curves and points
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self.get_draw_rect()

        # Layer 1: Dynamic gradient background based on current mode
        if not self._has_histogram:
            self._draw_gradient_background(painter, rect)

        # Layer 2: Dark overlay for professional tone
            self._draw_dark_overlay(painter, rect)

        # Layer 3: Grid system with center baseline
        self._draw_grid(painter, rect)

        # Layer 4: Smooth curve (current mode)
        self._draw_curve(painter, self._current_mode, rect)

        # Layer 5: Control points with interaction feedback
        self._draw_control_points(painter, rect)

        # Layer 6: Border frame
        self._draw_border(painter, rect)
        painter.end()

    def _draw_gradient_background(self, painter: QPainter, rect: QRectF):
        """
        Draw dynamic gradient background based on current curve mode.

        - Lum vs ...: Black to white (luminance gradient)
        - Hue vs ...: Rainbow spectrum (hue gradient 0-360°)
        - Sat vs ...: Gray to saturated color (saturation gradient)
        """
        mode_info = CURVE_MODES[self._current_mode]
        x_axis = mode_info['x_axis']

        gradient = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.top())

        if x_axis == 'lum':
            # Luminance gradient: black to white
            gradient.setColorAt(0.0, QColor(0, 0, 0))
            gradient.setColorAt(1.0, QColor(205, 205, 205))
            painter.fillRect(rect, gradient)

        elif x_axis == 'hue':
            # Rainbow gradient: full hue spectrum (0-360°)
            # HSV hue stops: Red(0) -> Yellow(60) -> Green(120) -> Cyan(180) -> Blue(240) -> Magenta(300) -> Red(360)
            hue_stops = [
                (0.0, QColor(255, 0, 0)),      # Red
                (0.167, QColor(255, 255, 0)),  # Yellow
                (0.333, QColor(0, 255, 0)),    # Green
                (0.5, QColor(0, 255, 255)),    # Cyan
                (0.667, QColor(0, 0, 255)),    # Blue
                (0.833, QColor(255, 0, 255)),  # Magenta
                (1.0, QColor(255, 0, 0)),      # Red (wrap)
            ]
            for pos, color in hue_stops:
                gradient.setColorAt(pos, QColor.fromHsvF(color.hsvHueF(), 1.0, 0.34))
            painter.fillRect(rect, gradient)

        elif x_axis == 'sat':
            # Saturation gradient: gray to saturated red
            gradient.setColorAt(0.0, QColor(0, 0, 0))
            gradient.setColorAt(1.0, QColor(205, 205, 205))
            painter.fillRect(rect, gradient)

    def _draw_dark_overlay(self, painter: QPainter, rect: QRectF):
        """
        Overlay semi-transparent dark layer to maintain professional dark tone.
        Prevents the bright gradient from overwhelming the UI.
        """
        if CURVE_MODES[self._current_mode]['x_axis'] == 'hue':
            shade = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            shade.setColorAt(0.0, QColor(0, 0, 0, 80))
            shade.setColorAt(0.5, QColor(0, 0, 0, 0))
            shade.setColorAt(1.0, QColor(0, 0, 0, 80))
            painter.fillRect(rect, shade)

    def _draw_grid(self, painter: QPainter, rect: QRectF):
        """
        Draw 4x4 grid system with prominent center baseline.
        - Regular grid: subtle, low contrast
        - Center baseline: more visible, acts as visual anchor for 1.0 multiplier
        """
        for vertical, divisions in ((True, max(4, round(rect.width() / 20))),
                                    (False, max(4, round(rect.height() / 20)))):
            for i in range(1, divisions):
                painter.setPen(QPen(QColor(220, 220, 225, 48 if i % 5 == 0 else 24), 0.6))
                x = rect.left() + rect.width() * i / divisions
                y = rect.top() + rect.height() * i / divisions
                if vertical:
                    painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
                else:
                    painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

        # Center baseline (Y=127.5, multiplier 1.0) - more prominent
        center_pen = QPen(QColor(255, 255, 255, 80), 1.0)
        center_pen.setStyle(Qt.PenStyle.SolidLine)
        painter.setPen(center_pen)
        center_y = rect.top() + rect.height() * 0.5
        painter.drawLine(int(rect.left()), int(center_y), int(rect.right()), int(center_y))

    def _draw_curve(self, painter: QPainter, curve_name: str, rect: QRectF):
        """
        Draw smooth spline curve with professional quality.
        - Thickness: 2.0-2.5px for visual presence
        - Color: bright white/light gray for high contrast
        - High-density sampling for ultra-smooth rendering
        """
        points = self.curves[curve_name]
        if len(points) < 2:
            return

        pts_sorted = sorted(points, key=lambda p: p.x())
        min_x = int(pts_sorted[0].x())
        max_x = int(pts_sorted[-1].x())

        # Calculate curve values for the full range
        vals = self.calculate_curve(points)

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
            for i in range(1, num_samples + 1):
                sample_x = min_x + i * step
                if sample_x > max_x:
                    sample_x = max_x

                # Interpolate Y value between integer samples
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

        # Curve pen: thick, bright white for professional look
        curve_color = QColor(240, 240, 240)
        curve_pen = QPen(curve_color, 1.0)
        curve_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        curve_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(curve_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

    def _draw_control_points(self, painter: QPainter, rect: QRectF):
        """
        Draw control points with professional interaction feedback.

        **Default state:**
        - Solid filled circle (radius 3.5px)
        - Dark fill with thin white border

        **Selected/Active state:**
        - Larger radius (5px)
        - Pure white fill
        - Semi-transparent white halo underneath for visual emphasis
        """
        points = self.curves[self._current_mode]

        for i, p in enumerate(points):
            wx, wy = self.to_widget(p.x(), p.y())
            is_selected = (i == self.selected_point_idx)
            is_endpoint = (i == 0 or i == len(points) - 1)

            if is_selected:
                # === Selected Point: Prominent visual feedback ===
                # Layer 1: Halo glow (semi-transparent white)
                halo_color = QColor(255, 255, 255, 80)
                painter.setBrush(halo_color)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(QPointF(wx, wy), 8, 8)

                # Layer 2: Outer ring (thin dark border)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(60, 60, 60), 1.5))
                painter.drawEllipse(QPointF(wx, wy), 5.5, 5.5)

                # Layer 3: Inner filled circle (pure white)
                painter.setBrush(QColor(255, 255, 255))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(QPointF(wx, wy), 5, 5)

            else:
                # === Default Point: Subtle, refined appearance ===
                # Inner filled circle (dark fill)
                fill_color = QColor(45, 45, 48)
                painter.setBrush(fill_color)

                # Thin white border
                border_color = QColor(200, 200, 200) if is_endpoint else QColor(180, 180, 180)
                painter.setPen(QPen(border_color, 1.2))
                painter.drawEllipse(QPointF(wx, wy), 3.5, 3.5)

    def _draw_border(self, painter: QPainter, rect: QRectF):
        """
        Draw subtle border frame around the drawing area.
        """
        border_pen = QPen(QColor(7, 7, 8), 0.8)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(border_pen)
        painter.drawRect(rect)

    # ============================================================
    # CURVE INTERPOLATION AND EDITING
    # ============================================================

    def calculate_curve(self, points):
        """Calculate interpolated curve values using PCHIP."""
        pts = sorted(points, key=lambda p: p.x())
        xs = np.array([p.x() for p in pts])
        ys = np.array([p.y() for p in pts])

        try:
            if len(xs) < 2:
                return np.full(256, ys[0] if len(ys) > 0 else 127.5)

            xi = np.arange(256)

            # === Non-Hue curves: standard interpolation ===
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
            # AUD-38304: 裸 except 违反 .clinerules 且会掩盖编程错误
            # （如 AttributeError）。只捕获插值域错误并降级到线性插值：
            # - ValueError: PCHIP 构造拒绝退化/非单调控制点
            # - IndexError: 控制点不足时的越界访问
            return np.interp(np.arange(256), xs, ys)

    def _is_endpoint(self, idx: int) -> bool:
        """Check if the given index is an endpoint (first or last point)."""
        pts = self.curves[self._current_mode]
        return idx == 0 or idx == len(pts) - 1

    def mousePressEvent(self, event):
        pos = event.position()
        mx, my = self.from_widget(pos.x(), pos.y())

        pts = self.curves[self._current_mode]
        clicked_idx = -1

        # Hit detection threshold: larger for better UX
        hit_radius = 14.0  # Pixels
        for i, p in enumerate(pts):
            wx, wy = self.to_widget(p.x(), p.y())
            dist = np.sqrt((wx - pos.x())**2 + (wy - pos.y())**2)
            if dist < hit_radius:
                hit_radius = dist
                clicked_idx = i

        if event.button() == Qt.MouseButton.LeftButton:
            # 【手势边界契约】只有真正命中/插入控制点才算一次交互：
            # 空白处点击不得发 interaction_started——否则 started/ended
            # 不对称会污染撤销栈，且 idle 渲染定时器（唯一重启点在
            # on_interaction_ended）永不重启。
            self._press_interaction = False
            if clicked_idx != -1:
                self.interaction_started.emit()
                self._press_interaction = True
                self.selected_point_idx = clicked_idx
                self._inspected_point_idx = clicked_idx

                # === Hue Wrap-around: Sync endpoints on click ===
                # Only sync when curve has exactly 2 endpoints (no extra control points)
                if self._should_sync_endpoints() and self._is_endpoint(clicked_idx):
                    # Sync both endpoints to the clicked point's Y value
                    new_y = pts[clicked_idx].y()
                    # AUD-38303: 只有同步真正改动数据时才置脏并重建 LUT，
                    # 否则 UI 显示的曲线与渲染侧 LUT 状态发散；同时避免
                    # 点击已同步端点时触发无意义的重绘/信号。
                    if pts[0].y() != new_y or pts[-1].y() != new_y:
                        pts[0] = QPointF(0, new_y)
                        pts[-1] = QPointF(255, new_y)
                        self._lut_dirty = True
                        self.update_lut()
            else:
                rect = self.get_draw_rect()
                if rect.adjusted(-5, -5, 5, 5).contains(pos):
                    self.interaction_started.emit()
                    self._press_interaction = True
                    mx_clamped = max(0, min(255, mx))
                    my_clamped = max(0, min(255, my))
                    new_point = QPointF(mx_clamped, my_clamped)
                    pts.append(new_point)
                    pts.sort(key=lambda p: p.x())
                    self.selected_point_idx = pts.index(new_point)
                    self._inspected_point_idx = self.selected_point_idx
                    self.update_lut()
        elif event.button() == Qt.MouseButton.RightButton:
            if clicked_idx != -1 and not self._is_endpoint(clicked_idx) and len(pts) > 2:
                self.interaction_started.emit()
                pts.pop(clicked_idx)
                self.selected_point_idx = -1
                self._lut_dirty = True
                self.update_lut()
                self.interaction_ended.emit()

        self._update_info_label()
        self.update()

    def selected_values(self) -> tuple[float, float]:
        points = self.curves[self._current_mode]
        if not points:
            return 0.0, 0.0 if CURVE_MODES[self._current_mode]['y_type'] == 'shift' else 1.0
        index = max(0, min(self._inspected_point_idx, len(points) - 1))
        point = points[index]
        mode = CURVE_MODES[self._current_mode]
        x = point.x() / 255.0 * (360.0 if mode['x_axis'] == 'hue' else 1.0)
        y = self.y_to_hue_shift(point.y()) * 360.0 if mode['y_type'] == 'shift' else self.y_to_multiplier(point.y())
        return x, y

    def set_selected_value(self, axis: str, value: float) -> None:
        """Edit the inspected point through the existing LUT/undo signal path."""
        points = self.curves[self._current_mode]
        if not points:
            return
        index = max(0, min(self._inspected_point_idx, len(points) - 1))
        point = points[index]
        mode = CURVE_MODES[self._current_mode]
        x, y = point.x(), point.y()
        if axis == 'x':
            if index == 0 or index == len(points) - 1:
                return
            x = value / (360.0 if mode['x_axis'] == 'hue' else 1.0) * 255.0
            x = max(points[index - 1].x() + 0.1, min(points[index + 1].x() - 0.1, x))
        else:
            y = self.hue_shift_to_y(value / 360.0) if mode['y_type'] == 'shift' else self.multiplier_to_y(value)
            y = max(0.0, min(255.0, y))
            if self._should_sync_endpoints() and self._is_endpoint(index):
                points[0] = QPointF(0, y)
                points[-1] = QPointF(255, y)
        points[index] = QPointF(x, y)
        self._lut_dirty = True
        self.update_lut()
        self._update_info_label(y)
        self.update()

    def add_anchor(self, position: float) -> None:
        """Add or inspect a preset position without displacing the curve."""
        points = self.curves[self._current_mode]
        x = max(0.0, min(255.0, position * 255.0))
        for i, point in enumerate(points):
            if abs(point.x() - x) < 0.5:
                self._inspected_point_idx = i
                self._update_info_label(point.y())
                self.update()
                return
        y = float(np.interp(x, np.arange(256), self.calculate_curve(points)))
        self.interaction_started.emit()
        point = QPointF(x, y)
        points.append(point)
        points.sort(key=lambda p: p.x())
        self._inspected_point_idx = points.index(point)
        self._lut_dirty = True
        self.update_lut()
        self.flush()
        self.interaction_ended.emit()
        self._update_info_label(y)
        self.update()

    def _is_hue_curve(self) -> bool:
        """Check if current curve has hue as X-axis (wrap-around required)."""
        mode_info = CURVE_MODES[self._current_mode]
        return mode_info['x_axis'] == 'hue'
    
    def _should_sync_endpoints(self) -> bool:
        """
        Check if endpoint synchronization should be active.
        Only sync when:
        1. Current curve is hue-based (X-axis is hue)
        2. Curve has exactly 2 points (only the endpoints)
        
        Once user adds extra control points, sync is disabled for fine-tuning.
        """
        if not self._is_hue_curve():
            return False
        pts = self.curves[self._current_mode]
        return len(pts) == 2

    def mouseMoveEvent(self, event):
        if self.selected_point_idx != -1:
            pos = event.position()
            mx, my = self.from_widget(pos.x(), pos.y())

            mx = max(0, min(255, mx))
            my = max(0, min(255, my))

            pts = self.curves[self._current_mode]

            if self._is_endpoint(self.selected_point_idx):
                original_x = pts[self.selected_point_idx].x()
                mx = original_x
                
                # === Hue Wrap-around Synchronization ===
                # Only active when curve has exactly 2 endpoints (no extra control points)
                if self._should_sync_endpoints():
                    if self.selected_point_idx == 0:
                        # Dragging left endpoint, sync right endpoint Y value
                        pts[-1] = QPointF(255, my)
                    elif self.selected_point_idx == len(pts) - 1:
                        # Dragging right endpoint, sync left endpoint Y value
                        pts[0] = QPointF(0, my)
            else:
                if self.selected_point_idx > 0:
                    mx = max(mx, pts[self.selected_point_idx-1].x() + 0.1)
                if self.selected_point_idx < len(pts) - 1:
                    mx = min(mx, pts[self.selected_point_idx+1].x() - 0.1)

            pts[self.selected_point_idx] = QPointF(mx, my)

            # Update info label based on current mode's Y type
            self._update_info_label(my)

            self._lut_dirty = True
            self.update_lut()
            self.update()

    def mouseReleaseEvent(self, event):
        # 对称收尾：press 发过 interaction_started 的手势，release 必须
        # 先 flush 节流 pending 再发 ended——否则 on_interaction_ended 的
        # autosave/idle 渲染先于最终曲线值应用。
        if getattr(self, '_press_interaction', False):
            self._press_interaction = False
            self.flush()
            self.interaction_ended.emit()
        self.selected_point_idx = -1
        self.update()

    def mouseDoubleClickEvent(self, event):
        default_pts = [QPointF(0, 127.5), QPointF(255, 127.5)]
        # 只有曲线确实偏离默认时，双击重置才算一次可撤销交互
        changed = self.curves[self._current_mode] != default_pts
        if changed:
            self.interaction_started.emit()
        self.curves[self._current_mode] = default_pts
        self.selected_point_idx = -1
        self._update_info_label()
        self._lut_dirty = True
        self.update_lut()
        self.update()
        if changed:
            self.flush()
            self.interaction_ended.emit()

    def _update_info_label(self, y_value: float = 127.5) -> None:
        """Update info label based on current mode's Y type."""
        mode_info = CURVE_MODES[self._current_mode]
        y_type = mode_info['y_type']

        if y_type == 'mult':
            # Multiplier type: 0.0-2.0 range
            multiplier = self.y_to_multiplier(y_value)
            self.info_label.setText(f"{multiplier:.2f}x")
        elif y_type == 'shift':
            # Shift type: -0.5 to +0.5 range (for hue)
            shift = self.y_to_hue_shift(y_value)
            self.info_label.setText(f"{shift:+.2f}")
        self.point_state_changed.emit()

    def y_to_multiplier(self, y_value: float) -> float:
        """Convert Y coordinate (0-255) to saturation multiplier (0.0-2.0). INVERTED."""
        return (255.0 - y_value) / 127.5

    def multiplier_to_y(self, multiplier: float) -> float:
        """Convert saturation multiplier (0.0-2.0) to Y coordinate (0-255). INVERTED."""
        return 255.0 - (multiplier * 127.5)

    def y_to_hue_shift(self, y_value: float) -> float:
        """Convert Y coordinate (0-255) to hue shift (-0.5 to +0.5). INVERTED."""
        # Y=0 (top) -> +0.5 (positive shift)
        # Y=127.5 (center) -> 0.0 (no shift)
        # Y=255 (bottom) -> -0.5 (negative shift)
        return (127.5 - y_value) / 255.0

    def hue_shift_to_y(self, shift: float) -> float:
        """Convert hue shift (-0.5 to +0.5) to Y coordinate (0-255). INVERTED."""
        return 127.5 - (shift * 255.0)

    def update_lut(self, emit_changed: bool = True) -> None:
        """Calculate LUT for all 6 curves and update cache. Generates (6, 256) float32 array."""
        if self._lut_dirty:
            self._lut_cache = np.zeros((6, 256), dtype=np.float32)

            for i, mode in enumerate(MODE_ORDER):
                curve_y = self.calculate_curve(self.curves[mode])
                mode_info = CURVE_MODES[mode]
                y_type = mode_info['y_type']

                if y_type == 'mult':
                    # Multiplier: 0.0-2.0 range, default 1.0
                    self._lut_cache[i] = (255.0 - curve_y) / 127.5
                elif y_type == 'shift':
                    # Hue shift: -0.5 to +0.5 range, default 0.0
                    self._lut_cache[i] = (127.5 - curve_y) / 255.0

            self._lut_dirty = False

        if not emit_changed:
            self._pending_lut = None
            self._pending_data = None
            self._update_timer.stop()
            return

        self._pending_lut = self._lut_cache.copy()
        self._pending_data = self.get_curves_data()

        if not self._update_timer.isActive():
            self._update_timer.start()

    def _emit_changed_signal(self) -> None:
        """Throttled signal emission."""
        if self._pending_lut is not None:
            self.changed.emit(self._pending_lut, self._pending_data)
            self._pending_lut = None
            self._pending_data = None

    def flush(self):
        """立即发射待处理的 LUT 变更信号（跳过节流延迟）。
        用于 apply grade 等需要同步应用曲线的场景。
        """
        if self._pending_lut is not None:
            self._update_timer.stop()
            self._emit_changed_signal()

    def reset_current_curve(self, emit_changed: bool = True):
        """Reset only the current curve to default."""
        default_pts = [QPointF(0, 127.5), QPointF(255, 127.5)]
        # 重置构成一次可撤销交互：偏离默认时发 started/ended 对
        #（此前 reset 按钮改引擎状态但不压撤销栈，Ctrl+Z 撤不掉）
        changed = self.curves[self._current_mode] != default_pts
        if changed and emit_changed:
            self.interaction_started.emit()
        self.curves[self._current_mode] = default_pts
        self.selected_point_idx = -1
        self._update_info_label()
        self._lut_dirty = True
        self.update_lut(emit_changed=emit_changed)
        self.update()
        if changed and emit_changed:
            self.flush()
            self.interaction_ended.emit()

    def reset_all_curves(self, emit_changed: bool = True):
        """Reset all 6 curves to default."""
        default_pts = [QPointF(0, 127.5), QPointF(255, 127.5)]
        # 同 reset_current_curve：偏离默认的重置才算一次可撤销交互
        changed = any(self.curves[m] != default_pts for m in MODE_ORDER)
        if changed and emit_changed:
            self.interaction_started.emit()
        for mode in MODE_ORDER:
            self.curves[mode] = [QPointF(0, 127.5), QPointF(255, 127.5)]
        self.selected_point_idx = -1
        self._update_info_label()
        self._lut_dirty = True
        self.update_lut(emit_changed=emit_changed)
        self.update()
        if changed and emit_changed:
            self.flush()
            self.interaction_ended.emit()

    def get_curves_data(self):
        """Returns all control points as dictionary."""
        data = {}
        for mode in MODE_ORDER:
            data[mode] = [(p.x(), p.y()) for p in self.curves[mode]]
        return data

    def set_curves_data(self, data, emit_changed: bool = False):
        """Restores all control points from data dictionary.
        
        Args:
            data: Dictionary of control points per mode
            emit_changed: If True, emit changed signal after update (default False)
                          When False, only updates UI cache without triggering timer
        """
        if not data:
            self.reset_all_curves(emit_changed=emit_changed)
            return

        for mode in MODE_ORDER:
            if mode in data:
                self.curves[mode] = [QPointF(x, y) for x, y in data[mode]]
            else:
                self.curves[mode] = [QPointF(0, 127.5), QPointF(255, 127.5)]

        self.selected_point_idx = -1
        self._inspected_point_idx = 0
        self._update_info_label()
        self._lut_dirty = True
        
        # Rebuild LUT cache without starting timer
        if self._lut_dirty:
            self._lut_cache = np.zeros((6, 256), dtype=np.float32)
            for i, mode in enumerate(MODE_ORDER):
                curve_y = self.calculate_curve(self.curves[mode])
                mode_info = CURVE_MODES[mode]
                y_type = mode_info['y_type']
                if y_type == 'mult':
                    self._lut_cache[i] = (255.0 - curve_y) / 127.5
                elif y_type == 'shift':
                    self._lut_cache[i] = (127.5 - curve_y) / 255.0
            self._lut_dirty = False
        
        self.update()
        
        if emit_changed:
            self._pending_lut = self._lut_cache.copy()
            self._pending_data = self.get_curves_data()
            if not self._update_timer.isActive():
                self._update_timer.start()

    def set_lut(self, lut, emit_changed: bool = False):
        """Restore HSL curve control points from LUT array.
        
        Args:
            lut: LUT array of shape (6, 256) float32
            emit_changed: If True, emit changed signal after update (default False)
        
        Note: This method generates approximate control points by sampling
        key positions in the LUT. It's used for legacy project restore when
        only LUT is available but no control points data.
        
        Coordinate conversions:
        - Multiplier type (LumSat, HueSat, HueLum, SatSat, SatLum): 
          y = 255.0 - value * 127.5 (value range 0.0-2.0, default 1.0)
        - Shift type (HueHue):
          y = 127.5 - shift * 255.0 (shift range -0.5 to +0.5, default 0.0)
        """
        if lut is None:
            return
            
        lut = np.asarray(lut, dtype=np.float32)
        
        if lut.shape[0] != 6 or lut.shape[1] != 256:
            return
        
        sample_positions = [0, 32, 64, 96, 128, 160, 192, 224, 255]

        was_blocked = self.blockSignals(True)

        try:
            for i, mode in enumerate(MODE_ORDER):
                mode_info = CURVE_MODES[mode]
                y_type = mode_info['y_type']
                curve_lut = lut[i]

                points = []
                for x in sample_positions:
                    if x < len(curve_lut):
                        val = curve_lut[x]
                        if y_type == 'mult':
                            y_ui = 255.0 - val * 127.5
                        else:
                            y_ui = 127.5 - val * 255.0
                        y_ui = max(0.0, min(255.0, y_ui))
                        points.append(QPointF(float(x), y_ui))

                if len(points) >= 2:
                    self.curves[mode] = points
                else:
                    self.curves[mode] = [QPointF(0, 127.5), QPointF(255, 127.5)]

            self._lut_dirty = True
            self._lut_cache = None
            self.update_lut(emit_changed=emit_changed)
            self.update()
        finally:
            self.blockSignals(was_blocked)

        self.point_state_changed.emit()

        # AUD-38302: changed 信号必须在 blockSignals 恢复之后发射。
        # 旧实现在 blocked 区内调用 _emit_changed_signal()：
        # 1) Qt 级 blockSignals 直接吞掉 self.changed.emit；
        # 2) _pending_lut 已被置 None，16ms 节流定时器重放时也无事可做。
        # 结果 emit_changed=True 时信号永不送达接收方。
        if emit_changed and self._pending_lut is not None:
            self._update_timer.stop()
            self._emit_changed_signal()

    def add_point_from_color(self, r: float, g: float, b: float) -> bool:
        """
        Add a control point on the current curve based on RGB color picked from image.

        This method:
        1. Converts RGB (0-255) to the X-axis coordinate based on current mode's x_axis type
        2. Uses PCHIP interpolation to find the exact Y value at that X position
        3. Inserts the point seamlessly without altering the curve shape

        Args:
            r, g, b: RGB values in 0-255 range

        Returns:
            True if point was added successfully, False otherwise
        """
        import colorsys

        mode_info = CURVE_MODES[self._current_mode]
        x_axis = mode_info['x_axis']

        # Normalize RGB to 0.0-1.0 for colorsys
        r_norm = r / 255.0
        g_norm = g / 255.0
        b_norm = b / 255.0

        # Calculate X coordinate based on x_axis type
        x_coord = 0.0

        if x_axis == 'lum':
            # Luminance: standard BT.601 luma formula
            # Y = 0.299*R + 0.587*G + 0.114*B
            luma = 0.299 * r + 0.587 * g + 0.114 * b
            x_coord = luma  # Already in 0-255 range

        elif x_axis == 'hue':
            # Hue: convert RGB to HSV, extract hue (0-360°), map to 0-255
            h, s, v = colorsys.rgb_to_hsv(r_norm, g_norm, b_norm)
            # h is in 0.0-1.0 range, convert to degrees then to 0-255
            hue_deg = h * 360.0
            x_coord = (hue_deg / 360.0) * 255.0

        elif x_axis == 'sat':
            # Saturation: convert RGB to HSV, extract saturation (0.0-1.0), map to 0-255
            h, s, v = colorsys.rgb_to_hsv(r_norm, g_norm, b_norm)
            x_coord = s * 255.0

        # Clamp X to valid range
        x_coord = max(0.0, min(255.0, x_coord))

        # Get current curve points
        pts = self.curves[self._current_mode]

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
                self._inspected_point_idx = self.selected_point_idx
                self._update_info_label(p.y())
                self.update()
                return True

        # Insert new point
        pts.append(new_point)
        pts.sort(key=lambda p: p.x())

        # Update selected point index to the newly added point
        self.selected_point_idx = pts.index(new_point)
        self._inspected_point_idx = self.selected_point_idx
        self._update_info_label(new_point.y())

        # Emit interaction signals for proper render pipeline.
        # 顺序契约：started（压撤销基线）→ 应用变更（flush 使 changed
        # 立即到达引擎）→ ended（autosave/idle 渲染）。
        self.interaction_started.emit()

        # Mark LUT as dirty and update
        self._lut_dirty = True
        self.update_lut()
        self.update()
        self.flush()
        self.interaction_ended.emit()

        return True
