"""
Full-height mixer strip boundary using the shared panel divider.
"""

from PyQt6.QtWidgets import QWidget, QSizePolicy
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QPainter, QColor, QPaintEvent

from src.shared.ui.style_sheet import C


class MixerSeparator(QWidget):
    """混音器轨道分隔线"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Expanding
        )
        self.setFixedWidth(1)

    def sizeHint(self) -> QSize:
        return QSize(1, 10)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(C.PANEL_DIVIDER))
        painter.drawRect(self.rect())
