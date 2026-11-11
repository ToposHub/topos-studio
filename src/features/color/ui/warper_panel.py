"""Resolve-shaped Warper UI. Only legacy square HS edits reach the renderer.

New capabilities publish separate presentation signals. Their backend work is
specified in docs/plans/2026-11-12-color-warper-backend-plan.md.
"""
from __future__ import annotations

from typing import Any

import numpy as np
from PyQt6.QtCore import QEvent, QObject, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QResizeEvent
from PyQt6.QtWidgets import (
    QButtonGroup, QDialog, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from src.features.color.ui.warper_tools import MeshTools, StrokeTools, tool_button, tr
from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.chroma_warp_canvas import ChromaWarpCanvas
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.warper_canvas import WarperCanvas
from src.shared.ui.widgets.warper_icons import warper_icon

MODES = ("chroma_warp", "hue_saturation", "chroma_luma")
MODE_LABELS = {"chroma_warp": "Chroma Warp", "hue_saturation": "Hue - Saturation", "chroma_luma": "Chroma - Luma"}


class _PaletteStack(QStackedWidget):
    """A short palette must not inherit the hidden Chroma Warp page's height."""

    def sizeHint(self) -> QSize:
        return self.currentWidget().sizeHint() if self.currentWidget() else super().sizeHint()

    def minimumSizeHint(self) -> QSize:
        return self.currentWidget().minimumSizeHint() if self.currentWidget() else super().minimumSizeHint()


class WarperPanel(QWidget):
    warper_changed = pyqtSignal(object)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    reset_clicked = pyqtSignal()
    ui_state_changed = pyqtSignal(dict)
    command_requested = pyqtSignal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = "hue_saturation"
        self._updating = False
        self._legacy_interaction_open = False
        self._dialog: QDialog | None = None
        self._footer_narrow: bool | None = None
        self._resolutions = {"hue_saturation": (6, 6), "chroma_luma": (6, 6)}
        self._linked = {"hue_saturation": True, "chroma_luma": True}
        self._spaces = {"hue_saturation": "HSP", "chroma_luma": "HSP"}
        self.init_ui()

    def init_ui(self) -> None:
        self.setObjectName("colorWarperPanel")
        self.setMinimumWidth(600)
        self.setStyleSheet(f"QWidget#warperSurface {{ background: {C.BG_PANEL}; }}")
        self._host_layout = QVBoxLayout(self)
        self._host_layout.setContentsMargins(0, 0, 0, 0)
        self._host_layout.setSpacing(0)
        self.surface = QWidget()
        self.surface.setObjectName("warperSurface")
        self.surface.installEventFilter(self)
        layout = QVBoxLayout(self.surface)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._host_layout.addWidget(self.surface)
        self._build_header(layout)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        self.canvas_stack = QStackedWidget()
        self.warper = WarperCanvas()
        self.chroma_luma = WarperCanvas("chroma_luma")
        self.chroma_warp = ChromaWarpCanvas()
        self.canvases = {"chroma_warp": self.chroma_warp, "hue_saturation": self.warper, "chroma_luma": self.chroma_luma}
        for mode in MODES:
            canvas = self.canvases[mode]
            self.canvas_stack.addWidget(canvas)
            canvas.ui_edited.connect(self._emit_ui)
            canvas.selection_changed.connect(self._selection_changed)
        self.warper.changed.connect(self._legacy_changed)
        self.warper.interaction_started.connect(self._legacy_start)
        self.warper.interaction_ended.connect(self._legacy_end)
        self.chroma_luma.grid_activated.connect(self._grid_activated)
        left_layout.addWidget(self.canvas_stack, 1)
        self._build_footer(left_layout)
        body.addWidget(left, 1)
        self._build_tools(body)
        layout.addLayout(body, 1)
        self.expanded_placeholder = QPushButton(tr("show_expanded", "Show Color Warper"))
        self.expanded_placeholder.clicked.connect(self._raise_expanded)
        self.expanded_placeholder.hide()
        self._host_layout.addWidget(self.expanded_placeholder)
        self.set_mode(self.mode)

    def _build_header(self, layout: QVBoxLayout) -> None:
        header = QWidget()
        header.setFixedHeight(35)
        header.setStyleSheet(f"background: {C.BG_DARK};")
        top = QHBoxLayout(header)
        top.setContentsMargins(8, 0, 3, 0)
        top.setSpacing(2)
        self.title = QLabel()
        self.title.setStyleSheet(f"color: {C.TEXT_PRIMARY}; font-size: 11px; font-weight: bold;")
        top.addWidget(self.title, 1)
        group = QButtonGroup(self)
        group.setExclusive(True)
        self.mode_buttons: dict[str, QToolButton] = {}
        for mode, icon in zip(MODES, ("chroma", "hs", "cl")):
            button = tool_button(icon, tr(mode, MODE_LABELS[mode]), True)
            button.setObjectName(f"warper_mode_{mode}")
            button.setFixedSize(32, 26)
            button.setIconSize(QSize(18, 18))
            group.addButton(button)
            button.setChecked(mode == self.mode)
            button.clicked.connect(lambda checked=False, mode=mode: self.set_mode(mode))
            top.addWidget(button)
            self.mode_buttons[mode] = button
        self.expand_button = tool_button("expand", tr("expand", "Expand"))
        self.expand_button.setFixedWidth(32)
        self.expand_button.clicked.connect(self.toggle_expanded)
        top.addWidget(self.expand_button)
        self.reset_btn = tool_button("reset", tr("reset_all", "Reset All"))
        self.reset_btn.setFixedWidth(32)
        self.reset_btn.clicked.connect(self.on_reset)
        top.addWidget(self.reset_btn)
        self.options_button = tool_button("more", tr("options", "Options"))
        self.options_button.setFixedWidth(32)
        self.options_menu = QMenu(self.options_button)
        self.grid_action = self.options_menu.addAction(tr("show_grid", "Show Grid"))
        self.grid_action.setCheckable(True)
        self.grid_action.setChecked(True)
        self.density_action = self.options_menu.addAction(tr("show_distribution", "Show Color Distribution"))
        self.density_action.setCheckable(True)
        self.density_action.setChecked(True)
        self.grid_action.toggled.connect(self._display_changed)
        self.density_action.toggled.connect(self._display_changed)
        self.options_menu.addSeparator()
        self.options_menu.addAction(tr("reset_selection", "Reset Selection / Pins"), lambda: self._mesh_action("reset_selection"))
        self.options_button.setMenu(self.options_menu)
        self.options_button.setStyleSheet(self.options_button.styleSheet() + "QToolButton::menu-indicator { image: none; width: 0px; }")
        self.options_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        top.addWidget(self.options_button)
        layout.addWidget(header)

    @staticmethod
    def _combo(items: list[str], width: int) -> ChevronComboBox:
        combo = ChevronComboBox()
        combo.addItems(items)
        combo.setFixedWidth(width)
        return combo

    def _build_footer(self, layout: QVBoxLayout) -> None:
        self.footer = QWidget()
        self.footer.setFixedHeight(47)
        self.footer.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._footer_layout = QVBoxLayout(self.footer)
        self._footer_layout.setContentsMargins(0, 0, 0, 0)
        self._footer_layout.setSpacing(0)
        self._footer_row = QWidget()
        self._footer_layout.addWidget(self._footer_row)
        footer = QHBoxLayout(self._footer_row)
        self._footer_controls = footer
        footer.setContentsMargins(10, 0, 10, 0)
        footer.setSpacing(4)
        self.resolution_h_icon = tool_button("resolution_h", tr("hue_resolution", "Hue Resolution"))
        self.resolution_s_icon = tool_button("resolution_s", tr("sat_resolution", "Saturation Resolution"))
        self.grid_combo = self._combo(["6", "8", "12", "16", "24"], 59)
        self.saturation_combo = self._combo(["6", "8", "12", "16", "24"], 59)
        self.grid_combo.currentIndexChanged.connect(self.on_grid_size_changed)
        self.saturation_combo.currentIndexChanged.connect(lambda index: self._resolution_changed(1))
        for icon, combo in ((self.resolution_h_icon, self.grid_combo), (self.resolution_s_icon, self.saturation_combo)):
            icon.setFixedWidth(20)
            footer.addWidget(icon)
            footer.addWidget(combo)
            icon.clicked.connect(lambda checked=False, combo=combo: combo.showPopup())
        self.link_button = tool_button("link", tr("link", "Link"), True)
        self.link_button.setFixedWidth(26)
        self.link_button.setChecked(True)
        self.link_button.clicked.connect(self._link_changed)
        footer.addWidget(self.link_button)
        footer.addStretch()
        self._axis_group = QWidget()
        axis_group_layout = QVBoxLayout(self._axis_group)
        axis_group_layout.setContentsMargins(0, 0, 0, 0)
        axis_group_layout.setSpacing(1)
        axis_layout = QHBoxLayout()
        axis_group_layout.addLayout(axis_layout)
        axis_layout.setContentsMargins(0, 0, 0, 0)
        axis_layout.setSpacing(4)
        self.axis_label = QLabel(tr("axis_angle", "Axis Angle"))
        self.axis_label.setStyleSheet(f"color: {C.TEXT_HINT}; font-size: 11px;")
        axis_layout.addWidget(self.axis_label)
        self.axis_angle = DragValue("", -180, 180, 0, 2, compact=True)
        self.axis_angle.setFixedWidth(60)
        self.axis_angle.setAccessibleName(tr("axis_angle", "Axis Angle"))
        self.axis_angle.valueChanged.connect(self._axis_changed)
        axis_layout.addWidget(self.axis_angle)
        axis_spectrum = QWidget()
        axis_spectrum.setFixedHeight(2)
        axis_spectrum.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ba1741, stop:0.25 #dfb63a, stop:0.5 #42baa5, stop:0.75 #2990c3, stop:1 #ba1741);")
        axis_group_layout.addWidget(axis_spectrum)
        footer.addWidget(self._axis_group)
        space_icon = tool_button("space", tr("color_space", "Color Space"))
        space_icon.setFixedWidth(20)
        footer.addWidget(space_icon)
        self.space_combo = self._combo(["HSV", "HSL", "HSP"], 82)
        self.space_combo.setCurrentText("HSP")
        self.space_combo.setAccessibleName(tr("color_space", "Color Space"))
        self.space_combo.currentTextChanged.connect(self._space_changed)
        footer.addWidget(self.space_combo)
        space_icon.clicked.connect(lambda: self.space_combo.showPopup())
        layout.addWidget(self.footer)

    def _build_tools(self, body: QHBoxLayout) -> None:
        self.tools_scroll = QScrollArea()
        self.tools_scroll.setObjectName("warperToolsScroll")
        self.tools_scroll.setFixedWidth(253)
        self.tools_scroll.setWidgetResizable(True)
        self.tools_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.tools_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tools_scroll.setStyleSheet(f"QScrollArea#warperToolsScroll {{ background: {C.BG_PANEL}; border-left: 1px solid {C.PANEL_DIVIDER}; }}")
        self.tools_stack = _PaletteStack()
        self.hs_tools = MeshTools()
        self.cl_tools = MeshTools(True)
        self.stroke_tools = StrokeTools()
        self.tools = {"chroma_warp": self.stroke_tools, "hue_saturation": self.hs_tools, "chroma_luma": self.cl_tools}
        for mode in MODES:
            palette = self.tools[mode]
            self.tools_stack.addWidget(palette)
            palette.tool_selected.connect(self._tool_changed)
        for palette in (self.hs_tools, self.cl_tools):
            palette.action_requested.connect(self._mesh_action)
            palette.component_edited.connect(self._component_edited)
            palette.lock_changed.connect(self._lock_changed)
            palette.range_bar.range_changed.connect(self._range_changed)
            palette.interaction_started.connect(self._numeric_start)
            palette.interaction_ended.connect(self._numeric_end)
        self.cl_tools.grid_selected.connect(self.chroma_luma.activate_grid)
        self.stroke_tools.action_requested.connect(self._stroke_action)
        self.stroke_tools.parameter_edited.connect(self._stroke_parameter)
        self.tools_scroll.setWidget(self.tools_stack)
        body.addWidget(self.tools_scroll)

    def active_canvas(self) -> WarperCanvas | ChromaWarpCanvas:
        return self.canvases[self.mode]

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"Unknown Color Warper mode: {mode}")
        previous = self.active_canvas()
        if isinstance(previous, WarperCanvas):
            previous._end_edit()
        previous.cancel_gesture()
        self.mode = mode
        self._updating = True
        try:
            self.mode_buttons[mode].setChecked(True)
            self.canvas_stack.setCurrentWidget(self.canvases[mode])
            self.tools_stack.setCurrentWidget(self.tools[mode])
            self.tools_stack.updateGeometry()
            self.tools_scroll.setFixedWidth(max(253, self.tools[mode].minimumSizeHint().width() + 1))
            self.title.setText(f"Color Warper - {tr(mode, MODE_LABELS[mode])}")
            self.footer.setVisible(mode != "chroma_warp")
            rectangular = mode == "chroma_luma"
            self.axis_label.setVisible(rectangular)
            self.axis_angle.setVisible(rectangular)
            self._axis_group.setVisible(rectangular)
            self.resolution_h_icon.setIcon(warper_icon("cl_columns" if rectangular else "resolution_h"))
            self.resolution_s_icon.setIcon(warper_icon("cl_rows" if rectangular else "resolution_s"))
            self.cl_tools.action_buttons["all"].setIcon(warper_icon("all_rect"))
            if mode != "chroma_warp":
                items = ["4", "6", "8", "12", "16", "24"] if rectangular else ["6", "8", "12", "16", "24"]
                for combo, value in zip((self.grid_combo, self.saturation_combo), self._resolutions[mode]):
                    combo.clear()
                    combo.addItems(items)
                    combo.setCurrentText(str(value))
                self.link_button.setChecked(self._linked[mode])
                self.space_combo.setCurrentText(self._spaces[mode])
                for widget, key, fallback in (
                    (self.resolution_h_icon, "chroma_resolution" if rectangular else "hue_resolution", "Chroma Resolution" if rectangular else "Hue Resolution"),
                    (self.resolution_s_icon, "luma_resolution" if rectangular else "sat_resolution", "Luma Resolution" if rectangular else "Saturation Resolution"),
                ):
                    widget.setToolTip(tr(key, fallback))
                    widget.setAccessibleName(tr(key, fallback))
                self.grid_combo.setAccessibleName(self.resolution_h_icon.accessibleName())
                self.saturation_combo.setAccessibleName(self.resolution_s_icon.accessibleName())
            self._sync_tools()
        finally:
            self._updating = False
        self._adapt_footer()
        self._emit_ui()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_axis_group"):
            self._adapt_footer()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.surface and event.type() == QEvent.Type.Resize and hasattr(self, "_axis_group"):
            self._adapt_footer()
        return super().eventFilter(watched, event)

    def _adapt_footer(self) -> None:
        sidebar_width = self.tools_scroll.width() if hasattr(self, "tools_scroll") else 253
        # Shared Topos inputs need more width than Resolve's 44px fields, but
        # should still keep Axis Angle inline in the usual 550–625px canvas.
        narrow = self.mode == "chroma_luma" and self.surface.width() - sidebar_width < 480
        if narrow == self._footer_narrow:
            return
        self._footer_narrow = narrow
        self._footer_controls.removeWidget(self._axis_group)
        self._footer_layout.removeWidget(self._axis_group)
        if narrow:
            self._footer_layout.addWidget(self._axis_group, 0, Qt.AlignmentFlag.AlignCenter)
        else:
            self._footer_controls.insertWidget(self._footer_controls.count() - 2, self._axis_group)
        self.footer.setFixedHeight(70 if narrow else 47)

    def ui_state(self) -> dict[str, Any]:
        return {"version": 1, "mode": self.mode, "linked": dict(self._linked),
                "show_grid": self.grid_action.isChecked(), "show_distribution": self.density_action.isChecked(),
                "modes": {mode: canvas.snapshot() for mode, canvas in self.canvases.items()}}

    def _emit_ui(self) -> None:
        if not self._updating:
            self.ui_state_changed.emit(self.ui_state())

    def _request(self, action: str, **payload: Any) -> None:
        if not self._updating:
            self.command_requested.emit({"mode": self.mode, "action": action, "payload": payload,
                                         "state": self.active_canvas().snapshot()})

    def _legacy_changed(self, data: np.ndarray) -> None:
        if not self._updating and self._legacy_supported():
            self.warper_changed.emit(data)

    def _legacy_supported(self) -> bool:
        return (self.mode == "hue_saturation" and self.warper.ring_count == self.warper.grid_size
                and self._spaces["hue_saturation"] == "HSV")

    def _legacy_start(self) -> None:
        if self._legacy_supported() and not self._updating:
            self._legacy_interaction_open = True
            self.interaction_started.emit()

    def _legacy_end(self) -> None:
        if self._legacy_interaction_open:
            self._legacy_interaction_open = False
            self.interaction_ended.emit()

    def _numeric_start(self, component: str) -> None:
        if self.mode == "hue_saturation" and component == "luma":
            return
        canvas = self.active_canvas()
        if isinstance(canvas, WarperCanvas):
            canvas.begin_component_drag()

    def _numeric_end(self, component: str) -> None:
        if self.mode == "hue_saturation" and component == "luma":
            return
        canvas = self.active_canvas()
        if isinstance(canvas, WarperCanvas):
            canvas.end_component_drag()

    def on_grid_size_changed(self, index: int) -> None:
        self._resolution_changed(0)

    def _resolution_changed(self, axis: int) -> None:
        if self._updating or self.mode == "chroma_warp":
            return
        source = self.grid_combo if axis == 0 else self.saturation_combo
        value = int(source.currentText())
        self._updating = True
        try:
            if self.link_button.isChecked():
                (self.saturation_combo if axis == 0 else self.grid_combo).setCurrentText(str(value))
            sizes = (int(self.grid_combo.currentText()), int(self.saturation_combo.currentText()))
            self._resolutions[self.mode] = sizes
            canvas = self.active_canvas()
            if isinstance(canvas, WarperCanvas):
                canvas.set_resolution(*sizes)
        finally:
            self._updating = False
        if self._legacy_supported():
            self.warper_changed.emit(None)
        self._sync_tools()
        self._request("resolution", columns=sizes[0], rows=sizes[1])
        self._emit_ui()

    def _link_changed(self, checked: bool) -> None:
        if self._updating or self.mode == "chroma_warp":
            return
        self._linked[self.mode] = checked
        if checked:
            self._resolution_changed(0)
        self._emit_ui()

    def _space_changed(self, value: str) -> None:
        if self._updating or self.mode == "chroma_warp":
            return
        self._spaces[self.mode] = value
        self.active_canvas().color_space = value
        self._request("color_space", value=value)
        self._emit_ui()

    def _axis_changed(self, value: float) -> None:
        if self._updating:
            return
        self.chroma_luma.axis_angle = value
        self.chroma_luma.update()
        self._request("axis_angle", value=value)
        self._emit_ui()

    def _tool_changed(self, tool: str) -> None:
        canvas = self.active_canvas()
        canvas.cancel_gesture()
        canvas.tool = tool
        canvas.setCursor(Qt.CursorShape.CrossCursor if tool not in {"select", "pin"} else Qt.CursorShape.ArrowCursor)
        self._request("tool", tool=tool)
        self._emit_ui()

    def _mesh_action(self, action: str) -> None:
        canvas = self.active_canvas()
        if isinstance(canvas, WarperCanvas):
            canvas.perform(action)
            self._request(action)

    def _component_edited(self, component: str, value: float | None, smooth: bool) -> None:
        if self._updating:
            return
        canvas = self.active_canvas()
        if isinstance(canvas, WarperCanvas):
            canvas.edit_component(component, value, smooth)
            self._request("component", component=component, value=value, smooth=smooth)

    def _lock_changed(self) -> None:
        if self._updating or self.mode == "chroma_warp":
            return
        palette = self.hs_tools if self.mode == "hue_saturation" else self.cl_tools
        canvas = self.active_canvas()
        if isinstance(canvas, WarperCanvas):
            canvas.auto_lock = palette.auto_lock.isChecked()
            canvas.lock_points = palette.lock_combo.currentIndex() + 1
            palette.lock_combo.setEnabled(canvas.auto_lock)
            for key, button in palette.lock_axes.items():
                button.setEnabled(canvas.auto_lock)
                if button.isChecked():
                    canvas.lock_axis = key
        self._request("auto_lock")
        self._emit_ui()

    def _range_changed(self, low: float, high: float) -> None:
        canvas = self.active_canvas()
        if isinstance(canvas, WarperCanvas):
            canvas.set_range(low, high)
            self._request("range", low=low, high=high)

    def _grid_activated(self, index: int) -> None:
        self.cl_tools.grid_buttons[index].setChecked(True)
        self.cl_tools.range_bar.grid_index = index
        self.cl_tools.range_bar.update()
        self._request("active_grid", index=index)
        self._emit_ui()

    def _selection_changed(self) -> None:
        self._sync_tools()
        self._emit_ui()

    def _sync_tools(self) -> None:
        canvas = self.active_canvas()
        if isinstance(canvas, WarperCanvas):
            palette = self.hs_tools if self.mode == "hue_saturation" else self.cl_tools
            palette.sync_selection(canvas.selected_values())
            for action in ("falloff_add", "falloff_sub", "convert_pin", "column", "row", "ring"):
                if action in palette.action_buttons:
                    palette.action_buttons[action].setEnabled(bool(canvas.selected))
            palette.action_buttons["reset_selection"].setEnabled(bool(canvas.selected or canvas.pins or canvas.falloff))
        else:
            parameters = canvas.parameters
            if canvas.selected is not None:
                kind, index = canvas.selected
                parameters = (canvas.strokes if kind == "stroke" else canvas.pins)[index]["parameters"]
            self.stroke_tools.sync_selection(canvas.selected is not None, parameters)

    def _stroke_action(self, action: str) -> None:
        if action == "delete":
            self.chroma_warp.delete_selection()
        elif action == "reset_pin":
            for key, widget in self.stroke_tools.values.items():
                self.chroma_warp.set_parameter(key, widget.defaultValue())
            self._sync_tools()
        self._request(action)

    def _stroke_parameter(self, key: str, value: float | str) -> None:
        if self._updating:
            return
        self.chroma_warp.set_parameter(key, value)
        self._sync_tools()
        self._request("stroke_parameter", key=key, value=value)

    def _display_changed(self, checked: bool) -> None:
        for canvas in self.canvases.values():
            canvas.show_grid = self.grid_action.isChecked()
            canvas.show_density = self.density_action.isChecked()
            canvas.update()
        self._emit_ui()

    def on_reset(self) -> None:
        self.active_canvas().reset()
        if self._legacy_supported():
            self.warper_changed.emit(None)
            self.reset_clicked.emit()
        self._sync_tools()
        self._request("reset")
        self._emit_ui()

    def set_params(self, grid_data: np.ndarray | None, emit: bool = False) -> None:
        """Hydrate legacy HS without stale drag samples or recursive edits."""
        if grid_data is not None:
            data = np.asarray(grid_data)
            if data.ndim != 3 or data.shape[0] != data.shape[1] or data.shape[2] != 2 or not np.isfinite(data).all():
                raise ValueError("Expected a finite square Hue/Saturation delta grid")
            size = int(data.shape[0])
            if size not in {6, 8, 12, 16, 24}:
                raise ValueError(f"Unsupported legacy Warper resolution: {size}")
        else:
            size = self.warper.grid_size
        self._updating = True
        self.warper.cancel_gesture()
        self.warper.blockSignals(True)
        try:
            self.warper.set_grid_size(size)
            if grid_data is not None:
                self.warper.apply_deltas(data)
            self._spaces["hue_saturation"] = "HSV"
            self.warper.color_space = "HSV"
            self._resolutions["hue_saturation"] = (size, size)
            if self.mode == "hue_saturation":
                self.grid_combo.setCurrentText(str(size))
                self.saturation_combo.setCurrentText(str(size))
                self.space_combo.setCurrentText(self._spaces["hue_saturation"])
            self.warper.update()
            self._sync_tools()
        finally:
            self.warper.blockSignals(False)
            self._updating = False
        if emit:
            self.warper_changed.emit(grid_data)

    def reset_all(self) -> None:
        # Existing ToolsPanel.reset_all calls this as an application-wide reset,
        # even when a presentation-only mode is currently visible.
        self._updating = True
        try:
            for canvas in self.canvases.values():
                canvas.reset()
            self.axis_angle.blockSignals(True)
            self.axis_angle.setValue(0)
            self.axis_angle.blockSignals(False)
            self.chroma_luma.axis_angle = 0.0
            self._sync_tools()
        finally:
            self._updating = False
        self.warper_changed.emit(None)
        self.reset_clicked.emit()
        self._request("reset_all")
        self._emit_ui()

    def toggle_expanded(self) -> None:
        if self._dialog is not None:
            self.collapse_expanded()
            return
        dialog = QDialog(self.window())
        dialog.setWindowTitle("Color Warper")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setMinimumSize(700, 400)
        dialog.resize(1122, 648)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        self._host_layout.removeWidget(self.surface)
        layout.addWidget(self.surface)
        self.expanded_placeholder.show()
        self._dialog = dialog
        dialog.finished.connect(self._restore_expanded)
        dialog.show()

    def collapse_expanded(self) -> None:
        """Return the palette to its dock when leaving the Color workspace."""
        if self._dialog is not None:
            self._dialog.close()

    def _raise_expanded(self) -> None:
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()

    def _restore_expanded(self, result: int) -> None:
        if self._dialog is None:
            return
        self._dialog.layout().removeWidget(self.surface)
        self.surface.setParent(self)
        self._host_layout.insertWidget(0, self.surface)
        self.surface.show()
        self.expanded_placeholder.hide()
        self._dialog = None
