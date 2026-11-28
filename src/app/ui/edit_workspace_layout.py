"""Responsive dock columns and expanded resize targets for workspace dividers."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QDockWidget, QMainWindow

from src.shared.ui.dock_resize_handle import DockResizeHandle


class WorkspaceDockResizeHandles(QObject):
    column_shares_changed = pyqtSignal(tuple)
    compact_share_changed = pyqtSignal(float)

    def __init__(self, host: QMainWindow, viewer: QDockWidget, timeline: QDockWidget,
                 sides: tuple[QDockWidget, ...], side_groups: tuple[tuple[QDockWidget, ...], ...],
                 *, saved_shares: tuple[int, int, int] | None = None,
                 saved_compact_share: float | None = None) -> None:
        super().__init__(host)
        self._host = host
        self._viewer, self._timeline, self._sides = viewer, timeline, sides
        self._side_groups = side_groups
        self._left = DockResizeHandle(host, Qt.Orientation.Horizontal)
        self._right = DockResizeHandle(host, Qt.Orientation.Horizontal)
        self._row = DockResizeHandle(host, Qt.Orientation.Vertical)
        self._active = False
        self._default_shares: tuple[int, int, int] | None = None
        self._manual_shares = saved_shares
        self._compact_share = saved_compact_share
        self._sides_compact = False
        self._column_inputs: tuple[
            int, int, int, tuple[tuple[bool, bool, Qt.DockWidgetArea, int], ...],
        ] | None = None
        self._side_columns: tuple[QDockWidget | None, QDockWidget | None] = (None, None)
        self._left.drag_finished.connect(self._remember_shares)
        self._right.drag_finished.connect(self._remember_shares)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._refresh)
        for dock in (host, viewer, timeline, *sides):
            dock.installEventFilter(self)

    def set_active(self, active: bool, default_shares: tuple[int, int, int] | None = None) -> None:
        self._active = active
        self._default_shares = default_shares
        self._column_inputs = None
        if active:
            self._timer.start(0)
        else:
            self._timer.stop()
            for handle in (self._left, self._right, self._row):
                handle.hide()

    def reset_column_shares(self) -> None:
        self._manual_shares = None
        self._compact_share = None
        self._sides_compact = False
        self._column_inputs = None
        self.column_shares_changed.emit(())
        self.compact_share_changed.emit(0.0)

    def _remember_shares(self) -> None:
        if self._default_shares is None:
            return
        shares = self._manual_shares or self._default_shares
        self._host.layout().activate()
        left, right = self._find_side_columns()
        total = sum(dock.width() for dock in (left, self._viewer, right) if dock is not None)
        if self._sides_compact:
            if left is not None:
                self._compact_share = left.width() / total
                self.compact_share_changed.emit(self._compact_share)
            return
        if left is not None and right is not None:
            self._manual_shares = left.width(), self._viewer.width(), right.width()
        elif (side := left or right) is not None:
            # Keep the hidden side's share when editing the visible split.
            hidden = max(1, min(9998, round(10000 * shares[2 if left is not None else 0] / sum(shares))))
            visible = max(1, min(9999 - hidden, round(10000 * side.width() / total)))
            center = 10000 - hidden - visible
            self._manual_shares = (visible, center, hidden) if left is not None else (hidden, center, visible)
        else:
            return
        self.column_shares_changed.emit(self._manual_shares)

    def _fit_columns(self, left: QDockWidget | None, right: QDockWidget | None) -> None:
        shares = self._manual_shares or self._default_shares
        if shares is None or (left is None and right is None):
            return

        columns = [dock for dock in (left, self._viewer, right) if dock is not None]
        available = self._host.contentsRect().width() - (len(columns) - 1) * self._left.separator_extent()
        total = sum(shares)
        left_share = shares[0] / total if left is not None else 0.0
        right_share = shares[2] / total if right is not None else 0.0
        if self._sides_compact and self._compact_share is not None:
            left_share = self._compact_share

        weights = {self._viewer: 1.0 - left_share - right_share}
        if left is not None:
            weights[left] = left_share
        if right is not None:
            weights[right] = right_share
        minimums: dict[QDockWidget, int] = {}
        for dock in columns:
            tabs = (dock, *self._host.tabifiedDockWidgets(dock))
            minimums[dock] = max(self._minimum_width(tab) for tab in tabs if not tab.isHidden())
        if sum(minimums.values()) > available:
            return

        # Fix any column that hits its minimum, then split the rest proportionally.
        remaining = columns.copy()
        targets: dict[QDockWidget, int] = {}
        space = available
        while remaining:
            scale = space / sum(weights[dock] for dock in remaining)
            limited = [dock for dock in remaining if scale * weights[dock] < minimums[dock]]
            if not limited:
                for dock in remaining:
                    targets[dock] = round(scale * weights[dock])
                targets[remaining[-1]] += available - sum(targets.values())
                break
            for dock in limited:
                targets[dock] = minimums[dock]
                space -= minimums[dock]
                remaining.remove(dock)
        widths = [targets[dock] for dock in columns]
        if any(abs(dock.width() - width) > 1 for dock, width in zip(columns, widths)):
            self._host.resizeDocks(columns, widths, Qt.Orientation.Horizontal)
            self._host.layout().activate()

    @staticmethod
    def screen_width(host: QMainWindow) -> int:
        screen = host.screen()
        return screen.availableGeometry().width() if screen else host.width()

    def _layout_inputs(self) -> tuple[int, int, int, tuple[tuple[bool, bool, Qt.DockWidgetArea, int], ...]]:
        """Only inputs to the layout can schedule a fit, never resulting dock sizes."""
        columns = tuple(
            (dock.isHidden(), dock.isFloating(), self._host.dockWidgetArea(dock), self._minimum_width(dock))
            for dock in self._sides
        )
        return (self._host.contentsRect().width(), self.screen_width(self._host),
                self._minimum_width(self._viewer), columns)

    @staticmethod
    def _minimum_width(dock: QDockWidget) -> int:
        return max(dock.minimumWidth(), dock.minimumSizeHint().width())

    @classmethod
    def needs_compact_columns(
        cls, width: int, viewer: QDockWidget, groups: tuple[tuple[QDockWidget, ...], ...],
    ) -> bool:
        if not all(groups):
            return False
        sidebar_minimum = sum(max(map(cls._minimum_width, stack)) for stack in groups)
        return width < sidebar_minimum + cls._minimum_width(viewer) + 24

    def _adapt_side_groups(self) -> None:
        if self._default_shares is None or len(self._side_groups) != 2:
            return
        media, inspector = self._side_groups
        if not media or not inspector or any(dock.isFloating() for dock in (media[0], inspector[0], self._viewer)):
            return

        top = Qt.DockWidgetArea.TopDockWidgetArea
        left = Qt.DockWidgetArea.LeftDockWidgetArea
        right = Qt.DockWidgetArea.RightDockWidgetArea
        media_area = self._host.dockWidgetArea(media[0])
        inspector_area = self._host.dockWidgetArea(inspector[0])
        if self._host.dockWidgetArea(self._viewer) != top:
            return
        legacy_row = media_area == left and inspector_area in (left, right)
        if not legacy_row and (media_area, inspector_area) != (top, top):
            return

        # Account for the two stacks separately, even when they are tabified.
        open_groups = tuple(
            tuple(dock for dock in stack if not dock.isFloating() and not dock.isHidden()) or stack[:1]
            for stack in self._side_groups
        )
        screen_width = self.screen_width(self._host)
        available = min(self._host.width(), screen_width)
        compact = self.needs_compact_columns(available, self._viewer, open_groups)

        spanning_tabs = any(
            peer in inspector for dock in media for peer in self._host.tabifiedDockWidgets(dock)
        )
        both_groups_open = all(any(not dock.isHidden() for dock in group) for group in (media, inspector))
        if spanning_tabs or both_groups_open:
            self._sides_compact = spanning_tabs
        if not legacy_row and self._sides_compact == compact:
            return

        self._rebuild_row(media, inspector, compact=compact)
        self._sides_compact = compact
        if (compact and self._host.width() > screen_width
                and not (self._host.isMaximized() or self._host.isFullScreen())):
            self._host.resize(screen_width, self._host.height())

    def _rebuild_row(
        self, media: tuple[QDockWidget, ...], inspector: tuple[QDockWidget, ...], *, compact: bool,
    ) -> None:
        """Put all upper columns in one Qt split, as in the Color workspace."""
        host = self._host
        shown = {dock: not dock.isHidden() for dock in (*media, *inspector) if not dock.isFloating()}
        active_media = next((dock for dock in media if self._visible(dock)), None)
        active_inspector = next((dock for dock in inspector if self._visible(dock)), None)
        # Keep the viewer mounted so splitting the row does not hide its widget.
        for dock in shown:
            host.removeDockWidget(dock)

        horizontal = Qt.Orientation.Horizontal
        host.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, media[0])
        host.splitDockWidget(media[0], self._viewer, horizontal)
        if not compact:
            host.splitDockWidget(self._viewer, inspector[0], horizontal)
        stacks = ((media[0], (*media, *inspector)),) if compact else (
            (media[0], media), (inspector[0], inspector),
        )
        for anchor, members in stacks:
            for dock in members:
                if dock not in shown:
                    continue
                if dock is not anchor:
                    host.tabifyDockWidget(anchor, dock)
                dock.setVisible(shown[dock])
        self._viewer.show()
        front_tabs = ((active_media or active_inspector,) if compact else (
            active_media or next((dock for dock in media if shown.get(dock)), None),
            active_inspector or next((dock for dock in inspector if shown.get(dock)), None),
        ))
        for front in front_tabs:
            if front is not None:
                front.raise_()
        host.layout().activate()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if getattr(self, "_active", False) and event.type() in (
            QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show,
            QEvent.Type.Hide, QEvent.Type.LayoutRequest,
        ):
            if not self._timer.isActive():
                self._timer.start(0)
        return False

    def _find_side_columns(self) -> tuple[QDockWidget | None, QDockWidget | None]:
        viewer = self._viewer
        sides = [dock for dock in self._sides if self._visible(dock)]
        gap = self._left.separator_extent()
        left = next((dock for dock in sides if 0 <= viewer.x() - dock.geometry().right() - 1 <= gap), None)
        right = next((dock for dock in sides if 0 <= dock.x() - viewer.geometry().right() - 1 <= gap), None)
        return left, right

    def _refresh(self) -> None:
        viewer, timeline = self._viewer, self._timeline
        if not self._visible(viewer):
            self._side_columns = (None, None)
            for handle in (self._left, self._right, self._row):
                handle.hide()
            return

        if (self._default_shares is not None and not self._left.is_dragging and not self._right.is_dragging
                and self._layout_inputs() != self._column_inputs):
            self._adapt_side_groups()
            self._column_inputs = self._layout_inputs()
            self._fit_columns(*self._find_side_columns())

        left, right = self._side_columns = self._find_side_columns()
        self._left.place_column(left, viewer, include_tab_strip=True)
        self._right.place_column(viewer, right, include_tab_strip=True)

        gap = timeline.y() - viewer.geometry().bottom() - 1
        if self._visible(timeline) and 0 <= gap <= 6:
            rect = timeline.geometry()
            radius = DockResizeHandle.HIT_RADIUS
            self._row.place(QRect(rect.x(), rect.y() - radius, rect.width(), radius * 2),
                            viewer, timeline)
        else:
            self._row.hide()

    def _visible(self, dock: QDockWidget) -> bool:
        return (dock.isVisible() and not dock.isFloating()
                and self._host.contentsRect().intersects(dock.geometry())
                and not dock.visibleRegion().isEmpty())
