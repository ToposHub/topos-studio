"""Scene-referred Tonal Grade controls for the Color workspace."""

from __future__ import annotations

import math
from collections.abc import Mapping
from numbers import Real

import numpy as np
from PyQt6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QBrush, QColor, QHideEvent, QLinearGradient, QMouseEvent, QPainter, QPainterPath,
    QPaintEvent, QPen, QPolygonF,
)
from PyQt6.QtWidgets import (
    QBoxLayout, QButtonGroup, QHBoxLayout, QLabel, QSizePolicy, QStackedWidget, QToolButton,
    QVBoxLayout, QWidget,
)

from src.features.color.core.base_grade import BASE_GRADE_DEFAULTS, BASE_GRADE_LIMITS, base_grade_curve
from src.features.color.ui.color_wheel import (
    HUE_OFFSET_ANGLE, ModeTabButton, ZoneIconWidget,
    _ACCENT_CONTRAST, _ACCENT_NEUTRAL, _ACCENT_RAINBOW, _ACCENT_TEMP, _ACCENT_TINT,
    _GradingValue, _PrimaryValueRow, _WheelCanvas,
)
from src.features.color.ui.panel_chrome import (
    PARAMETER_LABEL_STYLE, SECTION_TITLE_STYLE, create_panel_divider, create_panel_layout,
    style_reset_button,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C


_ZONE_LABELS = {
    "dim": ("Dim", "lift"),
    "balance": ("Balance", "gamma"),
    "light": ("Light", "gain"),
    "dark": ("Dark", "lift"),
    "bright": ("Bright", "gain"),
}
_PIVOT_LABEL = ("base_grade.pivot", "Pivot")
_SAT_LABEL = ("grading.sat", "Saturation")
_EXPOSURE_DRAG_RATE = 0.01
# The primary Gain ring moves 18% grey by about 0.27 EV over 60 logical pixels.
_EXPOSURE_RING_EV_PER_PIXEL = 0.0045
_TINT_DISK_RADIUS = 0.45
_EV_MIN = -8.0
_EV_MAX = 8.0
_CURVE_INPUT_EV = np.linspace(_EV_MIN, _EV_MAX, 129, dtype=np.float32)
_CURVE_INPUT = np.exp2(_CURVE_INPUT_EV) * np.float32(0.18)


def _field(
    key: str, label: str, accent: str, *, decimals: int, width: int, drag_rate: float,
) -> _GradingValue:
    lower, upper = BASE_GRADE_LIMITS[key]
    field = _GradingValue(lower, upper, BASE_GRADE_DEFAULTS[key], decimals, accent,
                          drag_rate=drag_rate, height=22)
    field.fit_display_range(width)
    field.setToolTip(label)
    field.setAccessibleName(label)
    field.setObjectName(key)
    return field


class _BaseGradeCanvas(_WheelCanvas):
    """The shared wheel disk with a separate drag track on each side."""

    _ARC_GAP = 12.0
    _ARC_ANGLE = 58.0
    _ARC_ANGLE_RAD = math.radians(_ARC_ANGLE)
    _ARC_HEIGHT_FRACTION = math.sin(_ARC_ANGLE_RAD)
    _TRACK_WIDTH = 3.0
    _THUMB_RADIUS = 4.5
    _HIT_RADIUS = 8.0
    _ARC_COLORS = (
        ("saturation", -1, "#ff0000", "#ffffff"),
        ("exposure", 1, "#ffffff", "#000000"),
    )

    def __init__(self, wheel: _BaseGradeWheel) -> None:
        super().__init__(wheel)
        self._grade_wheel = wheel
        self._hovered_arc: str | None = None
        self._dragged_arc: str | None = None
        self._drag_position = 0.0
        self._last_y = 0.0

    def _geometry(self) -> tuple[QPointF, float]:
        center, radius = super()._geometry()
        # Leave room for the side tracks and their handles at both extremes.
        horizontal_limit = center.x() - self._ARC_GAP - 7.0
        vertical_limit = (center.y() - 7.0) / self._ARC_HEIGHT_FRACTION - self._ARC_GAP
        return center, max(1.0, min(radius - 4.0, horizontal_limit, vertical_limit))

    def _arc_at(self, point: QPointF) -> str | None:
        center, radius = self._geometry()
        delta = point - center
        angle = math.atan2(delta.y(), abs(delta.x()))
        angle = max(-self._ARC_ANGLE_RAD, min(self._ARC_ANGLE_RAD, angle))
        nearest = QPointF(math.cos(angle), math.sin(angle)) * (radius + self._ARC_GAP)
        if math.hypot(abs(delta.x()) - nearest.x(), delta.y() - nearest.y()) <= self._HIT_RADIUS:
            return "saturation" if delta.x() < 0 else "exposure"
        return None

    def _set_hovered_arc(self, key: str | None) -> None:
        if self._hovered_arc == key:
            return
        self._hovered_arc = key
        if key is None:
            self.unsetCursor()
            self.setToolTip("")
        else:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            label = self._grade_wheel._control(key).accessibleName()
            hint = I18N.tr("base_grade.arc_drag",
                           "Drag up or down. Shift for fine adjustment. Double-click to reset.")
            self.setToolTip(f"{label}\n{hint}")
        self.update()

    def leaveEvent(self, event: QEvent) -> None:
        self._set_hovered_arc(None)
        super().leaveEvent(event)

    def cancel_drag(self) -> None:
        super().cancel_drag()
        if self._dragged_arc is not None:
            self._dragged_arc = None
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center, disk_radius = self._geometry()
        radius = disk_radius + self._ARC_GAP
        track = QRectF(center.x() - radius, center.y() - radius, 2.0 * radius, 2.0 * radius)
        arc_height = radius * self._ARC_HEIGHT_FRACTION
        for key, side, top_color, bottom_color in self._ARC_COLORS:
            origin = 180.0 if side < 0 else 0.0
            position = self._grade_wheel._arc_position(key)
            angle = math.degrees(math.asin(position * self._ARC_HEIGHT_FRACTION)) * side
            active = key in (self._hovered_arc, self._dragged_arc)
            cap = Qt.PenCapStyle.RoundCap
            line = Qt.PenStyle.SolidLine
            start, span = round((origin - self._ARC_ANGLE) * 16), round(self._ARC_ANGLE * 32)
            gradient = QLinearGradient(0.0, center.y() - arc_height, 0.0, center.y() + arc_height)
            gradient.setColorAt(0.0, QColor(top_color))
            gradient.setColorAt(1.0, QColor(bottom_color))
            painter.setPen(QPen(QBrush(gradient), self._TRACK_WIDTH, line, cap))
            painter.drawArc(track, start, span)
            theta = math.radians(origin + angle)
            thumb = center + QPointF(math.cos(theta), -math.sin(theta)) * radius
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#f0b44c" if active else "#f0f1f4"))
            painter.drawEllipse(thumb, self._THUMB_RADIUS, self._THUMB_RADIUS)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        key = self._arc_at(event.position())
        if key is None:
            super().mousePressEvent(event)
        elif event.button() == Qt.MouseButton.MiddleButton:
            self._grade_wheel._reset(key)
            event.accept()
        elif event.button() == Qt.MouseButton.LeftButton:
            self._dragged_arc = key
            self._drag_position = self._grade_wheel._arc_position(key)
            self._last_y = event.position().y()
            self.interaction_started.emit()
            self.update()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        key = self._dragged_arc
        if key is None:
            if not (self.dragging_knob or self.dragging_ring):
                self._set_hovered_arc(self._arc_at(event.position()))
            super().mouseMoveEvent(event)
            return
        y = event.position().y()
        fine = 0.2 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1.0
        movement = (self._last_y - y) * fine
        self._last_y = y
        if key == "exposure":
            self._grade_wheel._drag_exposure(movement)
        elif movement:
            _, radius = self._geometry()
            arc_height = (radius + self._ARC_GAP) * self._ARC_HEIGHT_FRACTION
            position = self._drag_position + movement / arc_height
            self._drag_position = max(-1.0, min(1.0, position))
            self._grade_wheel._set_arc_position(key, self._drag_position)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragged_arc is not None and event.button() == Qt.MouseButton.LeftButton:
            self.mouseMoveEvent(event)
            self._dragged_arc = None
            self._set_hovered_arc(self._arc_at(event.position()))
            self.update()
            self._grade_wheel._on_value_interaction_ended()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        key = self._arc_at(event.position())
        if key is not None and event.button() == Qt.MouseButton.LeftButton:
            self._grade_wheel._reset(key)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)


class _BaseGradeWheel(QWidget):
    """A native Topos color wheel with exposure, opponent tint, and saturation."""

    changed = pyqtSignal(dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()

    def __init__(self, zone: str) -> None:
        super().__init__()
        name, icon = _ZONE_LABELS[zone]
        self._zone = zone
        self._title = I18N.tr(f"base_grade.{zone}", name)
        self.rotation_degrees = HUE_OFFSET_ANGLE
        self.knob_pos = QPointF()
        self.controls: dict[str, _GradingValue] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(2)
        heading = QHBoxLayout()
        heading.setSpacing(0)
        heading.addWidget(ZoneIconWidget(icon))
        heading.addStretch()
        title = QLabel(self._title, self)
        title.setStyleSheet(SECTION_TITLE_STYLE)
        heading.addWidget(title)
        heading.addStretch()
        reset = QToolButton(self)
        style_reset_button(reset)
        reset.setToolTip(I18N.tr("grading.reset_wheel", "Reset") + " " + self._title)
        reset.clicked.connect(self._reset_wheel)
        heading.addWidget(reset)
        layout.addLayout(heading)

        self.canvas = _BaseGradeCanvas(self)
        self.canvas.setMinimumSize(94, 75)
        self.canvas.setAccessibleName(self._title + " " + I18N.tr("base_grade.tint", "Tint"))
        self.canvas.interaction_started.connect(self.interaction_started.emit)
        layout.addWidget(self.canvas, 1)

        exposure = I18N.tr("grading.exp", "Exposure")
        sat = I18N.tr("base_grade.global_sat", "Global Saturation") if zone == "balance" else I18N.tr(*_SAT_LABEL)
        footer = QWidget(self)
        footer.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._footer_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, footer)
        self._footer_layout.setContentsMargins(0, 0, 0, 0)
        self._footer_layout.setSpacing(2)
        self._footer_layout.addStretch(1)
        self._footer_layout.addLayout(self._value_group((
            ("saturation", "SAT", sat, _ACCENT_RAINBOW, 2, 0.008 if zone == "balance" else 0.01, 28),
            ("a", "A", I18N.tr("base_grade.tint_a", "Tint A"), _ACCENT_TINT, 3, 0.001, 34),
        )))
        self._footer_layout.addLayout(self._value_group((
            ("b", "B", I18N.tr("base_grade.tint_b", "Tint B"), _ACCENT_TEMP, 3, 0.001, 34),
            ("exposure", "EV", exposure, _ACCENT_CONTRAST, 2, _EXPOSURE_DRAG_RATE, 30),
        )))
        self._footer_layout.addStretch(1)
        self.inline_width = (self._footer_layout.minimumSize().width()
                             + layout.contentsMargins().left() + layout.contentsMargins().right())
        layout.addWidget(footer)

        shaping = QWidget(self)
        shaping.setFixedHeight(22)
        if zone != "balance":
            falloff = I18N.tr("base_grade.falloff", "Falloff")
            shaping.setLayout(self._value_group((
                ("pivot", I18N.tr(*_PIVOT_LABEL), I18N.tr("base_grade.zone_pivot", "Pivot in stops"),
                 _ACCENT_NEUTRAL, 2, 0.02, 30),
                ("falloff", falloff, falloff, _ACCENT_NEUTRAL, 2, 0.003, 28),
            ), inline=True))
        layout.addWidget(shaping)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_compact(self, compact: bool) -> None:
        direction = QBoxLayout.Direction.TopToBottom if compact else QBoxLayout.Direction.LeftToRight
        if self._footer_layout.direction() != direction:
            self._footer_layout.setDirection(direction)

    def _value_group(
        self, specs: tuple[tuple[str, str, str, str, int, float, int], ...], *, inline: bool = False,
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4 if inline else 2)
        row.addStretch(1)
        for key, caption, label, accent, precision, rate, width in specs:
            pair = QHBoxLayout() if inline else QVBoxLayout()
            pair.setSpacing(2 if inline else 0)
            text = QLabel(caption, self)
            text.setStyleSheet(PARAMETER_LABEL_STYLE)
            text.setAlignment(Qt.AlignmentFlag.AlignCenter)
            text.setToolTip(label)
            pair.addWidget(text)
            full_key = self._parameter_key(key)
            field = _field(full_key, f"{self._title} {label}", accent,
                           decimals=precision, width=width, drag_rate=rate)
            field.valueChanged.connect(lambda value, k=full_key: self._field_changed(k, value))
            field.interaction_started.connect(self.interaction_started.emit)
            field.interaction_ended.connect(self.interaction_ended.emit)
            self.controls[full_key] = field
            pair.addWidget(field)
            row.addLayout(pair)
        row.addStretch(1)
        return row

    def set_rotation_degrees(self, rotation: float) -> None:
        self.rotation_degrees = rotation % 360.0
        self.refresh()

    def refresh(self) -> None:
        """Update the puck and arcs from the stored numeric settings."""
        a, b = (self._value(axis) / _TINT_DISK_RADIUS for axis in ("a", "b"))
        angle = math.radians(self.rotation_degrees - 15.0)
        cos_angle, sin_angle = math.cos(angle), math.sin(angle)
        self.knob_pos = QPointF(a * cos_angle - b * sin_angle, -a * sin_angle - b * cos_angle)
        self.canvas.update()

    def _parameter_key(self, key: str) -> str:
        if self._zone == "balance" and key == "saturation":
            return "base_grade_saturation"
        return f"base_grade_{self._zone}_{key}"

    def _control(self, key: str) -> _GradingValue:
        return self.controls[self._parameter_key(key)]

    def _value(self, key: str) -> float:
        return self._control(key).value()

    def _field_changed(self, key: str, value: float) -> None:
        self.refresh()
        self.changed.emit({key: value})

    def _set_wheel_values(self, **values: float) -> None:
        changes = {}
        for key, value in values.items():
            full_key = self._parameter_key(key)
            field = self.controls[full_key]
            original = field.value()
            blocked = field.blockSignals(True)
            field.setValue(value)
            field.blockSignals(blocked)
            if original != field.value():
                changes[full_key] = field.value()
        if changes:
            self.refresh()
            self.changed.emit(changes)

    def _edit_knob(self, point: QPointF) -> None:
        angle = math.radians(self.rotation_degrees - 15.0)
        cos_angle, sin_angle = math.cos(angle), math.sin(angle)
        self._set_wheel_values(
            a=(point.x() * cos_angle - point.y() * sin_angle) * _TINT_DISK_RADIUS,
            b=(-point.x() * sin_angle - point.y() * cos_angle) * _TINT_DISK_RADIUS,
        )

    def _drag_exposure(self, pixels: float) -> None:
        if pixels:
            self._set_wheel_values(exposure=self._value("exposure") + pixels * _EXPOSURE_RING_EV_PER_PIXEL)

    def _drag_master_ring(self, angle: float, radius: float) -> None:
        self._drag_exposure(angle * radius)

    def _master_arc_position(self) -> float:
        return self._arc_position("exposure")

    def _arc_position(self, key: str) -> float:
        full_key = self._parameter_key(key)
        minimum, maximum = BASE_GRADE_LIMITS[full_key]
        neutral = BASE_GRADE_DEFAULTS[full_key]
        delta = self._value(key) - neutral
        return delta / (maximum - neutral if delta >= 0.0 else neutral - minimum)

    def _set_arc_position(self, key: str, position: float) -> None:
        full_key = self._parameter_key(key)
        minimum, maximum = BASE_GRADE_LIMITS[full_key]
        neutral = BASE_GRADE_DEFAULTS[full_key]
        span = maximum - neutral if position >= 0.0 else neutral - minimum
        self._set_wheel_values(**{key: neutral + position * span})

    def _on_value_interaction_ended(self) -> None:
        self.interaction_ended.emit()

    def _reset(self, *keys: str) -> None:
        defaults = {key: BASE_GRADE_DEFAULTS[self._parameter_key(key)] for key in keys}
        if all(self._value(key) == default for key, default in defaults.items()):
            return
        self.canvas.cancel_drag()
        self.interaction_started.emit()
        self._set_wheel_values(**defaults)
        self.interaction_ended.emit()

    def _reset_wheel(self) -> None:
        self._reset("a", "b", "exposure", "saturation")

    def reset_wheel_only(self) -> None:
        self._reset("a", "b")


class _ToneCurve(QWidget):
    """Neutral response in exposure stops around 18% gray."""

    pivot_changed = pyqtSignal(str, float)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()

    _PIVOT_COLORS = ("#8cadd5", "#d9b588")
    _OFFSCREEN_DRAG_EV = 3.0

    def __init__(self) -> None:
        super().__init__()
        self._output_ev = _CURVE_INPUT_EV
        self._global_ev = _CURVE_INPUT_EV
        self._stage_ev = {zone: _CURVE_INPUT_EV for zone in ("dim", "light", "dark", "bright")}
        self._params = BASE_GRADE_DEFAULTS.copy()
        self._zones = ("dim", "light")
        self._hovered_pivot: str | None = None
        self._dragged_pivot: str | None = None
        self._drag_input_ev = 0.0
        self._last_x = 0.0
        self.setMinimumWidth(190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setToolTip(I18N.tr("base_grade.tone_curve_tooltip",
                               "Neutral tone response in stops relative to 18% gray. Drag a triangle to adjust its zone pivot."))
        self.setAccessibleName(I18N.tr("base_grade.tone_curve", "Tone Curve"))

    @staticmethod
    def _log_ev(level: np.ndarray) -> np.ndarray:
        return np.log2(np.maximum(level, 1e-8) / np.float32(0.18))

    def set_params(self, params: Mapping[str, float]) -> None:
        if params == self._params:
            return
        stages: dict[str, np.ndarray] = {}
        self._output_ev = self._log_ev(base_grade_curve(_CURVE_INPUT, params, stage_levels=stages))
        self._global_ev = self._log_ev(stages.pop("global"))
        # Keep the pivot projection ordered if an extreme grade folds the neutral ramp.
        self._stage_ev = {zone: np.maximum.accumulate(self._log_ev(level)) for zone, level in stages.items()}
        self._params = dict(params)
        self.update()

    def set_zones(self, lower: str, upper: str) -> None:
        self._zones = lower, upper
        self._set_hovered_pivot(None)
        self.update()

    def _frame_rect(self) -> QRectF:
        return QRectF(self.rect().adjusted(0, 0, -1, -1))

    def _plot_rect(self) -> QRectF:
        return self._frame_rect().adjusted(9.0, 22.0, -9.0, -41.0)

    def _edge_slope(self, zone: str, index: int) -> float:
        stage = self._stage_ev[zone]
        neighbor = 1 if index == 0 else -2
        slope = float((stage[index] - stage[neighbor])
                      / (_CURVE_INPUT_EV[index] - _CURVE_INPUT_EV[neighbor]))
        minimum, maximum = BASE_GRADE_LIMITS[f"base_grade_{zone}_pivot"]
        offscreen = float(stage[0]) - minimum if index == 0 else maximum - float(stage[-1])
        # Compress the offscreen interval so an edge handle can be brought back in one drag.
        return max(0.1, slope, offscreen / self._OFFSCREEN_DRAG_EV)

    def _input_for_pivot(self, zone: str, pivot: float | None = None) -> float:
        if pivot is None:
            pivot = self._params[f"base_grade_{zone}_pivot"]
        stage = self._stage_ev[zone]
        if pivot < stage[0]:
            return _EV_MIN + (pivot - float(stage[0])) / self._edge_slope(zone, 0)
        if pivot > stage[-1]:
            return _EV_MAX + (pivot - float(stage[-1])) / self._edge_slope(zone, -1)
        return float(np.interp(pivot, stage, _CURVE_INPUT_EV))

    def _pivot_for_input(self, zone: str, input_ev: float) -> float:
        stage = self._stage_ev[zone]
        if input_ev < _EV_MIN:
            return float(stage[0]) + (input_ev - _EV_MIN) * self._edge_slope(zone, 0)
        if input_ev > _EV_MAX:
            return float(stage[-1]) + (input_ev - _EV_MAX) * self._edge_slope(zone, -1)
        return float(np.interp(input_ev, _CURVE_INPUT_EV, stage))

    def _pivot_point(self, zone: str, plot: QRectF) -> QPointF:
        source = max(_EV_MIN, min(_EV_MAX, self._input_for_pivot(zone)))
        x = plot.left() + plot.width() * (source - _EV_MIN) / (_EV_MAX - _EV_MIN)
        # Separate lanes keep both handles selectable when the projected pivots overlap.
        return QPointF(x, plot.bottom() + 7.0 + 13.0 * self._zones.index(zone))

    def _pivot_at(self, position: QPointF) -> str | None:
        plot = self._plot_rect()
        if not plot.isEmpty():
            for zone in self._zones:
                delta = position - self._pivot_point(zone, plot)
                if abs(delta.x()) <= 9.0 and abs(delta.y()) <= 6.0:
                    return zone
        return None

    def _set_hovered_pivot(self, zone: str | None) -> None:
        if self._hovered_pivot == zone:
            return
        self._hovered_pivot = zone
        if zone is None:
            self.unsetCursor()
        else:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.update()

    def cancel_drag(self) -> bool:
        active = self._dragged_pivot is not None
        self._dragged_pivot = None
        self._set_hovered_pivot(None)
        if active:
            self.update()
        return active

    def hideEvent(self, event: QHideEvent) -> None:
        if self.cancel_drag():
            self.interaction_ended.emit()
        super().hideEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        if self._dragged_pivot is None:
            self._set_hovered_pivot(None)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        zone = self._pivot_at(event.position())
        if event.button() != Qt.MouseButton.LeftButton or zone is None:
            super().mousePressEvent(event)
            return
        self._dragged_pivot = zone
        self._drag_input_ev = self._input_for_pivot(zone)
        self._last_x = event.position().x()
        self._set_hovered_pivot(zone)
        self.interaction_started.emit()
        self.update()
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        zone = self._dragged_pivot
        if zone is None:
            self._set_hovered_pivot(self._pivot_at(event.position()))
            super().mouseMoveEvent(event)
            return
        x = event.position().x()
        dx = x - self._last_x
        self._last_x = x
        plot = self._plot_rect()
        if dx and not plot.isEmpty():
            self._drag_input_ev += dx * (_EV_MAX - _EV_MIN) / plot.width()
            minimum, maximum = BASE_GRADE_LIMITS[f"base_grade_{zone}_pivot"]
            pivot = self._pivot_for_input(zone, self._drag_input_ev)
            limited = max(minimum, min(maximum, pivot))
            if limited != pivot:
                self._drag_input_ev = self._input_for_pivot(zone, limited)
            self.pivot_changed.emit(zone, limited)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragged_pivot is not None:
            self.mouseMoveEvent(event)
            self._dragged_pivot = None
            self.interaction_ended.emit()
            self._set_hovered_pivot(self._pivot_at(event.position()))
            self.update()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    @staticmethod
    def _response_path(values: np.ndarray, plot: QRectF) -> QPainterPath:
        path = QPainterPath()
        for index, (in_ev, out_ev) in enumerate(zip(_CURVE_INPUT_EV, values)):
            x = plot.left() + plot.width() * (float(in_ev) - _EV_MIN) / (_EV_MAX - _EV_MIN)
            y = plot.bottom() - plot.height() * (float(out_ev) - _EV_MIN) / (_EV_MAX - _EV_MIN)
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        return path

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame, plot = self._frame_rect(), self._plot_rect()
        painter.setPen(QColor(C.PANEL_DIVIDER))
        painter.setBrush(QColor(C.COLOR_PANEL_INSET))
        painter.drawRoundedRect(frame, 3.0, 3.0)
        if plot.isEmpty():
            return

        font = painter.font()
        font.setPixelSize(10)
        painter.setFont(font)
        painter.setPen(QColor(C.TEXT_SECONDARY))
        painter.drawText(frame.adjusted(3.0, 3.0, -3.0, 0.0),
                         Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
                         I18N.tr("base_grade.tone_curve", "Tone Curve"))
        painter.setPen(QPen(QColor(C.BORDER_DARK), 0.75))
        for fraction in (0.25, 0.5, 0.75):
            x = plot.left() + plot.width() * fraction
            y = plot.bottom() - plot.height() * fraction
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        painter.setPen(QPen(QColor(C.TEXT_HINT), 0.8, Qt.PenStyle.DotLine))
        painter.drawLine(plot.bottomLeft(), plot.topRight())

        pivots = [(zone, QColor(color), self._pivot_point(zone, plot))
                  for zone, color in zip(self._zones, self._PIVOT_COLORS)]
        painter.save()
        painter.setClipRect(plot.adjusted(-1.0, -1.0, 1.0, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for lane, (zone, color, point) in enumerate(pivots):
            guide = QColor(color)
            guide.setAlpha(235 if zone in (self._hovered_pivot, self._dragged_pivot) else 135)
            pen = QPen(guide, 1.0, Qt.PenStyle.DashLine)
            pen.setDashOffset(3.0 * lane)
            painter.setPen(pen)
            painter.drawLine(QPointF(point.x(), plot.top()), QPointF(point.x(), plot.bottom()))
        painter.setPen(QPen(QColor("#f0b44c"), 1.4))
        painter.drawPath(self._response_path(self._output_ev, plot))
        painter.setPen(QPen(QColor("#a2a9b5"), 1.0, Qt.PenStyle.DashLine))
        painter.drawPath(self._response_path(self._global_ev, plot))
        painter.restore()

        painter.setPen(QPen(QColor(C.TEXT_HINT), 0.75))
        painter.drawLine(plot.bottomLeft(), plot.bottomRight())
        for fraction, label in ((0.0, "−8"), (0.5, "0"), (1.0, "+8")):
            x = plot.left() + plot.width() * fraction
            painter.drawText(QRectF(x - 13.0, plot.bottom() + 26.0, 26.0, 13.0),
                             Qt.AlignmentFlag.AlignCenter, label)
        for zone, color, point in pivots:
            active = zone in (self._hovered_pivot, self._dragged_pivot)
            painter.setPen(QPen(QColor("#eef0f5" if active else "#151619"), 1.0))
            painter.setBrush(color.lighter(125) if active else color)
            source = self._input_for_pivot(zone)
            if _EV_MIN <= source <= _EV_MAX:
                vertices = (QPointF(0.0, -4.0), QPointF(-5.5, 3.5), QPointF(5.5, 3.5))
            else:
                inward = 1 if source < _EV_MIN else -1
                vertices = (QPointF(4.0 * inward, 0.0), QPointF(-3.5 * inward, 5.0),
                            QPointF(-3.5 * inward, -5.0))
            painter.drawPolygon(QPolygonF([point + vertex for vertex in vertices]))


class BaseGradePanel(QWidget):
    """Five exposure/tint grades plus global and zone-specific shaping."""

    changed = pyqtSignal(dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._syncing = False
        self._interaction_active = False
        self._pending: dict[str, float] = {}
        self._accepted_params = BASE_GRADE_DEFAULTS.copy()
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(16)
        self._update_timer.timeout.connect(self._publish)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)

        header, layout = create_panel_layout(self, margins=(8, 8, 4), spacing=4)
        title = QLabel(I18N.tr("base_grade.title", "Tonal Grade"))
        title.setStyleSheet(SECTION_TITLE_STYLE)
        header.addWidget(title)
        header.addStretch()
        self.zone_buttons = QButtonGroup(self)
        for index, (mode, label) in enumerate((
            ("dim_light", I18N.tr("base_grade.dim_light", "Dim / Balance / Light")),
            ("dark_bright", I18N.tr("base_grade.dark_bright", "Dark / Balance / Bright")),
        )):
            button = ModeTabButton(mode, label)
            button.setObjectName(f"baseGradeMode_{mode}")
            self.zone_buttons.addButton(button, index)
            header.addWidget(button)
        self.zone_buttons.button(0).setChecked(True)
        self.zone_buttons.idClicked.connect(self._switch_zones)
        divider = create_panel_divider(Qt.Orientation.Vertical)
        divider.setFixedHeight(16)
        header.addWidget(divider)
        self.reset_btn = QToolButton(self)
        style_reset_button(self.reset_btn, global_reset=True)
        reset_label = I18N.tr("base_grade.reset", "Reset Tonal Grade")
        self.reset_btn.setToolTip(reset_label)
        self.reset_btn.setAccessibleName(reset_label)
        self.reset_btn.clicked.connect(self.reset)
        header.addWidget(self.reset_btn)

        globals_layout = QHBoxLayout()
        globals_layout.setSpacing(0)
        self._globals: dict[str, _PrimaryValueRow] = {}
        for name, label, accent, rate, precision in (
            ("flare", I18N.tr("base_grade.flare", "Flare"), _ACCENT_NEUTRAL, 0.001, 3),
            ("contrast", I18N.tr("grading.contrast", "Contrast"), _ACCENT_CONTRAST, 0.004, 3),
            ("contrast_pivot", I18N.tr("base_grade.contrast_pivot", "Contrast Pivot"), _ACCENT_NEUTRAL, 0.02, 2),
        ):
            key = f"base_grade_{name}"
            minimum, maximum = BASE_GRADE_LIMITS[key]
            row = _PrimaryValueRow(label, minimum, maximum, default=BASE_GRADE_DEFAULTS[key],
                                   drag_rate=rate, accent=accent, decimals=precision)
            row.spin.setObjectName(key)
            row.valueChanged.connect(lambda value, k=key: self._changed({k: value}))
            row.interactionStarted.connect(self._start_interaction)
            row.interactionEnded.connect(self._finish_interaction)
            self._globals[key] = row
            globals_layout.addWidget(row, 1)
        layout.addLayout(globals_layout)
        layout.addWidget(create_panel_divider(Qt.Orientation.Horizontal))

        self._wheel_row = QWidget(self)
        wheel_layout = QHBoxLayout(self._wheel_row)
        wheel_layout.setContentsMargins(0, 0, 0, 0)
        wheel_layout.setSpacing(4)
        self._wheels = {zone: _BaseGradeWheel(zone) for zone in _ZONE_LABELS}
        for wheel in self._wheels.values():
            wheel.changed.connect(self._changed)
            wheel.interaction_started.connect(self._start_interaction)
            wheel.interaction_ended.connect(self._finish_interaction)
        self._lower = QStackedWidget()
        self._lower.addWidget(self._wheels["dim"])
        self._lower.addWidget(self._wheels["dark"])
        self._upper = QStackedWidget()
        self._upper.addWidget(self._wheels["light"])
        self._upper.addWidget(self._wheels["bright"])
        wheel_layout.addWidget(self._lower, 1)
        wheel_layout.addWidget(self._wheels["balance"], 1)
        wheel_layout.addWidget(self._upper, 1)
        self.tone_curve = _ToneCurve()
        self.tone_curve.pivot_changed.connect(self._set_zone_pivot)
        self.tone_curve.interaction_started.connect(self._start_interaction)
        self.tone_curve.interaction_ended.connect(self._finish_interaction)
        wheel_layout.addWidget(self.tone_curve, 1)
        wheel_width = max(wheel.inline_width for wheel in self._wheels.values())
        self._inline_wheels_width = 3 * wheel_width + 2 * wheel_layout.spacing()
        self._curve_show_width = self._inline_wheels_width + self.tone_curve.minimumWidth() + wheel_layout.spacing()
        self.tone_curve.hide()
        self._wheel_row.setMinimumWidth(self._wheel_row.minimumSizeHint().width())
        self._wheel_row.installEventFilter(self)
        layout.addWidget(self._wheel_row, 1)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._wheel_row and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            width = self._wheel_row.width()
            self.tone_curve.setVisible(width >= self._curve_show_width)
            for wheel in self._wheels.values():
                wheel.set_compact(width < self._inline_wheels_width)
        return super().eventFilter(watched, event)

    def hideEvent(self, event: QHideEvent) -> None:
        self.finish_edit(commit=True)
        super().hideEvent(event)

    def _switch_zones(self, index: int) -> None:
        if self._lower.currentIndex() == index:
            return
        self.finish_edit(commit=True)
        self._lower.setCurrentIndex(index)
        self._upper.setCurrentIndex(index)
        self.tone_curve.set_zones(*(("dim", "light") if index == 0 else ("dark", "bright")))

    def _set_zone_pivot(self, zone: str, pivot: float) -> None:
        self._wheels[zone]._control("pivot").setValue(pivot)

    def _changed(self, changes: dict[str, float]) -> None:
        if self._syncing:
            return
        self._pending.update(changes)
        if not self._update_timer.isActive():
            self._update_timer.start()

    def _publish(self) -> None:
        if self._pending:
            patch, self._pending = self._pending, {}
            self._accepted_params.update(patch)
            self.tone_curve.set_params(self._accepted_params)
            self.changed.emit(patch)

    def _start_interaction(self) -> None:
        if not self._interaction_active:
            self._interaction_active = True
            self.interaction_started.emit()

    def _finish_interaction(self) -> None:
        active = self._interaction_active
        self._interaction_active = False
        self.flush_pending_changes()
        if active:
            self.interaction_ended.emit()

    def finish_edit(self, *, commit: bool) -> None:
        """Stop pointer input before leaving the tool or restoring a grade."""
        self.tone_curve.cancel_drag()
        for _, field in self._iter_fields():
            field.cancel_drag(end_on_release=False)
            field._cancel_edit()
        for wheel in self._wheels.values():
            wheel.canvas.cancel_drag()
        if not commit and self._pending:
            restore = {key: self._accepted_params[key] for key in self._pending}
            self._pending.clear()
            self._set_values(restore)
        self._finish_interaction()

    def flush_pending_changes(self) -> None:
        self._update_timer.stop()
        self._publish()

    def set_rotation_degrees(self, rotation: float) -> None:
        for wheel in self._wheels.values():
            wheel.set_rotation_degrees(rotation)

    def get_params(self) -> dict[str, float]:
        values = {key: row.value() for key, row in self._globals.items()}
        values.update({key: field.value() for wheel in self._wheels.values()
                       for key, field in wheel.controls.items()})
        return values

    def set_params(self, params: Mapping[str, float]) -> None:
        """Project incoming programmatic values onto controls without posting an edit."""
        self.finish_edit(commit=False)
        self._set_values(params)

    def _set_values(self, params: Mapping[str, float]) -> None:
        self._syncing = True
        try:
            for key, field in self._iter_fields():
                if key not in params:
                    continue
                value = params[key]
                if not isinstance(value, Real) or not math.isfinite(value):
                    value = BASE_GRADE_DEFAULTS[key]
                blocked = field.blockSignals(True)
                try:
                    field.setValue(value)
                finally:
                    field.blockSignals(blocked)
                self._accepted_params[key] = field.value()
            for wheel in self._wheels.values():
                wheel.refresh()
            self.tone_curve.set_params(self._accepted_params)
        finally:
            self._syncing = False

    def _iter_fields(self) -> list[tuple[str, _GradingValue]]:
        return [(key, row.spin) for key, row in self._globals.items()] + [
            entry for wheel in self._wheels.values() for entry in wheel.controls.items()
        ]

    def reset(self) -> None:
        self.finish_edit(commit=True)
        before = self.get_params()
        changes = {key: value for key, value in BASE_GRADE_DEFAULTS.items() if before[key] != value}
        if not changes:
            return
        self._start_interaction()
        self._set_values(BASE_GRADE_DEFAULTS)
        self._changed(changes)
        self._finish_interaction()
