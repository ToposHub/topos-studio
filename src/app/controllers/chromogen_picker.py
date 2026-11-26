"""Route the Chromogen eyedropper through the Color viewer's render input tap."""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np
from PyQt6.QtCore import QEvent, QObject, QPointF, Qt
from PyQt6.QtGui import QCursor, QKeyEvent, QMouseEvent

from src.shared.color.color_picker import create_dropper_cursor

if TYPE_CHECKING:
    from src.features.color.ui.canvas import Canvas
    from src.features.color.ui.chromogen_panel import ChromogenPanel


class ChromogenPicker(QObject):
    def __init__(
        self, canvas: Canvas, panel: ChromogenPanel, *,
        refresh: Callable[[], object], available: Callable[[], bool],
        disarm_peers: Callable[[], None],
    ) -> None:
        super().__init__(canvas)
        self._canvas = canvas
        self._viewport = canvas.viewport()
        self._panel = panel
        self._refresh = refresh
        self._available = available
        self._disarm_peers = disarm_peers
        self._mode: str | None = None
        self._key: tuple | None = None
        self._sample: tuple | None = None
        self._click: QPointF | None = None
        self._drag_start: QPointF | None = None
        self._dragging = False
        self._cursor: QCursor | None = None
        self._previous_cursor: tuple[bool, QCursor] | None = None
        self._viewport.installEventFilter(self)
        panel.pick_requested.connect(self._toggle_color)
        panel.pick_hue_requested.connect(self._toggle_hue)

    def _toggle_color(self, enabled: bool) -> None:
        self._toggle("color", enabled)

    def _toggle_hue(self, enabled: bool) -> None:
        self._toggle("hue", enabled)

    def _toggle(self, mode: str, enabled: bool) -> None:
        if not enabled:
            if self._mode == mode:
                self.cancel()
            return
        if mode != self._mode:
            self._click = None
            self._drag_start = None
            self._dragging = False
        self._mode = mode
        self._disarm_peers()
        if self._viewport.underMouse() and self._available():
            self._show_cursor(True)
        self._refresh()

    def cancel(self) -> None:
        self._mode = self._key = self._sample = self._click = None
        self._drag_start = None
        self._dragging = False
        self._show_cursor(False)
        self._panel.set_pick_active(False)

    def request(self, node: str, source: tuple, revision: tuple) -> tuple | None:
        if (self._mode is None or not self._panel.isVisible() or not self._panel.isEnabled()
                or not self._available()):
            return None
        key = (node, source, revision, "chromogen")
        if key != self._key:
            if self._key is not None and key[:2] != self._key[:2]:
                self._click = None
                self._drag_start = None
                self._dragging = False
                self._panel.set_picked_color(None)
            self._key = key
            self._sample = None
        return None if self._sample is not None else key

    def accept(self, sample: tuple) -> None:
        request, rect, _, rgb = sample
        if request == self._key and rect is not None and self._panel.isEnabled() and self._available():
            self._sample = rect, rgb
            if self._click is not None and not self._dragging:
                self._pick(self._click)

    def _pick(self, position: QPointF) -> None:
        if self._sample is None:
            self._click = position
            self._refresh()
            return
        self._click = None
        (left, top, width, height), rgb = self._sample
        point = self._canvas.mapToScene(position.toPoint())
        if width <= 0 or height <= 0 or not self._canvas.sceneRect().contains(point):
            return
        x, y = (point.x() - left) / width, (point.y() - top) / height
        if 0.0 <= x < 1.0 and 0.0 <= y < 1.0:
            start, self._drag_start = self._drag_start, None
            if self._mode == "hue" and start is not None and (position - start).manhattanLength() >= 5.0:
                origin = self._canvas.mapToScene(start.toPoint())
                x0 = np.clip((origin.x() - left) / width, 0.0, 1.0)
                y0 = np.clip((origin.y() - top) / height, 0.0, 1.0)
                columns = np.minimum(np.linspace(x0, x, 5) * rgb.shape[1], rgb.shape[1] - 1).astype(int)
                rows = np.minimum(np.linspace(y0, y, 5) * rgb.shape[0], rgb.shape[0] - 1).astype(int)
                value = rgb[np.ix_(rows, columns)].mean(axis=(0, 1))
            else:
                value = rgb[int(y * rgb.shape[0]), int(x * rgb.shape[1])]
            if np.isfinite(value).all():
                self._panel.set_picked_color(tuple(float(channel) for channel in value))
                self.cancel()

    def _show_cursor(self, visible: bool) -> None:
        if visible and self._previous_cursor is None:
            if self._cursor is None:
                self._cursor = create_dropper_cursor()
            self._previous_cursor = (
                self._viewport.testAttribute(Qt.WidgetAttribute.WA_SetCursor), self._viewport.cursor())
            self._viewport.setCursor(self._cursor)
        elif not visible and self._previous_cursor is not None:
            had_cursor, cursor = self._previous_cursor
            self._previous_cursor = None
            if self._viewport.cursor() == self._cursor:
                if had_cursor:
                    self._viewport.setCursor(cursor)
                else:
                    self._viewport.unsetCursor()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not self._viewport or self._mode is None:
            return super().eventFilter(watched, event)
        kind = event.type()
        if (isinstance(event, QKeyEvent) and kind == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape):
            self.cancel()
            return True
        if (not self._panel.isVisible() or not self._panel.isEnabled() or not self._available()
                or kind in (QEvent.Type.Leave, QEvent.Type.WindowDeactivate, QEvent.Type.UngrabMouse)):
            self._show_cursor(False)
            self._dragging = False
            self._click = None
            self._drag_start = None
            return False
        if kind == QEvent.Type.Enter:
            self._show_cursor(True)
        if isinstance(event, QMouseEvent):
            if not self._dragging and not self._canvas.accepts_warper_picker_at(event.position()):
                self._show_cursor(False)
                return False
            self._show_cursor(True)
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._drag_start = event.position()
                self._dragging = True
                return True
            if self._dragging and kind == QEvent.Type.MouseMove:
                return True
            if self._dragging and kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self._dragging = False
                self._pick(event.position())
                return True
        return False
