"""Virtual, model-backed gallery layouts for native Files.

Only entry metadata and thumbnail aspect-ratio hints enter this view.  It never
opens media, probes the filesystem, or creates a widget for an individual item.
"""
from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import (
    QAbstractItemModel, QItemSelection, QItemSelectionModel, QModelIndex, QPoint,
    QPersistentModelIndex, QRect, QSize, Qt, QTimer,
)
from PyQt6.QtGui import QColor, QHideEvent, QKeyEvent, QMouseEvent, QPaintEvent, QPainter, QPen, QRegion, QResizeEvent
from PyQt6.QtWidgets import QApplication, QAbstractItemView, QStyle, QStyleOptionViewItem, QWidget

from src.features.files.contracts import Entry

GALLERY_MARGIN = 8
_BUCKET_HEIGHT = 256
_LAYOUT_MODES = frozenset(("adaptive", "masonry", "grid"))
GALLERY_ENTRY_ROLE = int(Qt.ItemDataRole.UserRole) + 1


@dataclass(frozen=True)
class GallerySpacing:
    inset: int = 6
    gap: int = 8
    caption_height: int = 44


DEFAULT_GALLERY_SPACING = GallerySpacing()


def _bounded_ratio(value: float) -> float:
    """Bound unusual media to manageable cells; the delegate still fits all pixels."""
    return max(0.25, min(8.0, value)) if math.isfinite(value) and value > 0.0 else 1.0


def gallery_rectangles(
    aspects: Sequence[float], width: int, thumbnail_size: int, mode: str,
    *, spacing: GallerySpacing = DEFAULT_GALLERY_SPACING, adaptive_tail_scale: float = 1.0,
) -> tuple[list[QRect], int]:
    """Return document-space cells and height, preserving model row order.

    Adaptive rows are justified around a target image height; a short final row
    remains at the target unless it can be filled within the tail scale.
    Masonry uses equal-width shortest columns.
    Grid uses equal square image areas. Every cell includes the caption/insets.
    """
    if mode not in _LAYOUT_MODES:
        raise ValueError(f"Unknown Files gallery layout: {mode}")
    if not aspects:
        return [], 0
    ratios = [_bounded_ratio(float(ratio)) for ratio in aspects]
    gap = spacing.gap
    padding = 2 * spacing.inset
    available = max(padding + 1, int(width) - 2 * GALLERY_MARGIN)
    target = max(32, int(thumbnail_size))
    footer = padding + spacing.caption_height
    cells: list[QRect] = []
    if mode == "adaptive":
        start = 0
        top = GALLERY_MARGIN
        max_items = (available + gap) // (padding + gap + 1)
        while start < len(ratios):
            end = start
            summed = 0.0
            limit = min(len(ratios), start + max_items)
            while end < limit:
                summed += ratios[end]
                end += 1
                count = end - start
                fit = (available - count * padding - (count - 1) * gap) / summed
                if fit <= target:
                    if count > 1:
                        previous = ((available - (count - 1) * padding - (count - 2) * gap)
                                    / (summed - ratios[end - 1]))
                        if abs(previous - target) < abs(fit - target):
                            end -= 1
                            summed -= ratios[end]
                    break
            count = end - start
            image_space = available - count * padding - (count - 1) * gap
            fit = image_space / summed
            justify = end < len(ratios) or fit <= target * adaptive_tail_scale
            image_height = max(1, round(fit if justify else target))
            image_pixels = image_space if justify else max(count, round(summed * image_height))
            width_per_ratio = image_pixels / summed
            left = GALLERY_MARGIN
            ideal_edge = 0.0
            edge = 0
            for item_number, ratio in enumerate(ratios[start:end], 1):
                ideal_edge += ratio * width_per_ratio
                # Allocate the rounding remainder across the row and leave each cell one pixel.
                next_edge = min(max(edge + 1, round(ideal_edge)), image_pixels - (count - item_number))
                image_width = next_edge - edge
                cells.append(QRect(left, top, image_width + padding, image_height + footer))
                left += image_width + padding + gap
                edge = next_edge
            top += image_height + footer + gap
            start = end
    else:
        columns = max(1, round((available + gap) / (target + padding + gap)))
        usable = available - (columns - 1) * gap
        column_width = usable // columns
        image_width = max(1, column_width - padding)
        if mode == "grid":
            row_height = image_width + footer
            for row in range(len(ratios)):
                column = row % columns
                edge = column * usable // columns
                next_edge = (column + 1) * usable // columns
                cells.append(QRect(GALLERY_MARGIN + column * gap + edge,
                                   GALLERY_MARGIN + row // columns * (row_height + gap),
                                   next_edge - edge, row_height))
        else:
            heights = [(GALLERY_MARGIN, column) for column in range(columns)]
            heapq.heapify(heights)
            for ratio in ratios:
                top, column = heapq.heappop(heights)
                image_height = max(1, min(target * 4, round(image_width / ratio)))
                height = image_height + footer
                cells.append(QRect(GALLERY_MARGIN + column * (column_width + gap),
                                   top, column_width, height))
                heapq.heappush(heights, (top + height + gap, column))
    return cells, max(cell.bottom() + 1 for cell in cells) + GALLERY_MARGIN


class FilesGalleryView(QAbstractItemView):
    """Three gallery layouts sharing one Qt model and selection model."""

    def __init__(self, parent: QWidget | None = None, *, spacing: GallerySpacing = DEFAULT_GALLERY_SPACING,
                 adaptive_tail_scale: float = 1.0) -> None:
        super().__init__(parent)
        self._spacing = spacing
        self._adaptive_tail_scale = adaptive_tail_scale
        self._mode = "adaptive"
        self._thumbnail_size = 180
        self._rectangles: list[QRect] = []
        self._paths: list[str] = []
        self._path_rows: dict[str, int] = {}
        self._known_aspect_paths: set[str] = set()
        self._model_aspects: list[float | None] = []
        self._rated_entries: list[tuple[int, Entry]] = []
        self._snapshot_dirty = True
        self._aspect_hints: dict[str, float] = {}
        self._buckets: dict[int, list[int]] = {}
        self._content_height = 0
        self._laying_out = False
        self._connected_model: QAbstractItemModel | None = None
        self._anchor: tuple[str, int] | None = None
        self._hover_row = -1
        self._range_anchor = QPersistentModelIndex()
        self._shift_press_handled = False
        self._rubber_origin: QPoint | None = None
        self._rubber_pointer = QPoint()
        self._rubber_active = False
        self._rubber_base_rows: set[int] = set()
        self._rubber_operation = "replace"
        self._rubber_timer = QTimer(self)
        self._rubber_timer.setInterval(30)
        self._rubber_timer.timeout.connect(self._auto_scroll_rubber)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._rebuild_layout)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def setModel(self, model: QAbstractItemModel | None) -> None:
        self._stop_rubber_selection()
        self._range_anchor = QPersistentModelIndex()
        previous = self._connected_model
        signal_names = ("modelReset", "layoutChanged", "rowsInserted", "rowsRemoved", "rowsMoved", "dataChanged")
        if previous is not None:
            for name in signal_names:
                callback = self._model_data_changed if name == "dataChanged" else self._model_structure_changed
                getattr(previous, name).disconnect(callback)
        super().setModel(model)
        self._connected_model = model
        if model is not None:
            for name in signal_names:
                callback = self._model_data_changed if name == "dataChanged" else self._model_structure_changed
                getattr(model, name).connect(callback)
        self._snapshot_dirty = True
        self._schedule_layout()

    def _model_data_changed(self, *_args: Any) -> None:
        self._snapshot_dirty = True
        self._schedule_layout()

    def _model_structure_changed(self, *_args: Any) -> None:
        self._snapshot_dirty = True
        self._stop_rubber_selection()
        self._range_anchor = QPersistentModelIndex()
        self._shift_press_handled = False
        self._hover_row = -1
        self._schedule_layout()

    def setLayoutMode(self, mode: str) -> None:
        if mode not in _LAYOUT_MODES:
            raise ValueError(f"Unknown Files gallery layout: {mode}")
        if mode != self._mode:
            self._stop_rubber_selection()
            self._anchor = self._capture_anchor()
            self._mode = mode
            self.doItemsLayout()

    def layoutMode(self) -> str:
        return self._mode

    def setThumbnailSize(self, size: int) -> None:
        bounded = max(32, min(1024, int(size)))
        if bounded != self._thumbnail_size:
            self._stop_rubber_selection()
            self._anchor = self._capture_anchor()
            self._thumbnail_size = bounded
            self.doItemsLayout()

    def thumbnailSize(self) -> int:
        return self._thumbnail_size

    def setGallerySpacing(self, spacing: GallerySpacing) -> None:
        if spacing != self._spacing:
            self._stop_rubber_selection()
            self._spacing = spacing
            self.doItemsLayout()

    def gridSize(self) -> QSize:
        side = self._thumbnail_size + 2 * self._spacing.inset
        return QSize(side, side + self._spacing.caption_height)

    def setAspectRatio(self, path: str, ratio: float) -> None:
        if not math.isfinite(ratio) or ratio <= 0.0:
            return
        value = _bounded_ratio(ratio)
        if abs(self._aspect_hints.get(path, 0.0) - value) < 0.0001:
            return
        self._aspect_hints[path] = value
        if path not in self._known_aspect_paths and self._mode != "grid":
            self._schedule_layout(delay_ms=32)

    def clearAspectRatios(self) -> None:
        self._aspect_hints.clear()
        self._schedule_layout()

    def _capture_anchor(self) -> tuple[str, int] | None:
        if not self._rectangles:
            return None
        top = self.verticalOffset()
        rows = self._candidate_rows(QRect(0, top, self.viewport().width(), self.viewport().height()))
        visible = [row for row in rows if self._rectangles[row].bottom() >= top]
        if not visible:
            return None
        row = min(visible, key=lambda item: (self._rectangles[item].top(), item))
        return self._paths[row], self._rectangles[row].top() - top

    def _schedule_layout(self, *_args: Any, delay_ms: int = 0) -> None:
        if not hasattr(self, "_layout_timer") or self._laying_out:
            return
        if not self._layout_timer.isActive():
            self._anchor = self._capture_anchor()
            self._layout_timer.start(delay_ms)
        elif not delay_ms and self._layout_timer.interval():
            self._layout_timer.start(0)
        if not delay_ms:
            self.viewport().update()

    def doItemsLayout(self) -> None:
        if not hasattr(self, "_layout_timer"):
            return
        if self._anchor is None:
            self._anchor = self._capture_anchor()
        self._layout_timer.stop()
        self._rebuild_layout()

    def _ensure_layout(self) -> None:
        model = self.model()
        count = model.rowCount(self.rootIndex()) if model is not None else 0
        if not self._laying_out and (self._snapshot_dirty or count != len(self._rectangles)):
            self.doItemsLayout()

    def _rebuild_layout(self) -> None:
        if self._laying_out:
            return
        self._laying_out = True
        self._layout_timer.stop()
        try:
            model = self.model()
            count = model.rowCount(self.rootIndex()) if model is not None else 0
            # Resizing only changes geometry. Reuse the immutable metadata
            # snapshot instead of calling through Qt/Python for every item.
            if self._snapshot_dirty or count != len(self._paths):
                paths: list[str] = []
                model_aspects: list[float | None] = []
                rated_entries: list[tuple[int, Entry]] = []
                known: set[str] = set()
                for row in range(count):
                    entry = model.index(row, 0, self.rootIndex()).data(GALLERY_ENTRY_ROLE)
                    if isinstance(entry, Entry):
                        path = entry.path
                        if entry.user.rating:
                            rated_entries.append((row, entry))
                        width, height = entry.technical.width, entry.technical.height
                        if width and height and width > 0 and height > 0:
                            ratio = width / height
                            known.add(path)
                        else:
                            ratio = None
                    else:
                        path, ratio = str(row), None
                    paths.append(path)
                    model_aspects.append(ratio)
                self._paths = paths
                self._path_rows = {path: row for row, path in enumerate(paths)}
                self._known_aspect_paths = known
                self._model_aspects = model_aspects
                self._rated_entries = rated_entries
                self._snapshot_dirty = False
            aspects = [ratio if ratio is not None else self._aspect_hints.get(path, 1.0)
                       for path, ratio in zip(self._paths, self._model_aspects)]
            for _ in range(2):
                width = self.viewport().width()
                rectangles, content_height = gallery_rectangles(
                    aspects, width, self._thumbnail_size, self._mode,
                    spacing=self._spacing, adaptive_tail_scale=self._adaptive_tail_scale)
                buckets: dict[int, list[int]] = {}
                for row, rect in enumerate(rectangles):
                    for bucket in range(rect.top() // _BUCKET_HEIGHT, rect.bottom() // _BUCKET_HEIGHT + 1):
                        buckets.setdefault(bucket, []).append(row)
                # Scrollbar changes can synchronously call indexAt; publish geometry
                # and its lookup together before allowing those reentrant callbacks.
                self._rectangles, self._content_height, self._buckets = rectangles, content_height, buckets
                bar = self.verticalScrollBar()
                bar.setPageStep(max(1, self.viewport().height()))
                bar.setSingleStep(max(24, self._thumbnail_size // 4))
                bar.setRange(0, max(0, self._content_height - self.viewport().height()))
                if self.viewport().width() == width:
                    break
            update_rating_layout = getattr(self.itemDelegate(), "update_rating_layout", None)
            if callable(update_rating_layout):
                update_rating_layout(self._rated_entries, self._rectangles, self.font())
            if self._anchor is not None:
                path, offset = self._anchor
                row = self._path_rows.get(path)
                self.verticalScrollBar().setValue(self._rectangles[row].top() - offset if row is not None else 0)
            self._anchor = None
            if self.state() == QAbstractItemView.State.EditingState:
                self.updateEditorGeometries()
        finally:
            self._laying_out = False
        self.viewport().update()

    def _candidate_rows(self, rect: QRect) -> list[int]:
        if rect.isEmpty():
            return []
        rows: set[int] = set()
        for bucket in range(max(0, rect.top() // _BUCKET_HEIGHT), rect.bottom() // _BUCKET_HEIGHT + 1):
            rows.update(self._buckets.get(bucket, ()))
        return sorted(row for row in rows if row < len(self._rectangles) and self._rectangles[row].intersects(rect))

    def visibleIndexes(self) -> list[QModelIndex]:
        self._ensure_layout()
        model = self.model()
        if model is None:
            return []
        rect = self.viewport().rect().translated(0, self.verticalOffset())
        return [model.index(row, 0, self.rootIndex()) for row in self._candidate_rows(rect)]

    def visualRect(self, index: QModelIndex) -> QRect:
        self._ensure_layout()
        if not index.isValid() or not 0 <= index.row() < len(self._rectangles):
            return QRect()
        return self._rectangles[index.row()].translated(0, -self.verticalOffset())

    def indexAt(self, point: QPoint) -> QModelIndex:
        self._ensure_layout()
        model = self.model()
        if model is None:
            return QModelIndex()
        document_point = point + QPoint(0, self.verticalOffset())
        rows = self._candidate_rows(QRect(document_point, QSize(1, 1)))
        return model.index(rows[0], 0, self.rootIndex()) if rows else QModelIndex()

    def scrollTo(self, index: QModelIndex, hint: QAbstractItemView.ScrollHint = QAbstractItemView.ScrollHint.EnsureVisible) -> None:
        self._ensure_layout()
        if not index.isValid() or not 0 <= index.row() < len(self._rectangles):
            return
        rect = self._rectangles[index.row()]
        bar = self.verticalScrollBar()
        height = self.viewport().height()
        if hint == self.ScrollHint.PositionAtTop:
            value = rect.top()
        elif hint == self.ScrollHint.PositionAtBottom:
            value = rect.bottom() + 1 - height
        elif hint == self.ScrollHint.PositionAtCenter:
            value = rect.center().y() - height // 2
        elif rect.top() < bar.value():
            value = rect.top()
        elif rect.bottom() >= bar.value() + height:
            value = rect.bottom() + 1 - height
        else:
            return
        bar.setValue(value)

    def horizontalOffset(self) -> int:
        return 0

    def verticalOffset(self) -> int:
        return self.verticalScrollBar().value()

    def isIndexHidden(self, index: QModelIndex) -> bool:
        return not index.isValid() or index.parent() != self.rootIndex()

    def setSelection(self, rect: QRect, flags: QItemSelectionModel.SelectionFlag) -> None:
        self._ensure_layout()
        model = self.model()
        selection_model = self.selectionModel()
        if model is None or selection_model is None:
            return
        document = rect.normalized().translated(0, self.verticalOffset())
        self._select_rows(set(self._candidate_rows(document)), flags)

    def _select_rows(self, rows: set[int], flags: QItemSelectionModel.SelectionFlag) -> None:
        model = self.model()
        selection_model = self.selectionModel()
        if model is None or selection_model is None:
            return
        valid_rows = sorted(row for row in rows if 0 <= row < model.rowCount(self.rootIndex()))
        selection = QItemSelection()
        if valid_rows:
            start = previous = valid_rows[0]
            last_column = model.columnCount(self.rootIndex()) - 1
            for row in valid_rows[1:] + [valid_rows[-1] + 2]:
                if row != previous + 1:
                    selection.select(model.index(start, 0, self.rootIndex()),
                                     model.index(previous, last_column, self.rootIndex()))
                    start = row
                previous = row
        selection_model.select(selection, flags)

    def _select_range(self, target: QModelIndex, additive: bool) -> None:
        anchor = QModelIndex(self._range_anchor) if self._range_anchor.isValid() else self.currentIndex()
        if not anchor.isValid():
            anchor = target
            self._range_anchor = QPersistentModelIndex(anchor)
        rows = set(range(min(anchor.row(), target.row()), max(anchor.row(), target.row()) + 1))
        flags = QItemSelectionModel.SelectionFlag.Select if additive else QItemSelectionModel.SelectionFlag.ClearAndSelect
        self._select_rows(rows, flags)
        self.selectionModel().setCurrentIndex(target, QItemSelectionModel.SelectionFlag.NoUpdate)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and self.selectionModel() is not None:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            modifiers = event.modifiers()
            additive = bool(modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
            if index.isValid() and modifiers & Qt.KeyboardModifier.ShiftModifier:
                self._select_range(index, additive)
                self._shift_press_handled = True
                event.accept()
                return
            if not index.isValid():
                self._rubber_origin = event.position().toPoint() + QPoint(0, self.verticalOffset())
                self._rubber_pointer = event.position().toPoint()
                self._rubber_active = False
                self._rubber_base_rows = {item.row() for item in self.selectionModel().selectedRows(0)}
                self._rubber_operation = "toggle" if additive else "add" if modifiers & Qt.KeyboardModifier.ShiftModifier else "replace"
                if self._rubber_operation == "replace":
                    self.selectionModel().clearSelection()
                self.setState(QAbstractItemView.State.DragSelectingState)
                event.accept()
                return
            self._range_anchor = QPersistentModelIndex(index)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._shift_press_handled:
            self._shift_press_handled = False
            index = self.indexAt(event.position().toPoint())
            if index.isValid():
                self.clicked.emit(index)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._rubber_origin is not None:
            self._update_rubber_selection(event.position().toPoint())
            self._stop_rubber_selection()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _rubber_rectangle(self) -> QRect:
        if self._rubber_origin is None:
            return QRect()
        origin = self._rubber_origin - QPoint(0, self.verticalOffset())
        return QRect(origin, self._rubber_pointer).normalized()

    def _update_rubber_selection(self, pointer: QPoint) -> None:
        self._rubber_pointer = pointer
        if self._rubber_origin is None:
            return
        rect = self._rubber_rectangle()
        if rect.width() + rect.height() >= QApplication.startDragDistance():
            self._rubber_active = True
        if self._rubber_active:
            rows = set(self._candidate_rows(rect.translated(0, self.verticalOffset())))
            if self._rubber_operation == "toggle":
                rows ^= self._rubber_base_rows
            elif self._rubber_operation == "add":
                rows |= self._rubber_base_rows
            self._select_rows(rows, QItemSelectionModel.SelectionFlag.ClearAndSelect)
            edge = 24
            if pointer.y() < edge or pointer.y() > self.viewport().height() - edge:
                if not self._rubber_timer.isActive():
                    self._rubber_timer.start()
            else:
                self._rubber_timer.stop()
        self.viewport().update()

    def _auto_scroll_rubber(self) -> None:
        if self._rubber_origin is None or not self._rubber_active:
            self._rubber_timer.stop()
            return
        bar = self.verticalScrollBar()
        edge = 24
        pointer = self._rubber_pointer.y()
        if pointer < edge:
            delta = -max(8, min(64, edge - pointer))
        elif pointer > self.viewport().height() - edge:
            delta = max(8, min(64, pointer - self.viewport().height() + edge))
        else:
            self._rubber_timer.stop()
            return
        bar.setValue(bar.value() + delta)
        self._update_rubber_selection(self._rubber_pointer)

    def _stop_rubber_selection(self) -> None:
        self._rubber_origin = None
        self._rubber_active = False
        self._rubber_base_rows = set()
        self._rubber_timer.stop()
        if self.state() == QAbstractItemView.State.DragSelectingState:
            self.setState(QAbstractItemView.State.NoState)
        self.viewport().update()

    def cancelSelectionDrag(self) -> bool:
        """Cancel a pending rubber band before a host consumes its Escape shortcut."""
        active = self._rubber_origin is not None
        if active:
            self._stop_rubber_selection()
        return active

    def keyPressEvent(self, event: QKeyEvent) -> None:
        actions = {Qt.Key.Key_Left: self.CursorAction.MoveLeft,
                   Qt.Key.Key_Right: self.CursorAction.MoveRight,
                   Qt.Key.Key_Up: self.CursorAction.MoveUp,
                   Qt.Key.Key_Down: self.CursorAction.MoveDown,
                   Qt.Key.Key_Home: self.CursorAction.MoveHome,
                   Qt.Key.Key_End: self.CursorAction.MoveEnd,
                   Qt.Key.Key_PageUp: self.CursorAction.MovePageUp,
                   Qt.Key.Key_PageDown: self.CursorAction.MovePageDown}
        action = actions.get(event.key())
        if action is not None and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            target = self.moveCursor(action, event.modifiers())
            if target.isValid():
                if not self._range_anchor.isValid():
                    self._range_anchor = QPersistentModelIndex(self.currentIndex())
                additive = bool(event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
                self._select_range(target, additive)
                self.scrollTo(target)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Escape and self._rubber_origin is not None:
            self._stop_rubber_selection()
            event.accept()
            return
        super().keyPressEvent(event)
        if action is not None and not event.modifiers():
            self._range_anchor = QPersistentModelIndex(self.currentIndex())

    def visualRegionForSelection(self, selection: QItemSelection) -> QRegion:
        region = QRegion()
        for index in self.visibleIndexes():
            if selection.contains(index):
                region |= QRegion(self.visualRect(index))
        return region

    def moveCursor(self, action: QAbstractItemView.CursorAction, modifiers: Qt.KeyboardModifier) -> QModelIndex:
        del modifiers
        self._ensure_layout()
        model = self.model()
        count = len(self._rectangles)
        if model is None or not count:
            return QModelIndex()
        current = self.currentIndex().row()
        if current < 0:
            return model.index(0, 0, self.rootIndex())
        if action == self.CursorAction.MoveHome:
            target = 0
        elif action == self.CursorAction.MoveEnd:
            target = count - 1
        elif action in (self.CursorAction.MoveNext, self.CursorAction.MovePrevious):
            target = max(0, min(count - 1, current + (1 if action == self.CursorAction.MoveNext else -1)))
        else:
            origin = self._rectangles[current].center()
            vertical = action in (self.CursorAction.MoveUp, self.CursorAction.MoveDown,
                                  self.CursorAction.MovePageUp, self.CursorAction.MovePageDown)
            forward = action in (self.CursorAction.MoveDown, self.CursorAction.MoveRight, self.CursorAction.MovePageDown)
            page = action in (self.CursorAction.MovePageUp, self.CursorAction.MovePageDown)
            desired = self.viewport().height() if page else 0
            candidates: list[tuple[float, int]] = []
            for row, rect in enumerate(self._rectangles):
                if row == current:
                    continue
                center = rect.center()
                along = center.y() - origin.y() if vertical else center.x() - origin.x()
                across = center.x() - origin.x() if vertical else center.y() - origin.y()
                if (forward and along > 0) or (not forward and along < 0):
                    candidates.append((abs(abs(along) - desired) + abs(across) * 2.0, row))
            target = min(candidates)[1] if candidates else current
        return model.index(target, 0, self.rootIndex())

    def paintEvent(self, event: QPaintEvent) -> None:
        self._ensure_layout()
        painter = QPainter(self.viewport())
        try:
            painter.setClipRegion(event.region())
            base = QStyleOptionViewItem()
            self.initViewItemOption(base)
            selection = self.selectionModel()
            current = self.currentIndex()
            for index in self.visibleIndexes():
                option = QStyleOptionViewItem(base)
                option.rect = self.visualRect(index)
                option.widget = self.viewport()
                option.state &= ~(QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus | QStyle.StateFlag.State_MouseOver)
                if selection is not None and selection.isSelected(index):
                    option.state |= QStyle.StateFlag.State_Selected
                if self.hasFocus() and current.row() == index.row():
                    option.state |= QStyle.StateFlag.State_HasFocus
                if self._hover_row == index.row():
                    option.state |= QStyle.StateFlag.State_MouseOver
                self.itemDelegateForIndex(index).paint(painter, option, index)
            if self._rubber_active:
                rect = self._rubber_rectangle()
                color = self.palette().highlight().color()
                painter.setPen(QPen(color, 1.0))
                fill = QColor(color)
                fill.setAlpha(35)
                painter.setBrush(fill)
                painter.drawRect(rect)
        finally:
            painter.end()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._rubber_origin is not None:
            self._update_rubber_selection(event.position().toPoint())
            event.accept()
            return
        row = self.indexAt(event.position().toPoint()).row()
        if row != self._hover_row:
            self._hover_row = row
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self._hover_row = -1
        self.viewport().update()
        super().leaveEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        self._anchor = self._capture_anchor()
        super().resizeEvent(event)
        self._schedule_layout()

    def hideEvent(self, event: QHideEvent) -> None:
        self._stop_rubber_selection()
        self._range_anchor = QPersistentModelIndex()
        self._shift_press_handled = False
        super().hideEvent(event)

    def updateGeometries(self) -> None:
        self._schedule_layout()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        del dx, dy
        if not self._laying_out and self.state() == QAbstractItemView.State.EditingState:
            self.updateEditorGeometries()
        self.viewport().update()
