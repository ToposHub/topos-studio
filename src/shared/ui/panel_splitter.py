"""Thin panel rules with a separate, generous native resize hit area.

The handle owns its line and the short branches of nested splitters that end
at that handle. Content widgets, headers and margins are never painted over.
"""

from __future__ import annotations

from weakref import WeakSet

from PyQt6 import sip
from PyQt6.QtCore import QByteArray, QPoint, QRect, Qt
from PyQt6.QtGui import QColor, QHideEvent, QPaintEvent, QResizeEvent, QShowEvent
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QSplitter, QSplitterHandle, QWidget

from src.shared.ui.style_sheet import C

PANEL_RESIZE_EXTENT = 8

_splitters: WeakSet[PanelSplitter] = WeakSet()


def splitter_rules(window: QWidget) -> list[tuple[Qt.Orientation, QRect]]:
    """Snapshot visible handle rules without walking editor content trees."""
    rules: list[tuple[Qt.Orientation, QRect]] = []
    for splitter in tuple(_splitters):
        if sip.isdeleted(splitter) or splitter.window() is not window or not splitter.isVisible():
            continue
        for index in range(1, splitter.count()):
            handle = splitter.handle(index)
            if handle is not None and handle.isVisible():
                origin = handle.mapTo(window, QPoint())
                rect = QRect(origin, handle.size())
                if splitter.orientation() == Qt.Orientation.Horizontal:
                    rect = QRect(rect.left() + (rect.width() - 1) // 2,
                                 rect.top(), 1, rect.height())
                else:
                    rect = QRect(rect.left(),
                                 rect.top() + (rect.height() - 1) // 2,
                                 rect.width(), 1)
                if window.rect().intersects(rect):
                    rules.append((splitter.orientation(), rect))
    return rules


class _PanelSplitterHandle(QSplitterHandle):
    def __init__(self, orientation: Qt.Orientation, parent: QSplitter) -> None:
        super().__init__(orientation, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        color = QColor(C.PANEL_DIVIDER)
        vertical = self.orientation() == Qt.Orientation.Horizontal
        center = (self.width() - 1) // 2 if vertical else (self.height() - 1) // 2
        if vertical:
            painter.fillRect(center, 0, 1, self.height(), color)
        else:
            painter.fillRect(0, center, self.width(), 1, color)

        # A nested rule that actually reaches this gutter gets one branch to
        # its center. Rules separated by a toolbar or padding do not qualify.
        window = self.window()
        origin = self.mapTo(window, QPoint())
        bounds = QRect(origin, self.size())
        for orientation, rule in splitter_rules(window):
            if orientation == self.orientation():
                continue
            if vertical and bounds.top() <= rule.top() <= bounds.bottom():
                y = rule.top() - origin.y()
                if rule.right() == bounds.left() - 1:
                    painter.fillRect(0, y, center + 1, 1, color)
                elif rule.left() == bounds.right() + 1:
                    painter.fillRect(center, y, self.width() - center, 1, color)
            elif not vertical and bounds.left() <= rule.left() <= bounds.right():
                x = rule.left() - origin.x()
                if rule.bottom() == bounds.top() - 1:
                    painter.fillRect(x, 0, 1, center + 1, color)
                elif rule.top() == bounds.bottom() + 1:
                    painter.fillRect(x, center, 1, self.height() - center, color)


class PanelSplitter(QSplitter):
    """Native QSplitter dragging with an eight-pixel target and a 1px rule."""

    def __init__(
        self,
        orientation: Qt.Orientation = Qt.Orientation.Horizontal,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(orientation, parent)
        self.setHandleWidth(PANEL_RESIZE_EXTENT)
        _splitters.add(self)
        self.splitterMoved.connect(self._refresh_rules)

    def createHandle(self) -> QSplitterHandle:
        return _PanelSplitterHandle(self.orientation(), self)

    def restoreState(self, state: QByteArray) -> bool:
        restored = super().restoreState(state)
        # Qt persists handleWidth alongside pane sizes. Old 1px states must
        # not shrink the new hit target after a workspace has been restored.
        self.setHandleWidth(PANEL_RESIZE_EXTENT)
        self._refresh_rules()
        return restored

    def setSizes(self, sizes: list[int]) -> None:
        super().setSizes(sizes)
        # Programmatic layout/default-size changes do not emit splitterMoved.
        self._refresh_rules()

    def _refresh_rules(self, *_args: int) -> None:
        window = self.window()
        for splitter in tuple(_splitters):
            if not sip.isdeleted(splitter) and splitter.window() is window:
                for index in range(1, splitter.count()):
                    handle = splitter.handle(index)
                    if handle is not None:
                        handle.update()
        window.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._refresh_rules()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._refresh_rules()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self._refresh_rules()
