"""Chroma Warp pointer gestures and stroke/pin display."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFocusEvent, QImage, QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPainterPathStroker,
    QPaintEvent, QPen,
)
from PyQt6.QtWidgets import QWidget

from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.warper_canvas import WarperCanvas


# The stroke radii for ranges 0.020, 0.040 and 0.080 measure 22, 41 and
# 78 pixels on a 389-pixel Resolve plot. Pin radii measure range × width.
_STROKE_RANGE_BASE = 0.010
_STROKE_RANGE_SCALE = 2.36


class ChromaWarpCanvas(QWidget):
    ui_edited = pyqtSignal()
    selection_changed = pyqtSignal()
    edit_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(180, 160)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.tool = "stroke"
        self.strokes: list[dict[str, Any]] = []
        self.pins: list[dict[str, Any]] = []
        self.selected: tuple[str, int] | None = None
        self._drawing: list[list[float]] = []
        self._drawing_moved = False
        self._drawing_id: str | None = None
        self._dragging = False
        self._drag_backup: list[float] | None = None
        self._stroke_backup: list[list[float]] | None = None
        self._drag_handle = "end"
        self._press_point: list[float] | None = None
        self._background: QImage | None = None
        self.density_image: QImage | None = None
        self.viewer_sample: np.ndarray | None = None
        self.white_point = [0.426, 0.534]
        self.show_grid = True
        self.show_density = True
        self.parameters = {"chroma_range": 0.04, "tonal_low": 1.0, "tonal_high": 1.0,
                           "tonal_pivot": 0.5, "exposure": 0.0, "stroke_mode": "normal"}
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(16)
        self._preview_timer.timeout.connect(self._publish_preview)
        self._preview_open = False

    def plot_rect(self) -> QRectF:
        side = min(self.width(), self.height())
        return QRectF((self.width() - side) / 2, (self.height() - side) / 2, side, side)

    def _screen(self, point: list[float]) -> QPointF:
        rect = self.plot_rect()
        return QPointF(rect.left() + point[0] * rect.width(), rect.top() + point[1] * rect.height())

    def _normalized(self, point: QPointF) -> list[float]:
        rect = self.plot_rect()
        return [max(0, min(1, (point.x() - rect.left()) / rect.width())),
                max(0, min(1, (point.y() - rect.top()) / rect.height()))]

    def snapshot(self, *, preview: bool = False) -> dict[str, Any]:
        strokes = self.strokes
        if preview and self._has_stroke_draft():
            strokes = [*strokes, self._stroke_from_points(self._drawing)]
        return {"mode": "chroma_warp", "tool": self.tool,
                "strokes": [{"id": s["id"], "path": [list(p) for p in s["path"]], "mode": s["mode"],
                             "parameters": dict(s["parameters"])} for s in strokes],
                "pins": [{"id": p["id"], "position": list(p["position"]),
                          "parameters": dict(p["parameters"])} for p in self.pins],
                "selected": self.selected,
                "parameters": dict(self.parameters)}

    def cancel_gesture(self) -> None:
        self._preview_timer.stop()
        if self._has_drag_change() and self.selected is not None:
            kind, index = self.selected
            if kind == "pin" and self._drag_backup is not None:
                self.pins[index]["position"] = self._drag_backup
            elif kind == "stroke" and self._stroke_backup is not None:
                self.strokes[index]["path"] = self._stroke_backup
            self.ui_edited.emit()
        self._drawing.clear()
        self._drawing_moved = False
        self._drawing_id = None
        self._clear_drag()
        self._close_preview("cancel")
        self.update()

    def reset(self) -> None:
        self.cancel_gesture(); self.strokes.clear(); self.pins.clear(); self.selected = None
        self.parameters = {"chroma_range": 0.04, "tonal_low": 1.0, "tonal_high": 1.0,
                           "tonal_pivot": 0.5, "exposure": 0.0, "stroke_mode": "normal"}
        self.selection_changed.emit(); self.ui_edited.emit(); self.update()

    def delete_selection(self) -> None:
        if self.selected is None:
            return
        self.cancel_gesture()
        kind, index = self.selected
        (self.strokes if kind == "stroke" else self.pins).pop(index)
        self.selected = None
        self.selection_changed.emit(); self.ui_edited.emit(); self.update()
        self.edit_requested.emit("atomic")

    def set_parameter(self, key: str, value: float | str) -> None:
        if key.startswith("tonal_"):
            self.parameters[key] = value
            for item in (*self.strokes, *self.pins):
                item["parameters"][key] = value
        if self.selected is not None:
            kind, index = self.selected
            item = self.strokes[index] if kind == "stroke" else self.pins[index]
            item["parameters"][key] = value
            if key == "stroke_mode" and kind == "stroke":
                item["mode"] = "point_stroke" if value == "point_to_point" else "stroke"
        else:
            self.parameters[key] = value
        self.ui_edited.emit()
        self.update()

    def set_density_image(self, image: QImage | None) -> None:
        self.density_image = image.copy() if image is not None else None
        self.update()

    def set_viewer_sample(self, point: np.ndarray | None) -> None:
        self.viewer_sample = point
        self.update()

    def _field(self) -> QImage:
        if self._background is None:
            yy, xx = np.mgrid[0:512, 0:512] / 511
            # Display-only chromaticity backdrop: its neutral origin is offset
            # from the square's geometric center. Keep the full field so the
            # low-intensity area beyond the gamut outline remains visible.
            dx, dy = (xx - 0.426) * 1.12, yy - 0.534
            hue = np.mod((-np.arctan2(dy, dx) - 1.983) / (2 * np.pi), 1)
            sat = np.minimum(1.0, np.hypot(dx, dy) * 2.40) ** 0.58
            rgb = np.ascontiguousarray(WarperCanvas._rgb(hue, sat, np.full_like(xx, 0.5)))
            self._background = QImage(rgb.data, 512, 512, 512 * 3, QImage.Format.Format_RGB888).copy()
        return self._background

    def _paint_field(self, p: QPainter, rect: QRectF) -> None:
        path = QPainterPath(self._screen([0.10, 0.95]))
        path.cubicTo(self._screen([0.04, 0.65]), self._screen([0.05, 0.18]), self._screen([0.25, 0.04]))
        path.cubicTo(self._screen([0.53, 0.12]), self._screen([0.88, 0.37]), self._screen([0.95, 0.55]))
        path.cubicTo(self._screen([0.90, 0.76]), self._screen([0.30, 0.91]), self._screen([0.10, 0.95]))
        # Draw the square first, then attenuate only the region outside the
        # gamut. Clipping the whole field used to remove the reference's
        # defining square gradient and its faint background grid.
        p.drawImage(rect, self._field())
        outside = QPainterPath()
        outside.addRect(rect)
        outside = outside.subtracted(path)
        p.fillPath(outside, QColor(0, 0, 0, 170))

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(C.BG_PANEL))
        rect = self.plot_rect()
        self._paint_field(p, rect)
        p.save(); p.setClipRect(rect)
        if self.show_grid:
            p.setPen(QPen(QColor(0, 0, 0, 60), 0.6))
            for n in range(11):
                p.drawLine(self._screen([n / 10, 0]), self._screen([n / 10, 1]))
                p.drawLine(self._screen([0, n / 10]), self._screen([1, n / 10]))
            p.drawLine(rect.topLeft(), rect.bottomRight()); p.drawLine(rect.topRight(), rect.bottomLeft())
        if self.show_density and self.density_image is not None:
            p.drawImage(rect, self.density_image)
        self._paint_neutral(p)
        for i, stroke in enumerate(self.strokes):
            selected = self.selected == ("stroke", i)
            points = stroke["path"]
            if selected and not self._drawing:
                end = points[-1] if stroke["mode"] == "stroke" else None
                self._paint_range(p, points[0], float(stroke["parameters"]["chroma_range"]), end=end)
            self._paint_stroke(p, points, selected)
        if self._drawing:
            end = self._drawing[-1] if self.tool != "point_stroke" else None
            self._paint_range(p, self._drawing[0], float(self.parameters["chroma_range"]), end=end)
            self._paint_stroke(p, self._drawing, True)
        for i, pin in enumerate(self.pins):
            selected = self.selected == ("pin", i)
            if selected:
                self._paint_range(p, pin["position"], float(pin["parameters"]["chroma_range"]), pin=True)
            p.setPen(QPen(QColor(C.ORANGE if selected else "#e0e0e0"), 1))
            p.setBrush(QColor(C.BG_PANEL))
            p.drawEllipse(self._screen(pin["position"]), 3.5, 3.5)
        if self.viewer_sample is not None:
            self._paint_viewer_sample(p, self.viewer_sample)
        p.restore()
        p.end()

    def _paint_viewer_sample(self, painter: QPainter, point: np.ndarray) -> None:
        sample = self._screen(point.tolist())
        painter.setPen(QPen(QColor("#fff2ae"), 1))
        painter.drawLine(sample - QPointF(4, 0), sample + QPointF(4, 0))
        painter.drawLine(sample - QPointF(0, 4), sample + QPointF(0, 4))

    def _paint_neutral(self, painter: QPainter) -> None:
        painter.setPen(QPen(QColor(235, 235, 240, 105), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(self._screen(self.white_point), 6, 6)

    def _paint_range(
        self, painter: QPainter, center: list[float], chroma_range: float,
        *, end: list[float] | None = None, pin: bool = False,
    ) -> None:
        if chroma_range <= 0.0:
            return
        radius = self._range_radius(chroma_range, pin=pin)
        color = QColor(C.ORANGE)
        color.setAlpha(170)
        painter.setPen(QPen(color, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if end is None or end == center:
            painter.drawEllipse(self._screen(center), radius, radius)
        else:
            direction = QPainterPath(self._screen(center))
            direction.lineTo(self._screen(end))
            outline = QPainterPathStroker()
            outline.setWidth(2.0 * radius)
            outline.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.drawPath(outline.createStroke(direction))

    def _range_radius(self, chroma_range: float, *, pin: bool = False) -> float:
        if chroma_range <= 0.0:
            return 0.0
        return self.plot_rect().width() * (
            chroma_range if pin else _STROKE_RANGE_BASE + _STROKE_RANGE_SCALE * chroma_range)

    def _inside_range(self, distance: QPointF, chroma_range: float) -> bool:
        radius = self._range_radius(chroma_range)
        return distance.x() ** 2 + distance.y() ** 2 < radius ** 2

    def _paint_stroke(self, p: QPainter, points: list[list[float]], selected: bool) -> None:
        start, end = self._screen(points[0]), self._screen(points[-1])
        color = QColor(C.ORANGE if selected else "#dadadd")
        delta = end - start
        length = (delta.x() ** 2 + delta.y() ** 2) ** 0.5
        if length > 0.0:
            p.setPen(QPen(color, 1.4))
            p.drawLine(start, end)
            unit = delta / length
            normal = QPointF(-unit.y(), unit.x())
            tip = end - unit * min(length * 0.5, 4.0)
            p.drawLine(tip, tip - unit * 6 + normal * 3)
            p.drawLine(tip, tip - unit * 6 - normal * 3)
            p.setBrush(QColor(C.BG_PANEL))
            p.drawEllipse(end, 3.5, 3.5)
        p.setPen(QPen(QColor("#e3ebfa"), 1.0))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(start, 3.5, 3.5)

    def _has_stroke_draft(self) -> bool:
        return len(self._drawing) > 1 and self._drawing[0] != self._drawing[-1]

    def _stroke_from_points(self, points: list[list[float]]) -> dict[str, Any]:
        parameters = dict(self.parameters)
        parameters["stroke_mode"] = "point_to_point" if self.tool == "point_stroke" else "normal"
        return {"id": self._drawing_id or uuid4().hex, "path": [list(points[0]), list(points[-1])],
                "mode": self.tool, "parameters": parameters}

    def _finish_stroke(self, points: list[list[float]]) -> None:
        self.strokes.append(self._stroke_from_points(points))
        self.selected = ("stroke", len(self.strokes) - 1)
        self.selection_changed.emit(); self.ui_edited.emit(); self.update()

    def _publish_preview(self) -> None:
        self._preview_open = True
        self.edit_requested.emit("update")

    def _close_preview(self, phase: str) -> None:
        if self._preview_open:
            self._preview_open = False
            self.edit_requested.emit(phase)

    def _clear_drag(self) -> None:
        self._dragging = False
        self._press_point = None
        self._drag_backup = None
        self._stroke_backup = None

    def _has_drag_change(self) -> bool:
        if not self._dragging or self.selected is None:
            return False
        kind, index = self.selected
        if kind == "pin":
            return self.pins[index]["position"] != self._drag_backup
        return self.strokes[index]["path"] != self._stroke_backup

    def _hit(self, point: QPointF) -> tuple[tuple[str, int], str] | None:
        for i in reversed(range(len(self.pins))):
            if (self._screen(self.pins[i]["position"]) - point).manhattanLength() < 12:
                return ("pin", i), "end"
        influence = None
        for i in reversed(range(len(self.strokes))):
            points = [self._screen(p) for p in self.strokes[i]["path"]]
            stroke = self.strokes[i]
            if points[0] == points[-1]:
                if (points[0] - point).manhattanLength() < 12:
                    return ("stroke", i), "body"
            else:
                for handle, endpoint in (("end", points[-1]), ("start", points[0])):
                    if (endpoint - point).manhattanLength() < 12:
                        return ("stroke", i), handle
            start, end = points[0], points[-1]
            line, offset = end - start, point - start
            length2 = line.x() ** 2 + line.y() ** 2
            fraction = np.clip((offset.x() * line.x() + offset.y() * line.y()) / max(length2, 1e-9), 0.0, 1.0)
            distance = start + line * float(fraction) - point
            if length2 and distance.manhattanLength() < 8:
                return ("stroke", i), "body"
            if (influence is None and stroke["mode"] == "stroke"
                    and self._inside_range(distance, float(stroke["parameters"]["chroma_range"]))):
                influence = ("stroke", i), "influence"
        return influence

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus(); point = self._normalized(event.position())
        if not self.plot_rect().contains(event.position()):
            return
        if self.tool != "select":
            self._begin_plot_edit(point)
        else:
            self._begin_selection(event.position(), point)
        self.update()

    def _begin_selection(self, position: QPointF, point: list[float]) -> None:
        hit = self._hit(position)
        self.selected = hit[0] if hit is not None else None
        self._dragging = hit is not None
        if hit is not None:
            (kind, index), self._drag_handle = hit
            self._press_point = point
            if kind == "pin":
                self._drag_backup = list(self.pins[index]["position"])
            else:
                self._stroke_backup = [list(p) for p in self.strokes[index]["path"]]
        self.selection_changed.emit()

    def _begin_plot_edit(self, point: list[float]) -> None:
        if self.tool in {"stroke", "point_stroke"}:
            self._drawing = [point, point]
            self._drawing_moved = False
            self._drawing_id = uuid4().hex
        elif self.tool == "add_pin":
            self.pins.append({"id": uuid4().hex, "position": point, "parameters": dict(self.parameters)})
            self.selected = ("pin", len(self.pins) - 1)
            self.selection_changed.emit(); self.ui_edited.emit()
            self.edit_requested.emit("atomic")
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._move_plot_edit(self._normalized(event.position()))

    def _move_plot_edit(self, point: list[float]) -> None:
        if self._drawing:
            if point == self._drawing[-1]:
                return
            self._drawing_moved = True
            self._drawing[-1] = point
        elif self._dragging and self.selected is not None:
            kind, index = self.selected
            delta = np.asarray(point) - self._press_point
            if kind == "pin":
                target = np.clip(np.asarray(self._drag_backup) + delta, 0, 1).tolist()
                if self.pins[index]["position"] == target:
                    return
                self.pins[index]["position"] = target
            elif self._drag_handle == "body" and self._stroke_backup is not None:
                original = np.asarray(self._stroke_backup)
                target = (original + np.clip(delta, -original.min(axis=0), 1 - original.max(axis=0))).tolist()
                if self.strokes[index]["path"] == target:
                    return
                self.strokes[index]["path"] = target
            else:
                endpoint = 0 if self._drag_handle == "start" else -1
                if self._drag_handle == "influence" and point != self._press_point:
                    target = point
                else:
                    target = np.clip(np.asarray(self._stroke_backup[endpoint]) + delta, 0, 1).tolist()
                if self.strokes[index]["path"][endpoint] == target:
                    return
                self.strokes[index]["path"][endpoint] = target
            self.ui_edited.emit()
        else:
            return
        if not (self._has_stroke_draft() or self._has_drag_change() or self._preview_open):
            self._preview_timer.stop()
        elif not self._preview_timer.isActive():
            self._preview_timer.start()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._drawing or self._dragging:
            self._move_plot_edit(self._normalized(event.position()))
        self._finish_plot_edit()

    def _finish_plot_edit(self) -> None:
        pending = self._preview_timer.isActive()
        self._preview_timer.stop()
        changed = self._has_stroke_draft() or self._has_drag_change()
        added_point = bool(self._drawing) and not self._drawing_moved
        if self._drawing:
            if changed or added_point:
                self._finish_stroke(self._drawing)
            self._drawing.clear()
            self._drawing_moved = False
            self._drawing_id = None
        self._clear_drag()
        if added_point:
            self.edit_requested.emit("atomic")
        elif changed:
            if pending or not self._preview_open:
                self._publish_preview()
            self._close_preview("commit")
        else:
            self._close_preview("cancel")
        self.update()

    def begin_viewer_drag(self, point: np.ndarray, *, pin: bool = False) -> QPointF | None:
        normalized = np.clip(point, 0.0, 1.0).tolist()
        if not pin and self.tool == "select":
            self._begin_selection(self._screen(normalized), normalized)
            self.update()
            return self._screen(normalized) if self._dragging else None
        tool = self.tool
        if pin:
            self.tool = "add_pin"
        try:
            self._begin_plot_edit(normalized)
        finally:
            self.tool = tool
        return self._screen(normalized) if self._drawing else None

    def move_viewer_drag(self, position: QPointF, *, finish: bool = False) -> None:
        self._move_plot_edit(self._normalized(position))
        if finish:
            self._finish_plot_edit()

    def viewer_delta(self, delta: QPointF) -> QPointF:
        return delta

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self.cancel_gesture()
        super().focusOutEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in {Qt.Key.Key_Delete, Qt.Key.Key_Backspace}:
            self.delete_selection()
        elif event.key() == Qt.Key.Key_Escape:
            self.cancel_gesture()
        else:
            super().keyPressEvent(event)
