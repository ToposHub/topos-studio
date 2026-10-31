from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QCheckBox, QGroupBox, QPushButton)
from src.shared.i18n.i18n import I18N
from PyQt6.QtCore import pyqtSignal, QSize, QTimer
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.assets.icon_manager import icon_reset

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

    def get_style_sheet(self):
        """Return panel-specific style sheet.
        
        QGroupBox/QCheckBox/QPushButton use global DARK_THEME styles.
        """
        return """
            FilmGrainPanel {
                background-color: #1a1a1a;
            }
        """

    def init_ui(self) -> None:
        self.setStyleSheet(self.get_style_sheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Enable toggle and reset button row (above Basic Settings)
        enable_row = QHBoxLayout()
        self.enable_check = ToggleSwitch(checked=False)
        self.enable_check.toggled.connect(self.emit_params)

        self._title_label = QLabel(I18N.tr("film_grain.enable", "启用效果"))
        self._title_label.setStyleSheet("color: #bbbbbb; font-weight: bold; font-size: 10px; letter-spacing: 1px;")

        self.reset_btn = QPushButton()
        self.reset_btn.setObjectName("reset_btn")
        self.reset_btn.setIcon(icon_reset())
        self.reset_btn.setIconSize(QSize(12, 12))
        self.reset_btn.setFixedSize(16, 16)
        self.reset_btn.setToolTip(I18N.tr("tooltip.reset_film_grain", "重置所有胶片颗粒参数"))
        self.reset_btn.clicked.connect(self.reset_all)

        enable_row.addWidget(self.enable_check)
        enable_row.addWidget(self._title_label)
        enable_row.addStretch()
        enable_row.addWidget(self.reset_btn)
        layout.addLayout(enable_row)

        layout.addSpacing(4)

        # Main Controls Group
        controls_group = QGroupBox(I18N.tr("film_grain.basic_settings", "基本设置"))
        controls_group.setStyleSheet(self.get_style_sheet())
        controls_layout = QVBoxLayout(controls_group)
        controls_layout.setContentsMargins(4, 0, 4, 4)
        controls_layout.setSpacing(2)

        # 1. Intensity
        self.intensity_control = self._create_row(I18N.tr("film_grain.intensity", "强度"), 0.0, 1.0, 0.25, 0.01, label_width=80)
        controls_layout.addWidget(self.intensity_control)

        # 3. Size
        self.size_control = self._create_row(I18N.tr("film_grain.size", "颗粒大小"), 0.1, 10.0, 2.0, 0.1, label_width=80)
        controls_layout.addWidget(self.size_control)

        # 4. Monochrome
        self.mono_check = QCheckBox(I18N.tr("film_grain.monochrome", "单色颗粒"))
        self.mono_check.setChecked(True)
        # Use global DARK_THEME QCheckBox style
        self.mono_check.stateChanged.connect(self.emit_params)
        controls_layout.addWidget(self.mono_check)

        # P1-COLOR-13: 动画颗粒（seed 随帧号确定推进）+ 显式换种子
        self.animated_check = QCheckBox(
            I18N.tr("film_grain.animated", "动画（随帧变化）"))
        self.animated_check.setChecked(False)
        self.animated_check.setToolTip(I18N.tr(
            "film_grain.animated_tooltip",
            "启用后颗粒种子随帧号确定推进：同一帧重渲一致，逐帧颗粒图案变化"))
        self.animated_check.stateChanged.connect(self.emit_params)
        controls_layout.addWidget(self.animated_check)

        reseed_row = QHBoxLayout()
        reseed_row.addSpacing(80)
        self.reseed_btn = QPushButton(
            I18N.tr("film_grain.reseed", "换种子"))
        self.reseed_btn.setToolTip(I18N.tr(
            "film_grain.reseed_tooltip", "更换颗粒图案种子（可撤销渲染重渲一致）"))
        self.reseed_btn.clicked.connect(self.reseed_requested.emit)
        reseed_row.addWidget(self.reseed_btn)
        reseed_row.addStretch()
        controls_layout.addLayout(reseed_row)

        controls_layout.addStretch()
        layout.addWidget(controls_group)

        # Add spacing between groups
        layout.addSpacing(8)

        # Response Curve Group
        response_group = QGroupBox(I18N.tr("film_grain.luminance_response", "亮度响应"))
        response_group.setStyleSheet(self.get_style_sheet())
        response_layout = QVBoxLayout(response_group)
        response_layout.setContentsMargins(4, 0, 4, 4)
        response_layout.setSpacing(2)

        self.shadows_control = self._create_row(I18N.tr("film_grain.shadows", "阴影"), 0.0, 1.0, 0.0, 0.01, label_width=80)
        response_layout.addWidget(self.shadows_control)

        self.midtones_control = self._create_row(I18N.tr("film_grain.midtones", "中间调"), 0.0, 1.0, 1.0, 0.01, label_width=80)
        response_layout.addWidget(self.midtones_control)

        self.highlights_control = self._create_row(I18N.tr("film_grain.highlights", "高光"), 0.0, 1.0, 0.5, 0.01, label_width=80)
        response_layout.addWidget(self.highlights_control)
        
        response_layout.addStretch()
        layout.addWidget(response_group)
        
        layout.addStretch()

    def _create_row(self, label, min_val, max_val, default_val, step, label_width=80):
        """Create a ParamSliderRow with panel interaction wiring."""
        row = ParamSliderRow(
            label, min_val, max_val, default_val,
            step=step, decimals=2, label_width=label_width)
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

