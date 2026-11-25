"""A dedicated palette for selective color grading on a corrector."""
from __future__ import annotations

import sys
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import numpy as np
from PyQt6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QCursor, QHideEvent, QImage, QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPen, QPolygonF, QShowEvent,
    QTransform,
)
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea, QSlider, QToolButton,
    QVBoxLayout, QWidget,
)

from src.features.color.core.warper_chroma709 import chroma_warp_color, chroma_warp_position
from src.features.color.core.warper_density import plot_density_rgba, warper_plot_positions
from src.features.color.core.warper_state import (
    MAX_PINS, MAX_STROKES, WarperPinPoint, WarperStroke, XGradeState,
)
from src.features.color.core.x_grade_domain import X_GRADE_SCOPE_AXES, X_GRADE_WORKING_MESSAGE
from src.features.color.ui.panel_chrome import (
    PARAMETER_LABEL_STYLE, PARAMETER_ROW_HEIGHT, SECTION_TITLE_STYLE,
    create_panel_divider, create_panel_header, style_header_button,
)
from src.features.color.ui.warper_panel import WarperPanel
from src.features.color.ui.warper_tools import reset_button, tool_button
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.chroma_warp_canvas import ChromaWarpCanvas
from src.shared.ui.widgets.drag_value import DragValue

if TYPE_CHECKING:
    from src.features.color.core.layer import Layer


def _tr(key: str, fallback: str) -> str:
    return I18N.tr(f"x_grade.{key}", fallback)


_WIDTH_SCALE = 5.0
_NEUTRAL_POSITION = chroma_warp_position(np.ones(3))
# Align the scope's opponent axes using the measured red/orange, green,
# blue and neutral chart anchors. Authoring coordinates remain unchanged.
_SCOPE_AXES = X_GRADE_SCOPE_AXES
_SCOPE_INVERSE = np.linalg.inv(_SCOPE_AXES)
_CONTOUR_ANGLES = np.linspace(0.0, 2.0 * np.pi, 48, endpoint=False)
_CONTOUR = np.stack((np.cos(_CONTOUR_ANGLES), np.sin(_CONTOUR_ANGLES)), axis=1)
_REFERENCE_SCOPE_PIXELS = 900.0
_VIEWER_DRAG_RATIO = 0.191
_SLIDER_LIMITS = {"exposure": (-3.0, 3.0, 123), "width": (0.0, 1.0, 127)}
_NUMERIC_CONTROLS = (
    ("exposure", "Exposure", -8.0, 8.0, 0.0),
    ("width", "Width", 0.01, 1.0, 0.2),
    ("tonal_pivot", "Pivot", 0.0, 1.0, 0.5),
    ("falloff", "Falloff", 0.0, 1.0, 0.5),
)


class XGradeCanvas(ChromaWarpCanvas):
    """Point-centered adjustments with modifier gestures on the plot."""

    protect_neutral = True
    read_only = False
    viewer_modifiers = Qt.KeyboardModifier.NoModifier
    _parameter_drag: tuple[str, float, QPointF] | None = None
    _parameter_value = 0.0
    limit_reached = pyqtSignal()

    def plot_rect(self) -> QRectF:
        return QRectF(0.0, 0.0, max(1, self.width()), max(1, self.height()))

    @staticmethod
    def display_positions(points: np.ndarray) -> np.ndarray:
        return (points - _NEUTRAL_POSITION) @ _SCOPE_AXES + 0.5

    def _screen(self, point: list[float]) -> QPointF:
        rect = self.plot_rect()
        x, y = self.display_positions(np.asarray(point))
        return QPointF(rect.left() + x * rect.width(), rect.top() + y * rect.height())

    def _normalized(self, point: QPointF) -> list[float]:
        rect = self.plot_rect()
        normalized = np.array(((point.x() - rect.left()) / rect.width(), (point.y() - rect.top()) / rect.height()))
        return np.clip((normalized - 0.5) @ _SCOPE_INVERSE + _NEUTRAL_POSITION, 0.0, 1.0).tolist()

    def viewer_delta(self, delta: QPointF) -> QPointF:
        if self._parameter_drag is not None:
            return delta
        rect = self.plot_rect()
        return QPointF(delta.x() * rect.width(), delta.y() * rect.height()) * (
            _VIEWER_DRAG_RATIO / _REFERENCE_SCOPE_PIXELS)

    def _field(self) -> QImage:
        if self._background is None:
            size = 256
            y, x = np.mgrid[0:size, 0:size] / (size - 1)
            coordinates = (np.stack((x, y), axis=-1) - 0.5) @ _SCOPE_INVERSE + _NEUTRAL_POSITION
            rgb = chroma_warp_color(coordinates)
            rgb /= rgb.max(axis=-1, keepdims=True)
            distance = np.clip(np.hypot(x - 0.5, y - 0.5) * 2.0, 0.0, 1.0)
            light = 0.23 + 0.24 * distance * distance * (3.0 - 2.0 * distance)
            base = np.array(QColor(C.BG_PANEL).getRgb()[:3]) / 255.0
            field = np.ascontiguousarray(np.round((base + rgb * light[..., None]) * 255), dtype=np.uint8)
            self._background = QImage(field.data, size, size, size * 3, QImage.Format.Format_RGB888).copy()
        return self._background

    @staticmethod
    def _scope_outline(rect: QRectF) -> QPainterPath:
        """Shape the display field in screen space, separate from the RGB gamut."""
        transform = QTransform(rect.width(), 0.0, 0.0, rect.height(), rect.left(), rect.top())
        path = QPainterPath(QPointF(0.136, 0.902))
        path.cubicTo(0.035, 0.792, 0.040, 0.367, 0.174, 0.190)
        path.cubicTo(0.247, 0.075, 0.440, 0.048, 0.620, 0.099)
        path.cubicTo(0.827, 0.135, 0.958, 0.320, 0.937, 0.500)
        path.cubicTo(0.921, 0.650, 0.675, 0.730, 0.500, 0.810)
        path.cubicTo(0.366, 0.865, 0.209, 0.980, 0.136, 0.902)
        path.closeSubpath()
        return transform.map(path)

    def _paint_field(self, painter: QPainter, rect: QRectF) -> None:
        outline = self._scope_outline(rect)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(rect, self._field())
        outside = QPainterPath()
        outside.addRect(rect)
        painter.fillPath(outside.subtracted(outline), QColor(0, 0, 0, 170))

    def _paint_viewer_sample(self, painter: QPainter, point: np.ndarray) -> None:
        sample = self._screen(point.tolist())
        color = chroma_warp_color(point)
        color /= color.max()
        fill = QColor.fromRgbF(*np.clip(color, 0.0, 1.0))
        painter.setBrush(fill)
        painter.setPen(QPen(QColor(0, 0, 0, 210), 3))
        painter.drawEllipse(sample, 5.0, 5.0)
        painter.setPen(QPen(QColor("#f5f7fa"), 1.4))
        painter.drawEllipse(sample, 5.0, 5.0)

    def cancel_gesture(self) -> None:
        if self._parameter_drag is not None:
            key, original, _ = self._parameter_drag
            if self._dragging:
                self.set_parameter(key, original)
            self._parameter_drag = None
        super().cancel_gesture()

    def _paint_neutral(self, painter: QPainter) -> None:
        if not self.protect_neutral:
            return
        point = self._screen(self.white_point)
        painter.setPen(QPen(QColor(C.TEXT_SECONDARY), 1))
        painter.drawLine(point - QPointF(3, 3), point + QPointF(3, 3))
        painter.drawLine(point - QPointF(3, -3), point + QPointF(3, -3))

    def _paint_range(
        self, painter: QPainter, center: list[float], chroma_range: float,
        *, end: list[float] | None = None, pin: bool = False,
    ) -> None:
        if pin and self._drawing:
            return
        if self._drawing and self._drawing_moved and self._parameter_drag is not None:
            if self._parameter_drag[0] == "chroma_range":
                chroma_range = self._parameter_value
        pen = QPen(QColor(248, 250, 255, 225), 1.1)
        pen.setDashPattern([1.6, 2.5])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolygon(self._range_outline(center, chroma_range, end=end))

    def _range_outline(
        self, center: list[float], chroma_range: float, *, end: list[float] | None = None,
    ) -> QPolygonF:
        source, target = self.display_positions(np.asarray([center, end if end is not None else center]))
        offset = target - source
        distance = float(np.linalg.norm(offset))
        radius = self._range_fraction(chroma_range)
        # Match the white scope feedback: long strokes expand across the
        # color neighborhood and extend slightly more along the drag axis.
        transform = np.diag(radius * np.array((0.87, 0.41)) + 0.55 * distance)
        if distance:
            transform += 0.02 / distance * np.outer(offset, offset)
        outline = source + 0.72 * offset + _CONTOUR @ transform
        rect = self.plot_rect()
        return QPolygonF([QPointF(rect.left() + x * rect.width(), rect.top() + y * rect.height())
                          for x, y in outline])

    @staticmethod
    def _range_fraction(chroma_range: float) -> float:
        return min(0.5, 0.012 + 2.1 * chroma_range)

    def _inside_range(self, distance: QPointF, chroma_range: float) -> bool:
        rect = self.plot_rect()
        return ((distance.x() / rect.width()) ** 2 + (distance.y() / rect.height()) ** 2
                < self._range_fraction(chroma_range) ** 2)

    def _begin_pin(self, point: list[float]) -> None:
        tool = self.tool
        self.tool = "add_pin"
        try:
            self._begin_plot_edit(point)
        finally:
            self.tool = tool

    def _begin_plot_edit(self, point: list[float]) -> None:
        limit = len(self.pins) >= MAX_PINS if self.tool == "add_pin" else len(self.strokes) >= MAX_STROKES
        if limit:
            self.limit_reached.emit()
            return
        super()._begin_plot_edit(point)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self.read_only:
            if event.button() == Qt.MouseButton.LeftButton:
                hit = self._hit(event.position())
                self.selected = hit[0] if hit is not None else None
                self.selection_changed.emit()
                self.update()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.plot_rect().contains(event.position()):
            modifiers = event.modifiers()
            hit = self._hit(event.position())
            if modifiers & Qt.KeyboardModifier.ShiftModifier and hit is not None:
                self.selected = hit[0]
                self.delete_selection()
                return
            if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
                self._begin_pin(self._normalized(event.position()))
                return
            if self.tool == "stroke" and hit is not None and hit[1] != "influence":
                self.setFocus()
                self._begin_selection(event.position(), self._normalized(event.position()))
                return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self.read_only:
            super().keyPressEvent(event)

    def set_parameter(self, key: str, value: float | str) -> None:
        # These values belong to a single correction. The Warper canvas
        # shares its tonal limits across all strokes.
        if self.selected is None:
            return
        kind, index = self.selected
        item = (self.strokes if kind == "stroke" else self.pins)[index]
        item["parameters"][key] = value
        self.update()

    def begin_viewer_drag(self, point: np.ndarray, *, pin: bool = False) -> QPointF | None:
        screen = self._screen(np.clip(point, 0.0, 1.0).tolist())
        alt = bool(self.viewer_modifiers & Qt.KeyboardModifier.AltModifier)
        control_key = (Qt.KeyboardModifier.MetaModifier if sys.platform == "darwin"
                       else Qt.KeyboardModifier.ControlModifier)
        control = bool(self.viewer_modifiers & control_key)
        if not pin and self.tool != "add_pin" and (not alt or control):
            hit = self._hit(screen)
            if hit is not None and hit[0][0] == "stroke":
                _, index = hit[0]
                self.selected = hit[0]
                self._dragging = True
                self._drag_handle = "end"
                self._press_point = self._normalized(screen)
                self._stroke_backup = [list(p) for p in self.strokes[index]["path"]]
                if control:
                    key = "chroma_range" if alt else "exposure"
                    self._parameter_drag = key, float(self.strokes[index]["parameters"][key]), screen
                self.selection_changed.emit()
                self.update()
                return screen
        tool = self.tool
        if tool == "select":
            self.tool = "stroke"
        try:
            start = super().begin_viewer_drag(point, pin=pin)
        finally:
            self.tool = tool
        if start is not None and self._drawing and control:
            key = "chroma_range" if alt else "exposure"
            self._parameter_drag = key, float(self.parameters[key]), start
        return start

    def _has_stroke_draft(self) -> bool:
        return super()._has_stroke_draft() or (
            self._parameter_drag is not None and bool(self._drawing) and self._drawing_moved)

    def _stroke_from_points(self, points: list[list[float]]) -> dict[str, Any]:
        stroke = super()._stroke_from_points(points)
        stroke["mode"] = "stroke"
        if self._parameter_drag is not None and self._drawing_moved:
            key, _, _ = self._parameter_drag
            stroke["parameters"][key] = self._parameter_value
        return stroke

    def move_viewer_drag(self, position: QPointF, *, finish: bool = False) -> None:
        if self._parameter_drag is None:
            if self._drawing and (position - self._screen(self._drawing[-1])).manhattanLength() < 1e-6:
                if finish:
                    self._finish_plot_edit()
                return
            super().move_viewer_drag(position, finish=finish)
            return
        key, original, origin = self._parameter_drag
        delta = (origin.y() - position.y()) / _REFERENCE_SCOPE_PIXELS
        low, high, scale = (0.002, 0.2, 0.4) if key == "chroma_range" else (-8.0, 8.0, 8.0)
        self._parameter_value = max(low, min(high, original + delta * scale))
        changed = self._parameter_value != original
        if self._drawing:
            self._drawing_moved = changed
        else:
            self.set_parameter(key, self._parameter_value)
            self.selection_changed.emit()
        if changed or self._preview_open:
            if finish:
                self._preview_timer.stop()
                if changed and self._drawing:
                    self._finish_stroke(self._drawing)
                self._drawing.clear()
                self._drawing_moved = False
                self._drawing_id = None
                self._clear_drag()
                if changed:
                    self._publish_preview()
                self._close_preview("commit" if changed else "cancel")
            elif not self._preview_timer.isActive():
                self._preview_timer.start()
        elif finish:
            self._preview_timer.stop()
            self._drawing.clear()
            self._drawing_id = None
            self._clear_drag()
        if finish:
            self._parameter_drag = None
        self.update()


class XGradePanel(WarperPanel):
    """Share the viewer sampler and gesture lifecycle with Color Warper."""

    palette_title = "X Grade"
    _consume_viewer_hover = True
    notice_requested = pyqtSignal(str)

    def init_ui(self) -> None:
        self.setObjectName("xGradePanel")
        self.setMinimumWidth(365)
        self.mode = "chroma_warp"
        self._palette_hosted = True
        self._saved_grade = XGradeState()
        self._target_id: str | None = None
        self._working_supported = True
        self._warper_host: WarperPanel | None = None
        self._dropper_cursor: QCursor | None = None
        self._previous_viewer_cursor: tuple[bool, QCursor] | None = None

        self.surface = QWidget()
        self.surface.installEventFilter(self)
        self._host_layout = QVBoxLayout(self)
        self._host_layout.setContentsMargins(0, 0, 0, 0)
        self._host_layout.addWidget(self.surface)
        layout = QVBoxLayout(self.surface)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._standalone_header = header = create_panel_header()
        top = QHBoxLayout(header)
        top.setContentsMargins(8, 0, 3, 0)
        top.setSpacing(3)
        title = QLabel(I18N.tr("panel.x_grade", "X Grade"))
        title.setStyleSheet(SECTION_TITLE_STYLE)
        top.addWidget(title, 1)
        self.viewer_controls = QWidget()
        actions = QHBoxLayout(self.viewer_controls)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(2)
        self.picker_button = tool_button("picker", _tr("viewer_picker", "Pick and drag colors in the viewer"), True)
        self.picker_button.setToolTip(_tr(
            "viewer_help", "Drag to adjust color. Ctrl-drag for exposure, Ctrl+Alt-drag for width, Alt-drag to add."))
        style_header_button(self.picker_button, width=32)
        self.picker_button.toggled.connect(self._viewer_picker_toggled)
        actions.addWidget(self.picker_button)
        expand = tool_button("expand", I18N.tr("warper.expand", "Expand"))
        style_header_button(expand, width=32)
        expand.clicked.connect(self.toggle_expanded)
        top.addWidget(expand)
        reset = reset_button(_tr("reset", "Reset X Grade"), global_reset=True)
        reset.clicked.connect(self.on_reset)
        actions.addWidget(reset)
        options = tool_button("more", _tr("options", "Options"))
        style_header_button(options, width=32)
        menu = QMenu(options)
        self.grid_action = menu.addAction(_tr("grid", "Show Grid"))
        self.grid_action.setCheckable(True)
        self.density_action = menu.addAction(_tr("distribution", "Show Color Distribution"))
        self.density_action.setCheckable(True)
        self.density_action.setChecked(True)
        self.grid_action.toggled.connect(self._display_changed)
        self.density_action.toggled.connect(self._display_changed)
        options.setMenu(menu)
        options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        actions.addWidget(options)
        top.addWidget(self.viewer_controls)
        layout.addWidget(header)

        self._working_notice = QLabel(_tr("working_only", X_GRADE_WORKING_MESSAGE))
        self._working_notice.setWordWrap(True)
        self._working_notice.setContentsMargins(8, 4, 8, 4)
        self._working_notice.setStyleSheet(PARAMETER_LABEL_STYLE)
        self._working_notice.hide()
        layout.addWidget(self._working_notice)

        body = QHBoxLayout()
        body.setSpacing(0)
        self.chroma_warp = XGradeCanvas()
        self.chroma_warp.setToolTip(_tr("plot_help", "Drag a color. Ctrl/Command-click to protect it. Shift-click to remove an adjustment."))
        self.chroma_warp.show_grid = False
        self.chroma_warp.white_point = chroma_warp_position(np.ones(3)).tolist()
        self.canvases = {self.mode: self.chroma_warp}
        self.chroma_warp.edit_requested.connect(self._request_palette)
        self.chroma_warp.selection_changed.connect(self._sync_tools)
        self.chroma_warp.limit_reached.connect(lambda: self.notice_requested.emit(
            _tr("limit", "This corrector has reached the X Grade adjustment limit.")))
        body.addWidget(self.chroma_warp, 1)
        body.addWidget(create_panel_divider(Qt.Orientation.Vertical))

        sidebar = QWidget()
        sidebar.setObjectName("xGradeControls")
        sidebar.setStyleSheet(f"""
            QWidget#xGradeControls {{ background: {C.BG_PANEL}; }}
            QWidget#xGradeControls QLabel, QWidget#xGradeControls QCheckBox {{ color: {C.TEXT_SECONDARY}; }}
        """)
        self._build_controls(sidebar)
        scroll = QScrollArea()
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(170)
        scroll.viewport().setStyleSheet(f"background: {C.BG_PANEL};")
        scroll.setWidget(sidebar)
        body.addWidget(scroll)
        layout.addLayout(body, 1)
        self.expanded_placeholder = QPushButton(_tr("show_expanded", "Show X Grade"))
        self.expanded_placeholder.clicked.connect(self._raise_expanded)
        self.expanded_placeholder.hide()
        self._host_layout.addWidget(self.expanded_placeholder)
        self._sync_tools()

    def _build_controls(self, sidebar: QWidget) -> None:
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        tools = QHBoxLayout()
        tools.setSpacing(3)
        group = QButtonGroup(sidebar)
        self._tool_buttons = group
        for tool, key, label in (("stroke", "draw", "Adjust Color"), ("add_pin", "pin", "Protect Color"),
                                 ("select", "select", "Select and Move")):
            button = tool_button(tool, _tr(key, label), True)
            button.setChecked(tool == "stroke")
            group.addButton(button)
            button.clicked.connect(lambda _checked, value=tool: self._tool_changed(value))
            tools.addWidget(button)
        self._delete = tool_button("delete", _tr("delete", "Delete Selected Adjustment"))
        self._delete.clicked.connect(self.chroma_warp.delete_selection)
        tools.addWidget(self._delete)
        layout.addLayout(tools)

        self._neutral = QCheckBox(_tr("neutral", "Protect Neutral Color"))
        self._neutral.setToolTip(_tr("neutral_help", "Keep neutrals free of color shifts. Exposure can still affect them."))
        self._neutral.setChecked(True)
        self._neutral.toggled.connect(self._protect_neutral)
        layout.addWidget(self._neutral)
        layout.addWidget(create_panel_divider(Qt.Orientation.Horizontal))

        self._selection_label = QLabel()
        self._selection_label.setStyleSheet(SECTION_TITLE_STYLE)
        layout.addWidget(self._selection_label)
        self._values: dict[str, DragValue] = {}
        self._sliders: dict[str, QSlider] = {}
        self._rows: dict[str, QWidget] = {}
        for key, title, low, high, default in _NUMERIC_CONTROLS:
            row = QWidget()
            field = QHBoxLayout(row)
            field.setContentsMargins(0, 0, 0, 0)
            label = QLabel(_tr(key, title))
            label.setStyleSheet(PARAMETER_LABEL_STYLE)
            field.addWidget(label, 1)
            control = DragValue("", low, high, default, 3 if key == "width" else 2, compact=True)
            control.setAccessibleName(label.text())
            control.setFixedSize(58, PARAMETER_ROW_HEIGHT)
            control.valueChanged.connect(lambda value, name=key: self._set_value(name, value))
            control.interaction_started.connect(self._begin_palette_value)
            control.interaction_ended.connect(self._end_palette_value)
            field.addWidget(control)
            control.installEventFilter(self)
            self._values[key], self._rows[key] = control, row
            layout.addWidget(row)
            if key in _SLIDER_LIMITS:
                slider_low, slider_high, slider_width = _SLIDER_LIMITS[key]
                slider = QSlider(Qt.Orientation.Horizontal)
                slider.setRange(round(slider_low * 1000), round(slider_high * 1000))
                slider.setFixedSize(slider_width, 13)
                slider.setAccessibleName(label.text())
                slider.installEventFilter(self)
                slider.sliderPressed.connect(self._begin_palette_value)
                slider.sliderReleased.connect(self._end_palette_value)
                slider.valueChanged.connect(lambda value, name=key: self._move_slider(name, value))
                self._sliders[key] = slider
                layout.addWidget(slider, alignment=Qt.AlignmentFlag.AlignRight)

        self._zone = ChevronComboBox()
        self._zone.setAccessibleName(_tr("zone", "Exposure Zone"))
        for value, title in (("master", "Master"), ("dim", "Dim"), ("light", "Light")):
            self._zone.addItem(_tr(value, title), value)
        self._zone.currentIndexChanged.connect(self._set_zone)
        layout.insertWidget(4, self._zone)
        layout.addStretch(1)

    def _plot_positions(self, rgb: np.ndarray, encoded: bool) -> np.ndarray:
        return warper_plot_positions(rgb, "chroma_warp", encoded_input=encoded)

    def embed_in_warper(self, host: WarperPanel) -> QWidget:
        self._warper_host = host
        self._standalone_header.hide()
        return self.viewer_controls

    def _viewer_surface_visible(self) -> bool:
        return super()._viewer_surface_visible() and (
            self._warper_host is None or self._warper_host._viewer_surface_visible())

    def arm_viewer_picker(self) -> None:
        if self._working_supported and self._viewer_tool_visible() and self._viewer_viewport is not None:
            self.picker_button.setChecked(True)

    def suspend_viewer_picker(self) -> None:
        self._cancel_numeric_edit()
        self._suspend_viewer_tool()
        self.chroma_warp.cancel_gesture()

    def _set_picker_cursor(self, active: bool) -> None:
        viewport = self._viewer_viewport
        if viewport is None:
            self._previous_viewer_cursor = None
            return
        if active:
            if self._dropper_cursor is None:
                from src.shared.color.color_picker import create_dropper_cursor

                self._dropper_cursor = create_dropper_cursor()
            if self._previous_viewer_cursor is None:
                self._previous_viewer_cursor = (viewport.testAttribute(Qt.WidgetAttribute.WA_SetCursor),
                                                viewport.cursor())
            viewport.setCursor(self._dropper_cursor)
        elif self._previous_viewer_cursor is not None:
            had_cursor, previous = self._previous_viewer_cursor
            self._previous_viewer_cursor = None
            if viewport.cursor() == self._dropper_cursor:
                if had_cursor:
                    viewport.setCursor(previous)
                else:
                    viewport.unsetCursor()

    def request_viewer_input(self, node_id: str, source: tuple, *, revision: tuple = ()) -> tuple | None:
        if not self._working_supported:
            return None
        return super().request_viewer_input(node_id, source, revision=revision)

    def accept_viewer_input(self, sample: tuple) -> None:
        if not self._working_supported:
            return
        request, rect, encoded, rgb = sample
        if len(request) == 4 and request[-1] == "x_grade":
            super().accept_viewer_input((request[:3], rect, encoded, rgb))

    def _refresh_density(self) -> None:
        image = None
        if self._viewer_input is not None and self.density_action.isChecked():
            _, encoded, rgb = self._viewer_input
            points = self.chroma_warp.display_positions(self._plot_positions(rgb, encoded)[:, 0])
            pixels = plot_density_rgba(points)
            height, width = pixels.shape[:2]
            image = QImage(pixels.data, width, height, width * 4, QImage.Format.Format_RGBA8888)
        self.chroma_warp.set_density_image(image)

    def _viewer_mouse(self, event: QMouseEvent) -> bool:
        if not self._working_supported:
            return False
        self.chroma_warp.viewer_modifiers = event.modifiers()
        return super()._viewer_mouse(event)

    def set_working_supported(self, supported: bool) -> None:
        if self._working_supported == supported:
            return
        if not supported:
            self._cancel_numeric_edit()
            self._cancel_viewer_drag()
            self.chroma_warp.cancel_gesture()
            if self._pending_palette_edit:
                self._request_palette("cancel")
                self._load_grade(self._saved_grade, self._target_id, force=True)
            self.picker_button.setChecked(False)
            self._clear_viewer_input()
        self._working_supported = supported
        self.chroma_warp.read_only = not supported
        self.picker_button.setEnabled(supported)
        self._neutral.setEnabled(supported)
        for button in self._tool_buttons.buttons():
            button.setEnabled(supported)
        self._working_notice.setVisible(not supported)
        self._sync_tools()

    def _tool_changed(self, tool: str) -> None:
        self.chroma_warp.cancel_gesture()
        self.chroma_warp.tool = tool
        self.chroma_warp.setCursor(Qt.CursorShape.ArrowCursor if tool == "select" else Qt.CursorShape.CrossCursor)

    def _selected_item(self) -> tuple[dict[str, Any] | None, bool]:
        canvas = self.chroma_warp
        if canvas.selected is None:
            return None, False
        kind, index = canvas.selected
        pin = kind == "pin"
        return (canvas.pins if pin else canvas.strokes)[index], pin

    def _sync_tools(self) -> None:
        item, pin = self._selected_item()
        values = item["parameters"] if item is not None else self.chroma_warp.parameters
        zone = str(values.get("x_zone", "master"))
        self._delete.setEnabled(self._working_supported and item is not None)
        self._selection_label.setText(
            _tr("pin_selected", "Protected Color") if pin else _tr("correction", "Correction"))
        self._zone.setEnabled(self._working_supported and item is not None and not pin)
        self._zone.blockSignals(True)
        self._zone.setCurrentIndex(max(0, self._zone.findData(zone)))
        self._zone.blockSignals(False)
        for key, control in self._values.items():
            self._rows[key].setVisible(key not in {"tonal_pivot", "falloff"} or zone != "master")
            control.setEnabled(self._working_supported and item is not None and (not pin or key == "width"))
            value = float(values.get("x_falloff", 0.5) if key == "falloff" else
                          float(values["chroma_range"]) * _WIDTH_SCALE if key == "width" else values[key])
            control.blockSignals(True)
            control.setValue(value)
            control.blockSignals(False)
            if (slider := self._sliders.get(key)) is not None:
                slider.setEnabled(control.isEnabled())
                slider.blockSignals(True)
                slider.setValue(round(value * 1000))
                slider.blockSignals(False)

    def _move_slider(self, key: str, code: int) -> None:
        self._values[key].setValue(code / 1000.0)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if self._numeric_palette_edit and (
            event.type() == QEvent.Type.WindowDeactivate
            or (watched is not self._viewer_viewport and event.type() == QEvent.Type.UngrabMouse)
        ):
            self._cancel_numeric_edit()
        return super().eventFilter(watched, event)

    def hideEvent(self, event: QHideEvent) -> None:
        self.suspend_viewer_picker()
        super().hideEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._warper_host is not None:
            QTimer.singleShot(0, self.arm_viewer_picker)

    def _cancel_numeric_edit(self) -> None:
        if self._numeric_palette_edit:
            self._numeric_palette_edit = False
            self._request_palette("cancel")
            self._load_grade(self._saved_grade, self._target_id, force=True)

    def _set_value(self, key: str, value: float) -> None:
        if self._updating:
            return
        if key == "width":
            key, value = "chroma_range", value / _WIDTH_SCALE
        elif key == "falloff":
            key = "x_falloff"
        self.chroma_warp.set_parameter(key, value)
        self._sync_tools()
        self._request_palette("update" if self._numeric_palette_edit else "atomic")

    def _set_zone(self, _index: int) -> None:
        if self._updating:
            return
        self.chroma_warp.set_parameter("x_zone", self._zone.currentData())
        self._sync_tools()
        self._request_palette("atomic")

    def _protect_neutral(self, enabled: bool) -> None:
        self.chroma_warp.protect_neutral = enabled
        self.chroma_warp.update()
        self._request_palette("atomic")

    def _snapshot_grade(self, preview: bool) -> XGradeState:
        canvas = self.chroma_warp.snapshot(preview=preview)
        old_strokes = {stroke.stroke_id: stroke for stroke in self._saved_grade.strokes}
        old_pins = {pin.pin_id: pin for pin in self._saved_grade.pin_points}

        def params(item: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
            values = dict(item["parameters"])
            values["pivot"] = values.pop("tonal_pivot")
            values.pop("stroke_mode", None)
            zone = values.get("x_zone", "master")
            falloff = float(values.get("x_falloff", 0.5))
            values["tonal_low"] = falloff if zone == "light" else 1.0
            values["tonal_high"] = falloff if zone == "dim" else 1.0
            return {**previous, **values}

        strokes = []
        for item in canvas["strokes"]:
            path = tuple(tuple(point) for point in item["path"])
            source, target = (tuple(point) for point in chroma_warp_color(np.asarray((path[0], path[-1]))))
            old = old_strokes.get(item["id"])
            strokes.append(WarperStroke(item["id"], "normal", source, target,
                                        params(item, old.params if old else {}), path,
                                        _extra_fields=old._extra_fields if old else {}))
        pins = []
        for item in canvas["pins"]:
            old = old_pins.get(item["id"])
            pins.append(WarperPinPoint(item["id"], tuple(item["position"]), params(item, old.params if old else {}),
                                       _extra_fields=old._extra_fields if old else {}))
        return replace(self._saved_grade, strokes=tuple(strokes), pin_points=tuple(pins),
                       protect_neutral=self.chroma_warp.protect_neutral)

    def _request_palette(self, phase: str, *, reset: bool = False) -> None:
        if self._updating or (not self._working_supported and not reset and phase != "cancel"):
            return
        payload: dict[str, Any] = {"phase": phase}
        if reset:
            payload["reset"] = True
        if phase in {"update", "atomic"}:
            state = self._snapshot_grade(preview=phase == "update")
            payload["state"] = state.to_dict()
            self._pending_palette_edit = phase == "update"
        elif not self._pending_palette_edit:
            return
        else:
            self._pending_palette_edit = False
        self.command_requested.emit({"mode": "x_grade", "action": "x_grade_edit", "payload": payload})

    def load_layer_state(self, layer: Layer, *, force: bool = False) -> None:
        state = layer.warper_state.x_grade if layer.warper_state is not None else None
        self._load_grade(state, layer.id, force=force)

    def clear_target(self) -> None:
        self._load_grade(None, None, force=True)

    def _load_grade(self, state: XGradeState | None, target_id: str | None, *, force: bool = False) -> None:
        target_changed = target_id != self._target_id
        if (not force and not target_changed and not self._pending_palette_edit and not self._numeric_palette_edit
                and (state or XGradeState()).to_dict() == self._saved_grade.to_dict()):
            return
        self._updating = True
        try:
            if target_changed:
                self._clear_viewer_input()
            self._target_id = target_id
            self._pending_palette_edit = self._numeric_palette_edit = False
            canvas = self.chroma_warp
            item, pin = self._selected_item()
            selected_id = item["id"] if item is not None and not target_changed else None
            canvas.reset()
            self._saved_grade = state or XGradeState()
            canvas.protect_neutral = self._saved_grade.protect_neutral
            self._neutral.setChecked(canvas.protect_neutral)

            def controls(values: dict[str, Any]) -> dict[str, Any]:
                return {**canvas.parameters, **values, "tonal_pivot": values.get("pivot", 0.5)}

            for stroke in self._saved_grade.strokes:
                path = stroke.path or chroma_warp_position(np.asarray((stroke.source_rgb, stroke.target_rgb)))
                canvas.strokes.append({"id": stroke.stroke_id, "path": [list(point) for point in path],
                                       "mode": "stroke", "parameters": controls(stroke.params)})
            for point in self._saved_grade.pin_points:
                canvas.pins.append({"id": point.pin_id, "position": list(point.position),
                                    "parameters": controls(point.params)})
            items = canvas.pins if pin else canvas.strokes
            for index, candidate in enumerate(items):
                if candidate["id"] == selected_id:
                    canvas.selected = ("pin" if pin else "stroke", index)
                    break
            self._sync_tools()
            canvas.update()
        finally:
            self._updating = False

    def cancel_edit(self) -> None:
        if self._pending_palette_edit or self._numeric_palette_edit:
            self._load_grade(self._saved_grade, self._target_id, force=True)
        self._cancel_viewer_drag()
        self.chroma_warp.cancel_gesture()

    def on_reset(self) -> None:
        self.chroma_warp.cancel_gesture()
        self._updating = True
        try:
            self.chroma_warp.reset()
            self._neutral.setChecked(True)
        finally:
            self._updating = False
        self._request_palette("atomic", reset=True)
