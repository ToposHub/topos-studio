"""Compact curve-type glyphs for the shared curve palette header."""
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QToolButton


class CurveModeButton(QToolButton):
    def __init__(self, mode: str, title: str, parent=None) -> None:
        super().__init__(parent)
        self.mode = mode
        self.setCheckable(True)
        self.setFixedSize(28, 24)
        self.setIcon(self._curve_icon(mode))
        self.setToolTip(title)
        self.setAccessibleName(title)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet('QToolButton { border: none; background: transparent; }'
                           'QToolButton:hover { background: #353539; }')

    @staticmethod
    def _curve_icon(mode: str) -> QIcon:
        pixmap = QPixmap(72, 72)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(4.0, 4.0)
        painter.setPen(QPen(QColor("white"), 1.3))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if mode == 'Custom':
            painter.drawRect(QRectF(1.5, 3.5, 15, 11))
            path = QPainterPath(QPointF(3, 13))
            path.cubicTo(8, 12, 8, 6, 15, 5)
            painter.drawPath(path)
        elif mode.startswith('Hue'):
            painter.drawEllipse(QRectF(2, 2, 14, 14))
            for angle in range(0, 360, 60):
                painter.save()
                painter.translate(9, 9)
                painter.rotate(angle)
                painter.drawLine(QPointF(0, -7), QPointF(0, -4))
                painter.restore()
            if mode == 'HueHue':
                painter.drawArc(QRectF(5, 5, 8, 8), 20 * 16, 250 * 16)
            elif mode == 'HueSat':
                painter.drawEllipse(QRectF(6, 6, 6, 6))
            else:
                painter.drawLine(QPointF(5, 9), QPointF(13, 9))
        else:
            path = QPainterPath(QPointF(8, 2))
            path.cubicTo(8, 6, 2, 8, 2, 12)
            path.cubicTo(2, 18, 14, 18, 14, 12)
            path.cubicTo(14, 8, 8, 4, 8, 2)
            painter.drawPath(path)
            if mode != 'LumSat':
                painter.drawLine(QPointF(10, 2), QPointF(17, 12))
            if mode == 'SatLum':
                painter.drawLine(QPointF(5, 12), QPointF(11, 12))
        painter.end()
        return QIcon(pixmap)
