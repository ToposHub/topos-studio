"""Color Warper tools, mesh display, and grading commands."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import numpy as np
from PyQt6.QtCore import QEvent, QObject, QPointF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QHideEvent, QImage, QMouseEvent, QPixmap, QResizeEvent, QShowEvent, QTransform
from PyQt6.QtWidgets import (
    QButtonGroup, QDialog, QGraphicsPixmapItem, QGraphicsView, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from src.features.color.core.warper_hs_solver import resample_calibrated_grid
from src.features.color.core.warper_state import (
    CHROMA709_ALGORITHM, CL_GRID_NAMES, CURRENT_ALGORITHM, HS_GRID_NAME, HS_SPACES709_ALGORITHM,
    LEGACY_ALGORITHM, RESOLVE_HS_ALGORITHMS,
    RESOLVE_HS_ALGORITHM, RESOLVE_HS_ALGORITHM_V2, RESOLVE_HS_ALGORITHM_V3,
    WARPER_SCHEMA_VERSION, WarperGrid, WarperPin, WarperPinPoint, WarperStateV2,
    WarperStroke, legacy_from_ndarray, make_point_id, resample_grid,
)
from src.features.color.ui.panel_chrome import (
    PARAMETER_BAR_BOTTOM_MARGIN, PARAMETER_BAR_HEIGHT, PARAMETER_LABEL_STYLE, SECTION_TITLE_STYLE,
    create_panel_divider, create_panel_header, style_header_button,
)
from src.features.color.ui.warper_tools import MeshTools, StrokeTools, reset_button, tool_button, tr
from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.chroma_warp_canvas import ChromaWarpCanvas
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.warper_canvas import (
    CALIBRATED_MAX_RADIUS, CALIBRATED_SAT_SCALE, WHITE_POINT, PointKey, WarperCanvas,
)
from src.shared.ui.widgets.warper_icons import warper_icon

if TYPE_CHECKING:
    from src.features.color.core.layer import Layer

MODES = ("chroma_warp", "hue_saturation", "chroma_luma")
MODE_LABELS = {"chroma_warp": "Chroma Warp", "hue_saturation": "Hue - Saturation", "chroma_luma": "Chroma - Luma"}
HS_RESOLUTIONS = (6, 8, 12, 16, 24)
_TOOLS_SIDEBAR_WIDTH = 234


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

    def __init__(self, parent: QWidget | None = None, *,
                 use_measured_warper: Callable[[], bool] | None = None) -> None:
        super().__init__(parent)
        self.mode = "hue_saturation"
        self._updating = False
        self._legacy_interaction_open = False
        self._calibration_algorithm: str | None = None
        self._use_measured_warper = use_measured_warper
        self._allow_automatic_calibration = False
        self._pending_mesh_edit = False
        self._pending_palette_edit = False
        self._numeric_palette_edit = False
        self._saved_warper: WarperStateV2 | None = None
        self._palette_hosted = False
        self._viewer: QGraphicsView | None = None
        self._viewer_viewport: QWidget | None = None
        self._viewer_refresh: Callable[[], None] | None = None
        self._viewer_overlay_changed: Callable[[], None] | None = None
        self._viewer_available: Callable[[], bool] | None = None
        self._viewer_request: tuple | None = None
        self._viewer_received: tuple | None = None
        self._viewer_input: tuple | None = None
        self._viewer_drag: tuple[QPointF, QPointF, WarperCanvas | ChromaWarpCanvas] | None = None
        self._isolation: PointKey | None = None
        self._isolation_item: QGraphicsPixmapItem | None = None
        self._dialog: QDialog | None = None
        self._footer_narrow: bool | None = None
        self._resolutions = {"hue_saturation": (6, 6), "chroma_luma": (6, 6)}
        self._linked = {"hue_saturation": True, "chroma_luma": True}
        self._spaces = {"hue_saturation": "HSP", "chroma_luma": "HSP"}
        self.init_ui()

    def init_ui(self) -> None:
        from src.features.color.core.warper_chroma709 import chroma_warp_position

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
        self.chroma_warp.white_point = chroma_warp_position(np.ones(3)).tolist()
        self.canvases = {"chroma_warp": self.chroma_warp, "hue_saturation": self.warper, "chroma_luma": self.chroma_luma}
        for mode in MODES:
            canvas = self.canvases[mode]
            self.canvas_stack.addWidget(canvas)
            canvas.ui_edited.connect(self._emit_ui)
            canvas.selection_changed.connect(self._selection_changed)
        self.warper.changed.connect(self._mesh_changed)
        self.warper.interaction_started.connect(self._legacy_start)
        self.warper.interaction_ended.connect(self._mesh_interaction_ended)
        self.warper.interaction_cancelled.connect(lambda: self._request_calibrated("cancel"))
        self.warper.interaction_cancelled.connect(lambda: self._request_palette("cancel"))
        self.warper.isolation_changed.connect(self._isolate_colors)
        self.chroma_luma.isolation_changed.connect(self._isolate_colors)
        self.chroma_luma.grid_activated.connect(self._grid_activated)
        self.chroma_luma.changed.connect(lambda data: self._request_palette("update"))
        self.chroma_luma.interaction_ended.connect(lambda: self._request_palette("commit"))
        self.chroma_luma.interaction_cancelled.connect(lambda: self._request_palette("cancel"))
        self.chroma_warp.edit_requested.connect(self._request_palette)
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
        header = create_panel_header()
        top = QHBoxLayout(header)
        top.setContentsMargins(8, 0, 3, 0)
        top.setSpacing(2)
        self.title = QLabel()
        self.title.setStyleSheet(SECTION_TITLE_STYLE)
        top.addWidget(self.title, 1)
        group = QButtonGroup(self)
        group.setExclusive(True)
        self.mode_buttons: dict[str, QToolButton] = {}
        for mode, icon in zip(MODES, ("chroma", "hs", "cl")):
            button = tool_button(icon, tr(mode, MODE_LABELS[mode]), True)
            button.setObjectName(f"warper_mode_{mode}")
            style_header_button(button, width=32)
            button.setIconSize(QSize(18, 18))
            button.setStyleSheet(button.styleSheet() +
                                 "QToolButton:checked { background: #393b42; border-radius: 3px; }")
            group.addButton(button)
            button.setChecked(mode == self.mode)
            button.clicked.connect(lambda checked=False, mode=mode: self._select_mode(mode))
            top.addWidget(button)
            self.mode_buttons[mode] = button
        self.picker_button = tool_button("picker", tr("viewer_picker", "Pick and drag colors in the viewer"), True)
        style_header_button(self.picker_button, width=32)
        self.picker_button.toggled.connect(self._viewer_picker_toggled)
        self.picker_button.hide()
        top.addWidget(self.picker_button)
        self.expand_button = tool_button("expand", tr("expand", "Expand"))
        style_header_button(self.expand_button, width=32)
        self.expand_button.clicked.connect(self.toggle_expanded)
        top.addWidget(self.expand_button)
        self.reset_btn = reset_button(tr("reset_all", "Reset All"), global_reset=True)
        self.reset_btn.clicked.connect(self.on_reset)
        top.addWidget(self.reset_btn)
        self.options_button = tool_button("more", tr("options", "Options"))
        style_header_button(self.options_button, width=32)
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
        self.upgrade_calibration_action = self.options_menu.addAction(
            tr("upgrade_hs_algorithm", "Upgrade Hue–Saturation Algorithm to v2"))
        self.upgrade_calibration_action.triggered.connect(self._upgrade_calibration)
        self.refine_calibration_action = self.options_menu.addAction(
            tr("upgrade_hs_algorithm_v3", "Upgrade Hue–Saturation Algorithm to v3"))
        self.refine_calibration_action.triggered.connect(self._refine_calibration)
        self.options_menu.addAction(tr("reset_selection", "Reset Selection / Pins"), lambda: self._mesh_action("reset_selection"))
        self.options_button.setMenu(self.options_menu)
        self.options_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        top.addWidget(self.options_button)
        layout.addWidget(header)

    @staticmethod
    def _combo(items: list[str], width: int) -> ChevronComboBox:
        combo = ChevronComboBox()
        combo.setStyleSheet(combo.styleSheet() + "QComboBox { padding-left: 6px; padding-right: 19px; }")
        combo.addItems(items)
        combo.setFixedWidth(width)
        return combo

    @staticmethod
    def _set_resolution_text(combo: ChevronComboBox, value: int) -> None:
        text = str(value)
        if combo.findText(text) < 0:
            combo.addItem(text)
        combo.setCurrentText(text)

    def _build_footer(self, layout: QVBoxLayout) -> None:
        self.footer = QWidget()
        self.footer.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._footer_layout = QVBoxLayout(self.footer)
        self._footer_layout.setContentsMargins(0, 0, 0, PARAMETER_BAR_BOTTOM_MARGIN)
        self._footer_layout.setSpacing(0)
        self._footer_layout.addWidget(create_panel_divider(Qt.Orientation.Horizontal))
        self._footer_row = QWidget()
        self._footer_row.setFixedHeight(PARAMETER_BAR_HEIGHT)
        self._footer_layout.addWidget(self._footer_row)
        footer = QHBoxLayout(self._footer_row)
        self._footer_controls = footer
        footer.setContentsMargins(6, 0, 6, 0)
        footer.setSpacing(2)
        self.resolution_h_icon = tool_button("resolution_h", tr("hue_resolution", "Hue Resolution"))
        self.resolution_s_icon = tool_button("resolution_s", tr("sat_resolution", "Saturation Resolution"))
        items = [str(size) for size in HS_RESOLUTIONS]
        self.grid_combo = self._combo(items, 48)
        self.saturation_combo = self._combo(items, 48)
        self.grid_combo.currentIndexChanged.connect(self.on_grid_size_changed)
        self.saturation_combo.currentIndexChanged.connect(lambda index: self._resolution_changed(1))
        for icon, combo in ((self.resolution_h_icon, self.grid_combo), (self.resolution_s_icon, self.saturation_combo)):
            icon.setFixedWidth(20)
            footer.addWidget(icon)
            footer.addWidget(combo)
            icon.clicked.connect(lambda checked=False, combo=combo: combo.showPopup())
        self.link_button = tool_button("link", tr("link", "Link"), True)
        self.link_button.setFixedWidth(24)
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
        axis_layout.setSpacing(2)
        self.axis_label = QLabel(tr("axis_angle", "Axis Angle"))
        self.axis_label.setStyleSheet(PARAMETER_LABEL_STYLE)
        axis_layout.addWidget(self.axis_label)
        self.axis_angle = DragValue("", -180, 180, 0, 2, compact=True)
        self.axis_angle.setFixedWidth(56)
        self.axis_angle.setAccessibleName(tr("axis_angle", "Axis Angle"))
        self.axis_angle.valueChanged.connect(self._axis_changed)
        self.axis_angle.interaction_started.connect(self._begin_palette_value)
        self.axis_angle.interaction_ended.connect(self._end_palette_value)
        axis_layout.addWidget(self.axis_angle)
        axis_spectrum = QWidget()
        axis_spectrum.setFixedHeight(2)
        axis_spectrum.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #ba1741, stop:0.25 #dfb63a, stop:0.5 #42baa5, stop:0.75 #2990c3, stop:1 #ba1741);")
        axis_group_layout.addWidget(axis_spectrum)
        footer.addWidget(self._axis_group)
        space_icon = tool_button("space", tr("color_space", "Color Space"))
        space_icon.setFixedWidth(20)
        footer.addWidget(space_icon)
        self.space_combo = self._combo(["HSV", "HSL", "HSY", "HSP"], 64)
        self.space_combo.setCurrentText("HSP")
        self.space_combo.setAccessibleName(tr("color_space", "Color Space"))
        self.space_combo.currentTextChanged.connect(self._space_changed)
        footer.addWidget(self.space_combo)
        space_icon.clicked.connect(lambda: self.space_combo.showPopup())
        layout.addWidget(self.footer)

    def _build_tools(self, body: QHBoxLayout) -> None:
        self._tools_divider = create_panel_divider(Qt.Orientation.Vertical)
        body.addWidget(self._tools_divider)
        self.tools_scroll = QScrollArea()
        self.tools_scroll.setObjectName("warperToolsScroll")
        self.tools_scroll.setFixedWidth(_TOOLS_SIDEBAR_WIDTH)
        self.tools_scroll.setWidgetResizable(True)
        self.tools_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.tools_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tools_scroll.setStyleSheet(f"QScrollArea#warperToolsScroll {{ background: {C.BG_PANEL}; border: none; }}")
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
        for key, value in self.stroke_tools.values.items():
            value.interaction_started.connect(self._begin_palette_value)
            value.interaction_ended.connect(self._end_palette_value)
            slider = self.stroke_tools.sliders[key]
            slider.sliderPressed.connect(self._begin_palette_value)
            slider.sliderReleased.connect(self._end_palette_value)
        self.tools_scroll.setWidget(self.tools_stack)
        body.addWidget(self.tools_scroll)

    def active_canvas(self) -> WarperCanvas | ChromaWarpCanvas:
        return self.canvases[self.mode]

    def _select_mode(self, mode: str) -> None:
        previous = self.mode
        self.set_mode(mode)
        if (self._saved_warper is not None
                and (previous == "chroma_warp") != (mode == "chroma_warp")):
            saved = self._saved_warper.for_mode(mode)
            if saved is None and mode != "chroma_warp":
                sibling = "chroma_luma" if mode == "hue_saturation" else "hue_saturation"
                saved = self._saved_warper.for_mode(sibling)
            self._request_palette("atomic", saved=saved)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"Unknown Color Warper mode: {mode}")
        previous = self.active_canvas()
        previous.cancel_gesture()
        previous.set_viewer_sample(None)
        self._cancel_viewer_drag()
        self._end_palette_value()
        self.mode = mode
        self._updating = True
        try:
            self.mode_buttons[mode].setChecked(True)
            self.canvas_stack.setCurrentWidget(self.canvases[mode])
            self.tools_stack.setCurrentWidget(self.tools[mode])
            self.tools_stack.updateGeometry()
            scrollbar_width = self.tools_scroll.verticalScrollBar().sizeHint().width()
            content_width = self.tools[mode].minimumSizeHint().width()
            self.tools_scroll.setFixedWidth(max(_TOOLS_SIDEBAR_WIDTH, content_width + scrollbar_width))
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
                sizes = (4, *HS_RESOLUTIONS) if rectangular else HS_RESOLUTIONS
                items = [str(size) for size in sizes]
                for combo, value in zip((self.grid_combo, self.saturation_combo), self._resolutions[mode]):
                    combo.clear()
                    combo.addItems(items)
                    self._set_resolution_text(combo, value)
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
            self._sync_calibration_controls()
            self._sync_tools()
        finally:
            self._updating = False
        self._adapt_footer()
        self._refresh_density()
        self._emit_ui()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_axis_group"):
            self._adapt_footer()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.surface and event.type() == QEvent.Type.Resize and hasattr(self, "_axis_group"):
            self._adapt_footer()
        if watched is self._viewer_viewport and self.isVisible() and self.picker_button.isChecked():
            if event.type() in (QEvent.Type.UngrabMouse, QEvent.Type.WindowDeactivate):
                self._cancel_viewer_drag()
            if event.type() == QEvent.Type.Leave:
                self.active_canvas().set_viewer_sample(None)
            if isinstance(event, QMouseEvent) and self._viewer_mouse(event):
                return True
        return super().eventFilter(watched, event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._viewer_refresh is not None:
            self._viewer_received = None
            QTimer.singleShot(0, self._request_viewer_refresh)

    def hideEvent(self, event: QHideEvent) -> None:
        self._cancel_viewer_drag()
        self.active_canvas().cancel_gesture()
        self._isolate_colors(None)
        self.active_canvas().set_viewer_sample(None)
        super().hideEvent(event)

    def bind_viewer(self, viewer: QGraphicsView, refresh: Callable[[], None], *,
                    available: Callable[[], bool] | None = None,
                    overlay_changed: Callable[[], None] | None = None) -> None:
        self._viewer = viewer
        self._viewer_viewport = viewer.viewport()
        self._viewer_viewport.setMouseTracking(True)
        self._viewer_refresh = refresh
        self._viewer_overlay_changed = overlay_changed
        self._viewer_available = available
        self._viewer_viewport.installEventFilter(self)
        viewer.destroyed.connect(self._forget_viewer)
        self.picker_button.show()

    def _forget_viewer(self) -> None:
        self._viewer = None
        self._viewer_viewport = None
        self._viewer_refresh = None
        self._viewer_overlay_changed = None
        self._viewer_available = None
        self._isolation_item = None
        self._isolation = None
        self._cancel_viewer_drag()

    def _cancel_viewer_drag(self) -> None:
        if self._viewer_drag is not None:
            _, _, canvas = self._viewer_drag
            self._viewer_drag = None
            canvas.cancel_gesture()

    def _request_viewer_refresh(self) -> None:
        if self._viewer_refresh is not None and self.isVisible():
            self._viewer_refresh()

    def request_viewer_input(self, node_id: str, source: tuple, *, revision: tuple = ()) -> tuple | None:
        if not self.isVisible() or not (self.density_action.isChecked() or self.picker_button.isChecked()
                                        or self._isolation is not None):
            return None
        target = (node_id, source, revision)
        if target == self._viewer_request:
            return target if target != self._viewer_received else None
        previous = self._viewer_request
        if previous is not None and previous[:2] != target[:2]:
            self._clear_viewer_input()
        elif previous is not None and previous[2][1:] != revision[1:]:
            self.active_canvas().set_viewer_sample(None)
            self._show_isolation(False)
        self._viewer_request = target
        return target

    def _clear_viewer_input(self) -> None:
        self._viewer_request = self._viewer_received = self._viewer_input = None
        self._cancel_viewer_drag()
        self._isolate_colors(None)
        self.active_canvas().set_viewer_sample(None)
        self._refresh_density()

    def accept_viewer_input(self, sample: tuple) -> None:
        request, rect, encoded, rgb = sample
        if request != self._viewer_request or request == self._viewer_received or rect is None:
            return
        self._viewer_received = request
        self._viewer_input = rect, encoded, rgb
        self._refresh_density()
        if self._isolation is not None:
            self._paint_isolation()

    def _viewer_picker_toggled(self, enabled: bool) -> None:
        if not enabled:
            self.active_canvas().set_viewer_sample(None)
            self._cancel_viewer_drag()
        if enabled and self._viewer_input is None and self._viewer_refresh is not None:
            self._viewer_refresh()

    def _plot_positions(self, rgb: np.ndarray, encoded: bool) -> np.ndarray:
        from src.features.color.core.warper_density import warper_plot_positions

        return warper_plot_positions(rgb, self.mode, angle=self.chroma_luma.axis_angle,
                                     color_space=self._spaces.get(self.mode, "HSP").lower(), encoded_input=encoded)

    def _refresh_density(self) -> None:
        from src.features.color.core.warper_density import plot_density_rgba

        canvas = self.active_canvas()
        images = []
        if self._viewer_input is not None and self.density_action.isChecked():
            _, encoded, rgb = self._viewer_input
            points = self._plot_positions(rgb, encoded)
            for index in range(points.shape[1]):
                pixels = plot_density_rgba(points[:, index])
                height, width = pixels.shape[:2]
                images.append(QImage(pixels.data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy())
        if isinstance(canvas, WarperCanvas):
            canvas.density_overlays = tuple(images)
            canvas.update()
        else:
            canvas.set_density_image(images[0] if images else None)

    def _isolate_colors(self, key: PointKey | None) -> None:
        self._isolation = key
        if key is None:
            self._show_isolation(False)
        elif self._viewer_input is None or self._viewer_received != self._viewer_request:
            self._request_viewer_refresh()
        else:
            self._paint_isolation()

    def _paint_isolation(self) -> None:
        canvas = self.active_canvas()
        if self._isolation is None or self._viewer is None or not isinstance(canvas, WarperCanvas):
            return
        if self._viewer_available is not None and not self._viewer_available():
            return
        rect, encoded, rgb = self._viewer_input
        if self._isolation_item is None:
            self._isolation_item = QGraphicsPixmapItem()
            self._isolation_item.setZValue(100)
            self._isolation_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self._isolation_item.setVisible(False)
            QGraphicsView.scene(self._viewer).addItem(self._isolation_item)
        points = self._plot_positions(rgb, encoded)[:, canvas.active_grid]
        affected = canvas.affected_positions(self._isolation, points).reshape(rgb.shape[:2])
        height, width = affected.shape
        mask = np.zeros((height, width, 4), dtype=np.uint8)
        mask[..., 3] = np.where(affected, 0, 255)
        image = QImage(mask.data, width, height, width * 4, QImage.Format.Format_RGBA8888)
        self._isolation_item.setPixmap(QPixmap.fromImage(image))
        self._isolation_item.setPos(rect[0], rect[1])
        self._isolation_item.setTransform(QTransform.fromScale(rect[2] / width, rect[3] / height))
        self._show_isolation(True)

    def _show_isolation(self, visible: bool) -> None:
        if self._isolation_item is not None and self._isolation_item.isVisible() != visible:
            self._isolation_item.setVisible(visible)
            if self._viewer_overlay_changed is not None:
                self._viewer_overlay_changed()

    def _viewer_mouse(self, event: QMouseEvent) -> bool:
        if self._viewer_available is not None and not self._viewer_available():
            self._cancel_viewer_drag()
            return False
        if self._viewer_drag is not None and event.type() in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease):
            origin, plot, canvas = self._viewer_drag
            finishing = event.type() == QEvent.Type.MouseButtonRelease
            if not finishing and not event.buttons() & Qt.MouseButton.LeftButton:
                self._cancel_viewer_drag()
                return False
            if not finishing or event.button() == Qt.MouseButton.LeftButton:
                if finishing:
                    self._viewer_drag = None
                canvas.move_viewer_drag(plot + event.position() - origin, finish=finishing)
                return True
        click = event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton
        hover = event.type() == QEvent.Type.MouseMove and event.buttons() == Qt.MouseButton.NoButton
        if not (click or hover) or self._viewer is None:
            return False
        canvas = self.active_canvas()
        if self._viewer_input is None or self._viewer_received != self._viewer_request:
            canvas.set_viewer_sample(None)
            return False
        (left, top, width, height), encoded, rgb = self._viewer_input
        point = self._viewer.mapToScene(event.position().toPoint())
        if width <= 0 or height <= 0 or not self._viewer.sceneRect().contains(point):
            canvas.set_viewer_sample(None)
            return False
        x, y = (point.x() - left) / width, (point.y() - top) / height
        if not 0 <= x < 1 or not 0 <= y < 1:
            canvas.set_viewer_sample(None)
            return False
        pixel = rgb[int(y * rgb.shape[0]), int(x * rgb.shape[1])]
        if not np.isfinite(pixel).all():
            return False
        points = self._plot_positions(pixel[None, :], encoded)[0]
        index = canvas.active_grid if isinstance(canvas, WarperCanvas) else 0
        if hover:
            canvas.set_viewer_sample(points[index])
            return False
        pin = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        origin = canvas.begin_viewer_drag(points[index], pin=pin)
        if origin is not None:
            self._viewer_drag = event.position(), origin, canvas
        return True

    def _adapt_footer(self) -> None:
        if not hasattr(self, "tools_scroll"):
            return
        canvas_width = self.surface.width() - self.tools_scroll.width() - self._tools_divider.width()
        inline_width = self._footer_controls.minimumSize().width()
        if self._footer_narrow:
            # The wrapped axis controls currently live outside the main row.
            inline_width += self._axis_group.minimumSizeHint().width() + self._footer_controls.spacing()
        narrow = self.mode == "chroma_luma" and canvas_width < inline_width
        if narrow == self._footer_narrow:
            return
        self._footer_narrow = narrow
        self._footer_controls.removeWidget(self._axis_group)
        self._footer_layout.removeWidget(self._axis_group)
        if narrow:
            self._footer_layout.insertWidget(0, self._axis_group, 0, Qt.AlignmentFlag.AlignCenter)
        else:
            self._footer_controls.insertWidget(self._footer_controls.count() - 2, self._axis_group,
                                              0, Qt.AlignmentFlag.AlignVCenter)

    def ui_state(self) -> dict[str, Any]:
        return {"version": 1, "mode": self.mode, "linked": dict(self._linked),
                "show_grid": self.grid_action.isChecked(), "show_distribution": self.density_action.isChecked(),
                "modes": {mode: canvas.snapshot() for mode, canvas in self.canvases.items()}}

    def _emit_ui(self) -> None:
        if not self._updating:
            self.ui_state_changed.emit(self.ui_state())

    def _request(self, action: str, **payload: Any) -> None:
        if not self._updating and not (
                (self._palette_hosted and (self.mode != "hue_saturation" or self._uses_palette_hs())) or
                self._calibration_algorithm or (self.mode == "hue_saturation" and self.warper.calibrated_hs)):
            self.command_requested.emit({"mode": self.mode, "action": action, "payload": payload,
                                         "state": self.active_canvas().snapshot()})

    def _request_calibrated(self, phase: str, offsets: np.ndarray | None = None, *, upgrade: bool = False) -> None:
        if self._updating:
            return
        payload: dict[str, Any] = {"phase": phase}
        if upgrade:
            payload["upgrade_to_v2"] = True
        elif phase in {"atomic", "update"}:
            payload["enabled"] = self.warper.calibrated_hs
            if self.warper.calibrated_hs:
                payload["algorithm_version"] = self._calibration_algorithm or RESOLVE_HS_ALGORITHM_V3
                if offsets is None:
                    offsets = self.warper.calibrated_offsets()
                white = self._white_point()
                if self._calibration_algorithm is None and not (offsets.any() or self.warper.pins or any(white)):
                    if self._pending_mesh_edit:
                        self._request_calibrated("cancel")
                    return
                self._pending_mesh_edit = phase == "update"
                payload["offsets"] = offsets.tolist()
                payload["pins"] = [make_point_id(HS_GRID_NAME, *key) for key in sorted(self.warper.pins)]
                payload["white_point"] = white
        elif phase in {"commit", "cancel"}:
            if not self._pending_mesh_edit:
                return
            self._pending_mesh_edit = False
        self.command_requested.emit({"mode": "hue_saturation", "action": "calibrated_mesh",
                                     "payload": payload})

    def _request_palette(self, phase: str, *, saved: WarperStateV2 | None = None) -> None:
        if self._updating or not self._palette_hosted:
            return
        payload: dict[str, Any] = {"phase": phase}
        if phase in {"update", "atomic"}:
            state = saved or self._palette_state(preview=phase == "update")
            payload["state"] = state.to_dict()
            self._pending_palette_edit = phase == "update"
        elif not self._pending_palette_edit:
            return
        else:
            self._pending_palette_edit = False
        self.command_requested.emit({"mode": self.mode, "action": "palette_edit", "payload": payload})

    def _begin_palette_value(self) -> None:
        self._numeric_palette_edit = True

    def _end_palette_value(self) -> None:
        if self._numeric_palette_edit:
            self._numeric_palette_edit = False
            self._request_palette("commit")

    def _sync_calibration_controls(self) -> None:
        calibrated = self.warper.calibrated_hs
        self.upgrade_calibration_action.setVisible(
            self.mode == "hue_saturation" and self._calibration_algorithm == RESOLVE_HS_ALGORITHM)
        self.refine_calibration_action.setVisible(
            self.mode == "hue_saturation" and self._calibration_algorithm == RESOLVE_HS_ALGORITHM_V2)
        self.space_combo.setEnabled(self._palette_hosted or self.mode != "hue_saturation" or not calibrated)
        for key, widget in self.hs_tools.values.items():
            blocked = widget.blockSignals(True)
            maximum = CALIBRATED_MAX_RADIUS * CALIBRATED_SAT_SCALE if calibrated and key == "sat" else 1.0
            widget.setRange(0.0, maximum)
            widget.setDecimals(2 if calibrated else 3)
            widget.blockSignals(blocked)

    def refresh_automatic_calibration(self) -> None:
        if not self._allow_automatic_calibration:
            return
        enabled = bool(self._use_measured_warper and self._use_measured_warper())
        if enabled == self.warper.calibrated_hs:
            return
        self.warper.cancel_gesture()
        self.set_params(None, target_id=self._warper_target_id)

    def confirm_mesh_edit(self, state: WarperStateV2 | None = None) -> None:
        self._allow_automatic_calibration = False
        if self._palette_hosted:
            self._saved_warper = state
        if state is not None:
            hue = state.for_mode("hue_saturation")
            if hue is not None and hue.algorithm_version in RESOLVE_HS_ALGORITHMS:
                self._calibration_algorithm = hue.algorithm_version
        self._sync_calibration_controls()

    def _sync_hs_space(self) -> None:
        self._spaces["hue_saturation"] = self.warper.color_space = (
            "HSP" if self.warper.calibrated_hs else "HSV")
        if self.mode == "hue_saturation":
            self.space_combo.setCurrentText(self.warper.color_space)
        self._sync_calibration_controls()
        self._sync_tools()

    def _upgrade_calibration(self) -> None:
        if self._updating or self.mode != "hue_saturation":
            return
        self.warper._end_edit()
        if self._calibration_algorithm != RESOLVE_HS_ALGORITHM:
            return
        self._calibration_algorithm = RESOLVE_HS_ALGORITHM_V2
        self._sync_calibration_controls()
        self._request_calibrated("atomic", upgrade=True)
        self._emit_ui()

    def _refine_calibration(self) -> None:
        if self._updating or self.mode != "hue_saturation" or self._calibration_algorithm != RESOLVE_HS_ALGORITHM_V2:
            return
        self.warper._end_edit()
        self.command_requested.emit({"mode": self.mode, "action": "calibrated_mesh",
                                     "payload": {"phase": "atomic", "upgrade_to_v3": True}})

    def _mesh_changed(self, data: np.ndarray) -> None:
        if self.warper.calibrated_hs:
            self._request_calibrated("update", data)
        elif self._uses_palette_hs():
            self._request_palette("update")
        elif not self._updating and self._legacy_supported():
            self.warper_changed.emit(data)

    def _legacy_supported(self) -> bool:
        return (self.mode == "hue_saturation" and not self.warper.calibrated_hs
                and not self._uses_palette_hs()
                and self.warper.ring_count == self.warper.grid_size
                and self._spaces["hue_saturation"] == "HSV")

    def _uses_palette_hs(self) -> bool:
        return self._palette_hosted and not self.warper.calibrated_hs

    def _legacy_start(self) -> None:
        if self._legacy_supported() and not self._updating:
            self._legacy_interaction_open = True
            self.interaction_started.emit()

    def _mesh_interaction_ended(self) -> None:
        if self.warper.calibrated_hs:
            self._request_calibrated("commit")
        elif self._uses_palette_hs():
            self._request_palette("commit")
        if self._legacy_interaction_open:
            self._legacy_interaction_open = False
            self.interaction_ended.emit()

    def _numeric_start(self, component: str) -> None:
        if (self.mode == "hue_saturation" and component == "luma"
                and not (self.warper.calibrated_hs or self._uses_palette_hs())):
            return
        canvas = self.active_canvas()
        if isinstance(canvas, WarperCanvas):
            canvas.begin_component_drag()

    def _numeric_end(self, component: str) -> None:
        if (self.mode == "hue_saturation" and component == "luma"
                and not (self.warper.calibrated_hs or self._uses_palette_hs())):
            return
        canvas = self.active_canvas()
        if isinstance(canvas, WarperCanvas):
            canvas.end_component_drag()

    def on_grid_size_changed(self, index: int) -> None:
        self._resolution_changed(0)

    def _resolution_changed(self, axis: int) -> None:
        if self._updating or self.mode == "chroma_warp":
            return
        combos = (self.grid_combo, self.saturation_combo)
        value = int(combos[axis].currentText())
        canvas = self.active_canvas()
        canvas._end_edit()
        self._updating = True
        try:
            if self.link_button.isChecked():
                self._set_resolution_text(combos[1 - axis], value)
            sizes = tuple(int(combo.currentText()) for combo in combos)
            if sizes == self._resolutions[self.mode]:
                return
            self._resolutions[self.mode] = sizes
            if canvas is self.warper and (canvas.calibrated_hs or self._uses_palette_hs()):
                white = self._white_point()
                self._hydrate_grid(resample_calibrated_grid(self._calibrated_grid(), sizes[1], sizes[0]))
                self._set_white_point(white)
            elif canvas is self.chroma_luma and self._palette_hosted:
                grids = self._chroma_grids()
                self._set_chroma_grids({name: resample_grid(grid, sizes[1] + 1, sizes[0] + 1)
                                        for name, grid in grids.items()})
            else:
                canvas.set_resolution(*sizes)
        finally:
            self._updating = False
        if self._legacy_supported():
            self.warper_changed.emit(None)
        self._sync_tools()
        if canvas.calibrated_hs:
            self._request_calibrated("atomic")
        elif self._palette_hosted:
            self._request_palette("atomic")
        else:
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
        if self._palette_hosted and self.mode == "hue_saturation":
            self.warper.calibrated_hs = bool(value == "HSP" and self._use_measured_warper
                                             and self._use_measured_warper())
            if self.warper.calibrated_hs and self._calibration_algorithm is None:
                self._calibration_algorithm = RESOLVE_HS_ALGORITHM_V3
            elif not self.warper.calibrated_hs:
                self._calibration_algorithm = None
            self._allow_automatic_calibration = False
        self._sync_calibration_controls()
        if self._palette_hosted:
            self._request_palette("atomic")
        else:
            self._request("color_space", value=value)
        self._emit_ui()

    def _axis_changed(self, value: float) -> None:
        if self._updating:
            return
        self.chroma_luma.axis_angle = value
        self.chroma_luma.update()
        self._refresh_density()
        if self._palette_hosted:
            self._request_palette("update" if self._numeric_palette_edit else "atomic")
        else:
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
            self.stroke_tools.sync_selection(canvas.selected is not None, parameters,
                                             pin=canvas.selected is not None and canvas.selected[0] == "pin")

    def _stroke_action(self, action: str) -> None:
        if action == "delete":
            self.chroma_warp.delete_selection()
        elif action == "reset_pin" and self.chroma_warp.selected is not None:
            kind, _ = self.chroma_warp.selected
            fields = ("chroma_range",) if kind == "pin" else ("chroma_range", "exposure")
            for key in fields:
                self.chroma_warp.set_parameter(key, self.stroke_tools.values[key].defaultValue())
            self._sync_tools()
            self._request_palette("atomic")
        if not self._palette_hosted:
            self._request(action)

    def _stroke_parameter(self, key: str, value: float | str) -> None:
        if self._updating:
            return
        self.chroma_warp.set_parameter(key, value)
        self._sync_tools()
        if self._palette_hosted:
            self._request_palette("update" if self._numeric_palette_edit else "atomic")
        else:
            self._request("stroke_parameter", key=key, value=value)

    def _display_changed(self, checked: bool) -> None:
        for canvas in self.canvases.values():
            canvas.show_grid = self.grid_action.isChecked()
            canvas.show_density = self.density_action.isChecked()
            canvas.update()
        self._refresh_density()
        if self.density_action.isChecked() and self._viewer_input is None and self._viewer_refresh is not None:
            QTimer.singleShot(0, self._request_viewer_refresh)
        self._emit_ui()

    def on_reset(self) -> None:
        self.active_canvas().reset()
        if self._legacy_supported():
            self.warper_changed.emit(None)
            self.reset_clicked.emit()
        self._sync_tools()
        if self.mode == "hue_saturation" and self.warper.calibrated_hs:
            self._request_calibrated("atomic")
        elif self._palette_hosted:
            self._request_palette("atomic")
        else:
            self._request("reset")
        self._emit_ui()

    def _hydrate_grid(self, grid: WarperGrid) -> None:
        self.warper.grid_size, self.warper.ring_count = grid.cols, grid.rows
        self.warper.reset_grid()
        offsets = grid.offsets
        if grid.components == 2:
            offsets = np.pad(offsets, ((0, 0), (0, 0), (0, 1)))
        self.warper.apply_calibrated_offsets(offsets)
        for pin in grid.pins:
            parts = pin.point_id.rsplit(":", 2)
            if len(parts) == 3 and parts[0] == grid.name and parts[1].isdigit() and parts[2].isdigit():
                row, col = map(int, parts[1:])
                if row < grid.rows and col < grid.cols:
                    self.warper.pins.add((row, col))

    def _calibrated_grid(self) -> WarperGrid:
        canvas = self.warper
        grid = WarperGrid.identity(HS_GRID_NAME, canvas.ring_count, canvas.grid_size, ("dh", "ds", "dl"))
        pins = tuple(WarperPin(make_point_id(HS_GRID_NAME, *key)) for key in sorted(canvas.pins))
        return replace(grid.with_offsets(canvas.calibrated_offsets()), pins=pins)

    def _white_point(self) -> tuple[float, float, float]:
        x, y = map(float, self.warper.white_point)
        return x, y, self.warper.luma_values.get(WHITE_POINT, 0.5) - 0.5

    def _set_white_point(self, value: tuple[float, float, float]) -> None:
        self.warper.white_point[:] = value[:2]
        if value[2]:
            self.warper.luma_values[WHITE_POINT] = value[2] + 0.5
        else:
            self.warper.luma_values.pop(WHITE_POINT, None)
        self.warper.update()

    def _chroma_grids(self) -> dict[str, WarperGrid]:
        canvas = self.chroma_luma
        shape = (canvas.ring_count + 1, canvas.grid_size + 1)
        x, y = np.meshgrid(np.linspace(-1, 1, shape[1]), np.linspace(-1, 1, shape[0]))
        identity = np.stack((x, y), axis=-1).astype(np.float32)
        grids = {}
        for index, name in enumerate(CL_GRID_NAMES):
            if index == canvas.active_grid:
                points, pins = canvas.points, canvas.pins
            else:
                points, _, pins = canvas._grid_states.get(index, (identity, set(), set()))
            offsets = ((points - identity) * (0.5, -0.5))[::-1].astype(np.float32)
            offsets[np.abs(offsets) < 1e-7] = 0.0
            cl = self._saved_warper.for_mode("chroma_luma") if self._saved_warper is not None else None
            previous = cl.grids.get(name) if cl is not None else None
            old_pins = {pin.point_id: pin for pin in previous.pins} if previous is not None else {}
            pin_ids = (make_point_id(name, shape[0] - 1 - row, col) for row, col in sorted(pins))
            grid = WarperGrid.identity(name, *shape, ("dc", "dl"))
            grids[name] = replace(grid.with_offsets(offsets),
                                  pins=tuple(old_pins.get(pid) or WarperPin(pid) for pid in pin_ids),
                                  _extra_fields=previous._extra_fields if previous is not None else {})
        return grids

    def _set_chroma_grids(self, grids: dict[str, WarperGrid]) -> None:
        canvas = self.chroma_luma
        first = grids[CL_GRID_NAMES[0]]
        canvas.set_resolution(first.cols - 1, first.rows - 1)
        original = canvas.points.copy()
        for index, name in enumerate(CL_GRID_NAMES):
            grid = grids[name]
            offsets = grid.offsets
            if offsets.shape[:2] != original.shape[:2]:
                grid = resample_grid(grid, first.rows, first.cols)
                offsets = grid.offsets
            points = (original + offsets[::-1] * (2.0, -2.0)).astype(np.float32)
            pins = set()
            for pin in grid.pins:
                parts = pin.point_id.rsplit(":", 2)
                if len(parts) == 3 and parts[0] == name and parts[1].isdigit() and parts[2].isdigit():
                    row, col = map(int, parts[1:])
                    if 0 <= row < first.rows and 0 <= col < first.cols:
                        pins.add((first.rows - 1 - row, col))
            if index == canvas.active_grid:
                canvas.points, canvas.pins = points, pins
            else:
                canvas._grid_states[index] = (points, set(), pins)
        canvas.update()

    def _palette_state(self, *, preview: bool) -> WarperStateV2:
        previous = self._saved_warper.for_mode(self.mode) if self._saved_warper is not None else None
        extras = previous._extra_fields if previous is not None else {}
        if self.mode == "hue_saturation":
            if self.warper.calibrated_hs:
                grid = self._calibrated_grid()
                version = self._calibration_algorithm or RESOLVE_HS_ALGORITHM_V3
            else:
                canvas = self.warper
                offsets = canvas.calibrated_offsets()
                version = HS_SPACES709_ALGORITHM
                semantics = ("dh", "ds", "dl")
                old_grid = previous.grids[HS_GRID_NAME] if previous is not None else None
                if (previous is not None and previous.algorithm_version in (LEGACY_ALGORITHM, CURRENT_ALGORITHM)
                        and previous.color_space == canvas.color_space.lower() and not offsets[..., 2].any()):
                    version, semantics = previous.algorithm_version, ("dh", "ds")
                grid = WarperGrid.identity(HS_GRID_NAME, canvas.ring_count, canvas.grid_size, semantics)
                old_pins = {pin.point_id: pin for pin in old_grid.pins} if old_grid is not None else {}
                pin_ids = (make_point_id(HS_GRID_NAME, *key) for key in sorted(canvas.pins))
                grid = replace(grid.with_offsets(offsets[..., :len(semantics)]),
                               pins=tuple(old_pins.get(pid) or WarperPin(pid, _extra_fields={"affects_response": True})
                                          for pid in pin_ids),
                               _extra_fields=old_grid._extra_fields if old_grid is not None else {})
            return WarperStateV2(WARPER_SCHEMA_VERSION, version, self.mode,
                                 self.warper.color_space.lower(), 0.0, {HS_GRID_NAME: grid}, _extra_fields=extras,
                                 white_point=self._white_point())
        if self.mode == "chroma_luma":
            extras = {**extras, "legacy_hs_encoded": bool(self._use_measured_warper and self._use_measured_warper())}
            return WarperStateV2(WARPER_SCHEMA_VERSION, CHROMA709_ALGORITHM, self.mode,
                                 self.chroma_luma.color_space.lower(), self.chroma_luma.axis_angle,
                                 self._chroma_grids(), _extra_fields=extras)

        from src.features.color.core.warper_chroma709 import chroma_warp_color

        canvas = self.chroma_warp.snapshot(preview=preview)
        previous_strokes = {} if previous is None else {stroke.stroke_id: stroke for stroke in previous.strokes}
        previous_pins = {} if previous is None else {pin.pin_id: pin for pin in previous.pin_points}

        def params(item: dict[str, Any], previous_params: dict[str, float] | None) -> dict[str, float]:
            values = item["parameters"]
            return {**(previous_params or {}), "chroma_range": values["chroma_range"], "tonal_low": values["tonal_low"],
                    "tonal_high": values["tonal_high"], "pivot": values["tonal_pivot"],
                    "exposure": values["exposure"]}

        strokes = []
        for item in canvas["strokes"]:
            path = tuple(map(tuple, item["path"]))
            source, target = map(tuple, chroma_warp_color(np.asarray((path[0], path[-1]))))
            old = previous_strokes.get(item["id"])
            strokes.append(WarperStroke(item["id"], item["parameters"]["stroke_mode"], source, target,
                                        params(item, old.params if old else None), path,
                                        _extra_fields=old._extra_fields if old else {}))
        pins = []
        for item in canvas["pins"]:
            old = previous_pins.get(item["id"])
            pins.append(WarperPinPoint(item["id"], tuple(item["position"]), params(item, old.params if old else None),
                                       _extra_fields=old._extra_fields if old else {}))
        return WarperStateV2(WARPER_SCHEMA_VERSION, CHROMA709_ALGORITHM, self.mode, "hsp", 0.0, {},
                             tuple(strokes), tuple(pins), _extra_fields=extras)

    def load_layer_state(self, layer: Layer) -> None:
        self._cancel_viewer_drag()
        self._palette_hosted = True
        self.warper.persistent_mesh = True
        state = layer.warper_state
        if self.set_warper_state(state, target_id=layer.id):
            return
        if layer.id != getattr(self, "_warper_target_id", None):
            self._clear_viewer_input()
        self.set_params(layer.warper_params, target_id=layer.id, preserve_legacy=(
            state is not None or layer.warper_state_unparsed is not None))
        if layer.warper_params is not None and layer.warper_space == 0:
            state = legacy_from_ndarray(layer.warper_params)
        self._load_chroma_modes(state)

    def set_warper_state(self, state: WarperStateV2 | None, *, target_id: str | None = None) -> bool:
        """Hydrate saved palettes without emitting grading commands."""
        if state is None:
            return False
        hs = state.for_mode("hue_saturation")
        same_target = target_id is not None and target_id == getattr(self, "_warper_target_id", None)
        if not same_target:
            self._clear_viewer_input()
        if hs is None:
            self.set_params(None, target_id=target_id)
        elif not self._hydrate_hs(hs, target_id=target_id, same_target=same_target):
            return False
        self._load_chroma_modes(state, keep_selection=same_target)
        if ((self.mode == "chroma_warp") != (state.mode == "chroma_warp")
                or (not same_target and state.mode == "chroma_luma")):
            self.set_mode(state.mode)
        return True

    def _hydrate_hs(self, state: WarperStateV2, *, target_id: str | None, same_target: bool) -> bool:
        grid = state.grids[HS_GRID_NAME]
        calibrated = state.algorithm_version in RESOLVE_HS_ALGORITHMS
        if not calibrated and state.algorithm_version != HS_SPACES709_ALGORITHM and grid.components != 2:
            return False
        space = state.color_space.upper()
        keep_selection = same_target and (self.warper.ring_count, self.warper.grid_size) == (grid.rows, grid.cols)
        selected = set(self.warper.selected) if keep_selection else set()
        self._updating = True
        try:
            self.warper.cancel_gesture()
            self._pending_mesh_edit = False
            self._allow_automatic_calibration = False
            self.warper.calibrated_hs = calibrated
            self._calibration_algorithm = state.algorithm_version if calibrated else None
            self._spaces["hue_saturation"] = self.warper.color_space = space
            self._resolutions["hue_saturation"] = (grid.cols, grid.rows)
            if grid.cols != grid.rows:
                self._linked["hue_saturation"] = False
            self._hydrate_grid(grid)
            self._set_white_point(state.white_point)
            self.warper.selected = selected
            self._warper_target_id = target_id
            if self.mode == "hue_saturation":
                self._set_resolution_text(self.grid_combo, grid.cols)
                self._set_resolution_text(self.saturation_combo, grid.rows)
                self.space_combo.setCurrentText(space)
                self.link_button.setChecked(self._linked["hue_saturation"])
            self._sync_calibration_controls()
            self._sync_tools()
        finally:
            self._updating = False
        return True

    def _load_chroma_modes(self, state: WarperStateV2 | None, *, keep_selection: bool = False) -> None:
        self._updating = True
        try:
            self._pending_palette_edit = self._numeric_palette_edit = False
            self._saved_warper = state
            cl = state.for_mode("chroma_luma") if state is not None else None
            cw = state.for_mode("chroma_warp") if state is not None else None
            canvas = self.chroma_luma
            canvas.cancel_gesture()
            old_size = (canvas.ring_count, canvas.grid_size)
            selections = {index: selected for index, (_, selected, _) in canvas._grid_states.items()}
            selections[canvas.active_grid] = canvas.selected.copy()
            if not keep_selection:
                canvas.active_grid = 0
            if cl is None:
                canvas.set_resolution(6, 6)
            else:
                self._set_chroma_grids(cl.grids)
                if keep_selection and old_size == (canvas.ring_count, canvas.grid_size):
                    canvas.selected = selections.get(canvas.active_grid, set())
                    for index, (points, _, pins) in canvas._grid_states.items():
                        canvas._grid_states[index] = (points, selections.get(index, set()), pins)
            canvas.axis_angle = cl.axis_angle if cl is not None else 0.0
            canvas.color_space = cl.color_space.upper() if cl is not None else "HSP"
            self._spaces["chroma_luma"] = canvas.color_space
            self._resolutions["chroma_luma"] = (canvas.grid_size, canvas.ring_count)
            self._linked["chroma_luma"] = canvas.grid_size == canvas.ring_count
            self.axis_angle.setValue(canvas.axis_angle)
            if self.mode == "chroma_luma":
                self._set_resolution_text(self.grid_combo, canvas.grid_size)
                self._set_resolution_text(self.saturation_combo, canvas.ring_count)
                self.space_combo.setCurrentText(canvas.color_space)
                self.link_button.setChecked(self._linked["chroma_luma"])

            warp = self.chroma_warp
            selected_item = None
            if keep_selection and warp.selected is not None:
                kind, index = warp.selected
                selected_item = (kind, (warp.strokes if kind == "stroke" else warp.pins)[index]["id"])
            warp.reset()
            if cw is not None:
                from src.features.color.core.warper_chroma709 import chroma_warp_position
                from src.features.color.core.warper_state import default_stroke_params

                def controls(params: dict[str, float] | None, mode: str = "normal") -> dict[str, float | str]:
                    values = {**default_stroke_params(), **(params or {})}
                    values["tonal_pivot"] = values.pop("pivot")
                    return {**values, "stroke_mode": mode}

                for stroke in cw.strokes:
                    path = stroke.path or chroma_warp_position(np.asarray([stroke.source_rgb, stroke.target_rgb]))
                    warp.strokes.append({"id": stroke.stroke_id, "path": [list(p) for p in path],
                                         "parameters": controls(stroke.params, stroke.mode),
                                         "mode": "point_stroke" if stroke.mode == "point_to_point" else "stroke"})
                for pin in cw.pin_points:
                    warp.pins.append({"id": pin.pin_id, "position": list(pin.position),
                                      "parameters": controls(pin.params)})
                if warp.strokes:
                    warp.parameters = dict(warp.strokes[0]["parameters"])
            if selected_item is not None:
                kind, item_id = selected_item
                items = warp.strokes if kind == "stroke" else warp.pins
                warp.selected = next(((kind, index) for index, item in enumerate(items) if item["id"] == item_id), None)
            self.cl_tools.grid_buttons[canvas.active_grid].setChecked(True)
            self._sync_tools()
            self._refresh_density()
        finally:
            self._updating = False

    def set_params(self, grid_data: np.ndarray | None, emit: bool = False, *,
                   target_id: str | None = None, preserve_legacy: bool = False) -> None:
        """Hydrate legacy HS without stale drag samples or recursive edits."""
        if grid_data is not None:
            data = np.asarray(grid_data)
            if data.ndim != 3 or data.shape[0] != data.shape[1] or data.shape[2] != 2 or not np.isfinite(data).all():
                raise ValueError("Expected a finite square Hue/Saturation delta grid")
            size = int(data.shape[0])
            if size not in HS_RESOLUTIONS:
                raise ValueError(f"Unsupported legacy Warper resolution: {size}")
        else:
            size = self.warper.grid_size if self.warper.grid_size in HS_RESOLUTIONS else HS_RESOLUTIONS[0]
        self._updating = True
        self.warper.cancel_gesture()
        self.warper.blockSignals(True)
        try:
            self._allow_automatic_calibration = grid_data is None and not preserve_legacy
            self.warper.calibrated_hs = bool(
                self._allow_automatic_calibration and self._use_measured_warper
                and self._use_measured_warper())
            self._calibration_algorithm = None
            self._pending_mesh_edit = False
            self._warper_target_id = target_id
            self.warper.set_grid_size(size)
            if grid_data is not None:
                self.warper.apply_deltas(data)
            self._resolutions["hue_saturation"] = (size, size)
            if self.mode == "hue_saturation":
                self.grid_combo.setCurrentText(str(size))
                self.saturation_combo.setCurrentText(str(size))
            self.warper.update()
            self._sync_hs_space()
        finally:
            self.warper.blockSignals(False)
            self._updating = False
        if emit:
            self.warper_changed.emit(grid_data)

    def reset_all(self) -> None:
        # Existing ToolsPanel.reset_all calls this as an application-wide reset,
        # even when a presentation-only mode is currently visible.
        self._updating = True
        calibrated = self.warper.calibrated_hs
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
        if self._palette_hosted:
            self.command_requested.emit({"mode": self.mode, "action": "palette_edit",
                                         "payload": {"phase": "atomic", "state": None}})
        elif calibrated:
            self._request_calibrated("atomic")
        else:
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
