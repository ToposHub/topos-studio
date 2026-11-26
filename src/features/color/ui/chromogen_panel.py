"""Compose and inspect a look in the Look Dev panel."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import numpy as np
from PyQt6.QtCore import QEvent, QIODevice, QObject, QSaveFile, QSignalBlocker, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QDropEvent, QHideEvent, QIcon, QResizeEvent
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from src.features.color.core.chromogen import (
    CHROMOGEN_KEY, CHROMOGEN_MAX_STAGES, CHROMOGEN_PRESETS, CHROMOGEN_VERSION, MODULATION_CONTROLS, STAGE_SPECS,
    ControlSpec, apply_chromogen_linear, chromogen_compatibility_error, chromogen_coordinates, chromogen_hue,
    new_chromogen_stage, normalize_chromogen,
)
from src.features.color.ui.chromogen_plot import ChromogenPlot
from src.features.color.ui.panel_chrome import (
    HEADER_HEIGHT, ColorParameterRow, PARAMETER_LABEL_STYLE, SECTION_TITLE_STYLE,
    create_panel_divider,
    create_panel_layout, style_header_button, style_reset_button,
)
from src.shared.assets.icon_manager import (
    icon_add, icon_color_picker, icon_delete, icon_down, icon_duplicate, icon_export, icon_folder_open,
    icon_grid, icon_reset, icon_up, icon_visible,
)
from src.shared.i18n.i18n import I18N
from src.shared.project.color_graph import unfreeze_params
from src.shared.ui.panel_splitter import PanelSplitter
from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.chevron_combo import ChevronComboBox


_PRESET_SIZE_LIMIT = 1024 * 1024
_FAMILIES = (("fundamental", "Fundamental"), ("advanced", "Advanced"), ("sector", "Sector"))
_STAGE_ID_ROLE = Qt.ItemDataRole.UserRole
_VALUE_WIDTH = 56
_COLUMN_INSET = 8
_FOOTER_CONTROL_HEIGHT = 24
_PROJECTIONS = (("radial", "Radial", "Radial"), ("eab", "Eab Cube", "Eab"),
                ("rgb", "RGB Cube", "RGB"), ("response", "Stage Response", "Resp."))


def _text(key: str, default: str) -> str:
    return I18N.tr(f"chromogen.{key}", default)


def _stage_label(kind: str) -> str:
    return _text(f"stage.{kind}", STAGE_SPECS[kind].label)


def _control_label(spec: ControlSpec) -> str:
    return _text(f"control.{spec.key}", spec.label)


def _supports_hue_pick(kind: str) -> bool:
    return kind == "neutral_tint" or kind.startswith("sector_")


def _empty_look() -> dict[str, Any]:
    return {"version": CHROMOGEN_VERSION, "enabled": True, "amount": 1.0, "stages": []}


def _preset_contents(look: Mapping[str, Any]) -> tuple[bool, float, list[dict[str, Any]]]:
    stages = [{key: value for key, value in stage.items() if key != "id"} for stage in look["stages"]]
    return look["enabled"], look["amount"], stages


def _column_layout(column: QWidget) -> tuple[QHBoxLayout, QVBoxLayout, QHBoxLayout]:
    """Give all three splitter panes the same header and footer geometry."""
    layout = QVBoxLayout(column)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    header, footer = (QWidget(column) for _ in range(2))
    for bar in (header, footer):
        bar.setFixedHeight(HEADER_HEIGHT)
    head_tools, foot_tools = QHBoxLayout(header), QHBoxLayout(footer)
    for tools in (head_tools, foot_tools):
        tools.setContentsMargins(_COLUMN_INSET, 0, _COLUMN_INSET, 0)
        tools.setSpacing(2)
        tools.setAlignment(Qt.AlignmentFlag.AlignVCenter)
    body = QVBoxLayout()
    body.setContentsMargins(_COLUMN_INSET, 3, _COLUMN_INSET, 3)
    layout.addWidget(header)
    layout.addWidget(create_panel_divider(Qt.Orientation.Horizontal))
    layout.addLayout(body, 1)
    layout.addWidget(create_panel_divider(Qt.Orientation.Horizontal))
    layout.addWidget(footer)
    return head_tools, body, foot_tools


def _button(
    parent: QWidget, icon: QIcon, label: str, *, checkable: bool = False,
) -> QToolButton:
    button = QToolButton(parent)
    style_header_button(button, width=24, icon=icon)
    button.setToolTip(label)
    button.setAccessibleName(label)
    button.setCheckable(checkable)
    return button


class _StageList(QListWidget):
    order_changed = pyqtSignal(list)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("chromogenStages")
        self.setAccessibleName(_text("stages", "Stages"))
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.setUniformItemSizes(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(f"""
            QListWidget#chromogenStages {{
                background: {C.COLOR_PANEL_INSET}; border: 1px solid {C.PANEL_DIVIDER}; border-radius: 3px;
                outline: none;}}
            QListWidget#chromogenStages::item {{
                min-height: 23px; padding: 2px 4px; border-left: 2px solid transparent;}}
            QListWidget#chromogenStages::item:selected {{
                background: {C.COLOR_PANEL_RAISED}; color: {C.TEXT_PRIMARY}; border-left: 2px solid {C.ORANGE};}}
        """)

    def ids(self) -> list[str]:
        return [self.item(row).data(_STAGE_ID_ROLE) for row in range(self.count())]

    def dropEvent(self, event: QDropEvent) -> None:
        before = self.ids()
        with QSignalBlocker(self):
            super().dropEvent(event)
        after = self.ids()
        if after != before and len(before) == len(after) and set(before) == set(after):
            self.order_changed.emit(after)


class _Parameter(ColorParameterRow):
    """A parameter row whose gesture can be cancelled when the look changes."""

    def __init__(
        self, label: str, minimum: float, maximum: float, default: float, *,
        step: float, decimals: int, label_width: int, suffix: str = "",
        always_show_slider: bool = False,
    ) -> None:
        super().__init__(label, minimum, maximum, default, step=step, decimals=decimals,
                         suffix=suffix, label_width=label_width, value_fixed_width=_VALUE_WIDTH,
                         slider_auto_hide=not always_show_slider, slider_show_width=256)
        self.slider.setVisible(always_show_slider)
        self.slider.use_groove_drag_span = True
        self.slider.setSingleStep(max(1, round(step * self.scale)))
        self.label.setToolTip(label)
        self.spin.setFixedWidth(_VALUE_WIDTH)
        self.spin.setAccessibleName(label)
        layout = self.layout()
        layout.setStretchFactor(self.slider, 1)
        if not always_show_slider:
            layout.insertStretch(0)

    def cancel_edit(self) -> None:
        with QSignalBlocker(self.slider), QSignalBlocker(self.spin):
            if self.slider.isSliderDown():
                self.slider._vdrag_active = False
                self.slider.setSliderDown(False)
            if self.spin._dragging:
                self.spin._dragging = self.spin._drag_engaged = False
                self.spin._value_label.setStyleSheet(self.spin._normal_value_style)
            self.spin._cancel_edit()


class ChromogenPanel(QWidget):
    """Edit a bounded, ordered look and emit its grading patch."""

    changed = pyqtSignal(dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    pick_requested = pyqtSignal(bool)
    pick_hue_requested = pyqtSignal(bool)
    export_lut_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chromogenPanel")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self._state: dict[str, Any] | None = None
        self._accepted: dict[str, Any] | None = None
        self._unsupported: Any = None
        self._selected_id: str | None = None
        self._active_edit = False
        self._syncing = False
        self._control_stage: tuple[str, str] | None = None
        self._rows: dict[str, _Parameter] = {}
        self._publish_timer = QTimer(self)
        self._publish_timer.setSingleShot(True)
        self._publish_timer.setInterval(16)
        self._publish_timer.timeout.connect(self.flush_pending_changes)

        header, outer = create_panel_layout(self, margins=(_COLUMN_INSET, _COLUMN_INSET, 0), spacing=4)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        title = QLabel(_text("title", "Look Dev"), self)
        title.setStyleSheet(SECTION_TITLE_STYLE)
        header.addWidget(title)
        header.addStretch()
        self.pick_button = _button(self, icon_color_picker(), _text("pick", "Pick a color from the viewer"), checkable=True)
        self.pick_button.toggled.connect(self._pick_color_toggled)
        header.addWidget(self.pick_button)
        lut_button = _button(self, icon_export(), _text("export_lut", "Export Look Dev LUT"))
        lut_button.clicked.connect(self.export_lut_requested)
        header.addWidget(lut_button)
        preset_button = _button(self, icon_folder_open(), _text("presets", "Look Dev presets"))
        preset_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        preset_button.setMenu(self._preset_menu(preset_button))
        header.addWidget(preset_button)
        header.addWidget(create_panel_divider(Qt.Orientation.Vertical))
        self.bypass_button = _button(self, icon_visible(), _text("bypass_look", "Bypass look"), checkable=True)
        self.bypass_button.toggled.connect(self._set_bypassed)
        header.addWidget(self.bypass_button)
        self.reset_button = _button(self, icon_reset(), _text("clear_look", "Clear look"))
        self.reset_button.clicked.connect(self.reset)
        header.addWidget(self.reset_button)

        splitter = PanelSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._create_stage_column())
        splitter.addWidget(self._create_scope_column())
        parameters = self._create_parameter_column()
        splitter.addWidget(parameters)
        for index, factor in enumerate((1, 3, 1)):
            splitter.setStretchFactor(index, factor)
        splitter.setSizes([154, 460, parameters.minimumWidth() + 18])
        outer.addWidget(splitter, 1)
        self._refresh()

    def _create_stage_column(self) -> QWidget:
        column = QWidget(self)
        tools, body, footer = _column_layout(column)
        title = QLabel(_text("stages", "Stages"), column)
        title.setStyleSheet(SECTION_TITLE_STYLE)
        tools.addWidget(title, 1)
        self.add_button = _button(column, icon_add(), _text("add_stage", "Add stage"))
        self.add_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.add_button.setMenu(self._add_menu(self.add_button))
        tools.addWidget(self.add_button)
        self.stage_list = _StageList(column)
        self.stage_list.setMinimumWidth(0)
        self.stage_list.currentItemChanged.connect(self._select_item)
        self.stage_list.itemChanged.connect(self._edit_stage_item)
        self.stage_list.order_changed.connect(self._reorder)
        body.addWidget(self.stage_list)

        self.up_button = _button(column, icon_up(), _text("move_up", "Move stage up"))
        self.down_button = _button(column, icon_down(), _text("move_down", "Move stage down"))
        self.duplicate_button = _button(column, icon_duplicate(), _text("duplicate_stage", "Duplicate stage"))
        self.delete_button = _button(column, icon_delete(), _text("delete_stage", "Delete stage"))
        self.up_button.clicked.connect(lambda: self.move_stage(-1))
        self.down_button.clicked.connect(lambda: self.move_stage(1))
        self.duplicate_button.clicked.connect(self.duplicate_stage)
        self.delete_button.clicked.connect(self.delete_stage)
        for button in (self.up_button, self.down_button, self.duplicate_button, self.delete_button):
            button.setFixedHeight(_FOOTER_CONTROL_HEIGHT)
            footer.addWidget(button)
        footer.addStretch()
        column.setMinimumWidth(footer.minimumSize().width())
        return column

    def _create_scope_column(self) -> QWidget:
        column = QWidget(self)
        self._scope_column = column
        tools, body, footer = _column_layout(column)
        self.preset = ChevronComboBox(column)
        self.preset.setAccessibleName(_text("presets", "Look Dev presets"))
        self.preset.setPlaceholderText(_text("select_preset", "Select preset"))
        self.preset.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        for name in CHROMOGEN_PRESETS:
            self.preset.addItem(_text(f"preset.{name}", name), name)
        self.preset.activated.connect(self._choose_preset)
        tools.addWidget(self.preset)
        tools.addStretch()

        self.scope = ChromogenPlot(column)
        self.projection = ChevronComboBox(column)
        self.projection.setAccessibleName(_text("scope_view", "Scope view"))
        self.projection.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        for key, label, _ in _PROJECTIONS:
            self.projection.addItem(_text(key, label), key)
        self._compact_scope = False
        self.projection.currentIndexChanged.connect(self._set_projection)
        footer.addWidget(self.projection)
        self.gamut = ChevronComboBox(column)
        self.gamut.setAccessibleName(_text("scope_gamut", "Scope sample gamut"))
        self.gamut.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.gamut.addItem("Eab", "eab")
        self.gamut.addItem("LMS", "lms")
        self.gamut.addItem("709", "rec709")
        self.gamut.addItem("2020", "rec2020")
        self.gamut.currentIndexChanged.connect(self._set_gamut)
        footer.addWidget(self.gamut)
        footer.addStretch()
        self.compare_button = _button(column, icon_visible(), _text("compare", "Show original samples"), checkable=True)
        self.compare_button.setChecked(True)
        self.compare_button.toggled.connect(self.scope.set_compare)
        tools.addWidget(self.compare_button)
        self._view_reset = _button(column, icon_grid(), _text("reset_view", "Reset scope view"))
        self._view_reset.clicked.connect(self.scope.reset_view)
        tools.addWidget(self._view_reset)
        body.addWidget(self.scope)
        self._full_scope_width = footer.sizeHint().width()
        self._scope_header_width = tools.sizeHint().width()
        self._set_compact_scope(True)
        column.setMinimumWidth(self.projection.sizeHint().width() + self.gamut.sizeHint().width()
                               + 2 * _COLUMN_INSET + footer.spacing())
        column.installEventFilter(self)
        return column

    def _create_parameter_column(self) -> QWidget:
        column = QWidget(self)
        tools, body, self._strength_tools = _column_layout(column)
        self.stage_title = QLabel(_text("stage_parameters", "Stage Parameters"), column)
        self.stage_title.setStyleSheet(SECTION_TITLE_STYLE)
        self.stage_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        tools.addWidget(self.stage_title, 1)
        self.hue_pick_button = _button(
            column, icon_color_picker(), _text("pick_hue", "Pick the stage hue"), checkable=True,
        )
        self.hue_pick_button.hide()
        self.hue_pick_button.toggled.connect(self._pick_hue_toggled)
        tools.addWidget(self.hue_pick_button)
        self.reset_stage_button = QToolButton(column)
        style_reset_button(self.reset_stage_button)
        reset_label = _text("reset_stage", "Reset selected stage")
        self.reset_stage_button.setToolTip(reset_label)
        self.reset_stage_button.setAccessibleName(reset_label)
        self.reset_stage_button.clicked.connect(self.reset_stage)
        tools.addWidget(self.reset_stage_button)
        scroller = QScrollArea(column)
        self._parameter_scroll = scroller
        scroller.setFrameShape(QFrame.Shape.NoFrame)
        scroller.setWidgetResizable(True)
        scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._params_widget = QWidget(scroller)
        self._params_layout = QVBoxLayout(self._params_widget)
        self._params_layout.setContentsMargins(0, 0, 0, 0)
        self._params_layout.setSpacing(2)
        scroller.setWidget(self._params_widget)
        body.addWidget(scroller)
        strength = _text("strength", "Strength")
        self._amount = _Parameter(strength, 0.0, 1.0, 1.0, step=0.01, decimals=2,
                                  label_width=52, always_show_slider=True)
        self._amount.setObjectName("chromogenStrength")
        self._amount.setFixedHeight(_FOOTER_CONTROL_HEIGHT)
        self._amount.valueChanged.connect(self._set_amount)
        self._amount.interactionStarted.connect(self._start_edit)
        self._amount.interactionEnded.connect(self._end_edit)
        self._strength_tools.addWidget(self._amount)

        self._amount.label.ensurePolished()
        metrics = self._amount.label.fontMetrics()
        self._amount.label.setFixedWidth(metrics.horizontalAdvance(strength) + 2)
        controls = [*MODULATION_CONTROLS]
        for stage in STAGE_SPECS.values():
            controls.extend(stage.controls)
        name_width = max(metrics.horizontalAdvance(_control_label(control)) for control in controls)
        self._stage_label_width = name_width + 3
        scrollbar_width = scroller.verticalScrollBar().sizeHint().width()
        column.setMinimumWidth(max(144, self._stage_label_width + _VALUE_WIDTH + scrollbar_width + 4
                                   + 2 * _COLUMN_INSET, self._strength_tools.minimumSize().width() + scrollbar_width))
        scroller.viewport().installEventFilter(self)
        return column

    def _set_compact_scope(self, compact: bool) -> None:
        self._compact_scope = compact
        with QSignalBlocker(self.projection):
            for index, (key, label, short) in enumerate(_PROJECTIONS):
                name = _text(f"short_{key}", short) if compact else _text(key, label)
                self.projection.setItemText(index, name)

    def _choose_preset(self, index: int) -> None:
        name = self.preset.itemData(index)
        if name and (self._state is None or _preset_contents(self._state) != _preset_contents(CHROMOGEN_PRESETS[name])):
            self.apply_preset(CHROMOGEN_PRESETS[name])

    def _sync_preset_choice(self) -> None:
        state = _preset_contents(self._state) if self._state else None
        name = next((name for name, preset in CHROMOGEN_PRESETS.items() if _preset_contents(preset) == state), None)
        with QSignalBlocker(self.preset):
            self.preset.setCurrentIndex(self.preset.findData(name) if name is not None else -1)
            self.preset.setEnabled(self._unsupported is None)

    def _set_projection(self, index: int) -> None:
        projection = self.projection.itemData(index)
        self.scope.set_projection(projection)
        self.compare_button.setEnabled(projection != "response")

    def _set_gamut(self, index: int) -> None:
        self.scope.set_gamut(self.gamut.itemData(index))

    def set_picked_color(self, rgb: tuple[float, float, float] | None) -> None:
        """Present the source color and apply its hue if the sector picker is armed."""
        self.scope.set_picked_color(rgb)
        if rgb is None or not self.hue_pick_button.isChecked() or self._selected_id is None:
            return
        sample = np.asarray(rgb, dtype=np.float32).reshape(1, 3)
        index = self._selected_index()
        if index > 0:
            sample = apply_chromogen_linear(
                sample, {**_empty_look(), "stages": self._stages()[:index]},
            )
        chroma = chromogen_coordinates(sample)[0, 1:]
        if np.isfinite(chroma).all() and float(np.linalg.norm(chroma)) > 1e-5:
            self.set_picked_hue(float(chromogen_hue(sample)[0]))

    def set_picked_hue(self, hue: float) -> None:
        index = self._selected_index()
        if index < 0 or not math.isfinite(hue):
            return
        kind = self._stages()[index]["kind"]
        control = next((spec for spec in STAGE_SPECS[kind].controls if spec.key == "hue"), None)
        if control is not None and _supports_hue_pick(kind):
            hue = (hue - control.minimum) % 360.0 + control.minimum
            self._mutate(lambda state: state["stages"][index]["params"].update(hue=hue))

    def set_pick_active(self, active: bool) -> None:
        """Reflect an externally completed or cancelled pick."""
        with QSignalBlocker(self.pick_button):
            self.pick_button.setChecked(active and not self.hue_pick_button.isChecked())
        if not active:
            with QSignalBlocker(self.hue_pick_button):
                self.hue_pick_button.setChecked(False)

    def _pick_color_toggled(self, active: bool) -> None:
        if active:
            with QSignalBlocker(self.hue_pick_button):
                self.hue_pick_button.setChecked(False)
        self.pick_requested.emit(active)

    def _pick_hue_toggled(self, active: bool) -> None:
        if active:
            with QSignalBlocker(self.pick_button):
                self.pick_button.setChecked(False)
        self.pick_hue_requested.emit(active)

    def _add_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu(parent)
        for family, label in _FAMILIES:
            category = menu.addMenu(_text(f"family.{family}", label))
            for kind, spec in STAGE_SPECS.items():
                if spec.family == family:
                    category.addAction(_stage_label(kind), lambda checked=False, k=kind: self.add_stage(k))
        return menu

    def _preset_menu(self, parent: QWidget) -> QMenu:
        menu = QMenu(parent)
        replace = menu.addMenu(_text("replace_preset", "Replace with preset"))
        append = menu.addMenu(_text("append_preset", "Append preset"))
        for label in CHROMOGEN_PRESETS:
            title = _text(f"preset.{label}", label)
            replace.addAction(title, lambda checked=False, name=label: self.apply_preset(CHROMOGEN_PRESETS[name]))
            append.addAction(title, lambda checked=False, name=label: self.apply_preset(CHROMOGEN_PRESETS[name], append=True))
        menu.addSeparator()
        menu.addAction(_text("import_replace", "Import preset and replace"), lambda: self._import_dialog(append=False))
        menu.addAction(_text("import_append", "Import preset and append"), lambda: self._import_dialog(append=True))
        menu.addAction(_text("save_preset", "Save look as preset"), self._export_dialog)
        return menu

    def _stages(self) -> list[dict[str, Any]]:
        return self._state["stages"] if self._state else []

    def _selected_index(self) -> int:
        return next((i for i, stage in enumerate(self._stages()) if stage["id"] == self._selected_id), -1)

    def _refresh(self) -> None:
        self._syncing = True
        try:
            stages = self._stages()
            if not any(stage["id"] == self._selected_id for stage in stages):
                self._selected_id = stages[0]["id"] if stages else None
            with QSignalBlocker(self.stage_list):
                rebuild = self.stage_list.ids() != [stage["id"] for stage in stages]
                if rebuild:
                    self.stage_list.clear()
                for index, stage in enumerate(stages):
                    if rebuild:
                        item = QListWidgetItem()
                        item.setData(_STAGE_ID_ROLE, stage["id"])
                        item.setFlags((item.flags() | Qt.ItemFlag.ItemIsUserCheckable |
                                       Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsEditable)
                                      & ~Qt.ItemFlag.ItemIsDropEnabled)
                        self.stage_list.addItem(item)
                    else:
                        item = self.stage_list.item(index)
                    name = stage.get("name") or _stage_label(stage["kind"])
                    item.setText(name)
                    tip = _text("stage_item_tooltip", "Check to enable, drag to reorder, double-click to rename.")
                    item.setToolTip(f"{name}\n{tip}")
                    item.setCheckState(Qt.CheckState.Checked if stage["enabled"] else Qt.CheckState.Unchecked)
                    if stage["id"] == self._selected_id:
                        self.stage_list.setCurrentItem(item)
            self._amount.setValue(float(self._state["amount"]) if self._state else self._amount._default_val)
            with QSignalBlocker(self.bypass_button):
                self.bypass_button.setChecked(self._state is not None and not self._state["enabled"])
            self._sync_preset_choice()
            self._refresh_selection()
        finally:
            self._syncing = False

    def _refresh_selection(self) -> None:
        index = self._selected_index()
        stages = self._stages()
        stage = stages[index] if index >= 0 else None
        stage_id = stage["id"] if stage else None
        control_stage = (stage_id, stage["kind"]) if stage else None
        if control_stage != self._control_stage or not self._params_layout.count():
            self.hue_pick_button.setChecked(False)
            self._build_controls(stage)
        if stage:
            for key, row in self._rows.items():
                row.setValue(stage["params"].get(key, row._default_val))
        exists, editable = bool(stages), self._unsupported is None
        self._amount.setEnabled(editable and exists)
        self.bypass_button.setEnabled(editable and exists)
        self.reset_button.setEnabled(editable and exists)
        self.add_button.setEnabled(editable and len(stages) < CHROMOGEN_MAX_STAGES)
        self.up_button.setEnabled(editable and index > 0)
        self.down_button.setEnabled(editable and 0 <= index < len(stages) - 1)
        self.duplicate_button.setEnabled(editable and stage is not None and len(stages) < CHROMOGEN_MAX_STAGES)
        self.delete_button.setEnabled(editable and stage is not None)
        self.reset_stage_button.setEnabled(editable and stage is not None)
        self.hue_pick_button.setVisible(stage is not None and _supports_hue_pick(stage["kind"]))
        self._params_widget.setEnabled(editable and stage is not None)
        title = _stage_label(stage["kind"]) if stage else _text("stage_parameters", "Stage Parameters")
        self.stage_title.setText(title)
        self.stage_title.setToolTip(title)
        self.scope.set_state(self._state, stage_id)

    def _build_controls(self, stage: dict[str, Any] | None) -> None:
        while self._params_layout.count():
            item = self._params_layout.takeAt(0)
            if widget := item.widget():
                widget.hide()
                widget.deleteLater()
        self._rows.clear()
        self._control_stage = (stage["id"], stage["kind"]) if stage else None
        if stage:
            specification = STAGE_SPECS[stage["kind"]]
            for control in specification.controls:
                self._append_control(stage["id"], control)
            if specification.supports_modulation:
                self._params_layout.addWidget(create_panel_divider(Qt.Orientation.Horizontal))
                title = QLabel(_text("modulation", "Modulation"), self._params_widget)
                title.setStyleSheet(PARAMETER_LABEL_STYLE)
                self._params_layout.addWidget(title)
                for control in MODULATION_CONTROLS:
                    self._append_control(stage["id"], control)
        else:
            label = (_text("unsupported_look", "This look uses an unsupported version.") if self._unsupported is not None
                     else _text("empty_stages", "Add a stage to build the look."))
            empty = QLabel(label, self._params_widget)
            empty.setStyleSheet(PARAMETER_LABEL_STYLE)
            empty.setWordWrap(True)
            self._params_layout.addWidget(empty)
        self._params_layout.addStretch()

    def _append_control(self, stage_id: str, spec: ControlSpec) -> None:
        row = _Parameter(_control_label(spec), spec.minimum, spec.maximum, spec.default, step=spec.step,
                         decimals=spec.decimals, suffix=spec.suffix, label_width=self._stage_label_width)
        row.setObjectName(f"chromogen_{spec.key}")
        row.valueChanged.connect(lambda value, sid=stage_id, key=spec.key: self._set_control(sid, key, value))
        row.interactionStarted.connect(self._start_edit)
        row.interactionEnded.connect(self._end_edit)
        self._rows[spec.key] = row
        self._params_layout.addWidget(row)

    def _select_item(self, item: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        if self._syncing:
            return
        stage_id = item.data(_STAGE_ID_ROLE) if item else None
        self.finish_edit(commit=True)
        self._selected_id = stage_id
        self._refresh_selection()

    def _edit_stage_item(self, item: QListWidgetItem) -> None:
        if self._syncing or self._state is None:
            return
        stage_id = item.data(_STAGE_ID_ROLE)
        enabled = item.checkState() == Qt.CheckState.Checked
        name = item.text().strip()[:128]

        def change(state: dict[str, Any]) -> None:
            for stage in state["stages"]:
                if stage["id"] == stage_id:
                    stage["enabled"] = enabled
                    if name and name != _stage_label(stage["kind"]):
                        stage["name"] = name
                    else:
                        stage.pop("name", None)
                    break

        self._mutate(change)
        if not name:
            self._refresh()

    def _set_bypassed(self, bypassed: bool) -> None:
        if self._state:
            self._mutate(lambda state: state.update(enabled=not bypassed))

    def _set_amount(self, amount: float) -> None:
        if self._syncing or self._state is None or self._state["amount"] == amount:
            return
        self._edit(lambda state: state.update(amount=amount))

    def _set_control(self, stage_id: str, key: str, value: float) -> None:
        if self._syncing or self._state is None or stage_id != self._selected_id:
            return
        index = self._selected_index()
        if index < 0 or self._stages()[index]["params"].get(key) == value:
            return
        self._edit(lambda state: state["stages"][index]["params"].update({key: value}))

    def _start_edit(self) -> None:
        if not self._active_edit and not self._syncing:
            self._active_edit = True
            self.interaction_started.emit()

    def _end_edit(self) -> None:
        active = self._active_edit
        self._active_edit = False
        self.flush_pending_changes()
        if active:
            self.interaction_ended.emit()

    def _edit(self, change: Callable[[dict[str, Any]], None]) -> None:
        atomic = not self._active_edit
        self._start_edit()
        draft = deepcopy(self._state)
        if draft is not None:
            change(draft)
            self._state = draft
        if atomic:
            self._end_edit()
        elif not self._publish_timer.isActive():
            self._publish_timer.start()

    def _mutate(self, change: Callable[[dict[str, Any]], None], selected: str | None = None) -> None:
        if self._unsupported is not None:
            return
        self.finish_edit(commit=True)
        draft = deepcopy(self._state) if self._state is not None else _empty_look()
        change(draft)
        normalized = normalize_chromogen(draft)
        if normalized is not None and not normalized["stages"]:
            normalized = None
        if normalized == self._state:
            return
        self._start_edit()
        self._state = normalized
        if selected is not None:
            self._selected_id = selected
        self._refresh()
        self._end_edit()

    def add_stage(self, kind: str) -> None:
        if kind not in STAGE_SPECS or len(self._stages()) >= CHROMOGEN_MAX_STAGES:
            return
        stage = new_chromogen_stage(kind)
        index = self._selected_index()
        self._mutate(lambda state: state["stages"].insert(index + 1 if index >= 0 else len(state["stages"]), stage),
                     selected=stage["id"])

    def _reorder(self, order: list[str]) -> None:
        indexed = {stage["id"]: stage for stage in self._stages()}
        if indexed.keys() != set(order) or len(order) != len(indexed):
            return
        self._mutate(lambda state: state.update(stages=[indexed[stage_id] for stage_id in order]))

    def move_stage(self, offset: int) -> None:
        index = self._selected_index()
        destination = index + offset
        if index < 0 or not 0 <= destination < len(self._stages()):
            return

        def move(state: dict[str, Any]) -> None:
            stages = state["stages"]
            stages.insert(destination, stages.pop(index))

        self._mutate(move)

    def duplicate_stage(self) -> None:
        index = self._selected_index()
        if index < 0 or len(self._stages()) >= CHROMOGEN_MAX_STAGES:
            return
        stage = deepcopy(self._stages()[index])
        stage["id"] = new_chromogen_stage(stage["kind"])["id"]
        self._mutate(lambda state: state["stages"].insert(index + 1, stage), selected=stage["id"])

    def delete_stage(self) -> None:
        index = self._selected_index()
        if index >= 0:
            self._mutate(lambda state: state["stages"].pop(index))

    def reset_stage(self) -> None:
        index = self._selected_index()
        if index >= 0:
            params = new_chromogen_stage(self._stages()[index]["kind"])["params"]
            self._mutate(lambda state: state["stages"][index]["params"].update(params))

    def reset(self) -> None:
        self._mutate(lambda state: state.update(_empty_look()))

    def apply_preset(self, raw: Mapping[str, Any], *, append: bool = False) -> bool:
        if self._unsupported is not None:
            return False
        preset = normalize_chromogen(raw)
        if preset is None or not preset["stages"]:
            return False
        if append and len(self._stages()) + len(preset["stages"]) > CHROMOGEN_MAX_STAGES:
            return False
        for stage in preset["stages"]:
            stage["id"] = new_chromogen_stage(stage["kind"])["id"]
        self._mutate(
            (lambda state: state["stages"].extend(preset["stages"])) if append else (lambda state: state.update(preset)),
            selected=preset["stages"][0]["id"],
        )
        return True

    def _import_dialog(self, *, append: bool) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, _text("import_preset", "Import Look Dev preset"), "", _text("preset_files", "JSON files (*.json)"),
        )
        if path:
            try:
                with Path(path).open("rb") as stream:
                    data = stream.read(_PRESET_SIZE_LIMIT + 1)
                if len(data) > _PRESET_SIZE_LIMIT or not self.apply_preset(json.loads(data), append=append):
                    raise ValueError(_text("invalid_preset", "Invalid preset or too many stages."))
            except (OSError, ValueError, RecursionError) as error:
                QMessageBox.warning(self, _text("import_preset", "Import Look Dev preset"), str(error))

    def _export_dialog(self) -> None:
        if self._state is None or not self._stages():
            return
        dialog = QFileDialog(self, _text("save_preset", "Save look as preset"))
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setNameFilter(_text("preset_files", "JSON files (*.json)"))
        dialog.setDefaultSuffix("json")
        dialog.selectFile("Look Dev.json")
        if not dialog.exec():
            return
        path = dialog.selectedFiles()[0]
        self.finish_edit(commit=True)
        try:
            data = json.dumps(self._state, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as error:
            QMessageBox.warning(self, _text("save_preset", "Save look as preset"), str(error))
            return
        file = QSaveFile(path)
        if (not file.open(QIODevice.OpenModeFlag.WriteOnly)
                or file.write(data) != len(data) or not file.commit()):
            QMessageBox.warning(self, _text("save_preset", "Save look as preset"), file.errorString())

    def get_params(self) -> dict[str, Any]:
        return {CHROMOGEN_KEY: deepcopy(self._unsupported if self._unsupported is not None else self._state)}

    def set_params(self, params: Mapping[str, Any]) -> None:
        raw = unfreeze_params(params.get(CHROMOGEN_KEY))
        unsupported = raw if chromogen_compatibility_error(raw) else None
        state = normalize_chromogen(raw) if unsupported is None else None
        self.finish_edit(commit=False)
        if self._state != state or self._unsupported != unsupported:
            self._state = state
            self._unsupported = deepcopy(unsupported)
            self._accepted = deepcopy(state)
            if state is None:
                self._build_controls(None)
            self._refresh()

    def finish_edit(self, *, commit: bool) -> None:
        """Close active input and either commit or discard the unpublished draft."""
        self._amount.cancel_edit()
        for row in self._rows.values():
            row.cancel_edit()
        if not commit and self._state != self._accepted:
            self._state = deepcopy(self._accepted)
            self._refresh()
        self._end_edit()

    def flush_pending_changes(self) -> None:
        self._publish_timer.stop()
        if self._state != self._accepted:
            self._accepted = deepcopy(self._state)
            self._sync_preset_choice()
            self.scope.set_state(self._state, self._selected_id)
            self.changed.emit(self.get_params())

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self._scope_column and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            width = self._scope_column.width()
            self._view_reset.setVisible(width >= self._scope_header_width)
            compact = width < self._full_scope_width
            if compact != self._compact_scope:
                self._set_compact_scope(compact)
        elif isinstance(event, QResizeEvent) and watched is self._parameter_scroll.viewport():
            scrollbar_width = self._parameter_scroll.width() - event.size().width()
            self._strength_tools.setContentsMargins(_COLUMN_INSET, 0, _COLUMN_INSET + scrollbar_width, 0)
        return super().eventFilter(watched, event)

    def hideEvent(self, event: QHideEvent) -> None:
        self.finish_edit(commit=True)
        self.pick_button.setChecked(False)
        self.hue_pick_button.setChecked(False)
        super().hideEvent(event)
