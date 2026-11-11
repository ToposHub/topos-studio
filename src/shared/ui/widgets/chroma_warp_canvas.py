"""Chroma Warp stroke/pin editor. All edits are presentation state only."""
from __future__ import annotations

from typing import Any

import numpy as np
from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PyQt6.QtWidgets import QWidget

from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.warper_canvas import WarperCanvas


class ChromaWarpCanvas(QWidget):
    ui_edited = pyqtSignal()
    selection_changed = pyqtSignal()

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
        self._anchor: list[float] | None = None
        self._dragging = False
        self._drag_backup: list[float] | None = None
        self._stroke_backup: list[list[float]] | None = None
        self._drag_handle = "end"
        self._press_point: list[float] | None = None
        self._background: QImage | None = None
        self.density_image: QImage | None = None
        self.show_grid = True
        self.show_density = True
        self.parameters = {"chroma_range": 0.04, "tonal_low": 1.0, "tonal_high": 1.0,
                           "tonal_pivot": 0.5, "exposure": 0.0, "stroke_mode": "normal"}

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

    def snapshot(self) -> dict[str, Any]:
        return {"mode": "chroma_warp", "tool": self.tool,
                "strokes": [{"path": [list(p) for p in s["path"]], "mode": s["mode"],
                             "parameters": dict(s["parameters"])} for s in self.strokes],
                "pins": [{"position": list(p["position"]), "parameters": dict(p["parameters"])} for p in self.pins],
                "selected": self.selected,
                "parameters": dict(self.parameters)}

    def cancel_gesture(self) -> None:
        if self._drag_backup is not None and self.selected is not None:
            kind, index = self.selected
            if kind == "pin":
                self.pins[index]["position"] = self._drag_backup
            elif self._stroke_backup is not None:
                self.strokes[index]["path"] = self._stroke_backup
            self.ui_edited.emit()
        self._drawing.clear(); self._anchor = None; self._dragging = False
        self._drag_backup = None
        self._stroke_backup = None
        self.update()

    def reset(self) -> None:
        self.cancel_gesture(); self.strokes.clear(); self.pins.clear(); self.selected = None
        self.selection_changed.emit(); self.ui_edited.emit(); self.update()

    def delete_selection(self) -> None:
        if self.selected is None:
            return
        self.cancel_gesture()
        kind, index = self.selected
        (self.strokes if kind == "stroke" else self.pins).pop(index)
        self.selected = None
        self.selection_changed.emit(); self.ui_edited.emit(); self.update()

    def set_parameter(self, key: str, value: float | str) -> None:
        if self.selected is not None:
            kind, index = self.selected
            item = self.strokes[index] if kind == "stroke" else self.pins[index]
            item["parameters"][key] = value
            if key == "stroke_mode" and kind == "stroke":
                item["mode"] = "point_stroke" if value == "point_to_point" else "stroke"
        else:
            self.parameters[key] = value
        self.ui_edited.emit()

    def set_density_image(self, image: QImage | None) -> None:
        self.density_image = image.copy() if image is not None else None
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

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(C.BG_PANEL))
        rect = self.plot_rect()
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
        p.save(); p.setClipRect(rect)
        if self.show_grid:
            p.setPen(QPen(QColor(0, 0, 0, 60), 0.6))
            for n in range(11):
                p.drawLine(self._screen([n / 10, 0]), self._screen([n / 10, 1]))
                p.drawLine(self._screen([0, n / 10]), self._screen([1, n / 10]))
            p.drawLine(rect.topLeft(), rect.bottomRight()); p.drawLine(rect.topRight(), rect.bottomLeft())
        if self.show_density and self.density_image is not None:
            p.drawImage(rect, self.density_image)
        p.restore()
        for i, stroke in enumerate(self.strokes):
            self._paint_stroke(p, stroke["path"], self.selected == ("stroke", i))
        if self._drawing:
            self._paint_stroke(p, self._drawing, True)
        if self._anchor is not None:
            p.setPen(QColor(C.ORANGE)); p.drawEllipse(self._screen(self._anchor), 4, 4)
        for i, pin in enumerate(self.pins):
            p.setPen(QPen(QColor(C.ORANGE if self.selected == ("pin", i) else "#e0e0e0"), 1))
            pos = self._screen(pin["position"])
            p.drawEllipse(pos, 4, 4); p.drawLine(pos - QPointF(6, 0), pos + QPointF(6, 0))
            p.drawLine(pos - QPointF(0, 6), pos + QPointF(0, 6))
        p.end()

    def _paint_stroke(self, p: QPainter, points: list[list[float]], selected: bool) -> None:
        if not points:
            return
        p.setPen(QPen(QColor(C.ORANGE if selected else "#dadadd"), 1.4))
        path = QPainterPath(self._screen(points[0]))
        for point in points[1:]:
            path.lineTo(self._screen(point))
        p.drawPath(path)
        p.setBrush(QColor(C.BG_PANEL))
        p.drawEllipse(self._screen(points[0]), 3.5, 3.5)
        p.setBrush(QColor(C.ORANGE if selected else "#dadadd"))
        p.drawEllipse(self._screen(points[-1]), 3.5, 3.5)
        if len(points) > 1:
            end, prev = self._screen(points[-1]), self._screen(points[-2])
            delta = end - prev
            length = max(1.0, (delta.x() ** 2 + delta.y() ** 2) ** 0.5)
            unit = delta / length
            normal = QPointF(-unit.y(), unit.x())
            p.drawLine(end, end - unit * 7 + normal * 3)
            p.drawLine(end, end - unit * 7 - normal * 3)

    def _finish_stroke(self, points: list[list[float]]) -> None:
        if len(points) < 2:
            return
        parameters = dict(self.parameters)
        parameters["stroke_mode"] = "point_to_point" if self.tool == "point_stroke" else "normal"
        self.strokes.append({"path": [list(p) for p in points], "mode": self.tool, "parameters": parameters})
        self.selected = ("stroke", len(self.strokes) - 1)
        self.selection_changed.emit(); self.ui_edited.emit(); self.update()

    def _hit(self, point: QPointF) -> tuple[tuple[str, int], str] | None:
        for i in reversed(range(len(self.pins))):
            if (self._screen(self.pins[i]["position"]) - point).manhattanLength() < 12:
                return ("pin", i), "end"
        for i in reversed(range(len(self.strokes))):
            points = [self._screen(p) for p in self.strokes[i]["path"]]
            for handle, endpoint in (("end", points[-1]), ("start", points[0])):
                if (endpoint - point).manhattanLength() < 12:
                    return ("stroke", i), handle
            for a, b in zip(points, points[1:]):
                ab, ap = b - a, point - a
                denominator = ab.x() ** 2 + ab.y() ** 2
                fraction = max(0, min(1, (ap.x() * ab.x() + ap.y() * ab.y()) / max(1e-9, denominator)))
                if ((a + ab * fraction) - point).manhattanLength() < 8:
                    return ("stroke", i), "body"
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus(); point = self._normalized(event.position())
        if not self.plot_rect().contains(event.position()):
            return
        if self.tool == "stroke":
            self._drawing = [point]
        elif self.tool == "point_stroke":
            if self._anchor is None:
                self._anchor = point
            else:
                self._finish_stroke([self._anchor, point]); self._anchor = None
        elif self.tool == "add_pin":
            self.pins.append({"position": point, "parameters": dict(self.parameters)})
            self.selected = ("pin", len(self.pins) - 1)
            self.selection_changed.emit(); self.ui_edited.emit()
        else:
            hit = self._hit(event.position())
            self.selected = hit[0] if hit is not None else None
            self._dragging = self.selected is not None
            if self.selected is not None:
                kind, index = self.selected
                self._drag_handle = hit[1]
                self._press_point = point
                self._drag_backup = list(self.pins[index]["position"] if kind == "pin" else self.strokes[index]["path"][-1])
                if kind == "stroke":
                    self._stroke_backup = [list(p) for p in self.strokes[index]["path"]]
            self.selection_changed.emit()
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        point = self._normalized(event.position())
        if self._drawing:
            self._drawing.append(point)
        elif self._dragging and self.selected is not None:
            kind, index = self.selected
            if kind == "pin":
                self.pins[index]["position"] = point
            elif self._drag_handle == "body" and self._stroke_backup is not None:
                delta = np.array(point) - self._press_point
                self.strokes[index]["path"] = np.clip(np.array(self._stroke_backup) + delta, 0, 1).tolist()
            else:
                self.strokes[index]["path"][0 if self._drag_handle == "start" else -1] = point
            self.ui_edited.emit()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drawing:
            self._finish_stroke(self._drawing)
            self._drawing.clear()
        self._dragging = False
        self._drag_backup = None
        self._stroke_backup = None

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in {Qt.Key.Key_Delete, Qt.Key.Key_Backspace}:
            self.delete_selection()
        elif event.key() == Qt.Key.Key_Escape:
            self.cancel_gesture()
        else:
            super().keyPressEvent(event)
