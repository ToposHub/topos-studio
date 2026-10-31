"""
Mixer Separator - 复刻 Kdenlive MixerSeparator

轨道之间的分隔线，使用渐变淡入淡出效果。
"""

from PyQt6.QtWidgets import QWidget, QSizePolicy
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QPainter, QColor


class MixerSeparator(QWidget):
    """混音器轨道分隔线"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Expanding
        )
        self.setFixedWidth(1)

    def sizeHint(self) -> QSize:
        return QSize(1, 10)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        # 使用接近面板暗阶的细线，只保留层次而不制造割裂感。
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#202024"))
        painter.drawRect(self.rect())
