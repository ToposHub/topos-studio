"""Video Power Window palette; edits use MaskPanel's existing commit signals.

The five template rows are always present, as in Resolve. Shape-specific
softness controls edit real model fields; irrelevant controls remain disabled.
Image-specific segmentation and brush tools live in MaskPanel.
"""
from __future__ import annotations

import copy
import math
from typing import Any

from PyQt6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QShortcut, QWheelEvent, QMouseEvent
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.assets.icon_manager import icon_reset

WINDOW_TYPES = ("rect", "circle", "polygon", "curve", "gradient")
WINDOW_NAMES = {"rect": "Linear", "circle": "Circle", "polygon": "Polygon", "curve": "Curve", "gradient": "Gradient"}


def window_icon(kind: str, *, add: bool = False, muted: bool = False) -> QIcon:
    """Draw resolution-independent, neutral shape glyphs without font symbols."""
    pix = QPixmap(32, 32)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor("#87898a" if muted else "#f4f4f4")
    painter.setPen(QPen(color, 2.5))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if kind == "rect":
        painter.drawRect(QRectF(8, 8, 16, 16))
    elif kind == "circle":
        painter.drawEllipse(QRectF(8, 8, 16, 16))
    elif kind == "polygon":
        painter.drawLine(QPointF(8, 24), QPointF(24, 8))
    elif kind == "curve":
        path = QPainterPath(QPointF(10, 23))
        path.lineTo(14, 11)
        path.lineTo(23, 8)
        path.lineTo(21, 17)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(10, 23), QPointF(18, 15))
        painter.drawEllipse(QRectF(16.5, 12.5, 3, 3))
    elif kind == "gradient":
        gradient = QLinearGradient(9, 23, 23, 9)
        gradient.setColorAt(0, QColor("#515354"))
        gradient.setColorAt(1, color)
        painter.fillRect(QRectF(7, 7, 18, 18), gradient)
    elif kind == "invert":
        painter.setPen(QPen(color, 1))
        painter.drawRect(QRectF(7, 7, 18, 18))
        painter.setBrush(color)
        painter.drawEllipse(QRectF(11, 11, 10, 10))
    elif kind == "visible":
        painter.setPen(QPen(color, 1))
        for y in range(8, 25, 3):
            painter.drawLine(QPointF(8, y), QPointF(24, y))
        painter.drawEllipse(QRectF(8, 8, 16, 16))
    elif kind == "reset":
        painter.setPen(QPen(color, 1.3))
        painter.drawArc(QRectF(8, 8, 16, 16), 40 * 16, 300 * 16)
        painter.drawLine(QPointF(9, 8), QPointF(9, 14))
        painter.drawLine(QPointF(9, 14), QPointF(15, 14))
    if add:
        painter.setPen(QPen(color, 1.4))
        painter.drawLine(QPointF(5, 3), QPointF(5, 9))
        painter.drawLine(QPointF(2, 6), QPointF(8, 6))
    painter.end()
    icon = QIcon(pix)
    disabled = QPixmap(32, 32)
    disabled.fill(Qt.GlobalColor.transparent)
    painter = QPainter(disabled)
    painter.setOpacity(0.22)
    painter.drawPixmap(0, 0, pix)
    painter.end()
    icon.addPixmap(disabled, QIcon.Mode.Disabled)
    return icon


STYLE = """
PowerWindowPanel { background: #242429; }
PowerWindowPanel QWidget#window_title { background: #202024; border-bottom: 1px solid #0d0d0f; }
PowerWindowPanel QLabel { background: transparent; color: #bebfc1; font-size: 11px; }
PowerWindowPanel QLabel:disabled { color: #56585a; }
PowerWindowPanel QLabel#window_heading { color: #eeeeef; font-weight: 600; }
PowerWindowPanel QWidget#window_parameters { background: #2b2d31; border-left: 1px solid #0d0d0f; }
PowerWindowPanel QWidget#window_tools { background: #2b2d31; border-bottom: 1px solid #0d0d0f; }
PowerWindowPanel QScrollArea { border: 0; background: #242429; }
PowerWindowPanel QWidget#window_rows { background: #242429; }
PowerWindowPanel QFrame#window_row { background: #242429; border-bottom: 1px solid #101014; }
PowerWindowPanel QFrame#window_row[alternate="true"] { background: #202024; }
PowerWindowPanel QFrame#window_row[selected="true"] { background: #29292e; }
PowerWindowPanel QPushButton { color: #97999a; background: transparent; border: 0; padding: 0; font-size: 11px; }
PowerWindowPanel QPushButton:hover { background: #34363a; }
PowerWindowPanel QPushButton#window_shape { background: #202024; border: 1px solid #151518; border-radius: 1px; }
PowerWindowPanel QPushButton#window_shape:checked { border: 1px solid #b84343; }
PowerWindowPanel QPushButton#window_delete { border: 1px solid #414449; border-radius: 11px; }
PowerWindowPanel QPushButton:disabled { color: #535559; }
PowerWindowPanel QPushButton#window_toggle:checked { background: #45464c; }
PowerWindowPanel QScrollBar:vertical { background: #242429; width: 7px; }
PowerWindowPanel QScrollBar::handle:vertical { background: #626366; min-height: 20px; }
PowerWindowPanel QScrollBar::add-line:vertical, PowerWindowPanel QScrollBar::sub-line:vertical { height: 0; }
"""


class WindowValue(DragValue):
    """Use the palette's compact field appearance through enable/disable changes."""

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            self._apply_palette_style()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        if not self.isEditing():
            self._apply_palette_style()

    def _end_edit(self) -> None:
        super()._end_edit()
        self._apply_palette_style()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        step = self._single_step
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            step *= 10.0
        elif event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            step *= 0.1
        delta = event.angleDelta().y()
        candidate = self.value() + (step if delta > 0 else -step if delta < 0 else 0.0)
        candidate = max(self._min, min(self._max, candidate))
        if candidate != self.value():
            self.interaction_started.emit()
            self.setValue(candidate)
            self.interaction_ended.emit()
        event.accept()

    def _apply_palette_style(self) -> None:
        color = "#b6b8b9" if self.isEnabled() else "#56585a"
        self._value_label.setStyleSheet(
            f"QLabel {{ background:#242629; color:{color}; border:1px solid #17181a; "
            "border-radius:1px; font-size:11px; font-weight:normal; padding:0; }")


class PowerWindowPanel(QWidget):
    """Video palette with stable model identities and signal-only edits."""

    shape_added = pyqtSignal(str)
    shape_removed = pyqtSignal(str)
    shape_selected = pyqtSignal(str)
    shape_updated = pyqtSignal(str, dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    reset_requested = pyqtSignal()
    duplicate_requested = pyqtSignal(str)
    tracker_requested = pyqtSignal()
    copy_to_layer_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PowerWindowPanel")
        self.setStyleSheet(STYLE)
        self._shapes: dict[str, Any] = {}
        self._selected_id: str | None = None
        self._updating = False
        self._image_aspect = 1.0
        self._geometry: dict[str, Any] = {}
        self._visibility_overrides: dict[str, bool] = {}
        self._row_buttons: dict[str, QPushButton] = {}
        self.controls: dict[str, DragValue] = {}
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        title = QWidget(self)
        title.setObjectName("window_title")
        title.setFixedHeight(31)
        title_layout = QHBoxLayout(title)
        title_layout.setContentsMargins(10, 0, 0, 0)
        label = QLabel(I18N.tr("window.palette_title", "Window"), title)
        label.setObjectName("window_heading")
        title_layout.addWidget(label)
        title_layout.addStretch(1)
        self.reset_button = QPushButton(title)
        self.reset_button.setIcon(icon_reset())
        self.reset_button.setFixedSize(34, 29)
        self.reset_button.setToolTip(I18N.tr("window.reset_all", "Reset all windows"))
        self.reset_button.clicked.connect(self.reset_requested.emit)
        title_layout.addWidget(self.reset_button)
        self.menu_button = QPushButton("⋯", title)
        self.menu_button.setFixedSize(34, 29)
        self.menu_button.clicked.connect(self._show_menu)
        title_layout.addWidget(self.menu_button)
        root.addWidget(title)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        left = QWidget(self)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        tools = QWidget(left)
        tools.setObjectName("window_tools")
        tools.setFixedHeight(47)
        tool_layout = QHBoxLayout(tools)
        tool_layout.setContentsMargins(15, 0, 20, 0)
        tool_layout.setSpacing(9)
        self.add_buttons: dict[str, QPushButton] = {}
        for kind in WINDOW_TYPES:
            button = QPushButton(tools)
            button.setIcon(window_icon(kind, add=True, muted=True))
            button.setIconSize(QSize(24, 24))
            button.setFixedSize(40, 32)
            button.setAccessibleName(WINDOW_NAMES[kind])
            button.setToolTip(I18N.tr(f"window.add_{kind}", f"Add {WINDOW_NAMES[kind]} window"))
            button.clicked.connect(lambda _checked=False, k=kind: self.shape_added.emit(k))
            self.add_buttons[kind] = button
            tool_layout.addWidget(button)
        tool_layout.addStretch(1)
        self.delete_button = QPushButton(I18N.tr("window.delete", "Delete"), tools)
        self.delete_button.setObjectName("window_delete")
        self.delete_button.setFixedSize(72, 22)
        self.delete_button.clicked.connect(self._delete_selected)
        tool_layout.addWidget(self.delete_button)
        left_layout.addWidget(tools)
        self.scroll = QScrollArea(left)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rows_host = QWidget(self.scroll)
        self.rows_host.setObjectName("window_rows")
        self.rows_layout = QVBoxLayout(self.rows_host)
        self.rows_layout.setContentsMargins(0, 0, 0, 0)
        self.rows_layout.setSpacing(0)
        self.scroll.setWidget(self.rows_host)
        left_layout.addWidget(self.scroll, 1)
        body.addWidget(left, 605)
        self.parameters = self._build_parameters()
        body.addWidget(self.parameters, 395)
        root.addLayout(body, 1)
        self.set_shapes([])
        self._delete_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.rows_host)
        self._delete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._delete_shortcut.activated.connect(self._delete_selected)

    def _build_parameters(self) -> QWidget:
        panel = QWidget(self)
        panel.setObjectName("window_parameters")
        panel.setMinimumWidth(280)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 16, 7, 12)
        layout.setSpacing(8)
        heading = QLabel(I18N.tr("window.transform", "Transform"), panel)
        heading.setObjectName("window_heading")
        layout.addWidget(heading)
        layout.addSpacing(12)
        grid = QGridLayout()
        grid.setContentsMargins(7, 0, 20, 0)
        grid.setHorizontalSpacing(11)
        grid.setVerticalSpacing(8)
        definitions = (
            ("size", "Size", 0.01, 1000.0, 50.0),
            ("aspect", "Aspect", 0.01, 1000.0, 50.0),
            ("pan", "Pan", -500.0, 500.0, 50.0),
            ("tilt", "Tilt", -500.0, 500.0, 50.0),
            ("rotate", "Rotate", -360.0, 360.0, 0.0),
            ("opacity", "Opacity", 0.0, 100.0, 100.0),
        )
        for index, (key, label, minimum, maximum, default) in enumerate(definitions):
            self._add_control(grid, index // 2, (index % 2) * 2, key, label, minimum, maximum, default)
        layout.addLayout(grid)
        line = QFrame(panel)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background: #101014; border:0;")
        line.setFixedHeight(1)
        layout.addWidget(line)
        layout.addSpacing(7)
        heading = QLabel(I18N.tr("window.softness", "Softness"), panel)
        heading.setObjectName("window_heading")
        layout.addWidget(heading)
        layout.addSpacing(12)
        grid = QGridLayout()
        grid.setContentsMargins(7, 0, 20, 0)
        grid.setHorizontalSpacing(11)
        grid.setVerticalSpacing(8)
        for index, (key, label, default) in enumerate((
            ("soft1", "Soft 1", 4.13), ("soft2", "Soft 2", 50.0),
            ("soft3", "Soft 3", 50.0), ("soft4", "Soft 4", 50.0),
            ("inside", "Inside", 50.0), ("outside", "Outside", 50.0),
        )):
            self._add_control(grid, index // 2, (index % 2) * 2, key, label, 0.0, 100.0, default)
        layout.addLayout(grid)
        layout.addStretch(1)
        return panel

    def _add_control(self, grid: QGridLayout, row: int, col: int, key: str,
                     label: str, minimum: float, maximum: float, default: float) -> None:
        text = QLabel(I18N.tr(f"window.{key}", label), self.parameters if hasattr(self, "parameters") else self)
        text.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        value = WindowValue("", minimum, maximum, default, decimals=2, compact=True)
        value.setFixedSize(52, 20)
        value._value_label.setFixedHeight(20)
        value._apply_palette_style()
        value._edit.setFixedHeight(20)
        value.setAccessibleName(label)
        value.valueChanged.connect(lambda _v, k=key: self._edit_control(k))
        value.interaction_started.connect(self.interaction_started.emit)
        value.interaction_ended.connect(self.interaction_ended.emit)
        value.setProperty("parameterLabel", text)
        self.controls[key] = value
        grid.addWidget(text, row, col)
        grid.addWidget(value, row, col + 1)

    @staticmethod
    def _kind(shape: Any) -> str:
        kind = getattr(shape, "type", "group")
        if kind == "curve":
            points = getattr(shape, "points", [])
            if getattr(shape, "name", "") == "Polygon" or (points and all(p.get("type") == "corner" for p in points)):
                return "polygon"
        return kind

    def set_shapes(self, shapes: list[Any]) -> None:
        """Rebuild rows from model objects without changing their selection or data."""
        self._visibility_overrides.clear()
        flattened: list[Any] = []

        def collect(items: list[Any]) -> None:
            for item in items:
                if hasattr(item, "children"):
                    collect(item.children)
                else:
                    flattened.append(item)

        collect(shapes)
        self._shapes = {str(s.id): s for s in flattened}
        if self._selected_id not in self._shapes:
            self._selected_id = None
        while self.rows_layout.count():
            item = self.rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._row_buttons.clear()
        index = 0
        for kind in WINDOW_TYPES:
            matching = [s for s in flattened if self._kind(s) == kind]
            for shape in matching or [None]:
                self._add_row(kind, shape, index)
                index += 1
        for shape in flattened:
            if self._kind(shape) not in WINDOW_TYPES:
                self._add_row(self._kind(shape), shape, index)
                index += 1
        self.rows_layout.addStretch(1)
        if self._selected_id:
            self.select_shape(self._selected_id)
        else:
            self._refresh_controls()

    def _add_row(self, kind: str, shape: Any | None, index: int) -> None:
        row = QFrame(self.rows_host)
        row.setObjectName("window_row")
        row.setProperty("alternate", index % 2 == 1)
        row.setProperty("selected", bool(shape and shape.id == self._selected_id))
        row.setFixedHeight(54)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(23, 0, 16, 0)
        layout.setSpacing(17)
        button = QPushButton(row)
        button.setObjectName("window_shape")
        button.setFixedSize(46, 38)
        button.setIconSize(QSize(30, 30))
        button.setIcon(window_icon(kind))
        button.setCheckable(True)
        button.setChecked(bool(shape and shape.id == self._selected_id))
        button.setToolTip(str(getattr(shape, "name", None) or WINDOW_NAMES.get(kind, kind)))
        if shape:
            sid = str(shape.id)
            button.clicked.connect(lambda _checked=False, s=sid: self._select_from_click(s))
            self._row_buttons[sid] = button
            row.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            row.customContextMenuRequested.connect(lambda pos, w=row, s=sid: self._show_row_menu(s, w.mapToGlobal(pos)))
        else:
            button.clicked.connect(lambda _checked=False, k=kind: self.shape_added.emit(k))
        layout.addWidget(button)
        layout.addStretch(1)
        for action, attribute in (("invert", "invert"), ("visible", "visible")):
            toggle = QPushButton(row)
            toggle.setObjectName("window_toggle")
            toggle.setIcon(window_icon(action, muted=True))
            toggle.setIconSize(QSize(23, 23))
            toggle.setFixedSize(24, 24)
            toggle.setCheckable(True)
            toggle.setEnabled(shape is not None)
            toggle.setToolTip(I18N.tr(f"window.{action}", "Invert window" if action == "invert" else "Enable window"))
            if shape:
                toggle.setChecked(bool(getattr(shape, attribute, False)))
                toggle.toggled.connect(lambda checked, s=str(shape.id), a=attribute: self._emit_edit(s, {a: checked}))
            layout.addWidget(toggle)
        self.rows_layout.addWidget(row)

    def _select_from_click(self, shape_id: str) -> None:
        if self._selected_id == shape_id:
            self._emit_edit(shape_id, {"visible": not self._window_visible(shape_id)})
            return
        self.select_shape(shape_id)
        self.shape_selected.emit(shape_id)

    def _window_visible(self, shape_id: str) -> bool:
        return self._visibility_overrides.get(shape_id, bool(getattr(self._shapes.get(shape_id), "visible", False)))

    def select_shape(self, shape_id: str) -> None:
        """Programmatic selection is quiet, including a deferred list rebuild."""
        self._selected_id = shape_id
        for sid, button in self._row_buttons.items():
            button.setChecked(sid == shape_id and self._window_visible(sid))
            row = button.parentWidget()
            row.setProperty("selected", sid == shape_id)
            row.style().unpolish(row)
            row.style().polish(row)
        shape = self._shapes.get(shape_id)
        if shape is not None:
            self._geometry = {
                "center": list(shape.center), "size": list(shape.size),
                "rotation": float(shape.rotation),
                "points": copy.deepcopy(getattr(shape, "points", [])),
                "grad_points": copy.deepcopy(getattr(shape, "grad_points", [])),
                "edge_softness": copy.deepcopy(getattr(shape, "edge_softness", None)),
            }
        self._refresh_controls()

    def set_frame_aspect(self, aspect: float) -> None:
        """Compatibility entry point for source width divided by source height."""
        self.set_image_aspect(aspect)

    def set_image_aspect(self, aspect: float) -> None:
        """Source pixel width / height, for round windows on non-square viewers."""
        if math.isfinite(aspect) and aspect > 0:
            self._image_aspect = aspect
            self._refresh_controls()

    def set_geometry(self, geometry: dict[str, Any]) -> None:
        """Sync frame-evaluated geometry, including explicit Bezier/gradient points."""
        for field in ("center", "size", "rotation", "points", "grad_points", "edge_softness"):
            if field in geometry:
                self._geometry[field] = copy.deepcopy(geometry[field])
        if all(field in geometry for field in ("center", "size", "rotation")):
            self.set_transform_params(geometry["center"], geometry["size"], geometry["rotation"])

    def set_transform_params(self, center: list[float], size: list[float], rotation: float) -> None:
        self._geometry.update(center=list(center), size=list(size), rotation=float(rotation))
        shape = self._shapes.get(self._selected_id or "")
        is_rect = getattr(shape, "type", "") == "rect"
        base_size = 0.4 if is_rect else 0.15 if getattr(shape, "type", "") == "circle" else 0.3
        aspect_factor = 1.0 if is_rect else self._image_aspect
        self._set_values({"size": size[0] / base_size * 50.0,
                          "aspect": size[0] * aspect_factor / max(1e-6, size[1]) * 50.0,
                          "pan": center[0] * 100.0, "tilt": (1.0 - center[1]) * 100.0,
                          "rotate": rotation})

    def set_params(self, feather: float, opacity: float) -> None:
        shape = self._shapes.get(self._selected_id or "")
        kind = getattr(shape, "type", "")
        if kind == "rect":
            edges = getattr(shape, "edge_softness", None)
            values = list(edges) if edges is not None else [feather * 0.8] * 4
            self._set_values({**{f"soft{i + 1}": value for i, value in enumerate(values)},
                              "opacity": opacity * 100.0})
        else:
            soft = float(getattr(shape, "softness", 0.2)) * 100.0 if kind == "gradient" else feather
            self._set_values({"soft1": soft, "opacity": opacity * 100.0})

    def _set_values(self, values: dict[str, float]) -> None:
        self._updating = True
        try:
            for key, value in values.items():
                self.controls[key].setValue(value)
        finally:
            self._updating = False

    def _refresh_controls(self) -> None:
        shape = self._shapes.get(self._selected_id or "")
        valid = shape is not None
        editable = valid and self._window_visible(self._selected_id) and not bool(getattr(shape, "locked", False))
        kind = getattr(shape, "type", "")
        for key, value in self.controls.items():
            supported = key not in {"soft2", "soft3", "soft4", "inside", "outside"}
            if key in {"soft2", "soft3", "soft4"}:
                supported = kind == "rect"
            if key in {"inside", "outside"}:
                supported = kind == "curve" and bool(getattr(shape, "points", []))
            value.setEnabled(editable and supported)
            label = value.property("parameterLabel")
            if label is not None:
                label.setEnabled(editable and supported)
            if kind == "rect" and key in {"soft1", "soft2", "soft3", "soft4"}:
                edge_name = {"soft1": "Left", "soft2": "Right", "soft3": "Top", "soft4": "Bottom"}[key]
                value.setToolTip(I18N.tr(f"window.{key}_edge", f"{edge_name} edge softness (% of image width)"))
            elif not supported:
                value.setToolTip(I18N.tr("window.softness_unavailable", "Independent edge softness is not supported for this window"))
            else:
                value.setToolTip("")
        self.delete_button.setEnabled(valid)
        if valid:
            self.set_transform_params(list(shape.center), list(shape.size), shape.rotation)
            self.set_params(shape.feather, shape.opacity)
            points = getattr(shape, "points", [])
            for key, field in (("inside", "fi"), ("outside", "fo")):
                values = [float(p[field]) for p in points if field in p]
                fallback = float(shape.feather) / 2.0 if kind == "curve" else 50.0
                self._set_values({key: sum(values) / len(values) if values else fallback})
            if kind != "rect":
                self._set_values({key: 50.0 for key in ("soft2", "soft3", "soft4")})

    def _emit_edit(self, shape_id: str, values: dict[str, Any]) -> None:
        self.interaction_started.emit()
        if "visible" in values:
            self._visibility_overrides[shape_id] = bool(values["visible"])
            self.select_shape(shape_id)
        self.shape_updated.emit(shape_id, values)
        self.interaction_ended.emit()

    def _edit_control(self, key: str) -> None:
        if self._updating or not self._selected_id or self._selected_id not in self._shapes:
            return
        shape = self._shapes[self._selected_id]
        value = self.controls[key].value()
        if key == "opacity":
            params: dict[str, Any] = {"opacity": value / 100.0}
        elif key in {"soft1", "soft2", "soft3", "soft4"}:
            if shape.type == "rect":
                edges = self._geometry.get("edge_softness")
                edges = list(edges) if edges is not None else [float(shape.feather) * 0.8] * 4
                edges[int(key[-1]) - 1] = value
                self._geometry["edge_softness"] = edges
                params = {"edge_softness": edges}
            else:
                params = {"softness": value / 100.0} if shape.type == "gradient" else {"feather": value}
        elif key in {"inside", "outside"}:
            points = copy.deepcopy(self._geometry.get("points", shape.points))
            for point in points:
                point["fi" if key == "inside" else "fo"] = value
            self._geometry["points"] = points
            params = {"points": points}
        else:
            old = copy.deepcopy(self._geometry)
            center = list(old.get("center", shape.center))
            size = list(old.get("size", shape.size))
            rotation = float(old.get("rotation", shape.rotation))
            if key == "pan":
                center[0] = value / 100.0
            elif key == "tilt":
                center[1] = 1.0 - value / 100.0
            elif key == "size":
                base_size = 0.4 if shape.type == "rect" else 0.15 if shape.type == "circle" else 0.3
                ratio = value * base_size / 50.0 / max(1e-6, size[0])
                size = [size[0] * ratio, size[1] * ratio]
            elif key == "aspect":
                aspect_factor = 1.0 if shape.type == "rect" else self._image_aspect
                size[1] = size[0] * aspect_factor * 50.0 / max(0.01, value)
            elif key == "rotate":
                rotation = value
            params = {"center": center, "size": size, "rotation": rotation}
            self._transform_points(old, params)
            self._geometry.update(copy.deepcopy(params))
        self.shape_updated.emit(self._selected_id, params)

    def _transform_points(self, old: dict[str, Any], params: dict[str, Any]) -> None:
        """Apply numeric controls to curves/gradients, whose pixels use explicit points."""
        shape = self._shapes[self._selected_id]
        if shape.type not in {"curve", "gradient"}:
            return
        oc = old.get("center", shape.center)
        os = old.get("size", shape.size)
        center, size = params["center"], params["size"]
        # Rotation is retained as metadata and applied once by both viewer and
        # renderer. Bake only translation/scale into the explicit point geometry.

        def transform(point: list[float] | tuple[float, float]) -> list[float]:
            x = (point[0] - oc[0]) * size[0] / max(1e-6, os[0])
            y = (point[1] - oc[1]) * size[1] / max(1e-6, os[1])
            return [center[0] + x, center[1] + y]

        if shape.type == "curve":
            points = copy.deepcopy(old.get("points", shape.points))
            for point in points:
                for field in ("pos", "c1", "c2"):
                    if field in point:
                        point[field] = transform(point[field])
            params["points"] = points
        else:
            params["grad_points"] = [transform(p) for p in old.get("grad_points", shape.grad_points)]

    def _delete_selected(self) -> None:
        if self._selected_id in self._shapes:
            self.shape_removed.emit(self._selected_id)

    def _show_row_menu(self, shape_id: str, position: Any) -> None:
        self.select_shape(shape_id)
        self.shape_selected.emit(shape_id)
        menu = QMenu(self)
        duplicate = menu.addAction(I18N.tr("mask.duplicate_window", "Duplicate window"))
        track = menu.addAction(I18N.tr("mask.send_tracker", "Send to Tracker"))
        lock = menu.addAction(I18N.tr("window.lock", "Lock window"))
        lock.setCheckable(True)
        lock.setChecked(bool(getattr(self._shapes[shape_id], "locked", False)))
        chosen = menu.exec(position)
        if chosen is duplicate:
            self.duplicate_requested.emit(shape_id)
        elif chosen is track:
            self.tracker_requested.emit()
        elif chosen is lock:
            self._emit_edit(shape_id, {"locked": lock.isChecked()})

    def _show_menu(self) -> None:
        menu = QMenu(self)
        duplicate = menu.addAction(I18N.tr("mask.duplicate_window", "Duplicate window"))
        duplicate.setEnabled(self._selected_id in self._shapes)
        copy_action = menu.addAction(I18N.tr("mask.copy_windows_to_layer", "Copy windows to layer…"))
        track = menu.addAction(I18N.tr("mask.send_tracker", "Send to Tracker"))
        track.setEnabled(self._selected_id in self._shapes)
        chosen = menu.exec(self.menu_button.mapToGlobal(self.menu_button.rect().bottomLeft()))
        if chosen is duplicate and self._selected_id:
            self.duplicate_requested.emit(self._selected_id)
        elif chosen is copy_action:
            self.copy_to_layer_requested.emit()
        elif chosen is track:
            self.tracker_requested.emit()
