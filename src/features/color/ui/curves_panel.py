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
    QScrollArea, QSizePolicy, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from src.features.color.core.curves_soft_clip import (
    normalize_curves_edit,
    normalize_curves_soft_clip,
)
from src.shared.assets.icon_manager import icon_link, icon_reset, icon_unlink
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import (
    C, CURVE_RESET_BTN_STYLE, SCROLL_AREA_TRANSPARENT_STYLE,
)
from src.shared.ui.widgets.curves_widget import CurvesWidget
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.ui.widgets.curve_mode_button import CurveModeButton
from src.shared.ui.widgets.hsl_curves_widget import CURVE_MODES
from src.features.color.ui.hsl_curves_panel import HSLCurvesPanel

CURVE_PALETTE_ORDER = ('Custom', 'HueHue', 'HueSat', 'HueLum', 'LumSat', 'SatSat', 'SatLum')


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
                 *, hsl_panel: HSLCurvesPanel | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("curves_panel")
        # 接线守卫先于任何控件连接初始化：构造期的 toggle/setValue 噪声
        # 不得触发节流发射（_wiring_ready 在 __init__ 尾部才放行）。
        self._syncing = False
        self._wiring_ready = False
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
        layout.setContentsMargins(6, 4, 6, 6)
        layout.setSpacing(4)
        toolbar = QHBoxLayout()
        self.mode_title = QLabel(I18N.tr("curves_palette.custom", "Custom"))
        toolbar.addWidget(self.mode_title)
        # Keep the picker beside its active curve mode so it is easy to find.
        self.picker_btn = curves_widget.picker_btn
        toolbar.addWidget(self.picker_btn)
        toolbar.addStretch()
        self.mode_buttons: dict[str, CurveModeButton] = {}
        self.mode_group = QButtonGroup(self)
        for mode in CURVE_PALETTE_ORDER:
            title = self._mode_title(mode)
            button = CurveModeButton(mode, title, self)
            self.mode_buttons[mode] = button
            self.mode_group.addButton(button)
            button.clicked.connect(lambda _checked=False, m=mode: self.set_curve_mode(m))
            toolbar.addWidget(button)
        self.mode_buttons['Custom'].setChecked(True)
        # Reparent the original buttons, retaining their existing slots/state.
        self.reset_curves_btn = curves_widget.btn_container.layout().itemAt(
            curves_widget.btn_container.layout().count() - 1
        ).widget()
        toolbar.addWidget(self.reset_curves_btn)
        self.reset_hsl_btn = self.hsl_panel.hsl_curves_widget.reset_btn
        toolbar.addWidget(self.reset_hsl_btn)
        self.reset_hsl_btn.hide()
        curves_widget.btn_container.hide()
        layout.addLayout(toolbar)

        self.body = QSplitter(Qt.Orientation.Horizontal, self)
        self.body.setChildrenCollapsible(False)
        self.body.setHandleWidth(8)
        self.body.setStyleSheet("""
            QSplitter::handle { background: transparent; }
            QSplitter::handle:hover { background: rgba(255, 122, 0, 72); }
            QSplitter::handle:pressed { background: rgba(255, 122, 0, 128); }
        """)
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
        controls.setContentsMargins(4, 2, 4, 2)
        controls.setSpacing(4)

        self.edit_link_btn = self._link_button("edit_link")
        self.edit_group = QButtonGroup(self)
        controls.addLayout(self._section_header(
            I18N.tr("curves_palette.edit", "Edit"), self.edit_link_btn,
            "YRGB", self.edit_buttons, self.edit_group, self._reset_edit,
        ))
        for channel in "YRGB":
            row = ParamSliderRow(
                channel, 0.0, 100.0, 100.0, step=1.0, decimals=0,
                label_width=16, slider_auto_hide=False, value_fixed_width=44,
            )
            row.label.setStyleSheet(f"color: {self._channel_color(channel)}; font-weight: bold;")
            row.slider.setStyleSheet(self._channel_slider_style(channel))
            row.setObjectName(f"curve_amount_{channel}")
            line = QHBoxLayout()
            line.setSpacing(2)
            line.addWidget(row, 1)
            reset = self._reset_button(
                I18N.tr("curves_palette.reset_amount", "Reset channel amount"),
                color="#95959a",
                size=20,
                icon_size=12,
            )
            reset.clicked.connect(row.reset)
            line.addWidget(reset)
            controls.addLayout(line)
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
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet(f"color: {C.PANEL_DIVIDER};")
        controls.addWidget(divider)

        self.soft_clip_link_btn = self._link_button("soft_clip_link")
        self.soft_clip_group = QButtonGroup(self)
        controls.addLayout(self._section_header(
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
            row_min, row_max = self._soft_row_range(key, self._soft_defaults)
            row = ParamSliderRow(
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
            reset = self._reset_button(
                I18N.tr("curves_palette.reset_parameter", "Reset parameter"),
                color="#95959a",
                size=20,
                icon_size=12,
            )
            reset.clicked.connect(lambda _checked=False, name=key: self._reset_soft_parameter(name))
            line.addWidget(reset)
            controls.addLayout(line)
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
            row.interactionStarted.connect(self.interaction_started)
            row.interactionEnded.connect(self._on_row_interaction_ended)
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
    def _channel_slider_style(cls, channel: str) -> str:
        """Color each YRGB amount handle to match its channel button."""
        color = cls._channel_color(channel)
        return f"""
            QSlider::handle:horizontal {{
                background: {color};
                border: 1px solid #1e1e1e;
                width: 10px;
                height: 10px;
                margin: -4px 0;
                border-radius: 5px;
            }}
            QSlider::handle:horizontal:hover {{
                border: 1px solid #f2f2f2;
            }}
        """

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

    def _reset_button(
        self,
        tooltip: str,
        color: str = "#ff8800",
        size: int = 22,
        icon_size: int = 16,
    ) -> QPushButton:
        button = QPushButton()
        button.setIcon(icon_reset(color))
        button.setIconSize(QSize(icon_size, icon_size))
        button.setFixedSize(size, size)
        button.setStyleSheet(CURVE_RESET_BTN_STYLE)
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
        self.curves_widget.edit_linked = linked
        self.edit_group.setExclusive(False)
        active = "Y" if self.curves_widget.active_channel == "M" else self.curves_widget.active_channel
        for channel, button in self.edit_buttons.items():
            button.setChecked(linked or channel == active)
        self.edit_group.setExclusive(not linked)
        self._schedule_edit_emit()

    def _reset_edit(self) -> None:
        for row in self.edit_rows.values():
            row.reset()
        self._schedule_edit_emit()

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
        self._store_soft_value(key, value)
        self._schedule_soft_emit()

    def _store_soft_value(self, key: str, value: float) -> None:
        targets = "RGB" if self.soft_clip_link_btn.isChecked() else self._soft_channel
        for channel in targets:
            self._soft_values[channel] = self._clamped_channel(
                self._soft_values[channel], key, value)
        self._refresh_soft_rows()

    @staticmethod
    def _clamped_channel(values: dict, key: str, value: float) -> dict:
        """Apply one control change with the shared mobile-app clamps.

        Mirrors ``SoftClipChannelParams.withControl``: clip points stay
        ordered inside [-1, 2] and the two knee widths share the gap
        exclusively so ``low_soft + high_soft <= high_clip - low_clip``.
        """
        out = dict(values)
        gap = max(out["high_clip"] - out["low_clip"], 0.0)
        if key == "low_clip":
            out["low_clip"] = min(max(value, -1.0), min(1.0, out["high_clip"]))
            new_gap = max(out["high_clip"] - out["low_clip"], 0.0)
            out["low_soft"] = min(max(out["low_soft"], 0.0), new_gap)
            out["high_soft"] = min(
                max(out["high_soft"], 0.0), max(new_gap - out["low_soft"], 0.0))
        elif key == "low_soft":
            out["low_soft"] = min(max(value, 0.0), max(gap - out["high_soft"], 0.0))
        elif key == "high_clip":
            out["high_clip"] = min(
                max(value, max(0.0, out["low_clip"])), 2.0)
            new_gap = max(out["high_clip"] - out["low_clip"], 0.0)
            out["low_soft"] = min(max(out["low_soft"], 0.0), new_gap)
            out["high_soft"] = min(
                max(out["high_soft"], 0.0), max(new_gap - out["low_soft"], 0.0))
        else:  # high_soft
            out["high_soft"] = min(max(value, 0.0), max(gap - out["low_soft"], 0.0))
        # Snap onto the 2-decimal UI grid so stored state, the drag box and
        # the slider int domain agree despite binary float noise in `gap`.
        return {name: round(val, 2) for name, val in out.items()}

    @staticmethod
    def _soft_row_range(key: str, values: dict) -> tuple:
        """Slider bounds per control, mirroring the mobile SoftClipPanel.

        Bounds snap to the 2-decimal UI grid to avoid float-noise ticks.
        """
        gap = max(values["high_clip"] - values["low_clip"], 0.0)
        if key == "low_clip":
            bounds = (-1.0, min(1.0, values["high_clip"]))
        elif key == "low_soft":
            bounds = (0.0, max(gap - values["high_soft"], 0.0))
        elif key == "high_clip":
            bounds = (max(0.0, values["low_clip"]), 2.0)
        else:
            bounds = (0.0, max(gap - values["low_soft"], 0.0))
        return (round(bounds[0], 2), round(bounds[1], 2))

    def _refresh_soft_rows(self) -> None:
        """Push the constrained values of the active channel into the rows."""
        channel = self._soft_values[self._soft_channel]
        for key, row in self.soft_clip_rows.items():
            row.blockSignals(True)
            row_min, row_max = self._soft_row_range(key, channel)
            row.setRange(row_min, row_max)
            row.setValue(channel[key])
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
            values = dict(self._soft_values[self._soft_channel])
            self._soft_values = {ch: dict(values) for ch in "RGB"}
        self.soft_clip_group.setExclusive(False)
        for channel, button in self.soft_clip_buttons.items():
            button.setChecked(linked or channel == self._soft_channel)
        self.soft_clip_group.setExclusive(not linked)
        self._refresh_soft_rows()
        self._schedule_soft_emit()

    def _reset_soft_clip(self) -> None:
        targets = "RGB" if self.soft_clip_link_btn.isChecked() else self._soft_channel
        for channel in targets:
            self._soft_values[channel] = dict(self._soft_defaults)
        self._select_soft_channel(self._soft_channel)
        self._schedule_soft_emit()

    def _reset_soft_parameter(self, key: str) -> None:
        targets = "RGB" if self.soft_clip_link_btn.isChecked() else self._soft_channel
        for channel in targets:
            self._soft_values[channel] = self._clamped_channel(
                self._soft_values[channel], key, self._soft_defaults[key])
        self._refresh_soft_rows()
        self._schedule_soft_emit()

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
            "linked": self.soft_clip_link_btn.isChecked(),
            "channels": {ch: dict(values) for ch, values in self._soft_values.items()},
        }

    def get_edit_payload(self) -> dict:
        """Edit 强度/链接的模型形 payload（``edit_changed`` 载荷）。"""
        return {
            "linked": self.edit_link_btn.isChecked(),
            "amounts": self.curves_widget.get_edit_amounts(),
        }

    def get_layer_state(self) -> dict:
        """当前侧栏状态（回填收集口径，与 ``set_layer_state`` 对偶）。"""
        return {
            "curves_soft_clip": self.get_soft_clip_payload(),
            "curves_edit": self.get_edit_payload(),
        }

    def set_layer_state(self, state: dict) -> None:
        """图层/片段回填（任务 7）：屏蔽一切信号与节流发射。

        草稿不跨目标泄漏——每次回填整体覆盖四参数与 Edit 强度；上一目标
        的滑条值不会残留。
        """
        self.set_soft_clip_state(state.get("curves_soft_clip") or {})
        self.set_edit_state(state.get("curves_edit") or {})

    def set_soft_clip_state(self, state: dict) -> None:
        """回填四参数柔化裁切（归一化 + 界面刷新，不发渲染信号）。"""
        normalized = normalize_curves_soft_clip(state)
        self._syncing = True
        try:
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
            self.curves_widget.set_edit_amounts(normalized["amounts"])
            for channel, row in self.edit_rows.items():
                row.setValue(self.curves_widget.edit_amounts[channel])
            linked = normalized["linked"]
            self.edit_link_btn.blockSignals(True)
            self.edit_link_btn.setChecked(linked)
            self.edit_link_btn.blockSignals(False)
            self.curves_widget.edit_linked = linked
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
