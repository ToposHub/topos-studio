"""Panel boundaries with Qt's native resize targets.

Only the rule occupies layout space. Qt expands a narrow splitter handle over
the adjacent panes for dragging, while its contents/mask stay on the boundary.
Panel backgrounds and header rules therefore meet at the visible black line.
Tools can opt into a dedicated, wider resize grip.
"""

from __future__ import annotations

from weakref import WeakSet

from PyQt6 import sip
from PyQt6.QtCore import QByteArray, QEvent, QRect, Qt
from PyQt6.QtGui import QColor, QEnterEvent, QHideEvent, QPaintEvent, QResizeEvent, QShowEvent
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QSplitter, QSplitterHandle, QWidget

from src.shared.ui.style_sheet import C

PANEL_RULE_WIDTH = 1
_PANEL_GRIP_WIDTH = 9

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
                content = handle.rule_rect()
                rect = QRect(handle.mapTo(window, content.topLeft()), content.size())
                if window.rect().intersects(rect):
                    rules.append((splitter.orientation(), rect))
    return rules


class _PanelSplitterHandle(QSplitterHandle):
    def __init__(self, orientation: Qt.Orientation, parent: PanelSplitter) -> None:
        super().__init__(orientation, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    def rule_rect(self) -> QRect:
        rect = self.contentsRect()
        if self.splitter().handleWidth() <= PANEL_RULE_WIDTH:
            return rect
        if self.orientation() == Qt.Orientation.Horizontal:
            return QRect(rect.center().x(), rect.top(), PANEL_RULE_WIDTH, rect.height())
        return QRect(rect.left(), rect.center().y(), rect.width(), PANEL_RULE_WIDTH)

    def paintEvent(self, event: QPaintEvent) -> None:
        del event
        painter = QPainter(self)
        if self.splitter().handleWidth() > PANEL_RULE_WIDTH:
            rect = self.contentsRect()
            hovered = self.underMouse() and self.isEnabled()
            painter.fillRect(rect, QColor(C.BG_ACTIVE if hovered else C.BG_PANEL))
            painter.fillRect(self.rule_rect(), QColor(C.PANEL_DIVIDER))
            center = rect.center()
            color = QColor(C.ORANGE_HOVER if hovered else C.TEXT_HINT)
            for offset in (-5, 0, 5):
                if self.orientation() == Qt.Orientation.Horizontal:
                    painter.fillRect(center.x() - 1, center.y() + offset - 1, 3, 3, color)
                else:
                    painter.fillRect(center.x() + offset - 1, center.y() - 1, 3, 3, color)
        else:
            painter.fillRect(self.contentsRect(), QColor(C.PANEL_DIVIDER))

    def enterEvent(self, event: QEnterEvent) -> None:
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        super().leaveEvent(event)
        self.update()


class PanelSplitter(QSplitter):
    """A single-pixel seam by default, with an optional visible resize grip."""

    def __init__(
        self,
        orientation: Qt.Orientation = Qt.Orientation.Horizontal,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(orientation, parent)
        self.resize_grip_enabled = False
        self.setHandleWidth(PANEL_RULE_WIDTH)
        _splitters.add(self)
        self.splitterMoved.connect(self._refresh_rules)

    def createHandle(self) -> _PanelSplitterHandle:
        return _PanelSplitterHandle(self.orientation(), self)

    def set_resize_grip(self, enabled: bool) -> None:
        if self.resize_grip_enabled == enabled:
            return
        self.resize_grip_enabled = enabled
        self._apply_handle_width()
        self._refresh_rules()

    def _apply_handle_width(self) -> None:
        self.setHandleWidth(_PANEL_GRIP_WIDTH if self.resize_grip_enabled else PANEL_RULE_WIDTH)

    def restoreState(self, state: QByteArray) -> bool:
        restored = super().restoreState(state)
        # Ignore stale saved handle widths and keep the caller's current mode.
        self._apply_handle_width()
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
