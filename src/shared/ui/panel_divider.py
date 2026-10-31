"""Cross-platform custom borders for dock panels.

Qt's ``QMainWindow::separator`` is a style subcontrol and is not painted at
the dock boundary consistently by every platform style. The overlay below
keeps the visible 1px divider owned by the application instead of the native
window style, while remaining transparent to mouse input.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QDockWidget, QWidget

from src.shared.ui.style_sheet import C

__all__ = ["PanelDockWidget"]


class _PanelDividerOverlay(QWidget):
    """Paint the dock outline above its content without stealing input."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("PanelDividerOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event) -> None:  # noqa: D401 - Qt override
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        pen = QPen(QColor(C.PANEL_DIVIDER))
        pen.setWidth(1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))


class PanelDockWidget(QDockWidget):
    """QDockWidget with an application-owned, platform-neutral 1px outline."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._divider_overlay = _PanelDividerOverlay(self)
        self.refresh_divider_overlay()

    def refresh_divider_overlay(self) -> None:
        self._divider_overlay.setGeometry(self.rect())
        self._divider_overlay.show()
        self._divider_overlay.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.refresh_divider_overlay()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_divider_overlay()
