"""Custom curves palette: curve canvas + Resolve-style Edit / Soft Clip sidebar.

Soft Clip 四参数与 Edit 强度经 ``soft_clip_changed`` / ``edit_changed``
提交正式模型（16ms 节流 + interaction 结束 flush）；画布控制点仍走原
``CurvesWidget.changed``——两路互不重复提交。回填一律经
``set_layer_state``（屏蔽信号、不发渲染）。
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from src.shared.ui.panel_splitter import PanelSplitter
from src.features.color.core.curves_calibration import (
    CURVES_CALIBRATION_VERSION_KEY,
    CUSTOM_CURVES_CALIBRATION_VERSION, HSL_CURVES_CALIBRATION_VERSION,
    curves_calibration_version,
)
from src.features.color.core.curves_soft_clip import (
    RESOLVE_SOFT_CLIP_BOUNDS,
    SOFT_CLIP_CALIBRATION_VERSION,
    SOFT_CLIP_PARAM_KEYS,
    clamp_resolve_soft_clip_control,
    curves_edit_is_default,
    curves_soft_clip_is_identity,
    normalize_curves_edit,
    normalize_curves_soft_clip,
    soft_clip_calibration_version,
)
from src.shared.assets.icon_manager import icon_link, icon_unlink
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import (
    C, SCROLL_AREA_TRANSPARENT_STYLE,
)
from src.shared.ui.widgets.curves_widget import CurvesWidget
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.ui.widgets.curve_mode_button import CurveModeButton
from src.shared.ui.widgets.hsl_curves_widget import CURVE_MODES
from src.features.color.ui.hsl_curves_panel import HSLCurvesPanel
from src.features.color.ui.panel_chrome import (
    ColorParameterRow, PARAMETER_ROW_SPACING, SECTION_TITLE_STYLE,
    create_panel_header, style_header_button, style_reset_button,
)

CURVE_PALETTE_ORDER = ('Custom', 'HueHue', 'HueSat', 'HueLum', 'LumSat', 'SatSat', 'SatLum')
_SOFT_CLIP_DISPLAY_OFFSETS = {"low_clip": 50.0, "high_clip": -150.0}


class CurvesPanel(QWidget):
    """Keep the original curve canvas/API, with a Resolve-style control sidebar.

    侧栏操作不发原 ``CurvesWidget.changed``：Edit 强度/链接经
    ``edit_changed``，四参数柔化裁切经 ``soft_clip_changed``（引擎侧
    ``set_curves_edit_params`` / ``set_curves_soft_clip_params`` 消费）。
    """

    soft_clip_changed = pyqtSignal(dict)
    edit_changed = pyqtSignal(dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    curve_mode_changed = pyqtSignal(str)

    def __init__(self, curves_widget: CurvesWidget, parent: QWidget | None = None,
                 *, hsl_panel: HSLCurvesPanel | None = None,
                 use_measured_curves: Callable[[], bool] | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("curves_panel")
        # 接线守卫先于任何控件连接初始化：构造期的 toggle/setValue 噪声
        # 不得触发节流发射（_wiring_ready 在 __init__ 尾部才放行）。
        self._syncing = False
        self._wiring_ready = False
        self._soft_version = 0
        self._allow_automatic_soft_calibration = False
        self.soft_calibration_pending = False
        self.soft_clip_interaction_active = False
        self._use_measured_curves = use_measured_curves
        self._allow_automatic_calibration = False
        self.automatic_calibration_pending = False
        self.hsl_calibration_pending = False
        # Keep the dock resizable on compact screens while preserving enough
        # room for a useful graph and the channel controls side by side.
        self.setMinimumWidth(420)
        self.curves_widget = curves_widget
        self.hsl_panel = hsl_panel if hsl_panel is not None else HSLCurvesPanel()
        self.curve_mode = 'Custom'
        self.edit_rows: dict[str, ParamSliderRow] = {}
        self.soft_clip_rows: dict[str, ParamSliderRow] = {}
        self.edit_buttons: dict[str, QPushButton] = {}
        self.soft_clip_buttons: dict[str, QPushButton] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = create_panel_header()
        toolbar = QHBoxLayout(header)
        toolbar.setContentsMargins(6, 0, 6, 0)
        toolbar.setSpacing(4)
        self.mode_title = QLabel(I18N.tr("curves_palette.custom", "Custom"))
        self.mode_title.setStyleSheet(SECTION_TITLE_STYLE)
        toolbar.addWidget(self.mode_title)
        # Keep the picker beside its active curve mode so it is easy to find.
        self.picker_btn = curves_widget.picker_btn
        style_header_button(self.picker_btn, width=28, icon=self.picker_btn.icon())
        toolbar.addWidget(self.picker_btn)
        toolbar.addStretch()
        self.mode_buttons: dict[str, CurveModeButton] = {}
        self.mode_group = QButtonGroup(self)
        for mode in CURVE_PALETTE_ORDER:
            title = self._mode_title(mode)
            button = CurveModeButton(mode, title, self)
            style_header_button(button, width=28, icon=button.icon())
            button.setIconSize(QSize(26, 18))
            self.mode_buttons[mode] = button
            self.mode_group.addButton(button)
            button.clicked.connect(lambda _checked=False, m=mode: self.set_curve_mode(m))
            toolbar.addWidget(button)
        self.mode_buttons['Custom'].setChecked(True)
        # Reparent the original buttons, retaining their existing slots/state.
        self.reset_curves_btn = curves_widget.btn_container.layout().itemAt(
            curves_widget.btn_container.layout().count() - 1
        ).widget()
        style_reset_button(self.reset_curves_btn, global_reset=True)
        toolbar.addWidget(self.reset_curves_btn)
        self.reset_hsl_btn = self.hsl_panel.hsl_curves_widget.reset_btn
        style_reset_button(self.reset_hsl_btn, global_reset=True)
        toolbar.addWidget(self.reset_hsl_btn)
        self.reset_hsl_btn.hide()
        curves_widget.btn_container.hide()
        layout.addWidget(header)

        self.body = PanelSplitter(Qt.Orientation.Horizontal, self)
        self.body.setChildrenCollapsible(False)
        self.pages = QStackedWidget(self)
        self.custom_page = QWidget(self.pages)
        custom_layout = QVBoxLayout(self.custom_page)
        custom_layout.setContentsMargins(0, 0, 0, 0)
        custom_layout.addWidget(self.body)
        self.pages.addWidget(self.custom_page)
        self.hsl_panel.set_integrated(True)
        self.pages.addWidget(self.hsl_panel)
        layout.addWidget(self.pages, 1)
        curves_widget.setMinimumSize(220, 180)
        self.body.addWidget(curves_widget)

        self.sidebar_scroll = QScrollArea(self)
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setStyleSheet(SCROLL_AREA_TRANSPARENT_STYLE)
        self.sidebar_scroll.setMinimumWidth(170)
        self.sidebar_scroll.setMaximumWidth(320)
        self.sidebar_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.sidebar = QWidget()
        self.sidebar.setObjectName("curves_sidebar")
        controls = QVBoxLayout(self.sidebar)
        controls.setContentsMargins(0, 2, 0, 2)
        controls.setSpacing(4)

        edit_controls = QVBoxLayout()
        edit_controls.setContentsMargins(4, 0, 4, 0)
        edit_controls.setSpacing(PARAMETER_ROW_SPACING)
        controls.addLayout(edit_controls)
        self.edit_link_btn = self._link_button("edit_link")
        self.edit_group = QButtonGroup(self)
        edit_controls.addLayout(self._section_header(
            I18N.tr("curves_palette.edit", "Edit"), self.edit_link_btn,
            "YRGB", self.edit_buttons, self.edit_group, self._reset_edit,
        ))
        for channel in "YRGB":
            row = ColorParameterRow(
                channel, 0.0, 100.0, 100.0, step=1.0, decimals=0,
                label_width=16, slider_auto_hide=False, value_fixed_width=44,
            )
            row.label.setStyleSheet(f"color: {self._channel_color(channel)}; font-size: 11px; font-weight: 600; background: transparent;")
            row.setObjectName(f"curve_amount_{channel}")
            line = QHBoxLayout()
            line.setSpacing(2)
            line.addWidget(row, 1)
            reset = self._reset_button(I18N.tr("curves_palette.reset_amount", "Reset channel amount"))
            reset.clicked.connect(lambda _checked=False, ch=channel: self._reset_edit_amount(ch))
            line.addWidget(reset)
            edit_controls.addLayout(line)
            self.edit_rows[channel] = row
            row.valueChanged.connect(
                lambda value, ch=channel: self._store_edit_amount(ch, value)
            )
            self.edit_buttons[channel].clicked.connect(
                lambda _checked=False, ch=channel: self._select_curve_channel(ch)
            )
        self.edit_buttons["Y"].setChecked(True)
        self.edit_link_btn.toggled.connect(self._set_edit_linked)
        self._set_edit_linked(True)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background: {C.PANEL_DIVIDER}; border: none;")
        controls.addWidget(divider)

        soft_clip_controls = QVBoxLayout()
        soft_clip_controls.setContentsMargins(4, 0, 4, 0)
        soft_clip_controls.setSpacing(PARAMETER_ROW_SPACING)
        controls.addLayout(soft_clip_controls)
        self.soft_clip_link_btn = self._link_button("soft_clip_link")
        self.soft_clip_group = QButtonGroup(self)
        soft_clip_controls.addLayout(self._section_header(
            I18N.tr("curves_palette.soft_clip", "Soft Clip"), self.soft_clip_link_btn,
            "RGB", self.soft_clip_buttons, self.soft_clip_group, self._reset_soft_clip,
        ))
        # Physical Soft Clip parameters, aligned with the mobile app's
        # SoftClipChannelParams (shaders/soft_clip.frag). Defaults are exact
        # identity: Low sits on the black floor, High on the white ceiling and
        # both knees are closed, so super-whites/negative RGB pass through.
        self._soft_defaults = {
            "low_clip": 0.0, "low_soft": 0.0, "high_clip": 1.0, "high_soft": 0.0,
        }
        self._soft_values = {ch: dict(self._soft_defaults) for ch in "RGB"}
        self._soft_channel = "R"
        for key, fallback in (
            ("low_clip", "Low"), ("low_soft", "Low Soft"),
            ("high_clip", "High"), ("high_soft", "High Soft"),
        ):
            row_min, row_max = self._soft_row_range(key)
            row = ColorParameterRow(
                I18N.tr(f"curves_palette.{key}", fallback), row_min, row_max,
                self._soft_defaults[key], step=0.01, decimals=2,
                label_width=64, slider_auto_hide=False, value_fixed_width=44,
            )
            row.label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            row.setObjectName(f"soft_clip_{key}")
            row.valueChanged.connect(lambda value, name=key: self._on_soft_row_changed(name, value))
            line = QHBoxLayout()
            line.setSpacing(2)
            line.addWidget(row, 1)
            reset = self._reset_button(I18N.tr("curves_palette.reset_parameter", "Reset parameter"))
            reset.clicked.connect(lambda _checked=False, name=key: self._reset_soft_parameter(name))
            line.addWidget(reset)
            soft_clip_controls.addLayout(line)
            self.soft_clip_rows[key] = row
        for channel, button in self.soft_clip_buttons.items():
            button.clicked.connect(lambda _checked=False, ch=channel: self._select_soft_channel(ch))
        self.soft_clip_link_btn.toggled.connect(self._set_soft_linked)
        self._set_soft_linked(True)
        controls.addStretch(1)

        # —— 正式接线状态（任务 6/7）：16ms 节流 + interaction 结束 flush。
        self._soft_timer = QTimer(self)
        self._soft_timer.setSingleShot(True)
        self._soft_timer.setInterval(16)
        self._soft_timer.timeout.connect(self._emit_soft_now)
        self._edit_timer = QTimer(self)
        self._edit_timer.setSingleShot(True)
        self._edit_timer.setInterval(16)
        self._edit_timer.timeout.connect(self._emit_edit_now)
        for row in self.soft_clip_rows.values():
            row.interactionStarted.connect(self._on_soft_interaction_started)
            row.interactionEnded.connect(self._on_soft_interaction_ended)
        for row in self.edit_rows.values():
            row.interactionStarted.connect(self.interaction_started)
            row.interactionEnded.connect(self._on_row_interaction_ended)
        self._wiring_ready = True

        self.sidebar_scroll.setWidget(self.sidebar)
        self.body.addWidget(self.sidebar_scroll)
        self.body.setStretchFactor(0, 1)
        self.body.setStretchFactor(1, 0)
        self.body.setSizes([700, 250])
        self.hsl_panel.hsl_curves_widget.mode_changed.connect(self.set_curve_mode)

    @staticmethod
    def _mode_title(mode: str) -> str:
        if mode == 'Custom':
            return I18N.tr('curves_palette.custom', 'Custom')
        return I18N.tr(f"hsl_curves.{CURVE_MODES[mode]['name']}",
                       CURVE_MODES[mode]['name'].replace('_', ' ').title())

    def set_curve_mode(self, mode: str) -> None:
        """Navigate editors without changing a curve, LUT, or undo history."""
        if mode not in CURVE_PALETTE_ORDER or mode == self.curve_mode:
            return
        self.curves_widget.deactivate_picker()
        self.hsl_panel.deactivate_picker()
        self.curves_widget.clear()
        self.hsl_panel.hsl_curves_widget.clear_scope()
        self.curve_mode = mode
        custom = mode == 'Custom'
        self.mode_buttons[mode].setChecked(True)
        self.mode_title.setText(self._mode_title(mode))
        self.pages.setCurrentIndex(0 if custom else 1)
        self.picker_btn.setVisible(custom)
        self.reset_curves_btn.setVisible(custom)
        self.reset_hsl_btn.setVisible(not custom)
        if not custom:
            self.hsl_panel.hsl_curves_widget.current_mode = mode
            # Resolve-style one-shot picking: entering an HSL curve makes the
            # next viewer click add a point; the existing pick handler turns
            # this back off after sampling.
            self.hsl_panel.hsl_curves_widget.picker_btn.setChecked(True)
        self.curve_mode_changed.emit(mode)

    @staticmethod
    def _channel_color(channel: str) -> str:
        return {
            "Y": "#dddddf",
            "R": "#ff1018",
            "G": "#00d020",
            "B": "#123cff",
        }[channel]

    @classmethod
    def _channel_button_style(cls, channel: str) -> str:
        """Solid DaVinci-style channel button with a clear selected outline."""
        color = cls._channel_color(channel)
        foreground = "#202024" if channel in {"Y", "G"} else "#ffffff"
        return f"""
            QPushButton {{
                background-color: #252529;
                color: #99999e;
                border: 1px solid #77777c;
                border-radius: 3px;
                font-size: 12px;
                font-weight: bold;
                padding: 0px;
            }}
            QPushButton:checked {{
                background-color: {color};
                color: {foreground};
                border: 1px solid {color};
            }}
            QPushButton:hover {{
                border-color: #dddddf;
            }}
        """

    def _link_button(self, name: str) -> QPushButton:
        button = QPushButton()
        button.setObjectName(name)
        button.setCheckable(True)
        button.setChecked(True)
        button.setFixedSize(22, 22)
        button.setIconSize(QSize(14, 14))
        button.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                padding: 0px;
            }
            QPushButton:hover {
                background: transparent;
                border: none;
            }
        """)
        button.setToolTip(I18N.tr("curves_palette.link", "Link channels"))
        button.setAccessibleName(button.toolTip())
        button.setIcon(icon_link())
        button.toggled.connect(lambda linked: button.setIcon(icon_link() if linked else icon_unlink()))
        return button

    def _reset_button(self, tooltip: str) -> QPushButton:
        button = QPushButton()
        style_reset_button(button)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        return button

    def _section_header(
        self, title: str, link: QPushButton, channels: str,
        buttons: dict[str, QPushButton], group: QButtonGroup, reset_slot: Callable[[], None],
    ) -> QHBoxLayout:
        header = QHBoxLayout()
        header.setSpacing(3)
        label = QLabel(title)
        label.setStyleSheet(SECTION_TITLE_STYLE)
        header.addWidget(label)
        header.addStretch()
        header.addWidget(link)
        for channel in channels:
            button = QPushButton(channel)
            button.setCheckable(True)
            button.setFixedSize(22, 22)
            button.setStyleSheet(self._channel_button_style(channel))
            button.setAccessibleName(f"{title} {channel}")
            group.addButton(button)
            buttons[channel] = button
            header.addWidget(button)
        reset = self._reset_button(I18N.tr("curves_palette.reset_section", "Reset section"))
        reset.clicked.connect(reset_slot)
        header.addWidget(reset)
        return header

    def _select_curve_channel(self, channel: str) -> None:
        # Existing canvas supports one active editing channel. Link remains a
        # UI draft until multi-channel curve editing has a model contract.
        if self.edit_link_btn.isChecked():
            self.edit_link_btn.setChecked(False)
        self.edit_group.setExclusive(False)
        for name, button in self.edit_buttons.items():
            button.setChecked(name == channel)
        self.edit_group.setExclusive(True)
        self.curves_widget.set_active_channel("M" if channel == "Y" else channel)

    def _set_edit_linked(self, linked: bool) -> None:
        # 链接编辑语义落在画布：拖动/加点/删点同步到全部通道（任务 5）
        self.curves_widget.set_edit_linked(linked)
        self.edit_group.setExclusive(False)
        active = "Y" if self.curves_widget.active_channel == "M" else self.curves_widget.active_channel
        for channel, button in self.edit_buttons.items():
            button.setChecked(linked or channel == active)
        self.edit_group.setExclusive(not linked)
        self._schedule_edit_emit()

    def _reset_edit(self) -> None:
        self.interaction_started.emit()
        self.set_edit_state({**self.get_edit_payload(), "amounts": {}})
        self._schedule_edit_emit()
        self._on_row_interaction_ended()

    def _reset_edit_amount(self, channel: str) -> None:
        self.interaction_started.emit()
        self._store_edit_amount(channel, 100.0)
        self._on_row_interaction_ended()

    def _store_edit_amount(self, channel: str, value: float) -> None:
        """侧栏强度 -> 画布合成（compose_curves_lut），行值回吸防漂移。"""
        amounts = self.curves_widget.get_edit_amounts()
        amounts[channel] = float(value)
        self.curves_widget.set_edit_amounts(amounts)
        row = self.edit_rows.get(channel)
        if row is not None:
            row.setValue(self.curves_widget.edit_amounts[channel])
        self._schedule_edit_emit()

    def _on_soft_row_changed(self, key: str, value: float) -> None:
        if self._soft_version:
            value = (value - _SOFT_CLIP_DISPLAY_OFFSETS.get(key, 0.0)) / 200.0
        self._store_soft_value(key, value)

    def _store_soft_value(self, key: str, value: float) -> None:
        targets = "RGB" if self.soft_clip_link_btn.isChecked() else self._soft_channel
        independent = self._uses_independent_soft_controls()
        changed = False
        for channel in targets:
            current = self._soft_values[channel]
            if independent:
                updated = {**current, key: clamp_resolve_soft_clip_control(key, round(value, 5))}
            else:
                digits = 5 if self._soft_version else 2
                low, high = self._legacy_soft_edit_bounds(key, current)
                limited = min(max(round(value, digits), low), high)
                updated = {**current, key: round(limited, 12)}
            changed |= updated != current
            self._soft_values[channel] = updated
        if changed:
            if independent:
                self._soft_version = SOFT_CLIP_CALIBRATION_VERSION
            self._schedule_soft_emit()
        self._refresh_soft_rows()

    def _uses_independent_soft_controls(self) -> bool:
        if self._soft_version == SOFT_CLIP_CALIBRATION_VERSION:
            return True
        if self._soft_version != 1 or not (self._use_measured_curves and self._use_measured_curves()):
            return False
        if not all(
            lo <= values[key] <= hi
            for values in self._soft_values.values()
            for key, (lo, hi) in RESOLVE_SOFT_CLIP_BOUNDS.items()
        ):
            return False
        if self.soft_clip_link_btn.isChecked():
            return True
        # An edit to one old channel may upgrade the node only if its
        # untouched channels render the same with both versions.
        for channel, values in self._soft_values.items():
            if channel == self._soft_channel:
                continue
            low, ls, high, hs = (values[key] for key in SOFT_CLIP_PARAM_KEYS)
            if low + ls > 0.5 or 1.0 - high + hs > 0.5:
                return False
            if not hs and high != 1.0 and high - low + ls != min(high, 1.0):
                return False
        return True

    @staticmethod
    def _legacy_soft_edit_bounds(key: str, values: dict) -> tuple[float, float]:
        """Keep the mobile model legal by limiting only the edited value."""
        low, high = values["low_clip"], values["high_clip"]
        low_soft, high_soft = values["low_soft"], values["high_soft"]
        if key == "low_clip":
            return -1.0, min(1.0, high - low_soft - high_soft)
        if key == "high_clip":
            return max(0.0, low + low_soft + high_soft), 2.0
        other_soft = high_soft if key == "low_soft" else low_soft
        return 0.0, max(0.0, high - low - other_soft)

    @staticmethod
    def _soft_row_range(key: str) -> tuple[float, float]:
        """The legacy sliders use the full physical range for each field."""
        if key == "low_clip":
            return -1.0, 1.0
        if key == "high_clip":
            return 0.0, 2.0
        return 0.0, 3.0

    def _refresh_soft_rows(self) -> None:
        """Push the constrained values of the active channel into the rows."""
        channel = self._soft_values[self._soft_channel]
        independent = self._uses_independent_soft_controls()
        for key, row in self.soft_clip_rows.items():
            value, default = channel[key], self._soft_defaults[key]
            if independent:
                row_min, row_max = 0.0, 100.0
            else:
                row_min, row_max = self._soft_row_range(key)
            if self._soft_version:
                offset = _SOFT_CLIP_DISPLAY_OFFSETS.get(key, 0.0)
                value = round(value * 200.0 + offset, 2)
                default = default * 200.0 + offset
                if not independent:
                    row_min = min(value, max(0.0, row_min * 200.0 + offset))
                    row_max = max(value, min(100.0, row_max * 200.0 + offset))
            row.blockSignals(True)
            row.slider.use_groove_drag_span = independent
            row.setRange(row_min, row_max)
            row.setDefaultValue(default)
            row.setValue(value)
            row.blockSignals(False)

    def _select_soft_channel(self, channel: str) -> None:
        if self.soft_clip_link_btn.isChecked():
            self.soft_clip_link_btn.setChecked(False)
        self._soft_channel = channel
        self.soft_clip_group.setExclusive(False)
        for name, button in self.soft_clip_buttons.items():
            button.setChecked(name == channel)
        self.soft_clip_group.setExclusive(True)
        self._refresh_soft_rows()

    def _set_soft_linked(self, linked: bool) -> None:
        if linked:
            source = "R" if self._uses_independent_soft_controls() else self._soft_channel
            values = self._soft_values[source]
            self._soft_values = {ch: dict(values) for ch in "RGB"}
        self.soft_clip_group.setExclusive(False)
        for channel, button in self.soft_clip_buttons.items():
            button.setChecked(linked or channel == self._soft_channel)
        self.soft_clip_group.setExclusive(not linked)
        self._refresh_soft_rows()
        self._schedule_soft_emit()

    def _reset_soft_clip(self) -> None:
        self.interaction_started.emit()
        targets = "RGB" if self.soft_clip_link_btn.isChecked() else self._soft_channel
        for channel in targets:
            self._soft_values[channel] = dict(self._soft_defaults)
        self._refresh_soft_rows()
        self._schedule_soft_emit()
        self._on_row_interaction_ended()

    def _reset_soft_parameter(self, key: str) -> None:
        self.interaction_started.emit()
        self._store_soft_value(key, self._soft_defaults[key])
        self._on_row_interaction_ended()

    def get_ui_state(self) -> dict:
        """Return detached draft state for future wiring; never a render payload."""
        return {
            "edit_channel": self.curves_widget.active_channel,
            "edit_linked": self.edit_link_btn.isChecked(),
            "edit_amounts": {ch: row.value() for ch, row in self.edit_rows.items()},
            "soft_clip_channel": self._soft_channel,
            "soft_clip_linked": self.soft_clip_link_btn.isChecked(),
            "soft_clip_channels": {
                ch: dict(values) for ch, values in self._soft_values.items()
            },
        }

    # ============================================================
    # 正式参数信号（任务 6/7）：节流发射 + 回填
    # ============================================================

    def get_soft_clip_payload(self) -> dict:
        """四参数柔化裁切的模型形 payload（``soft_clip_changed`` 载荷）。"""
        return {
            **({CURVES_CALIBRATION_VERSION_KEY: self._soft_version}
               if self._soft_version else {}),
            "linked": self.soft_clip_link_btn.isChecked(),
            "channels": {ch: dict(values) for ch, values in self._soft_values.items()},
        }

    def get_edit_payload(self) -> dict:
        """Edit 强度/链接的模型形 payload（``edit_changed`` 载荷）。"""
        return {
            **({CURVES_CALIBRATION_VERSION_KEY: self.curves_widget.calibration_version}
               if self.curves_widget.calibrated else {}),
            "linked": self.edit_link_btn.isChecked(),
            "amounts": self.curves_widget.get_edit_amounts(),
        }

    def get_layer_state(self, *, include_pending: bool = False) -> dict:
        """当前侧栏状态（回填收集口径，与 ``set_layer_state`` 对偶）。"""
        state = {
            "curves_soft_clip": self.get_soft_clip_payload(),
            "curves_edit": self.get_edit_payload(),
        }
        if not include_pending:
            if self.automatic_calibration_pending:
                state["curves_edit"].pop(CURVES_CALIBRATION_VERSION_KEY, None)
            if self.soft_calibration_pending:
                state["curves_soft_clip"].pop(CURVES_CALIBRATION_VERSION_KEY, None)
        return state

    def set_layer_state(self, state: dict) -> None:
        """图层/片段回填（任务 7）：屏蔽一切信号与节流发射。

        草稿不跨目标泄漏——每次回填整体覆盖四参数与 Edit 强度；上一目标
        的滑条值不会残留。
        """
        self._soft_timer.stop()
        self._edit_timer.stop()
        raw_soft = state.get("curves_soft_clip")
        raw_edit = state.get("curves_edit")
        soft = raw_soft if isinstance(raw_soft, dict) else {}
        edit = raw_edit if isinstance(raw_edit, dict) else {}
        self.hsl_calibration_pending = (
            curves_calibration_version(edit) == CUSTOM_CURVES_CALIBRATION_VERSION
            and not (state.get("hsl_curve_data") or state.get("has_hsl_curve_lut"))
            and bool(self._use_measured_curves and self._use_measured_curves())
        )
        self._allow_automatic_calibration = (
            not (state.get("curves_data") or state.get("hsl_curve_data") or state.get("has_curve_lut"))
            and CURVES_CALIBRATION_VERSION_KEY not in soft
            and CURVES_CALIBRATION_VERSION_KEY not in edit
            and soft.get("linked", True)
            and edit.get("linked", True)
            and curves_soft_clip_is_identity(soft)
            and curves_edit_is_default(edit)
        )
        self.set_soft_clip_state(soft)
        self._allow_automatic_soft_calibration = (
            not self._soft_version and self.soft_clip_link_btn.isChecked()
            and curves_soft_clip_is_identity(self.get_soft_clip_payload())
        )
        self.set_edit_state(edit)
        self.refresh_automatic_calibration()

    def _set_curve_model(self, version: int) -> None:
        self.curves_widget.set_calibrated(bool(version), version=version)
        hsl_version = HSL_CURVES_CALIBRATION_VERSION if self.hsl_calibration_pending else version
        self.hsl_panel.hsl_curves_widget.set_calibrated(bool(hsl_version), version=hsl_version)

    def get_hsl_calibration_upgrade(self) -> dict:
        state = self.get_layer_state()
        state["curves_edit"][CURVES_CALIBRATION_VERSION_KEY] = HSL_CURVES_CALIBRATION_VERSION
        return state

    def confirm_hsl_calibration_upgrade(self) -> None:
        self.hsl_calibration_pending = False
        self._set_curve_model(HSL_CURVES_CALIBRATION_VERSION)

    def refresh_automatic_calibration(self) -> None:
        measured = bool(self._use_measured_curves and self._use_measured_curves())
        self.automatic_calibration_pending = self._allow_automatic_calibration and measured
        if self._allow_automatic_calibration:
            self._set_curve_model(HSL_CURVES_CALIBRATION_VERSION if measured else 0)
        self.soft_calibration_pending = self._allow_automatic_soft_calibration and measured
        if self._allow_automatic_soft_calibration:
            self._soft_version = SOFT_CLIP_CALIBRATION_VERSION if measured else 0
            self._refresh_soft_rows()

    def confirm_curve_edit(self) -> None:
        """Keep the current model once an edit has been accepted."""
        self._allow_automatic_calibration = False
        self.automatic_calibration_pending = False

    def confirm_soft_clip_edit(self) -> None:
        self._allow_automatic_soft_calibration = False
        self.soft_calibration_pending = False

    def set_soft_clip_state(self, state: dict) -> None:
        """回填四参数柔化裁切（归一化 + 界面刷新，不发渲染信号）。"""
        normalized = normalize_curves_soft_clip(state)
        self._syncing = True
        try:
            self._soft_version = soft_clip_calibration_version(normalized)
            self._soft_values = {
                ch: dict(normalized["channels"][ch]) for ch in "RGB"
            }
            self.soft_clip_link_btn.blockSignals(True)
            self.soft_clip_link_btn.setChecked(normalized["linked"])
            self.soft_clip_link_btn.blockSignals(False)
            self.soft_clip_group.setExclusive(False)
            for name, button in self.soft_clip_buttons.items():
                button.setChecked(
                    normalized["linked"] or name == self._soft_channel)
            self.soft_clip_group.setExclusive(not normalized["linked"])
            self._refresh_soft_rows()
        finally:
            self._syncing = False

    def set_edit_state(self, state: dict) -> None:
        """回填 Edit 强度/链接（归一化 + 行/画布刷新，不发渲染信号）。"""
        normalized = normalize_curves_edit(state)
        self._syncing = True
        try:
            self._set_curve_model(curves_calibration_version(normalized))
            self.curves_widget.set_edit_amounts(normalized["amounts"])
            for channel, row in self.edit_rows.items():
                row.setValue(self.curves_widget.edit_amounts[channel])
            linked = normalized["linked"]
            self.edit_link_btn.blockSignals(True)
            self.edit_link_btn.setChecked(linked)
            self.edit_link_btn.blockSignals(False)
            self.curves_widget.set_edit_linked(linked)
            active = ("Y" if self.curves_widget.active_channel == "M"
                      else self.curves_widget.active_channel)
            self.edit_group.setExclusive(False)
            for channel, button in self.edit_buttons.items():
                button.setChecked(linked or channel == active)
            self.edit_group.setExclusive(not linked)
        finally:
            self._syncing = False

    def _schedule_soft_emit(self) -> None:
        if self._wiring_ready and not self._syncing \
                and not self._soft_timer.isActive():
            self._soft_timer.start()

    def _schedule_edit_emit(self) -> None:
        if self._wiring_ready and not self._syncing \
                and not self._edit_timer.isActive():
            self._edit_timer.start()

    def _emit_soft_now(self) -> None:
        self.soft_clip_changed.emit(self.get_soft_clip_payload())

    def _emit_edit_now(self) -> None:
        self.edit_changed.emit(self.get_edit_payload())

    def _flush_pending(self) -> None:
        """把未决的节流值立即发出（interaction 结束时最终值一次到位）。"""
        if self._soft_timer.isActive():
            self._soft_timer.stop()
            self._emit_soft_now()
        if self._edit_timer.isActive():
            self._edit_timer.stop()
            self._emit_edit_now()

    def _on_row_interaction_ended(self) -> None:
        self._flush_pending()
        self.interaction_ended.emit()

    def _on_soft_interaction_started(self) -> None:
        self.soft_clip_interaction_active = True
        self.interaction_started.emit()

    def _on_soft_interaction_ended(self) -> None:
        self._flush_pending()
        self.soft_clip_interaction_active = False
        self.interaction_ended.emit()
