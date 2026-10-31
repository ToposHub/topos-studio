from PyQt6.QtWidgets import QSlider
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor

from src.shared.ui.style_sheet import gradient_slider_groove_style

class ClickableSlider(QSlider):
    doubleClicked = pyqtSignal()

    def __init__(self, orientation=Qt.Orientation.Horizontal, default_val=0, parent=None) -> None:
        super().__init__(orientation, parent)
        self.default_val = default_val

    def mouseDoubleClickEvent(self, event):
        self.setValue(self.default_val)
        self.doubleClicked.emit()

    def set_gradient_stops(self, stops):
        """
        Sets a linear gradient background for the slider groove.
        :param stops: List of tuples (position, color_hex_or_qcolor)
        """
        grad_str = "qlineargradient(x1:0, y1:0, x2:1, y2:0, "
        for pos, color in stops:
            if isinstance(color, QColor):
                color_str = f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"
            else:
                color_str = color
            grad_str += f"stop:{pos} {color_str}, "
        grad_str = grad_str.rstrip(", ") + ")"
        
        self.setStyleSheet(gradient_slider_groove_style(grad_str))
