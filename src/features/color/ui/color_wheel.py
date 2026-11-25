"""
色轮组件模块（Topos 原创"光学双环"视觉版）
Color Wheel Module - ColorWheel widget and GradingPanel (original visual design)

合并自:
- src/ui/widgets/color_wheel.py (ColorWheel 类, now at src/features/color/ui/color_wheel.py)
- src/ui/grading_panel.py (GradingPanel 类, now at src/features/color/ui/color_wheel.py)

视觉说明（原创设计，非达芬奇复刻）：
- 单轮为"光学内凹双环"：极细外环（Master 亮度弧 + 环拖拽）+ 高饱和彩环
  + 低饱和度色相内盘 + 十字准星控制点。
- 底部为金属滚花拨轮（KnurledJogWheel，相对式拖拽，不吸附）。
- 面板下方标签可在色轮与 Base Grade 间切换。色轮标签包含
  色轮（Lift/Gamma/Gain/Offset）/ Color Bars / Log 色轮 / 色调饱和度。

状态模型与信号契约保持不变：
- 色轮内部仍为 (hue, sat, brightness)，加性轮（Offset / Log 四轮）经
  params_to_offset_rgb 转为加性 RGB 偏移；数值框显示值经 display 换算。
- grading_changed 仍为 13 个位置参数（exposure 由面板内部状态透传，
  UI 不再直接暴露曝光参数，其位置由 Pivot 取代）；新增扩展参数经
  grading_extended_changed(dict) 下发。
"""

import colorsys
import math
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Optional
from PyQt6.QtWidgets import (QWidget, QToolButton, QPushButton, QHBoxLayout,
                             QVBoxLayout, QStackedWidget, QSizePolicy, QLabel,
                             QFrame)
from PyQt6.QtCore import QEvent, QObject, Qt, QPointF, pyqtSignal, QRectF, QTimer
from PyQt6.QtGui import (QPainter, QColor, QConicalGradient, QRadialGradient,
                         QBrush, QPen, QMouseEvent, QKeyEvent, QPainterPath,
                         QLinearGradient)

from src.shared.assets.icon_manager import icon_add, icon_delete, icon_circle
from src.shared.ui.widgets.drag_value import DragValue, drag_speed_per_pixel
from src.shared.ui.style_sheet import C
from src.shared.i18n.i18n import I18N
from src.features.color.core.base_grade import BASE_GRADE_DEFAULTS
from src.features.color.core.composition_uniforms import (
    params_to_offset_rgb, params_to_rgb, offset_chroma_scale, offset_master_scale,
)
from src.features.color.core.layer import DEFAULT_EXTENDED_GRADING_PARAMS
from src.features.color.core.primary_calibration import (
    PRIMARY_CALIBRATION_CURRENT_VERSION, PRIMARY_CALIBRATION_TEMP_SCALE,
    PRIMARY_CALIBRATION_VERSION_KEY, ungraded_primary_params,
)
from src.features.color.ui.primary_wheel_response import (
    PUCK_DRAG_GAIN, RGB, brightness_multiplier, channel_drag_rate, constrain_chroma, master_drag_rate,
    multiplier_brightness, scalar_drag_rate, shift_master, wheel_chroma, wheel_luma, wheel_position,
)
from src.features.color.ui.tonal_saturation_panel import TonalSaturationPanel
from src.features.color.ui.panel_chrome import (
    PARAMETER_LABEL_STYLE, SECTION_TITLE_STYLE,
    PARAMETER_BAR_BOTTOM_MARGIN, PARAMETER_BAR_HEIGHT, create_panel_header, header_button_color,
    style_header_button, style_reset_button,
)

if TYPE_CHECKING:
    from src.features.color.ui.base_grade_panel import _BaseGradeWheel


# ============================================================================
# 达芬奇色轮角度校准常量
# ============================================================================
# Resolve's wheel: red at upper left (120°), blue at right, cyan lower right.
# Qt's conical gradient and the pointer both advance counterclockwise.
HUE_OFFSET_ANGLE = 120.0

# 面板模式页索引（mode_stack / 上下参数条 stack 共用）
MODE_WHEELS = 0
MODE_BARS = 1
MODE_LOG = 2
MODE_TONAL = 3

# 加性轮（Offset / Log）数值框显示范围（显示单位 = 加性偏移 × 100）。
# 覆盖正向模型可达包络（色度 ±50 / 主亮度 ±100，组合极值约 [-125, 150]）；
# 超包络输入反解时 master 会被收窄到 [-1, 1]（显示吸附回可达值）。
_OFFSET_DISPLAY_MIN = -200.0
_OFFSET_DISPLAY_MAX = 200.0

# 标量参数行（色温/色调/对比度等）数值框底部的渐变标识条配色——
# 取自 Wheel_UI 设计稿：左端 = 参数负方向的色彩语义，右端 = 正方向。
_ACCENT_TEMP = ("qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                "stop:0 #3b82f6, stop:0.5 #525866, stop:1 #e09f3e)")
_ACCENT_TINT = ("qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                "stop:0 #28a768, stop:0.5 #525866, stop:1 #c04888)")
_ACCENT_CONTRAST = ("qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                    "stop:0 #262930, stop:1 #d4d7dd)")
_ACCENT_RAINBOW = ("qlineargradient(x1:0, y1:0, x2:1, y2:0, "
                   "stop:0 #d93848, stop:0.35 #d4b12a, "
                   "stop:0.68 #28a768, stop:1 #337ab7)")
_ACCENT_NEUTRAL = "#32363f"


# ============================================================================
# 加性轮显示单位换算（Offset / Log 四轮共用）
# ============================================================================

def offset_hsb_to_display(hue: float, sat: float,
                          brightness: float) -> tuple[float, float, float]:
    """(h, s, b) → 数值框显示值（加性偏移 × 100，中性 (0, 0, 0)）。"""
    bias = params_to_offset_rgb(hue, sat, brightness)
    return (bias[0] * 100.0, bias[1] * 100.0, bias[2] * 100.0)


def offset_display_to_hsb(rx: float, gy: float,
                          bz: float) -> tuple[float, float, float]:
    """显示值 → (h, s, b) 精确反解（offset_hsb_to_display 的逆映射）。

    正向：bias = (chroma_norm - 1) * CHROMA + master * MASTER。
    反向：master = clamp(mean(bias))；chroma_norm = (bias - master) /
    CHROMA + 1，再经 HSV 反算 hue/sat（对等比缩放不变，按最大分量
    归一后取色相与饱和度）。
    """
    bx = max(_OFFSET_DISPLAY_MIN, min(_OFFSET_DISPLAY_MAX, rx)) / 100.0
    by = max(_OFFSET_DISPLAY_MIN, min(_OFFSET_DISPLAY_MAX, gy)) / 100.0
    bz = max(_OFFSET_DISPLAY_MIN, min(_OFFSET_DISPLAY_MAX, bz)) / 100.0
    master_limit = offset_master_scale()
    master = max(-master_limit, min(master_limit, (bx + by + bz) / 3.0))
    chroma_scale = offset_chroma_scale()
    chroma = [(v - master) / chroma_scale + 1.0 for v in (bx, by, bz)]
    m = max(chroma[0], chroma[1], chroma[2], 1e-6)
    hue, sat, _ = colorsys.rgb_to_hsv(*(max(0.0, v / m) for v in chroma))
    return hue * 360.0, sat, master


class _GradingValue(DragValue):
    """Compact value cell with a channel underline and the shared edit gestures."""

    def __init__(self, minimum: float, maximum: float, default: float,
                 decimals: int = 2, accent: str = "#919195",
                 drag_span: float | None = None, *, drag_rate: float | None = None,
                 height: int = 24) -> None:
        super().__init__("", minimum, maximum, default, decimals=decimals, compact=True)
        self._drag_span = drag_span
        self._drag_rate = drag_rate
        self._cancelled_drag = False
        self.setFixedValueWidth(44)
        self.setFixedHeight(height)

        self._value_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout = self.layout()
        field = QVBoxLayout()
        field.setContentsMargins(0, 0, 0, 0)
        field.setSpacing(0)
        for widget in (self._value_label, self._edit):
            layout.removeWidget(widget)
            field.addWidget(widget)
        self._accent_bar = QWidget(self)
        self._accent_bar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._accent_bar.setFixedHeight(2)
        self._accent_bar.setStyleSheet(f"background: {accent}; border: none;")
        field.addWidget(self._accent_bar)
        layout.addLayout(field)
        layout.setAlignment(field, Qt.AlignmentFlag.AlignVCenter)

    def fit_display_range(self, minimum_width: int) -> None:
        self._value_label.ensurePolished()
        metrics = self._value_label.fontMetrics()
        text_width = max(metrics.horizontalAdvance(self._format_value(value))
                         for value in (self._min, self._max))
        # Leave room for the field border, padding, and inline editor cursor.
        self.setFixedValueWidth(max(minimum_width, text_width + 6))

    @staticmethod
    def _precision(modifiers: Qt.KeyboardModifier) -> float:
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            return 10.0
        if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            return 0.1
        return 1.0

    def set_drag_rate(self, rate: float | None) -> None:
        self._drag_rate = rate

    def _drag_sensitivity(self, modifiers: Qt.KeyboardModifier) -> float:
        if self._drag_rate is not None:
            return self._drag_rate * self._precision(modifiers)
        if self._drag_span is None:
            return super()._drag_sensitivity(modifiers)
        return drag_speed_per_pixel(self._drag_span, modifiers)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._cancelled_drag = False
        if event.button() == Qt.MouseButton.MiddleButton:
            if self.value() != self.defaultValue():
                self.interaction_started.emit()
                self.setValue(self.defaultValue())
                self.interaction_ended.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        super().mouseMoveEvent(event)
        if self._drag_rate is not None and self._drag_engaged:
            # Only the next movement uses a newly pressed modifier. An overshoot
            # at a limit does not have to be dragged back before values change.
            self._drag_start_x = event.position().x()
            self._drag_start_value = self.value()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._cancelled_drag:
            self._cancelled_drag = False
            self.interaction_ended.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def cancel_drag(self, *, end_on_release: bool = True) -> None:
        """Keep the gesture boundary while discarding the old pointer baseline."""
        if self._dragging:
            self._cancelled_drag = self._drag_engaged and end_on_release
            self._dragging = self._drag_engaged = False
            self._value_label.setStyleSheet(self._normal_value_style)

    def wheelEvent(self, event: Any) -> None:
        step = self._single_step * self._precision(event.modifiers())
        delta = event.angleDelta().y()
        target = self.value() + (step if delta > 0 else -step if delta < 0 else 0.0)
        target = max(self._min, min(self._max, target))
        if target != self.value():
            self.interaction_started.emit()
            self.setValue(target)
            self.interaction_ended.emit()
        event.accept()


class _PrimaryValueRow(QWidget):
    """Label and value only; display units are independent of stored grade values."""

    _VALUE_WIDTH = 48
    valueChanged = pyqtSignal(float)
    interactionStarted = pyqtSignal()
    interactionEnded = pyqtSignal()

    def __init__(self, label: str, minimum: float, maximum: float,
                 *, drag_rate: float, scale: float = 1.0, offset: float = 0.0,
                 decimals: int = 2, accent: str = "#919195",
                 default: float | None = None) -> None:
        super().__init__()
        self._scale = scale
        self._offset = offset
        self._drag_rate = drag_rate
        self._bounds = minimum, maximum
        # 引擎值域内的默认值（None 时回退旧行为 offset；用于 pivot /
        # lum_mix 等非零默认参数）。
        self._default_value = float(offset if default is None else default)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 3, 2, 3)
        layout.setSpacing(4)
        layout.addStretch()
        self.label = QLabel(label)
        self.label.setStyleSheet(PARAMETER_LABEL_STYLE)
        self.label.setMinimumWidth(1)
        self.label.setToolTip(label)
        layout.addWidget(self.label)
        self.spin = _GradingValue(minimum * scale + offset,
                                  maximum * scale + offset,
                                  self._default_value * scale + offset,
                                  decimals, accent, drag_rate=drag_rate * scale)
        self.spin.setAccessibleName(label)
        self.spin.setFixedValueWidth(self._VALUE_WIDTH)
        layout.addWidget(self.spin)
        layout.addStretch()
        self.spin.valueChanged.connect(self._on_value_changed)
        self.spin.interaction_started.connect(self.interactionStarted.emit)
        self.spin.interaction_ended.connect(self.interactionEnded.emit)

    def _on_value_changed(self, value: float) -> None:
        self.valueChanged.emit((value - self._offset) / self._scale)

    def value(self) -> float:
        return (self.spin.value() - self._offset) / self._scale

    def setValue(self, value: float) -> None:
        self.spin.setValue(value * self._scale + self._offset)

    def set_display_units(self, scale: float, offset: float, decimals: int, *,
                          display_bounds: tuple[float, float] | None = None) -> None:
        value = self.value()
        blocked = self.spin.blockSignals(True)
        self._scale, self._offset = scale, offset
        minimum, maximum = display_bounds or (
            self._bounds[0] * scale + offset, self._bounds[1] * scale + offset)
        self.spin.setRange(minimum, maximum)
        self.spin.setDefaultValue(self._default_value * scale + offset)
        self.spin.setDecimals(decimals)
        self.spin.fit_display_range(self._VALUE_WIDTH)
        self.spin.set_drag_rate(self._drag_rate * scale)
        self.setValue(value)
        self.spin.blockSignals(blocked)

    def reset(self) -> None:
        self.setValue(self._default_value)

    def blockSignals(self, block: bool) -> bool:
        self.spin.blockSignals(block)
        return super().blockSignals(block)


# ============================================================================
# 精致影调区间图标（Lift / Gamma / Gain / Offset / Log 区间）
# ============================================================================

class ZoneIconWidget(QWidget):
    """影调十字图标：中心十字准星 + 按影调类型着色的定位点。"""

    def __init__(self, zone: Optional[str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.zone = zone
        self.setFixedSize(22, 22)

    def paintEvent(self, event: Any) -> None:
        if not self.zone:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 中心精准十字准星（1px 亚像素对齐）
        painter.setPen(QPen(QColor("#b8bec8"), 1.25,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        painter.drawLine(QPointF(9.0, 3.5), QPointF(9.0, 14.5))
        painter.drawLine(QPointF(3.5, 9.0), QPointF(14.5, 9.0))

        if self.zone == "lift":
            pen, fill = QColor("#525866"), QColor("#090a0c")
        elif self.zone == "gamma":
            pen, fill = QColor("#9ca3af"), QColor("#6b7280")
        elif self.zone == "gain":
            pen, fill = QColor("#ffffff"), QColor("#f3f4f6")
        elif self.zone == "offset":
            pen, fill = QColor("#d99b38"), QColor("#2a2213")
        else:  # log 区间（shadow/midtone/highlight）：中性空心点
            pen, fill = QColor("#8b909b"), QColor("#1a1c21")
        painter.setPen(QPen(pen, 1.1))
        painter.setBrush(fill)
        painter.drawEllipse(QPointF(14.5, 14.5), 2.6, 2.6)
        painter.end()


# ============================================================================
# 金属滚花主亮度拨轮（相对式拖拽，不吸附；来自新 UI 设计稿）
# ============================================================================

class KnurledJogWheel(QWidget):
    """Master thumbwheel: relative horizontal drag without a clickable track.

    色轮、色条和 Log 的拨轮宽度取自上方固定宽度的数值组。

    Signals:
        dragStarted / dragEnded: 手势边界（撤销粒度对齐一次手势一次 push）
        deltaMoved(float): 相对像素增量（已含修饰键系数）
        resetRequested: 中键 / 双击请求主亮度归零
    """

    dragStarted = pyqtSignal()
    dragEnded = pyqtSignal()
    deltaMoved = pyqtSignal(float)
    resetRequested = pyqtSignal()

    _PITCH = 6.0

    def __init__(self, width: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(width, 14)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._phase = 0.0
        self._dragging = False
        self._last_x = 0.0
        self._hovered = False

    def set_master_value(self, delta: float, rate: float) -> None:
        phase = (delta / rate * PUCK_DRAG_GAIN) % self._PITCH
        if phase != self._phase:
            self._phase = phase
            self.update()

    def cancel_drag(self) -> None:
        if self._dragging:
            self._dragging = False
            self.update()

    def enterEvent(self, event: Any) -> None:
        self._hovered = True
        self.update()

    def leaveEvent(self, event: Any) -> None:
        self._hovered = False
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self.resetRequested.emit()
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._dragging = True
            self._last_x = event.position().x()
            self.dragStarted.emit()
            self.update()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            dx = event.position().x() - self._last_x
            self._last_x = event.position().x()
            fine = 0.2 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 1.0
            if dx:
                self.deltaMoved.emit(dx * fine)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            # 与旧拨轮一致：release 位置相对最后一次 move 的增量也要计入，
            # 否则手势末端的少量拖动会被丢掉。
            self.mouseMoveEvent(event)
            self._dragging = False
            self.update()
            self.dragEnded.emit()
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.resetRequested.emit()
            event.accept()

    def wheelEvent(self, event: Any) -> None:
        dx = (event.angleDelta().y() / 120.0) * self._PITCH
        fine = 0.2 if (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) else 1.0
        self._nudge(dx * fine)
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        navigation = (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up,
                      Qt.Key.Key_Down)
        if event.key() in navigation:
            step = -self._PITCH if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up) \
                else self._PITCH
            self._nudge(step)
            event.accept()
        else:
            super().keyPressEvent(event)

    def _nudge(self, dx: float) -> None:
        if dx:
            if self._dragging:
                self.deltaMoved.emit(dx)
                return
            self.dragStarted.emit()
            self.deltaMoved.emit(dx)
            self.dragEnded.emit()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        rect = QRectF(0.5, 0.5, w - 1.0, h - 1.0)
        radius = h / 2.0

        cylinder = QLinearGradient(0.0, 0.0, w, 0.0)
        cylinder.setColorAt(0.00, QColor("#08090b"))
        cylinder.setColorAt(0.15, QColor("#15171b"))
        cylinder.setColorAt(0.50, QColor("#2a2d34" if self._hovered else "#23262c"))
        cylinder.setColorAt(0.85, QColor("#15171b"))
        cylinder.setColorAt(1.00, QColor("#08090b"))

        border = QColor("#d99b38" if self._dragging else "#0a0b0d")
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(QBrush(cylinder))
        painter.drawRoundedRect(rect, radius, radius)

        clip = QPainterPath()
        clip.addRoundedRect(rect.adjusted(1.0, 1.0, -1.0, -1.0), radius, radius)
        painter.setClipPath(clip)

        pitch = self._PITCH
        x = -pitch + self._phase
        while x < w + pitch:
            norm_pos = (x - w / 2.0) / (w / 2.0)
            if abs(norm_pos) <= 1.0:
                cos_light = math.cos(norm_pos * (math.pi * 0.48))
                dark_alpha = int(190 * cos_light)
                light_alpha = int(75 * cos_light)
                painter.setPen(QPen(QColor(6, 7, 9, dark_alpha), 1.4))
                painter.drawLine(QPointF(x, 1.5), QPointF(x, h - 1.5))
                painter.setPen(QPen(QColor(190, 198, 210, light_alpha), 0.9))
                painter.drawLine(QPointF(x + 1.4, 2.0), QPointF(x + 1.4, h - 2.0))
            x += pitch
        painter.end()


# ============================================================================
# 光学内凹双环色轮画布
# ============================================================================

class _WheelCanvas(QWidget):
    """Optical dual-ring disk; drawing and hit testing share one geometry.

    - 内盘（inner_r 以内）：相对式 trackerball 拖拽 hue/sat（按下不跳转）。
    - 外环（inner_r..outer_r）：相对式角向拖拽调 Master 亮度。
    """

    interaction_started = pyqtSignal()

    _RING_WIDTH = 4.0
    _HUE_RING_FRACTION = 0.09

    def __init__(self, wheel: "ColorWheel | _BaseGradeWheel") -> None:
        super().__init__(wheel)
        self._wheel = wheel
        self.dragging_knob = False
        self.dragging_ring = False
        self._last_pointer = QPointF()
        self._last_ring_angle = 0.0
        self._hovered = False
        self.setMinimumSize(96, 110)
        self.setMouseTracking(True)

    # ── 几何（绘制与命中测试共用） ────────────────────────────────────

    def _geometry(self) -> tuple[QPointF, float]:
        # 半径随画布的可用空间缩放，封顶 100px。
        radius = max(1.0, min((self.width() - 12.0) / 2.0,
                              (self.height() - 6.0) / 2.0, 100.0))
        return QPointF(self.width() / 2.0, self.height() / 2.0), radius

    def _inner_radius(self, outer_radius: float) -> float:
        return max(1.0, outer_radius - self._RING_WIDTH)

    def cancel_drag(self) -> None:
        self.dragging_knob = False
        self.dragging_ring = False

    def _hue_gradient(self, center: QPointF) -> QConicalGradient:
        gradient = QConicalGradient(center, self._wheel.rotation_degrees)
        for sector in range(7):
            gradient.setColorAt(sector / 6.0, QColor.fromHsvF((sector % 6) / 6.0, 1.0, 1.0))
        return gradient

    # ── 绘制 ─────────────────────────────────────────────────────────

    def enterEvent(self, event: Any) -> None:
        self._hovered = True
        self.update()

    def leaveEvent(self, event: Any) -> None:
        self._hovered = False
        self.update()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center, outer_r = self._geometry()
        inner_r = self._inner_radius(outer_r)
        disk_r = inner_r * (1.0 - self._HUE_RING_FRACTION)

        # 1. 极细精密外环凹槽（Master 亮度弧所在环带）
        painter.setPen(QPen(QColor("#505663" if self._hovered else "#343842"), 0.75))
        painter.setBrush(QColor("#121419"))
        painter.drawEllipse(center, outer_r, outer_r)

        # 2. 外环 Master 亮度指示弧（默认位于右下侧，随亮度角向移动）
        ring_track_r = (outer_r + inner_r) / 2.0
        track_rect = QRectF(center.x() - ring_track_r, center.y() - ring_track_r,
                            ring_track_r * 2.0, ring_track_r * 2.0)
        norm_y = self._wheel._master_arc_position()
        base_span = 34.0
        center_deg = 285.0 - norm_y * 150.0
        start_deg = center_deg - base_span / 2.0
        arc_col = QColor("#f0b44c") if abs(norm_y) > 0.004 else QColor("#d9dde4")
        painter.setPen(QPen(arc_col, 1.7, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        painter.drawArc(track_rect, int(start_deg * 16), int(base_span * 16))

        # 3. 彩环与内盘共享色相，彩环也属于 Hue/Sat 拖拽区域。
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(self._hue_gradient(center)))
        painter.drawEllipse(center, inner_r, inner_r)

        # 中性遮罩限定在内盘，色相从中性中心向外轻微增强。
        mask = QRadialGradient(center, disk_r)
        mask.setColorAt(0.0, QColor(47, 47, 47))
        mask.setColorAt(0.48, QColor(42, 42, 42, 243))
        mask.setColorAt(1.0, QColor(29, 29, 29, 225))
        painter.setBrush(QBrush(mask))
        painter.drawEllipse(center, disk_r, disk_r)

        # 彩环和 Master 外环的细分界线。
        painter.setPen(QPen(QColor(0, 0, 0, 120), 0.65))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, inner_r, inner_r)

        # 4. 极细十字基准线与同心圆
        painter.setPen(QPen(QColor(255, 255, 255, 26), 0.75))
        painter.drawEllipse(center, inner_r * 0.52, inner_r * 0.52)
        painter.drawLine(center + QPointF(-disk_r, 0.0), center + QPointF(disk_r, 0.0))
        painter.drawLine(center + QPointF(0.0, -disk_r), center + QPointF(0.0, disk_r))

        # 5. 动态矢量牵引线与准星控制点
        magnitude = math.hypot(self._wheel.knob_pos.x(), self._wheel.knob_pos.y())
        puck = center + self._wheel.knob_pos * (inner_r / max(1.0, magnitude))
        if magnitude > 0.01:
            painter.setPen(QPen(QColor(240, 180, 76, 180), 1.1))
            painter.drawLine(center, puck)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(220, 224, 232, 160))
        painter.drawEllipse(center, 1.6, 1.6)

        ring_col = QColor("#f0b44c") if magnitude > 0.01 else QColor("#e5e7eb")
        painter.setPen(QPen(QColor(0, 0, 0, 160), 2.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(puck, 4.6, 4.6)

        painter.setPen(QPen(ring_col, 1.25))
        painter.setBrush(QColor(24, 26, 31, 200))
        painter.drawEllipse(puck, 4.4, 4.4)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(ring_col)
        painter.drawEllipse(puck, 1.6, 1.6)
        painter.end()

    # ── 交互 ─────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        center, outer_r = self._geometry()
        inner_r = self._inner_radius(outer_r)
        delta = event.position() - center
        dist = math.hypot(delta.x(), delta.y())
        if event.button() == Qt.MouseButton.RightButton:
            # 右键 = 仅重置轮盘色度（保留 Master 亮度）
            self._wheel.reset_wheel_only()
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if inner_r < dist <= outer_r + 3.0:
            self.dragging_ring = True
            self._last_ring_angle = math.atan2(delta.y(), delta.x())
            self.interaction_started.emit()
            event.accept()
        elif dist <= inner_r:
            self.dragging_knob = True
            self._last_pointer = event.position()
            self.interaction_started.emit()
            # Resolve uses a relative trackerball: pressing elsewhere on the
            # disk does not jump the current correction to that location.
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        center, outer_r = self._geometry()
        inner_r = self._inner_radius(outer_r)
        if self.dragging_knob:
            movement = event.position() - self._last_pointer
            self._last_pointer = event.position()
            if not movement.isNull():
                self._update_knob(center + self._wheel.knob_pos * inner_r
                                  + movement * PUCK_DRAG_GAIN)
            event.accept()
        elif self.dragging_ring:
            delta = event.position() - center
            angle = math.atan2(delta.y(), delta.x())
            delta_angle = angle - self._last_ring_angle
            while delta_angle > math.pi:
                delta_angle -= 2.0 * math.pi
            while delta_angle < -math.pi:
                delta_angle += 2.0 * math.pi
            self._last_ring_angle = angle
            self._wheel._drag_master_ring(delta_angle, inner_r)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        # None 事件防护：无按下状态的 release（程序化调用/合成事件）直接忽略。
        if event is None or event.button() != Qt.MouseButton.LeftButton:
            return
        if self.dragging_knob:
            self.mouseMoveEvent(event)
            self.dragging_knob = False
            self._wheel._on_value_interaction_ended()
        elif self.dragging_ring:
            self.mouseMoveEvent(event)
            self.dragging_ring = False
            self._wheel._on_value_interaction_ended()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        center, outer_r = self._geometry()
        delta = event.position() - center
        if event.button() == Qt.MouseButton.LeftButton \
                and math.hypot(delta.x(), delta.y()) <= outer_r:
            # 双击 = 仅重置轮盘色度（保留 Master 亮度，与右键一致）
            self._wheel.reset_wheel_only()

    def _update_knob(self, pos: QPointF) -> None:
        center, outer_r = self._geometry()
        inner_r = self._inner_radius(outer_r)
        delta = pos - center
        distance = math.hypot(delta.x(), delta.y())
        wheel = self._wheel
        # An existing numeric correction can project beyond the disk. Start
        # from that correction so the first mouse movement does not snap it.
        limit = inner_r * max(1.0, math.hypot(wheel.knob_pos.x(), wheel.knob_pos.y()))
        if distance > limit:
            delta *= limit / distance
        wheel._edit_knob(delta / inner_r)


# ============================================================================
# 单轮控件（标题行 + 色轮盘 + 数值框行 + 滚花拨轮）
# ============================================================================

class ColorWheel(QWidget):
    """单个色轮控件 - 用于选择色调、饱和度、亮度。

    Features:
    - 光学内凹双环：外环 = Master 亮度（角向拖拽 + 指示弧），
      内盘 = Hue/Sat（相对式 trackerball）
    - 紧凑的 Master + RGB 数值输入框（加性轮仅 RGB 三框）
    - 底部金属滚花拨轮（Master 亮度，相对式拖拽）
    - 标题行：影调图标 + 名称 + 重置按钮

    Signals:
        changed: (hue, sat, brightness) 当参数改变时发射
        interaction_started: 交互开始时发射
        interaction_ended: 交互结束时发射
    """
    # Emits (hue_offset, sat_scale, brightness_offset)
    # hue in degrees [0, 360], sat [0, 1.0], brightness [-1.0, 1.0]
    changed = pyqtSignal(float, float, float)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    _RING_SENSITIVITY = 2.0 / (2.0 * math.pi * 1.4)

    def __init__(
        self,
        title: str = "Wheel",
        parent: Optional[QWidget] = None,
        rotation_degrees: float = HUE_OFFSET_ANGLE,
        *,
        mode: str = "gain",
        math: str = "mult",
        zone_icon: Optional[str] = None,
        show_master_box: bool = True,
    ) -> None:
        super().__init__(parent)
        self.title = title
        self.mode = mode
        # math: "mult" = 乘性色轮（lift/gamma/gain，显示乘率-中性值）；
        #       "add"  = 加性轮（offset / log 四轮，显示偏移×100）。
        self.math = math
        self._add_display_scale = 1.0
        self._add_display_base = 0.0
        self._measured_response = False
        self._master_rate = master_drag_rate(mode)
        self.show_master_box = show_master_box and math == "mult"
        # A wheel's physical orientation is a calibration value, not a
        # rendering constant.  Keep it on each wheel so preferences can be
        # applied live without changing the grading data itself.
        self.rotation_degrees = float(rotation_degrees) % 360.0

        # === 标题行 + 色轮画布 + 数值框 + 滚花拨轮 ===
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(2, 0, 2, 0)
        outer_layout.setSpacing(2)

        # --- 标题行：影调图标 + 居中标题 + 重置按钮 ---
        header = QHBoxLayout()
        header.setContentsMargins(2, 2, 2, 0)
        self.zone_icon = ZoneIconWidget(zone_icon)
        header.addWidget(self.zone_icon)
        header.addStretch(1)
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet(SECTION_TITLE_STYLE)
        header.addWidget(self.title_label)
        header.addStretch(1)
        self.reset_btn = QToolButton(self)
        style_reset_button(self.reset_btn)
        self.reset_btn.setToolTip(I18N.tr("grading.reset_wheel", "重置") + " " + title)
        self.reset_btn.clicked.connect(self.reset)
        header.addWidget(self.reset_btn)
        outer_layout.addLayout(header)

        # --- 色轮画布区域（自绘，stretch=1 优先占用可用高度） ---
        self._wheel_canvas = _WheelCanvas(self)
        outer_layout.addWidget(self._wheel_canvas, 1)

        # --- 数值框行：乘性轮 YRGB 4 框；加性轮 RGB 3 框 ---
        val_row = QHBoxLayout()
        val_row.setContentsMargins(0, 0, 0, 0)
        val_row.setSpacing(1)

        if self.math == "add":
            neutral = 0.0
            # 加性轮无乘率显示偏移
            self._display_offset = 0.0
            box_width = 52
            box_span = 30.0
            self._bright_drag = None
            self._r_drag = _GradingValue(_OFFSET_DISPLAY_MIN,
                                         _OFFSET_DISPLAY_MAX, neutral,
                                         accent="#f1383d", drag_span=box_span)
            self._g_drag = _GradingValue(_OFFSET_DISPLAY_MIN,
                                         _OFFSET_DISPLAY_MAX, neutral,
                                         accent="#24d958", drag_span=box_span)
            self._b_drag = _GradingValue(_OFFSET_DISPLAY_MIN,
                                         _OFFSET_DISPLAY_MAX, neutral,
                                         accent="#3476ff", drag_span=box_span)
            boxes = (self._r_drag, self._g_drag, self._b_drag)
            for control, channel in zip(boxes, ("R", "G", "B")):
                control.setToolTip(channel)
                control.setFixedValueWidth(box_width)
        else:
            neutral = 1.0 if mode == "gain" else 0.0
            # 显示偏移 ≠ 中性值：lift/gamma 显示"乘率 − 1"（中性 0.00），
            # gain 显示绝对乘率（中性 1.00）——与既有显示单位契约一致。
            self._display_offset = 0.0 if mode == "gain" else 1.0
            self._bright_drag = _GradingValue(neutral - 1.0 + 2.0 ** -6.0,
                                              neutral + 63.0, neutral,
                                              accent="#c7c7c9", drag_span=2.0)
            self._bright_drag.setToolTip(I18N.tr("grading.wheel_master", "主亮度"))
            self._r_drag = _GradingValue(neutral - 1.0 if mode != "gain" else 0.0,
                                         neutral + 191.0, neutral,
                                         accent="#f1383d", drag_span=2.0)
            self._g_drag = _GradingValue(neutral - 1.0 if mode != "gain" else 0.0,
                                         neutral + 191.0, neutral,
                                         accent="#24d958", drag_span=2.0)
            self._b_drag = _GradingValue(neutral - 1.0 if mode != "gain" else 0.0,
                                         neutral + 191.0, neutral,
                                         accent="#3476ff", drag_span=2.0)
            boxes = (self._bright_drag, self._r_drag, self._g_drag, self._b_drag)
            for control, channel in zip(boxes[1:], ("R", "G", "B")):
                control.setToolTip(channel)
        for control in boxes:
            val_row.addWidget(control)

        # 数值组与拨轮共用宽度，左右边缘随布局一起对齐。
        val_container = QWidget()
        val_container.setLayout(val_row)
        value_width = val_row.minimumSize().width()
        val_container.setFixedWidth(value_width)
        outer_layout.addWidget(val_container, alignment=Qt.AlignmentFlag.AlignHCenter)

        # --- 固定宽度的主亮度拨轮，与色盘和通道数值居中对齐 ---
        self.master_jog = KnurledJogWheel(value_width)
        self.master_jog.setToolTip(I18N.tr("grading.brightness_dial_tooltip",
            "Drag left/right to adjust master brightness; double-click to reset. Shift: fine."))
        self.master_jog.setAccessibleName(title + " " +
                                          I18N.tr("grading.wheel_master", "Master"))
        jog_row = QHBoxLayout()
        jog_row.setContentsMargins(0, 7, 0, 2)
        jog_row.addWidget(self.master_jog, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer_layout.addLayout(jog_row)

        # Internal State
        self.hue: float = 0.0      # 0 to 360
        self.sat: float = 0.0      # 0 to 1.0
        self.brightness: float = 0.0  # -1.0 to 1.0
        self._updating_brightness = False
        self._updating_rgb = False

        self.knob_pos = QPointF(0, 0)  # Relative to center, normalized -1 to 1
        self.dragging_knob: bool = False

        # === 信号节流机制 ===
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(16)  # ~60FPS
        self._update_timer.timeout.connect(self._emit_changed_signal)

        self._pending_hue: Optional[float] = None
        self._pending_sat: Optional[float] = None
        self._pending_val: Optional[float] = None

        # 转发画布的交互信号
        self._wheel_canvas.interaction_started.connect(self.interaction_started.emit)

        # 亮度 + RGB 输入框变化 → 更新色轮
        if self._bright_drag is not None:
            self._bright_drag.valueChanged.connect(self._on_brightness_drag_changed)
        self.master_jog.deltaMoved.connect(self._on_jog_delta)
        self.master_jog.dragStarted.connect(self.interaction_started.emit)
        self.master_jog.dragEnded.connect(self._on_value_interaction_ended)
        self.master_jog.resetRequested.connect(self._on_master_reset)
        self._r_drag.valueChanged.connect(self._on_rgb_changed)
        self._g_drag.valueChanged.connect(self._on_rgb_changed)
        self._b_drag.valueChanged.connect(self._on_rgb_changed)
        for control in boxes:
            control.interaction_started.connect(self.interaction_started.emit)
            control.interaction_ended.connect(self._on_value_interaction_ended)

        # 初始同步 RGB 显示
        self._update_rgb_values()

    # === 代理方法：保持与 GradingPanel 的兼容接口 ===

    def paintEvent(self, event) -> None:
        """不绘制任何内容 — 由 _wheel_canvas 子控件负责。"""
        pass

    def reset(self) -> None:
        # A previous drag/value edit may still be waiting in the 60 FPS
        # coalescing timer.  If it is allowed to fire after reset, it restores
        # the old color immediately and makes the reset button appear broken.
        self._cancel_pending()
        self.dragging_knob = False
        self._wheel_canvas.dragging_knob = False
        self._wheel_canvas.dragging_ring = False

        # 单轮重置是一次完整交互：started 在变更落地前发出，撤销栈才能
        # 记住重置前的色轮状态（此前只发 ended，重置无法 Ctrl+Z）。
        if (self.hue, self.sat, self.brightness) != (0.0, 0.0, 0.0):
            self.interaction_started.emit()

        self.hue = 0.0
        self.sat = 0.0
        self.brightness = 0.0
        self.knob_pos = QPointF(0, 0)
        self._update_rgb_values()
        self._wheel_canvas.update()
        self.changed.emit(0.0, 0.0, 0.0)
        # A single-wheel reset is a completed adjustment, not a continuing
        # drag.  This also lets the ColorStack integration persist it once.
        self.interaction_ended.emit()

    def reset_wheel_only(self) -> None:
        """仅重置轮盘色度（hue/sat → 中性），保留 Master 亮度。

        对应新 UI 的双击 / 右键语义；一次完整交互（可撤销）。
        """
        if (self.hue, self.sat) == (0.0, 0.0) or self.dragging_knob:
            return
        self._cancel_pending()
        self.interaction_started.emit()
        master = self._master_value()
        self.hue = 0.0
        self.sat = 0.0
        if self._measured_response:
            self.brightness = (max(-1.0, min(1.0, master)) if self.math == "add"
                               else multiplier_brightness(master))
        self.knob_pos = QPointF(0, 0)
        self._queue_change()
        self._on_value_interaction_ended()

    def _on_master_reset(self) -> None:
        """拨轮中键 / 双击：仅 Master 亮度归零（保留 hue/sat）。"""
        neutral = 0.0 if self.math == "add" else 1.0
        if abs(self._master_value() - neutral) < 1e-12 or self.dragging_knob:
            return
        self.interaction_started.emit()
        self._change_master(neutral)
        # 先 flush 在途 pending 再结束手势：changed 必须先于 ended，
        # 撤销快照才能拿到归零后的值。
        self._on_value_interaction_ended()

    def set_hsv(self, hue: float, sat: float, brightness: float) -> None:
        """从后端引擎设置 Hue/Sat/Brightness 值，更新 UI 控制点位置"""
        # 程序化回写（undo/关键帧/切图层/Tangent）必须取消在途的节流
        # pending：否则 16ms 后定时器用过期 hue/sat 发 changed，把引擎
        # 刚回写的值又覆盖回旧值（与 reset() 的防护理应一致）。
        self._cancel_pending()

        self.hue = hue
        self.sat = sat
        self.brightness = brightness

        self._update_knob_position()

        # 同步主亮度和 RGB 输入框（防递归）
        self._update_rgb_values()

        self._wheel_canvas.update()

    def set_rotation_degrees(self, rotation_degrees: float) -> None:
        """Set the physical/display calibration angle and repaint the wheel."""

        try:
            rotation = float(rotation_degrees)
        except (TypeError, ValueError):
            rotation = HUE_OFFSET_ANGLE
        self.rotation_degrees = rotation % 360.0
        self._update_knob_position()
        self._wheel_canvas.update()

    def _screen_angle_for_hue(self, hue: float) -> float:
        """Convert a grading hue to the calibrated screen angle."""

        return (float(hue) + self.rotation_degrees) % 360.0

    def _update_knob_position(self) -> None:
        if self._measured_response:
            x, y = wheel_position(self.mode, self._wheel_rgb())
            self.knob_pos = self._rotate_pointer(QPointF(x, y), inverse=False)
            return
        screen_angle = math.radians(self._screen_angle_for_hue(self.hue))
        self.knob_pos = QPointF(
            self.sat * math.cos(screen_angle),
            -self.sat * math.sin(screen_angle),
        )

    def _rotate_pointer(self, point: QPointF, *, inverse: bool) -> QPointF:
        angle = math.radians(HUE_OFFSET_ANGLE - self.rotation_degrees)
        if inverse:
            angle = -angle
        c, s = math.cos(angle), math.sin(angle)
        return QPointF(c * point.x() - s * point.y(), s * point.x() + c * point.y())

    def _edit_knob(self, point: QPointF) -> None:
        if self._measured_response:
            reference = self._rotate_pointer(point, inverse=True)
            chroma = wheel_chroma(self.mode, reference.x(), reference.y())
            rgb = self._wheel_rgb()
            master = wheel_luma(rgb)
            target = tuple(master + value for value in chroma)
            new_rgb = constrain_chroma(rgb, target, additive=self.math == "add")
            if new_rgb == rgb:
                return
            self._assign_rgb(new_rgb)
            self._update_knob_position()
        else:
            if self.knob_pos == point:
                return
            self.knob_pos = point
            self.sat = min(1.0, math.hypot(point.x(), point.y()))
            if self.sat > 1e-8:
                self.hue = (math.degrees(math.atan2(-point.y(), point.x()))
                            - self.rotation_degrees) % 360.0
        self._queue_change()

    def set_measured_response(self, enabled: bool) -> None:
        self._measured_response = enabled
        rate = channel_drag_rate(self.mode) if enabled else None
        for field in (self._bright_drag, self._r_drag, self._g_drag, self._b_drag):
            if field is not None:
                field.set_drag_rate(rate)
        self._update_knob_position()
        self._update_rgb_values()
        self._wheel_canvas.update()

    def blockSignals(self, block: bool) -> bool:
        self.master_jog.blockSignals(block)
        if self._bright_drag is not None:
            self._bright_drag.blockSignals(block)
        self._r_drag.blockSignals(block)
        self._g_drag.blockSignals(block)
        self._b_drag.blockSignals(block)
        return super().blockSignals(block)

    def _cancel_pending(self) -> None:
        if self._update_timer.isActive():
            self._update_timer.stop()
        self._pending_hue = None
        self._pending_sat = None
        self._pending_val = None

    def _set_brightness(self, brightness: float) -> None:
        """外环 / 拨轮路径：直接设置 Master 亮度并合并高频信号。"""
        brightness = max(-1.0, min(1.0, brightness))
        if self._updating_brightness or brightness == self.brightness:
            return
        self.brightness = brightness
        self._queue_change()

    def _queue_change(self) -> None:
        self._pending_hue = self.hue
        self._pending_sat = self.sat
        self._pending_val = self.brightness
        self._update_rgb_values()
        self._wheel_canvas.update()
        if not self._update_timer.isActive():
            self._update_timer.start()

    def _on_jog_delta(self, dx: float) -> None:
        if dx and not self._updating_brightness:
            self._change_master(self._master_value() + dx * self._master_rate)

    def _drag_master_ring(self, angle: float, radius: float) -> None:
        if self._measured_response:
            self._on_jog_delta(angle * radius)
        else:
            self._set_brightness(self.brightness + angle * self._RING_SENSITIVITY)

    def _on_brightness_drag_changed(self, val: float) -> None:
        """亮度数值变化，更新状态并合并高频信号。"""
        if not self._updating_brightness and self.math != "add":
            self._change_master(val + self._display_offset)

    def _change_master(self, value: float) -> None:
        if self._measured_response:
            rgb = self._wheel_rgb()
            new_rgb = shift_master(rgb, value - wheel_luma(rgb), additive=self.math == "add")
            if new_rgb != rgb:
                self._assign_rgb(new_rgb)
                self._update_knob_position()
                self._queue_change()
        else:
            self._set_brightness(value if self.math == "add" else multiplier_brightness(value))

    def _emit_changed_signal(self) -> None:
        if self._pending_hue is not None:
            self.changed.emit(self._pending_hue, self._pending_sat, self._pending_val)
            self._pending_hue = None
            self._pending_sat = None
            self._pending_val = None

    def _on_value_interaction_ended(self) -> None:
        """Flush value-box changes before ending the wheel gesture."""
        self.flush_pending_changes()
        self.interaction_ended.emit()

    def flush_pending_changes(self) -> None:
        """Publish the last wheel value before a host snapshots the grade."""
        if self._update_timer.isActive():
            self._update_timer.stop()
            self._emit_changed_signal()

    @staticmethod
    def rgb_multipliers(hue: float, sat: float, brightness: float) -> RGB:
        """Forward HSV→RGB multiplier math shared by wheels and bars."""
        r, g, b = params_to_rgb(hue, sat, brightness)
        return r, g, b

    def _wheel_rgb(self) -> RGB:
        if self.math == "add":
            r, g, b = params_to_offset_rgb(self.hue, self.sat, self.brightness)
            return r, g, b
        return self.rgb_multipliers(self.hue, self.sat, self.brightness)

    def _master_value(self, rgb: RGB | None = None) -> float:
        if self._measured_response:
            return wheel_luma(self._wheel_rgb() if rgb is None else rgb)
        return self.brightness if self.math == "add" else brightness_multiplier(self.brightness)

    def _master_arc_position(self) -> float:
        if not self._measured_response:
            return max(-1.0, min(1.0, self.brightness))
        master = self._master_value()
        return (max(-1.0, min(1.0, master)) if self.math == "add"
                else multiplier_brightness(master))

    def _assign_rgb(self, rgb: RGB) -> None:
        if self.math == "add":
            self.hue, self.sat, self.brightness = offset_display_to_hsb(
                *(value * 100.0 for value in rgb))
        else:
            self.hue, self.sat, self.brightness = self._rgb_to_hsv(
                *(max(0.0, value) for value in rgb))

    def _update_rgb_values(self) -> None:
        """Synchronize the controls with the stored grade without emitting edits."""
        self._updating_rgb = True
        self._updating_brightness = True
        rgb = self._wheel_rgb()
        master = self._master_value(rgb)
        fields = self._r_drag, self._g_drag, self._b_drag
        if self.math == "add":
            for field, value in zip(fields, rgb):
                field.setValue(value * 100.0 * self._add_display_scale + self._add_display_base)
        else:
            if self._bright_drag is not None:
                self._bright_drag.setValue(master - self._display_offset)
            for field, value in zip(fields, rgb):
                field.setValue(value - self._display_offset)
        self.master_jog.set_master_value(master - (0.0 if self.math == "add" else 1.0),
                                         self._master_rate)
        self._updating_rgb = False
        self._updating_brightness = False

    def _on_rgb_changed(self, _val: float) -> None:
        """Invert the RGB numeric edit and position the control point."""
        if self._updating_rgb:
            return
        fields = self._r_drag, self._g_drag, self._b_drag
        if self.math == "add":
            self._assign_rgb(tuple((field.value() - self._add_display_base)
                                   / (100.0 * self._add_display_scale) for field in fields))
        else:
            self._assign_rgb(tuple(field.value() + self._display_offset for field in fields))
        self._update_knob_position()
        self._queue_change()

    def set_additive_display_units(self, scale: float, base: float) -> None:
        """Use calibrated numeric units without changing the stored wheel position."""
        self._add_display_scale, self._add_display_base = scale, base
        self._updating_rgb = True
        for field in (self._r_drag, self._g_drag, self._b_drag):
            field.setRange(_OFFSET_DISPLAY_MIN * scale + base, _OFFSET_DISPLAY_MAX * scale + base)
            field.setDefaultValue(base)
        self._update_rgb_values()

    @staticmethod
    def _rgb_to_hsv(r: float, g: float, b: float) -> tuple[float, float, float]:
        """Invert the stored RGB factors without quantizing small drag steps."""
        hue, sat, _ = colorsys.rgb_to_hsv(r, g, b)
        return hue * 360.0, sat, multiplier_brightness((r + g + b) / 3.0)


# ============================================================================
# 模式切换图标按钮（色轮 / 色条 / Log / 色调饱和度）
# ============================================================================

class ModeTabButton(QPushButton):
    """Draw the mode glyph using the shared title-row icon states."""

    def __init__(self, mode_key: str, tooltip: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.mode_key = mode_key
        self.setCheckable(True)
        style_header_button(self, width=36)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        col = header_button_color(self)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        painter.setPen(QPen(col, 1.25))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        if self.mode_key == "wheels":
            painter.drawEllipse(QPointF(cx, cy), 6.25, 6.25)
            painter.setBrush(col)
            painter.drawEllipse(QPointF(cx, cy), 1.3, 1.3)
        elif self.mode_key == "bars":
            for idx, dx in enumerate((-5.0, -1.7, 1.7, 5.0)):
                painter.drawLine(QPointF(cx + dx, cy - 5.5),
                                 QPointF(cx + dx, cy + 5.5))
                notch_y = cy + (-2.0 if idx % 2 == 0 else 2.0)
                painter.setPen(QPen(QColor("#17191c"), 2.0))
                painter.drawLine(QPointF(cx + dx - 1.0, notch_y),
                                 QPointF(cx + dx + 1.0, notch_y))
                painter.setPen(QPen(col, 1.25))
        elif self.mode_key == "log":
            painter.drawArc(QRectF(cx - 6.0, cy - 6.0, 10.0, 10.0),
                            30 * 16, 260 * 16)
            painter.setBrush(col)
            painter.drawEllipse(QPointF(cx - 1.5, cy - 1.0), 1.1, 1.1)
            font = painter.font()
            font.setPixelSize(5)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRectF(cx - 4.0, cy + 1.0, 11.0, 6.0),
                             Qt.AlignmentFlag.AlignCenter, "LOG")
        elif self.mode_key in ("dim_light", "dark_bright"):
            # Inner tonal ranges versus the two ends of the tonal scale.
            extreme = self.mode_key == "dark_bright"
            painter.drawLine(QPointF(cx - 7.5, cy + 4.5), QPointF(cx + 7.5, cy + 4.5))
            spread = 6.5 if extreme else 3.5
            for dx, height in ((-spread, 9.0 if extreme else 5.0),
                               (0.0, 2.5 if extreme else 8.0),
                               (spread, 9.0 if extreme else 5.0)):
                painter.drawLine(QPointF(cx + dx, cy + 4.5 - height), QPointF(cx + dx, cy + 4.5))
        else:  # tonal：三条影调带（暗/中/亮）
            painter.setPen(QPen(col, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for dy in (-4.5, 0.0, 4.5):
                painter.drawLine(QPointF(cx - 5.0, cy + dy),
                                 QPointF(cx + 5.0, cy + dy))
        painter.end()


def _v_divider() -> QFrame:
    """列间 1px 分割线（与 dock 分割线同色）。"""
    line = QFrame()
    line.setFixedWidth(1)
    line.setStyleSheet(f"background-color: {C.PANEL_DIVIDER}; border: none;")
    return line


def _h_divider() -> QFrame:
    """功能区之间的 1px 横向黑色细线（对齐设计稿的功能分区）。"""
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background-color: {C.PANEL_DIVIDER}; border: none;")
    return line


# ============================================================================
# 完整调色面板（色轮 / Color Bars / Log 色轮 / 色调饱和度 / Base Grade）
# ============================================================================

class GradingPanel(QWidget):
    """完整调色面板 - 四轮与上下两排数值参数。

    Features:
    - 主色轮、色条和 Log 色轮都在四列放得下时显示 OFFSET
    - Pivot（对比度枢轴）取代曝光位置；曝光改为内部透传状态（引擎/
      自动平衡仍可写入，UI 不再直接编辑）
    - Lum Mix / Log Low/High Range 等扩展参数经
      ``grading_extended_changed(dict)`` 下发
    - 全局重置按钮

    Signals:
        grading_changed: 基础 13 参改变时发射（契约不变）
        grading_extended_changed: 扩展参数 dict 改变时发射
        interaction_started: 交互开始时发射
        interaction_ended: 交互结束时发射
        reset_clicked: 重置按钮点击时发射
    """
    # Emits LGG, exposure, temp, tint, contrast, shadow, saturation,
    # vibrance, mid_detail, hue_shift, highlights; stored values stay unchanged.
    grading_changed = pyqtSignal(tuple, tuple, tuple, float, float, float, float, float, float, float, float, float, float)
    # 一级校色扩展参数（Offset 轮 / pivot / Log 四轮 / Lum Mix）
    grading_extended_changed = pyqtSignal(dict)
    # 色调区间饱和度（TonalSaturationPanel 转发）
    tonal_saturation_changed = pyqtSignal(float, float, float)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    reset_clicked = pyqtSignal()
    # P1-COLOR-03: 关键帧模式开关 / 当前帧快照 / 删除当前帧关键帧
    keyframe_mode_changed = pyqtSignal(bool)
    keyframe_snapshot_requested = pyqtSignal()
    keyframe_remove_requested = pyqtSignal()

    # Primary edits emit the original complete parameter group. Base Grade
    # emits its own changed keys through the same controller entry point.
    _EXTENDED_KEYS = tuple(key for key in DEFAULT_EXTENDED_GRADING_PARAMS
                           if key not in BASE_GRADE_DEFAULTS)

    def __init__(self, parent: Optional[QWidget] = None, *,
                 use_measured_primaries: Callable[[], bool] | None = None) -> None:
        super().__init__(parent)
        self._use_measured_primaries = use_measured_primaries
        self._calibration_version = 0
        self._display_calibration_version = 0
        self._ungraded = True
        self._stored_log_offset = (0.0, 0.0, 0.0)

        # 节流定时器：限制信号发射频率为 60FPS (16ms)
        self._grading_update_timer = QTimer(self)
        self._grading_update_timer.setSingleShot(True)
        self._grading_update_timer.setInterval(16)
        self._grading_update_timer.timeout.connect(self._emit_grading_changed)

        self._extended_update_timer = QTimer(self)
        self._extended_update_timer.setSingleShot(True)
        self._extended_update_timer.setInterval(16)
        self._extended_update_timer.timeout.connect(self._emit_grading_extended)

        self._syncing_ui = False

        # P9.1: Color stack tracking
        self._current_stack_id: str | None = None
        self._save_callback: Any = None  # Callable[(dict[str, Any],), None]

        # 曝光：UI 不再直接暴露（位置由 Pivot 取代），作为内部透传状态
        # 保持 grading_changed 13 参契约（自动平衡等引擎路径仍可写入）。
        self._exposure_value: float = 0.0

        self._init_ui()
        self.refresh_primary_calibration()

    def _init_ui(self) -> None:
        from src.features.color.ui.base_grade_panel import BaseGradePanel
        from src.features.color.ui.color_bars_panel import ColorBarsPanel

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, PARAMETER_BAR_BOTTOM_MARGIN)
        layout.setSpacing(0)
        self.setLayout(layout)

        # ── 0. 顶部导航行：关键帧按钮 + 模式标题/切换 + 全局重置 ──────
        header = create_panel_header()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 0, 10, 0)
        header_layout.setSpacing(4)

        # P1-COLOR-03: 关键帧控制（录制模式 / 当前帧快照 / 删除当前帧关键帧）
        self.keyframe_mode_btn = QPushButton()
        self.keyframe_mode_btn.setCheckable(True)
        style_header_button(self.keyframe_mode_btn, width=24, icon=icon_circle())
        self.keyframe_mode_btn.setToolTip(I18N.tr(
            "grading.keyframe_mode_tooltip",
            "关键帧模式：开启后，调色参数修改会在当前帧记录关键帧"))
        self.keyframe_mode_btn.toggled.connect(self.keyframe_mode_changed.emit)

        self.kf_snapshot_btn = QPushButton()
        style_header_button(self.kf_snapshot_btn, width=24, icon=icon_add())
        self.kf_snapshot_btn.setToolTip(I18N.tr(
            "grading.keyframe_snapshot_tooltip",
            "在当前帧为所有已动画参数记录关键帧"))
        self.kf_snapshot_btn.clicked.connect(
            self.keyframe_snapshot_requested.emit)

        self.kf_remove_btn = QPushButton()
        style_header_button(self.kf_remove_btn, width=24, icon=icon_delete())
        self.kf_remove_btn.setToolTip(I18N.tr(
            "grading.keyframe_remove_tooltip",
            "删除当前帧的所有调色关键帧"))
        self.kf_remove_btn.clicked.connect(
            self.keyframe_remove_requested.emit)

        self.mode_title = QLabel(I18N.tr("panel.color_wheel", "色轮"))
        self.mode_title.setStyleSheet(SECTION_TITLE_STYLE)

        self.reset_all_btn = QPushButton()
        self.reset_all_btn.setObjectName("reset_btn")
        style_reset_button(self.reset_all_btn, global_reset=True)
        self.reset_all_btn.setToolTip(I18N.tr(
            "grading.reset_all_tooltip", "重置全部调色参数"))
        self.reset_all_btn.clicked.connect(self.reset_all)

        header_layout.addWidget(self.keyframe_mode_btn)
        header_layout.addWidget(self.kf_snapshot_btn)
        header_layout.addWidget(self.kf_remove_btn)
        header_layout.addSpacing(6)
        header_layout.addWidget(self.mode_title)
        header_layout.addStretch(1)
        self._mode_buttons: dict[str, ModeTabButton] = {}
        for m_key, m_tip in (
            ("wheels", I18N.tr("panel.color_wheel", "色轮")),
            ("bars", I18N.tr("panel.color_bars", "Color Bars")),
            ("log", I18N.tr("panel.log_wheels", "Log 色轮")),
            ("tonal", I18N.tr("panel.tonal_saturation", "色调饱和度")),
        ):
            btn = ModeTabButton(m_key, m_tip)
            btn.clicked.connect(
                lambda _checked, k=m_key: self._switch_mode(k))
            self._mode_buttons[m_key] = btn
            header_layout.addWidget(btn)
        divider = QFrame()
        divider.setFixedWidth(1)
        divider.setFixedHeight(16)
        divider.setStyleSheet(f"background-color: {C.BORDER_DARK}; border: none;")
        header_layout.addSpacing(2)
        header_layout.addWidget(divider)
        header_layout.addSpacing(2)
        header_layout.addWidget(self.reset_all_btn)
        layout.addWidget(header)
        # 功能区之间的 1px 横向黑色细线（标题行 | 顶部参数 | 轮区 | 底部
        # 参数，对齐设计稿与达芬奇的分区方式）。
        self._h_line_mid = _h_divider()
        self._h_line_bottom = _h_divider()

        # ── 1. 顶部参数条（Primaries 与 Log 两页；共享参数各自持孪生行，
        #      经 _scalar_rows 注册表双向同步） ─────────────────────────
        self._scalar_rows: dict[str, list[_PrimaryValueRow]] = {}

        # 基础键（grading_changed 13 参）与扩展键（grading_extended_changed）
        _BASE_SLIDER_KEYS = {"temp", "tint", "contrast", "mid_detail",
                             "vibrance", "shadow", "highlights", "saturation",
                             "hue_shift"}

        def _make_row(i18n_key: str, fallback: str, key: str, min_val: float,
                      max_val: float, default_val: float, *, scale: float = 1.0,
                      offset: float = 0.0, decimals: int = 2,
                      accent: str = "#919195") -> _PrimaryValueRow:
            return self._create_prim_slider(
                I18N.tr(i18n_key, fallback), min_val, max_val, default_val,
                key=key, base=key in _BASE_SLIDER_KEYS, scale=scale,
                offset=offset, decimals=decimals, accent=accent)

        # Primaries 顶排（色轮 / Color Bars 页共享）
        self.temp_slider = _make_row(
            "grading.temp", "色温", "temp", -100, 100, 0,
            accent=_ACCENT_TEMP)
        self.tint_slider = _make_row(
            "grading.tint", "色调", "tint", -100, 100, 0,
            accent=_ACCENT_TINT)
        self.contrast_slider = _make_row(
            "grading.contrast", "对比度", "contrast", -100, 100, 0,
            scale=0.01, offset=1.0, decimals=3, accent=_ACCENT_CONTRAST)
        self.pivot_slider = _make_row(
            "grading.pivot", "Pivot", "pivot", 0.05, 1.0, 0.18, decimals=3,
            accent=_ACCENT_NEUTRAL)
        self.mid_detail_slider = _make_row(
            "grading.mid_det", "中间细节", "mid_detail", -100, 100, 0,
            accent=_ACCENT_NEUTRAL)
        # Primaries 底排
        self.vibrance_slider = _make_row(
            "raw.color_boost", "Color Boost", "vibrance", -100, 100, 0,
            accent=_ACCENT_RAINBOW)
        self.shadow_slider = _make_row(
            "grading.shadow", "阴影", "shadow", -100, 100, 0,
            accent=_ACCENT_CONTRAST)
        self.highlights_slider = _make_row(
            "raw.highlights", "Highlights", "highlights", -100, 100, 0,
            accent=_ACCENT_CONTRAST)
        self.sat_slider = _make_row(
            "grading.sat", "饱和度", "saturation", -100, 100, 0,
            scale=0.5, offset=50.0, accent=_ACCENT_RAINBOW)
        self.hue_slider = _make_row(
            "grading.hue", "色相", "hue_shift", -180, 180, 0,
            scale=100.0 / 360.0, offset=50.0, accent=_ACCENT_RAINBOW)
        self.lum_mix_slider = _make_row(
            "grading.lum_mix", "Lum Mix", "lum_mix", 0, 100, 100, decimals=2,
            accent=_ACCENT_RAINBOW)
        self.lum_mix_slider.hide()
        # Log 顶排孪生行（temp/tint/contrast/pivot 与 Primaries 共享状态）
        self.log_low_range_slider = _make_row(
            "grading.log_low_range", "Low Range", "log_low_range",
            0.0, 1.0, 0.333, decimals=3, accent=_ACCENT_NEUTRAL)
        self.log_high_range_slider = _make_row(
            "grading.log_high_range", "High Range", "log_high_range",
            0.0, 1.0, 0.550, decimals=3, accent=_ACCENT_NEUTRAL)
        _log_temp = _make_row("grading.temp", "色温", "temp", -100, 100, 0,
                              accent=_ACCENT_TEMP)
        _log_tint = _make_row("grading.tint", "色调", "tint", -100, 100, 0,
                              accent=_ACCENT_TINT)
        _log_contrast = _make_row("grading.contrast", "对比度", "contrast",
                                  -100, 100, 0, scale=0.01, offset=1.0,
                                  decimals=3, accent=_ACCENT_CONTRAST)
        _log_pivot = _make_row("grading.pivot", "Pivot", "pivot", 0.05, 1.0,
                               0.18, decimals=3, accent=_ACCENT_NEUTRAL)
        # Log 底排孪生行
        _log_mid_detail = _make_row("grading.mid_det", "中间细节",
                                    "mid_detail", -100, 100, 0,
                                    accent=_ACCENT_NEUTRAL)
        _log_vibrance = _make_row("raw.color_boost", "Color Boost", "vibrance",
                                  -100, 100, 0, accent=_ACCENT_RAINBOW)
        _log_shadow = _make_row("grading.shadow", "阴影", "shadow", -100, 100, 0,
                                accent=_ACCENT_CONTRAST)
        _log_highlights = _make_row("raw.highlights", "Highlights",
                                    "highlights", -100, 100, 0,
                                    accent=_ACCENT_CONTRAST)
        _log_sat = _make_row("grading.sat", "饱和度", "saturation",
                             -100, 100, 0, scale=0.5, offset=50.0,
                             accent=_ACCENT_RAINBOW)
        _log_hue = _make_row("grading.hue", "色相", "hue_shift", -180, 180, 0,
                             scale=100.0 / 360.0, offset=50.0,
                             accent=_ACCENT_RAINBOW)

        def _param_bar(rows: list[_PrimaryValueRow]) -> QWidget:
            bar = QWidget()
            # 横向 Ignored：参数行不抬面板最小宽度——否则底排 6 行会把
            # 面板最小宽度顶到 ~660px 以上，而色轮半径封顶 100px 恰好要
            # 面板 <660px 才开始缩，拖分割线就全程缩不动了。行内空间
            # 不足时优先收缩 stretch、再裁切 label（色轮优先缩小）。
            bar.setSizePolicy(QSizePolicy.Policy.Ignored,
                              QSizePolicy.Policy.Preferred)
            bar_layout = QHBoxLayout(bar)
            bar_layout.setContentsMargins(10, 0, 10, 0)
            bar_layout.setSpacing(2)
            for row in rows:
                bar_layout.addWidget(row)
            return bar

        self.top_bar_stack = QStackedWidget()
        self.top_bar_stack.setFixedHeight(PARAMETER_BAR_HEIGHT)
        # 页 0：Primaries（色轮 / Color Bars 模式共享）
        self.top_bar_stack.addWidget(_param_bar([
            self.temp_slider, self.tint_slider, self.contrast_slider,
            self.pivot_slider, self.mid_detail_slider,
        ]))
        # 页 1：Log 模式（Low/High Range 取代中间细节）
        self.top_bar_stack.addWidget(_param_bar([
            _log_temp, _log_tint, _log_contrast, _log_pivot,
            self.log_low_range_slider, self.log_high_range_slider,
        ]))

        self.bottom_bar_stack = QStackedWidget()
        self.bottom_bar_stack.setFixedHeight(PARAMETER_BAR_HEIGHT)
        # 页 0：Primaries（Lum Mix 补入底排）
        self.bottom_bar_stack.addWidget(_param_bar([
            self.vibrance_slider, self.shadow_slider, self.highlights_slider,
            self.sat_slider, self.hue_slider, self.lum_mix_slider,
        ]))
        # 页 1：Log 模式（中间细节并入底排）
        self.bottom_bar_stack.addWidget(_param_bar([
            _log_mid_detail, _log_vibrance, _log_shadow, _log_highlights,
            _log_sat, _log_hue,
        ]))
        # 布局顺序沿用既有面板：色温/色调/对比度等在面板顶部，
        # Color Boost/阴影/高光/饱和度/色相/Lum Mix 在面板底部。
        layout.addWidget(self.top_bar_stack)
        layout.addWidget(self._h_line_mid)

        # ── 2. 中央模式栈 ─────────────────────────────────────────────
        self.mode_stack = QStackedWidget()

        # 页 0：Primaries 色轮（Offset 在面板足够宽时加入同一排）。
        self._wheel_page = QWidget()
        wheels_layout = QHBoxLayout(self._wheel_page)
        wheels_layout.setContentsMargins(10, 0, 10, 0)
        wheels_layout.setSpacing(12)
        self.lift_wheel = ColorWheel(I18N.tr("grading.lift", "暗部"),
                                     mode="lift", zone_icon="lift")
        self.gamma_wheel = ColorWheel(I18N.tr("grading.gamma", "中灰"),
                                      mode="gamma", zone_icon="gamma")
        self.gain_wheel = ColorWheel(I18N.tr("grading.gain", "亮部"),
                                     mode="gain", zone_icon="gain")
        self.offset_wheel = ColorWheel(
            I18N.tr("grading.offset", "偏移"), mode="offset", math="add",
            zone_icon="offset", show_master_box=False)
        for wheel in (self.lift_wheel, self.gamma_wheel, self.gain_wheel,
                      self.offset_wheel):
            wheel.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Expanding)
            wheels_layout.addWidget(wheel, 1)
        self.mode_stack.addWidget(self._wheel_page)

        # 页 1：Color Bars（含 Offset 列）
        self._bars_page = QWidget()
        bars_page_layout = QVBoxLayout(self._bars_page)
        bars_page_layout.setContentsMargins(0, 0, 0, 0)
        bars_page_layout.setSpacing(0)
        self.color_bars_panel = ColorBarsPanel()
        bars_page_layout.addWidget(self.color_bars_panel, 1)
        self.mode_stack.addWidget(self._bars_page)

        # 页 2：Log 色轮（Shadow / Midtone / Highlight / Offset，RGB 三框）。
        self._log_page = QWidget()
        log_layout = QHBoxLayout(self._log_page)
        log_layout.setContentsMargins(10, 0, 10, 0)
        log_layout.setSpacing(12)
        self.log_shadow_wheel = ColorWheel(
            I18N.tr("grading.log_shadow", "Shadow"), mode="log_shadow",
            math="add", zone_icon=None, show_master_box=False)
        self.log_midtone_wheel = ColorWheel(
            I18N.tr("grading.log_midtone", "Midtone"), mode="log_midtone",
            math="add", zone_icon=None, show_master_box=False)
        self.log_highlight_wheel = ColorWheel(
            I18N.tr("grading.log_highlight", "Highlight"), mode="log_highlight",
            math="add", zone_icon=None, show_master_box=False)
        self.log_offset_wheel = ColorWheel(
            I18N.tr("grading.log_offset", "Offset"), mode="log_offset",
            math="add", zone_icon="offset", show_master_box=False)
        for wheel in (self.log_shadow_wheel, self.log_midtone_wheel,
                      self.log_highlight_wheel, self.log_offset_wheel):
            wheel.setSizePolicy(QSizePolicy.Policy.Expanding,
                                QSizePolicy.Policy.Expanding)
            log_layout.addWidget(wheel, 1)
        self.mode_stack.addWidget(self._log_page)

        # 用三个四列布局中最大的最小宽度作为共同门槛。
        offset_rows = (
            (self._wheel_page, self.offset_wheel),
            (self.color_bars_panel, self.color_bars_panel.offset),
            (self._log_page, self.log_offset_wheel),
        )
        self._offset_columns = tuple(column for _, column in offset_rows)
        self._offset_show_width = max(row.minimumSizeHint().width() for row, _ in offset_rows)
        for row, column in offset_rows:
            column.hide()
            # 始终沿用三列布局的下限，保持分隔条可一次拖窄。
            row.setMinimumWidth(row.minimumSizeHint().width())
            row.setSizePolicy(QSizePolicy.Policy.Ignored, row.sizePolicy().verticalPolicy())
        self.mode_stack.installEventFilter(self)

        # 页 3：色调区间饱和度（独立面板，复用原实现）
        self.tonal_saturation_panel = TonalSaturationPanel()
        self.tonal_saturation_panel.tonal_saturation_changed.connect(
            self.tonal_saturation_changed.emit)
        self.tonal_saturation_panel.interaction_started.connect(
            self.interaction_started.emit)
        self.tonal_saturation_panel.interaction_ended.connect(
            self.interaction_ended.emit)
        self.mode_stack.addWidget(self.tonal_saturation_panel)

        # The app docks this widget independently. Keep one parameter and
        # interaction facade for both primary-grading tools.
        self.base_grade_panel = BaseGradePanel()
        self.base_grade_panel.changed.connect(self.grading_extended_changed.emit)
        self.base_grade_panel.interaction_started.connect(self.interaction_started.emit)
        self.base_grade_panel.interaction_ended.connect(self.interaction_ended.emit)

        mode_layout = QHBoxLayout()
        mode_layout.setContentsMargins(6, 0, 6, 0)
        mode_layout.addWidget(self.mode_stack)
        layout.addLayout(mode_layout, 1)
        layout.addWidget(self._h_line_bottom)
        # 底排参数（Color Boost / 阴影 / 高光 / 饱和度 / 色相 / Lum Mix）
        layout.addWidget(self.bottom_bar_stack)

        # ── 3. 信号连接 ───────────────────────────────────────────────
        # 乘性三轮 → 基础 13 参；加性轮/扩展滑条 → 扩展 dict。
        for wheel in (self.lift_wheel, self.gamma_wheel, self.gain_wheel):
            wheel.changed.connect(self._on_param_changed)
        for wheel in (self.log_shadow_wheel, self.log_midtone_wheel, self.log_highlight_wheel):
            wheel.changed.connect(self._on_extended_param_changed)
        for wheel in (self.offset_wheel, self.log_offset_wheel):
            wheel.changed.connect(lambda *_, source=wheel: self._on_offset_changed(source))

        # 色轮 ↔ Color Bars 双向同步：同一组 (hue, sat, brightness)。
        for wheel, column in (
                (self.lift_wheel, self.color_bars_panel.lift),
                (self.gamma_wheel, self.color_bars_panel.gamma),
                (self.gain_wheel, self.color_bars_panel.gain),
                (self.offset_wheel, self.color_bars_panel.offset)):
            wheel.changed.connect(
                lambda h, s, b, c=column: c.set_hsv(h, s, b))
            column.changed.connect(
                lambda h, s, b, w=wheel: self._on_bars_changed(w, h, s, b))
        self.color_bars_panel.interaction_started.connect(
            self.interaction_started.emit)
        self.color_bars_panel.interaction_ended.connect(
            self.interaction_ended.emit)

        for wheel in (self.lift_wheel, self.gamma_wheel, self.gain_wheel,
                      self.offset_wheel, self.log_shadow_wheel,
                      self.log_midtone_wheel, self.log_highlight_wheel,
                      self.log_offset_wheel):
            wheel.interaction_started.connect(self.interaction_started.emit)
            wheel.interaction_ended.connect(self.interaction_ended.emit)

        self._switch_mode("wheels")

    # ==================== 模式切换 ====================

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.mode_stack and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            show_offset = self.mode_stack.contentsRect().width() >= self._offset_show_width
            for column in self._offset_columns:
                column.setVisible(show_offset)
            self.lum_mix_slider.setVisible(show_offset)
        return super().eventFilter(watched, event)

    def _switch_mode(self, mode: str) -> None:
        """Switch the primary tool page and synchronize the header icons."""
        page_index = {"wheels": MODE_WHEELS, "bars": MODE_BARS,
                      "log": MODE_LOG, "tonal": MODE_TONAL}[mode]
        for key, btn in self._mode_buttons.items():
            was_blocked = btn.blockSignals(True)
            btn.setChecked(key == mode)
            btn.blockSignals(was_blocked)
            btn.update()
        self.mode_stack.setCurrentIndex(page_index)
        if page_index in (MODE_WHEELS, MODE_BARS):
            self.mode_title.setText(I18N.tr("panel.color_wheel", "色轮")
                                    if page_index == MODE_WHEELS
                                    else I18N.tr("panel.color_bars", "Color Bars"))
            self.top_bar_stack.setCurrentIndex(0)
            self.bottom_bar_stack.setCurrentIndex(0)
            self.top_bar_stack.setVisible(True)
            self.bottom_bar_stack.setVisible(True)
            self._h_line_mid.setVisible(True)
            self._h_line_bottom.setVisible(True)
        elif page_index == MODE_LOG:
            self.mode_title.setText(I18N.tr("panel.log_wheels", "Log 色轮"))
            self.top_bar_stack.setCurrentIndex(1)
            self.bottom_bar_stack.setCurrentIndex(1)
            self.top_bar_stack.setVisible(True)
            self.bottom_bar_stack.setVisible(True)
            self._h_line_mid.setVisible(True)
            self._h_line_bottom.setVisible(True)
        else:
            self.mode_title.setText(I18N.tr("panel.tonal_saturation", "色调饱和度"))
            self.top_bar_stack.setVisible(False)
            self.bottom_bar_stack.setVisible(False)
            self._h_line_mid.setVisible(False)
            self._h_line_bottom.setVisible(False)

    # ==================== 数值行（label + DragValue） ====================

    def _create_prim_slider(self, label: str, min_val: float, max_val: float,
                            default_val: float, *, key: str, base: bool = True,
                            scale: float = 1.0, offset: float = 0.0,
                            decimals: int = 2,
                            accent: str = "#919195") -> _PrimaryValueRow:
        """创建 label + DragValue 参数行并登记到注册表。

        同一参数可能同时出现在 Primaries 与 Log 两条参数条上（孪生行），
        注册表保证值双向同步与批量回填/重置。
        """
        row = _PrimaryValueRow(label, min_val, max_val, scale=scale,
                               offset=offset, decimals=decimals, accent=accent,
                               default=default_val, drag_rate=scalar_drag_rate(key))
        self._scalar_rows.setdefault(key, []).append(row)
        row.valueChanged.connect(
            lambda value, r=row, k=key, is_base=base:
            self._on_scalar_row_changed(k, is_base, r, value))
        row.interactionStarted.connect(self.interaction_started.emit)
        row.interactionEnded.connect(self._on_slider_interaction_ended)
        return row

    def _on_scalar_row_changed(self, key: str, is_base: bool,
                               source: _PrimaryValueRow, value: float) -> None:
        """参数行变化：同步孪生行后走对应节流路径。"""
        if self._syncing_ui:
            return
        self._sync_scalar_rows(key, source, value)
        timer = self._grading_update_timer if is_base else self._extended_update_timer
        if not timer.isActive():
            timer.start()

    def _sync_scalar_rows(self, key: str, source: _PrimaryValueRow, value: float) -> None:
        """同一参数的孪生行（Primaries/Log 参数条）静默跟随。"""
        for row in self._scalar_rows[key]:
            if row is source:
                continue
            blocked = row.blockSignals(True)
            try:
                row.setValue(value)
            finally:
                row.blockSignals(blocked)

    # ==================== 信号发射 ====================

    def _on_slider_interaction_ended(self) -> None:
        """Flush the last throttled slider value before ending its gesture."""
        self.flush_pending_changes()
        self.interaction_ended.emit()

    def _emit_grading_changed(self) -> None:
        """节流定时器回调：发射真正的 grading_changed 信号。"""
        if self._syncing_ui:
            return
        lift = (self.lift_wheel.hue, self.lift_wheel.sat, self.lift_wheel.brightness)
        gamma = (self.gamma_wheel.hue, self.gamma_wheel.sat, self.gamma_wheel.brightness)
        gain = (self.gain_wheel.hue, self.gain_wheel.sat, self.gain_wheel.brightness)

        # 数值显示单位由控件转换为原有引擎参数；曝光为内部透传状态。
        exp = self._exposure_value
        temp = self.temp_slider.value()
        tint = self.tint_slider.value()
        contrast = self.contrast_slider.value()
        shadow = self.shadow_slider.value()
        sat = self.sat_slider.value()
        vibrance = self.vibrance_slider.value()
        mid_det = self.mid_detail_slider.value()
        hue = self.hue_slider.value()

        self.grading_changed.emit(lift, gamma, gain, exp, temp, tint, contrast,
                                  shadow, sat, vibrance, mid_det, hue,
                                  self.highlights_slider.value())

    def _emit_grading_extended(self) -> None:
        """节流定时器回调：发射扩展参数 dict（Offset / pivot / Log / LumMix）。"""
        if self._syncing_ui:
            return
        self.grading_extended_changed.emit(self._get_primary_extended_params())

    def _on_param_changed(self) -> None:
        """乘性三轮变化 → 基础 13 参信号。"""
        self._emit_grading_changed()

    def _on_extended_param_changed(self) -> None:
        """加性轮变化 → 扩展 dict 信号。"""
        self._emit_grading_extended()

    def get_extended_params(self) -> dict[str, Any]:
        """Snapshot all extended grading controls for persistence."""
        return {**self._get_primary_extended_params(), **self.base_grade_panel.get_params()}

    def _get_primary_extended_params(self) -> dict[str, Any]:
        return {
            PRIMARY_CALIBRATION_VERSION_KEY: self._calibration_version,
            "offset": (self.offset_wheel.hue, self.offset_wheel.sat,
                       self.offset_wheel.brightness),
            "pivot": self.pivot_slider.value(),
            "log_shadow": (self.log_shadow_wheel.hue, self.log_shadow_wheel.sat,
                           self.log_shadow_wheel.brightness),
            "log_midtone": (self.log_midtone_wheel.hue,
                            self.log_midtone_wheel.sat,
                            self.log_midtone_wheel.brightness),
            "log_highlight": (self.log_highlight_wheel.hue,
                              self.log_highlight_wheel.sat,
                              self.log_highlight_wheel.brightness),
            "log_offset": (self._stored_log_offset if self._uses_measured_primaries else
                           (self.log_offset_wheel.hue, self.log_offset_wheel.sat,
                            self.log_offset_wheel.brightness)),
            "log_low_range": self.log_low_range_slider.value(),
            "log_high_range": self.log_high_range_slider.value(),
            "lum_mix": self.lum_mix_slider.value(),
        }

    def _on_bars_changed(self, wheel: ColorWheel, hue: float, sat: float,
                         brightness: float) -> None:
        wheel.blockSignals(True)
        try:
            wheel.set_hsv(hue, sat, brightness)
        finally:
            wheel.blockSignals(False)
        if wheel.math == "add":
            self._on_offset_changed(wheel)
        else:
            self._on_param_changed()

    def _on_offset_changed(self, source: ColorWheel) -> None:
        if self._uses_measured_primaries:
            other = self.offset_wheel if source is self.log_offset_wheel else self.log_offset_wheel
            blocked = other.blockSignals(True)
            other.set_hsv(source.hue, source.sat, source.brightness)
            other.blockSignals(blocked)
            self.color_bars_panel.offset.set_hsv(source.hue, source.sat, source.brightness)
        self._on_extended_param_changed()

    def _configure_calibration_units(self, enabled: bool) -> None:
        for row in self._scalar_rows["temp"]:
            row.set_display_units(PRIMARY_CALIBRATION_TEMP_SCALE if enabled else 1.0, 0.0, 1 if enabled else 2)
        for row in self._scalar_rows["pivot"]:
            row.set_display_units(1.0, 0.255 if enabled else 0.0, 3, display_bounds=(
                (0.0, 1.0) if self._display_calibration_version == PRIMARY_CALIBRATION_CURRENT_VERSION else None))
        for wheel in (self.log_shadow_wheel, self.log_midtone_wheel, self.log_highlight_wheel):
            wheel.set_additive_display_units(0.01 if enabled else 1.0, 0.0)
        for wheel in (self.offset_wheel, self.log_offset_wheel):
            wheel.set_additive_display_units(1.0, 25.0 if enabled else 0.0)
        self.color_bars_panel.offset.set_display_base(25.0 if enabled else 0.0)
        for wheel in (self.lift_wheel, self.gamma_wheel, self.gain_wheel, self.offset_wheel,
                      self.log_shadow_wheel, self.log_midtone_wheel,
                      self.log_highlight_wheel, self.log_offset_wheel):
            wheel.set_measured_response(enabled)
        for column in self.color_bars_panel.columns():
            column.set_measured_response(enabled)

    @property
    def _uses_measured_primaries(self) -> bool:
        return self._display_calibration_version in (1, PRIMARY_CALIBRATION_CURRENT_VERSION)

    @property
    def pending_primary_calibration_version(self) -> int:
        """The profile displayed for a neutral node until a grade is written."""
        return self._display_calibration_version if self._calibration_version == 0 else 0

    def refresh_primary_calibration(self) -> None:
        """Refresh display units when the active video timeline space changes."""
        version = self._calibration_version
        if (version == 0 and self._ungraded and self._use_measured_primaries is not None
                and self._use_measured_primaries()):
            version = PRIMARY_CALIBRATION_CURRENT_VERSION
        if version != self._display_calibration_version:
            self._display_calibration_version = version
            self._configure_calibration_units(self._uses_measured_primaries)

    def load_extended_params(self, params: Mapping[str, Any], *, animated: bool = False) -> None:
        """Project a complete grade onto the tools without committing changes."""
        self._ungraded = not animated and ungraded_primary_params(params)
        self.set_extended_params({key: params.get(key, default) for key, default
                                  in DEFAULT_EXTENDED_GRADING_PARAMS.items()})

    def confirm_primary_calibration(self, version: int) -> None:
        """Keep the units of the profile persisted by the first grade edit."""
        self._calibration_version = version
        self.refresh_primary_calibration()

    def flush_pending_changes(self) -> None:
        """Bring engine listeners up to date before saving full layer payloads."""
        if self._syncing_ui:
            return
        for wheel in (self.lift_wheel, self.gamma_wheel, self.gain_wheel,
                      self.offset_wheel, self.log_shadow_wheel,
                      self.log_midtone_wheel, self.log_highlight_wheel,
                      self.log_offset_wheel):
            wheel.flush_pending_changes()
        self.color_bars_panel.flush_pending_changes()
        self.base_grade_panel.flush_pending_changes()
        if self._grading_update_timer.isActive():
            self._grading_update_timer.stop()
            self._emit_grading_changed()
        if self._extended_update_timer.isActive():
            self._extended_update_timer.stop()
            self._emit_grading_extended()

    def set_wheel_rotation(self, rotation_degrees: float) -> None:
        """Apply one physical/display calibration angle to all wheels."""

        for wheel in (self.lift_wheel, self.gamma_wheel, self.gain_wheel,
                      self.offset_wheel, self.log_shadow_wheel,
                      self.log_midtone_wheel, self.log_highlight_wheel,
                      self.log_offset_wheel):
            wheel.set_rotation_degrees(rotation_degrees)
        self.base_grade_panel.set_rotation_degrees(self.lift_wheel.rotation_degrees)

    def set_wheel_params(
        self,
        wheel_name: str,
        values: tuple[float, float, float],
        *,
        emit: bool = False,
    ) -> None:
        """Update one color wheel without rewriting the whole grading panel.

        Hardware trackerballs can publish many more updates than a mouse drag.
        Updating all wheels, sliders, and RGB readouts for every one of
        those updates makes the UI thread the limiting factor.  This narrow
        path preserves the normal ``grading_changed`` signal while repainting
        only the wheel that actually changed.
        """

        wheel_map = {
            "lift": (self.lift_wheel, self.color_bars_panel.lift),
            "gamma": (self.gamma_wheel, self.color_bars_panel.gamma),
            "gain": (self.gain_wheel, self.color_bars_panel.gain),
            "offset": (self.offset_wheel, self.color_bars_panel.offset),
            "log_shadow": (self.log_shadow_wheel, None),
            "log_midtone": (self.log_midtone_wheel, None),
            "log_highlight": (self.log_highlight_wheel, None),
            "log_offset": (self.log_offset_wheel, None),
        }
        entry = wheel_map.get(wheel_name)
        if entry is None:
            raise ValueError(f"unsupported grading wheel: {wheel_name!r}")
        wheel, column = entry
        if not isinstance(values, (tuple, list)) or len(values) != 3:
            raise ValueError("invalid grading wheel value")

        try:
            hue, saturation, brightness = (float(value) for value in values)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid grading wheel value") from exc

        wheel.blockSignals(True)
        try:
            wheel.set_hsv(hue, saturation, brightness)
        finally:
            wheel.blockSignals(False)

        if column is not None:
            column.blockSignals(True)
            try:
                column.set_hsv(hue, saturation, brightness)
            finally:
                column.blockSignals(False)

        if emit:
            if wheel is self.offset_wheel or wheel is self.log_offset_wheel:
                self._on_offset_changed(wheel)
            elif wheel.math == "add":
                self._emit_grading_extended()
            else:
                self._emit_grading_changed()

    def set_params(self, lift: tuple, gamma: tuple, gain: tuple,
                   exp: float, temp: float, tint: float, contrast: float,
                   shadow: float, sat: float, vibrance: float = 0.0,
                   mid_det: float = 0.0, hue: float = 0.0, emit: bool = False,
                   *, highlights: float = 0.0) -> None:
        self._syncing_ui = True
        self._grading_update_timer.stop()
        # 切层/回填瞬间若有在途扩展节流，必须一并取消：否则回填后定时器
        # 会把上一目标的陈旧扩展 dict 发射出去（跨目标污染）。
        self._extended_update_timer.stop()

        for w in [self.lift_wheel, self.gamma_wheel, self.gain_wheel]:
            w.blockSignals(True)
        self.color_bars_panel.blockSignals(True)

        self.lift_wheel.set_hsv(*lift)
        self.gamma_wheel.set_hsv(*gamma)
        self.gain_wheel.set_hsv(*gain)
        self.color_bars_panel.lift.set_hsv(*lift)
        self.color_bars_panel.gamma.set_hsv(*gamma)
        self.color_bars_panel.gain.set_hsv(*gain)

        # 曝光为内部透传状态（UI 不再直接编辑）
        self._exposure_value = float(exp)

        # 按注册表批量回填（孪生行一并更新，_syncing_ui 挡住回环）
        for key, v in [
            ("temp", temp), ("tint", tint),
            ("contrast", contrast), ("shadow", shadow),
            ("highlights", highlights),
            ("saturation", sat), ("vibrance", vibrance),
            ("mid_detail", mid_det), ("hue_shift", hue),
        ]:
            for row in self._scalar_rows.get(key, []):
                blocked = row.blockSignals(True)
                try:
                    row.setValue(v)
                finally:
                    row.blockSignals(blocked)

        for w in [self.lift_wheel, self.gamma_wheel, self.gain_wheel]:
            w.blockSignals(False)
        self.color_bars_panel.blockSignals(False)

        self._syncing_ui = False

        if emit:
            self._emit_grading_changed()

    def set_extended_params(self, params: dict[str, Any],
                            emit: bool = False) -> None:
        """扩展参数 → UI 单向同步（引擎/工程回填；不发信号除非 emit）。"""
        self._syncing_ui = True
        self._extended_update_timer.stop()
        if PRIMARY_CALIBRATION_VERSION_KEY in params:
            self.confirm_primary_calibration(params[PRIMARY_CALIBRATION_VERSION_KEY])
        if "log_offset" in params:
            value = params["log_offset"]
            if isinstance(value, (tuple, list)) and len(value) == 3:
                self._stored_log_offset = tuple(value)

        wheels = [
            (self.offset_wheel, self.color_bars_panel.offset, "offset"),
            (self.log_shadow_wheel, None, "log_shadow"),
            (self.log_midtone_wheel, None, "log_midtone"),
            (self.log_highlight_wheel, None, "log_highlight"),
            (self.log_offset_wheel, None, "log_offset"),
        ]
        for wheel, column, key in wheels:
            if key == "log_offset" and self._uses_measured_primaries:
                key = "offset"
            if key not in params:
                continue
            value = params[key]
            if not isinstance(value, (tuple, list)) or len(value) != 3:
                continue
            wheel.blockSignals(True)
            try:
                wheel.set_hsv(*value)
            finally:
                wheel.blockSignals(False)
            if column is not None:
                column.blockSignals(True)
                try:
                    column.set_hsv(*value)
                finally:
                    column.blockSignals(False)

        for key in ("pivot", "log_low_range", "log_high_range", "lum_mix"):
            if key not in params:
                continue
            for row in self._scalar_rows.get(key, []):
                row.blockSignals(True)
                try:
                    row.setValue(float(params[key]))
                finally:
                    row.blockSignals(False)

        base_grade = {key: value for key, value in params.items() if key.startswith("base_grade_")}
        if base_grade:
            self.base_grade_panel.set_params(base_grade)

        self._syncing_ui = False

        if emit:
            self.grading_extended_changed.emit(self.get_extended_params())

    def reset_all(self) -> None:
        self._syncing_ui = True
        self._stored_log_offset = (0.0, 0.0, 0.0)
        self._grading_update_timer.stop()
        self._extended_update_timer.stop()

        wheels = [self.lift_wheel, self.gamma_wheel, self.gain_wheel,
                  self.offset_wheel, self.log_shadow_wheel,
                  self.log_midtone_wheel, self.log_highlight_wheel,
                  self.log_offset_wheel]
        rows = [row for rows in self._scalar_rows.values() for row in rows]
        for w in wheels:
            w.blockSignals(True)
        self.color_bars_panel.blockSignals(True)
        for row in rows:
            row.blockSignals(True)

        for w in wheels:
            w.reset()
        for column in self.color_bars_panel.columns():
            column.reset()
        for row in rows:
            row.reset()

        for w in wheels:
            w.blockSignals(False)
        self.color_bars_panel.blockSignals(False)
        for row in rows:
            row.blockSignals(False)

        self._exposure_value = 0.0
        self.base_grade_panel.set_params(BASE_GRADE_DEFAULTS)

        self._syncing_ui = False

        self._emit_grading_changed()
        self.grading_extended_changed.emit(self.get_extended_params())
        self.reset_clicked.emit()

    def set_tonal_saturation(self, lift_sat: float, gamma_sat: float,
                             gain_sat: float) -> None:
        """引擎/工程 → UI 单向同步（转发到色调饱和度页）。"""
        self.tonal_saturation_panel.set_params(lift_sat, gamma_sat, gain_sat)

    # ==================== P1-COLOR-03: Grade Keyframes ====================

    def use_central_keyframes(self) -> None:
        """The project-backed Keyframes panel owns animation controls."""
        blocked = self.keyframe_mode_btn.blockSignals(True)
        self.keyframe_mode_btn.setChecked(False)
        self.keyframe_mode_btn.blockSignals(blocked)
        for button in (self.keyframe_mode_btn, self.kf_snapshot_btn, self.kf_remove_btn):
            button.hide()

    @property
    def keyframe_mode(self) -> bool:
        """关键帧录制模式是否开启。"""
        return self.keyframe_mode_btn.isChecked()

    def set_keyframe_state(self, mode: bool, track_count: int = 0,
                           has_current_frame_kf: bool = False) -> None:
        """外部（控制器）回显关键帧状态：模式开关 + 轨道数提示。"""
        was_blocked = self.keyframe_mode_btn.blockSignals(True)
        self.keyframe_mode_btn.setChecked(bool(mode))
        self.keyframe_mode_btn.blockSignals(was_blocked)
        suffix = f"  [{track_count}]" if track_count else ""
        self.keyframe_mode_btn.setText(f"◆{suffix}")
        self.kf_snapshot_btn.setEnabled(track_count > 0)
        self.kf_remove_btn.setEnabled(has_current_frame_kf)

    # ==================== P9.1: Color Stack Integration ====================

    @property
    def current_stack_id(self) -> str | None:
        """当前关联的调色栈 ID。"""
        return self._current_stack_id

    @current_stack_id.setter
    def current_stack_id(self, stack_id: str | None) -> None:
        """设置当前关联的调色栈 ID。"""
        if stack_id != self._current_stack_id:
            self.base_grade_panel.finish_edit(commit=False)
        self._current_stack_id = stack_id
        for rows in self._scalar_rows.values():
            for row in rows:
                row.spin.cancel_drag()

    def set_save_callback(self, callback: Any) -> None:
        """
        设置保存回调函数。

        Args:
            callback: 回调函数，签名为 (grading_params: dict) -> None
        """
        self._save_callback = callback

    def get_current_params(self) -> dict[str, Any]:
        """
        获取当前 GradingPanel 的调色参数。

        Returns:
            调色参数字典，格式与 ColorStackData.grading_params 一致
            （含扩展参数键）
        """
        params = {
            "lift": (self.lift_wheel.hue, self.lift_wheel.sat,
                     self.lift_wheel.brightness),
            "gamma": (self.gamma_wheel.hue, self.gamma_wheel.sat,
                      self.gamma_wheel.brightness),
            "gain": (self.gain_wheel.hue, self.gain_wheel.sat,
                     self.gain_wheel.brightness),
            "exposure": self._exposure_value,
            "temp": self.temp_slider.value(),
            "tint": self.tint_slider.value(),
            "contrast": self.contrast_slider.value(),
            "shadow": self.shadow_slider.value(),
            "highlights": self.highlights_slider.value(),
            "saturation": self.sat_slider.value(),
            "vibrance": self.vibrance_slider.value(),
            "mid_detail": self.mid_detail_slider.value(),
            "sharpness": 0,  # Not exposed in GradingPanel
            "hue_shift": self.hue_slider.value(),
        }
        params.update(self.get_extended_params())
        return params

    def trigger_save(self) -> None:
        """
        触发保存回调（如果已设置）。

        用于手动触发保存，例如在切换 clip 或页面时。
        """
        if self._save_callback and self._current_stack_id:
            self.flush_pending_changes()
            params = self.get_current_params()
            self._save_callback(params)
