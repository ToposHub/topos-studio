"""A transparent mouse target over the native dock divider."""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt6.QtGui import QHideEvent, QMouseEvent
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QWidget


class DockResizeHandle(QWidget):
    HIT_RADIUS = 5
    drag_finished = pyqtSignal()

    def __init__(self, host: QMainWindow, orientation: Qt.Orientation) -> None:
        super().__init__(host)
        self._host = host
        self._orientation = orientation
        self._docks: tuple[QDockWidget, QDockWidget] | None = None
        self._drag: tuple[int, int, int] | None = None
        self._tool_tab_height = 0
        self.setStyleSheet("background: transparent; border: none;")
        self.setCursor(Qt.CursorShape.SplitHCursor if self._horizontal else Qt.CursorShape.SplitVCursor)
        self.hide()

    @property
    def is_dragging(self) -> bool:
        return self._drag is not None

    @property
    def _horizontal(self) -> bool:
        return self._orientation == Qt.Orientation.Horizontal

    def place(self, geometry: QRect, before: QDockWidget, after: QDockWidget,
              *, tool_tab_height: int = 0) -> None:
        self._docks = (before, after)
        self._tool_tab_height = tool_tab_height
        self.setGeometry(geometry)
        self.raise_()
        self.show()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._docks is None:
            super().mousePressEvent(event)
            return
        before, after = self._docks
        origin = self._position(event)
        self._drag = ((origin, before.width(), after.width()) if self._horizontal
                      else (origin, before.height(), after.height() + self._tool_tab_height))
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag is None or self._docks is None:
            super().mouseMoveEvent(event)
            return
        origin, before, after = self._drag
        delta = self._position(event) - origin
        self._host.resizeDocks(self._docks, [max(1, before + delta), max(1, after - delta)],
                               self._orientation)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag is not None and event.button() == Qt.MouseButton.LeftButton:
            origin = self._drag[0]
            self._drag = None
            if self._position(event) != origin:
                self.drag_finished.emit()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        self._drag = None
        super().hideEvent(event)

    def _position(self, event: QMouseEvent) -> int:
        position: QPoint = event.globalPosition().toPoint()
        return position.x() if self._horizontal else position.y()

    def place_column(self, before: QDockWidget | None, after: QDockWidget | None,
                     *, include_tab_strip: bool = False) -> None:
        if before is None or after is None:
            self.hide()
            return
        left, right = before.geometry(), after.geometry()
        # Only cover adjacent columns in the same row. A user may move a main dock.
        top, bottom = max(left.top(), right.top()), min(left.bottom(), right.bottom())
        if not 0 <= right.left() - left.right() <= 2 or bottom <= top:
            self.hide()
            return
        if include_tab_strip and abs(left.top() - right.top()) <= 32:
            top = min(left.top(), right.top())
        radius = self.HIT_RADIUS
        self.place(QRect(right.left() - radius, top, radius * 2, bottom - top + 1),
                   before, after)
