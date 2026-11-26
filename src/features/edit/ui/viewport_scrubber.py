"""Resolve-style monitor scrubber drawing and direct-position gestures.

The helpers can be used by the existing SourceSlider without replacing it, so
its sliderPressed/Released connections and In/Out state retain their owner.
Only standard QSlider state and signals are changed; no media work lives here.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent, QPaintEvent, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QSlider, QWidget

from src.features.edit.ui.timeline_edit_affordances import paint_range_grip
from src.shared.video.viewer_colors import VIEWER_TRANSPORT_BACKGROUND
from src.shared.ui.widgets.scrubber_gesture import (
    cancel_monitor_scrub, scrubber_mouse_move, scrubber_mouse_press,
    scrubber_mouse_release, scrubber_track_bounds,
)


def _value_x(slider: QSlider, value: int, left: float, right: float) -> float:
    span = slider.maximum() - slider.minimum()
    if span <= 0:
        return left
    fraction = (max(slider.minimum(), min(slider.maximum(), value)) - slider.minimum()) / span
    if slider.invertedAppearance():
        fraction = 1.0 - fraction
    return left + fraction * (right - left)


def _mark(slider: QSlider, name: str) -> int | None:
    value = getattr(slider, name, None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def monitor_range_marks(slider: QSlider) -> tuple[int | None, int | None]:
    preview = getattr(slider, "_range_preview", None)
    return preview if preview is not None else (_mark(slider, "_in_point"), _mark(slider, "_out_point"))


def monitor_range_grip_at(slider: QSlider, pos: QPointF) -> str | None:
    """Find the nearest endpoint within the lower part of the scrubber."""
    if not 11.0 <= pos.y() <= slider.height():
        return None
    left, right = scrubber_track_bounds(slider)
    start, end = monitor_range_marks(slider)
    x_in = _value_x(slider, slider.minimum() if start is None else start, left, right)
    x_out = _value_x(slider, slider.maximum() if end is None else end, left, right)
    in_distance, out_distance = abs(pos.x() - x_in), abs(pos.x() - x_out)
    if min(in_distance, out_distance) > 12.0:
        return None
    if x_in == x_out:
        on_left = pos.x() <= x_in
        return "in" if on_left != slider.invertedAppearance() else "out"
    return "in" if in_distance <= out_distance else "out"


def paint_monitor_scrubber(slider: QSlider) -> None:
    """Draw the 24px monitor ruler using logical pixels at the native DPR."""
    painter = QPainter(slider)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.fillRect(slider.rect(), QColor(VIEWER_TRANSPORT_BACKGROUND))
    left, right = scrubber_track_bounds(slider)
    groove = QRectF(left, 11.0, max(0.0, right - left), 4.0)
    painter.setPen(QPen(QColor("#0c0c0f"), 1.0))
    painter.setBrush(QColor("#151518"))
    painter.drawRoundedRect(groove, 1.0, 1.0)

    in_point, out_point = monitor_range_marks(slider)
    first = _value_x(slider, in_point if in_point is not None else slider.minimum(), left, right)
    last = _value_x(slider, out_point if out_point is not None else slider.maximum(), left, right)
    if in_point is not None or out_point is not None:
        painter.fillRect(QRectF(min(first, last), 12.0, max(1.0, abs(last - first)), 2.0), QColor("#707074"))

    current_x = _value_x(slider, slider.value(), left, right)
    current_pen = QPen(QColor("#dc5b50" if slider.isEnabled() else "#72514f"), 1.0)
    current_pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    painter.setPen(current_pen)
    painter.setBrush(QColor(VIEWER_TRANSPORT_BACKGROUND))
    flag = QPainterPath(QPointF(current_x - 4.0, 3.0))
    flag.lineTo(current_x + 4.0, 3.0)
    flag.lineTo(current_x + 4.0, 7.0)
    flag.lineTo(current_x, 10.0)
    flag.lineTo(current_x - 4.0, 7.0)
    flag.closeSubpath()
    painter.drawPath(flag)
    painter.drawLine(QPointF(current_x, 10.0), QPointF(current_x, 17.0))
    for edge, x in (("in", first), ("out", last)):
        paint_range_grip(
            painter, x, 15.5, active=edge == getattr(slider, "_range_hover_edge", None),
            enabled=slider.isEnabled(),
        )
    painter.end()


class MonitorScrubber(QSlider):
    """Horizontal preview ruler with ordinary QSlider keyboard semantics."""

    scrub_started_at = pyqtSignal(int)
    scrub_finished_at = pyqtSignal(int)
    scrub_cancelled = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setFixedHeight(24)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setTracking(True)
        self._in_point: int | None = None
        self._out_point: int | None = None

    def set_in_out_points(self, in_point: int | None, out_point: int | None) -> None:
        """Update the displayed range without changing the playhead."""
        if (in_point, out_point) != (self._in_point, self._out_point):
            self._in_point, self._out_point = in_point, out_point
            self.update()

    def event(self, event: QEvent) -> bool:
        if event.type() in {QEvent.Type.Hide, QEvent.Type.UngrabMouse} or (
            event.type() == QEvent.Type.EnabledChange and not self.isEnabled()
        ):
            was_dragging = self.isSliderDown()
            cancel_monitor_scrub(self)
            if was_dragging:
                self.scrub_cancelled.emit()
        return super().event(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        paint_monitor_scrubber(self)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not scrubber_mouse_press(self, event, on_start=self.scrub_started_at.emit):
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not scrubber_mouse_move(self, event):
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if scrubber_mouse_release(self, event):
            self.scrub_finished_at.emit(self.value())
        else:
            super().mouseReleaseEvent(event)
