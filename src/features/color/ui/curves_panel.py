"""Custom curves palette. Sidebar values are UI drafts until model wiring lands."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import (
    QBoxLayout, QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from src.shared.assets.icon_manager import icon_link, icon_reset, icon_unlink
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import (
    C, CURVE_RESET_BTN_STYLE, SCROLL_AREA_TRANSPARENT_STYLE,
)
from src.shared.ui.widgets.curves_widget import CurvesWidget
from src.shared.ui.widgets.param_slider_row import ParamSliderRow


class CurvesPanel(QWidget):
    """Keep the original curve canvas/API, with a Resolve-style control sidebar.

    Edit amounts, link switches and four-parameter soft clipping are deliberately
    local UI state. They must not emit the legacy soft-clip render payload.
    """

    def __init__(self, curves_widget: CurvesWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("curves_panel")
        # Production docks clamp this tool to 780-1680 px, where the canvas
        # and controls stay side by side; narrower embeds fall back to the
        # stacked layout in resizeEvent.
        self.curves_widget = curves_widget
        self.edit_rows: dict[str, ParamSliderRow] = {}
        self.soft_clip_rows: dict[str, ParamSliderRow] = {}
        self.edit_buttons: dict[str, QPushButton] = {}
        self.soft_clip_buttons: dict[str, QPushButton] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 6)
        layout.setSpacing(4)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel(I18N.tr("curves_palette.custom", "Custom")))
        toolbar.addStretch()
        # Reparent the original buttons, retaining their existing slots/state.
        self.picker_btn = curves_widget.picker_btn
        toolbar.addWidget(self.picker_btn)
        self.reset_curves_btn = curves_widget.btn_container.layout().itemAt(
            curves_widget.btn_container.layout().count() - 1
        ).widget()
        toolbar.addWidget(self.reset_curves_btn)
        curves_widget.btn_container.hide()
        layout.addLayout(toolbar)

        self.body = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.body.setSpacing(8)
        layout.addLayout(self.body, 1)
        curves_widget.setMinimumSize(180, 180)
        self.body.addWidget(curves_widget, 1)

        self.sidebar_scroll = QScrollArea(self)
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_scroll.setStyleSheet(SCROLL_AREA_TRANSPARENT_STYLE)
        self.sidebar_scroll.setMinimumWidth(250)
        self.sidebar_scroll.setMaximumWidth(320)
        self.body.setDirection(QBoxLayout.Direction.LeftToRight)
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
            row.valueChanged.connect(lambda value, name=key: self._store_soft_value(name, value))
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

        self.sidebar_scroll.setWidget(self.sidebar)
        self.body.addWidget(self.sidebar_scroll)

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        """Stack the canvas above the controls when the tool drops below
        the side-by-side width (dock layout clamps keep production wide)."""
        super().resizeEvent(event)
        narrow = self.width() < 760
        direction = (QBoxLayout.Direction.TopToBottom if narrow
                     else QBoxLayout.Direction.LeftToRight)
        if self.body.direction() != direction:
            self.body.setDirection(direction)

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
        self.edit_group.setExclusive(False)
        active = "Y" if self.curves_widget.active_channel == "M" else self.curves_widget.active_channel
        for channel, button in self.edit_buttons.items():
            button.setChecked(linked or channel == active)
        self.edit_group.setExclusive(not linked)

    def _reset_edit(self) -> None:
        for row in self.edit_rows.values():
            row.reset()

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

    def _reset_soft_clip(self) -> None:
        targets = "RGB" if self.soft_clip_link_btn.isChecked() else self._soft_channel
        for channel in targets:
            self._soft_values[channel] = dict(self._soft_defaults)
        self._select_soft_channel(self._soft_channel)

    def _reset_soft_parameter(self, key: str) -> None:
        targets = "RGB" if self.soft_clip_link_btn.isChecked() else self._soft_channel
        for channel in targets:
            self._soft_values[channel] = self._clamped_channel(
                self._soft_values[channel], key, self._soft_defaults[key])
        self._refresh_soft_rows()

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
