"""Color Warper presentation and pointer gestures; no image-processing backend.

The legacy Hue/Saturation coordinate encoding remains in ColorWarper. Display
rotation is separate so saved grades keep the same hue mapping.
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

from src.shared.color.warper_mesh import chroma_luma_mesh
from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.color_warper import ColorWarper

PointKey = tuple[int, int]
WHITE_POINT: PointKey = (-1, 0)
CALIBRATED_SAT_SCALE = 2.0 / math.sqrt(3.0)
CALIBRATED_MAX_RADIUS = 1.6
WHITE_POINT_MAX_RADIUS = 0.65


class WarperCanvas(ColorWarper):
    """Editable display mesh for HS or either of the two Chroma/Luma grids."""

    ui_edited = pyqtSignal()
    selection_changed = pyqtSignal()
    grid_activated = pyqtSignal(int)
    interaction_cancelled = pyqtSignal()
    isolation_changed = pyqtSignal(object)

    def __init__(self, mode: str = "hue_saturation", parent: QWidget | None = None) -> None:
        self.mode = mode
        self.ring_count = 6
        self.active_grid = 0
        self.selected: set[PointKey] = set()
        self.pins: set[PointKey] = set()
        self.luma_values: dict[PointKey, float] = {}
        self.white_point = np.zeros(2, np.float32)
        self._identity: np.ndarray | None = None
        self._display_meshes: dict[int, tuple[tuple, np.ndarray]] = {}
        self.viewer_sample: np.ndarray | None = None
        self._grid_states: dict[int, tuple[np.ndarray, set[PointKey], set[PointKey]]] = {}
        self.tool = "select"
        self.falloff = 0
        self.auto_lock = False
        self.lock_points = 1
        self.lock_axis = "columns"
        self.axis_angle = 0.0
        self.color_space = "HSP"
        self.calibrated_hs = False
        self.persistent_mesh = False
        self.show_grid = True
        self.show_density = True
        self.density_image: QImage | None = None
        self.density_overlays: tuple[QImage, ...] = ()
        self._background: tuple[tuple[int, int, str, float], QImage] | None = None
        self._lasso: list[QPointF] = []
        self._box_origin: QPointF | None = None
        self._box_end: QPointF | None = None
        self._pin_brush = False
        self._isolating = False
        self._drag_origin: np.ndarray | None = None
        self._drag_points: np.ndarray | None = None
        self._drag_display: np.ndarray | None = None
        self._drag_white = self.white_point.copy()
        self._push_location: np.ndarray | None = None
        self._gesture_active = False
        self._moved = False
        self._deselect_on_release: PointKey | None = None
        self._component_gesture = False
        self._calibrated_base: tuple[np.ndarray, set[PointKey], dict[PointKey, float], np.ndarray] | None = None
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

    def _selectable_keys(self) -> list[PointKey]:
        keys = self._keys()
        if self.mode == "hue_saturation" and (self.calibrated_hs or self.persistent_mesh):
            keys.append(WHITE_POINT)
        return keys

    def reset_grid(self) -> None:
        self.cancel_gesture()
        rows = self.ring_count + (self.mode == "chroma_luma")
        cols = self.grid_size + (self.mode == "chroma_luma")
        self.points = np.array([[self._initial_position(r, t) for t in range(cols)]
                                for r in range(rows)], dtype=np.float32)
        self._identity = self.points.copy()
        self._display_meshes.clear()
        self.white_point.fill(0.0)
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
        self._drag_display = None
        self._push_location = None
        self._box_origin = None
        self._box_end = None
        self._pin_brush = False
        self._set_isolation(None)
        self._deselect_on_release = None
        self._component_gesture = False
        self._lasso.clear()
        if self._gesture_active:
            self._gesture_active = False
            if self._calibrated_base is not None:
                self.points, self.pins, self.luma_values, self.white_point = self._calibrated_base
                self._calibrated_base = None
                self.interaction_cancelled.emit()
                self.selection_changed.emit()
                self.update()
            else:
                self.interaction_ended.emit()

    def set_grid_size(self, size: int) -> None:
        self.set_resolution(size, size)

    def set_resolution(self, columns: int, rows: int) -> None:
        low, high = (1, 63) if self.mode == "chroma_luma" else (2, 64)
        if not (low <= columns <= high and low <= rows <= high):
            raise ValueError("Invalid Warper grid resolution")
        self.grid_size, self.ring_count = columns, rows
        self.reset_grid()

    def calibrated_offsets(self) -> np.ndarray:
        """Return internal hue, normalized radius, and luma offsets."""
        radius = np.linalg.norm(self.points.astype(np.float64), axis=2)
        hue = (-np.arctan2(self.points[:, :, 1], self.points[:, :, 0]) - math.pi / 2) / math.tau
        dh = (hue - np.arange(self.grid_size) / self.grid_size + 0.5) % 1.0 - 0.5
        dh[radius < 1e-7] = 0.0
        ds = radius - np.arange(1, self.ring_count + 1)[:, None] / self.ring_count
        dl = np.zeros_like(radius)
        for key, value in self.luma_values.items():
            if key != WHITE_POINT:
                dl[key] = value - 0.5
        offsets = np.stack((dh, ds, dl), axis=2).astype(np.float32)
        offsets[np.abs(offsets) < 1e-7] = 0.0
        return offsets

    def apply_calibrated_offsets(self, offsets: np.ndarray) -> None:
        """Project a hydrated HS grid into the Cartesian canvas."""
        hue = offsets[:, :, 0] + np.arange(self.grid_size) / self.grid_size
        radius = np.maximum(0.0, offsets[:, :, 1] + np.arange(1, self.ring_count + 1)[:, None] / self.ring_count)
        angle = -hue * math.tau - math.pi / 2
        self.points = np.stack((radius * np.cos(angle), radius * np.sin(angle)), axis=2).astype(np.float32)
        self.luma_values = {tuple(key): float(offsets[key[0], key[1], 2]) + 0.5
                            for key in np.argwhere(offsets[:, :, 2])}
        self.update()

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

    def display_points(self, index: int | None = None) -> np.ndarray:
        """Derive the visible mesh from the authored anchors and locked points."""
        index = self.active_grid if index is None else index
        if index == self.active_grid:
            points, pins = self.points, self.pins
        else:
            points, _, pins = self._grid_states.get(index, (self._identity, set(), set()))
        white = self.white_point if self.mode == "hue_saturation" else ()
        token = (points.tobytes(), frozenset(pins), tuple(white), frozenset(self.luma_values))
        cached = self._display_meshes.get(index)
        if cached is not None and cached[0] == token:
            return cached[1]
        if self.mode == "chroma_luma":
            constraints = np.zeros((*points.shape[:2], 3), np.float32)
            constraints[..., :2] = (points - self._identity) * 0.5
            for key in pins:
                constraints[key][2] = 1.0
            field = chroma_luma_mesh(constraints)
            display = points.copy() if field is None else self._identity + field * 2.0
        else:
            edits = np.any(abs(points - self._identity) > 1e-6, axis=2)
            edits[-1, :] = True
            for key in (pins | self.luma_values.keys()) - {WHITE_POINT}:
                edits[key] = True
            display = points.copy()
            rings = np.arange(self.ring_count + 1)
            for col in range(self.grid_size):
                anchors = np.r_[0, 1 + np.flatnonzero(edits[:, col])]
                values = np.vstack((self.white_point, points[anchors[1:] - 1, col]))
                display[:, col] = np.column_stack([
                    np.interp(rings[1:], anchors, values[:, axis]) for axis in (0, 1)
                ])
        self._display_meshes[index] = token, display
        return display

    def _screen_position(self, point: np.ndarray, index: int) -> QPointF:
        rect = self.grid_rect(index)
        x, y = point
        if self.mode == "hue_saturation":
            angle = -math.pi / 6
            x, y = x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle)
        return rect.center() + QPointF(float(x) * rect.width() / 2, float(y) * rect.height() / 2)

    def point_screen(self, key: PointKey, index: int | None = None) -> QPointF:
        index = self.active_grid if index is None else index
        point = self.white_point if key == WHITE_POINT else self.display_points(index)[key]
        return self._screen_position(point, index)

    def _normalized(self, pos: QPointF) -> np.ndarray:
        rect = self.grid_rect(self.active_grid)
        x, y = (pos.x() - rect.center().x()) * 2 / rect.width(), (pos.y() - rect.center().y()) * 2 / rect.height()
        if self.mode == "hue_saturation":
            angle = math.pi / 6
            x, y = x * math.cos(angle) - y * math.sin(angle), x * math.sin(angle) + y * math.cos(angle)
        return np.array([x, y], dtype=np.float32)

    def _point_distance(self, pos: QPointF) -> tuple[float, PointKey]:
        points = self.display_points()
        keys = self._selectable_keys()
        distances = [((pos - self._screen_position(
            self.white_point if key == WHITE_POINT else points[key], self.active_grid)).manhattanLength(), key)
                     for key in keys]
        return min(distances)

    def _nearest_point(self, pos: QPointF) -> PointKey:
        return self._point_distance(pos)[1]

    def _get_point_at(self, pos: QPointF) -> PointKey | None:
        distance, key = self._point_distance(pos)
        return key if distance < 10 else None

    def set_viewer_sample(self, point: np.ndarray | None) -> None:
        self.viewer_sample = point
        self.update()

    def affected_positions(self, key: PointKey, uv: np.ndarray) -> np.ndarray:
        """Select the input colors within an anchor's unlocked mesh neighborhood."""
        if self.mode == "hue_saturation":
            xy = uv * 2.0 - 1.0
            radius = np.linalg.norm(xy, axis=1)
            if key == WHITE_POINT:
                return radius < 1.0
            row, col = key
            degrees = (-np.arctan2(xy[:, 1], xy[:, 0]) / math.tau - 1.0 / 3.0) % 1.0
            angular = abs((degrees - col / self.grid_size + 0.5) % 1.0 - 0.5)
            anchors = {candidate[0] for candidate in (self.pins | self.luma_values.keys())
                       if candidate[1] == col}
            anchors.update(np.flatnonzero(np.any(abs(self.points[:, col] - self._identity[:, col]) > 1e-6,
                                                axis=1)))
            lower = max((anchor for anchor in anchors if anchor < row), default=-1)
            upper = min((anchor for anchor in anchors if anchor > row), default=self.ring_count - 1)
            return ((angular < 1.0 / self.grid_size) & (radius > (lower + 1) / self.ring_count)
                    & (radius <= (upper + 1) / self.ring_count))
        rows, cols = self.points.shape[:2]
        locks = np.zeros((rows, cols, 3), np.float32)
        edited = np.any(abs(self.points - self._identity) > 1e-6, axis=2)
        locks[..., 2] = edited
        for pin in self.pins:
            locks[pin][2] = 1.0
        locks[key][0] = -0.1 if key[1] == cols - 1 else 0.1
        field = chroma_luma_mesh(locks)[..., 0]
        valid = np.all((uv >= 0.0) & (uv <= 1.0) & np.isfinite(uv), axis=1)
        pos = np.clip(np.where(np.isfinite(uv), uv, 0.0), 0.0, 1.0) * (cols - 1, rows - 1)
        x, y = np.minimum(pos.astype(np.intp), (cols - 2, rows - 2)).T
        dx, dy = pos.T - (x, y)
        influence = ((field[y, x] * (1.0 - dx) + field[y, x + 1] * dx) * (1.0 - dy)
                     + (field[y + 1, x] * (1.0 - dx) + field[y + 1, x + 1] * dx) * dy)
        return valid & (abs(influence) > 1e-4)

    def _set_isolation(self, key: PointKey | None) -> None:
        if key is not None or self._isolating:
            self._isolating = key is not None
            self.isolation_changed.emit(key)

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
        if self.show_density:
            if self.density_image is not None:
                p.drawImage(self.rect(), self.density_image)
            for index, density in enumerate(self.density_overlays):
                p.drawImage(self.grid_rect(index), density)
        grids = (0, 1) if self.mode == "chroma_luma" else (0,)
        if self.show_grid:
            for index in grids:
                self._paint_mesh(p, index)
        if self.mode == "chroma_luma":
            p.setPen(QColor(C.BORDER_DARK))
            p.drawLine(QPointF(self.width() / 2, 0), QPointF(self.width() / 2, self.height()))
        if self.viewer_sample is not None:
            rect = self.grid_rect(self.active_grid)
            x, y = self.viewer_sample
            pos = QPointF(rect.left() + x * rect.width(), rect.top() + y * rect.height())
            p.setPen(QPen(QColor("#fff2ae"), 1))
            p.drawLine(pos - QPointF(4, 0), pos + QPointF(4, 0))
            p.drawLine(pos - QPointF(0, 4), pos + QPointF(0, 4))
            nearest = self._nearest_point(pos)
            center = self.point_screen(nearest)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(QRectF(center.x() - 5, center.y() - 5, 10, 10))
        if self._moved and self.dragging_point is not None and self._drag_display is not None:
            key = self.dragging_point
            origin = self._drag_white if key == WHITE_POINT else self._drag_display[key]
            p.setPen(QPen(QColor(C.ORANGE), 1.2))
            p.drawLine(self._screen_position(origin, self.active_grid), self.point_screen(key))
        if len(self._lasso) > 1:
            p.setPen(QPen(QColor(C.ORANGE), 1, Qt.PenStyle.DashLine))
            p.setBrush(QColor(255, 136, 0, 25))
            p.drawPolygon(QPolygonF(self._lasso))
        if self._box_origin is not None and self._box_end is not None:
            p.setPen(QPen(QColor(C.ORANGE), 1, Qt.PenStyle.DashLine))
            p.setBrush(QColor(255, 136, 0, 25))
            p.drawRect(QRectF(self._box_origin, self._box_end).normalized())
        p.end()

    def _paint_mesh(self, p: QPainter, index: int) -> None:
        active = index == self.active_grid or self.mode == "hue_saturation"
        points = self.display_points(index)
        screen = [[self._screen_position(point, index) for point in row] for row in points]
        authored, _, pins = ((self.points, self.selected, self.pins) if active else
                             self._grid_states.get(index, (self._identity, set(), set())))
        edited = np.any(abs(authored - self._identity) > 1e-6, axis=2)
        if self.mode == "chroma_luma":
            rect = self.grid_rect(index)
            p.setPen(QPen(QColor(0, 0, 0, 60), 0.75))
            p.drawLine(rect.topLeft(), rect.bottomRight())
            p.drawLine(rect.topRight(), rect.bottomLeft())
        p.setPen(QPen(QColor(205, 205, 210, 125), 0.75))
        rows, cols = points.shape[:2]
        white = self._screen_position(self.white_point, index)
        for r in range(rows):
            for t in range(cols):
                current = screen[r][t]
                if r > 0:
                    p.drawLine(current, screen[r - 1][t])
                elif self.mode == "hue_saturation":
                    p.drawLine(current, white)
                if self.mode == "hue_saturation" or t + 1 < cols:
                    p.drawLine(current, screen[r][(t + 1) % cols])
        for key in self._keys():
            pos = screen[key[0]][key[1]]
            outer = (key[0] in {0, rows - 1} and key[1] in {0, cols - 1}
                     if self.mode == "chroma_luma" else key[0] == rows - 1)
            selected, pinned = active and key in self.selected, key in pins
            locked = outer or pinned or edited[key] or (active and key in self.luma_values)
            p.setPen(QPen(QColor("#111113"), 1.7 if locked else 0.3))
            p.setBrush(QColor(C.ORANGE if selected else "#e1e1e3"))
            radius = 3.5 if selected or (active and key == self.hover_point) else (3 if outer else 2)
            p.drawEllipse(pos, radius, radius)
        if self.mode == "hue_saturation":
            p.setPen(QPen(QColor("#171719"), 2))
            p.setBrush(QColor(C.ORANGE if WHITE_POINT in self.selected else "#cccccc"))
            p.drawEllipse(white, 3.5, 3.5)

    def _start_edit(self) -> None:
        if not self._gesture_active:
            self._gesture_active = True
            if self.calibrated_hs or self.persistent_mesh or self.mode == "chroma_luma":
                self._calibrated_base = (self.points.copy(), self.pins.copy(),
                                         self.luma_values.copy(), self.white_point.copy())
            self.interaction_started.emit()

    def _publish_mesh(self) -> None:
        self.ui_edited.emit()
        if self.calibrated_hs:
            self._pending_deltas = self.calibrated_offsets()
            if not self._changed_timer.isActive():
                self._changed_timer.start()
        elif self.mode == "chroma_luma":
            self._pending_deltas = self.points.copy()
            if not self._changed_timer.isActive():
                self._changed_timer.start()
        elif self.persistent_mesh:
            self._pending_deltas = self.calibrated_offsets()[..., :2]
            if not self._changed_timer.isActive():
                self._changed_timer.start()
        elif self.mode == "hue_saturation" and self.ring_count == self.grid_size:
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
            self._calibrated_base = None
            self.interaction_ended.emit()

    def begin_component_drag(self) -> None:
        self._component_gesture = True
        self._start_edit()

    def end_component_drag(self) -> None:
        self._component_gesture = False
        self._end_edit()

    def _toggle_pins(self, keys: set[PointKey]) -> None:
        display = self.display_points()
        for key in keys - {WHITE_POINT}:
            if key in self.pins:
                self.pins.remove(key)
            else:
                self.pins.add(key)
                self.points[key] = display[key]

    def _auto_pin(self) -> None:
        if not self.auto_lock:
            return
        targets = set()
        for key in self._keys():
            if key in self.selected:
                continue
            for r, t in self.selected:
                dr, dt = abs(key[0] - r), abs(key[1] - t)
                if self.mode == "hue_saturation":
                    dt = min(dt, self.grid_size - dt)
                distance = dt if self.lock_axis == "columns" else dr
                if self.mode == "hue_saturation":
                    distance = dr if dt == 0 else -1
                elif self.lock_axis == "single":
                    distance = max(dr, dt)
                if distance == self.lock_points:
                    targets.add(key)
        self._toggle_pins(targets - self.pins)

    def _arm_drag(self, key: PointKey, position: QPointF) -> None:
        self.dragging_point = key
        self._drag_origin = self._normalized(position)
        self._drag_points = self.points.copy()
        self._drag_display = self.display_points().copy()
        self._drag_white = self.white_point.copy()

    def _deform_at(self, center: np.ndarray) -> None:
        """Pull or push the local cell, or the interior points on the closest spoke."""
        original = self._drag_display
        if original is None:
            return
        rows, cols = original.shape[:2]
        target = self._drag_points.copy()
        scale = 1.2 if self.tool == "push" else 0.8
        if self.mode == "hue_saturation":
            angle = self._hue_from_pos(center)
            col = round(angle * cols) % cols
            axis = self._identity[-1, col]
            pivot = float(np.dot(center, axis))
            for row in range(rows - 1):
                key = (row, col)
                if key not in self.pins:
                    position = original[key]
                    radius = float(np.dot(position, axis))
                    target[key] = position + axis * (np.clip(pivot + (radius - pivot) * scale, 0.0, 1.0) - radius)
        else:
            x, y = np.clip((center + 1.0) * 0.5, 0.0, 1.0) * (cols - 1, rows - 1)
            row, col = min(int(y), rows - 2), min(int(x), cols - 2)
            for key in ((row, col), (row + 1, col), (row, col + 1), (row + 1, col + 1)):
                boundary = key[0] in (0, rows - 1) and key[1] in (0, cols - 1)
                if key not in self.pins and not boundary:
                    target[key] = np.clip(center + (original[key] - center) * scale, -1.0, 1.0)
        if not np.array_equal(self.points, target):
            self._start_edit()
            self._moved = True
            self.points = target
            self._publish_mesh()

    def _reset_anchor(self, key: PointKey) -> None:
        if key == WHITE_POINT:
            self.white_point.fill(0.0)
        else:
            self.points[key] = self._identity[key]
            self.pins.discard(key)
        self.luma_values.pop(key, None)

    def _limit_white(self) -> None:
        radius = np.linalg.norm(self.white_point)
        if radius > WHITE_POINT_MAX_RADIUS:
            self.white_point *= WHITE_POINT_MAX_RADIUS / radius

    def mousePressEvent(self, event: QMouseEvent) -> None:
        persistent = self.calibrated_hs or self.persistent_mesh or self.mode == "chroma_luma"
        if event.button() == Qt.MouseButton.RightButton and persistent:
            if self.mode == "chroma_luma":
                self.activate_grid(int(event.position().x() >= self.width() / 2))
            key = self._get_point_at(event.position())
            if key is not None:
                self._start_edit()
                self._reset_anchor(key)
                self.selected.discard(key)
                self._publish_mesh()
                self._end_edit()
                event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        self._moved = False
        self._deselect_on_release = None
        if self.mode == "chroma_luma":
            self.activate_grid(int(event.position().x() >= self.width() / 2))
        key = self._get_point_at(event.position())
        if key is not None and event.modifiers() & Qt.KeyboardModifier.AltModifier:
            self._set_isolation(key)
            event.accept()
            return
        additive = bool(event.modifiers() & (Qt.KeyboardModifier.ShiftModifier |
                                            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier))
        if self.tool in {"draw", "pin"}:
            self._pin_brush = self.tool == "pin"
            if not additive:
                if not self._pin_brush:
                    self.selected.clear()
            self._lasso = [event.position()]
            self.selection_changed.emit()
            return
        if self.tool in {"pull", "push"}:
            self.selected.clear()
            self._arm_drag(self._nearest_point(event.position()), event.position())
            self.dragging_point = None
            self._push_location = self._drag_origin
            self._deform_at(self._push_location)
            return
        if key is None:
            if not additive:
                self.selected.clear()
            if self.tool == "select":
                self._box_origin = self._box_end = event.position()
            self.selection_changed.emit(); self.update()
            return
        if persistent and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            if persistent:
                self._start_edit()
            self._toggle_pins({key})
            if persistent:
                self._publish_mesh()
                self._end_edit()
            else:
                self.ui_edited.emit(); self.update()
            return
        if additive:
            self.selected.symmetric_difference_update({key})
        elif key not in self.selected:
            self.selected = {key}
        elif not persistent:
            self._deselect_on_release = key
        if not (self.calibrated_hs or self.persistent_mesh) and self.mode == "hue_saturation":
            self._auto_pin()
        if key in self.selected and (persistent or key not in self.pins):
            self._arm_drag(key, event.position())
            self._moved = False
        self.selection_changed.emit(); self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._move_pointer(event.position())

    def _move_pointer(self, position: QPointF) -> None:
        if self._box_origin is not None:
            self._box_end = position
            self.update()
        elif self._lasso:
            self._lasso.append(position); self.update()
        elif self._push_location is not None:
            here = self._normalized(position)
            if np.linalg.norm(here - self._push_location) >= 0.004:
                self._push_location = here
                self._deform_at(here)
        elif self.dragging_point is not None and self._drag_points is not None:
            delta = self._normalized(position) - self._drag_origin
            if np.linalg.norm(delta) < 0.004 and not self._moved:
                return
            if (self.calibrated_hs or self.persistent_mesh or self.mode == "chroma_luma") and not self._moved:
                self._start_edit()
                self._auto_pin()
                self._drag_points = self.points.copy()
            self._moved = True
            self._start_edit()
            target = self._drag_points.copy()
            keys = self.selected if self._calibrated_base is not None else self.selected - self.pins
            for key in keys:
                if key == WHITE_POINT:
                    self.white_point = self._drag_white + delta
                    self._limit_white()
                else:
                    target[key] = self._drag_display[key] + delta
            if np.linalg.norm(delta) < 0.004 and self._calibrated_base is not None:
                points, pins, luma, white = self._calibrated_base
                target = points.copy()
                self.pins, self.luma_values, self.white_point = pins.copy(), luma.copy(), white.copy()
            if self.calibrated_hs:
                moved = np.any(target != self._drag_points, axis=2)
                radius = np.linalg.norm(target, axis=2)
                outside = moved & (radius > CALIBRATED_MAX_RADIUS)
                target[outside] *= CALIBRATED_MAX_RADIUS / radius[outside, None]
                self.points = target
            else:
                limit = 1.0 if self.mode == "chroma_luma" else 1.1
                self.points = np.clip(target, -limit, limit)
            self._publish_mesh()
        else:
            key = self._get_point_at(position)
            if self.hover_point != key:
                self.hover_point = key; self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self._set_isolation(None)
        if self._lasso or self._box_origin is not None or self.dragging_point is not None or self._push_location is not None:
            self._move_pointer(event.position())
        self._finish_pointer()

    def _finish_pointer(self) -> None:
        if self._box_origin is not None:
            rect = QRectF(self._box_origin, self._box_end).normalized()
            self.selected.update(key for key in self._selectable_keys() if rect.contains(self.point_screen(key)))
            self._box_origin = self._box_end = None
            self.selection_changed.emit()
        if self._lasso:
            path = QPainterPath(); path.addPolygon(QPolygonF(self._lasso)); path.closeSubpath()
            keys = {key for key in self._selectable_keys() if path.contains(self.point_screen(key))}
            keys.update(key for position in self._lasso if (key := self._get_point_at(position)) is not None)
            if self._pin_brush and keys - {WHITE_POINT}:
                self._start_edit()
                self._toggle_pins(keys)
                self._publish_mesh()
            elif not self._pin_brush:
                self.selected.update(keys)
            self._pin_brush = False
            self._lasso.clear(); self.selection_changed.emit(); self.ui_edited.emit()
        if self._deselect_on_release is not None and not self._moved:
            self.selected.discard(self._deselect_on_release)
            self.selection_changed.emit()
        self._deselect_on_release = None
        self.dragging_point = None
        self._push_location = None
        self._drag_origin = self._drag_points = self._drag_display = None
        base = self._calibrated_base
        if (base is not None and np.array_equal(self.points, base[0]) and self.pins == base[1]
                and self.luma_values == base[2] and np.array_equal(self.white_point, base[3])):
            self.cancel_gesture()
        else:
            self._end_edit()
        self.update()

    def begin_viewer_drag(self, point: np.ndarray, *, pin: bool = False) -> QPointF | None:
        rect = self.grid_rect(self.active_grid)
        position = QPointF(rect.left() + float(point[0]) * rect.width(),
                           rect.top() + float(point[1]) * rect.height())
        key = self._nearest_point(position)
        self.selected = {key}
        self.selection_changed.emit()
        self.update()
        if pin or self.tool == "pin":
            self._start_edit()
            self._toggle_pins({key})
            self._publish_mesh()
            self._end_edit()
            return None
        if key in self.pins and not (self.calibrated_hs or self.persistent_mesh or self.mode == "chroma_luma"):
            return None
        self._moved = False
        self._arm_drag(key, position)
        return position

    def move_viewer_drag(self, position: QPointF, *, finish: bool = False) -> None:
        self._move_pointer(position)
        if finish:
            self._finish_pointer()

    def focusOutEvent(self, event: QFocusEvent) -> None:
        if self.mode == "hue_saturation" and not (self.calibrated_hs or self.persistent_mesh):
            self._end_edit()
        self.cancel_gesture()
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            legacy = not (self.calibrated_hs or self.persistent_mesh) and self.mode == "hue_saturation"
            if legacy and self._drag_points is not None and self._moved:
                self.points = self._drag_points.copy(); self._publish_mesh()
            if legacy:
                self._end_edit()
            self.cancel_gesture()
            self.selected.clear()
            self.selection_changed.emit(); self.update()
        elif event.key() == Qt.Key.Key_A and event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            self.perform("all")
        elif event.key() in {Qt.Key.Key_Backspace, Qt.Key.Key_Delete}:
            self.perform("reset_points")
        else:
            super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Alt:
            self._set_isolation(None)
        super().keyReleaseEvent(event)

    def set_range(self, low: float, high: float) -> None:
        self._range = (low, high)
        if self.mode == "hue_saturation":
            if self.calibrated_hs or self.persistent_mesh:
                self.selected = {key for key in self._keys()
                                 if low <= ((key[1] / self.grid_size - 2.0 / 3.0) % 1.0) <= high}
            else:
                self.selected = {key for key in self._keys()
                                 if low <= self._hue_from_pos(self._initial_position(*key)) <= high}
        else:
            self.selected = {key for key in self._keys() if low <= key[1] / self.grid_size <= high}
        self.selection_changed.emit(); self.ui_edited.emit(); self.update()

    def perform(self, action: str) -> None:
        keys = set(self._selectable_keys())
        persistent = self.calibrated_hs or self.persistent_mesh or self.mode == "chroma_luma"
        previous_pins = self.pins.copy() if persistent else None
        previous_points = self.points.copy() if persistent else None
        if action == "invert":
            self.selected = keys - self.selected
        elif action == "convert_pin":
            self._toggle_pins(self.selected - self.pins)
            self.selected.clear()
        elif action in {"column", "ring", "row"}:
            seed = self.selected or ({self.hover_point} if self.hover_point is not None else set())
            axis = 1 if action == "column" else 0
            selected = {k[axis] for k in seed}
            result = {k for k in keys if k[axis] in selected}
            if self.tool == "pin":
                self._toggle_pins(result)
            else:
                self.selected.update(result)
        elif action == "all":
            if self.tool == "pin":
                pinnable = keys - {WHITE_POINT}
                self._toggle_pins(pinnable if self.pins == pinnable else pinnable - self.pins)
            else:
                self.selected = set() if self.selected == keys else keys
        elif action == "reset_selection":
            if persistent and self.selected:
                self._start_edit()
                for key in self.selected:
                    self._reset_anchor(key)
                self._publish_mesh()
                self._end_edit()
                return
            self.selected.clear(); self.pins.clear(); self.falloff = 0
        elif action.startswith("falloff_"):
            def neighbors(key: PointKey) -> set[PointKey]:
                if key == WHITE_POINT:
                    return {(0, col) for col in range(self.grid_size)}
                row, col = key
                adjacent = {(row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)}
                if self.mode == "hue_saturation":
                    adjacent = {(r, t % self.grid_size) for r, t in adjacent if r >= 0}
                    if row == 0:
                        adjacent.add(WHITE_POINT)
                return adjacent & keys

            if action == "falloff_add" and self.selected:
                self.selected |= set().union(*(neighbors(key) for key in self.selected))
            elif action == "falloff_sub":
                self.selected = {key for key in self.selected if neighbors(key) <= self.selected}
        elif action == "reset_points":
            self._start_edit()
            for key in self.selected:
                self._reset_anchor(key)
            self._publish_mesh(); self._end_edit()
        if previous_pins is not None and (
                previous_pins != self.pins or not np.array_equal(previous_points, self.points)):
            # Pins can change through a selection tool without moving any points.
            points, pins = self.points, self.pins
            self.points, self.pins = previous_points, previous_pins
            self._start_edit()
            self.points, self.pins = points, pins
            self._publish_mesh()
            self._end_edit()
        self.selection_changed.emit(); self.ui_edited.emit(); self.update()

    def edit_component(self, component: str, value: float | None = None, smooth: bool = False) -> None:
        persistent = self.calibrated_hs or self.persistent_mesh or self.mode == "chroma_luma"
        editable = self.selected if persistent else self.selected - self.pins
        if not editable:
            return
        if self.mode == "hue_saturation" and component == "luma":
            persistent = self.calibrated_hs or self.persistent_mesh
            if persistent:
                self._start_edit()
                self._auto_pin()
            for key in editable:
                if smooth:
                    self.luma_values[key] = (self.luma_values.get(key, 0.5) + 0.5) * 0.5
                elif value is None or value == 0.5:
                    self.luma_values.pop(key, None)
                else:
                    self.luma_values[key] = value
            if persistent:
                self._publish_mesh()
                if not self._component_gesture:
                    self._end_edit()
            else:
                self.ui_edited.emit()
                self.selection_changed.emit()
            return
        self._start_edit()
        if self.calibrated_hs:
            self._auto_pin()
        original = self.display_points()
        for key in editable:
            white = key == WHITE_POINT
            initial = np.zeros(2) if white else self._identity[key]
            point = self.white_point.copy() if white else original[key].copy()
            if self.mode == "chroma_luma":
                axis = 0 if component == "chroma" else 1
                if smooth:
                    point[axis] = (point[axis] + initial[axis]) * 0.5
                else:
                    point[axis] = initial[axis] if value is None else (2 * value if axis == 0 else 1 - 2 * value)
            elif component in {"hue", "sat"}:
                hue = self._hue_from_pos(point)
                radius = float(np.linalg.norm(point))
                if component == "hue":
                    baseline = self._hue_from_pos(initial)
                    if smooth:
                        hue += ((baseline - hue + 0.5) % 1.0 - 0.5) * 0.20
                    else:
                        hue = baseline if value is None else (-value if self.calibrated_hs else value)
                else:
                    baseline = float(np.linalg.norm(initial))
                    if smooth:
                        radius = (radius + baseline) * 0.5
                    else:
                        radius = baseline if value is None else value / CALIBRATED_SAT_SCALE if self.calibrated_hs else value
                angle = -hue * math.tau - math.pi / 2
                point[:] = (radius * math.cos(angle), radius * math.sin(angle))
            if white:
                self.white_point = point
                self._limit_white()
            else:
                self.points[key] = point
        self._publish_mesh()
        if not self._component_gesture:
            self._end_edit()

    def selected_values(self) -> dict[str, float | None]:
        if not self.selected:
            return {}
        visible = self.display_points()
        points = np.array([self.white_point if key == WHITE_POINT else visible[key] for key in sorted(self.selected)])
        if self.mode == "hue_saturation":
            values = {"hue": np.array([self._hue_from_pos(p) for p in points]), "sat": np.linalg.norm(points, axis=1),
                      "luma": np.array([self.luma_values.get(key, 0.5) for key in sorted(self.selected)])}
            if self.calibrated_hs:
                values["hue"] = (-values["hue"]) % 1.0
                values["hue"][np.isclose(values["hue"], 1.0, atol=1e-7, rtol=0.0)] = 0.0
                values["sat"] *= CALIBRATED_SAT_SCALE
        else:
            values = {"chroma": points[:, 0] * 0.5, "luma": (1 - points[:, 1]) / 2}
        return {key: float(v[0]) if np.ptp(v) < 1e-5 else None for key, v in values.items()}
