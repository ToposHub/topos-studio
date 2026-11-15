"""Topos Primaries - Color Bars 页（Lift/Gamma/Gain/Offset）。

竖直通道条 + 通道数值格 + 滚花主轮，与色轮页共享同一组
(hue, sat, brightness) 状态和显示单位，双向同步。

视觉（原创设计，非达芬奇复刻）：圆角内凹纯黑滑槽 + 通道渐变填充柱 +
带通道描边的中空游标 + 水平中心基准线；Offset 列顶部/底部带微箭头
标记（上=增、下=减）。

加性列（Offset）显示单位为加性偏移 × 100（与色轮页一致），仅
R/G/B 三条；乘性列（Lift/Gamma/Gain）保持 YRGB 四条与既有值域。
"""

import math
from typing import Any, Optional

from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QToolButton,
                             QLabel, QSizePolicy)
from PyQt6.QtCore import Qt, QPointF, QRectF, QSize, QTimer, pyqtSignal
from PyQt6.QtGui import (QPainter, QColor, QPen, QPainterPath, QLinearGradient,
                         QMouseEvent, QKeyEvent)

from src.shared.assets.icon_manager import icon_reset, IconColors
from src.shared.ui.widgets.drag_value import drag_speed_per_pixel
from src.shared.i18n.i18n import I18N
from src.features.color.ui.color_wheel import (
    ColorWheel, KnurledJogWheel, _GradingValue,
    offset_hsb_to_display, offset_display_to_hsb,
)

_TRACK_FILL = "#0e0f12"
_TRACK_EDGE = "#090a0c"
_DEFAULT_LINE = "#32363f"
_HANDLE_FILL = "#14161a"
_GRADIENT_BLACK = "#0e0f12"
_CHANNEL_COLORS = {"Y": "#e5e7eb", "R": "#ef4444", "G": "#22c55e", "B": "#3b82f6"}
_FILL_COLORS = {"Y": "#e5e7eb", "R": "#db3a3b", "G": "#3adc3b", "B": "#3b3bdb"}
_CHANNELS = ("Y", "R", "G", "B")
_ADD_CHANNELS = ("R", "G", "B")
# 加性列滑槽视觉范围（显示单位）：偏移理论极值约 ±150，视觉两端留余量。
# 与数值框（±200）不对称沿用既有约定："bars clamp at visual range ends;
# numbers keep the true value"——滑槽截断显示，数值框保留真实值。
_ADD_BAR_RANGE = (-150.0, 150.0)


class _ChannelBar(QWidget):
    """Vertical channel bar: relative drag, outlined handle, value gradient fill."""

    valueChanged = pyqtSignal(float)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()

    def __init__(self, channel: str, minimum: float, maximum: float,
                 default: float, zero: float = 0.0,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._channel = channel
        self._min = float(minimum)
        self._max = float(maximum)
        self._default = float(default)
        self._zero = float(zero)
        self._value = self._default
        self._pressed = False
        self._engaged = False
        self._start_y = 0.0
        self._start_value = 0.0
        self.setFixedWidth(24)
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(channel)

    def value(self) -> float:
        return self._value

    def setValue(self, value: float) -> None:
        value = max(self._min, min(self._max, float(value)))
        if value == self._value:
            return
        self._value = value
        self.update()
        self.valueChanged.emit(value)

    def _y_for(self, value: float) -> float:
        span = self._max - self._min
        ratio = (max(self._min, min(self._max, value)) - self._min) / span
        return self.height() * (1.0 - ratio)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        track = QRectF(2.0, 2.0, w - 4.0, h - 4.0)
        radius = track.width() / 2.0

        # 1. 圆角内凹纯黑滑槽
        painter.setPen(QPen(QColor(_TRACK_EDGE), 1.0))
        painter.setBrush(QColor(_TRACK_FILL))
        painter.drawRoundedRect(track, radius, radius)

        # 2. 水平中心基准刻度细线
        mid_y = track.center().y()
        painter.setPen(QPen(QColor(_DEFAULT_LINE), 0.8))
        painter.drawLine(QPointF(track.left(), mid_y), QPointF(track.right(), mid_y))

        # Offset 列：顶部/底部微箭头标记（上=增、下=减）
        if self._is_offset_bar:
            painter.setPen(QPen(QColor("#737985"), 1.1, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            cx = track.center().x()
            painter.drawLine(QPointF(cx - 2.5, 9.0), QPointF(cx, 6.5))
            painter.drawLine(QPointF(cx + 2.5, 9.0), QPointF(cx, 6.5))
            painter.drawLine(QPointF(cx - 2.5, h - 9.0), QPointF(cx, h - 6.5))
            painter.drawLine(QPointF(cx + 2.5, h - 9.0), QPointF(cx, h - 6.5))

        # 3. 裁剪圆角内部绘制通道渐变填充柱
        clip = QPainterPath()
        clip.addRoundedRect(track.adjusted(1.0, 1.0, -1.0, -1.0),
                            radius - 1.0, radius - 1.0)
        painter.setClipPath(clip)

        yv = self._y_for(self._value)
        y0 = self._y_for(self._zero)
        fill = QColor(_FILL_COLORS[self._channel])
        black = QColor(_GRADIENT_BLACK)
        if self._value > self._zero:
            if self._zero == self._min:
                gradient = QLinearGradient(0.0, yv, 0.0, h)
                gradient.setColorAt(0.0, fill)
                gradient.setColorAt(1.0, black)
                painter.fillRect(QRectF(track.left(), yv, track.width(),
                                        track.bottom() - yv), gradient)
            else:
                painter.fillRect(QRectF(track.left(), yv, track.width(),
                                        y0 - yv), fill)
        elif self._value < self._zero:
            gradient = QLinearGradient(0.0, y0, 0.0, h)
            gradient.setColorAt(0.0, fill)
            gradient.setColorAt(1.0, black)
            painter.fillRect(QRectF(track.left(), y0, track.width(),
                                    yv - y0), gradient)
        painter.setClipping(False)

        # 4. 带通道高亮描边的中空精密游标块
        handle = QRectF(track.left() - 0.5, yv - 3.0, track.width() + 1.0, 6.0)
        painter.setPen(QPen(QColor(_CHANNEL_COLORS[self._channel]), 1.4))
        painter.setBrush(QColor(_HANDLE_FILL))
        painter.drawRoundedRect(handle, 1.5, 1.5)
        dash = (QColor("#b9b9ba") if self._channel == "Y"
                else QColor(_CHANNEL_COLORS[self._channel]).lighter(170))
        painter.setPen(QPen(dash, 1.4))
        painter.drawLine(QPointF(handle.left() + 3.0, yv),
                         QPointF(handle.right() - 3.0, yv))
        painter.end()

    _is_offset_bar = False

    def _reset(self) -> None:
        if self._value != self._default:
            self.interaction_started.emit()
            self.setValue(self._default)
            self.interaction_ended.emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.MiddleButton:
            self._reset()
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._pressed = True
            self._engaged = False
            self._start_y = event.position().y()
            self._start_value = self._value
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._pressed:
            return
        dy = self._start_y - event.position().y()
        if not self._engaged:
            if abs(dy) < 3.0:
                return
            self._engaged = True
            self.interaction_started.emit()
        speed = drag_speed_per_pixel(self._max - self._min, event.modifiers())
        self.setValue(self._start_value + dy * speed)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._pressed and event.button() == Qt.MouseButton.LeftButton:
            if self._engaged:
                self.mouseMoveEvent(event)
                self.interaction_ended.emit()
            self._pressed = False
            self._engaged = False
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = False
            self._reset()
            event.accept()

    def wheelEvent(self, event: Any) -> None:
        step = 0.01
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            step *= 10.0
        elif event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            step *= 0.1
        delta = event.angleDelta().y()
        target = self._value + (step if delta > 0 else -step if delta < 0 else 0.0)
        target = max(self._min, min(self._max, target))
        if target != self._value:
            self.interaction_started.emit()
            self.setValue(target)
            self.interaction_ended.emit()
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        step = 0.01
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Right):
            self._reset_step(step)
        elif event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Left):
            self._reset_step(-step)
        else:
            super().keyPressEvent(event)

    def _reset_step(self, step: float) -> None:
        target = max(self._min, min(self._max, self._value + step))
        if target != self._value:
            self.interaction_started.emit()
            self.setValue(target)
            self.interaction_ended.emit()


class _OffsetBar(_ChannelBar):
    """Offset 列通道条：带上下微箭头标记的加性偏移条。"""

    _is_offset_bar = True


class _BarRow(QWidget):
    """The bars plus the horizontal default-value reference line."""

    def __init__(self, bars: list[_ChannelBar], default: float,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._bars = bars
        self._default = default
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(14)
        layout.addStretch(1)
        for bar in bars:
            layout.addWidget(bar)
        layout.addStretch(1)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        first, last = self._bars[0], self._bars[-1]
        y = first.y() + first._y_for(self._default)
        painter.setPen(QPen(QColor(_DEFAULT_LINE), 1.0))
        painter.drawLine(QPointF(first.x() - 16.0, y),
                         QPointF(last.x() + last.width() + 16.0, y))
        painter.end()


class _BarsColumn(QWidget):
    """One Lift/Gamma/Gain/Offset bar group; mirrors the ColorWheel signal contract."""

    changed = pyqtSignal(float, float, float)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()

    def __init__(self, title: str, mode: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._mode = mode
        self._math = "add" if mode in ("offset", "log_shadow", "log_midtone",
                                       "log_highlight", "log_offset") else "mult"
        self._channels = _ADD_CHANNELS if self._math == "add" else _CHANNELS
        self._is_offset = (mode == "offset")

        self.hue = 0.0
        self.sat = 0.0
        self.brightness = 0.0
        self._rgb = (1.0, 1.0, 1.0)
        self._updating = False
        self._pending: Optional[tuple[float, float, float]] = None
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(16)
        self._update_timer.timeout.connect(self._emit_changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(4, 0, 4, 0)
        header.addStretch(1)
        label = QLabel(title)
        label.setStyleSheet("color: #d4d7dd; font-size: 12px; font-weight: 600;")
        header.addWidget(label)
        header.addStretch(1)
        self.reset_btn = QToolButton()
        self.reset_btn.setIcon(icon_reset(IconColors.DEFAULT))
        self.reset_btn.setIconSize(QSize(13, 13))
        self.reset_btn.setFixedSize(20, 20)
        self.reset_btn.setToolTip(I18N.tr("grading.reset_wheel", "重置") + " " + title)
        self.reset_btn.setStyleSheet("""
            QToolButton { background: transparent; border: none; border-radius: 3px; }
            QToolButton:hover { background: #414146; }
        """)
        self.reset_btn.clicked.connect(self.reset)
        header.addWidget(self.reset_btn)
        layout.addLayout(header)

        if self._math == "add":
            bar_min, bar_max = _ADD_BAR_RANGE
            num_min, num_max = -200.0, 200.0
            neutral = 0.0
            # 加性轮无乘率显示偏移
            self._display_offset = 0.0
        else:
            bar_min, bar_max = (0.0, 2.0) if mode == "gain" else (-1.0, 1.0)
            num_min = 0.0 if mode == "gain" else -1.0
            num_max = (1.0 if mode == "gain" else 0.0) + 191.0
            neutral = 1.0 if mode == "gain" else 0.0
            # 显示偏移 ≠ 中性值：lift/gamma 显示"乘率 − 1"，gain 显示绝对乘率
            self._display_offset = 0.0 if mode == "gain" else 1.0
        self._neutral = neutral

        self._bars = {ch: self._make_bar(ch, bar_min, bar_max, neutral)
                      for ch in self._channels}
        self._bar_row = _BarRow([self._bars[ch] for ch in self._channels],
                                neutral)
        layout.addWidget(self._bar_row, 1)

        num_row = QHBoxLayout()
        num_row.setContentsMargins(0, 0, 0, 0)
        num_row.setSpacing(4)
        num_row.addStretch(1)
        self._nums: dict[str, _GradingValue] = {}
        box_width = 52 if self._math == "add" else 34
        box_span = 30.0 if self._math == "add" else 2.0
        box_height = 24 if self._math == "add" else 22
        for ch in self._channels:
            num = _GradingValue(num_min, num_max, neutral,
                                accent=_CHANNEL_COLORS[ch], drag_span=box_span)
            num.setFixedValueWidth(box_width)
            num.setFixedHeight(box_height)
            num.layout().setContentsMargins(0, 0, 0, 2)
            num._edit.setFixedHeight(box_height - 4)
            num.setToolTip(ch)
            self._nums[ch] = num
            num_row.addWidget(num)
        num_row.addStretch(1)
        layout.addLayout(num_row)

        self._dial = KnurledJogWheel(min_width=140)
        self._dial.setToolTip(I18N.tr("grading.brightness_dial_tooltip",
            "Drag left/right to adjust master brightness; double-click to reset. Shift: fine."))
        dial_row = QHBoxLayout()
        dial_row.setContentsMargins(8, 0, 8, 0)
        dial_row.addWidget(self._dial)
        layout.addLayout(dial_row)

        for ch in self._channels:
            self._bars[ch].valueChanged.connect(lambda v, c=ch: self._on_channel(c, v))
            self._nums[ch].valueChanged.connect(lambda v, c=ch: self._on_channel(c, v))
            self._bars[ch].interaction_started.connect(self.interaction_started.emit)
            self._nums[ch].interaction_started.connect(self.interaction_started.emit)
            self._bars[ch].interaction_ended.connect(self._on_interaction_ended)
            self._nums[ch].interaction_ended.connect(self._on_interaction_ended)
        self._dial.deltaMoved.connect(self._on_dial_delta)
        self._dial.dragStarted.connect(self.interaction_started.emit)
        self._dial.dragEnded.connect(self._on_interaction_ended)
        self._dial.resetRequested.connect(self._on_dial_reset)

    def _make_bar(self, channel: str, bar_min: float, bar_max: float,
                  neutral: float) -> _ChannelBar:
        if self._is_offset:
            return _OffsetBar(channel, bar_min, bar_max, neutral)
        return _ChannelBar(channel, bar_min, bar_max, neutral)

    # === state ===

    def _on_channel(self, channel: str, value: float) -> None:
        if self._updating:
            return
        if self._math == "add":
            display = {ch: self._nums[ch].value() for ch in self._channels}
            display[channel] = value
            self.hue, self.sat, self.brightness = offset_display_to_hsb(
                display.get("R", 0.0), display.get("G", 0.0),
                display.get("B", 0.0))
        elif channel == "Y":
            multiplier = value + self._display_offset
            stops = math.log2(max(multiplier, 2.0 ** -6.0))
            self.brightness = math.copysign(math.sqrt(abs(stops) / 6.0), stops)
            self._rgb = ColorWheel.rgb_multipliers(self.hue, self.sat, self.brightness)
        else:
            r, g, b = self._rgb
            mult = value + self._display_offset
            if channel == "R":
                r = max(0.0, mult)
            elif channel == "G":
                g = max(0.0, mult)
            else:
                b = max(0.0, mult)
            self._rgb = (r, g, b)
            self.hue, self.sat, self.brightness = ColorWheel._rgb_to_hsv(r, g, b)
        self._refresh_displays()
        self._pending = (self.hue, self.sat, self.brightness)
        if not self._update_timer.isActive():
            self._update_timer.start()

    def _on_dial_delta(self, dx: float) -> None:
        if self._updating:
            return
        self.brightness = max(-1.0, min(1.0, self.brightness + dx *
                                        drag_speed_per_pixel(2.0, Qt.KeyboardModifier.NoModifier)))
        self._refresh_displays()
        self._pending = (self.hue, self.sat, self.brightness)
        if not self._update_timer.isActive():
            self._update_timer.start()

    def _on_dial_reset(self) -> None:
        if self.brightness == 0.0:
            return
        self.interaction_started.emit()
        self.brightness = 0.0
        self._refresh_displays()
        self.changed.emit(self.hue, self.sat, self.brightness)
        self.interaction_ended.emit()

    def _refresh_displays(self) -> None:
        self._updating = True
        if self._math == "add":
            rx, gy, bz = offset_hsb_to_display(self.hue, self.sat, self.brightness)
            values = {"R": rx, "G": gy, "B": bz}
        else:
            r, g, b = ColorWheel.rgb_multipliers(self.hue, self.sat, self.brightness)
            self._rgb = (r, g, b)
            values = {"Y": (r + g + b) / 3.0 - self._display_offset,
                      "R": r - self._display_offset,
                      "G": g - self._display_offset,
                      "B": b - self._display_offset}
        for ch in self._channels:
            self._bars[ch].setValue(values[ch])
            self._nums[ch].setValue(values[ch])
        self._updating = False

    def _emit_changed(self) -> None:
        if self._pending is not None:
            self.changed.emit(*self._pending)
            self._pending = None

    def _on_interaction_ended(self) -> None:
        self.flush_pending_changes()
        self.interaction_ended.emit()

    def flush_pending_changes(self) -> None:
        if self._update_timer.isActive():
            self._update_timer.stop()
            self._emit_changed()

    def set_hsv(self, hue: float, sat: float, brightness: float) -> None:
        if self._update_timer.isActive():
            self._update_timer.stop()
        self._pending = None
        self.hue = hue
        self.sat = sat
        self.brightness = brightness
        self._refresh_displays()

    def reset(self) -> None:
        if self._update_timer.isActive():
            self._update_timer.stop()
        self._pending = None
        if (self.hue, self.sat, self.brightness) != (0.0, 0.0, 0.0):
            self.interaction_started.emit()
        self.hue = 0.0
        self.sat = 0.0
        self.brightness = 0.0
        self._refresh_displays()
        self.changed.emit(0.0, 0.0, 0.0)
        self.interaction_ended.emit()

    def blockSignals(self, block: bool) -> bool:
        for ch in self._channels:
            self._bars[ch].blockSignals(block)
            self._nums[ch].blockSignals(block)
        self._dial.blockSignals(block)
        return super().blockSignals(block)


class ColorBarsPanel(QWidget):
    """Lift/Gamma/Gain/Offset bar groups laid out like Resolve's Primaries bars."""

    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(12)
        self.lift = _BarsColumn(I18N.tr("grading.lift", "暗部"), "lift")
        self.gamma = _BarsColumn(I18N.tr("grading.gamma", "中灰"), "gamma")
        self.gain = _BarsColumn(I18N.tr("grading.gain", "亮部"), "gain")
        self.offset = _BarsColumn(I18N.tr("grading.offset", "偏移"), "offset")
        # Offset 列暂时隐藏（与色轮页一致）；控件保留并照常参与状态
        # 同步 / flush / blockSignals，随时可恢复。列间不放纵向分割线
        # （对齐达芬奇：仅用横向细线区分功能区）。
        for column in (self.lift, self.gamma, self.gain):
            column.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Expanding)
            column.interaction_started.connect(self.interaction_started.emit)
            column.interaction_ended.connect(self.interaction_ended.emit)
            layout.addWidget(column, 1)
        self.offset.setParent(self)
        self.offset.setVisible(False)

    def columns(self) -> tuple[_BarsColumn, _BarsColumn, _BarsColumn,
                               _BarsColumn]:
        return self.lift, self.gamma, self.gain, self.offset

    def flush_pending_changes(self) -> None:
        for column in self.columns():
            column.flush_pending_changes()

    def blockSignals(self, block: bool) -> bool:
        for column in self.columns():
            column.blockSignals(block)
        return super().blockSignals(block)
