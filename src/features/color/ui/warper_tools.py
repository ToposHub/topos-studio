"""Resolve-shaped Color Warper tool palettes using Topos' shared controls."""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QLinearGradient, QMouseEvent, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import (
    QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QSizePolicy,
    QSlider, QToolButton, QVBoxLayout, QWidget,
)

from src.features.color.ui.panel_chrome import (
    PARAMETER_LABEL_STYLE, PARAMETER_ROW_HEIGHT, SECTION_TITLE_STYLE,
    style_reset_button,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.ui.widgets.warper_icons import WARPER_TOOL_HOVER, WARPER_TOOL_IDLE, warper_icon


def tr(key: str, fallback: str) -> str:
    return I18N.tr(f"warper.{key}", fallback)


_TOOL_BUTTON_STYLE = f"""
    QToolButton {{
        background: {C.COLOR_PANEL_INSET}; border: 1px solid {C.PANEL_DIVIDER};
        color: {WARPER_TOOL_IDLE}; border-radius: 3px; padding: 0; margin: 0;
    }}
    QToolButton:hover {{ background: #35363c; color: {WARPER_TOOL_HOVER}; }}
    QToolButton:checked, QToolButton:pressed {{
        background: #45474f; color: white; border-color: #666a74;
    }}
    QToolButton:disabled {{ background: #222226; color: #646870; border-color: #19191d; }}
    QToolButton:focus {{ border-color: #9298a3; }}
    QToolButton[segment="first"] {{ border-top-right-radius: 0; border-bottom-right-radius: 0; }}
    QToolButton[segment="middle"], QToolButton[segment="last"] {{
        border-top-left-radius: 0; border-bottom-left-radius: 0; border-left-width: 0;
    }}
    QToolButton[segment="middle"] {{ border-radius: 0; }}
"""


def _join_buttons(*buttons: QToolButton) -> None:
    """Give adjacent tools a common outline and a single seam per pair."""
    if len(buttons) < 2:
        return
    buttons[0].setProperty("segment", "first")
    for button in buttons[1:-1]:
        button.setProperty("segment", "middle")
    buttons[-1].setProperty("segment", "last")


def tool_button(name: str, label: str, checkable: bool = False) -> QToolButton:
    button = QToolButton()
    button.setObjectName(f"warper_{name}")
    button.setIcon(warper_icon(name))
    button.setAccessibleName(label)
    button.setToolTip(label)
    button.setCheckable(checkable)
    button.setFixedHeight(24)
    button.setIconSize(QSize(18, 18))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setMinimumWidth(24)
    button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    button.setStyleSheet(_TOOL_BUTTON_STYLE)
    return button


_SIDEBAR_BUTTON_HEIGHT = 21


def _sidebar_button(name: str, label: str, checkable: bool = False) -> QToolButton:
    button = tool_button(name, label, checkable)
    button.setIconSize(QSize(16, 16))
    button.setFixedHeight(_SIDEBAR_BUTTON_HEIGHT)
    return button


def reset_button(label: str, *, global_reset: bool = False) -> QToolButton:
    button = QToolButton()
    button.setObjectName("warper_reset")
    button.setAccessibleName(label)
    button.setToolTip(label)
    style_reset_button(button, global_reset=global_reset)
    return button


def separator() -> QFrame:
    frame = QFrame()
    frame.setFrameShape(QFrame.Shape.HLine)
    frame.setFixedHeight(1)
    frame.setStyleSheet(f"background: {C.PANEL_DIVIDER}; border: none;")
    return frame


class WarperRange(QWidget):
    """Two handles select a displayed hue/chroma interval, with double-click reset."""
    range_changed = pyqtSignal(float, float)

    def __init__(self, rectangular: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rectangular = rectangular
        self.grid_index = 0
        self.low, self.high = 0.0, 1.0
        self._handle: str | None = None
        self._press: tuple[float, float, float] | None = None
        self.setFixedHeight(18)
        self.setAccessibleName(tr("range", "Range"))
        self.setToolTip(tr("range_help", "Drag to select a range; double-click to reset"))

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        if self.rectangular:
            colors = ("#595b35", "#515093") if self.grid_index == 0 else ("#316955", "#934b73")
            gradient.setColorAt(0, QColor(colors[0])); gradient.setColorAt(1, QColor(colors[1]))
        else:
            for i in range(7):
                gradient.setColorAt(i / 6, QColor.fromHsv((240 + i * 60) % 360, 255, 238))
        p.setPen(QPen(QColor(C.PANEL_DIVIDER), 1)); p.setBrush(gradient)
        p.drawRoundedRect(rect, 2, 2)
        if self.low > 0 or self.high < 1:
            p.fillRect(QRectF(1, 1, self.low * rect.width(), rect.height()), QColor(0, 0, 0, 140))
            p.fillRect(QRectF(self.high * rect.width(), 1, (1 - self.high) * rect.width(), rect.height()), QColor(0, 0, 0, 140))
            p.setPen(QPen(QColor(C.TEXT_PRIMARY), 1))
            for value in (self.low, self.high):
                x = 1 + value * rect.width()
                p.drawLine(QPointF(x, 2), QPointF(x, self.height() - 2))
        p.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x = self._position(event)
        self._press = x, self.low, self.high
        tolerance = 6 / max(1, self.width() - 2)
        low_distance, high_distance = abs(x - self.low), abs(x - self.high)
        if min(low_distance, high_distance) <= tolerance:
            self._handle = "low" if low_distance <= high_distance else "high"
        elif (self.low > 0 or self.high < 1) and self.low < x < self.high:
            self._handle = "translate"
        else:
            self._handle = "draw"

    def _position(self, event: QMouseEvent) -> float:
        return max(0.0, min(1.0, (event.position().x() - 1) / max(1, self.width() - 2)))

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press is None:
            return
        origin, low, high = self._press
        value = self._position(event)
        if self._handle == "translate":
            delta = min(1.0 - high, max(-low, value - origin))
            self.low, self.high = low + delta, high + delta
        elif self._handle == "draw":
            self.low, self.high = sorted((origin, value))
        elif self._handle == "low":
            self.low, self.high = min(low + value - origin, high), high
        elif self._handle == "high":
            self.low, self.high = low, max(high + value - origin, low)
        self.low, self.high = max(0.0, self.low), min(1.0, self.high)
        self.range_changed.emit(self.low, self.high)
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._handle is not None:
            self.mouseMoveEvent(event)
            self._handle = None
            self._press = None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self._handle = self._press = None
        self.low, self.high = 0.0, 1.0
        self.range_changed.emit(self.low, self.high); self.update()


class MeshTools(QWidget):
    tool_selected = pyqtSignal(str)
    action_requested = pyqtSignal(str)
    component_edited = pyqtSignal(str, object, bool)
    lock_changed = pyqtSignal()
    grid_selected = pyqtSignal(int)
    interaction_started = pyqtSignal(str)
    interaction_ended = pyqtSignal(str)

    def __init__(self, rectangular: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rectangular = rectangular
        self.setStyleSheet("QLabel { font-size: 11px; }")
        self.tool_buttons: dict[str, QToolButton] = {}
        self.action_buttons: dict[str, QToolButton] = {}
        self.values: dict[str, DragValue] = {}
        self.component_buttons: dict[str, list[QToolButton]] = {}
        self.value_labels: dict[str, QLabel] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 5); layout.setSpacing(3)
        self.grid_buttons: list[QToolButton] = []
        if rectangular:
            grids = QHBoxLayout(); grids.setSpacing(0)
            group = QButtonGroup(self); group.setExclusive(True)
            for i in range(2):
                button = QToolButton(); button.setText(tr(f"grid_{i + 1}", f"Grid {i + 1}"))
                button.setCheckable(True); button.setChecked(i == 0)
                button.setStyleSheet(_TOOL_BUTTON_STYLE)
                button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                button.setFixedHeight(_SIDEBAR_BUTTON_HEIGHT); group.addButton(button)
                button.clicked.connect(lambda checked=False, index=i: self.grid_selected.emit(index))
                grids.addWidget(button); self.grid_buttons.append(button)
            _join_buttons(*self.grid_buttons)
            layout.addLayout(grids); layout.addSpacing(2)
        title = QLabel(tr("tools", "Tools"))
        title.setStyleSheet(SECTION_TITLE_STYLE)
        layout.addWidget(title)
        row = QHBoxLayout(); row.setSpacing(0)
        group = QButtonGroup(self); group.setExclusive(True)
        for name, fallback in (("select", "Select / Deselect"), ("draw", "Draw Selection"),
                               ("pin", "Pin / De-pin"), ("pull", "Pull Points"), ("push", "Push Points")):
            button = _sidebar_button(name, tr(name, fallback), True)
            group.addButton(button); button.setChecked(name == "select")
            button.clicked.connect(lambda checked=False, name=name: self.tool_selected.emit(name))
            row.addWidget(button); self.tool_buttons[name] = button
        _join_buttons(*self.tool_buttons.values())
        layout.addLayout(row)
        layout.addWidget(separator())
        actions = QGridLayout()
        actions.setHorizontalSpacing(4)
        actions.setVerticalSpacing(2)
        labels = (("falloff_add", "Increase Fall Off / Smooth Selection"),
                  ("falloff_sub", "Decrease Fall Off / Smooth Selection"),
                  ("invert", "Invert Selection"), ("convert_pin", "Convert Selected to Pin"),
                  ("column", "Select / Pin Column"),
                  ("row" if rectangular else "ring", "Select / Pin Row" if rectangular else "Select / Pin Ring"),
                  ("all", "Select / Pin All or Deselect / De-pin All"), ("reset_selection", "Reset Selection / Pins"))
        for i, (name, label) in enumerate(labels):
            tooltip = tr(name, label)
            button = reset_button(tooltip) if name == "reset_selection" else _sidebar_button(name, tooltip)
            button.clicked.connect(lambda checked=False, name=name: self.action_requested.emit(name))
            actions.addWidget(button, i // 4, i % 4); self.action_buttons[name] = button
        actions.setAlignment(self.action_buttons["reset_selection"], Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(actions); layout.addSpacing(3)
        range_label = QLabel(tr("range", "Range"))
        range_label.setStyleSheet(SECTION_TITLE_STYLE)
        layout.addWidget(range_label)
        self.range_bar = WarperRange(rectangular)
        layout.addWidget(self.range_bar); layout.addSpacing(3)
        lock_row = QHBoxLayout(); lock_row.setSpacing(6)
        self.auto_lock = ToggleSwitch()
        self.auto_lock.setAccessibleName(tr("auto_lock", "Auto Lock"))
        self.auto_lock.toggled.connect(self.lock_changed.emit)
        lock_label = QLabel(tr("auto_lock", "Auto Lock"))
        lock_label.setStyleSheet(PARAMETER_LABEL_STYLE)
        lock_row.addWidget(self.auto_lock); lock_row.addWidget(lock_label); lock_row.addStretch()
        layout.addLayout(lock_row)
        locks = QHBoxLayout(); locks.setSpacing(5)
        self.lock_combo = ChevronComboBox(); self.lock_combo.setFixedWidth(95)
        self.lock_combo.addItems([tr("one_point", "1 Point"), tr("two_points", "2 Points")])
        self.lock_combo.setEnabled(False)
        self.lock_combo.currentIndexChanged.connect(self.lock_changed.emit)
        locks.addWidget(self.lock_combo)
        self.lock_axes: dict[str, QToolButton] = {}
        if rectangular:
            axes = QButtonGroup(self); axes.setExclusive(True)
            for name in ("columns", "rows", "single"):
                button = _sidebar_button(name, tr(f"auto_pin_{name}", f"Auto Pin surrounding {name}"), True)
                button.setEnabled(False); axes.addButton(button); button.setChecked(name == "columns")
                button.clicked.connect(self.lock_changed.emit)
                locks.addWidget(button); self.lock_axes[name] = button
        else:
            locks.addStretch()
        layout.addLayout(locks); layout.addSpacing(3)
        values = QHBoxLayout(); values.setSpacing(2)
        components = (("chroma", "Chroma"), ("luma", "Luma")) if rectangular else (("hue", "Hue"), ("sat", "Sat"), ("luma", "Luma"))
        for name, label in components:
            col = QVBoxLayout(); col.setSpacing(2)
            number_row = QHBoxLayout(); number_row.setSpacing(1)
            value_label = QLabel(tr(name, label))
            value_label.setStyleSheet(PARAMETER_LABEL_STYLE)
            value_label.setEnabled(False)
            self.value_labels[name] = value_label
            number_row.addWidget(value_label)
            lower, upper = (-0.5, 0.5) if name == "chroma" else (0.0, 1.0)
            value = DragValue("", lower, upper, 0, 2 if rectangular else 3, compact=True)
            value.setFixedValueWidth(42)
            value.setFixedSize(42, PARAMETER_ROW_HEIGHT)
            value.setAccessibleName(tr(name, label)); value.setMixedValue(True); value.setEnabled(False)
            value.valueChanged.connect(lambda v, key=name: self.component_edited.emit(key, v, False))
            value.interaction_started.connect(lambda key=name: self.interaction_started.emit(key))
            value.interaction_ended.connect(lambda key=name: self.interaction_ended.emit(key))
            number_row.addWidget(value); col.addLayout(number_row); self.values[name] = value
            buttons = QHBoxLayout(); buttons.setSpacing(3)
            self.component_buttons[name] = []
            if rectangular or name != "luma":
                smooth = _sidebar_button("smooth", tr(f"smooth_{name}", f"Smooth {label}"))
                smooth.setEnabled(False)
                smooth.clicked.connect(lambda checked=False, key=name: self.component_edited.emit(key, None, True))
                buttons.addWidget(smooth); self.component_buttons[name].append(smooth)
            reset = reset_button(tr(f"reset_{name}", f"Reset {label}"))
            reset.setEnabled(False)
            reset.clicked.connect(lambda checked=False, key=name: self.component_edited.emit(key, None, False))
            buttons.addWidget(reset); self.component_buttons[name].append(reset)
            col.addLayout(buttons); values.addLayout(col)
        layout.addLayout(values); layout.addStretch()

    def sync_selection(self, values: dict[str, float | None]) -> None:
        for key, widget in self.values.items():
            widget.setEnabled(bool(values))
            self.value_labels[key].setEnabled(bool(values))
            value = values.get(key)
            widget.setMixedValue(value is None)
            if value is not None:
                widget.blockSignals(True)
                widget.setValue(value)
                widget.blockSignals(False)
            for button in self.component_buttons[key]:
                button.setEnabled(bool(values))


class StrokeTools(QWidget):
    tool_selected = pyqtSignal(str)
    action_requested = pyqtSignal(str)
    parameter_edited = pyqtSignal(str, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tool_buttons: dict[str, QToolButton] = {}
        self.values: dict[str, DragValue] = {}
        self.sliders: dict[str, QSlider] = {}
        self.value_labels: dict[str, QLabel] = {}
        self.setStyleSheet(f"""
            QLabel {{ font-size: 11px; }}
            QLabel:disabled {{ color: {C.TEXT_HINT}; }}
        """)
        layout = QVBoxLayout(self); layout.setContentsMargins(6, 5, 6, 5); layout.setSpacing(2)
        title_label = QLabel(tr("tools", "Tools"))
        title_label.setStyleSheet(SECTION_TITLE_STYLE)
        layout.addWidget(title_label)
        layout.addSpacing(2)
        row = QHBoxLayout(); row.setSpacing(0)
        group = QButtonGroup(self); group.setExclusive(True)
        for name, label in (("stroke", "Add Stroke - Normal Mode"), ("point_stroke", "Add Stroke - Point To Point Mode"),
                            ("add_pin", "Add Pin Point"), ("select", "Select")):
            button = _sidebar_button(name, tr(name, label), True)
            group.addButton(button); button.setChecked(name == "stroke")
            button.clicked.connect(lambda checked=False, key=name: self.tool_selected.emit(key))
            row.addWidget(button); self.tool_buttons[name] = button
        self.delete_button = _sidebar_button("delete", tr("delete", "Delete the selected Strokes and Pin Points"))
        self.delete_button.setEnabled(False); self.delete_button.clicked.connect(lambda: self.action_requested.emit("delete"))
        _join_buttons(*self.tool_buttons.values(), self.delete_button)
        row.addWidget(self.delete_button); layout.addLayout(row)
        layout.addSpacing(2); layout.addWidget(separator()); layout.addSpacing(2)
        title = QHBoxLayout()
        self.pin_label = QLabel(tr("pin_title", "Pin")); self.pin_label.setEnabled(False)
        self.pin_label.setStyleSheet(SECTION_TITLE_STYLE)
        title.addWidget(self.pin_label); title.addStretch()
        reset = reset_button(tr("reset_pin", "Reset Pin"))
        reset.clicked.connect(lambda: self.action_requested.emit("reset_pin")); title.addWidget(reset)
        layout.addLayout(title)
        self.pin_reset = reset
        for key, label, low, high, default in (
            ("chroma_range", "Chroma Range", 0.0, 1.0, 0.04),
            ("tonal_low", "Tonal Range Low", 0.0, 1.0, 1.0),
            ("tonal_high", "Tonal Range High", 0.0, 1.0, 1.0),
            ("tonal_pivot", "Tonal Range Pivot", 0.0, 1.0, 0.5),
            ("exposure", "Exposure", -8.0, 8.0, 0.0),
        ):
            header = QHBoxLayout(); header.setSpacing(4)
            value_label = QLabel(tr(key, label)); value_label.setEnabled(False)
            value_label.setStyleSheet(PARAMETER_LABEL_STYLE)
            self.value_labels[key] = value_label
            header.addWidget(value_label); header.addStretch()
            value = DragValue("", low, high, default, 3, compact=True)
            value.setAccessibleName(tr(key, label)); value.setFixedSize(62, PARAMETER_ROW_HEIGHT); value.setEnabled(False)
            header.addWidget(value); layout.addLayout(header)
            slider = QSlider(Qt.Orientation.Horizontal); slider.setRange(0, 1000)
            slider.setFixedHeight(10); slider.setEnabled(False)
            slider.setValue(round((default - low) / (high - low) * 1000))
            value.valueChanged.connect(lambda v, key=key: self.parameter_edited.emit(key, v))
            slider.valueChanged.connect(lambda v, key=key, low=low, high=high: self.parameter_edited.emit(key, low + v / 1000 * (high - low)))
            layout.addWidget(slider)
            self.values[key], self.sliders[key] = value, slider
        modes = QHBoxLayout(); modes.setSpacing(0)
        self.mode_buttons: dict[str, QToolButton] = {}
        group = QButtonGroup(self); group.setExclusive(True)
        for key, icon, fallback in (("normal", "stroke", "Normal Mode"), ("point_to_point", "point_stroke", "Point to Point Mode")):
            button = _sidebar_button(icon, tr(f"{key}_mode", fallback), True); button.setFixedWidth(46)
            group.addButton(button); button.setChecked(key == "normal"); button.setEnabled(False)
            button.clicked.connect(lambda checked=False, key=key: self.parameter_edited.emit("stroke_mode", key))
            modes.addWidget(button); self.mode_buttons[key] = button
        _join_buttons(*self.mode_buttons.values())
        modes.addStretch(); layout.addLayout(modes); layout.addStretch()

    def sync_selection(self, selected: bool, parameters: dict[str, float | str], *, pin: bool = False) -> None:
        self.delete_button.setEnabled(selected); self.pin_reset.setEnabled(selected)
        self.pin_label.setEnabled(selected)
        self.pin_label.setText(tr("pin_title", "Pin") if pin or not selected else tr("stroke_title", "Stroke"))
        reset = tr("reset_pin", "Reset Pin") if pin or not selected else tr("reset_stroke", "Reset Stroke")
        self.pin_reset.setToolTip(reset)
        self.pin_reset.setAccessibleName(reset)
        for key, value in self.values.items():
            enabled = selected and (not pin or key == "chroma_range")
            value.setEnabled(enabled)
            self.value_labels[key].setEnabled(enabled)
            value.blockSignals(True); value.setValue(float(parameters[key])); value.blockSignals(False)
            slider = self.sliders[key]; slider.setEnabled(enabled)
            slider.blockSignals(True)
            slider.setValue(round((value.value() - value._min) / (value._max - value._min) * 1000))
            slider.blockSignals(False)
        for key, button in self.mode_buttons.items():
            button.setEnabled(selected and not pin); button.setChecked(not pin and key == parameters["stroke_mode"])
