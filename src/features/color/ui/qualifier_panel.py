from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QFrame, QGridLayout,
                             QCheckBox, QScrollArea,
                             QGroupBox, QButtonGroup)
from PyQt6.QtCore import pyqtSignal, QSize
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.ui.widgets.qualifier_range_bar import QualifierRangeBar
from src.shared.assets.icon_manager import icon_reset, icon_color_picker
from src.shared.i18n.i18n import I18N

class QualifierPanel(QWidget):
    params_changed = pyqtSignal(dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    picker_toggled = pyqtSignal(bool)
    # P1-COLOR-08: matte 预览请求。'highlight'/'black'/'matte' 请求以该模式
    # 开启 matte 预览；'' 请求关闭。预览模式不改变工程数据（视图态）。
    matte_preview_requested = pyqtSignal(str)

    # 支持的预览模式（顺序即 UI 顺序）
    MATTE_PREVIEW_MODES = ('highlight', 'black', 'matte')

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._updating_flag = False
        self.current_picker_mode = 'normal'  # 'normal', 'add', 'subtract'
        self.params = {
            "enabled": False,
            "hue_enabled": True,
            "hue_center": 0.5,
            "hue_width": 1.0,
            "hue_soft": 0.1,
            "sat_enabled": True,
            "sat_low": 0.0,
            "sat_high": 1.0,
            "sat_soft_low": 0.0,
            "sat_soft_high": 0.0,
            "lum_enabled": True,
            "lum_low": 0.0,
            "lum_high": 1.0,
            "lum_soft_low": 0.0,
            "lum_soft_high": 0.0,
            "blur": 0.0,
            "clean_black": 0.0,
            "clean_white": 0.0,
            "denoise": 0.0,
            "invert": False
        }
        self.init_ui()

    def get_style_sheet(self) -> str:
        """Return panel-specific style sheet.
        
        QGroupBox/QDoubleSpinBox/QPushButton use global DARK_THEME styles.
        Only picker button group styles are panel-specific.
        """
        return """
            QualifierPanel {
                background-color: #1a1a1a;
            }
            /* Picker button group styles */
            QPushButton#picker_normal_btn {
                border-top-left-radius: 4px;
                border-bottom-left-radius: 4px;
                border-top-right-radius: 0px;
                border-bottom-right-radius: 0px;
                padding: 4px 8px;
                min-width: 40px;
            }
            QPushButton#picker_add_btn {
                border-radius: 0px;
                padding: 4px 6px;
                min-width: 24px;
            }
            QPushButton#picker_sub_btn {
                border-top-left-radius: 0px;
                border-bottom-left-radius: 0px;
                border-top-right-radius: 4px;
                border-bottom-right-radius: 4px;
                padding: 4px 6px;
                min-width: 24px;
            }
            QPushButton#picker_normal_btn:checked, QPushButton#picker_add_btn:checked {
                background-color: #ff8800;
                color: white;
                border: 1px solid #ffa500;
            }
            QPushButton#picker_sub_btn:checked {
                background-color: #cc4444;
                color: white;
                border: 1px solid #dd5555;
            }
        """

    def init_ui(self) -> None:
        self.setStyleSheet(self.get_style_sheet())
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(4)

        # Header with Master Title, Matte Blur, and Picker
        header = QHBoxLayout()
        header.setSpacing(10)

        self.title_label = QLabel(I18N.tr("qualifier.title", "限定器"))
        self.title_label.setStyleSheet("""
            QLabel { font-weight: bold; color: #ff8800; min-width: 70px; font-size: 11px; }
        """)
        header.addWidget(self.title_label)

        # Matte Blur in Header (ParamSliderRow)
        self.blur_row = ParamSliderRow(
            I18N.tr("qualifier.blur", "Matte 模糊"),
            min_val=0, max_val=100, default_val=0,
            step=1, decimals=0,
            label_width=60,
        )
        self.blur_row.valueChanged.connect(
            lambda v: self.update_param("blur", float(v)))
        self.blur_row.interactionStarted.connect(self.interaction_started.emit)
        self.blur_row.interactionEnded.connect(self.interaction_ended.emit)
        header.addWidget(self.blur_row)

        header.addStretch()

        # Picker button group (Normal, Add, Subtract)
        self.picker_group = QButtonGroup(self)
        self.picker_group.setExclusive(True)

        self.picker_normal_btn = QPushButton(I18N.tr("qualifier.picker", "拾取"))
        self.picker_normal_btn.setObjectName("picker_normal_btn")
        self.picker_normal_btn.setIcon(icon_color_picker())
        self.picker_normal_btn.setCheckable(True)
        self.picker_normal_btn.setFixedHeight(24)
        self.picker_normal_btn.setToolTip(I18N.tr("tooltip.qualifier_picker_normal", "正常拾取：首次点击重置范围，拖动时扩展"))

        self.picker_add_btn = QPushButton("+")
        self.picker_add_btn.setObjectName("picker_add_btn")
        self.picker_add_btn.setIcon(icon_color_picker())
        self.picker_add_btn.setCheckable(True)
        self.picker_add_btn.setFixedHeight(24)
        self.picker_add_btn.setToolTip(I18N.tr("tooltip.qualifier_picker_add", "添加拾取：扩展范围以包含所选颜色"))

        self.picker_sub_btn = QPushButton("-")
        self.picker_sub_btn.setObjectName("picker_sub_btn")
        self.picker_sub_btn.setIcon(icon_color_picker())
        self.picker_sub_btn.setCheckable(True)
        self.picker_sub_btn.setFixedHeight(24)
        self.picker_sub_btn.setToolTip(I18N.tr("tooltip.qualifier_picker_sub", "减去拾取：缩小范围以排除所选颜色"))

        self.picker_group.addButton(self.picker_normal_btn, 0)
        self.picker_group.addButton(self.picker_add_btn, 1)
        self.picker_group.addButton(self.picker_sub_btn, 2)

        # Connect toggled signals
        self.picker_normal_btn.toggled.connect(self._on_picker_group_toggled)
        self.picker_add_btn.toggled.connect(self._on_picker_group_toggled)
        self.picker_sub_btn.toggled.connect(self._on_picker_group_toggled)

        header.addWidget(self.picker_normal_btn)
        header.addWidget(self.picker_add_btn)
        header.addWidget(self.picker_sub_btn)

        self.invert_check = QCheckBox(I18N.tr("qualifier.invert", "反转"))
        self.invert_check.setStyleSheet("min-width: 50px;")
        self.invert_check.toggled.connect(self.on_param_changed)
        header.addWidget(self.invert_check)

        self.reset_all_btn = QPushButton()
        self.reset_all_btn.setObjectName("reset_btn")
        self.reset_all_btn.setIcon(icon_reset())
        self.reset_all_btn.setIconSize(QSize(12, 12))
        self.reset_all_btn.setToolTip(I18N.tr("tooltip.reset_qualifier", "重置所有限定器参数"))
        self.reset_all_btn.setFixedSize(16, 16)
        self.reset_all_btn.clicked.connect(self.reset_all)
        header.addWidget(self.reset_all_btn)
        
        layout.addLayout(header)

        # Scroll Area for controls
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        
        container = QWidget()
        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(0, 4, 0, 4)
        main_layout.setSpacing(4)

        # 1. HUE Section (使用QGridLayout确保滑动条等长)
        self.hue_group = QGroupBox(I18N.tr("qualifier.hue", "色相"))
        hue_vlayout = QVBoxLayout()
        hue_vlayout.setContentsMargins(4, 0, 4, 4)
        hue_vlayout.setSpacing(2)
        self.hue_group.setLayout(hue_vlayout)
        
        self.hue_bar = QualifierRangeBar("hue")
        self.hue_bar.hue_changed.connect(self.on_hue_bar_changed)
        self.hue_bar.interaction_started.connect(self.interaction_started.emit)
        self.hue_bar.interaction_ended.connect(self.interaction_ended.emit)
        self.hue_bar.setFixedHeight(24)
        hue_vlayout.addWidget(self.hue_bar)
        
        # HUE 参数网格：4列 (Label, Slider, Spinbox) x 4组
        hue_grid = QGridLayout()
        hue_grid.setContentsMargins(0, 0, 0, 0)
        hue_grid.setSpacing(2)
        self.hue_center_ctrl = self.add_param_to_grid(hue_grid, I18N.tr("qualifier.center", "中心"), 0.0, 1.0, 0.5, 0.01, 0)
        self.hue_width_ctrl = self.add_param_to_grid(hue_grid, I18N.tr("qualifier.width", "宽度"), 0.0, 1.0, 1.0, 0.01, 1)
        self.hue_soft_ctrl = self.add_param_to_grid(hue_grid, I18N.tr("qualifier.soft", "柔化"), 0.0, 1.0, 0.1, 0.01, 2)
        # 第4列空白占位
        self.add_empty_slot_to_grid(hue_grid, 3)
        hue_vlayout.addLayout(hue_grid)
        
        main_layout.addWidget(self.hue_group)

        # 2. SATURATION Section
        self.sat_group = QGroupBox(I18N.tr("qualifier.saturation", "饱和度"))
        sat_vlayout = QVBoxLayout()
        sat_vlayout.setContentsMargins(4, 0, 4, 4)
        sat_vlayout.setSpacing(2)
        self.sat_group.setLayout(sat_vlayout)
        
        self.sat_bar = QualifierRangeBar("sat")
        self.sat_bar.range_changed.connect(self.on_range_bar_changed_sat)
        self.sat_bar.interaction_started.connect(self.interaction_started.emit)
        self.sat_bar.interaction_ended.connect(self.interaction_ended.emit)
        self.sat_bar.setFixedHeight(24)
        sat_vlayout.addWidget(self.sat_bar)
        
        # SATURATION 参数网格
        sat_grid = QGridLayout()
        sat_grid.setContentsMargins(0, 0, 0, 0)
        sat_grid.setSpacing(2)
        self.sat_low_ctrl = self.add_param_to_grid(sat_grid, I18N.tr("qualifier.low", "低"), 0.0, 1.0, 0.0, 0.01, 0)
        self.sat_high_ctrl = self.add_param_to_grid(sat_grid, I18N.tr("qualifier.high", "高"), 0.0, 1.0, 1.0, 0.01, 1)
        self.sat_soft_low_ctrl = self.add_param_to_grid(sat_grid, I18N.tr("qualifier.s_low", "柔化低"), 0.0, 1.0, 0.0, 0.01, 2)
        self.sat_soft_high_ctrl = self.add_param_to_grid(sat_grid, I18N.tr("qualifier.s_high", "柔化高"), 0.0, 1.0, 0.0, 0.01, 3)
        sat_vlayout.addLayout(sat_grid)
        
        main_layout.addWidget(self.sat_group)

        # 3. LUMINANCE Section
        self.lum_group = QGroupBox(I18N.tr("qualifier.luminance", "亮度"))
        lum_vlayout = QVBoxLayout()
        lum_vlayout.setContentsMargins(4, 0, 4, 4)
        lum_vlayout.setSpacing(2)
        self.lum_group.setLayout(lum_vlayout)
        
        self.lum_bar = QualifierRangeBar("lum")
        self.lum_bar.range_changed.connect(self.on_range_bar_changed_lum)
        self.lum_bar.interaction_started.connect(self.interaction_started.emit)
        self.lum_bar.interaction_ended.connect(self.interaction_ended.emit)
        self.lum_bar.setFixedHeight(24)
        lum_vlayout.addWidget(self.lum_bar)
        
        # LUMINANCE 参数网格
        lum_grid = QGridLayout()
        lum_grid.setContentsMargins(0, 0, 0, 0)
        lum_grid.setSpacing(2)
        self.lum_low_ctrl = self.add_param_to_grid(lum_grid, I18N.tr("qualifier.low", "低"), 0.0, 1.0, 0.0, 0.01, 0)
        self.lum_high_ctrl = self.add_param_to_grid(lum_grid, I18N.tr("qualifier.high", "高"), 0.0, 1.0, 1.0, 0.01, 1)
        self.lum_soft_low_ctrl = self.add_param_to_grid(lum_grid, I18N.tr("qualifier.s_low", "柔化低"), 0.0, 1.0, 0.0, 0.01, 2)
        self.lum_soft_high_ctrl = self.add_param_to_grid(lum_grid, I18N.tr("qualifier.s_high", "柔化高"), 0.0, 1.0, 0.0, 0.01, 3)
        lum_vlayout.addLayout(lum_grid)
        
        main_layout.addWidget(self.lum_group)

        # 4. MATTE REFINEMENT Section
        self.refine_group = QGroupBox(I18N.tr("qualifier.matte_refinement", "Matte 优化"))
        refine_vlayout = QVBoxLayout()
        refine_vlayout.setContentsMargins(4, 0, 4, 4)
        refine_vlayout.setSpacing(2)
        self.refine_group.setLayout(refine_vlayout)

        # P1-COLOR-08: matte 预览模式行（高亮/黑场/灰度，再点取消 = 关闭预览）
        preview_row = QHBoxLayout()
        preview_row.setSpacing(4)
        preview_lbl = QLabel(I18N.tr("qualifier.preview", "预览"))
        preview_lbl.setStyleSheet("color: #999999; font-size: 11px;")
        preview_row.addWidget(preview_lbl)

        self.preview_group = QButtonGroup(self)
        self.preview_group.setExclusive(False)  # 允许点击已选中按钮取消
        self._preview_buttons = {}
        preview_labels = {
            'highlight': I18N.tr("qualifier.preview_highlight", "高亮"),
            'black': I18N.tr("qualifier.preview_black", "黑场"),
            'matte': I18N.tr("qualifier.preview_matte", "灰度"),
        }
        for mode in self.MATTE_PREVIEW_MODES:
            btn = QPushButton(preview_labels[mode])
            btn.setCheckable(True)
            btn.setFixedHeight(20)
            btn.setToolTip(I18N.tr(
                f"tooltip.qualifier_preview_{mode}",
                {"highlight": "以原图+灰底显示限定区域",
                 "black": "以原图+黑底显示限定区域",
                 "matte": "显示纯灰度 matte（白=选中）"}[mode]))
            self.preview_group.addButton(btn)
            preview_row.addWidget(btn)
            self._preview_buttons[mode] = btn
        self.preview_group.buttonClicked.connect(self._on_preview_button_clicked)
        preview_row.addStretch()
        refine_vlayout.addLayout(preview_row)

        # MATTE REFINEMENT 参数网格
        refine_grid = QGridLayout()
        refine_grid.setContentsMargins(0, 0, 0, 0)
        refine_grid.setSpacing(2)
        self.clean_black_ctrl = self.add_param_to_grid(refine_grid, I18N.tr("qualifier.clean_b", "净化黑"), 0.0, 20.0, 0.0, 1.0, 0)
        self.clean_white_ctrl = self.add_param_to_grid(refine_grid, I18N.tr("qualifier.clean_w", "净化白"), 0.0, 20.0, 0.0, 1.0, 1)
        self.denoise_ctrl = self.add_param_to_grid(refine_grid, I18N.tr("qualifier.denoise", "降噪"), 0.0, 100.0, 0.0, 1.0, 2)
        # 第4列空白占位
        self.add_empty_slot_to_grid(refine_grid, 3)
        refine_vlayout.addLayout(refine_grid)

        main_layout.addWidget(self.refine_group)
        
        main_layout.addStretch()

        scroll.setWidget(container)
        layout.addWidget(scroll)

    def add_param_to_grid(self, grid: QGridLayout, label: str, min_val: float,
                          max_val: float, default_val: float, step: float,
                          col: int) -> ParamSliderRow:
        """Add a ParamSliderRow to grid at specified column."""
        row = ParamSliderRow(
            label, min_val, max_val, default_val,
            step=step, decimals=2 if step < 1 else 0,
            label_width=40, value_fixed_width=40)
        row.interactionStarted.connect(self.interaction_started.emit)
        row.interactionEnded.connect(self.interaction_ended.emit)
        row.valueChanged.connect(lambda _: self.on_slider_param_changed())
        
        # 列布局: 整个 ParamSliderRow 占 3 列
        base_col = col * 3
        grid.addWidget(row, 0, base_col, 1, 3)
        
        # 设置列拉伸因子
        grid.setColumnStretch(base_col + 1, 1)
        
        return row

    def add_empty_slot_to_grid(self, grid, col) -> None:
        """Add empty widgets to occupy a slot in the grid for alignment."""
        base_col = col * 3
        # 空白标签
        empty_lbl = QWidget()
        empty_lbl.setFixedWidth(40)
        grid.addWidget(empty_lbl, 0, base_col)
        # 空白滑动条区域
        empty_slider = QWidget()
        grid.addWidget(empty_slider, 0, base_col + 1)
        grid.setColumnStretch(base_col + 1, 1)
        # 空白数值框
        empty_spin = QWidget()
        empty_spin.setFixedWidth(40)
        grid.addWidget(empty_spin, 0, base_col + 2)

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
        """Update a single parameter and sync UI."""
        self.params[key] = value
        
        # Auto-activate when parameters are adjusted
        if not self._updating_flag and key != "enabled" and not self.params["enabled"]:
            self.params["enabled"] = True

        # Update visualization bars
        old = self.blockSignals(True)
        if "hue" in key:
            self.hue_bar.set_hue_params(self.params["hue_center"], self.params["hue_width"], self.params["hue_soft"])
            self.hue_center_ctrl.setValue(self.params["hue_center"])
            self.hue_width_ctrl.setValue(self.params["hue_width"])
            self.hue_soft_ctrl.setValue(self.params["hue_soft"])
        elif "sat" in key:
            self.sat_bar.set_range(self.params["sat_low"], self.params["sat_high"], 
                                 self.params["sat_soft_low"], self.params["sat_soft_high"])
            self.sat_low_ctrl.setValue(self.params["sat_low"])
            self.sat_high_ctrl.setValue(self.params["sat_high"])
            self.sat_soft_low_ctrl.setValue(self.params["sat_soft_low"])
            self.sat_soft_high_ctrl.setValue(self.params["sat_soft_high"])
        elif "lum" in key:
            self.lum_bar.set_range(self.params["lum_low"], self.params["lum_high"], 
                                 self.params["lum_soft_low"], self.params["lum_soft_high"])
            self.lum_low_ctrl.setValue(self.params["lum_low"])
            self.lum_high_ctrl.setValue(self.params["lum_high"])
            self.lum_soft_low_ctrl.setValue(self.params["lum_soft_low"])
            self.lum_soft_high_ctrl.setValue(self.params["lum_soft_high"])
        elif key == "denoise":
            self.denoise_ctrl.setValue(self.params.get("denoise", 0.0))
        self.blockSignals(old)

        if not self.signalsBlocked():
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
        self._updating_flag = True
        old = self.blockSignals(True)
        self.params.update(params)
        self.invert_check.setChecked(self.params["invert"])
        
        # Update all sliders and spinboxes
        self.hue_center_ctrl.setValue(self.params["hue_center"])
        self.hue_width_ctrl.setValue(self.params["hue_width"])
        self.hue_soft_ctrl.setValue(self.params["hue_soft"])
        
        self.sat_low_ctrl.setValue(self.params["sat_low"])
        self.sat_high_ctrl.setValue(self.params["sat_high"])
        self.sat_soft_low_ctrl.setValue(self.params["sat_soft_low"])
        self.sat_soft_high_ctrl.setValue(self.params["sat_soft_high"])
        
        self.lum_low_ctrl.setValue(self.params["lum_low"])
        self.lum_high_ctrl.setValue(self.params["lum_high"])
        self.lum_soft_low_ctrl.setValue(self.params["lum_soft_low"])
        self.lum_soft_high_ctrl.setValue(self.params["lum_soft_high"])
        
        self.blur_row.setValue(int(self.params["blur"]))
        
        self.clean_black_ctrl.setValue(self.params.get("clean_black", 0))
        self.clean_white_ctrl.setValue(self.params.get("clean_white", 0))
        self.denoise_ctrl.setValue(self.params.get("denoise", 0))

        # Update bars
        self.hue_bar.set_hue_params(self.params["hue_center"], self.params["hue_width"], self.params["hue_soft"])
        self.sat_bar.set_range(self.params["sat_low"], self.params["sat_high"], 
                             self.params["sat_soft_low"], self.params["sat_soft_high"])
        self.lum_bar.set_range(self.params["lum_low"], self.params["lum_high"], 
                             self.params["lum_soft_low"], self.params["lum_soft_high"])

        self.blockSignals(old)
        self._updating_flag = False
        self.params_changed.emit(self.params)

    def _on_preview_button_clicked(self, btn: QPushButton) -> None:
        """Preview mode button: click to enable, click the active one again to disable."""
        mode = None
        for m, b in self._preview_buttons.items():
            if b is btn:
                mode = m
                break
        if mode is None:
            return
        if btn.isChecked():
            # Uncheck the other buttons, keeping only the current mode
            for b in self._preview_buttons.values():
                if b is not btn:
                    b.setChecked(False)
            self.matte_preview_requested.emit(mode)
        else:
            self.matte_preview_requested.emit('')

    def set_matte_preview_active(self, active: bool, style: str = 'highlight') -> None:
        """External sync of preview button states (e.g. toggled from the viewport menu)."""
        for m, b in self._preview_buttons.items():
            b.setChecked(bool(active) and m == style)

    # NOTE: 不重写 resizeEvent — qualifier 用 grid 双列布局，
    # 每行 ParamSliderRow 宽度 ≠ 面板宽度，
    # 由 ParamSliderRow.resizeEvent 根据自身宽度判断 slider 显隐。