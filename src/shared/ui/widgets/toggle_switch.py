"""
ToggleSwitch - Pill-shaped toggle switch widget.

A reusable on/off switch with sliding circle indicator,
used as the standard enable/disable control across all panels.

Style colors are defined in src/shared/ui/style_sheet.py (class C).
Modify C.TOGGLE_PILL_BG / C.TOGGLE_CIRCLE_ON / C.TOGGLE_CIRCLE_OFF
to change the toggle appearance globally.
"""

from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QBrush

from src.shared.ui.style_sheet import C


class ToggleSwitch(QWidget):
    """
    Pill-shaped toggle switch with sliding circle indicator.

    Matches the style shown in the screenshot:
    - Dark pill background when off, orange circle indicator
    - Click to toggle, emits toggled signal
    """

    toggled = pyqtSignal(bool)

    def __init__(self, checked: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._checked = checked
        self.setFixedSize(32, 18)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def isChecked(self) -> bool:
        return self._checked

    def isDown(self) -> bool:
        """兼容 QPushButton.isDown() 接口。ToggleSwitch 是即时切换，无按下状态。"""
        return False

    def setChecked(self, checked: bool):
        if self._checked != checked:
            self._checked = checked
            self.update()

    def toggle(self):
        self.setChecked(not self._checked)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._checked = not self._checked
            self.update()
            self.toggled.emit(self._checked)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()
        padding = 2
        pill_h = h - padding * 2
        pill_w = w - padding * 2

        # Pill background
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(C.TOGGLE_PILL_BG)))
        painter.drawRoundedRect(padding, padding, pill_w, pill_h, pill_h / 2, pill_h / 2)

        # Circle indicator
        circle_size = pill_h - 2
        if self._checked:
            painter.setBrush(QBrush(QColor(C.TOGGLE_CIRCLE_ON)))
            cx = w - padding - circle_size - 1
        else:
            painter.setBrush(QBrush(QColor(C.TOGGLE_CIRCLE_OFF)))
            cx = padding + 1
        cy = padding + 1
        painter.drawEllipse(int(cx), int(cy), int(circle_size), int(circle_size))
