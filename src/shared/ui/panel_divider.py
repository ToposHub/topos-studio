"""Cross-platform custom borders for dock panels.

The dock overlay paints exposed panel edges. Shared edges are omitted there
and drawn once by the main window through the separator's wider resize hit
area. Both drawing paths stay transparent to mouse input.
"""

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QDockWidget, QMainWindow, QStyle, QWidget

from src.shared.ui.style_sheet import C

__all__ = ["PanelDockWidget", "paint_shared_dock_dividers"]

_FALLBACK_SEPARATOR_EXTENT = 8


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
    rectangles: list[QRect] = []
    for dock in main_window.findChildren(
        QDockWidget, options=Qt.FindChildOption.FindDirectChildrenOnly,
    ):
        if (
            dock is not exclude
            and dock.isVisible()
            and not dock.isFloating()
            and main_window.dockWidgetArea(dock)
            != Qt.DockWidgetArea.NoDockWidgetArea
        ):
            origin = dock.mapTo(main_window, QPoint(0, 0))
            rectangles.append(QRect(origin, dock.size()))

    center = main_window.centralWidget()
    if center is not None and center.isVisible():
        origin = center.mapTo(main_window, QPoint(0, 0))
        rectangles.append(QRect(origin, center.size()))
    return rectangles


class _PanelDividerOverlay(QWidget):
    """Paint the dock outline above its content without stealing input."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("PanelDividerOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

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

    def paintEvent(self, event) -> None:  # noqa: D401 - Qt override
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
        pen = QPen(QColor(C.PANEL_DIVIDER))
        pen.setWidth(1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        if not isinstance(dock, QDockWidget):
            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
            return

        origin = dock.mapTo(main_window, QPoint(0, 0)) if isinstance(
            main_window, QMainWindow
        ) else QPoint(0, 0)
        dock_rect = QRect(origin, dock.size())
        neighbors = (
            _dock_rectangles(main_window, exclude=dock)
            if isinstance(main_window, QMainWindow) and not dock.isFloating()
            else []
        )
        width = self.width()
        height = self.height()
        edges = (
            ("left", 0, 0, height - 1, dock_rect.top()),
            ("right", width - 1, 0, height - 1, dock_rect.top()),
            ("top", 0, 0, width - 1, dock_rect.left()),
            ("bottom", height - 1, 0, width - 1, dock_rect.left()),
        )
        for side, fixed, start, end, axis_origin in edges:
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
                    painter.drawLine(fixed, segment_start, fixed, segment_end)
                else:
                    painter.drawLine(segment_start, fixed, segment_end, fixed)


def paint_shared_dock_dividers(
    main_window: QMainWindow,
    painter: QPainter,
) -> None:
    """Draw one centered line through each dock separator hit area.

    This is called from the main window's paint event so it does not add an
    unmanaged child to QMainWindow's layout or intercept splitter drags.
    """
    rectangles = _dock_rectangles(main_window)

    separator_extent = main_window.style().pixelMetric(
        QStyle.PixelMetric.PM_DockWidgetSeparatorExtent,
        None,
        main_window,
    ) or _FALLBACK_SEPARATOR_EXTENT
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

    pen = QPen(QColor(C.PANEL_DIVIDER))
    pen.setWidth(1)
    pen.setCosmetic(True)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setPen(pen)
    for (orientation, position), segments in lines.items():
        segments.sort()
        merged: list[list[int]] = []
        for start, end in segments:
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in merged:
            if orientation == "vertical":
                painter.drawLine(position, start, position, end)
            else:
                painter.drawLine(start, position, end, position)


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
            main_window.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.refresh_divider_overlay()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self.refresh_divider_overlay()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_divider_overlay()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        main_window = self.window()
        if isinstance(main_window, QMainWindow):
            main_window.update()
