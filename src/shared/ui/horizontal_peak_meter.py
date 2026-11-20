"""Shared paint style for compact horizontal audio peak meters."""

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QLinearGradient, QPainter

from src.shared.ui.style_sheet import C


PEAK_GREEN = "#4CD490"
PEAK_AMBER = "#F0A848"
PEAK_RED = "#F26D6D"


def paint_horizontal_peak_bar(painter: QPainter, rect: QRectF, db: float, floor_db: float) -> None:
    """Paint a filled meter lane with green, amber and red peak zones."""
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(C.MIXER_WELL_BG))
    painter.drawRoundedRect(rect, 1.5, 1.5)
    fraction = min(1.0, max(0.0, (db - floor_db) / -floor_db))
    if fraction > 0.0:
        gradient = QLinearGradient(rect.left(), 0, rect.right(), 0)
        gradient.setColorAt(0.0, QColor(PEAK_GREEN))
        gradient.setColorAt(0.52, QColor(PEAK_GREEN))
        gradient.setColorAt(0.80, QColor(PEAK_AMBER))
        gradient.setColorAt(1.0, QColor(PEAK_RED))
        fill = QRectF(rect)
        fill.setWidth(rect.width() * fraction)
        painter.setBrush(gradient)
        painter.drawRoundedRect(fill, 1.5, 1.5)
