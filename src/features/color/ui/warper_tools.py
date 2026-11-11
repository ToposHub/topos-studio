"""Resolve-shaped Color Warper tool palettes using Topos' shared controls."""
from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QLinearGradient, QMouseEvent, QPainter, QPaintEvent, QPen
from PyQt6.QtWidgets import (
    QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QSizePolicy,
    QSlider, QToolButton, QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C, COMPOSITE_KEYFRAMES_ICON_BTN_STYLE
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.ui.widgets.warper_icons import warper_icon


def tr(key: str, fallback: str) -> str:
    return I18N.tr(f"warper.{key}", fallback)


def tool_button(name: str, label: str, checkable: bool = False) -> QToolButton:
    button = QToolButton()
    button.setObjectName(f"warper_{name}")
    button.setIcon(warper_icon(name))
    button.setAccessibleName(label)
    button.setToolTip(label)
    button.setCheckable(checkable)
    button.setFixedHeight(24)
    button.setMinimumWidth(24)
    button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    button.setStyleSheet(COMPOSITE_KEYFRAMES_ICON_BTN_STYLE)
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
            for i, color in enumerate(("#2a00dd", "#a800ef", "#ff006b", "#ff1400", "#fff000", "#00ef00", "#00ddc7", "#0011ee")):
                gradient.setColorAt(i / 7, QColor(color))
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
        x = event.position().x() / max(1, self.width())
        self._handle = "low" if abs(x - self.low) <= abs(x - self.high) else "high"
        self.mouseMoveEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._handle is None:
            return
        value = max(0.0, min(1.0, event.position().x() / max(1, self.width())))
        if self._handle == "low":
            self.low = min(value, self.high)
        else:
            self.high = max(value, self.low)
        self.range_changed.emit(self.low, self.high); self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._handle = None

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 10); layout.setSpacing(6)
        self.grid_buttons: list[QToolButton] = []
        if rectangular:
            grids = QHBoxLayout(); grids.setSpacing(0)
            group = QButtonGroup(self); group.setExclusive(True)
            for i in range(2):
                button = QToolButton(); button.setText(tr(f"grid_{i + 1}", f"Grid {i + 1}"))
                button.setCheckable(True); button.setChecked(i == 0)
                button.setStyleSheet(COMPOSITE_KEYFRAMES_ICON_BTN_STYLE)
                button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                button.setFixedHeight(24); group.addButton(button)
                button.clicked.connect(lambda checked=False, index=i: self.grid_selected.emit(index))
                grids.addWidget(button); self.grid_buttons.append(button)
            layout.addLayout(grids); layout.addSpacing(4)
        title = QLabel(tr("tools", "Tools")); title.setFixedHeight(13)
        layout.addWidget(title)
        row = QHBoxLayout(); row.setSpacing(0)
        group = QButtonGroup(self); group.setExclusive(True)
        for name, fallback in (("select", "Select / Deselect"), ("draw", "Draw Selection"),
                               ("pin", "Pin / De-pin"), ("pull", "Pull Points"), ("push", "Push Points")):
            button = tool_button(name, tr(name, fallback), True)
            group.addButton(button); button.setChecked(name == "select")
            button.clicked.connect(lambda checked=False, name=name: self.tool_selected.emit(name))
            row.addWidget(button); self.tool_buttons[name] = button
        layout.addLayout(row); layout.addSpacing(3); layout.addWidget(separator()); layout.addSpacing(3)
        actions = QGridLayout(); actions.setSpacing(4)
        labels = (("falloff_add", "Increase Fall Off / Smooth Selection"),
                  ("falloff_sub", "Decrease Fall Off / Smooth Selection"),
                  ("invert", "Invert Selection"), ("convert_pin", "Convert Selected to Pin"),
                  ("column", "Select / Pin Column"),
                  ("row" if rectangular else "ring", "Select / Pin Row" if rectangular else "Select / Pin Ring"),
                  ("all", "Select / Pin All or Deselect / De-pin All"), ("reset_selection", "Reset Selection / Pins"))
        for i, (name, label) in enumerate(labels):
            button = tool_button("reset" if name == "reset_selection" else name, tr(name, label))
            button.clicked.connect(lambda checked=False, name=name: self.action_requested.emit(name))
            actions.addWidget(button, i // 4, i % 4); self.action_buttons[name] = button
        layout.addLayout(actions); layout.addSpacing(6)
        range_label = QLabel(tr("range", "Range")); range_label.setFixedHeight(14)
        layout.addWidget(range_label)
        self.range_bar = WarperRange(rectangular)
        layout.addWidget(self.range_bar); layout.addSpacing(6)
        lock_row = QHBoxLayout(); lock_row.setSpacing(6)
        self.auto_lock = ToggleSwitch()
        self.auto_lock.setAccessibleName(tr("auto_lock", "Auto Lock"))
        self.auto_lock.toggled.connect(self.lock_changed.emit)
        lock_row.addWidget(self.auto_lock); lock_row.addWidget(QLabel(tr("auto_lock", "Auto Lock"))); lock_row.addStretch()
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
                button = tool_button(name, tr(f"auto_pin_{name}", f"Auto Pin surrounding {name}"), True)
                button.setEnabled(False); axes.addButton(button); button.setChecked(name == "columns")
                button.clicked.connect(self.lock_changed.emit)
                locks.addWidget(button); self.lock_axes[name] = button
        else:
            locks.addStretch()
        layout.addLayout(locks); layout.addSpacing(7)
        values = QHBoxLayout(); values.setSpacing(4)
        components = (("chroma", "Chroma"), ("luma", "Luma")) if rectangular else (("hue", "Hue"), ("sat", "Sat"), ("luma", "Luma"))
        for name, label in components:
            col = QVBoxLayout(); col.setSpacing(3)
            number_row = QHBoxLayout(); number_row.setSpacing(2)
            number_row.addWidget(QLabel(tr(name, label)))
            value = DragValue("", -1 if name == "chroma" else 0, 1, 0, 3, compact=True, stretchable=True)
            value.setFixedWidth(50)
            value.setAccessibleName(tr(name, label)); value.setMixedValue(True); value.setEnabled(False)
            value.valueChanged.connect(lambda v, key=name: self.component_edited.emit(key, v, False))
            value.interaction_started.connect(lambda key=name: self.interaction_started.emit(key))
            value.interaction_ended.connect(lambda key=name: self.interaction_ended.emit(key))
            number_row.addWidget(value); col.addLayout(number_row); self.values[name] = value
            buttons = QHBoxLayout(); buttons.setSpacing(0)
            self.component_buttons[name] = []
            if rectangular or name != "luma":
                smooth = tool_button("smooth", tr(f"smooth_{name}", f"Smooth {label}"))
                smooth.setEnabled(False)
                smooth.clicked.connect(lambda checked=False, key=name: self.component_edited.emit(key, None, True))
                buttons.addWidget(smooth); self.component_buttons[name].append(smooth)
            reset = tool_button("reset", tr(f"reset_{name}", f"Reset {label}")); reset.setEnabled(False)
            reset.clicked.connect(lambda checked=False, key=name: self.component_edited.emit(key, None, False))
            buttons.addWidget(reset); self.component_buttons[name].append(reset)
            col.addLayout(buttons); values.addLayout(col)
        layout.addLayout(values); layout.addStretch()

    def sync_selection(self, values: dict[str, float | None]) -> None:
        for key, widget in self.values.items():
            widget.setEnabled(bool(values))
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
        layout = QVBoxLayout(self); layout.setContentsMargins(10, 7, 10, 10); layout.setSpacing(4)
        layout.addWidget(QLabel(tr("tools", "Tools")))
        row = QHBoxLayout(); row.setSpacing(0)
        group = QButtonGroup(self); group.setExclusive(True)
        for name, label in (("stroke", "Add Stroke - Normal Mode"), ("point_stroke", "Add Stroke - Point To Point Mode"),
                            ("add_pin", "Add Pin Point"), ("select", "Select")):
            button = tool_button(name, tr(name, label), True)
            group.addButton(button); button.setChecked(name == "stroke")
            button.clicked.connect(lambda checked=False, key=name: self.tool_selected.emit(key))
            row.addWidget(button); self.tool_buttons[name] = button
        self.delete_button = tool_button("delete", tr("delete", "Delete the selected Strokes and Pin Points"))
        self.delete_button.setEnabled(False); self.delete_button.clicked.connect(lambda: self.action_requested.emit("delete"))
        row.addWidget(self.delete_button); layout.addLayout(row); layout.addWidget(separator())
        title = QHBoxLayout(); title.addWidget(QLabel(tr("pin_title", "Pin"))); title.addStretch()
        reset = tool_button("reset", tr("reset_pin", "Reset Pin")); reset.setFixedWidth(24)
        reset.clicked.connect(lambda: self.action_requested.emit("reset_pin")); title.addWidget(reset)
        layout.addLayout(title)
        self.pin_reset = reset
        for key, label, low, high, default in (
            ("chroma_range", "Chroma Range", 0.0, 1.0, 0.04),
            ("tonal_low", "Tonal Range Low", 0.0, 2.0, 1.0),
            ("tonal_high", "Tonal Range High", 0.0, 2.0, 1.0),
            ("tonal_pivot", "Tonal Range Pivot", 0.0, 1.0, 0.5),
            ("exposure", "Exposure", -8.0, 8.0, 0.0),
        ):
            header = QHBoxLayout(); header.setSpacing(4)
            header.addWidget(QLabel(tr(key, label))); header.addStretch()
            value = DragValue("", low, high, default, 3, compact=True)
            value.setAccessibleName(tr(key, label)); value.setFixedWidth(62); value.setEnabled(False)
            header.addWidget(value); layout.addLayout(header)
            slider = QSlider(Qt.Orientation.Horizontal); slider.setRange(0, 1000)
            slider.setFixedHeight(10); slider.setEnabled(False)
            slider.setValue(round((default - low) / (high - low) * 1000))
            value.valueChanged.connect(lambda v, key=key: self.parameter_edited.emit(key, v))
            slider.valueChanged.connect(lambda v, key=key, low=low, high=high: self.parameter_edited.emit(key, low + v / 1000 * (high - low)))
            layout.addWidget(slider); layout.addSpacing(4)
            self.values[key], self.sliders[key] = value, slider
        modes = QHBoxLayout(); modes.setSpacing(0)
        self.mode_buttons: dict[str, QToolButton] = {}
        group = QButtonGroup(self); group.setExclusive(True)
        for key, icon, fallback in (("normal", "stroke", "Normal Mode"), ("point_to_point", "point_stroke", "Point to Point Mode")):
            button = tool_button(icon, tr(f"{key}_mode", fallback), True); button.setFixedWidth(46)
            group.addButton(button); button.setChecked(key == "normal"); button.setEnabled(False)
            button.clicked.connect(lambda checked=False, key=key: self.parameter_edited.emit("stroke_mode", key))
            modes.addWidget(button); self.mode_buttons[key] = button
        modes.addStretch(); layout.addLayout(modes); layout.addStretch()

    def sync_selection(self, selected: bool, parameters: dict[str, float | str]) -> None:
        self.delete_button.setEnabled(selected); self.pin_reset.setEnabled(selected)
        for key, value in self.values.items():
            value.setEnabled(selected)
            value.blockSignals(True); value.setValue(float(parameters[key])); value.blockSignals(False)
            slider = self.sliders[key]; slider.setEnabled(selected)
            slider.blockSignals(True)
            slider.setValue(round((value.value() - value._min) / (value._max - value._min) * 1000))
            slider.blockSignals(False)
        for key, button in self.mode_buttons.items():
            button.setEnabled(selected); button.setChecked(key == parameters["stroke_mode"])
