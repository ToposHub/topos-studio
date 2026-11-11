"""Cross-platform custom borders for dock panels.

The dock overlay paints exposed panel edges. Shared edges are omitted there
and drawn once by the main window through the separator's wider resize hit
area. Both drawing paths stay transparent to mouse input.
"""

from functools import lru_cache

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, Qt
from PyQt6.QtGui import QColor, QPainter, QPaintEvent, QResizeEvent, QMoveEvent, QShowEvent, QHideEvent
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QStyle, QTabBar, QWidget

from src.shared.ui.style_sheet import C
from src.shared.ui.panel_splitter import splitter_rules

__all__ = ["PanelDockWidget", "paint_shared_dock_dividers"]

_FALLBACK_SEPARATOR_EXTENT = 8


def _dock_layout(main_window: QMainWindow) -> list[tuple[QWidget, QRect]]:
    """Treat a tab bar and its active dock as one layout cell.

    Inactive tab docks are still isVisible() in Qt, but have offscreen
    geometry. Only direct children that intersect the window participate.
    """
    children = main_window.findChildren(
        QWidget, options=Qt.FindChildOption.FindDirectChildrenOnly,
    )
    bars = [child for child in children if isinstance(child, QTabBar)
            and child.isVisible() and main_window.rect().intersects(child.geometry())]
    cells: list[tuple[QWidget, QRect]] = []
    for dock in children:
        if not isinstance(dock, QDockWidget) or not (
            dock.isVisible() and not dock.isFloating()
            and main_window.dockWidgetArea(dock) != Qt.DockWidgetArea.NoDockWidgetArea
            and main_window.rect().intersects(dock.geometry())
        ):
            continue
        rect = QRect(dock.geometry())
        if main_window.tabifiedDockWidgets(dock):
            for bar in bars:
                tab = bar.geometry()
                horizontal = (tab.left() == rect.left() and tab.right() == rect.right()
                              and (tab.bottom() + 1 == rect.top()
                                   or rect.bottom() + 1 == tab.top()))
                vertical = (tab.top() == rect.top() and tab.bottom() == rect.bottom()
                            and (tab.right() + 1 == rect.left()
                                 or rect.right() + 1 == tab.left()))
                if horizontal or vertical:
                    rect = rect.united(tab)
                    break
        cells.append((dock, rect))
    center = main_window.centralWidget()
    if center is not None and center.isVisible() and not center.size().isEmpty():
        cells.append((center, QRect(center.mapTo(main_window, QPoint()), center.size())))
    return cells


def _dock_rectangles(
    main_window: QMainWindow,
    *,
    exclude: QDockWidget | None = None,
) -> list[QRect]:
    """Snapshot layout neighbors without traversing dock content widgets.

    addDockWidget reparents participating docks directly to QMainWindow.
    Recursive discovery visits the full editor/inspector trees on every
    repaint, even though their descendants cannot be layout neighbors.
    """
    return [rect for widget, rect in _dock_layout(main_window) if widget is not exclude]


class _PanelDividerOverlay(QWidget):
    """Paint the dock outline above its content without stealing input."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("PanelDividerOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        if isinstance(parent, QTabBar):
            parent.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show):
            parent = self.parentWidget()
            if parent is not None:
                self.setGeometry(parent.rect())
                self.raise_()
                self.update()
                parent.window().update()
        return super().eventFilter(watched, event)

    @staticmethod
    def _uncovered_segments(
        start: int, end: int, covered: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        """Return inclusive edge segments not covered by a shared seam."""
        cursor = start
        result: list[tuple[int, int]] = []
        for covered_start, covered_end in sorted(covered):
            covered_start = max(start, covered_start)
            covered_end = min(end, covered_end)
            if covered_end < cursor:
                continue
            if covered_start > cursor:
                result.append((cursor, covered_start - 1))
            cursor = max(cursor, covered_end + 1)
            if cursor > end:
                break
        if cursor <= end:
            result.append((cursor, end))
        return result

    @staticmethod
    def _shared_edge_ranges(
        dock_rect: QRect,
        side: str,
        separator_extent: int,
        neighbors: list[QRect],
    ) -> list[tuple[int, int]]:
        """Find shared portions using the single snapshot for this paint."""
        max_gap = max(1, separator_extent) + 1
        shared: list[tuple[int, int]] = []
        for other in neighbors:
            if side == "left":
                gap = dock_rect.left() - other.right() - 1
                overlap_start = max(dock_rect.top(), other.top())
                overlap_end = min(dock_rect.bottom(), other.bottom())
            elif side == "right":
                gap = other.left() - dock_rect.right() - 1
                overlap_start = max(dock_rect.top(), other.top())
                overlap_end = min(dock_rect.bottom(), other.bottom())
            elif side == "top":
                gap = dock_rect.top() - other.bottom() - 1
                overlap_start = max(dock_rect.left(), other.left())
                overlap_end = min(dock_rect.right(), other.right())
            else:  # bottom
                gap = other.top() - dock_rect.bottom() - 1
                overlap_start = max(dock_rect.left(), other.left())
                overlap_end = min(dock_rect.right(), other.right())

            if 0 <= gap <= max_gap and overlap_start <= overlap_end:
                shared.append((overlap_start, overlap_end))
        return shared

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: D401 - Qt override
        del event
        dock = self.parentWidget()
        main_window = dock.window() if dock is not None else None
        separator_extent = _FALLBACK_SEPARATOR_EXTENT
        if isinstance(main_window, QMainWindow):
            separator_extent = main_window.style().pixelMetric(
                QStyle.PixelMetric.PM_DockWidgetSeparatorExtent,
                None,
                main_window,
            ) or _FALLBACK_SEPARATOR_EXTENT

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        color = QColor(C.PANEL_DIVIDER)
        origin = dock.mapTo(main_window, QPoint(0, 0)) if isinstance(
            main_window, QMainWindow
        ) else QPoint(0, 0)
        dock_rect = QRect(origin, dock.size())
        group_rect = dock_rect
        neighbors: list[QRect] = []
        if isinstance(main_window, QMainWindow):
            for widget, rect in _dock_layout(main_window):
                if widget is dock or (isinstance(dock, QTabBar) and rect.contains(dock_rect)):
                    group_rect = rect
                else:
                    neighbors.append(rect)
        width = self.width()
        height = self.height()
        edges = (
            ("left", 0, 0, height - 1, dock_rect.top()),
            ("right", width - 1, 0, height - 1, dock_rect.top()),
            ("top", 0, 0, width - 1, dock_rect.left()),
            ("bottom", height - 1, 0, width - 1, dock_rect.left()),
        )
        for side, fixed, start, end, axis_origin in edges:
            # A dock's content/tab join is internal to its group, not a
            # second panel border. The tab overlay supplies the outer edge.
            if getattr(dock_rect, side)() != getattr(group_rect, side)():
                continue
            shared = self._shared_edge_ranges(
                dock_rect, side, separator_extent, neighbors
            )
            local_shared = [
                (shared_start - axis_origin, shared_end - axis_origin)
                for shared_start, shared_end in shared
            ]
            for segment_start, segment_end in self._uncovered_segments(
                start, end, local_shared
            ):
                if side in ("left", "right"):
                    painter.fillRect(fixed, segment_start, 1, segment_end - segment_start + 1, color)
                else:
                    painter.fillRect(segment_start, fixed, segment_end - segment_start + 1, 1, color)


def _divider_segments(
    rectangles: list[QRect],
    separator_extent: int,
    extra_rules: list[tuple[str, int, int, int]] | None = None,
    *,
    include_outline: bool = False,
) -> list[tuple[str, int, int, int]]:
    """Cache geometry only; playback repaints do not rebuild the network."""
    return list(_cached_divider_segments(
        tuple(rect.getRect() for rect in rectangles), separator_extent,
        tuple(extra_rules or []), include_outline,
    ))


@lru_cache(maxsize=32)
def _cached_divider_segments(
    cells: tuple[tuple[int, int, int, int], ...],
    separator_extent: int,
    extra: tuple[tuple[str, int, int, int], ...],
    include_outline: bool,
) -> tuple[tuple[str, int, int, int], ...]:
    rectangles = [QRect(*cell) for cell in cells]
    rules = list(extra)
    if include_outline:
        rules.extend(_exposed_rules(rectangles, separator_extent))
    return tuple(_build_divider_segments(rectangles, separator_extent, rules))


def _build_divider_segments(
    rectangles: list[QRect],
    separator_extent: int,
    extra_rules: list[tuple[str, int, int, int]],
) -> list[tuple[str, int, int, int]]:
    """Build one connected network, bridging only empty layout gutters."""
    max_gap = max(1, separator_extent) + 1
    lines: dict[tuple[str, int], list[tuple[int, int]]] = {}

    def add_line(orientation: str, position: int, start: int, end: int) -> None:
        if start <= end:
            lines.setdefault((orientation, position), []).append((start, end))

    for index, first in enumerate(rectangles):
        for second in rectangles[index + 1:]:
            if first.right() < second.left():
                left, right = first, second
            elif second.right() < first.left():
                left, right = second, first
            else:
                left = right = None
            if left is not None and right is not None:
                gap = right.left() - left.right() - 1
                start = max(left.top(), right.top())
                end = min(left.bottom(), right.bottom())
                if 0 <= gap <= max_gap and start <= end:
                    position = (left.right() + right.left()) // 2
                    add_line("vertical", position, start, end)

            if first.bottom() < second.top():
                top, bottom = first, second
            elif second.bottom() < first.top():
                top, bottom = second, first
            else:
                top = bottom = None
            if top is not None and bottom is not None:
                gap = bottom.top() - top.bottom() - 1
                start = max(top.left(), bottom.left())
                end = min(top.right(), bottom.right())
                if 0 <= gap <= max_gap and start <= end:
                    position = (top.bottom() + bottom.top()) // 2
                    add_line("horizontal", position, start, end)

    for orientation, position, start, end in extra_rules or []:
        add_line(orientation, position, start, end)

    def clear(orientation: str, position: int, start: int, end: int) -> bool:
        if start > end:
            return True
        strip = (QRect(position, start, 1, end - start + 1)
                 if orientation == "vertical"
                 else QRect(start, position, end - start + 1, 1))
        return not any(rect.intersects(strip) for rect in rectangles)

    def merge() -> list[tuple[str, int, int, int]]:
        result: list[tuple[str, int, int, int]] = []
        for (orientation, position), segments in lines.items():
            merged: list[list[int]] = []
            for start, end in sorted(segments):
                if merged and (start <= merged[-1][1] + 1 or (
                    start - merged[-1][1] - 1 <= max_gap
                    and clear(orientation, position, merged[-1][1] + 1, start - 1)
                )):
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            result.extend((orientation, position, start, end) for start, end in merged)
        return result

    merged = merge()
    horizontal = [line for line in merged if line[0] == "horizontal"]
    vertical = [line for line in merged if line[0] == "vertical"]

    def extension(orientation: str, position: int, start: int, end: int, target: int) -> bool:
        if target < start:
            return start - target <= max_gap and clear(orientation, position, target, start - 1)
        if target > end:
            return target - end <= max_gap and clear(orientation, position, end + 1, target)
        return True

    for _, y, x1, x2 in horizontal:
        for _, x, y1, y2 in vertical:
            if (extension("horizontal", y, x1, x2, x)
                    and extension("vertical", x, y1, y2, y)):
                add_line("horizontal", y, min(x1, x), max(x2, x))
                add_line("vertical", x, min(y1, y), max(y2, y))
    return merge()


def paint_shared_dock_dividers(main_window: QMainWindow, painter: QPainter) -> None:
    """Paint each shared boundary once, including T/cross/tab junctions."""
    rectangles = _dock_rectangles(main_window)
    separator_extent = main_window.style().pixelMetric(
        QStyle.PixelMetric.PM_DockWidgetSeparatorExtent, None, main_window,
    ) or _FALLBACK_SEPARATOR_EXTENT
    extra = []
    for orientation, rect in splitter_rules(main_window):
        if orientation == Qt.Orientation.Horizontal:
            extra.append(("vertical", rect.left(), rect.top(), rect.bottom()))
        else:
            extra.append(("horizontal", rect.top(), rect.left(), rect.right()))
    color = QColor(C.PANEL_DIVIDER)
    for orientation, position, start, end in _divider_segments(
        rectangles, separator_extent, extra, include_outline=True,
    ):
        if orientation == "vertical":
            painter.fillRect(position, start, 1, end - start + 1, color)
        else:
            painter.fillRect(start, position, end - start + 1, 1, color)


def _exposed_rules(rectangles: list[QRect], extent: int) -> list[tuple[str, int, int, int]]:
    """Include the perimeter in junctions, without adding shared double edges.

    The panel overlays paint the portions inside their own widgets. The main
    window paints only the missing bridge across the transparent gutters.
    """
    result: list[tuple[str, int, int, int]] = []
    for index, rect in enumerate(rectangles):
        neighbors = rectangles[:index] + rectangles[index + 1:]
        for side in ("left", "right", "top", "bottom"):
            vertical = side in ("left", "right")
            start, end = (rect.top(), rect.bottom()) if vertical else (rect.left(), rect.right())
            shared = _PanelDividerOverlay._shared_edge_ranges(rect, side, extent, neighbors)
            for first, last in _PanelDividerOverlay._uncovered_segments(start, end, shared):
                result.append(("vertical" if vertical else "horizontal", getattr(rect, side)(), first, last))
    return result


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
        main_window = self.window()
        if isinstance(main_window, QMainWindow):
            # Another panel may have become shared/exposed without changing
            # its own size (tab switch, hide, float or re-dock).
            for child in main_window.children():
                overlay = getattr(child, "_divider_overlay", None)
                if isinstance(overlay, _PanelDividerOverlay):
                    overlay.update()
            main_window.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.refresh_divider_overlay()

    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)
        self.refresh_divider_overlay()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.refresh_divider_overlay()

    def hideEvent(self, event: QHideEvent) -> None:
        super().hideEvent(event)
        self.refresh_divider_overlay()


def install_tab_divider_overlay(tab_bar: QTabBar) -> None:
    """Give the tab portion of a dock group the same single outer outline."""
    overlay = getattr(tab_bar, "_divider_overlay", None)
    if overlay is None:
        overlay = _PanelDividerOverlay(tab_bar)
        tab_bar._divider_overlay = overlay
    overlay.setGeometry(tab_bar.rect())
    overlay.show()
    overlay.raise_()
    overlay.update()
