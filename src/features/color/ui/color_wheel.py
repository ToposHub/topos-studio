"""
色轮组件模块 - 包含单个色轮控件和完整调色面板
Color Wheel Module - Contains ColorWheel widget and GradingPanel

合并自:
- src/ui/widgets/color_wheel.py (ColorWheel 类, now at src/features/color/ui/color_wheel.py)
- src/ui/grading_panel.py (GradingPanel 类, now at src/features/color/ui/color_wheel.py)
"""

import math
from typing import Any, Optional
from PyQt6.QtWidgets import (QWidget, QToolButton, QPushButton, QHBoxLayout,
                             QVBoxLayout, QTabWidget, QSizePolicy, QLabel)
from PyQt6.QtCore import Qt, QPointF, pyqtSignal, QRectF, QSize, QTimer
from PyQt6.QtGui import (QPainter, QColor, QConicalGradient, QRadialGradient,
                         QBrush, QPen, QMouseEvent, QKeyEvent, QPainterPath)

from src.shared.assets.icon_manager import (icon_reset, icon_add,
                                             icon_delete, icon_circle,
                                             IconColors)
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.topos_controls import ToposSlider
from src.shared.ui.style_sheet import C, PARAM_LABEL_STYLE
from src.shared.i18n.i18n import I18N
from src.features.color.ui.tonal_saturation_panel import TonalSaturationPanel


# ============================================================================
# 达芬奇色轮角度校准常量
# ============================================================================
# Resolve's wheel: red at upper left (120°), blue at right, cyan lower right.
# Qt's conical gradient and the pointer both advance counterclockwise.
HUE_OFFSET_ANGLE = 120.0


class _BrightnessDial(ToposSlider):
    """Horizontal master wheel: relative motion without a click-to-jump track."""

    SCALE = 10000

    def __init__(self, parent: QWidget) -> None:
        super().__init__(Qt.Orientation.Horizontal, default_value=0, parent=parent)
        self.setRange(-self.SCALE, self.SCALE)
        self.setSingleStep(100)
        self.setPageStep(1000)
        self.setFixedSize(179, 22)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._dragging = False
        self._start_x = 0.0
        self._start_value = 0

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        groove = QRectF(1.0, 5.0, self.width() - 2.0, self.height() - 10.0)
        painter.setPen(QPen(QColor(C.BORDER_DARK), 1.0))
        painter.setBrush(QColor(C.BG_DEEPEST))
        painter.drawRoundedRect(groove, 5.0, 5.0)
        clip = QPainterPath()
        clip.addRoundedRect(groove.adjusted(2.0, 2.0, -2.0, -2.0), 3.0, 3.0)
        painter.setClipPath(clip)
        spacing = 4.0
        phase = (self.value() / self.SCALE * 300.0) % spacing
        for index in range(int(self.width() / spacing) + 1):
            x = index * spacing + phase
            distance = abs(x - self.width() / 2.0) / (self.width() / 2.0)
            color = QColor(C.TEXT_HINT)
            color.setAlpha(round(200.0 * (1.0 - min(distance, 1.0)) + 35.0))
            painter.setPen(QPen(color, 1.0))
            painter.drawLine(QPointF(x, groove.top() + 3.0),
                             QPointF(x, groove.bottom() - 3.0))
        painter.setClipping(False)
        active = self.isSliderDown() or self.hasFocus() or self.value() != 0
        painter.setPen(QPen(QColor(C.ORANGE if active else C.TEXT_DIM), 2.0))
        center = self.width() / 2.0
        painter.drawLine(QPointF(center, groove.top() + 2.0),
                         QPointF(center, groove.bottom() - 2.0))
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._apply_discrete_value(0)
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._dragging = True
            self._start_x = event.position().x()
            self._start_value = self.value()
            self.setSliderDown(True)
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            from src.shared.ui.widgets.drag_value import drag_speed_per_pixel
            speed = drag_speed_per_pixel(2.0 * self.SCALE, event.modifiers())
            self.setValue(round(self._start_value + (event.position().x() - self._start_x) * speed))
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self.mouseMoveEvent(event)
            self._dragging = False
            self.setSliderDown(False)
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            if self.isSliderDown():
                self.setValue(0)
                self.setSliderDown(False)
            else:
                self._apply_discrete_value(0)
            event.accept()

    def _apply_discrete_value(self, value: int) -> None:
        value = max(self.minimum(), min(self.maximum(), value))
        if value != self.value():
            self.setSliderDown(True)
            self.setValue(value)
            self.setSliderDown(False)

    def wheelEvent(self, event: Any) -> None:
        step = float(self.singleStep())
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            step *= 10.0
        elif event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            step *= 0.1
        delta = event.angleDelta().y()
        self._apply_discrete_value(self.value() + round(step if delta > 0 else -step if delta < 0 else 0))
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        navigation = (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
                      Qt.Key.Key_PageUp, Qt.Key.Key_PageDown, Qt.Key.Key_Home, Qt.Key.Key_End)
        if event.key() in navigation:
            self.setSliderDown(True)
            super().keyPressEvent(event)
            self.setSliderDown(False)
        else:
            super().keyPressEvent(event)


class _GradingValue(DragValue):
    """Compact value cell with a channel underline and the shared edit gestures."""

    def __init__(self, minimum: float, maximum: float, default: float,
                 decimals: int = 2, accent: str = "#919195",
                 drag_span: float | None = None) -> None:
        super().__init__("", minimum, maximum, default, decimals=decimals, compact=True)
        self._accent = accent
        self._drag_span = drag_span
        self.setFixedValueWidth(44)
        self.setFixedHeight(24)
        # Keep the shared DragValue normal/hover/active/editor styles intact.
        # Channel identity lives only in a separate, non-interactive color bar.
        self.layout().setContentsMargins(0, 0, 0, 4)
        self._edit.setFixedHeight(20)
        self._accent_bar = QWidget(self)
        self._accent_bar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._accent_bar.setStyleSheet(f"background: {accent}; border: none;")

    def _drag_sensitivity(self, modifiers: Qt.KeyboardModifier) -> float:
        if self._drag_span is None:
            return super()._drag_sensitivity(modifiers)
        from src.shared.ui.widgets.drag_value import drag_speed_per_pixel
        return drag_speed_per_pixel(self._drag_span, modifiers)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._accent_bar.setGeometry(0, self.height() - 2, self.width(), 2)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            if self.value() != self.defaultValue():
                self.interaction_started.emit()
                self.setValue(self.defaultValue())
                self.interaction_ended.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def wheelEvent(self, event: Any) -> None:
        step = self._single_step
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            step *= 10.0
        elif event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            step *= 0.1
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

    valueChanged = pyqtSignal(float)
    interactionStarted = pyqtSignal()
    interactionEnded = pyqtSignal()

    def __init__(self, label: str, minimum: float, maximum: float,
                 *, scale: float = 1.0, offset: float = 0.0,
                 decimals: int = 2, accent: str = "#919195") -> None:
        super().__init__()
        self._scale = scale
        self._offset = offset
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 3, 2, 3)
        layout.setSpacing(4)
        layout.addStretch()
        self.label = QLabel(label)
        self.label.setStyleSheet(PARAM_LABEL_STYLE)
        layout.addWidget(self.label)
        self.spin = _GradingValue(minimum * scale + offset,
                                  maximum * scale + offset, offset,
                                  decimals, accent)
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

    def reset(self) -> None:
        self.setValue(0.0)

    def blockSignals(self, block: bool) -> bool:
        self.spin.blockSignals(block)
        return super().blockSignals(block)


class ColorWheel(QWidget):
    """单个色轮控件 - 用于选择色调、饱和度、亮度。
    
    Features:
    - 圆形色轮选择 Hue/Sat（DaVinci Resolve 风格双重渐变）
    - 紧凑的主亮度和 RGB 数值输入框
    - 底部 RGB 可拖拽输入框（拖拽/双击编辑 → 反算 HSV 更新色轮）
    - 右上角重置按钮
    - 中心十字星定位锚点
    - 灰色中心锚点与高对比度控制点
    
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

    def __init__(
        self,
        title: str = "Wheel",
        parent: Optional[QWidget] = None,
        rotation_degrees: float = HUE_OFFSET_ANGLE,
        *,
        mode: str = "gain",
    ) -> None:
        super().__init__(parent)
        self.title = title
        self.mode = mode
        # A wheel's physical orientation is a calibration value, not a
        # rendering constant.  Keep it on each wheel so preferences can be
        # applied live without changing the grading data itself.
        self.rotation_degrees = float(rotation_degrees) % 360.0

        # === 色轮画布 + 主亮度/RGB 数值框 + 独立主亮度拨盘 ===
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(1)

        # --- 色轮画布区域（自绘：色轮盘面，stretch=3 优先占用可用高度） ---
        self._wheel_canvas = _WheelCanvas(title, self)
        outer_layout.addWidget(self._wheel_canvas, 3)

        # --- 达芬奇风格：4 个紧凑数值框并列（固定宽度，不随面板拉伸） ---
        val_row = QHBoxLayout()
        val_row.setContentsMargins(0, 0, 0, 0)
        val_row.setSpacing(1)

        neutral = 1.0 if mode == "gain" else 0.0
        self._bright_drag = _GradingValue(neutral - 1.0 + 2.0 ** -6.0,
                                           neutral + 63.0, neutral,
                                           accent="#c7c7c9", drag_span=2.0)
        self._bright_drag.setToolTip(I18N.tr("grading.wheel_master", "主亮度"))
        self._r_drag = _GradingValue(neutral - 1.0 if mode != "gain" else 0.0, neutral + 191.0, neutral, accent="#f1383d", drag_span=2.0)
        self._g_drag = _GradingValue(neutral - 1.0 if mode != "gain" else 0.0, neutral + 191.0, neutral, accent="#24d958", drag_span=2.0)
        self._b_drag = _GradingValue(neutral - 1.0 if mode != "gain" else 0.0, neutral + 191.0, neutral, accent="#3476ff", drag_span=2.0)
        for control, channel in zip((self._r_drag, self._g_drag, self._b_drag), ("R", "G", "B")):
            control.setToolTip(channel)
        val_row.addWidget(self._bright_drag)
        val_row.addWidget(self._r_drag)
        val_row.addWidget(self._g_drag)
        val_row.addWidget(self._b_drag)

        # 用固定宽度容器包裹，保证 4 个框始终紧凑成组，不随面板宽度散开
        val_container = QWidget()
        val_container.setFixedWidth(179)  # 44 × 4 + 1 × 3
        val_container.setLayout(val_row)

        # 居中放置：左右各加 stretch，让数值框组对齐色轮中心
        val_center_layout = QHBoxLayout()
        val_center_layout.setContentsMargins(0, 0, 0, 0)
        val_center_layout.setSpacing(0)
        val_center_layout.addStretch(1)
        val_center_layout.addWidget(val_container)
        val_center_layout.addStretch(1)
        outer_layout.addLayout(val_center_layout)

        self.brightness_slider = _BrightnessDial(self)
        self.brightness_slider.setAccessibleName(title + " " + I18N.tr("grading.wheel_master", "Master"))
        self.brightness_slider.setToolTip(I18N.tr("grading.brightness_dial_tooltip",
            "Drag left/right to adjust master brightness; double-click to reset. Ctrl/⌘: fine, Shift: coarse."))
        outer_layout.addWidget(self.brightness_slider, 0, Qt.AlignmentFlag.AlignHCenter)

        # Internal State
        self.hue: float = 0.0      # 0 to 360
        self.sat: float = 0.0      # 0 to 1.0
        self.brightness: float = 0.0  # -1.0 to 1.0
        self._updating_brightness = False
        self._updating_rgb = False
        
        self.knob_pos = QPointF(0, 0)  # Relative to center, normalized -1 to 1
        self.dragging_knob: bool = False
        
        # 缓存几何参数，避免重复计算
        self._cached_radius: float = 0.0
        self._cached_center: QPointF = QPointF(0, 0)
        
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
        self._bright_drag.valueChanged.connect(self._on_brightness_drag_changed)
        self.brightness_slider.valueChanged.connect(self._on_brightness_slider_changed)
        self.brightness_slider.sliderPressed.connect(self.interaction_started.emit)
        self.brightness_slider.sliderReleased.connect(self._on_value_interaction_ended)
        self._r_drag.valueChanged.connect(self._on_rgb_changed)
        self._g_drag.valueChanged.connect(self._on_rgb_changed)
        self._b_drag.valueChanged.connect(self._on_rgb_changed)
        for control in (self._bright_drag, self._r_drag, self._g_drag, self._b_drag):
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
        if self._update_timer.isActive():
            self._update_timer.stop()
        self._pending_hue = None
        self._pending_sat = None
        self._pending_val = None
        self.dragging_knob = False
        self._wheel_canvas.dragging_knob = False

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

    def set_hsv(self, hue: float, sat: float, brightness: float) -> None:
        """从后端引擎设置 Hue/Sat/Brightness 值，更新 UI 控制点位置"""
        # 程序化回写（undo/关键帧/切图层/Tangent）必须取消在途的节流
        # pending：否则 16ms 后定时器用过期 hue/sat 发 changed，把引擎
        # 刚回写的值又覆盖回旧值（与 reset() 的防护理应一致）。
        if self._update_timer.isActive():
            self._update_timer.stop()
        self._pending_hue = None
        self._pending_sat = None
        self._pending_val = None

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
        screen_angle = math.radians(self._screen_angle_for_hue(self.hue))
        self.knob_pos = QPointF(
            self.sat * math.cos(screen_angle),
            -self.sat * math.sin(screen_angle),
        )

    def blockSignals(self, block: bool) -> bool:
        self.brightness_slider.blockSignals(block)
        self._bright_drag.blockSignals(block)
        self._r_drag.blockSignals(block)
        self._g_drag.blockSignals(block)
        self._b_drag.blockSignals(block)
        return super().blockSignals(block)

    def _on_brightness_slider_changed(self, value: int) -> None:
        if self._updating_brightness:
            return
        self.brightness = value / _BrightnessDial.SCALE
        self._pending_hue = self.hue
        self._pending_sat = self.sat
        self._pending_val = self.brightness
        self._update_rgb_values()
        if not self._update_timer.isActive():
            self._update_timer.start()

    def _on_brightness_drag_changed(self, val: float) -> None:
        """亮度数值变化，更新状态并合并高频信号。"""
        if self._updating_brightness:
            return
        multiplier = val + (0.0 if self.mode == "gain" else 1.0)
        stops = math.log2(max(multiplier, 2.0 ** -6.0))
        self.brightness = math.copysign(math.sqrt(abs(stops) / 6.0), stops)
        self._pending_hue = self.hue
        self._pending_sat = self.sat
        self._pending_val = self.brightness
        self._update_rgb_values()
        self._wheel_canvas.update()
        if not self._update_timer.isActive():
            self._update_timer.start()

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

    def _update_rgb_values(self) -> None:
        """从当前 hue/sat/brightness 计算 RGB 并更新 3 个 DragValue（防递归）。"""
        brightness = self.brightness
        stops = (brightness ** 2) * 6.0
        if brightness < 0:
            stops = -stops
        b_mult = math.pow(2.0, stops)

        if self.sat == 0:
            r, g, b = b_mult, b_mult, b_mult
        else:
            c = QColor.fromHsvF(self.hue / 360.0, self.sat, 1.0)
            rf, gf, bf = c.redF(), c.greenF(), c.blueF()
            avg = (rf + gf + bf) / 3.0
            r, g, b = (rf / avg) * b_mult, (gf / avg) * b_mult, (bf / avg) * b_mult

        self._updating_rgb = True
        self._updating_brightness = True
        self.brightness_slider.setValue(round(self.brightness * _BrightnessDial.SCALE))
        offset = 0.0 if self.mode == "gain" else 1.0
        self._bright_drag.setValue(b_mult - offset)
        self._r_drag.setValue(r - offset)
        self._g_drag.setValue(g - offset)
        self._b_drag.setValue(b - offset)
        self._updating_rgb = False
        self._updating_brightness = False

    def _on_rgb_changed(self, _val: float) -> None:
        """RGB DragValue 变化 → 反算 hue/sat/brightness → 更新色轮。"""
        if self._updating_rgb:
            return
        offset = 0.0 if self.mode == "gain" else 1.0
        r = max(0.0, self._r_drag.value() + offset)
        g = max(0.0, self._g_drag.value() + offset)
        b = max(0.0, self._b_drag.value() + offset)

        hue, sat, brightness = self._rgb_to_hsv(r, g, b)

        self.hue = hue
        self.sat = sat
        self.brightness = brightness

        # 更新控制点位置
        self._update_knob_position()

        self._update_rgb_values()
        self._wheel_canvas.update()

        # 通过节流发射 changed 信号
        self._pending_hue = self.hue
        self._pending_sat = self.sat
        self._pending_val = self.brightness
        if not self._update_timer.isActive():
            self._update_timer.start()

    @staticmethod
    def _rgb_to_hsv(r: float, g: float, b: float) -> tuple:
        """从显示 RGB 值反算 hue/sat/brightness。

        正向算法：
            b_mult = 2^(stops),  c = fromHsvF(hue/360, sat, 1.0)
            R = (c.r / avg) * b_mult  (G/B 同理)

        反向：
            b_mult = avg(R, G, B)  → brightness
            归一化色度 → hue + sat
        """
        # 亮度反算：b_mult = mean(R, G, B)
        b_mult = max((r + g + b) / 3.0, 1e-6)

        # brightness from b_mult: stops = log2(b_mult), brightness ≈ sqrt(|stops|/6)
        stops = math.log2(max(b_mult, 1e-6))
        if stops >= 0:
            brightness = math.sqrt(stops / 6.0)
        else:
            brightness = -math.sqrt(-stops / 6.0)
        brightness = max(-1.0, min(1.0, brightness))

        # 归一化色度
        nr = r / b_mult
        ng = g / b_mult
        nb = b / b_mult

        # AUD-25303：等比归一化而非 clamp(0,1) 截断。正向公式
        # (_update_rgb_values) 产出的色度分量可 >1（sat=0.5 时 nr=1.5），
        # 直接 clamp 会破坏分量比例、折叠饱和度（回读 sat=0.25）。
        # HSV 对统一缩放不变，按最大分量等比缩放即可还原比例。
        m = max(nr, ng, nb, 1e-6)
        c = QColor.fromRgbF(
            max(0.0, min(1.0, nr / m)),
            max(0.0, min(1.0, ng / m)),
            max(0.0, min(1.0, nb / m)))
        hue = float(c.hueF()) * 360.0 if c.hueF() >= 0 else 0.0
        sat = float(c.saturationF())

        return hue, sat, brightness


class _WheelCanvas(QWidget):
    """Resolve-style disk; drawing and hit testing share one geometry and hue basis."""

    interaction_started = pyqtSignal()
    def __init__(self, title: str, wheel: ColorWheel) -> None:
        super().__init__(wheel)
        self.title = title
        self._wheel = wheel
        self.dragging_knob = False
        self._press_pos = QPointF()
        self._press_knob = QPointF()
        self.setMinimumSize(96, 120)
        self.reset_btn = QToolButton(self)
        self.reset_btn.setIcon(icon_reset(IconColors.DEFAULT))
        self.reset_btn.setIconSize(QSize(13, 13))
        self.reset_btn.setFixedSize(20, 20)
        self.reset_btn.setToolTip(I18N.tr("grading.reset_wheel", "重置") + " " + title)
        self.reset_btn.setStyleSheet("""
            QToolButton { background: transparent; border: none; border-radius: 3px; }
            QToolButton:hover { background: #414146; }
        """)
        self.reset_btn.clicked.connect(wheel.reset)

    def _geometry(self) -> tuple[QPointF, float]:
        radius = max(1.0, min((self.width() - 26.0) / 2.0,
                              (self.height() - 38.0) / 2.0, 100.0))
        return QPointF(self.width() / 2.0, 24.0 + (self.height() - 26.0) / 2.0), radius

    def _hue_gradient(self, center: QPointF) -> QConicalGradient:
        gradient = QConicalGradient(center, self._wheel.rotation_degrees)
        for sector in range(7):
            gradient.setColorAt(sector / 6.0, QColor.fromHsvF((sector % 6) / 6.0, 1.0, 1.0))
        return gradient

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center, radius = self._geometry()
        self._wheel._cached_center = center
        self._wheel._cached_radius = radius
        painter.setPen(QColor("#dedee2"))
        font = painter.font()
        font.setPixelSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(22, 0, self.width() - 44, 22),
                         Qt.AlignmentFlag.AlignCenter, self.title)

        painter.setBrush(QColor("#171719"))
        painter.setPen(QPen(QColor("#101012"), 5.0))
        painter.drawEllipse(center, radius + 7.0, radius + 7.0)
        # A thin neutral surround and a bright, continuous hue ring.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#65656b"), 1.0))
        painter.drawEllipse(center, radius + 6.0, radius + 6.0)
        painter.setPen(Qt.PenStyle.NoPen)
        gradient = self._hue_gradient(center)
        painter.setBrush(QBrush(gradient))
        painter.drawEllipse(center, radius, radius)
        # The dark disk keeps the same hue field as the ring. The neutral
        # radial center prevents the former white glare from hiding the puck.
        painter.setBrush(QColor(0, 0, 0, 205))
        painter.drawEllipse(center, radius - 3.0, radius - 3.0)
        neutral = QRadialGradient(center, radius - 3.0)
        neutral.setColorAt(0.0, QColor(57, 57, 61, 255))
        neutral.setColorAt(1.0, QColor(40, 40, 44, 0))
        painter.setBrush(QBrush(neutral))
        painter.drawEllipse(center, radius - 3.0, radius - 3.0)
        painter.setPen(QPen(QColor(180, 180, 185, 75), 0.7))
        painter.drawLine(center + QPointF(-radius + 4, 0), center + QPointF(radius - 4, 0))
        painter.drawLine(center + QPointF(0, -radius + 4), center + QPointF(0, radius - 4))
        painter.setBrush(QColor("#a9a9ad"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(center, 1.5, 1.5)
        puck = center + self._wheel.knob_pos * radius
        painter.setPen(QPen(QColor("#18181a"), 1.0))
        painter.setBrush(QColor("#dfdfe1"))
        painter.drawEllipse(puck, 3.0, 3.0)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        center, radius = self._geometry()
        delta = event.position() - center
        if math.hypot(delta.x(), delta.y()) <= radius:
            self.dragging_knob = True
            self._press_pos = event.position()
            self._press_knob = QPointF(self._wheel.knob_pos)
            self.interaction_started.emit()
            # Resolve uses a relative trackerball: pressing elsewhere on the
            # disk does not jump the current correction to that location.
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.dragging_knob:
            center, radius = self._geometry()
            self._update_knob(center + self._press_knob * radius
                              + event.position() - self._press_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self.dragging_knob and event.button() == Qt.MouseButton.LeftButton:
            self.mouseMoveEvent(event)
            self.dragging_knob = False
            self._wheel._on_value_interaction_ended()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        center, radius = self._geometry()
        delta = event.position() - center
        if event.button() == Qt.MouseButton.LeftButton and math.hypot(delta.x(), delta.y()) <= radius:
            self._wheel.reset()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.reset_btn.move(self.width() - 25, 1)

    def _update_knob(self, pos: QPointF) -> None:
        center, radius = self._geometry()
        delta = pos - center
        distance = math.hypot(delta.x(), delta.y())
        if distance > radius:
            delta *= radius / distance
            distance = radius
        wheel = self._wheel
        wheel.knob_pos = delta / radius
        wheel.sat = distance / radius
        if distance > 1e-8:
            wheel.hue = (math.degrees(math.atan2(-delta.y(), delta.x()))
                         - wheel.rotation_degrees) % 360.0
        wheel._pending_hue = wheel.hue
        wheel._pending_sat = wheel.sat
        wheel._pending_val = wheel.brightness
        wheel._update_rgb_values()
        self.update()
        if not wheel._update_timer.isActive():
            wheel._update_timer.start()


class GradingPanel(QWidget):
    """完整调色面板 - 三个色轮与上下两排数值参数。
    
    Features:
    - LIFT/GAMMA/GAIN 三个色轮
    - 曝光、色温、色调、对比度、色彩增强、阴影、高光、饱和度、色相数值调节
    - 全局重置按钮
    
    Signals:
        grading_changed: 参数改变时发射
        interaction_started: 交互开始时发射
        interaction_ended: 交互结束时发射
        reset_clicked: 重置按钮点击时发射
    """
    # Emits LGG, exposure, temp, tint, contrast, shadow, saturation,
    # vibrance, mid_detail, hue_shift, highlights; stored values stay unchanged.
    # 将信号参数类型从 int 改为 float (或 generic object) 以支持高精度参数传递
    grading_changed = pyqtSignal(tuple, tuple, tuple, float, float, float, float, float, float, float, float, float, float)
    # 色调区间饱和度（第二页 TonalSaturationPanel 转发）
    tonal_saturation_changed = pyqtSignal(float, float, float)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    reset_clicked = pyqtSignal()
    # P1-COLOR-03: 关键帧模式开关 / 当前帧快照 / 删除当前帧关键帧
    keyframe_mode_changed = pyqtSignal(bool)
    keyframe_snapshot_requested = pyqtSignal()
    keyframe_remove_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        # 节流定时器：限制滑块信号发射频率为 60FPS (16ms)
        self._grading_update_timer = QTimer(self)
        self._grading_update_timer.setSingleShot(True)
        self._grading_update_timer.setInterval(16)
        self._grading_update_timer.timeout.connect(self._emit_grading_changed)
        
        self._syncing_ui = False

        # P9.1: Color stack tracking
        self._current_stack_id: str | None = None
        self._save_callback: Any = None  # Callable[(dict[str, Any],), None]

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(8, 2, 8, 2)  # 增加左右内边距
        layout.setSpacing(1)  # 进一步减小纵向间距
        self.setLayout(layout)

        # 0. Header controls：关键帧按钮 → 页签栏左角、全局重置 → 右角，
        #    与页签同排（QTabWidget corner widget），节约一排纵向空间。
        # P1-COLOR-03: 关键帧控制（录制模式 / 当前帧快照 / 删除当前帧关键帧）
        # 录制点图标：未激活灰色 / 激活红色，与"录制"语义一致。
        self.keyframe_mode_btn = QPushButton()
        self.keyframe_mode_btn.setCheckable(True)
        self.keyframe_mode_btn.setIcon(icon_circle(IconColors.DEFAULT))
        self.keyframe_mode_btn.setIconSize(QSize(10, 10))
        self.keyframe_mode_btn.setFixedSize(24, 20)
        self.keyframe_mode_btn.setToolTip(I18N.tr(
            "grading.keyframe_mode_tooltip",
            "关键帧模式：开启后，调色参数修改会在当前帧记录关键帧"))
        self.keyframe_mode_btn.toggled.connect(
            lambda checked: self.keyframe_mode_btn.setIcon(
                icon_circle(IconColors.RED if checked else IconColors.DEFAULT)))
        self.keyframe_mode_btn.toggled.connect(self.keyframe_mode_changed.emit)

        self.kf_snapshot_btn = QPushButton()
        self.kf_snapshot_btn.setIcon(icon_add(IconColors.ORANGE))
        self.kf_snapshot_btn.setIconSize(QSize(10, 10))
        self.kf_snapshot_btn.setFixedSize(24, 20)
        self.kf_snapshot_btn.setToolTip(I18N.tr(
            "grading.keyframe_snapshot_tooltip",
            "在当前帧为所有已动画参数记录关键帧"))
        self.kf_snapshot_btn.clicked.connect(
            self.keyframe_snapshot_requested.emit)

        self.kf_remove_btn = QPushButton()
        self.kf_remove_btn.setIcon(icon_delete(IconColors.RED))
        self.kf_remove_btn.setIconSize(QSize(10, 10))
        self.kf_remove_btn.setFixedSize(24, 20)
        self.kf_remove_btn.setToolTip(I18N.tr(
            "grading.keyframe_remove_tooltip",
            "删除当前帧的所有调色关键帧"))
        self.kf_remove_btn.clicked.connect(
            self.keyframe_remove_requested.emit)

        self.reset_all_btn = QPushButton()
        self.reset_all_btn.setObjectName("reset_btn")
        self.reset_all_btn.setIcon(icon_reset())
        self.reset_all_btn.setIconSize(QSize(12, 12))
        self.reset_all_btn.setFixedSize(16, 16)
        self.reset_all_btn.setToolTip(I18N.tr(
            "grading.reset_all_tooltip", "重置全部调色参数"))
        self.reset_all_btn.clicked.connect(self.reset_all)

        # 页签容器（参考 ColorMappingPanel）：页 1 = LGG 色轮（原有
        # 达芬奇式排版原样保留），页 2 = 色调区间饱和度。
        self.tonal_saturation_panel = TonalSaturationPanel()
        self.tonal_saturation_panel.tonal_saturation_changed.connect(
            self.tonal_saturation_changed.emit)
        self.tonal_saturation_panel.interaction_started.connect(
            self.interaction_started.emit)
        self.tonal_saturation_panel.interaction_ended.connect(
            self.interaction_ended.emit)

        self._wheel_page = QWidget()
        wheel_page_layout = QVBoxLayout(self._wheel_page)
        wheel_page_layout.setContentsMargins(0, 0, 0, 0)
        wheel_page_layout.setSpacing(1)

        self._grading_tabs = QTabWidget()
        self._grading_tabs.setObjectName("grading_tabs")
        self._grading_tabs.setStyleSheet(
            """
            QTabBar::tab {
                padding: 6px 15px;
                background: #222;
                color: #888;
            }
            QTabBar::tab:selected {
                background: #333;
                color: #cc6600;
            }
            """
        )
        self._grading_tabs.tabBar().setMinimumHeight(28)

        self._wheel_page.setStyleSheet("QWidget { background-color: #27272b; }")
        self.exp_slider = self._create_prim_slider(I18N.tr("grading.exp", "曝光"), -100, 100, 0)
        self.temp_slider = self._create_prim_slider(I18N.tr("grading.temp", "色温"), -100, 100, 0, accent="qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ed9b48, stop:1 #35bdcf)")
        self.tint_slider = self._create_prim_slider(I18N.tr("grading.tint", "色调"), -100, 100, 0, accent="qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4fca77, stop:1 #d859bb)")
        self.contrast_slider = self._create_prim_slider(I18N.tr("grading.contrast", "对比度"), -100, 100, 0, scale=0.01, offset=1.0, decimals=3)
        self.mid_detail_slider = self._create_prim_slider(I18N.tr("grading.mid_det", "中间细节"), -100, 100, 0)
        self.vibrance_slider = self._create_prim_slider(I18N.tr("raw.color_boost", "Color Boost"), -100, 100, 0, accent="#9e769e")
        self.shadow_slider = self._create_prim_slider(I18N.tr("grading.shadow", "阴影"), -100, 100, 0)
        self.highlights_slider = self._create_prim_slider(I18N.tr("raw.highlights", "Highlights"), -100, 100, 0)
        self.sat_slider = self._create_prim_slider(I18N.tr("grading.sat", "饱和度"), -100, 100, 0, scale=0.5, offset=50.0)
        self.hue_slider = self._create_prim_slider(I18N.tr("grading.hue", "色相"), -180, 180, 0, scale=100.0 / 360.0, offset=50.0, accent="#9e769e")

        top_row = QHBoxLayout()
        top_row.setContentsMargins(4, 2, 4, 2)
        top_row.setSpacing(2)
        for control in (self.temp_slider, self.tint_slider, self.contrast_slider,
                        self.exp_slider, self.mid_detail_slider):
            top_row.addWidget(control, 1)
        wheel_page_layout.addLayout(top_row)
        wheels_layout = QHBoxLayout()
        wheels_layout.setContentsMargins(4, 6, 4, 8)
        wheels_layout.setSpacing(12)
        self.lift_wheel = ColorWheel(I18N.tr("grading.lift", "暗部"), mode="lift")
        self.gamma_wheel = ColorWheel(I18N.tr("grading.gamma", "中灰"), mode="gamma")
        self.gain_wheel = ColorWheel(I18N.tr("grading.gain", "亮部"), mode="gain")
        for wheel in (self.lift_wheel, self.gamma_wheel, self.gain_wheel):
            wheel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            wheels_layout.addWidget(wheel, 1)
        wheel_page_layout.addLayout(wheels_layout, 1)
        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(4, 2, 4, 2)
        bottom_row.setSpacing(2)
        for control in (self.vibrance_slider, self.shadow_slider, self.highlights_slider,
                        self.sat_slider, self.hue_slider):
            bottom_row.addWidget(control, 1)
        wheel_page_layout.addLayout(bottom_row)

        kf_corner = QWidget()
        kf_corner_layout = QHBoxLayout(kf_corner)
        # 顶边距 4px：按钮整体下移、底对齐页签行，给图标留出头顶空间。
        kf_corner_layout.setContentsMargins(4, 4, 4, 0)
        kf_corner_layout.setSpacing(2)
        kf_corner_layout.addWidget(self.keyframe_mode_btn)
        kf_corner_layout.addWidget(self.kf_snapshot_btn)
        kf_corner_layout.addWidget(self.kf_remove_btn)
        kf_corner_layout.addStretch()

        reset_corner = QWidget()
        reset_corner_layout = QHBoxLayout(reset_corner)
        # 与左侧关键帧按钮同款下移/底对齐。
        reset_corner_layout.setContentsMargins(4, 4, 0, 0)
        reset_corner_layout.addWidget(self.reset_all_btn)

        self._grading_tabs.setCornerWidget(kf_corner, Qt.Corner.TopLeftCorner)
        self._grading_tabs.setCornerWidget(reset_corner, Qt.Corner.TopRightCorner)

        self._grading_tabs.addTab(
            self._wheel_page, I18N.tr("panel.color_wheel", "色轮"))
        self._grading_tabs.addTab(
            self.tonal_saturation_panel,
            I18N.tr("panel.tonal_saturation", "色调饱和度"))
        layout.addWidget(self._grading_tabs, stretch=1)

        # Connections
        self.lift_wheel.changed.connect(self._on_param_changed)
        self.gamma_wheel.changed.connect(self._on_param_changed)
        self.gain_wheel.changed.connect(self._on_param_changed)

        self.lift_wheel.interaction_started.connect(self.interaction_started.emit)
        self.lift_wheel.interaction_ended.connect(self.interaction_ended.emit)
        self.gamma_wheel.interaction_started.connect(self.interaction_started.emit)
        self.gamma_wheel.interaction_ended.connect(self.interaction_ended.emit)
        self.gain_wheel.interaction_started.connect(self.interaction_started.emit)
        self.gain_wheel.interaction_ended.connect(self.interaction_ended.emit)
        
        # 滑块信号连接（使用节流机制）
        for s in [self.exp_slider, self.temp_slider, self.tint_slider,
                  self.contrast_slider, self.vibrance_slider, self.sat_slider,
                  self.shadow_slider, self.highlights_slider, self.mid_detail_slider, self.hue_slider]:
            s.valueChanged.connect(lambda _v: self._on_slider_value_changed())
            s.interactionStarted.connect(self.interaction_started.emit)
            s.interactionEnded.connect(self._on_slider_interaction_ended)

    def _create_prim_slider(self, label: str, min_val: int, max_val: int,
                            default_val: int, *, scale: float = 1.0,
                            offset: float = 0.0, decimals: int = 2,
                            accent: str = "#919195") -> _PrimaryValueRow:
        # Keep the existing control attributes/API for engine, undo and Tangent.
        return _PrimaryValueRow(label, min_val, max_val, scale=scale,
                                offset=offset, decimals=decimals, accent=accent)

    def _on_slider_value_changed(self) -> None:
        """滑块值变化时的节流处理。
        
        不会立即发射 grading_changed 信号，而是启动/重置节流定时器。
        这样可以避免高频拖动时的大量重复计算。
        """
        if self._syncing_ui:
            return
        self._grading_update_timer.start()

    def _on_slider_interaction_ended(self) -> None:
        """Flush the last throttled slider value before ending its gesture."""
        self.flush_pending_changes()
        self.interaction_ended.emit()

    def flush_pending_changes(self) -> None:
        """Bring engine listeners up to date before saving full layer payloads."""
        if self._syncing_ui:
            return
        for wheel in (self.lift_wheel, self.gamma_wheel, self.gain_wheel):
            wheel.flush_pending_changes()
        if self._grading_update_timer.isActive():
            self._grading_update_timer.stop()
            self._emit_grading_changed()

    def _emit_grading_changed(self) -> None:
        """节流定时器回调：发射真正的 grading_changed 信号。"""
        if self._syncing_ui:
            return
        lift = (self.lift_wheel.hue, self.lift_wheel.sat, self.lift_wheel.brightness)
        gamma = (self.gamma_wheel.hue, self.gamma_wheel.sat, self.gamma_wheel.brightness)
        gain = (self.gain_wheel.hue, self.gain_wheel.sat, self.gain_wheel.brightness)

        # 数值显示单位由控件转换为原有引擎参数
        exp = self.exp_slider.value()
        temp = self.temp_slider.value()
        tint = self.tint_slider.value()
        contrast = self.contrast_slider.value()
        shadow = self.shadow_slider.value()
        sat = self.sat_slider.value()
        vibrance = self.vibrance_slider.value()
        mid_det = self.mid_detail_slider.value()
        hue = self.hue_slider.value()

        self.grading_changed.emit(lift, gamma, gain, exp, temp, tint, contrast, shadow, sat, vibrance, mid_det, hue, self.highlights_slider.value())

    def _on_param_changed(self) -> None:
        """直接发射 grading_changed 信号（保留用于色轮）。"""
        self._emit_grading_changed()

    def set_wheel_rotation(self, rotation_degrees: float) -> None:
        """Apply one physical/display calibration angle to all three wheels."""

        for wheel in (self.lift_wheel, self.gamma_wheel, self.gain_wheel):
            wheel.set_rotation_degrees(rotation_degrees)

    def set_wheel_params(
        self,
        wheel_name: str,
        values: tuple[float, float, float],
        *,
        emit: bool = False,
    ) -> None:
        """Update one color wheel without rewriting the whole grading panel.

        Hardware trackerballs can publish many more updates than a mouse drag.
        Updating all three wheels, sliders, and RGB readouts for every one of
        those updates makes the UI thread the limiting factor.  This narrow
        path preserves the normal ``grading_changed`` signal while repainting
        only the wheel that actually changed.
        """

        wheel = {
            "lift": self.lift_wheel,
            "gamma": self.gamma_wheel,
            "gain": self.gain_wheel,
        }.get(wheel_name)
        if wheel is None:
            raise ValueError(f"unsupported grading wheel: {wheel_name!r}")
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

        if emit:
            self._emit_grading_changed()

    def set_params(self, lift: tuple, gamma: tuple, gain: tuple, 
                   exp: float, temp: float, tint: float, contrast: float, 
                   shadow: float, sat: float, vibrance: float = 0.0, 
                   mid_det: float = 0.0, hue: float = 0.0, emit: bool = False,
                   *, highlights: float = 0.0) -> None:
        self._syncing_ui = True
        self._grading_update_timer.stop()
        
        for w in [self.lift_wheel, self.gamma_wheel, self.gain_wheel]:
            w.blockSignals(True)
        for s in [
            self.exp_slider, self.temp_slider, self.tint_slider,
            self.contrast_slider, self.shadow_slider, self.highlights_slider, self.sat_slider,
            self.vibrance_slider, self.mid_detail_slider, self.hue_slider
        ]:
            s.blockSignals(True)
        
        self.lift_wheel.set_hsv(*lift)
        self.gamma_wheel.set_hsv(*gamma)
        self.gain_wheel.set_hsv(*gain)
        
        for s, v in [
            (self.exp_slider, exp), (self.temp_slider, temp), (self.tint_slider, tint),
            (self.contrast_slider, contrast), (self.shadow_slider, shadow),
            (self.highlights_slider, highlights),
            (self.sat_slider, sat), (self.vibrance_slider, vibrance),
            (self.mid_detail_slider, mid_det), (self.hue_slider, hue)
        ]:
            s.setValue(v)
            
        for w in [self.lift_wheel, self.gamma_wheel, self.gain_wheel]:
            w.blockSignals(False)
        for s in [
            self.exp_slider, self.temp_slider, self.tint_slider,
            self.contrast_slider, self.shadow_slider, self.highlights_slider, self.sat_slider,
            self.vibrance_slider, self.mid_detail_slider, self.hue_slider
        ]:
            s.blockSignals(False)
        
        self._syncing_ui = False
        
        if emit:
            self._emit_grading_changed()

    def reset_all(self) -> None:
        self._syncing_ui = True
        self._grading_update_timer.stop()
        
        for w in [self.lift_wheel, self.gamma_wheel, self.gain_wheel]:
            w.blockSignals(True)
        for s in [
            self.exp_slider, self.temp_slider, self.tint_slider,
            self.contrast_slider, self.shadow_slider, self.highlights_slider, self.sat_slider,
            self.vibrance_slider, self.mid_detail_slider, self.hue_slider
        ]:
            s.blockSignals(True)
        
        self.lift_wheel.reset()
        self.gamma_wheel.reset()
        self.gain_wheel.reset()
        
        for s in [
            self.exp_slider, self.temp_slider, self.tint_slider,
            self.contrast_slider, self.shadow_slider, self.highlights_slider, self.sat_slider,
            self.vibrance_slider, self.mid_detail_slider, self.hue_slider
        ]:
            s.reset()
        
        for w in [self.lift_wheel, self.gamma_wheel, self.gain_wheel]:
            w.blockSignals(False)
        for s in [
            self.exp_slider, self.temp_slider, self.tint_slider,
            self.contrast_slider, self.shadow_slider, self.highlights_slider, self.sat_slider,
            self.vibrance_slider, self.mid_detail_slider, self.hue_slider
        ]:
            s.blockSignals(False)
        
        self._syncing_ui = False

        self._on_param_changed()
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
        self._current_stack_id = stack_id

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
        """
        lift = (self.lift_wheel.hue, self.lift_wheel.sat, self.lift_wheel.brightness)
        gamma = (self.gamma_wheel.hue, self.gamma_wheel.sat, self.gamma_wheel.brightness)
        gain = (self.gain_wheel.hue, self.gain_wheel.sat, self.gain_wheel.brightness)

        exp = self.exp_slider.value()
        temp = self.temp_slider.value()
        tint = self.tint_slider.value()
        contrast = self.contrast_slider.value()
        shadow = self.shadow_slider.value()
        sat = self.sat_slider.value()
        vibrance = self.vibrance_slider.value()
        mid_det = self.mid_detail_slider.value()
        hue = self.hue_slider.value()

        return {
            "lift": lift,
            "gamma": gamma,
            "gain": gain,
            "exposure": exp,
            "temp": temp,
            "tint": tint,
            "contrast": contrast,
            "shadow": shadow,
            "highlights": self.highlights_slider.value(),
            "saturation": sat,
            "vibrance": vibrance,
            "mid_detail": mid_det,
            "sharpness": 0,  # Not exposed in GradingPanel
            "hue_shift": hue,
        }

    def trigger_save(self) -> None:
        """
        触发保存回调（如果已设置）。

        用于手动触发保存，例如在切换 clip 或页面时。
        """
        if self._save_callback and self._current_stack_id:
            self.flush_pending_changes()
            params = self.get_current_params()
            self._save_callback(params)
