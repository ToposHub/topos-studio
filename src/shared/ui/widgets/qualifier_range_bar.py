"""Interactive qualifier range with neutral handles and feathered falloff."""

from PyQt6.QtWidgets import QWidget, QSizePolicy
from PyQt6.QtCore import Qt, QRectF, pyqtSignal, QPointF, QEvent
from PyQt6.QtGui import (
    QPainter, QLinearGradient, QColor, QPen, QPolygonF, QPaintEvent, QMouseEvent,
)


class QualifierRangeBar(QWidget):
    BAR_HEIGHT = 26
    # Display violet at both ends, like the reference palette. The stored hue
    # coordinate remains unchanged (the keyer/picker put red at model 0.5).
    HUE_DISPLAY_ORIGIN = 0.25
    range_changed = pyqtSignal(float, float, float, float)
    hue_changed = pyqtSignal(float, float, float)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()

    def __init__(self, type: str = "hue", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.type = type
        self.setFixedHeight(self.BAR_HEIGHT)
        self.setMinimumWidth(100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.low = 0.0
        self.high = 1.0
        self.soft_low = 0.0
        self.soft_high = 0.0
        self.center = 0.5
        self.hue_width = 1.0
        self.soft = 0.1
        self.active_handle: str | None = None
        self.mouse_pressed = False
        self.hover_handle: str | None = None
        self._range_anchor = 0.0
        self._hue_center_anchor = 0.0
        self._hue_drag_position = 0.0

    def set_range(self, low: float, high: float, soft_low: float = 0.0,
                  soft_high: float = 0.0) -> None:
        self.low, self.high = low, high
        self.soft_low, self.soft_high = soft_low, soft_high
        self.update()

    def set_hue_params(self, center: float, width: float, soft: float) -> None:
        self.center, self.hue_width, self.soft = center, width, soft
        self.update()

    def _bar_rect(self) -> QRectF:
        return QRectF(self.rect()).adjusted(3.0, 1.0, -3.0, -1.0)

    def _position_value(self, pos: QPointF) -> float:
        return self._model_value(self._position_fraction(pos))

    def _position_fraction(self, pos: QPointF) -> float:
        rect = self._bar_rect()
        return max(0.0, min(1.0, (pos.x() - rect.left()) / max(rect.width(), 1.0)))

    def _model_value(self, fraction: float) -> float:
        return (fraction + self.HUE_DISPLAY_ORIGIN) % 1.0 if self.type == 'hue' else fraction

    def _display_position(self, value: float) -> float:
        return (value - self.HUE_DISPLAY_ORIGIN) % 1.0 if self.type == 'hue' else value

    def _handle_positions(self) -> dict[str, float]:
        if self.type == 'hue':
            # A full circle has two visible ends, even though both have the
            # same circular coordinate. Do not collapse them into one handle.
            if self.hue_width >= 1.0:
                return {'start': 0.0, 'end': 1.0,
                        'center': self._display_position(self.center)}
            start = (self.center - self.hue_width * 0.5) % 1.0
            end = (self.center + self.hue_width * 0.5) % 1.0
            return {
                'start': self._display_position(start),
                'end': self._display_position(end),
                'center': self._display_position(self.center),
                'soft_start': self._display_position(start - self.soft),
                'soft_end': self._display_position(end + self.soft),
            }
        return {
            'low': self.low, 'high': self.high,
            'soft_low': max(0.0, self.low - self.soft_low),
            'soft_high': min(1.0, self.high + self.soft_high),
        }

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._bar_rect()
        self.draw_background(painter, rect)
        painter.save()
        painter.setClipRect(rect)
        self.draw_overlay(painter, rect)
        if not self.isEnabled():
            painter.fillRect(rect, QColor(39, 39, 43, 180))
        painter.restore()
        painter.setPen(QPen(QColor('#111113'), 2.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 2.0, 2.0)
        self._draw_handles(painter, rect)

    def draw_background(self, painter: QPainter, rect: QRectF) -> None:
        gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        if self.type == 'hue':
            for i in range(25):
                position = i / 24.0
                hue = (self._model_value(position) + 0.5) % 1.0
                gradient.setColorAt(position, QColor.fromHsvF(hue, 1.0, 1.0))
        elif self.type == 'sat':
            gradient.setColorAt(0.0, QColor('#7b8274'))
            gradient.setColorAt(1.0, QColor('#42df00'))
        else:
            gradient.setColorAt(0.0, QColor('#090b08'))
            gradient.setColorAt(1.0, QColor('#8a9b80'))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawRoundedRect(rect, 2.0, 2.0)

    @staticmethod
    def _falloff(distance: float, softness: float) -> float:
        if distance <= 0.0:
            return 1.0
        if softness <= 0.0:
            return 0.0
        t = min(1.0, distance / softness)
        return 1.0 - t * t * (3.0 - 2.0 * t)

    def draw_overlay(self, painter: QPainter, rect: QRectF) -> None:
        # Smooth falloff dims the unselected spectrum without tinting it orange.
        gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        for i in range(257):
            position = i / 256.0
            if self.type == 'hue':
                distance = abs(self._model_value(position) - self.center)
                distance = min(distance, 1.0 - distance)
                selected = self._falloff(distance - self.hue_width * 0.5, self.soft)
            else:
                selected = self._falloff(self.low - position, self.soft_low)
                selected *= self._falloff(position - self.high, self.soft_high)
            gradient.setColorAt(position, QColor(0, 0, 0, round(190 * (1.0 - selected))))
        painter.fillRect(rect, gradient)

    def _draw_handles(self, painter: QPainter, rect: QRectF) -> None:
        for name, position in self._handle_positions().items():
            x = rect.left() + position * rect.width()
            active = name == self.active_handle or name == self.hover_handle
            color = QColor('#ffffff' if active else '#d7d7db')
            if not self.isEnabled():
                color = QColor('#737379')
            if name == 'center':
                if active and self.hue_width < 1.0:
                    painter.setPen(QPen(color, 1.0, Qt.PenStyle.DotLine))
                    painter.drawLine(QPointF(x, rect.top() + 3), QPointF(x, rect.bottom() - 3))
                continue
            if name.startswith('soft'):
                painter.setPen(QPen(QColor('#111113'), 1.0))
                painter.setBrush(color)
                painter.drawPolygon(QPolygonF([
                    QPointF(x, rect.bottom() - 4), QPointF(x - 3, rect.bottom()),
                    QPointF(x + 3, rect.bottom()),
                ]))
            else:
                painter.setPen(QPen(QColor('#111113'), 4.0))
                painter.drawLine(QPointF(x, rect.top() + 3), QPointF(x, rect.bottom() - 4))
                painter.setPen(QPen(color, 1.0))
                painter.drawLine(QPointF(x, rect.top() + 3), QPointF(x, rect.bottom() - 4))
                painter.drawLine(QPointF(x - 1.5, rect.top() + 3), QPointF(x + 1.5, rect.top() + 3))

    def _get_handle_at(self, pos: QPointF) -> str | None:
        positions = self._handle_positions()
        rect = self._bar_rect()
        value = self._position_fraction(pos)
        tolerance = 7.0 / max(rect.width(), 1.0)
        # Feather handles sit along the bottom edge. This keeps them reachable
        # even at zero softness, where they share an x position with an edge.
        prefer_soft = pos.y() >= rect.bottom() - 5
        candidates = sorted(positions, key=lambda name: (
            name == 'center', name.startswith('soft') != prefer_soft))
        for name in candidates:
            if abs(value - positions[name]) <= tolerance:
                return name
        if self.type != 'hue' and self.low < value < self.high:
            return 'range'
        if self.type == 'hue':
            distance = abs(self._model_value(value) - self.center)
            if min(distance, 1.0 - distance) <= self.hue_width * 0.5:
                return 'center'
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.mouse_pressed = True
        self.active_handle = self._get_handle_at(event.position())
        value = self._position_value(event.position())
        self.interaction_started.emit()
        self._hue_drag_position = self._position_fraction(event.position())
        self._hue_center_anchor = value - self.center if self.active_handle == 'center' else 0.0
        if not self.active_handle:
            if self.type == 'hue':
                self.active_handle = 'center'
            else:
                self.active_handle = 'range'
                self._range_anchor = (self.high - self.low) * 0.5
        elif self.active_handle == 'range':
            self._range_anchor = value - self.low
        self._handle_mouse(event.position())
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self.hover_handle = self._get_handle_at(event.position())
        cursor = Qt.CursorShape.ArrowCursor
        if self.hover_handle:
            cursor = (Qt.CursorShape.SizeAllCursor if self.hover_handle in ('center', 'range')
                      else Qt.CursorShape.SizeHorCursor)
        self.setCursor(cursor)
        if self.mouse_pressed:
            self._handle_mouse(event.position())
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        self.hover_handle = None
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.mouse_pressed:
            self.mouse_pressed = False
            self.active_handle = None
            self.interaction_ended.emit()
            self.update()

    def _handle_mouse(self, pos: QPointF) -> None:
        value = self._position_value(pos)
        if self.type == 'hue':
            fraction = self._position_fraction(pos)
            difference = fraction - self._hue_drag_position
            if self.active_handle == 'center':
                center = value - self._hue_center_anchor
                self.center = center if 0.0 <= center <= 1.0 else center % 1.0
            elif self.active_handle in ('start', 'end'):
                direction = -1.0 if self.active_handle == 'start' else 1.0
                width = max(0.01, min(1.0, self.hue_width + direction * difference * 2.0))
                if self.hue_width >= 1.0 and width < 1.0:
                    # Every center describes the same full circle. When its
                    # visible end is first narrowed, keep that edge under the
                    # pointer rather than jumping to the stored circular seam.
                    self.center = (self.HUE_DISPLAY_ORIGIN + 0.5) % 1.0
                self.hue_width = width
            elif self.active_handle in ('soft_start', 'soft_end'):
                direction = -1.0 if self.active_handle == 'soft_start' else 1.0
                self.soft = max(0.0, min(1.0, self.soft + direction * difference))
            self._hue_drag_position = fraction
            self.hue_changed.emit(self.center, self.hue_width, self.soft)
        else:
            if self.active_handle == 'low':
                self.low = max(0.0, min(value, self.high - 0.01))
            elif self.active_handle == 'high':
                self.high = min(1.0, max(value, self.low + 0.01))
            elif self.active_handle == 'soft_low':
                self.soft_low = max(0.0, self.low - value)
            elif self.active_handle == 'soft_high':
                self.soft_high = max(0.0, value - self.high)
            elif self.active_handle == 'range':
                width = self.high - self.low
                self.low = max(0.0, min(1.0 - width, value - self._range_anchor))
                self.high = self.low + width
            self.range_changed.emit(self.low, self.high, self.soft_low, self.soft_high)
