"""Rotary knob face shared by the mixer console and timeline."""

import math

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen


def paint_rotary_knob(
    painter: QPainter, rect: QRectF, fraction: float, accent: QColor, *,
    bipolar: bool, highlighted: bool,
) -> None:
    """Paint the recessed arc, matte cap and pointer for a normalized value."""
    center = rect.center()
    size = min(rect.width(), rect.height())
    # Preserve the console proportions and scale the strokes in compact track rows.
    scale = min(1.0, size / 22.0)
    arc_radius = size / 2.0 - 2.6 * scale
    arc_rect = QRectF(
        center.x() - arc_radius, center.y() - arc_radius, 2.0 * arc_radius, 2.0 * arc_radius,
    )
    start_angle = 225.0
    total_span = -270.0
    angle = start_angle + fraction * total_span
    arc_origin = 90.0 if bipolar else start_angle
    span = angle - arc_origin

    painter.save()
    painter.setPen(QPen(QColor("#141417"), 2.1 * scale, cap=Qt.PenCapStyle.RoundCap))
    painter.drawArc(arc_rect, int(start_angle * 16), int(total_span * 16))
    if abs(span) > 0.5:
        painter.setPen(QPen(accent, 2.0 * scale, cap=Qt.PenCapStyle.RoundCap))
        painter.drawArc(arc_rect, int(arc_origin * 16), int(span * 16))

    cap_radius = arc_radius - 3.4 * scale
    painter.setPen(QPen(QColor("#111114"), 1.1 * scale))
    painter.setBrush(QColor("#8E9096" if highlighted else "#7A7C82"))
    painter.drawEllipse(center, cap_radius, cap_radius)

    radians = math.radians(angle)
    direction = QPointF(math.cos(radians), -math.sin(radians))
    painter.setPen(QPen(QColor("#141416"), 1.7 * scale, cap=Qt.PenCapStyle.RoundCap))
    painter.drawLine(
        center + direction * (cap_radius * 0.25),
        center + direction * (cap_radius - 1.2 * scale),
    )
    painter.restore()
