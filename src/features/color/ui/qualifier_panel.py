"""HSL qualifier palette, with range controls and a compact matte sidebar."""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFrame,
    QScrollArea, QButtonGroup, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize

from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.ui.widgets.qualifier_range_bar import QualifierRangeBar
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.assets.icon_manager import (
    icon_reset, icon_color_picker, icon_invert, get_icon,
)
from src.shared.i18n.i18n import I18N


class _RangeParameterRow(ParamSliderRow):
    """Show HSL as percentages while preserving the normalized model API."""

    def __init__(self, label: str, default: float, parent: QWidget) -> None:
        super().__init__(
            label, 0.0, 100.0, default * 100.0, step=0.1, decimals=1,
            label_width=0, value_fixed_width=48, slider_auto_hide=False,
            parent=parent,
        )
        self.label.setFixedWidth(self.label.fontMetrics().horizontalAdvance(label) + 2)
        self.slider.hide()
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def value(self) -> float:
        return super().value() / 100.0

    def setValue(self, value: float) -> None:
        super().setValue(value * 100.0)

    def reset(self) -> None:
        self.setValue(self._default_val / 100.0)

    def setDefaultValue(self, value: float) -> None:
        super().setDefaultValue(value * 100.0)


class QualifierPanel(QWidget):
    params_changed = pyqtSignal(dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    picker_toggled = pyqtSignal(bool)
    matte_preview_requested = pyqtSignal(str)

    CHANNEL_DEFAULTS = {
        "hue": {"hue_center": 0.5, "hue_width": 1.0, "hue_soft": 0.1},
        "sat": {"sat_low": 0.0, "sat_high": 1.0,
                "sat_soft_low": 0.0, "sat_soft_high": 0.0},
        "lum": {"lum_low": 0.0, "lum_high": 1.0,
                "lum_soft_low": 0.0, "lum_soft_high": 0.0},
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updating_flag = False
        self.current_picker_mode = 'normal'
        self.params = {
            "enabled": False,
            "hue_enabled": True, "sat_enabled": True, "lum_enabled": True,
            **{key: value for defaults in self.CHANNEL_DEFAULTS.values()
               for key, value in defaults.items()},
            "blur": 0.0, "clean_black": 0.0, "clean_white": 0.0,
            "denoise": 0.0, "invert": False,
        }
        self._channel_checks: dict[str, ToggleSwitch] = {}
        self._channel_controls: dict[str, list[ParamSliderRow]] = {}
        self._channel_bars: dict[str, QualifierRangeBar] = {}
        self._channel_resets: dict[str, QPushButton] = {}
        self.init_ui()
        self._sync_controls()

    def get_style_sheet(self) -> str:
        """Keep numeric inputs/sliders global; style only the palette chrome."""
        return """
            QualifierPanel { background: #27272b; }
            QWidget#qualifierHeader {
                background: #202024; border-bottom: 1px solid #111113;
            }
            QLabel#qualifierTitle {
                background: transparent; color: #eeeeee;
                font-size: 12px; font-weight: bold;
            }
            QWidget#qualifierTools {
                background: #2c2d32; border-bottom: 1px solid #111113;
            }
            QWidget#qualifierRanges, QWidget#qualifierChannel,
            QWidget#qualifierFinesse { background: #27272b; }
            QWidget#qualifierFinesse { border-left: 1px solid #111113; }
            QLabel#qualifierFinesseTitle {
                color: #eeeeee; font-size: 12px; background: transparent;
            }
            QPushButton[qualifierTool="true"] {
                background: transparent; border: 1px solid transparent;
                border-radius: 3px; padding: 0px; min-width: 0px;
                min-height: 0px; color: #a4a4a8; font-size: 11px;
            }
            QPushButton[qualifierTool="true"]:hover {
                background: #3a3b40; color: #eeeeee;
            }
            QPushButton[qualifierTool="true"]:checked {
                background: #45464b; border-color: #626269; color: #ff8800;
            }
            QLabel#qualifierChannelTitle {
                color: #eeeeee; font-size: 12px;
                background: transparent;
            }
            QFrame#qualifierToolDivider { background: #18181c; border: none; }
        """

    def _tool_button(self, name: str, tooltip: str) -> QPushButton:
        button = QPushButton(self)
        button.setObjectName(name)
        button.setProperty("qualifierTool", True)
        button.setFixedSize(28, 28)
        button.setIconSize(QSize(18, 18))
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        return button

    @staticmethod
    def _divider() -> QFrame:
        divider = QFrame()
        divider.setObjectName("qualifierToolDivider")
        divider.setFixedSize(1, 20)
        return divider

    def init_ui(self) -> None:
        self.setStyleSheet(self.get_style_sheet())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header_widget = QWidget(self)
        header_widget.setObjectName("qualifierHeader")
        header_widget.setFixedHeight(28)
        header = QHBoxLayout(header_widget)
        header.setContentsMargins(12, 0, 8, 0)
        header.setSpacing(3)
        self.title_label = QLabel(f'{I18N.tr("qualifier.title", "限定器")} - HSL')
        self.title_label.setObjectName("qualifierTitle")
        header.addWidget(self.title_label)
        header.addStretch()

        # Keep the preview API available to the canvas/controller. This palette
        # intentionally has no preview mode buttons in its title bar.
        self._preview_buttons: dict[str, QPushButton] = {}
        self.reset_all_btn = QPushButton(self)
        self.reset_all_btn.setObjectName("reset_btn")
        self.reset_all_btn.setToolTip(I18N.tr("tooltip.reset_qualifier", "重置所有限定器参数"))
        self.reset_all_btn.setAccessibleName(self.reset_all_btn.toolTip())
        self.reset_all_btn.setIcon(icon_reset())
        self.reset_all_btn.setIconSize(QSize(12, 12))
        self.reset_all_btn.setFixedSize(16, 16)
        self.reset_all_btn.clicked.connect(self.reset_all)
        header.addWidget(self.reset_all_btn)
        layout.addWidget(header_widget)

        tools_widget = QWidget(self)
        tools_widget.setObjectName("qualifierTools")
        tools_widget.setFixedHeight(34)
        tools = QHBoxLayout(tools_widget)
        tools.setContentsMargins(16, 0, 12, 0)
        tools.setSpacing(3)
        self.picker_group = QButtonGroup(self)
        self.picker_group.setExclusive(True)
        picker_specs = (
            ("normal", "", "正常拾取：首次点击重置范围，拖动时扩展"),
            ("sub", "−", "减去拾取：缩小范围以排除所选颜色"),
            ("add", "+", "添加拾取：扩展范围以包含所选颜色"),
        )
        for index, (mode, text, fallback) in enumerate(picker_specs):
            tooltip_key = 'sub' if mode == 'sub' else mode
            button = self._tool_button(
                f"picker_{mode}_btn", I18N.tr(f"tooltip.qualifier_picker_{tooltip_key}", fallback))
            button.setFixedWidth(30 if not text else 36)
            button.setIcon(icon_color_picker("#b5b5ba"))
            button.setText(text)
            button.setCheckable(True)
            self.picker_group.addButton(button, index)
            button.toggled.connect(self._on_picker_group_toggled)
            setattr(self, f"picker_{mode}_btn", button)
            tools.addWidget(button)
        tools.addSpacing(5)
        tools.addWidget(self._divider())
        tools.addSpacing(5)
        self.invert_check = self._tool_button(
            "qualifierInvert", I18N.tr("qualifier.invert", "反转"))
        self.invert_check.setIcon(icon_invert("#aaaaae"))
        self.invert_check.setCheckable(True)
        self.invert_check.toggled.connect(self.on_param_changed)
        tools.addWidget(self.invert_check)
        tools.addStretch()
        layout.addWidget(tools_widget)

        # A single scroll surface preserves the two-column palette in short docks.
        self.controls_scroll = QScrollArea(self)
        self.controls_scroll.setWidgetResizable(True)
        self.controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.controls_scroll.setStyleSheet("QScrollArea { border: none; background: #27272b; }")
        content = QWidget()
        body = QHBoxLayout(content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.ranges_widget = QWidget(content)
        self.ranges_widget.setObjectName("qualifierRanges")
        ranges = QVBoxLayout(self.ranges_widget)
        ranges.setContentsMargins(16, 8, 16, 8)
        ranges.setSpacing(3)

        channel_specs = (
            ("hue", I18N.tr("qualifier.hue", "色相"), (
                ("hue_center", I18N.tr("qualifier.center", "中心")),
                ("hue_width", I18N.tr("qualifier.width", "宽度")),
                ("hue_soft", I18N.tr("qualifier.soft", "柔化")),
            )),
            ("sat", I18N.tr("qualifier.saturation", "饱和度"), (
                ("sat_low", I18N.tr("qualifier.low", "低")),
                ("sat_high", I18N.tr("qualifier.high", "高")),
                ("sat_soft_low", I18N.tr("qualifier.s_low", "柔化低")),
                ("sat_soft_high", I18N.tr("qualifier.s_high", "柔化高")),
            )),
            ("lum", I18N.tr("qualifier.luminance", "亮度"), (
                ("lum_low", I18N.tr("qualifier.low", "低")),
                ("lum_high", I18N.tr("qualifier.high", "高")),
                ("lum_soft_low", I18N.tr("qualifier.s_low", "柔化低")),
                ("lum_soft_high", I18N.tr("qualifier.s_high", "柔化高")),
            )),
        )
        for channel, title, parameters in channel_specs:
            section = self._build_channel(channel, title, parameters)
            setattr(self, f"{channel}_group", section)
            ranges.addWidget(section)
        ranges.addStretch()
        self.hue_bar.hue_changed.connect(self.on_hue_bar_changed)
        self.sat_bar.range_changed.connect(self.on_range_bar_changed_sat)
        self.lum_bar.range_changed.connect(self.on_range_bar_changed_lum)
        body.addWidget(self.ranges_widget, 1)

        self.refine_group = QWidget(content)
        self.refine_group.setObjectName("qualifierFinesse")
        self.refine_group.setFixedWidth(188)
        finesse = QVBoxLayout(self.refine_group)
        finesse.setContentsMargins(16, 10, 16, 10)
        finesse.setSpacing(4)
        title = QLabel(I18N.tr("qualifier.matte_finesse", "Matte Finesse"))
        title.setObjectName("qualifierFinesseTitle")
        finesse.addWidget(title)
        finesse.addSpacing(2)
        matte_specs = (
            ("clean_black", I18N.tr("qualifier.clean_b", "净化黑"), 20.0),
            ("clean_white", I18N.tr("qualifier.clean_w", "净化白"), 20.0),
            ("blur", I18N.tr("qualifier.blur_radius", "模糊半径"), 100.0),
            ("denoise", I18N.tr("qualifier.denoise", "降噪"), 100.0),
        )
        self._matte_controls: dict[str, ParamSliderRow] = {}
        for key, label, maximum in matte_specs:
            row = self._build_matte_row(label, maximum)
            row.valueChanged.connect(lambda value, name=key: self.update_param(name, value))
            self._matte_controls[key] = row
            setattr(self, 'blur_row' if key == 'blur' else f'{key}_ctrl', row)
            finesse.addWidget(row)
        finesse.addStretch()
        body.addWidget(self.refine_group)
        self.controls_scroll.setWidget(content)
        layout.addWidget(self.controls_scroll, 1)

    def _connect_interaction(self, row: ParamSliderRow) -> None:
        row.interactionStarted.connect(self.interaction_started.emit)
        row.interactionEnded.connect(self.interaction_ended.emit)

    def _build_channel(
        self, channel: str, title: str, parameters: tuple[tuple[str, str], ...],
    ) -> QWidget:
        section = QWidget(self.ranges_widget)
        section.setObjectName("qualifierChannel")
        section.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        column = QVBoxLayout(section)
        column.setContentsMargins(0, 2, 0, 2)
        column.setSpacing(3)
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(6)
        switch = ToggleSwitch(checked=True, parent=section)
        switch.setAccessibleName(title)
        switch.setToolTip(title)
        switch.toggled.connect(lambda checked, name=channel: self._set_channel_enabled(name, checked))
        self._channel_checks[channel] = switch
        setattr(self, f"{channel}_enabled_check", switch)
        heading.addWidget(switch)
        label = QLabel(title, section)
        label.setObjectName("qualifierChannelTitle")
        heading.addWidget(label)
        heading.addStretch()
        reset = self._tool_button(f"qualifierReset_{channel}",
                                  f'{I18N.tr("lut.reset", "重置")} {title}')
        reset.setIcon(icon_reset("#95959a"))
        reset.setIconSize(QSize(12, 12))
        reset.setFixedSize(20, 20)
        reset.clicked.connect(lambda _checked=False, name=channel: self.reset_channel(name))
        self._channel_resets[channel] = reset
        heading.addWidget(reset)
        column.addLayout(heading)
        bar = QualifierRangeBar(channel, section)
        bar.setFixedHeight(QualifierRangeBar.BAR_HEIGHT)
        bar.interaction_started.connect(self.interaction_started.emit)
        bar.interaction_ended.connect(self.interaction_ended.emit)
        self._channel_bars[channel] = bar
        setattr(self, f"{channel}_bar", bar)
        column.addWidget(bar)
        fields = QHBoxLayout()
        fields.setContentsMargins(0, 0, 0, 0)
        fields.setSpacing(12)
        fields.addStretch()
        controls = []
        for key, label in parameters:
            control = _RangeParameterRow(label, self.params[key], section)
            control.valueChanged.connect(lambda _value: self.on_slider_param_changed())
            self._connect_interaction(control)
            setattr(self, f"{key}_ctrl", control)
            controls.append(control)
            fields.addWidget(control)
        self._channel_controls[channel] = controls
        column.addLayout(fields)
        return section

    def _build_matte_row(self, label: str, maximum: float) -> ParamSliderRow:
        row = ParamSliderRow(
            label, 0.0, maximum, 0.0, step=1.0, decimals=1,
            label_width=98, value_fixed_width=48, slider_auto_hide=False,
            parent=self.refine_group,
        )
        # Reuse both global controls, arranging the slider below label/value.
        box = row.layout()
        box.removeWidget(row.label)
        box.removeWidget(row.slider)
        box.removeWidget(row.spin)
        box.setDirection(QVBoxLayout.Direction.TopToBottom)
        box.setSpacing(0)
        heading = QWidget(row)
        heading_layout = QHBoxLayout(heading)
        heading_layout.setContentsMargins(0, 0, 0, 0)
        heading_layout.setSpacing(4)
        row.label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        heading_layout.addWidget(row.label)
        heading_layout.addStretch()
        heading_layout.addWidget(row.spin)
        box.addWidget(heading)
        box.addWidget(row.slider)
        self._connect_interaction(row)
        return row

    def _sync_controls(self) -> None:
        """Synchronize under a state lock; callers control when to submit."""
        was_updating = self._updating_flag
        self._updating_flag = True
        try:
            for channel, controls in self._channel_controls.items():
                enabled = bool(self.params[f"{channel}_enabled"])
                self._channel_checks[channel].setChecked(enabled)
                self._channel_bars[channel].setEnabled(enabled)
                for key, control in zip(self.CHANNEL_DEFAULTS[channel], controls):
                    control.setValue(self.params[key])
                    control.setEnabled(enabled)
            for key, control in self._matte_controls.items():
                control.setValue(self.params[key])
            self.invert_check.setChecked(bool(self.params["invert"]))
            self.hue_bar.set_hue_params(
                self.params["hue_center"], self.params["hue_width"], self.params["hue_soft"])
            self.sat_bar.set_range(self.params["sat_low"], self.params["sat_high"],
                                   self.params["sat_soft_low"], self.params["sat_soft_high"])
            self.lum_bar.set_range(self.params["lum_low"], self.params["lum_high"],
                                   self.params["lum_soft_low"], self.params["lum_soft_high"])
        finally:
            self._updating_flag = was_updating

    def _set_channel_enabled(self, channel: str, enabled: bool) -> None:
        if self._updating_flag:
            return
        self.interaction_started.emit()
        self.params[f"{channel}_enabled"] = enabled
        self.params["enabled"] = True
        self._sync_controls()
        self.params_changed.emit(self.params)
        self.interaction_ended.emit()

    def reset_channel(self, channel: str) -> None:
        self.interaction_started.emit()
        self.params.update(self.CHANNEL_DEFAULTS[channel])
        self.params[f"{channel}_enabled"] = True
        self.params["enabled"] = True
        self._sync_controls()
        self.params_changed.emit(self.params)
        self.interaction_ended.emit()

    def on_slider_param_changed(self) -> None:
        """Called when any slider/spin changes, updates params dict."""
        if self._updating_flag:
            return
            
        self.params["hue_center"] = self.hue_center_ctrl.value()
        self.params["hue_width"] = self.hue_width_ctrl.value()
        self.params["hue_soft"] = self.hue_soft_ctrl.value()
        
        self.params["sat_low"] = self.sat_low_ctrl.value()
        self.params["sat_high"] = self.sat_high_ctrl.value()
        self.params["sat_soft_low"] = self.sat_soft_low_ctrl.value()
        self.params["sat_soft_high"] = self.sat_soft_high_ctrl.value()
        
        self.params["lum_low"] = self.lum_low_ctrl.value()
        self.params["lum_high"] = self.lum_high_ctrl.value()
        self.params["lum_soft_low"] = self.lum_soft_low_ctrl.value()
        self.params["lum_soft_high"] = self.lum_soft_high_ctrl.value()
        
        self.params["clean_black"] = self.clean_black_ctrl.value()
        self.params["clean_white"] = self.clean_white_ctrl.value()
        self.params["denoise"] = self.denoise_ctrl.value()
        
        # Auto-activate when parameters are adjusted
        if not self.params["enabled"]:
            self.params["enabled"] = True

        # Update visualization bars
        self.hue_bar.set_hue_params(self.params["hue_center"], self.params["hue_width"], self.params["hue_soft"])
        self.sat_bar.set_range(self.params["sat_low"], self.params["sat_high"], 
                             self.params["sat_soft_low"], self.params["sat_soft_high"])
        self.lum_bar.set_range(self.params["lum_low"], self.params["lum_high"], 
                             self.params["lum_soft_low"], self.params["lum_soft_high"])

        self.params_changed.emit(self.params)

    def update_param(self, key: str, value: float) -> None:
        """Update one model parameter without recursive control submissions."""
        if self._updating_flag:
            return
        self.params[key] = value
        if key != "enabled":
            self.params["enabled"] = True
        self._sync_controls()
        self.params_changed.emit(self.params)

    def on_hue_bar_changed(self, c: float, w: float, s: float) -> None:
        self.params["hue_center"] = c
        self.params["hue_width"] = w
        self.params["hue_soft"] = s
        self.update_param("hue_center", c)

    def on_range_bar_changed_sat(self, l: float, h: float, sl: float, sh: float) -> None:
        self.params["sat_low"] = l
        self.params["sat_high"] = h
        self.params["sat_soft_low"] = sl
        self.params["sat_soft_high"] = sh
        self.update_param("sat_low", l)

    def on_range_bar_changed_lum(self, l: float, h: float, sl: float, sh: float) -> None:
        self.params["lum_low"] = l
        self.params["lum_high"] = h
        self.params["lum_soft_low"] = sl
        self.params["lum_soft_high"] = sh
        self.update_param("lum_low", l)

    def on_param_changed(self) -> None:
        """invert 复选框 toggled 槽（用户交互路径）。

        AUD-25600: set_params/reset_all 等程序化路径在 invert_check.setChecked
        前后已设置 _updating_flag 状态锁；此处必须检查锁直接返回，否则程序化
        同步会强制把限定器置为 enabled 并向引擎重复提交 params_changed。
        """
        if self._updating_flag:
            return
        self.params["invert"] = self.invert_check.isChecked()
        if not self.params["enabled"]:
            self.params["enabled"] = True
        self.params_changed.emit(self.params)

    def _on_picker_group_toggled(self, checked: bool) -> None:
        """Handle picker button group toggle events.
        
        Updates the current picker mode and emits picker_toggled signal
        when any button in the group is toggled.
        """
        if self._updating_flag:
            return
            
        # Determine which button is checked and update mode
        if self.picker_normal_btn.isChecked():
            self.current_picker_mode = 'normal'
        elif self.picker_add_btn.isChecked():
            self.current_picker_mode = 'add'
        elif self.picker_sub_btn.isChecked():
            self.current_picker_mode = 'subtract'
        else:
            # No button checked, reset to normal mode
            self.current_picker_mode = 'normal'
        
        # Emit signal if any button is checked
        is_any_checked = (self.picker_normal_btn.isChecked() or 
                         self.picker_add_btn.isChecked() or 
                         self.picker_sub_btn.isChecked())
        self.picker_toggled.emit(is_any_checked)

    def process_picked_color(self, h: float, s: float, v: float, reset_ranges: bool = False) -> None:
        """Processes picked color from canvas based on current picker mode.

        Supports three modes:
        - 'normal': Reset range on first click, expand on drag
        - 'add': Always expand range to include new color
        - 'subtract': Shrink range to exclude the picked color

        Args:
            h: Hue value (0.0-1.0)
            s: Saturation value (0.0-1.0, HSV 语义 delta/c_max)
            v: Value/brightness (0.0-1.0, HSV 语义 min(c_max, 1.0))
            reset_ranges: If True and in normal mode, reset ranges centered on picked color
        """
        # Shift Hue by 0.5 to match the UI Coordinate (Red at 0.5)
        shifted_h = (h + 0.5) % 1.0

        if self.current_picker_mode == 'subtract':
            self._process_subtract_mode(shifted_h, s, v)
        elif self.current_picker_mode == 'add':
            # Add mode: always expand (ignore reset_ranges)
            self._process_add_mode(shifted_h, s, v)
        else:
            # Normal mode: reset or expand based on reset_ranges
            if reset_ranges:
                self._process_normal_reset(shifted_h, s, v)
            else:
                self._process_add_mode(shifted_h, s, v)

        self.params["enabled"] = True
        self.set_params(self.params)

    def _process_normal_reset(self, shifted_h: float, s: float, l: float) -> None:
        """Normal mode first click: reset all ranges centered on picked color."""
        self.params["hue_center"] = shifted_h
        self.params["hue_width"] = 0.20
        self.params["hue_soft"] = 0.10
        
        self.params["sat_low"] = max(0.0, s - 0.20)
        self.params["sat_high"] = min(1.0, s + 0.20)
        self.params["sat_soft_low"] = 0.10
        self.params["sat_soft_high"] = 0.10
        
        self.params["lum_low"] = max(0.0, l - 0.20)
        self.params["lum_high"] = min(1.0, l + 0.20)
        self.params["lum_soft_low"] = 0.10
        self.params["lum_soft_high"] = 0.10

    def _process_add_mode(self, shifted_h: float, s: float, l: float) -> None:
        """Add mode: expand range minimally to include new color.
        
        Uses bounded hue expansion to prevent over-selection when dragging
        to select colors with large hue differences.
        """
        current_center = self.params["hue_center"]
        current_half_width = self.params["hue_width"] / 2.0
        
        # Calculate angular distance from center (handling wrap-around)
        diff = shifted_h - current_center
        while diff > 0.5:
            diff -= 1.0
        while diff < -0.5:
            diff += 1.0
        
        # Only expand if color is outside current range
        if abs(diff) > current_half_width:
            # Calculate required half-width to include this color
            required_half_width = abs(diff)
            
            # Clamp to maximum 40% of hue wheel (total width 0.8)
            # This prevents selecting the entire color spectrum
            max_half_width = 0.4
            new_half_width = min(required_half_width + 0.02, max_half_width)
            
            # Adjust center towards the new color
            # This keeps the range tight while still covering both colors
            if required_half_width <= max_half_width:
                # We can fit both: shift center towards new color
                center_shift = (required_half_width - current_half_width) / 2.0
                if diff > 0:
                    new_center = (current_center + center_shift) % 1.0
                else:
                    new_center = (current_center - center_shift) % 1.0
                self.params["hue_center"] = new_center
                self.params["hue_width"] = new_half_width * 2.0
            else:
                # Can't fit: just expand to max with center at midpoint
                # This handles extreme cases gracefully
                self.params["hue_width"] = max_half_width * 2.0
        
        self.params["hue_soft"] = min(0.2, self.params.get("hue_soft", 0.0) + 0.01)
        
        # Saturation and Luminance expansion
        self.params["sat_low"] = min(self.params["sat_low"], max(0.0, s - 0.05))
        self.params["sat_high"] = max(self.params["sat_high"], min(1.0, s + 0.05))
        self.params["sat_soft_low"] = min(0.2, self.params.get("sat_soft_low", 0.0) + 0.01)
        self.params["sat_soft_high"] = min(0.2, self.params.get("sat_soft_high", 0.0) + 0.01)
        
        self.params["lum_low"] = min(self.params["lum_low"], max(0.0, l - 0.05))
        self.params["lum_high"] = max(self.params["lum_high"], min(1.0, l + 0.05))
        self.params["lum_soft_low"] = min(0.2, self.params.get("lum_soft_low", 0.0) + 0.01)
        self.params["lum_soft_high"] = min(0.2, self.params.get("lum_soft_high", 0.0) + 0.01)

    def _process_subtract_mode(self, shifted_h: float, s: float, l: float) -> None:
        """Subtract mode: shrink range to exclude the picked color.
        
        For Hue: shrink hue_width to exclude the picked hue.
        For Sat/Lum: move the closer boundary to exclude the picked value.
        """
        # --- Hue subtraction ---
        current_center = self.params["hue_center"]
        current_half_width = self.params["hue_width"] / 2.0
        
        # Calculate angular distance from center (handling wrap-around)
        diff = shifted_h - current_center
        while diff > 0.5:
            diff -= 1.0
        while diff < -0.5:
            diff += 1.0
        
        # Only shrink if color is inside current range
        if abs(diff) < current_half_width:
            # Shrink the width so that the picked color is just outside
            # New half-width is the distance from center to picked color
            new_half_width = max(0.02, abs(diff) - 0.02)  # Minimum 0.02 to avoid zero width
            self.params["hue_width"] = new_half_width * 2.0
            
            # Optionally shift center towards the remaining selection
            # This keeps the selection centered on the remaining colors
            # We shift away from the subtracted color
            if abs(diff) > 0.01:  # Only shift if not exactly at center
                shift_amount = (current_half_width - new_half_width) / 2.0
                if diff > 0:
                    # Color was on the positive side, shift center negative
                    new_center = (current_center - shift_amount) % 1.0
                else:
                    # Color was on the negative side, shift center positive
                    new_center = (current_center + shift_amount) % 1.0
                self.params["hue_center"] = new_center
        
        # --- Saturation subtraction ---
        sat_low = self.params["sat_low"]
        sat_high = self.params["sat_high"]
        
        # Check if s is within current range
        if sat_low <= s <= sat_high:
            dist_to_low = s - sat_low
            dist_to_high = sat_high - s
            
            if dist_to_low <= dist_to_high:
                # Closer to low, move low up to exclude s
                new_low = min(s + 0.02, sat_high - 0.02)  # Ensure some range remains
                self.params["sat_low"] = max(0.0, new_low)
            else:
                # Closer to high, move high down to exclude s
                new_high = max(s - 0.02, sat_low + 0.02)  # Ensure some range remains
                self.params["sat_high"] = min(1.0, new_high)
        
        # --- Luminance subtraction ---
        lum_low = self.params["lum_low"]
        lum_high = self.params["lum_high"]
        
        # Check if l is within current range
        if lum_low <= l <= lum_high:
            dist_to_low = l - lum_low
            dist_to_high = lum_high - l
            
            if dist_to_low <= dist_to_high:
                # Closer to low, move low up to exclude l
                new_low = min(l + 0.02, lum_high - 0.02)  # Ensure some range remains
                self.params["lum_low"] = max(0.0, new_low)
            else:
                # Closer to high, move high down to exclude l
                new_high = max(l - 0.02, lum_low + 0.02)  # Ensure some range remains
                self.params["lum_high"] = min(1.0, new_high)

    def reset_all(self) -> None:
        """Resets all qualifier parameters to default."""
        self._updating_flag = True
        self.params.update({
            "enabled": False,
            "hue_enabled": True, "sat_enabled": True, "lum_enabled": True,
            "hue_center": 0.5, "hue_width": 1.0, "hue_soft": 0.1,
            "sat_low": 0.0, "sat_high": 1.0, "sat_soft_low": 0.0, "sat_soft_high": 0.0,
            "lum_low": 0.0, "lum_high": 1.0, "lum_soft_low": 0.0, "lum_soft_high": 0.0,
            "blur": 0.0, "clean_black": 0.0, "clean_white": 0.0,
            "denoise": 0.0, "invert": False
        })
        self.set_params(self.params)
        
        # Reset picker button states
        self.picker_group.setExclusive(False)  # Temporarily disable exclusivity
        self.picker_normal_btn.setChecked(False)
        self.picker_add_btn.setChecked(False)
        self.picker_sub_btn.setChecked(False)
        self.picker_group.setExclusive(True)  # Re-enable exclusivity
        self.current_picker_mode = 'normal'
        
        self._updating_flag = False

    def set_picker_active(self, active: bool) -> None:
        """Programmatically sets the picker button state.
        
        Args:
            active: If True, check picker_normal_btn if no button is checked.
                   If False, uncheck all picker buttons.
        """
        if active:
            # If activating and no button is checked, default to normal mode
            if not (self.picker_normal_btn.isChecked() or 
                   self.picker_add_btn.isChecked() or 
                   self.picker_sub_btn.isChecked()):
                self.picker_normal_btn.setChecked(True)
        else:
            # Deactivate: uncheck all buttons
            self.picker_group.setExclusive(False)  # Temporarily disable exclusivity
            self.picker_normal_btn.setChecked(False)
            self.picker_add_btn.setChecked(False)
            self.picker_sub_btn.setChecked(False)
            self.picker_group.setExclusive(True)  # Re-enable exclusivity

    def set_params(self, params: dict) -> None:
        self.params.update(params)
        self._sync_controls()
        self.params_changed.emit(self.params)

    def set_matte_preview_active(self, active: bool, style: str = 'highlight') -> None:
        """Compatibility hook for viewport preview state synchronization."""
        for m, b in self._preview_buttons.items():
            b.setChecked(bool(active) and m == style)
