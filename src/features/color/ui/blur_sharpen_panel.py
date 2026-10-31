from PyQt6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
                             QStackedWidget, QLabel)
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from PyQt6.QtCore import pyqtSignal, QSize, QTimer
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.assets.icon_manager import icon_reset


class BlurSharpenPanel(QWidget):
    """
    Panel for Blur/Sharpen controls with unified UI style matching RawPanel.
    """
    # Emits a dictionary of parameters
    effects_changed = pyqtSignal(dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    reset_clicked = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.params = {
            'enabled': False,
            'mode': 0,  # 0 = Basic, 1 = Advanced
            'sharpen': 0,
            'texture': 0,
            'halo': 0,
            'radius': 0.0,
            'iterations': 1,
            # Blur parameters
            'blur_enabled': False,
            'blur_mode': 0,  # 0 = Texture Soften, 1 = Optical Blur, 2 = Motion, 3 = Fast
            'blur_intensity': 0,
            'blur_detail': 0,
            'blur_radius': 0,
            'blur_bokeh': 0,
            'blur_angle': 0,
        }
        # Throttle slider-driven updates to ~60 FPS
        self._param_timer = QTimer()
        self._param_timer.setSingleShot(True)
        self._param_timer.timeout.connect(self._emit_pending_params)
        self._pending_params = None
        self._syncing_ui = False

        self.init_ui()

    def get_style_sheet(self):
        """Return panel-specific style sheet.
        
        QGroupBox/QComboBox/QSpinBox/QPushButton use global DARK_THEME styles.
        """
        return """
            BlurSharpenPanel {
                background-color: #1a1a1a;
            }
        """

    def init_ui(self) -> None:
        self.setStyleSheet(self.get_style_sheet())

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)
        self.setLayout(main_layout)

        # Header with Enable switch
        header_layout = QHBoxLayout()
        self.enable_cb = ToggleSwitch(checked=False)
        self.enable_cb.toggled.connect(self.on_enable_toggled)
        header_layout.addWidget(self.enable_cb)

        self._title_label = QLabel(I18N.tr("blur_sharpen.sharpen", "锐化"))
        self._title_label.setStyleSheet("color: #888; font-weight: bold; font-size: 10px; letter-spacing: 1px;")
        header_layout.addWidget(self._title_label)
        
        header_layout.addStretch()
        
        # Mode Selection
        mode_label = QLabel(I18N.tr("blur_sharpen.mode", "模式:"))
        mode_label.setStyleSheet("color: #666; font-size: 9px;")
        self.mode_combo = ChevronComboBox()
        self.mode_combo.addItem(I18N.tr("blur_sharpen.basic", "基础"))
        self.mode_combo.addItem(I18N.tr("blur_sharpen.advanced", "高级"))
        self.mode_combo.currentIndexChanged.connect(self.on_mode_changed)
        
        header_layout.addWidget(mode_label)
        header_layout.addWidget(self.mode_combo)
        header_layout.addStretch()
        
        # Reset All Button - 使用 icon_manager
        self.reset_btn = QPushButton()
        self.reset_btn.setObjectName("reset_btn")
        self.reset_btn.setIcon(icon_reset())
        self.reset_btn.setIconSize(QSize(12, 12))
        self.reset_btn.setFixedSize(16, 16)
        self.reset_btn.setToolTip(I18N.tr(
            "blur_sharpen.reset_sharpen_tooltip", "Reset All Sharpen Parameters"))
        self.reset_btn.clicked.connect(self.reset_all)
        header_layout.addWidget(self.reset_btn)
        
        main_layout.addLayout(header_layout)

        # Container for all parameter controls
        self.container = QWidget()
        container_layout = QVBoxLayout()
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(4)
        self.container.setLayout(container_layout)
        main_layout.addWidget(self.container)

        # Stacked Widget for Modes
        self.stack = QStackedWidget()
        
        # --- Basic Mode Page ---
        basic_page = QWidget()
        basic_layout = QVBoxLayout(basic_page)
        basic_layout.setContentsMargins(0, 0, 0, 0)
        basic_layout.setSpacing(4)
        
        from src.shared.config.config_manager import ConfigManager
        _cfg_sharpen = ConfigManager().get("default_sharpen_amount") or 0
        self.sharpen_slider = self._create_row(I18N.tr("blur_sharpen.sharpen_label", "锐化"), 0, 100, _cfg_sharpen)
        self.texture_slider = self._create_row(I18N.tr("blur_sharpen.texture", "纹理"), 0, 100, 0)
        self.halo_slider = self._create_row(I18N.tr("blur_sharpen.halo", "光晕"), 0, 100, 0)
        
        basic_layout.addWidget(self.sharpen_slider)
        basic_layout.addWidget(self.texture_slider)
        basic_layout.addWidget(self.halo_slider)
        basic_layout.addStretch()
        
        self.stack.addWidget(basic_page)

        # --- Advanced Mode Page ---
        adv_page = QWidget()
        adv_layout = QVBoxLayout(adv_page)
        adv_layout.setContentsMargins(0, 0, 0, 0)
        adv_layout.setSpacing(4)
        
        # Duplicate Sharpen slider for advanced mode
        self.adv_sharpen_slider = self._create_row(I18N.tr("blur_sharpen.sharpen_label", "锐化"), 0, 100, 0)
        # Radius: 0.0 - 10.0 (float, step=0.1)
        self.radius_slider = self._create_row(I18N.tr("blur_sharpen.radius", "半径"), 0, 10, 0, step=0.1, suffix=" px")
        # Iterations: 1 - 10 (int)
        self.iter_slider = self._create_row(I18N.tr("blur_sharpen.iterations", "迭代次数"), 1, 10, 1, step=1, decimals=0)
        
        adv_layout.addWidget(self.adv_sharpen_slider)
        adv_layout.addWidget(self.radius_slider)
        adv_layout.addWidget(self.iter_slider)
        adv_layout.addStretch()
        
        self.stack.addWidget(adv_page)
        
        # Sync the two Sharpen sliders
        self._sync_rows(self.sharpen_slider, self.adv_sharpen_slider)
        
        container_layout.addWidget(self.stack)
        container_layout.addStretch()
        
        # Separator line between Sharpen and Blur
        separator = QWidget()
        separator.setFixedHeight(1)
        separator.setStyleSheet("background-color: #333;")
        main_layout.addWidget(separator)
        
        # Blur Section Header
        blur_header_layout = QHBoxLayout()
        blur_header_layout.setContentsMargins(0, 8, 0, 4)
        
        self.blur_enable_cb = ToggleSwitch(checked=False)
        self.blur_enable_cb.toggled.connect(self.on_blur_enable_toggled)
        blur_header_layout.addWidget(self.blur_enable_cb)

        self._blur_title_label = QLabel(I18N.tr("blur_sharpen.blur", "模糊"))
        self._blur_title_label.setStyleSheet("color: #888; font-weight: bold; font-size: 10px; letter-spacing: 1px;")
        blur_header_layout.addWidget(self._blur_title_label)
        
        blur_header_layout.addStretch()
        
        # Blur Mode Selection
        blur_mode_label = QLabel(I18N.tr("blur_sharpen.type", "类型:"))
        blur_mode_label.setStyleSheet("color: #666; font-size: 9px;")
        self.blur_mode_combo = ChevronComboBox()
        self.blur_mode_combo.addItem(I18N.tr("blur_sharpen.texture_soften", "质感模糊"))  # 质感模糊
        self.blur_mode_combo.addItem(I18N.tr("blur_sharpen.optical_blur", "光学虚化"))    # 光学虚化
        self.blur_mode_combo.addItem(I18N.tr("blur_sharpen.motion", "动感模糊"))          # 动感模糊
        self.blur_mode_combo.addItem(I18N.tr("blur_sharpen.fast_blur", "快速模糊"))       # 快速模糊
        self.blur_mode_combo.currentIndexChanged.connect(self.on_blur_mode_changed)
        blur_header_layout.addWidget(blur_mode_label)
        blur_header_layout.addWidget(self.blur_mode_combo)
        blur_header_layout.addStretch()
        
        # Blur Reset Button - 使用 icon_manager
        self.blur_reset_btn = QPushButton()
        self.blur_reset_btn.setObjectName("reset_btn")
        self.blur_reset_btn.setIcon(icon_reset())
        self.blur_reset_btn.setIconSize(QSize(12, 12))
        self.blur_reset_btn.setFixedSize(16, 16)
        self.blur_reset_btn.setToolTip(I18N.tr(
            "blur_sharpen.reset_blur_tooltip", "Reset All Blur Parameters"))
        self.blur_reset_btn.clicked.connect(self.reset_blur)
        blur_header_layout.addWidget(self.blur_reset_btn)
        
        main_layout.addLayout(blur_header_layout)
        
        # Blur Parameter Container
        self.blur_container = QWidget()
        blur_container_layout = QVBoxLayout()
        blur_container_layout.setContentsMargins(0, 0, 0, 0)
        blur_container_layout.setSpacing(4)
        self.blur_container.setLayout(blur_container_layout)
        main_layout.addWidget(self.blur_container)
        
        # Stacked Widget for Blur Modes
        self.blur_stack = QStackedWidget()
        
        # --- Texture Soften Page (Bilateral) ---
        texture_page = QWidget()
        texture_layout = QVBoxLayout(texture_page)
        texture_layout.setContentsMargins(0, 0, 0, 0)
        texture_layout.setSpacing(4)
        self.blur_intensity_slider = self._create_row(I18N.tr("blur_sharpen.intensity", "强度"), 0, 100, 0)
        self.blur_detail_slider = self._create_row(I18N.tr("blur_sharpen.detail", "细节"), 0, 100, 50)
        texture_layout.addWidget(self.blur_intensity_slider)
        texture_layout.addWidget(self.blur_detail_slider)
        texture_layout.addStretch()
        self.blur_stack.addWidget(texture_page)
        
        # --- Optical Blur Page (Lens/Bokeh) ---
        optical_page = QWidget()
        optical_layout = QVBoxLayout(optical_page)
        optical_layout.setContentsMargins(0, 0, 0, 0)
        optical_layout.setSpacing(4)
        self.blur_radius_slider = self._create_row(I18N.tr("blur_sharpen.radius", "半径"), 0, 100, 0)
        self.blur_bokeh_slider = self._create_row(I18N.tr("blur_sharpen.bokeh", "散景"), 0, 100, 0)
        optical_layout.addWidget(self.blur_radius_slider)
        optical_layout.addWidget(self.blur_bokeh_slider)
        optical_layout.addStretch()
        self.blur_stack.addWidget(optical_page)
        
        # --- Motion Blur Page ---
        motion_page = QWidget()
        motion_layout = QVBoxLayout(motion_page)
        motion_layout.setContentsMargins(0, 0, 0, 0)
        motion_layout.setSpacing(4)
        self.blur_motion_intensity_slider = self._create_row(I18N.tr("blur_sharpen.intensity", "强度"), 0, 100, 0)
        self.blur_angle_slider = self._create_row(I18N.tr("blur_sharpen.angle", "角度"), 0, 360, 0)
        motion_layout.addWidget(self.blur_motion_intensity_slider)
        motion_layout.addWidget(self.blur_angle_slider)
        motion_layout.addStretch()
        self.blur_stack.addWidget(motion_page)
        
        # --- Fast Blur Page (Stack Blur) ---
        fast_page = QWidget()
        fast_layout = QVBoxLayout(fast_page)
        fast_layout.setContentsMargins(0, 0, 0, 0)
        fast_layout.setSpacing(4)
        self.blur_fast_intensity_slider = self._create_row(I18N.tr("blur_sharpen.intensity", "强度"), 0, 100, 0)
        self.blur_fast_radius_slider = self._create_row(I18N.tr("blur_sharpen.radius", "半径"), 0, 100, 0)
        fast_layout.addWidget(self.blur_fast_intensity_slider)
        fast_layout.addWidget(self.blur_fast_radius_slider)
        fast_layout.addStretch()
        self.blur_stack.addWidget(fast_page)
        
        blur_container_layout.addWidget(self.blur_stack)
        blur_container_layout.addStretch()
        
        main_layout.addStretch()

        # Initial State
        self.on_enable_toggled(self.enable_cb.isChecked())
        self.on_blur_enable_toggled(self.blur_enable_cb.isChecked())

    def _create_row(self, label, min_val, max_val, default_val, step=1.0,
                    decimals=0, suffix="", label_width=65):
        """Create a ParamSliderRow with panel interaction wiring."""
        row = ParamSliderRow(
            label, min_val, max_val, default_val,
            step=step, decimals=decimals, suffix=suffix,
            label_width=label_width)
        row.interactionStarted.connect(self.interaction_started.emit)
        row.interactionEnded.connect(self.interaction_ended.emit)
        row.interactionEnded.connect(self._flush_pending_params)
        row.valueChanged.connect(lambda _: self.on_param_changed())
        return row

    def _sync_rows(self, row1, row2) -> None:
        """Bidirectionally sync two ParamSliderRow values."""
        _syncing = [False]

        def row1_to_row2(val) -> None:
            if _syncing[0]:
                return
            _syncing[0] = True
            row2.blockSignals(True)
            row2.setValue(row1.value())
            row2.blockSignals(False)
            _syncing[0] = False

        def row2_to_row1(val) -> None:
            if _syncing[0]:
                return
            _syncing[0] = True
            row1.blockSignals(True)
            row1.setValue(row2.value())
            row1.blockSignals(False)
            _syncing[0] = False

        row1.valueChanged.connect(row1_to_row2)
        row2.valueChanged.connect(row2_to_row1)

    def on_enable_toggled(self, enabled) -> None:
        """Handle enable checkbox toggle."""
        self.container.setEnabled(enabled)
        self.on_param_changed()

    def on_mode_changed(self, index) -> None:
        self.params['mode'] = index
        self.stack.setCurrentIndex(index)
        self.on_param_changed()

    def on_blur_enable_toggled(self, enabled) -> None:
        """Handle blur enable checkbox toggle."""
        self.blur_container.setEnabled(enabled)
        self.on_param_changed()

    def on_blur_mode_changed(self, index) -> None:
        """Handle blur mode selection change."""
        self.params['blur_mode'] = index
        self.blur_stack.setCurrentIndex(index)
        self.on_param_changed()

    def _sync_mode_pages(self) -> None:
        """把下拉框当前索引同步到对应堆栈页面（AUD-25105/25106）。

        on_mode_changed / on_blur_mode_changed 是唯一更新页面的地方；
        set_params_dict 与 reset 路径在 blockSignals 下设置 combo 索引，
        这两个槽不会触发，必须在此显式同步——否则状态恢复/重置后 combo
        显示"高级/光学虚化"而堆栈仍停留在"基础/质感模糊"页。
        越界索引（-1，来自损坏的工程数据）跳过对应堆栈。
        """
        mode = self.mode_combo.currentIndex()
        if 0 <= mode < self.stack.count():
            self.stack.setCurrentIndex(mode)
        blur_mode = self.blur_mode_combo.currentIndex()
        if 0 <= blur_mode < self.blur_stack.count():
            self.blur_stack.setCurrentIndex(blur_mode)

    def on_param_changed(self) -> None:
        """Schedule throttled parameter emission."""
        if self._syncing_ui:
            return
        self._schedule_param_update()

    def _schedule_param_update(self) -> None:
        """Schedule a throttled parameter update (~60 FPS)."""
        self._pending_params = self.get_params_dict()
        if not self._param_timer.isActive():
            self._param_timer.start(16)

    def _emit_pending_params(self) -> None:
        """Emit pending params (called by timer timeout)."""
        if self._syncing_ui:
            return
        if self._pending_params is not None:
            self.effects_changed.emit(self._pending_params)
            self._pending_params = None

    def _flush_pending_params(self) -> None:
        """Immediately emit any pending params (called on slider release)."""
        if self._param_timer.isActive():
            self._param_timer.stop()
        self._emit_pending_params()

    def get_params_dict(self):
        """Get current parameters as dictionary.

        禁用分支只置 ``enabled``/``blur_enabled``=False 并保留滑条当前值
        （shader 端按旗标跳过计算，数值无需清零）。此前禁用时把参数全部
        置 0 且 ``set_effects_params`` 是整表替换——保存/重载或调色复制后
        用户的参数被静默清零。
        """
        # Sharpen parameters
        sharpen_enabled = self.enable_cb.isChecked()

        sharpen_params = {
            'enabled': sharpen_enabled,
            'mode': self.mode_combo.currentIndex(),
            # Note: sharpen value is synced between self.sharpen_slider and self.adv_sharpen_slider
            'sharpen': float(self.sharpen_slider.value()),
            'texture': float(self.texture_slider.value()),
            'halo': float(self.halo_slider.value()),
            'radius': float(self.radius_slider.value()),
            'iterations': int(self.iter_slider.value())
        }

        # Blur parameters
        blur_enabled = self.blur_enable_cb.isChecked()
        blur_mode = self.blur_mode_combo.currentIndex()

        # Map parameters based on active mode
        b_intensity = 0.0
        b_detail = 0.0
        b_radius = 0.0
        b_bokeh = 0.0
        b_angle = 0.0

        if blur_mode == 0: # Texture Soften
            b_intensity = float(self.blur_intensity_slider.value())
            b_detail = float(self.blur_detail_slider.value())
        elif blur_mode == 1: # Optical Blur
            b_radius = float(self.blur_radius_slider.value())
            b_bokeh = float(self.blur_bokeh_slider.value())
        elif blur_mode == 2: # Motion
            b_intensity = float(self.blur_motion_intensity_slider.value())
            b_angle = float(self.blur_angle_slider.value())
        elif blur_mode == 3: # Fast Blur
            b_intensity = float(self.blur_fast_intensity_slider.value())
            b_radius = float(self.blur_fast_radius_slider.value())

        blur_params = {
            'blur_enabled': blur_enabled,
            'blur_mode': blur_mode,
            'blur_intensity': b_intensity,
            'blur_detail': b_detail,
            'blur_radius': b_radius,
            'blur_bokeh': b_bokeh,
            'blur_angle': b_angle,
            # Keep specific ones for state persistence
            'texture_soften_intensity': float(self.blur_intensity_slider.value()),
            'texture_soften_detail': float(self.blur_detail_slider.value()),
            'optical_blur_radius': float(self.blur_radius_slider.value()),
            'optical_blur_bokeh': float(self.blur_bokeh_slider.value()),
            'motion_blur_intensity': float(self.blur_motion_intensity_slider.value()),
            'motion_blur_angle': float(self.blur_angle_slider.value()),
            'fast_blur_intensity': float(self.blur_fast_intensity_slider.value()),
            'fast_blur_radius': float(self.blur_fast_radius_slider.value()),
        }

        # Merge and return
        return {**sharpen_params, **blur_params}

    def set_params_dict(self, params, emit=False) -> None:
        """Set parameters from dictionary."""
        if not params:
            return
        
        self._syncing_ui = True
        self._param_timer.stop()
        self._pending_params = None
        
        all_rows = [
            self.sharpen_slider, self.adv_sharpen_slider,
            self.texture_slider, self.halo_slider,
            self.radius_slider, self.iter_slider,
            self.blur_intensity_slider, self.blur_detail_slider,
            self.blur_radius_slider, self.blur_bokeh_slider,
            self.blur_angle_slider, self.blur_motion_intensity_slider,
            self.blur_fast_intensity_slider, self.blur_fast_radius_slider,
        ]
        all_widgets = [
            self.enable_cb, self.mode_combo,
            self.blur_enable_cb, self.blur_mode_combo,
        ] + all_rows
        for w in all_widgets:
            w.blockSignals(True)
            
        self.enable_cb.setChecked(params.get('enabled', False))
        mode = params.get('mode', 0)
        self.mode_combo.setCurrentIndex(mode)
        
        sharpen_val = float(params.get('sharpen', 0))
        self.sharpen_slider.setValue(sharpen_val)
        self.adv_sharpen_slider.setValue(sharpen_val)
        
        self.texture_slider.setValue(float(params.get('texture', 0)))
        self.halo_slider.setValue(float(params.get('halo', 0)))
        
        self.radius_slider.setValue(float(params.get('radius', 0.0)))
        self.iter_slider.setValue(float(params.get('iterations', 1)))
        
        self.blur_enable_cb.setChecked(params.get('blur_enabled', False))
        blur_mode = params.get('blur_mode', 0)
        self.blur_mode_combo.setCurrentIndex(blur_mode)
        
        # 旧工程兼容：只有专用键（texture_soften_* 等）缺失时才回退到通用
        # blur_intensity/blur_radius，且回退值按 blur_mode 分派给对应模式的
        # 滑条（旧代码把通用 blur_radius 无条件灌给光学模糊，Fast 模式半径
        # 会错位归零）
        old_intensity = params.get('blur_intensity', 0)
        old_radius = params.get('blur_radius', 0)
        self.blur_intensity_slider.setValue(float(params.get('texture_soften_intensity', old_intensity if blur_mode == 0 else 0)))
        self.blur_detail_slider.setValue(float(params.get('texture_soften_detail', params.get('blur_detail', 50))))

        self.blur_radius_slider.setValue(float(params.get('optical_blur_radius', old_radius if blur_mode == 1 else 0)))
        self.blur_bokeh_slider.setValue(float(params.get('optical_blur_bokeh', params.get('blur_bokeh', 0))))

        self.blur_motion_intensity_slider.setValue(float(params.get('motion_blur_intensity', old_intensity if blur_mode == 2 else 0)))
        self.blur_angle_slider.setValue(float(params.get('motion_blur_angle', params.get('blur_angle', 0))))

        self.blur_fast_intensity_slider.setValue(float(params.get('fast_blur_intensity', old_intensity if blur_mode == 3 else 0)))
        self.blur_fast_radius_slider.setValue(float(params.get('fast_blur_radius', old_radius if blur_mode == 3 else 0)))
        
        for w in all_widgets:
            w.blockSignals(False)

        # AUD-25105: combo 索引在屏蔽信号下设置，堆栈页面不会随
        # on_mode_changed/on_blur_mode_changed 联动，必须显式同步。
        self._sync_mode_pages()

        self.container.setEnabled(self.enable_cb.isChecked())
        self.blur_container.setEnabled(self.blur_enable_cb.isChecked())
        
        self._syncing_ui = False
        
        if emit:
            self._pending_params = self.get_params_dict()
            self.effects_changed.emit(self._pending_params)
            self._pending_params = None

    def reset_all(self) -> None:
        """Reset all sharpen parameters to default values."""
        self._syncing_ui = True
        self._param_timer.stop()
        
        all_rows = [
            self.sharpen_slider, self.adv_sharpen_slider,
            self.texture_slider, self.halo_slider,
            self.radius_slider, self.iter_slider,
        ]
        all_widgets = [self.enable_cb, self.mode_combo] + all_rows
        for w in all_widgets:
            w.blockSignals(True)
        
        for row in all_rows:
            row.reset()
        self.mode_combo.setCurrentIndex(0)

        for w in all_widgets:
            w.blockSignals(False)

        # AUD-25106: mode_combo 在屏蔽信号下归零，堆栈页面同样需要归位
        self._sync_mode_pages()

        self._syncing_ui = False
        self._pending_params = self.get_params_dict()
        self.effects_changed.emit(self._pending_params)
        self._pending_params = None
        self.reset_clicked.emit()

    def reset_blur(self) -> None:
        """Reset all blur parameters to default values."""
        self._syncing_ui = True
        self._param_timer.stop()
        
        all_rows = [
            self.blur_intensity_slider, self.blur_detail_slider,
            self.blur_radius_slider, self.blur_bokeh_slider,
            self.blur_angle_slider, self.blur_motion_intensity_slider,
            self.blur_fast_intensity_slider, self.blur_fast_radius_slider,
        ]
        all_widgets = [self.blur_enable_cb, self.blur_mode_combo] + all_rows
        for w in all_widgets:
            w.blockSignals(True)
        
        for row in all_rows:
            row.reset()
        self.blur_mode_combo.setCurrentIndex(0)

        for w in all_widgets:
            w.blockSignals(False)

        # AUD-25106: blur_mode_combo 在屏蔽信号下归零，堆栈页面同样需要归位
        self._sync_mode_pages()

        self._syncing_ui = False
        self._pending_params = self.get_params_dict()
        self.effects_changed.emit(self._pending_params)
        self._pending_params = None
        # 与 reset_all 对齐（AUD-25106 附注）：两路 reset 的状态通知
        # 语义一致，均通知 reset_clicked。
        self.reset_clicked.emit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        ParamSliderRow.apply_visibility_to_all(
            ParamSliderRow.collect_rows(self), self.width())

