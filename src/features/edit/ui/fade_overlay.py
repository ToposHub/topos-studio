"""Drawing primitives shared by timeline clips and the mixer waveform."""
from __future__ import annotations

import math
from collections.abc import Callable

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen

FADE_HANDLE_SIZE = 6
FADE_HANDLE_HEIGHT = 9
FADE_HANDLE_HIT_RADIUS = 7
FADE_HANDLE_HIT_HEIGHT = 13


def paint_fade_ramp(
    painter: QPainter, band: QRectF, edge: float, span: float,
    gain_at: Callable[[float], float], *, outline: QColor | None = None,
    shade: QColor | None = None,
) -> None:
    """Paint the attenuation above the visible portion of a gain ramp."""
    if span == 0.0:
        return
    left = (band.left() - edge) / span
    right = (band.right() - edge) / span
    start = max(0.0, min(left, right))
    stop = min(1.0, max(left, right))
    if stop <= start:
        return
    steps = max(8, min(48, math.ceil(abs(span) * (stop - start))))
    curve = QPainterPath(QPointF(edge + span * start, band.bottom() - band.height() * gain_at(start)))
    for sample in range(1, steps + 1):
        progress = start + (stop - start) * sample / steps
        curve.lineTo(edge + span * progress, band.bottom() - band.height() * gain_at(progress))
    mask = QPainterPath(curve)
    mask.lineTo(edge + span * stop, band.top())
    mask.lineTo(edge + span * start, band.top())
    mask.closeSubpath()
    painter.fillPath(mask, shade if shade is not None else QColor(9, 12, 18, 115))
    if outline is not None:
        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(outline, 1.0))
        painter.drawPath(curve)
        painter.restore()


def paint_fade_tab(painter: QPainter, x: float, y: float, *, hover: bool = False) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(12, 12, 14, 220), 0.7))
    painter.setBrush(QColor(255, 255, 255, 242) if hover else QColor(246, 246, 246, 250))
    painter.drawRoundedRect(QRectF(x, y, FADE_HANDLE_SIZE, FADE_HANDLE_HEIGHT), 1.8, 1.8)
    painter.restore()


def paint_fade_curve_point(painter: QPainter, point: QPointF, color: QColor) -> None:
    painter.setPen(QPen(QColor(13, 17, 23), 1.0))
    painter.setBrush(color)
    painter.drawEllipse(point, 3.0, 3.0)
