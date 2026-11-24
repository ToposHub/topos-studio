from PyQt6.QtWidgets import (
    QCheckBox, QGridLayout, QLabel, QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)
from src.shared.i18n.i18n import I18N
from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.features.color.ui.panel_chrome import (
    ColorParameterRow, PARAMETER_NAME_STYLE, PARAMETER_ROW_HEIGHT, PARAMETER_ROW_SPACING,
    SECTION_TITLE_STYLE, create_panel_divider, create_panel_layout, style_reset_button,
)
from src.shared.ui.style_sheet import C

_LABEL_WIDTH = 80
_FORM_INSET = 10


class FilmGrainPanel(QWidget):
    grain_changed = pyqtSignal(dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    # P1-COLOR-13: 显式换种子（seed 只由参数决定，重渲可复现）
    reseed_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # Throttle slider-driven updates to ~60 FPS
        self._param_timer = QTimer()
        self._param_timer.setSingleShot(True)
        self._param_timer.timeout.connect(self._emit_pending_params)
        self._pending_params = None
        self._syncing_ui = False

        self.init_ui()

    def get_style_sheet(self) -> str:
        """Return the panel surface and option typography."""
        return f"""
            FilmGrainPanel {{ background-color: {C.BG_PANEL}; }}
            FilmGrainPanel QCheckBox, FilmGrainPanel QCheckBox:checked {{ {PARAMETER_NAME_STYLE} }}
            QToolButton#grainParameterSection {{
                {SECTION_TITLE_STYLE} border: none; padding: 0 8px; text-align: left;
            }}
            QToolButton#grainParameterSection:hover,
            QToolButton#grainParameterSection:focus {{ background: {C.COLOR_PANEL_RAISED}; }}
        """

    def init_ui(self) -> None:
        self.setStyleSheet(self.get_style_sheet())

        enable_row, layout = create_panel_layout(self, (8, 8, 0), 6)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.enable_check = ToggleSwitch(checked=False)
        self.enable_check.setAccessibleName(I18N.tr("film_grain.enable", "启用效果"))
        self.enable_check.toggled.connect(self.emit_params)

        self._title_label = QLabel(I18N.tr("panel.film_grain", "胶片颗粒"))
        self._title_label.setStyleSheet(SECTION_TITLE_STYLE)

        self.reset_btn = QPushButton()
        self.reset_btn.setObjectName("reset_btn")
        style_reset_button(self.reset_btn, global_reset=True)
        self.reset_btn.setToolTip(I18N.tr("tooltip.reset_film_grain", "重置所有胶片颗粒参数"))
        self.reset_btn.clicked.connect(self.reset_all)

        enable_row.addWidget(self.enable_check)
        enable_row.addWidget(self._title_label)
        enable_row.addStretch()
        enable_row.addWidget(self.reset_btn)

        self.mono_check = QCheckBox(I18N.tr("film_grain.monochrome", "单色颗粒"))
        self.mono_check.setChecked(True)
        self.mono_check.stateChanged.connect(self.emit_params)

        # P1-COLOR-13: 动画颗粒（seed 随帧号确定推进）+ 显式换种子
        self.animated_check = QCheckBox(
            I18N.tr("film_grain.animated", "动画（随帧变化）"))
        self.animated_check.setChecked(False)
        self.animated_check.setToolTip(I18N.tr(
            "film_grain.animated_tooltip",
            "启用后颗粒种子随帧号确定推进：同一帧重渲一致，逐帧颗粒图案变化"))
        self.animated_check.stateChanged.connect(self.emit_params)
        options = QGridLayout()
        options.setContentsMargins(_FORM_INSET + _LABEL_WIDTH + 4, 5, _FORM_INSET, 5)
        options.setHorizontalSpacing(8)
        options.setVerticalSpacing(0)
        options.setColumnStretch(0, 1)
        for checkbox in (self.mono_check, self.animated_check):
            checkbox.setFixedHeight(PARAMETER_ROW_HEIGHT)
        options.addWidget(self.mono_check, 0, 0)
        options.addWidget(self.animated_check, 1, 0, 1, 2)
        self.reseed_btn = QPushButton(
            I18N.tr("film_grain.reseed", "换种子"))
        self.reseed_btn.setFixedHeight(22)
        self.reseed_btn.setStyleSheet("QPushButton { padding: 0 10px; font-size: 11px; }")
        self.reseed_btn.setToolTip(I18N.tr(
            "film_grain.reseed_tooltip", "更换颗粒图案种子（可撤销渲染重渲一致）"))
        self.reseed_btn.clicked.connect(self.reseed_requested.emit)
        options.addWidget(self.reseed_btn, 0, 1)
        layout.addLayout(options)

        self.size_control = self._create_row(I18N.tr("film_grain.size", "颗粒大小"), 0.1, 10.0, 2.0, 0.1)
        self.intensity_control = self._create_row(I18N.tr("film_grain.intensity", "强度"), 0.0, 1.0, 0.25, 0.01)
        self.grain_section_toggle = self._add_parameter_section(
            layout, I18N.tr("film_grain.grain_parameters", "颗粒参数"),
            (self.size_control, self.intensity_control), expanded=True,
        )

        self.shadows_control = self._create_row(I18N.tr("film_grain.shadows", "阴影"), 0.0, 1.0, 0.0, 0.01)
        self.midtones_control = self._create_row(I18N.tr("film_grain.midtones", "中间调"), 0.0, 1.0, 1.0, 0.01)
        self.highlights_control = self._create_row(I18N.tr("film_grain.highlights", "高光"), 0.0, 1.0, 0.5, 0.01)
        self.response_section_toggle = self._add_parameter_section(
            layout, I18N.tr("film_grain.luminance_response", "亮度响应"),
            (self.shadows_control, self.midtones_control, self.highlights_control), expanded=False,
        )
        layout.addStretch()

    def _add_parameter_section(
        self, layout: QVBoxLayout, title: str, rows: tuple[ColorParameterRow, ...],
        *, expanded: bool,
    ) -> QToolButton:
        layout.addWidget(create_panel_divider(Qt.Orientation.Horizontal))
        toggle = QToolButton(self)
        toggle.setObjectName("grainParameterSection")
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle.setText(title)
        toggle.setCheckable(True)
        toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toggle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        toggle.setFixedHeight(23)
        layout.addWidget(toggle)

        content = QWidget(self)
        form = QVBoxLayout(content)
        form.setContentsMargins(_FORM_INSET, 2, _FORM_INSET, 6)
        form.setSpacing(PARAMETER_ROW_SPACING)
        for row in rows:
            form.addWidget(row)
        layout.addWidget(content)

        def set_expanded(visible: bool) -> None:
            content.setVisible(visible)
            toggle.setArrowType(Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow)

        toggle.setChecked(expanded)
        set_expanded(expanded)
        toggle.toggled.connect(set_expanded)
        return toggle

    def _create_row(self, label: str, min_val: float, max_val: float,
                    default_val: float, step: float) -> ColorParameterRow:
        """Create a ParamSliderRow with panel interaction wiring."""
        row = ColorParameterRow(
            label, min_val, max_val, default_val,
            step=step, decimals=2, label_width=_LABEL_WIDTH)
        row.interactionStarted.connect(self.interaction_started.emit)
        row.interactionEnded.connect(self.interaction_ended.emit)
        row.interactionEnded.connect(self._flush_pending_params)
        row.valueChanged.connect(lambda _: self.emit_params())
        return row

    def _build_params(self):
        return {
            "enabled": self.enable_check.isChecked(),
            "intensity": self.intensity_control.value(),
            "size": self.size_control.value(),
            "monochrome": self.mono_check.isChecked(),
            "animated": self.animated_check.isChecked(),
            "shadows": self.shadows_control.value(),
            "midtones": self.midtones_control.value(),
            "highlights": self.highlights_control.value()
        }

    def emit_params(self) -> None:
        """Schedule throttled parameter emission."""
        if self._syncing_ui:
            return
        self._schedule_param_update()

    def _schedule_param_update(self) -> None:
        """Schedule a throttled parameter update (~60 FPS)."""
        self._pending_params = self._build_params()
        if not self._param_timer.isActive():
            self._param_timer.start(16)

    def _emit_pending_params(self) -> None:
        """Emit pending params (called by timer timeout)."""
        if self._syncing_ui:
            return
        if self._pending_params is not None:
            self.grain_changed.emit(self._pending_params)
            self._pending_params = None

    def _flush_pending_params(self) -> None:
        """Immediately emit any pending params (called on slider release)."""
        if self._param_timer.isActive():
            self._param_timer.stop()
        self._emit_pending_params()

    def set_params(self, params, emit=False) -> None:
        self._syncing_ui = True
        self._param_timer.stop()
        self._pending_params = None
        
        all_rows = [
            self.intensity_control, self.size_control,
            self.shadows_control, self.midtones_control, self.highlights_control,
        ]
        all_widgets = [self.enable_check, self.mono_check,
                       self.animated_check] + all_rows
        for w in all_widgets:
            w.blockSignals(True)

        if "enabled" in params:
            self.enable_check.setChecked(params["enabled"])
        if "intensity" in params:
            self.intensity_control.setValue(params["intensity"])
        if "size" in params:
            self.size_control.setValue(params["size"])
        if "monochrome" in params:
            self.mono_check.setChecked(params["monochrome"])
        if "animated" in params:
            self.animated_check.setChecked(bool(params["animated"]))
        if "shadows" in params:
            self.shadows_control.setValue(params["shadows"])
        if "midtones" in params:
            self.midtones_control.setValue(params["midtones"])
        if "highlights" in params:
            self.highlights_control.setValue(params["highlights"])
            
        for w in all_widgets:
            w.blockSignals(False)
        
        self._syncing_ui = False
        
        if emit:
            self._pending_params = self._build_params()
            self.grain_changed.emit(self._pending_params)
            self._pending_params = None

    def reset_all(self) -> None:
        self._syncing_ui = True
        self._param_timer.stop()

        all_rows = [
            self.intensity_control, self.size_control,
            self.shadows_control, self.midtones_control, self.highlights_control,
        ]
        all_widgets = [self.enable_check, self.mono_check,
                       self.animated_check] + all_rows
        for w in all_widgets:
            w.blockSignals(True)

        for row in all_rows:
            row.reset()
        self.mono_check.setChecked(True)
        self.animated_check.setChecked(False)

        for w in all_widgets:
            w.blockSignals(False)
        
        self._syncing_ui = False
        
        self._pending_params = self._build_params()
        self.grain_changed.emit(self._pending_params)
        self._pending_params = None

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        ParamSliderRow.apply_visibility_to_all(
            ParamSliderRow.collect_rows(self), self.width())

