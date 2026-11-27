"""Responsive dividers for the photo Color workspace."""

from collections.abc import Callable

from PyQt6.QtCore import QEvent, QObject, QRect, Qt, QTimer
from PyQt6.QtWidgets import QDockWidget, QMainWindow

from src.shared.ui.dock_resize_handle import DockResizeHandle


def front_dock(docks: tuple[QDockWidget, ...]) -> QDockWidget | None:
    return next((dock for dock in docks if dock.isVisible() and not dock.isFloating()
                 and not dock.visibleRegion().isEmpty()), None)


class PhotoColorResizeHandles(QObject):
    def __init__(self, host: QMainWindow, viewer: QDockWidget, *,
                 sides: tuple[tuple[QDockWidget, ...], tuple[QDockWidget, ...]],
                 tools: tuple[tuple[QDockWidget, ...], tuple[QDockWidget, ...], tuple[QDockWidget, ...]],
                 fit_layout: Callable[[int, bool], None],
                 remember_sidebar: Callable[[int], None],
                 restyle_tabs: Callable[[], None]) -> None:
        super().__init__(host)
        self._host = host
        self._viewer = viewer
        self._sides, self._tools = sides, tools
        self._fit_layout = fit_layout
        self._restyle_tabs = restyle_tabs
        self._active = False
        self._last_fit: tuple[int, int, int, tuple[tuple[bool, bool], ...]] | None = None
        self._columns = [DockResizeHandle(host, Qt.Orientation.Horizontal) for _ in range(4)]
        self._row = DockResizeHandle(host, Qt.Orientation.Vertical)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._refresh)
        for index, handle in enumerate(self._columns[:2]):
            handle.drag_finished.connect(lambda side=index: remember_sidebar(side))
        for handle in (*self._columns, self._row):
            handle.drag_finished.connect(lambda: self._timer.start(0))
        self._docks = tuple(dict.fromkeys((viewer, *(dock for group in (*sides, *tools) for dock in group))))
        for widget in (host, *self._docks):
            widget.installEventFilter(self)

    def set_active(self, active: bool) -> None:
        self._active = active
        self._last_fit = None
        if active:
            self._timer.start(0)
        else:
            self._timer.stop()
            for handle in (*self._columns, self._row):
                handle.hide()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._active and event.type() in (
            QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Move, QEvent.Type.Resize,
            QEvent.Type.LayoutRequest,
        ) and not self._timer.isActive():
            self._timer.start(0)
        return False

    def _refresh(self) -> None:
        if self._host.isVisible() and not any(handle.is_dragging for handle in (*self._columns, self._row)):
            width = self._host.width()
            signature = (width, self._host.height(), self._viewer.height(),
                         tuple((dock.isHidden(), dock.isFloating()) for dock in self._docks))
            if signature != self._last_fit:
                previous = self._last_fit
                self._last_fit = signature
                resize_columns = previous is None or previous[0] != width or previous[3] != signature[3]
                self._fit_layout(width, resize_columns)
            self._restyle_tabs()
        viewer = front_dock((self._viewer,))
        if viewer is None:
            for handle in (*self._columns, self._row):
                handle.hide()
            return
        bottom = viewer.geometry().bottom()
        left, right = (front_dock(tuple(dock for dock in group if dock.y() < bottom)) for group in self._sides)
        primary, secondary, scopes = (front_dock(tuple(dock for dock in group if dock.y() >= bottom))
                                      for group in self._tools)
        for handle, pair in zip(self._columns, (
            (left, viewer), (viewer, right), (primary, secondary), (secondary or primary, scopes),
        )):
            handle.place_column(*pair)
        tools = primary or secondary or scopes
        if viewer is not None and tools is not None:
            view_rect, tools_rect = viewer.geometry(), tools.geometry()
            gap = tools_rect.top() - view_rect.bottom() - 1
            if 0 <= gap <= 32 and tools_rect.top() > view_rect.top():
                radius = self._row.HIT_RADIUS
                edge = view_rect.bottom() + 1
                self._row.place(QRect(0, edge - radius, self._host.width(), radius * 2),
                                viewer, tools, tool_tab_height=max(0, gap - 1))
                return
        self._row.hide()
