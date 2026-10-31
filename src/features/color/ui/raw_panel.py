from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QLabel, QGroupBox)
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import PANEL_TITLE_LABEL_STYLE, PARAM_LABEL_STYLE
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, QSize
import os
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.assets.icon_manager import icon_reset
from src.shared.paths.paths import get_lut_dir

class RawPanel(QWidget):
    """
    Panel for Camera RAW controls including white balance, exposure, 
    color management and tone controls with optimized interaction.
    """
    # Emits raw parameters dictionary
    raw_params_changed = pyqtSignal(dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._is_interacting = False
        self._pending_update = False
        self.input_lut_path = None
        self._syncing_ui = False
        
        # Interaction timeout timer (detects end of activity)
        self.interaction_timer = QTimer()
        self.interaction_timer.setSingleShot(True)
        self.interaction_timer.setInterval(500)
        self.interaction_timer.timeout.connect(self.end_interaction)
        
        # Throttling timer for smooth performance (approx 60fps)
        self.update_timer = QTimer()
        self.update_timer.setSingleShot(True)
        self.update_timer.setInterval(16)
        self.update_timer.timeout.connect(self._emit_params)
        
        self.setMinimumHeight(220)
        self.init_ui()

    def init_ui(self) -> None:
        # 使用全局 QSS，无需本地样式表

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)
        self.setLayout(main_layout)

        # Header with Enable switch
        header_layout = QHBoxLayout()
        self.enable_cb = ToggleSwitch(checked=False)
        self.enable_cb.toggled.connect(self.on_enable_toggled)
        header_layout.addWidget(self.enable_cb)

        self._title_label = QLabel(I18N.tr("raw.title", "RAW 控制"))
        self._title_label.setStyleSheet(PANEL_TITLE_LABEL_STYLE)
        header_layout.addWidget(self._title_label)
        
        header_layout.addStretch()
        
        self.reset_btn = QPushButton()
        self.reset_btn.setObjectName("reset_btn")
        self.reset_btn.setIcon(icon_reset())
        self.reset_btn.setIconSize(QSize(12, 12))
        self.reset_btn.setFixedSize(16, 16)
        self.reset_btn.setToolTip(I18N.tr(
            "raw.reset_tooltip", "Reset All RAW Parameters"))
        self.reset_btn.clicked.connect(self.reset_all)
        header_layout.addWidget(self.reset_btn)
        main_layout.addLayout(header_layout)

        # Main 3-Column Container
        self.container = QWidget()
        columns_layout = QHBoxLayout()
        columns_layout.setContentsMargins(0, 0, 0, 0)
        columns_layout.setSpacing(6)
        self.container.setLayout(columns_layout)
        main_layout.addWidget(self.container)

        # --- Column 1: Color Space & Basics ---
        col1 = QGroupBox(I18N.tr("raw.color_management", "色彩管理"))
        col1_layout = QVBoxLayout()
        col1_layout.setContentsMargins(4, 0, 4, 4)
        col1_layout.setSpacing(6)
        col1.setLayout(col1_layout)
        
        self.decode_combo = self.create_labeled_combo(I18N.tr("raw.decode_using", "解码方式:"), ["Clip"])
        col1_layout.addLayout(self.decode_combo['layout'])
        
        self.wb_combo = self.create_labeled_combo(I18N.tr("raw.white_balance", "白平衡:"), ["As Shot", "Auto", "Custom"])
        self.wb_combo['combo'].currentIndexChanged.connect(self.update_wb_visibility)
        col1_layout.addLayout(self.wb_combo['layout'])

        self.color_space_combo = self.create_labeled_combo(I18N.tr("raw.color_space", "色彩空间:"), [
            "sRGB", "Rec.709", "Rec.2020", "Adobe RGB", "DCI-P3", "Display P3",
            "ACEScg", "ARRI Wide Gamut 3", "Sony S-Gamut3", "Panasonic V-Gamut", "DaVinci Wide Gamut", 
            "REDWideGamutRGB", "Canon Cinema Gamut", "Fujifilm F-Gamut", "BMD Film Gen5", "CMYK Simulation"
        ])
        col1_layout.addLayout(self.color_space_combo['layout'])

        self.gamma_combo = self.create_labeled_combo(I18N.tr("raw.gamma", "伽马:"), [
            "Linear", "sRGB", "Rec.709", "Gamma 2.2", "Gamma 2.4", "Gamma 2.6", 
            "Rec.2100 HLG", "sLog3", "ARRI LogC3", "ACEScct", "Panasonic V-Log", "DaVinci Intermediate",
            "RED Log3G10", "Canon Log 2", "Canon Log 3", "Fujifilm F-Log", "Fujifilm F-Log2", "Apple Log"
        ])
        col1_layout.addLayout(self.gamma_combo['layout'])

        # Input LUT
        self.lut_combo = self.create_labeled_combo(I18N.tr("raw.input_lut", "输入 LUT:"), ["None"])
        self.load_lut_list()
        col1_layout.addLayout(self.lut_combo['layout'])
        
        col1_layout.addStretch()
        columns_layout.addWidget(col1, 0) # Use 0 to let it take minimum space

        # --- Column 2: White Balance & Primary ---
        col2 = QGroupBox(I18N.tr("raw.primary", "基本控制"))
        col2_layout = QVBoxLayout()
        col2_layout.setContentsMargins(4, 0, 4, 4)
        col2_layout.setSpacing(6)
        col2.setLayout(col2_layout)

        self.temp_control = self._create_row(I18N.tr("raw.temp", "色温"), 2000, 12000, 6500, 100)
        col2_layout.addWidget(self.temp_control)
        
        self.tint_control = self._create_row(I18N.tr("raw.tint", "色调"), -100, 100, 0, 1)
        col2_layout.addWidget(self.tint_control)
        
        self.exp_control = self._create_row(I18N.tr("raw.exposure", "曝光"), -5.0, 5.0, 0.0, 0.1)
        col2_layout.addWidget(self.exp_control)
        
        self.sharp_control = self._create_row(I18N.tr("raw.sharpness", "锐度"), 0, 100, 0, 1)
        col2_layout.addWidget(self.sharp_control)

        col2_layout.addStretch()
        columns_layout.addWidget(col2, 1)

        # --- Column 3: Advanced Tone Control ---
        col3 = QGroupBox(I18N.tr("raw.tone_detail", "色调与细节"))
        col3_layout = QVBoxLayout()
        col3_layout.setContentsMargins(4, 0, 4, 4)
        col3_layout.setSpacing(6)
        col3.setLayout(col3_layout)

        self.shadows_control = self._create_row(I18N.tr("raw.shadows", "阴影"), -100, 100, 0, 1)
        col3_layout.addWidget(self.shadows_control)

        self.highlights_control = self._create_row(I18N.tr("raw.highlights", "高光"), -100, 100, 0, 1)
        col3_layout.addWidget(self.highlights_control)

        self.color_boost_control = self._create_row(I18N.tr("raw.color_boost", "色彩增强"), 0, 100, 0, 1)
        col3_layout.addWidget(self.color_boost_control)

        self.sat_control = self._create_row(I18N.tr("raw.saturation", "饱和度"), 0, 200, 100, 1)
        col3_layout.addWidget(self.sat_control)

        self.midtone_control = self._create_row(I18N.tr("raw.mid_detail", "中间细节"), -100, 100, 0, 1)
        col3_layout.addWidget(self.midtone_control)
        
        self.lift_control = self._create_row(I18N.tr("raw.lift", "提升"), -1.0, 1.0, 0.0, 0.01)
        col3_layout.addWidget(self.lift_control)

        self.gain_control = self._create_row(I18N.tr("raw.gain", "增益"), 0.0, 4.0, 1.0, 0.01)
        col3_layout.addWidget(self.gain_control)

        self.contrast_control = self._create_row(I18N.tr("raw.contrast", "对比度"), 0.0, 2.0, 1.0, 0.01)
        col3_layout.addWidget(self.contrast_control)

        col3_layout.addStretch()
        columns_layout.addWidget(col3, 1)

        main_layout.addStretch()

        # Initial State
        self.update_wb_visibility()
        self.on_enable_toggled(self.enable_cb.isChecked())

    def _create_row(self, label, min_val, max_val, default_val, step):
        """Create a ParamSliderRow with panel interaction wiring."""
        decimals = 0 if step >= 1 else 2
        row = ParamSliderRow(
            label, min_val, max_val, default_val,
            step=step, decimals=decimals, label_width=65)
        row.interactionStarted.connect(self.start_interaction)
        row.interactionEnded.connect(self.end_interaction)
        row.valueChanged.connect(lambda _: self.on_param_changed())
        return row
    
    def create_labeled_combo(self, label_text, items):
        layout = QHBoxLayout()
        layout.setSpacing(4)
        label = QLabel(label_text)
        label.setFixedWidth(75) # Balanced width to show name but keep it narrow
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        label.setStyleSheet(PARAM_LABEL_STYLE)
        
        combo = ChevronComboBox()
        combo.setMaximumWidth(110) # UI stay narrow
        combo.addItems(items)
        combo.setMinimumHeight(18)
        combo.view().setMinimumWidth(200)
        combo.currentIndexChanged.connect(self.on_control_interacted)
        
        layout.addWidget(label)
        layout.addWidget(combo)
        return {'layout': layout, 'combo': combo}

    def load_lut_list(self) -> None:
        """Scans the luts/ directory and populates the lut_combo."""
        self.lut_combo['combo'].blockSignals(True)
        self.lut_combo['combo'].clear()
        self.lut_combo['combo'].addItem(I18N.tr("raw.none", "无"), None)
        
        lut_dir = get_lut_dir()
        if os.path.exists(lut_dir):
            for f in sorted(os.listdir(lut_dir)):
                if f.lower().endswith(".cube"):
                    full_path = os.path.join(lut_dir, f)
                    self.lut_combo['combo'].addItem(f, full_path)
        
        self.lut_combo['combo'].blockSignals(False)

    def on_enable_toggled(self, enabled) -> None:
        self.container.setEnabled(enabled)
        self.on_control_interacted()

    def update_wb_visibility(self) -> None:
        # We don't hide controls anymore in this new layout, 
        # but we might want to disable them if not Custom
        is_custom = self.wb_combo['combo'].currentText() == "Custom"
        self.temp_control.setEnabled(is_custom)
        self.tint_control.setEnabled(is_custom)

    def start_interaction(self) -> None:
        self.interaction_timer.stop()
        if not self._is_interacting:
            self._is_interacting = True
            self.interaction_started.emit()

    def end_interaction(self) -> None:
        self.interaction_timer.stop()
        if self._is_interacting:
            self._is_interacting = False
            self.interaction_ended.emit()

    def on_control_interacted(self) -> None:
        """Called by non-slider controls to simulate interaction state."""
        self.start_interaction()
        self.on_param_changed()
        self.interaction_timer.start()

    def on_param_changed(self) -> None:
        """Triggered when any parameter changes. Marks update as pending for the throttling timer."""
        if self._syncing_ui:
            return
        self._pending_update = True
        if not self.update_timer.isActive():
            self.update_timer.start()

    def _emit_params(self) -> None:
        """Throttled emission of raw parameters to prevent UI lag."""
        if self._pending_update:
            params = self.on_param_changed_return_dict()
            self.raw_params_changed.emit(params)
            self._pending_update = False

    def set_params(self, params, emit=False) -> None:
        if not params: return
        self._syncing_ui = True
        self.update_timer.stop()
        self._pending_update = False
        
        widgets = [
            self.enable_cb,
            self.decode_combo['combo'], self.wb_combo['combo'], 
            self.color_space_combo['combo'], self.gamma_combo['combo'], 
            self.lut_combo['combo'],
            self.temp_control, self.tint_control, 
            self.exp_control, self.sharp_control,
            self.shadows_control, self.highlights_control,
            self.color_boost_control, self.sat_control,
            self.midtone_control, self.lift_control,
            self.gain_control, self.contrast_control
        ]
        for w in widgets:
            w.blockSignals(True)
        
        self.enable_cb.setChecked(params.get('enabled', False))
        
        if 'decode_using' in params:
            self.decode_combo['combo'].setCurrentText(params['decode_using'])
            
        if 'wb_type' in params:
            wb_t = params['wb_type']
            if wb_t == 'camera': self.wb_combo['combo'].setCurrentText("As Shot")
            elif wb_t == 'auto': self.wb_combo['combo'].setCurrentText("Auto")
            else: self.wb_combo['combo'].setCurrentText("Custom")
            
        if 'color_space' in params:
            self.color_space_combo['combo'].setCurrentText(params['color_space'])
            
        if 'gamma' in params:
            self.gamma_combo['combo'].setCurrentText(params['gamma'])
            
        if 'input_lut' in params:
            idx = self.lut_combo['combo'].findData(params['input_lut'])
            if idx >= 0: self.lut_combo['combo'].setCurrentIndex(idx)
            
        def set_val(control, key) -> None:
            if key in params:
                control.spin.setValue(float(params[key]))
                
        set_val(self.temp_control, 'temp')
        set_val(self.tint_control, 'tint')
        set_val(self.exp_control, 'exposure')
        set_val(self.sharp_control, 'sharpness')
        
        set_val(self.shadows_control, 'shadows')
        set_val(self.highlights_control, 'highlights')
        set_val(self.color_boost_control, 'color_boost')
        set_val(self.sat_control, 'saturation')
        set_val(self.midtone_control, 'mid_detail')
        set_val(self.lift_control, 'lift')
        set_val(self.gain_control, 'gain')
        set_val(self.contrast_control, 'contrast')

        for w in widgets:
            w.blockSignals(False)
        
        self._syncing_ui = False
        
        self.update_wb_visibility()
        self.container.setEnabled(self.enable_cb.isChecked())
        
        if emit:
            self._pending_update = True
            self._emit_params()

    def on_param_changed_return_dict(self):
        wb_text = self.wb_combo['combo'].currentText()
        wb_type = "camera" if wb_text == "As Shot" else wb_text.lower()
        
        # Always return the full set of parameters to ensure the engine 
        # can reconcile the state (like clearing LUTs) even when 'enabled' is false.
        params = {
            'enabled': self.enable_cb.isChecked(),
            'decode_using': self.decode_combo['combo'].currentText(),
            'wb_type': wb_type,
            'color_space': self.color_space_combo['combo'].currentText(),
            'gamma': self.gamma_combo['combo'].currentText(),
            'input_lut': self.lut_combo['combo'].currentData(),
            
            'temp': self.temp_control.spin.value(),
            'tint': self.tint_control.spin.value(),
            'exposure': self.exp_control.spin.value(),
            'sharpness': self.sharp_control.spin.value(),
            
            'shadows': self.shadows_control.spin.value(),
            'highlights': self.highlights_control.spin.value(),
            'color_boost': self.color_boost_control.spin.value(),
            'saturation': self.sat_control.spin.value(),
            'mid_detail': self.midtone_control.spin.value(),
            'lift': self.lift_control.spin.value(),
            'gain': self.gain_control.spin.value(),
            'contrast': self.contrast_control.spin.value(),
        }
        
        return params

    def reset_all(self) -> None:
        """Resets all RAW parameters to their default values with robust signal management."""
        self.blockSignals(True)
        
        # 1. Collect all widgets to block signals during mass update
        # This prevents redundant "interaction_started" signals and UI flickering.
        widgets = [
            self.enable_cb,
            self.decode_combo['combo'], self.wb_combo['combo'], 
            self.color_space_combo['combo'], self.gamma_combo['combo'], 
            self.lut_combo['combo'],
            self.temp_control, self.tint_control, 
            self.exp_control, self.sharp_control,
            self.shadows_control, self.highlights_control,
            self.color_boost_control, self.sat_control,
            self.midtone_control, self.lift_control,
            self.gain_control, self.contrast_control
        ]
        for w in widgets:
            w.blockSignals(True)

        # 2. Reset UI values
        self.decode_combo['combo'].setCurrentIndex(0)
        self.wb_combo['combo'].setCurrentIndex(0) # As Shot
        self.color_space_combo['combo'].setCurrentText("sRGB") 
        self.gamma_combo['combo'].setCurrentText("sRGB")
        
        # Explicitly find and select "None" to be 100% sure
        none_idx = self.lut_combo['combo'].findText("None")
        if none_idx >= 0:
            self.lut_combo['combo'].setCurrentIndex(none_idx)
        elif self.lut_combo['combo'].count() > 0:
            self.lut_combo['combo'].setCurrentIndex(0)
        
        self.temp_control.setValue(6500)
        self.tint_control.setValue(0)
        self.exp_control.setValue(0.0)
        self.sharp_control.setValue(0)
        
        self.shadows_control.setValue(0)
        self.highlights_control.setValue(0)
        self.color_boost_control.setValue(0)
        self.sat_control.setValue(100)
        self.midtone_control.setValue(0)
        self.lift_control.setValue(0.0)
        self.gain_control.setValue(1.0)
        self.contrast_control.setValue(1.0)
        
        # 3. Restore signals and update internal state
        for w in widgets:
            w.blockSignals(False)
        self.blockSignals(False)
        
        # 4. Synchronize UI visibility
        self.update_wb_visibility()
        self.on_enable_toggled(self.enable_cb.isChecked())
        
        # 5. CRITICAL: Force interaction state to False and notify engine
        # This ensures the next update is treated as a final change (triggering full quality decode)
        self._is_interacting = False
        self.interaction_ended.emit() 
        
        # 6. Immediate dispatch of the clean state
        self._pending_update = True
        self._emit_params()

    def set_enabled_for_file(self, is_raw) -> None:
        # Always enable the panel to allow manual override for non-RAW images
        self.setEnabled(True)
        self._syncing_ui = True
        self.update_timer.stop()
        self._pending_update = False
        widgets = [
            self.enable_cb,
            self.exp_control,
            self.color_space_combo['combo'], self.gamma_combo['combo'],
        ]
        for widget in widgets:
            widget.blockSignals(True)
        
        if is_raw:
            self.enable_cb.setChecked(True)
            self.exp_control.setValue(0.5)
        else:
            # For non-RAW, default to disabled but allow user to check it
            self.enable_cb.setChecked(False)
            self.exp_control.setValue(0.0)
        
        # Default to sRGB for both
        self.color_space_combo['combo'].setCurrentText("sRGB")
        self.gamma_combo['combo'].setCurrentText("sRGB")
        
        for widget in widgets:
            widget.blockSignals(False)
        self._syncing_ui = False
        
        # Container visibility is only controlled by the checkbox now
        self.container.setEnabled(self.enable_cb.isChecked())

    # NOTE: 不重写 resizeEvent — raw_panel 用 3 列布局，
    # 每列中的 ParamSliderRow 宽度 ≠ 面板宽度，
    # 由 ParamSliderRow.resizeEvent 根据自身实际宽度判断 slider 显隐。

