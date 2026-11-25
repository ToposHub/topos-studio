"""State-aware tinting for monochrome Qt icons."""

from PyQt6.QtCore import QPointF, QRect, QRectF, QSize
from PyQt6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPixmap


class TintedIconEngine(QIconEngine):
    """Apply state colors to an icon while retaining its native DPI and alpha."""

    def __init__(self, icon: QIcon, *, idle: str, hover: str, active: str, disabled: str) -> None:
        super().__init__()
        self._icon = QIcon(icon)
        self._idle = idle
        self._hover = hover
        self._active = active
        self._disabled = disabled

    def clone(self) -> QIconEngine:
        return TintedIconEngine(self._icon, idle=self._idle, hover=self._hover,
                                active=self._active, disabled=self._disabled)

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        pixmap = self.scaledPixmap(rect.size(), mode, state, painter.device().devicePixelRatioF())
        painter.drawPixmap(rect, pixmap)

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:
        pixmap = self._icon.pixmap(size, scale, QIcon.Mode.Normal, state)
        if pixmap.isNull():
            return pixmap
        if mode == QIcon.Mode.Disabled:
            color = self._disabled
        elif state == QIcon.State.On or mode == QIcon.Mode.Selected:
            color = self._active
        elif mode == QIcon.Mode.Active:
            color = self._hover
        else:
            color = self._idle
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(QRectF(QPointF(), pixmap.deviceIndependentSize()), QColor(color))
        painter.end()
        return pixmap
