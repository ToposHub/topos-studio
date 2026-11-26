"""Interactive scopes for the Chromogen look and stage under the cursor."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np
from PyQt6.QtCore import QEvent, QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen, QWheelEvent
from PyQt6.QtWidgets import QWidget

from src.features.color.core.chromogen import (
    CHROMOGEN_VERSION, apply_chromogen_linear, chromogen_coordinates, chromogen_hue,
    chromogen_rgb_from_coordinates, chromogen_rgb_from_lms,
)
from src.shared.color.color_transform_provider import video_signal_gamut_matrix
from src.shared.color.types import ColorPrimaries
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C


_AXES = QColor("#565a64")
_GUIDE = QColor("#343842")
_TEXT = QColor("#aeb0b6")
_HOT = QColor("#f0b44c")
_RESPONSE_COLORS = (QColor("#e5a25c"), QColor("#83bf9e"), QColor("#8caef0"))
_RGB_BASIS = {
    "rec709": np.eye(3, dtype=np.float32),
    "rec2020": video_signal_gamut_matrix(ColorPrimaries.BT2020, ColorPrimaries.BT709).astype(np.float32),
}


def _rgb_cube() -> np.ndarray:
    axis = np.array((0.06, 0.21, 0.39, 0.60, 0.82, 1.0), dtype=np.float32)
    return np.stack(np.meshgrid(axis, axis, axis), axis=-1).reshape(-1, 3)


def _opponent_rays() -> np.ndarray:
    e, radius, angle = np.meshgrid(
        np.array((-1.75, 0.0, 1.85)),
        np.linspace(0.14, 1.08, 7),
        np.arange(24) * math.tau / 24,
        indexing="ij",
    )
    eab = np.stack((e, radius * np.cos(angle), radius * np.sin(angle)), axis=-1)
    return chromogen_rgb_from_coordinates(eab.reshape(-1, 3).astype(np.float32))


def _hue_ramp() -> np.ndarray:
    hues = np.arange(73)
    result = np.empty((len(hues), 3), dtype=np.float32)
    for index, hue in enumerate(hues):
        color = QColor.fromHsvF((hue % 72) / 72, 0.76, 0.65)
        result[index] = color.getRgbF()[:3]
    return result


def _display_color(rgb: np.ndarray, alpha: int) -> QColor:
    color = np.clip(rgb, 0.0, 1.0) ** (1.0 / 2.2)
    return QColor.fromRgbF(*color.tolist(), alpha / 255)


_CUBE_SAMPLES = _rgb_cube()
_GAMUT_SAMPLES = {
    "eab": _opponent_rays(), "lms": chromogen_rgb_from_lms(_CUBE_SAMPLES),
    **{name: _CUBE_SAMPLES @ matrix.T for name, matrix in _RGB_BASIS.items()},
}
_HUE_SAMPLES = _hue_ramp()
_CUBE_CORNERS = np.array([(i & 1, (i >> 1) & 1, (i >> 2) & 1) for i in range(8)], dtype=np.float32)
_CUBE_EDGES = tuple((i, i ^ m) for i in range(8) for m in (1, 2, 4) if not i & m)
_AXIS_LABELS = {"hue_axis": "Input hue −180°…+180°", "ev_axis": "Input −5 to +5 EV",
                "chroma_axis": "Neutral → saturated"}


class ChromogenPlot(QWidget):
    """Sample a deterministic color chart through the same operator as rendering."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state: dict[str, Any] | None = None
        self._stage_id: str | None = None
        self._projection = "radial"
        self._gamut = "eab"
        self._picked_color: tuple[float, float, float] | None = None
        self._compare = True
        self._zoom = 1.0
        self._pan = QPointF()
        self._yaw = -0.54
        self._pitch = 0.34
        self._drag: str | None = None
        self._last_point = QPointF()
        self._before = _GAMUT_SAMPLES[self._gamut]
        self._after = self._before
        self._picked_after: np.ndarray | None = None
        self._response: tuple[str, str, np.ndarray, np.ndarray] | None = None
        self.setMinimumSize(108, 90)
        self.setAccessibleName(I18N.tr("chromogen.scope", "Look Dev Scope"))
        self._update_tooltip()

    def sizeHint(self) -> QSize:
        return QSize(325, 245)

    def set_state(self, state: dict[str, Any] | None, stage_id: str | None) -> None:
        if self._state == state and self._stage_id == stage_id:
            return
        previous = self._state or {}
        next_state = state or {}
        recompute_response = self._stage_id != stage_id or previous.get("stages") != next_state.get("stages")
        self._state, self._stage_id = state, stage_id
        self._resample(recompute_response=recompute_response)

    def set_projection(self, projection: str) -> None:
        if self._projection != projection:
            self._projection = projection
            self._update_tooltip()
            self.reset_view()

    def set_gamut(self, gamut: str) -> None:
        if gamut in _GAMUT_SAMPLES and self._gamut != gamut:
            self._gamut = gamut
            self._resample()

    def set_compare(self, compare: bool) -> None:
        self._compare = compare
        self.update()

    def set_picked_color(self, rgb: tuple[float, float, float] | None) -> None:
        self._picked_color = rgb
        self._resample_picked_color()
        self.update()

    def reset_view(self) -> None:
        self._zoom = 1.0
        self._pan = QPointF()
        self._yaw = -0.54
        self._pitch = 0.34
        self.update()

    def _update_tooltip(self) -> None:
        if self._projection == "response":
            tip = I18N.tr("chromogen.response_tooltip", "The response of the selected stage.")
        elif self._projection == "radial":
            tip = I18N.tr("chromogen.radial_tooltip", "Drag to pan. Scroll to zoom. Double-click to reset the view.")
        else:
            tip = I18N.tr("chromogen.cube_tooltip", "Drag to rotate. Shift-drag to pan. Scroll to zoom.")
        self.setToolTip(tip)

    def _resample(self, *, recompute_response: bool = True) -> None:
        self._before = _GAMUT_SAMPLES[self._gamut]
        self._after = apply_chromogen_linear(self._before, self._state)
        if recompute_response:
            self._response = self._stage_response()
        self._resample_picked_color()
        self.update()

    def _resample_picked_color(self) -> None:
        self._picked_after = None
        if self._picked_color is not None:
            pixel = np.array([self._picked_color], dtype=np.float32)
            self._picked_after = apply_chromogen_linear(pixel, self._state)[0]

    def _apply_prefix(self, samples: np.ndarray, stages: list[Mapping[str, Any]]) -> np.ndarray:
        if not stages:
            return samples
        return apply_chromogen_linear(
            samples, {"version": CHROMOGEN_VERSION, "enabled": True, "amount": 1.0, "stages": stages},
        )

    def _stage_response(self) -> tuple[str, str, np.ndarray, np.ndarray] | None:
        if self._state is None or self._stage_id is None:
            return None
        stages = self._state["stages"]
        index = next((i for i, stage in enumerate(stages) if stage["id"] == self._stage_id), -1)
        if index < 0:
            return None
        kind = stages[index]["kind"]
        axis = np.linspace(0.0, 1.0, 73, dtype=np.float32)
        if kind.startswith("sector_"):
            x_axis = "hue_axis"
            samples = _HUE_SAMPLES[:, None, :] * np.array([0.43, 0.95, 1.45], dtype=np.float32)[None, :, None]
        elif kind in ("contrast_boost", "highlight_bleach", "brilliance_reduction"):
            x_axis = "ev_axis"
            intensity = 0.18 * np.exp2(axis * 10.0 - 5.0)
            anchors = np.array(((1.0, 1.0, 1.0), (1.15, 0.67, 0.30), (0.35, 0.63, 1.32)), dtype=np.float32)
            samples = intensity[:, None, None] * anchors
        else:
            x_axis = "chroma_axis"
            anchors = np.array(((1.0, 0.16, 0.04), (0.08, 0.85, 0.19), (0.10, 0.26, 1.0)), dtype=np.float32)
            samples = 0.23 + (anchors - 0.23) * axis[:, None, None]

        shape = samples.shape
        rgb = samples.reshape(-1, 3)
        basis = _RGB_BASIS.get(self._gamut)
        if basis is not None:
            rgb = rgb @ basis.T
        before = self._apply_prefix(rgb, stages[:index])
        after = self._apply_prefix(before, stages[index:index + 1])
        origin = chromogen_coordinates(before).reshape(shape)
        destination = chromogen_coordinates(after).reshape(shape)
        if kind in ("sector_brightness", "contrast_boost", "brilliance_reduction"):
            values = destination[..., 0] - origin[..., 0]
            key = "response_ev"
        elif kind in ("sector_skew", "sector_squash"):
            a = chromogen_hue(before).reshape(shape[:-1])
            b = chromogen_hue(after).reshape(shape[:-1])
            values = (b - a + 180.0) % 360.0 - 180.0
            key = "response_hue"
        else:
            values = (np.linalg.norm(destination[..., 1:], axis=-1)
                      - np.linalg.norm(origin[..., 1:], axis=-1))
            key = "response_chroma"
        if x_axis == "hue_axis":
            hue_axis = chromogen_hue(before).reshape(shape[:-1])
            order = hue_axis.argsort(axis=0)
            axis = (np.take_along_axis(hue_axis, order, axis=0) + 180.0) / 360.0
            values = np.take_along_axis(values, order, axis=0)
        else:
            axis = np.broadcast_to(axis[:, None], values.shape)
        return key, x_axis, axis, values

    def _coordinates(self, rgb: np.ndarray, radial: bool) -> np.ndarray:
        if self._projection == "rgb":
            return rgb * 2.0 - 1.0
        coordinates = chromogen_coordinates(rgb)
        if radial:
            return coordinates[..., 1:]
        return coordinates[..., [1, 0, 2]] / np.array((1.0, 5.0, 1.0), dtype=np.float32)

    def _projection_points(self, points: np.ndarray, plot: QRectF) -> tuple[np.ndarray, np.ndarray]:
        radial = self._projection == "radial"
        if radial:
            ux, uy = points.T
            depths = np.zeros(len(points), dtype=np.float32)
        else:
            x, y, z = points.T
            yaw_cos, yaw_sin = math.cos(self._yaw), math.sin(self._yaw)
            pitch_cos, pitch_sin = math.cos(self._pitch), math.sin(self._pitch)
            ux, depth = x * yaw_cos + z * yaw_sin, -x * yaw_sin + z * yaw_cos
            uy = y * pitch_cos - depth * pitch_sin
            depths = y * pitch_sin + depth * pitch_cos
        scale = min(plot.width(), plot.height()) * (0.44 if radial else 0.31) * self._zoom
        center = plot.center() + self._pan
        return np.column_stack((center.x() + ux * scale, center.y() - uy * scale)), depths

    def _paint_axes(self, painter: QPainter, plot: QRectF) -> None:
        if self._projection == "radial":
            center = plot.center() + self._pan
            radius = min(plot.width(), plot.height()) * 0.44 * self._zoom
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(_GUIDE, 0.7))
            for ring in (0.33, 0.67, 1.0):
                painter.drawEllipse(center, radius * ring, radius * ring)
            painter.setPen(QPen(_AXES, 0.75))
            painter.drawLine(center + QPointF(-radius, 0.0), center + QPointF(radius, 0.0))
            painter.drawLine(center + QPointF(0.0, -radius), center + QPointF(0.0, radius))
            return

        points, _ = self._projection_points(_CUBE_CORNERS * 2.0 - 1.0, plot)
        painter.setPen(QPen(_GUIDE, 0.9))
        for i, j in _CUBE_EDGES:
            painter.drawLine(QPointF(*points[i]), QPointF(*points[j]))

    def _paint_points(self, painter: QPainter, pixels: np.ndarray, plot: QRectF, *, before: bool) -> None:
        coordinates = self._coordinates(pixels, self._projection == "radial")
        mapped, depths = self._projection_points(coordinates, plot)
        painter.setPen(Qt.PenStyle.NoPen)
        for index in depths.argsort():
            point = QPointF(*mapped[index])
            if not plot.contains(point):
                continue
            painter.setBrush(_display_color(pixels[index], 97 if before else 208))
            radius = 1.7 if before else 2.2
            painter.drawEllipse(point, radius, radius)

    def _paint_pick(self, painter: QPainter, plot: QRectF) -> None:
        if self._picked_color is None or self._picked_after is None:
            return
        pixels = np.array((self._picked_color, self._picked_after), dtype=np.float32)
        mapped, _ = self._projection_points(self._coordinates(pixels, self._projection == "radial"), plot)
        a, b = (QPointF(*point) for point in mapped)
        painter.setPen(QPen(_HOT, 1.1))
        painter.drawLine(a, b)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(a, 4.2, 4.2)
        painter.setBrush(_HOT)
        painter.drawEllipse(b, 3.2, 3.2)

    def _paint_response(self, painter: QPainter, plot: QRectF) -> None:
        if self._response is None:
            painter.setPen(_TEXT)
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                             I18N.tr("chromogen.select_stage", "Select a stage"))
            return
        key, x_axis, axis, values = self._response
        area = plot.adjusted(12.0, 20.0, -12.0, -22.0)
        if area.isEmpty():
            return
        y_extent = max(0.02, float(np.max(np.abs(values))) * 1.16)
        center = area.center()
        painter.setPen(QPen(_GUIDE, 0.7))
        for fraction in (0.25, 0.5, 0.75):
            x = area.left() + fraction * area.width()
            painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
        painter.setPen(QPen(_AXES, 0.8, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(area.left(), center.y()), QPointF(area.right(), center.y()))
        for samples, line, color in zip(axis.T, values.T, _RESPONSE_COLORS):
            path = QPainterPath()
            for index, (x, value) in enumerate(zip(samples, line)):
                point = QPointF(area.left() + float(x) * area.width(),
                                center.y() - float(value) * area.height() / (2.0 * y_extent))
                (path.moveTo if index == 0 else path.lineTo)(point)
            painter.setPen(QPen(color, 1.3))
            painter.drawPath(path)
        titles = {"response_ev": "Exposure change (EV)", "response_hue": "Hue change (°)",
                  "response_chroma": "Chroma change"}
        painter.setPen(_TEXT)
        painter.drawText(plot.adjusted(5.0, 2.0, -5.0, 0.0), Qt.AlignmentFlag.AlignTop,
                         I18N.tr(f"chromogen.{key}", titles[key]))
        painter.drawText(plot.adjusted(5.0, 0.0, -5.0, -4.0), Qt.AlignmentFlag.AlignBottom,
                         I18N.tr(f"chromogen.{x_axis}", _AXIS_LABELS[x_axis]))

    def _plot_rects(self) -> tuple[QRectF, QRectF]:
        full = QRectF(self.rect()).adjusted(3.5, 3.5, -3.5, -3.5)
        if self._projection == "response":
            return QRectF(), full
        if self._response is None or full.width() < 360.0:
            return full, QRectF()
        # At dock widths that can fit the axes, keep the two views in sight.
        seam = full.left() + full.width() * 0.55
        return (QRectF(full.left(), full.top(), seam - full.left(), full.height()),
                QRectF(seam + 1.0, full.top(), full.right() - seam - 1.0, full.height()))

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QColor(C.PANEL_DIVIDER))
        painter.setBrush(QColor(C.COLOR_PANEL_INSET))
        painter.drawRoundedRect(frame, 3.0, 3.0)
        cloud, response = self._plot_rects()
        if not cloud.isEmpty():
            painter.save()
            painter.setClipRect(cloud)
            self._paint_axes(painter, cloud)
            if self._compare:
                self._paint_points(painter, self._before, cloud, before=True)
            self._paint_points(painter, self._after, cloud, before=False)
            self._paint_pick(painter, cloud)
            painter.restore()
        if not response.isEmpty():
            if not cloud.isEmpty():
                painter.setPen(QPen(_GUIDE, 1.0))
                painter.drawLine(cloud.topRight(), cloud.bottomRight())
            painter.setClipRect(response)
            self._paint_response(painter, response)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        cloud, _ = self._plot_rects()
        if not cloud.contains(event.position()) or event.button() not in (
            Qt.MouseButton.LeftButton, Qt.MouseButton.MiddleButton,
        ):
            super().mousePressEvent(event)
            return
        pan = (self._projection == "radial" or event.button() == Qt.MouseButton.MiddleButton
               or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
        self._drag = "pan" if pan else "rotate"
        self._last_point = event.position()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag is None:
            super().mouseMoveEvent(event)
            return
        movement = event.position() - self._last_point
        self._last_point = event.position()
        if self._drag == "pan":
            self._pan += movement
        else:
            self._yaw += movement.x() * 0.012
            self._pitch = max(-1.52, min(1.52, self._pitch + movement.y() * 0.012))
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag is not None:
            self.mouseMoveEvent(event)
            self._drag = None
            self.unsetCursor()
            event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self.reset_view()
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:
        cloud, _ = self._plot_rects()
        if not cloud.contains(event.position()):
            event.ignore()
            return
        old_zoom = self._zoom
        self._zoom = max(0.5, min(8.0, old_zoom * 1.14 ** (event.angleDelta().y() / 120)))
        center = cloud.center()
        cursor = event.position() - center
        self._pan = cursor + (self._pan - cursor) * (self._zoom / old_zoom)
        self.update()
        event.accept()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.UngrabMouse:
            self._drag = None
            self.unsetCursor()
        return super().event(event)
