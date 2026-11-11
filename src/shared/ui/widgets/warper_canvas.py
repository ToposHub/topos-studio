"""Color Warper presentation and pointer gestures; no image-processing backend.

The legacy Hue/Saturation coordinate encoding remains in ColorWarper. Display
rotation is deliberately separate so old projects keep the same hue mapping.
Other modes publish UI snapshots only, never a legacy warp texture.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFocusEvent, QImage, QKeyEvent, QMouseEvent, QPainter,
    QPainterPath, QPaintEvent, QPen, QPolygonF,
)
from PyQt6.QtWidgets import QWidget

from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.color_warper import ColorWarper

PointKey = tuple[int, int]


class WarperCanvas(ColorWarper):
    """Editable display mesh for HS or either of the two Chroma/Luma grids."""

    ui_edited = pyqtSignal()
    selection_changed = pyqtSignal()
    grid_activated = pyqtSignal(int)

    def __init__(self, mode: str = "hue_saturation", parent: QWidget | None = None) -> None:
        self.mode = mode
        self.ring_count = 6
        self.active_grid = 0
        self.selected: set[PointKey] = set()
        self.pins: set[PointKey] = set()
        self.luma_values: dict[PointKey, float] = {}
        self._grid_states: dict[int, tuple[np.ndarray, set[PointKey], set[PointKey]]] = {}
        self.tool = "select"
        self.falloff = 0
        self.auto_lock = False
        self.lock_points = 1
        self.lock_axis = "columns"
        self.axis_angle = 0.0
        self.color_space = "HSP"
        self.show_grid = True
        self.show_density = True
        self.density_image: QImage | None = None
        self._background: tuple[tuple[int, int, str, float], QImage] | None = None
        self._lasso: list[QPointF] = []
        self._drag_origin: np.ndarray | None = None
        self._drag_points: np.ndarray | None = None
        self._gesture_active = False
        self._moved = False
        self._deselect_on_release: PointKey | None = None
        self._component_gesture = False
        self._range = (0.0, 1.0)
        super().__init__(parent)
        self._padding = 29
        self.setMinimumSize(180, 160)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def _initial_position(self, r: int, t: int) -> np.ndarray:
        if self.mode == "chroma_luma":
            return np.array([-1.0 + 2.0 * t / self.grid_size,
                             -1.0 + 2.0 * r / self.ring_count], dtype=np.float32)
        angle = -(t / self.grid_size) * math.tau - math.pi / 2
        radius = (r + 1) / self.ring_count
        return np.array([radius * math.cos(angle), radius * math.sin(angle)], dtype=np.float32)

    def _keys(self) -> list[PointKey]:
        return [(r, t) for r in range(self.points.shape[0]) for t in range(self.points.shape[1])]

    def reset_grid(self) -> None:
        self.cancel_gesture()
        rows = self.ring_count + (self.mode == "chroma_luma")
        cols = self.grid_size + (self.mode == "chroma_luma")
        self.points = np.array([[self._initial_position(r, t) for t in range(cols)]
                                for r in range(rows)], dtype=np.float32)
        self.selected.clear()
        self.pins.clear()
        self.luma_values.clear()
        self.hover_point = None
        self.dragging_point = None
        self._grid_states.clear()
        self.selection_changed.emit()
        self.update()

    def cancel_gesture(self) -> None:
        timer = getattr(self, "_changed_timer", None)
        if timer is not None:
            timer.stop()
        self._pending_deltas = None
        self.dragging_point = None
        self._drag_origin = None
        self._drag_points = None
        self._deselect_on_release = None
        self._component_gesture = False
        self._lasso.clear()
        if self._gesture_active:
            self._gesture_active = False
            self.interaction_ended.emit()

    def set_grid_size(self, size: int) -> None:
        self.set_resolution(size, size)

    def set_resolution(self, columns: int, rows: int) -> None:
        if columns < 3 or rows < 1 or columns > 24 or rows > 24:
            raise ValueError("Warper resolution must be between 3 and 24")
        self.grid_size, self.ring_count = columns, rows
        self.reset_grid()

    def reset(self) -> None:
        self.reset_grid()
        self.ui_edited.emit()

    def activate_grid(self, index: int) -> None:
        if index == self.active_grid:
            return
        self.cancel_gesture()
        self._grid_states[self.active_grid] = (self.points.copy(), set(self.selected), set(self.pins))
        self.active_grid = index
        if index in self._grid_states:
            points, selected, pins = self._grid_states[index]
            self.points, self.selected, self.pins = points.copy(), set(selected), set(pins)
        else:
            self.points = np.array([[self._initial_position(r, t) for t in range(self.grid_size + 1)]
                                    for r in range(self.ring_count + 1)], dtype=np.float32)
            self.selected, self.pins = set(), set()
        self.hover_point = None
        self.selection_changed.emit()
        self.grid_activated.emit(index)
        self.update()

    def snapshot(self) -> dict[str, Any]:
        grids = {str(i): {"points": p.tolist(), "selected": sorted(s), "pins": sorted(n)}
                 for i, (p, s, n) in self._grid_states.items()}
        grids[str(self.active_grid)] = {"points": self.points.tolist(),
                                       "selected": sorted(self.selected), "pins": sorted(self.pins)}
        return {"mode": self.mode, "resolution": [self.grid_size, self.ring_count],
                "active_grid": self.active_grid, "grids": grids, "tool": self.tool,
                "falloff": self.falloff, "auto_lock": self.auto_lock,
                "lock_points": self.lock_points, "lock_axis": self.lock_axis,
                "range": list(self._range), "color_space": self.color_space,
                "luma": [[r, t, v] for (r, t), v in sorted(self.luma_values.items())],
                "axis_angle": self.axis_angle}

    def grid_rect(self, index: int = 0) -> QRectF:
        if self.mode == "chroma_luma":
            return self.field_rect(index).adjusted(16, 16, -16, -16)
        side = max(1.0, min(self.width(), self.height()) - self._padding)
        return QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)

    def field_rect(self, index: int = 0) -> QRectF:
        """Square color field, centered in either half of the CL canvas."""
        half = self.width() / 2.0
        side = max(33.0, min(half, self.height()))
        return QRectF(index * half + (half - side) / 2, (self.height() - side) / 2, side, side)

    def point_screen(self, key: PointKey, index: int | None = None) -> QPointF:
        rect = self.grid_rect(self.active_grid if index is None else index)
        x, y = self.points[key]
        if self.mode == "hue_saturation":
            angle = -math.pi / 6
            x, y = x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle)
        return rect.center() + QPointF(float(x) * rect.width() / 2, float(y) * rect.height() / 2)

    def _normalized(self, pos: QPointF) -> np.ndarray:
        rect = self.grid_rect(self.active_grid)
        x, y = (pos.x() - rect.center().x()) * 2 / rect.width(), (pos.y() - rect.center().y()) * 2 / rect.height()
        if self.mode == "hue_saturation":
            angle = math.pi / 6
            x, y = x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle)
        return np.array([x, y], dtype=np.float32)

    def _get_point_at(self, pos: QPointF) -> PointKey | None:
        distances = [(math.hypot((pos - self.point_screen(key)).x(),
                                (pos - self.point_screen(key)).y()), key) for key in self._keys()]
        distance, key = min(distances, default=(float("inf"), None))
        return key if distance < 8 else None

    @staticmethod
    def _rgb(hue: np.ndarray, sat: np.ndarray, value: np.ndarray) -> np.ndarray:
        phase = np.mod(hue[..., None] * 6.0 + np.array([5.0, 3.0, 1.0]), 6.0)
        rgb = value[..., None] * (1.0 - sat[..., None] * np.clip(np.minimum(phase, 4.0 - phase), 0.0, 1.0))
        return (np.clip(rgb, 0.0, 1.0) * 255).astype(np.uint8)

    def _background_image(self) -> QImage:
        # Fixed display raster: this is a color-field backdrop, never frame data.
        scale = min(1.0, 900 / max(1, self.width()), 700 / max(1, self.height()))
        width, height = max(1, round(self.width() * scale)), max(1, round(self.height() * scale))
        cache_key = (self.width(), self.height(), self.mode, self.axis_angle)
        if self._background is not None and self._background[0] == cache_key:
            return self._background[1]
        if self.mode == "hue_saturation":
            yy, xx = np.mgrid[0:height, 0:width]
            side = max(1, min(self.width(), self.height()) - self._padding)
            x = (xx * self.width() / width - self.width() / 2) * 2 / side
            y = (yy * self.height() / height - self.height() / 2) * 2 / side
            hue = np.mod((-np.arctan2(y, x) - 2 * math.pi / 3) / math.tau, 1.0)
            sat = np.minimum(np.hypot(x, y), 1.0)
            square_half = min(self.width(), self.height()) / 2
            outside = np.abs(xx * self.width() / width - self.width() / 2) > square_half
            value = np.where(outside, 0.10, 0.32)
        else:
            rgb = self._chroma_luma_field(
                np.arange(width, dtype=np.float32) * (self.width() / width),
                np.arange(height, dtype=np.float32) * (self.height() / height),
            )
        if self.mode == "hue_saturation":
            rgb = self._rgb(hue, sat, value)
        rgb = np.ascontiguousarray(rgb)
        result = QImage(rgb.data, width, height, width * 3, QImage.Format.Format_RGB888).copy()
        self._background = (cache_key, result)
        return result

    def _chroma_luma_field(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        # RGB display stops measured in clean parts of the reference field,
        # normalized from the screenshot's monitor ICC profile to sRGB. The
        # display calibration/profile itself is deliberately not embedded.
        # A horizontal HSV sweep introduces extra hue bends and a grey seam;
        # the reference instead has a neutral axis and a linear luma ramp.
        palettes = np.array([
            [[128, 131, 41], [126, 130, 65], [126, 129, 85], [125, 127, 105], [124, 124, 124],
             [121, 122, 140], [118, 116, 164], [113, 110, 194], [104, 96, 232]],
            [[0, 151, 103], [33, 145, 110], [77, 139, 115], [103, 132, 119], [124, 124, 124],
             [141, 116, 124], [158, 104, 128], [178, 89, 129], [198, 65, 129]],
        ], dtype=np.float32)
        rgb = np.empty((y.size, x.size, 3), dtype=np.float32)
        stops = np.linspace(0.0, 1.0, palettes.shape[1])
        angle = math.radians(self.axis_angle)
        weights = ((math.cos(angle), math.sin(angle)), (-math.sin(angle), math.cos(angle)))
        for index in (0, 1):
            rect = self.field_rect(index)
            begin = int(np.searchsorted(x, index * self.width() / 2))
            end = int(np.searchsorted(x, (index + 1) * self.width() / 2))
            u = (x[begin:end] - rect.left()) / rect.width()
            v = (y - rect.top()) / rect.height()
            gain = np.maximum(0.0, 1.022 - 0.899 * v)
            gain *= np.where((v >= 0) & (v <= 1), 1.0, 0.5)
            for channel in range(3):
                horizontal = np.full_like(u, 124.0)
                for axis, weight in enumerate(weights[index]):
                    if abs(weight) < 1e-7:
                        continue
                    axis_u = u if weight >= 0 else 1 - u
                    horizontal += abs(weight) * (np.interp(axis_u, stops, palettes[axis, :, channel]) - 124.0)
                horizontal *= np.where((u >= 0) & (u <= 1), 1.0, 0.5)
                # Interpolate along x and compute luma along y only once,
                # then broadcast; dragging Axis Angle never interpolates a
                # full two-dimensional raster per color channel.
                rgb[:, begin:end, channel] = horizontal[None, :] * gain[:, None]
        np.rint(rgb, out=rgb)
        np.clip(rgb, 0, 255, out=rgb)
        return rgb.astype(np.uint8)

    def set_density_image(self, image: QImage | None) -> None:
        """Accept a backend-produced overlay; never fabricate a distribution."""
        self.density_image = image.copy() if image is not None else None
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawImage(self.rect(), self._background_image())
        if self.show_density and self.density_image is not None:
            p.drawImage(self.rect(), self.density_image)
        grids = (0, 1) if self.mode == "chroma_luma" else (0,)
        original_points = self.points
        for index in grids:
            if index != self.active_grid:
                self.points = self._grid_states.get(index, (np.array([
                    [self._initial_position(r, t) for t in range(self.grid_size + 1)]
                    for r in range(self.ring_count + 1)]), set(), set()))[0]
            else:
                self.points = original_points
            if self.show_grid:
                self._paint_mesh(p, index)
            if self.mode == "chroma_luma":
                p.setPen(QColor(C.BORDER_DARK)); p.drawLine(QPointF(self.width() / 2, 0), QPointF(self.width() / 2, self.height()))
            self.points = original_points
        if len(self._lasso) > 1:
            p.setPen(QPen(QColor(C.ORANGE), 1, Qt.PenStyle.DashLine))
            p.setBrush(QColor(255, 136, 0, 25))
            p.drawPolygon(QPolygonF(self._lasso))
        p.end()

    def _paint_mesh(self, p: QPainter, index: int) -> None:
        active = index == self.active_grid or self.mode == "hue_saturation"
        if self.mode == "chroma_luma":
            rect = self.grid_rect(index)
            p.setPen(QPen(QColor(0, 0, 0, 60), 0.75))
            p.drawLine(rect.topLeft(), rect.bottomRight())
            p.drawLine(rect.topRight(), rect.bottomLeft())
        p.setPen(QPen(QColor(205, 205, 210, 125), 0.75))
        rows, cols = self.points.shape[:2]
        for r in range(rows):
            for t in range(cols):
                key = (r, t)
                current = self.point_screen(key, index)
                if r > 0:
                    p.drawLine(current, self.point_screen((r - 1, t), index))
                elif self.mode == "hue_saturation":
                    p.drawLine(current, self.grid_rect().center())
                if self.mode == "hue_saturation" or t + 1 < cols:
                    p.drawLine(current, self.point_screen((r, (t + 1) % cols), index))
        for key in self._keys():
            pos = self.point_screen(key, index)
            outer = (key[0] in {0, rows - 1} and key[1] in {0, cols - 1}
                     if self.mode == "chroma_luma" else key[0] == rows - 1)
            selected, pinned = active and key in self.selected, active and key in self.pins
            p.setPen(QPen(QColor("#111113"), 2 if outer else (1 if pinned else 0.3)))
            p.setBrush(QColor(C.ORANGE if selected else "#e1e1e3"))
            radius = 3.5 if selected or (active and key == self.hover_point) else (3 if outer else 2)
            if pinned:
                p.drawRect(QRectF(pos.x() - 3, pos.y() - 3, 6, 6))
            else:
                p.drawEllipse(pos, radius, radius)
        if self.mode == "hue_saturation":
            p.setPen(QPen(QColor("#171719"), 2)); p.setBrush(QColor("#cccccc"))
            p.drawEllipse(self.grid_rect().center(), 3, 3)

    def _start_edit(self) -> None:
        if not self._gesture_active:
            self._gesture_active = True
            self.interaction_started.emit()

    def _publish_mesh(self) -> None:
        self.ui_edited.emit()
        if self.mode == "hue_saturation" and self.ring_count == self.grid_size:
            self._pending_deltas = self._compute_deltas()
            if not self._changed_timer.isActive():
                self._changed_timer.start()
        self.selection_changed.emit()
        self.update()

    def _end_edit(self) -> None:
        if self._changed_timer.isActive():
            self._changed_timer.stop()
            self._emit_pending_change()
        if self._gesture_active:
            self._gesture_active = False
            self.interaction_ended.emit()

    def begin_component_drag(self) -> None:
        self._component_gesture = True
        self._start_edit()

    def end_component_drag(self) -> None:
        self._component_gesture = False
        self._end_edit()

    def _auto_pin(self) -> None:
        if not self.auto_lock:
            return
        for key in self._keys():
            if key in self.selected:
                continue
            for r, t in self.selected:
                dr, dt = abs(key[0] - r), abs(key[1] - t)
                if self.mode == "hue_saturation":
                    dt = min(dt, self.grid_size - dt)
                distance = dt if self.lock_axis == "columns" else dr
                if self.lock_axis == "single":
                    distance = max(dr, dt)
                if distance == self.lock_points:
                    self.pins.add(key)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        self._moved = False
        self._deselect_on_release = None
        if self.mode == "chroma_luma":
            self.activate_grid(int(event.position().x() >= self.width() / 2))
        key = self._get_point_at(event.position())
        additive = bool(event.modifiers() & (Qt.KeyboardModifier.ShiftModifier |
                                            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
        if self.tool == "draw":
            if not additive:
                self.selected.clear()
            self._lasso = [event.position()]
            self.selection_changed.emit()
            return
        if key is None:
            if not additive:
                self.selected.clear()
            self.selection_changed.emit(); self.update()
            return
        if self.tool == "pin":
            self.pins.symmetric_difference_update({key})
            self.ui_edited.emit(); self.update()
            return
        if additive:
            self.selected.symmetric_difference_update({key})
        elif key not in self.selected:
            self.selected = {key}
        else:
            self._deselect_on_release = key
        self._auto_pin()
        if key not in self.pins and key in self.selected:
            self.dragging_point = key
            self._drag_origin = self._normalized(event.position())
            self._drag_points = self.points.copy()
            self._moved = False
        self.selection_changed.emit(); self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._lasso:
            self._lasso.append(event.position()); self.update()
        elif self.dragging_point is not None and self._drag_points is not None:
            delta = self._normalized(event.position()) - self._drag_origin
            if np.linalg.norm(delta) < 0.004 and not self._moved:
                return
            self._moved = True
            self._start_edit()
            target = self._drag_points.copy()
            for key in self._keys():
                if key in self.pins:
                    continue
                weight = 1.0 if key in self.selected else 0.0
                if self.falloff and self.selected:
                    distance = min(abs(key[0] - s[0]) +
                                   (min(abs(key[1] - s[1]), self.grid_size - abs(key[1] - s[1]))
                                    if self.mode == "hue_saturation" else abs(key[1] - s[1]))
                                   for s in self.selected)
                    weight = max(weight, max(0.0, 1 - distance / (self.falloff + 1)))
                if self.tool in {"pull", "push"}:
                    distance = np.linalg.norm(self._drag_points[key] - self._drag_points[self.dragging_point])
                    direction = self._drag_points[key] - self._drag_points[self.dragging_point]
                    if distance > 0:
                        target[key] += direction / distance * np.linalg.norm(delta) * (1 if self.tool == "push" else -1) * max(0, 1 - distance)
                else:
                    target[key] += delta * weight
            # Only mesh display bounds, not an image/HDR pixel clamp.
            self.points = np.clip(target, -1.1, 1.1)
            self._publish_mesh()
        else:
            key = self._get_point_at(event.position())
            if self.hover_point != key:
                self.hover_point = key; self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._lasso:
            path = QPainterPath(); path.addPolygon(QPolygonF(self._lasso)); path.closeSubpath()
            self.selected.update(key for key in self._keys() if path.contains(self.point_screen(key)))
            self._lasso.clear(); self.selection_changed.emit(); self.ui_edited.emit()
        if self._deselect_on_release is not None and not self._moved:
            self.selected.discard(self._deselect_on_release)
            self.selection_changed.emit()
        self._deselect_on_release = None
        self.dragging_point = None
        self._drag_origin = self._drag_points = None
        self._end_edit(); self.update()

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self._end_edit()
        self.cancel_gesture()
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            if self._drag_points is not None and self._moved:
                self.points = self._drag_points.copy(); self._publish_mesh()
            self._end_edit(); self.cancel_gesture(); self.selected.clear()
            self.selection_changed.emit(); self.update()
        elif event.key() == Qt.Key.Key_A and event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            self.perform("all")
        elif event.key() in {Qt.Key.Key_Backspace, Qt.Key.Key_Delete}:
            self.perform("reset_points")
        else:
            super().keyPressEvent(event)

    def set_range(self, low: float, high: float) -> None:
        self._range = (low, high)
        if self.mode == "hue_saturation":
            self.selected = {key for key in self._keys()
                             if low <= self._hue_from_pos(self._initial_position(*key)) <= high}
        else:
            self.selected = {key for key in self._keys() if low <= key[1] / self.grid_size <= high}
        self.selection_changed.emit(); self.ui_edited.emit(); self.update()

    def perform(self, action: str) -> None:
        keys = set(self._keys())
        if action == "invert":
            self.selected = keys - self.selected
        elif action == "convert_pin":
            self.pins.update(self.selected); self.selected.clear()
        elif action in {"column", "ring", "row"}:
            seed = self.selected or ({self.hover_point} if self.hover_point is not None else set())
            axis = 1 if action == "column" else 0
            selected = {k[axis] for k in seed}
            result = {k for k in keys if k[axis] in selected}
            if self.tool == "pin":
                self.pins.symmetric_difference_update(result)
            else:
                self.selected.update(result)
        elif action == "all":
            if self.tool == "pin":
                self.pins = set() if self.pins == keys else keys
            else:
                self.selected = set() if self.selected == keys else keys
        elif action == "reset_selection":
            self.selected.clear(); self.pins.clear(); self.falloff = 0
        elif action.startswith("falloff_"):
            self.falloff = max(0, min(6, self.falloff + (1 if action == "falloff_add" else -1)))
        elif action == "reset_points":
            self._start_edit()
            for key in self.selected - self.pins:
                self.points[key] = self._initial_position(*key)
            self._publish_mesh(); self._end_edit()
        self.selection_changed.emit(); self.ui_edited.emit(); self.update()

    def edit_component(self, component: str, value: float | None = None, smooth: bool = False) -> None:
        editable = self.selected - self.pins
        if not editable:
            return
        if self.mode == "hue_saturation" and component == "luma":
            for key in editable:
                self.luma_values[key] = 0.5 if value is None else value
            self.ui_edited.emit()
            self.selection_changed.emit()
            return
        self._start_edit()
        original = self.points.copy()
        for key in editable:
            point = self.points[key]
            initial = self._initial_position(*key)
            r, t = key
            neighbor_keys = [(rr, tt) for rr, tt in ((r - 1, t), (r + 1, t), (r, t - 1), (r, t + 1))
                             if 0 <= rr < original.shape[0] and 0 <= tt < original.shape[1]]
            if self.mode == "chroma_luma":
                axis = 0 if component == "chroma" else 1
                point[axis] = initial[axis] if value is None else (value if axis == 0 else 1 - 2 * value)
                if smooth and neighbor_keys:
                    point[axis] = (original[key][axis] + np.mean([original[k][axis] for k in neighbor_keys])) / 2
            elif component in {"hue", "sat"}:
                hue = self._hue_from_pos(original[key])
                radius = float(np.linalg.norm(original[key]))
                if component == "hue":
                    hue = self._hue_from_pos(initial) if value is None else value
                    if smooth and neighbor_keys:
                        angles = [self._hue_from_pos(original[k]) * math.tau for k in [key, *neighbor_keys]]
                        hue = math.atan2(sum(math.sin(a) for a in angles), sum(math.cos(a) for a in angles)) / math.tau % 1
                else:
                    radius = float(np.linalg.norm(initial)) if value is None else value
                    if smooth and neighbor_keys:
                        radius = (np.linalg.norm(original[key]) + np.mean([np.linalg.norm(original[k]) for k in neighbor_keys])) / 2
                angle = -hue * math.tau - math.pi / 2
                self.points[key] = [radius * math.cos(angle), radius * math.sin(angle)]
        self._publish_mesh()
        if not self._component_gesture:
            self._end_edit()

    def selected_values(self) -> dict[str, float | None]:
        if not self.selected:
            return {}
        points = np.array([self.points[key] for key in sorted(self.selected)])
        if self.mode == "hue_saturation":
            values = {"hue": np.array([self._hue_from_pos(p) for p in points]), "sat": np.linalg.norm(points, axis=1),
                      "luma": np.array([self.luma_values.get(key, 0.5) for key in sorted(self.selected)])}
        else:
            values = {"chroma": points[:, 0], "luma": (1 - points[:, 1]) / 2}
        return {key: float(v[0]) if np.ptp(v) < 1e-5 else None for key, v in values.items()}
