"""One-pixel panel boundaries with Qt's overlapping native resize target.

Only the rule occupies layout space. Qt expands a narrow splitter handle over
the adjacent panes for dragging, while its contents/mask stay on the boundary.
Panel backgrounds and header rules therefore meet at the visible black line.
"""

from __future__ import annotations

from weakref import WeakSet

from PyQt6 import sip
from PyQt6.QtCore import QByteArray, QRect, Qt
from PyQt6.QtGui import QColor, QHideEvent, QPaintEvent, QResizeEvent, QShowEvent
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QSplitter, QSplitterHandle, QWidget

from src.shared.ui.style_sheet import C

PANEL_RULE_WIDTH = 1

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
                content = handle.contentsRect()
                rect = QRect(handle.mapTo(window, content.topLeft()), content.size())
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
        painter.fillRect(self.contentsRect(), color)


class PanelSplitter(QSplitter):
    """A 1px layout seam; Qt supplies the wider overlapping drag target."""

    def __init__(
        self,
        orientation: Qt.Orientation = Qt.Orientation.Horizontal,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(orientation, parent)
        self.setHandleWidth(PANEL_RULE_WIDTH)
        _splitters.add(self)
        self.splitterMoved.connect(self._refresh_rules)

    def createHandle(self) -> QSplitterHandle:
        return _PanelSplitterHandle(self.orientation(), self)

    def restoreState(self, state: QByteArray) -> bool:
        restored = super().restoreState(state)
        # Old states persist the former 8px gutter. Keep restored panel
        # surfaces adjacent instead of reintroducing the parent background.
        self.setHandleWidth(PANEL_RULE_WIDTH)
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
