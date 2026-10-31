from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton, QTabWidget, QScrollArea
from src.shared.i18n.i18n import I18N
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QTimer
from PyQt6.QtGui import QColor
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.assets.icon_manager import icon_reset

class MixerPanel(QWidget):
    # Emits dict of parameters
    mixer_changed = pyqtSignal(dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    reset_clicked = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.colors = ['red', 'orange', 'yellow', 'green', 'aqua', 'blue', 'purple', 'magenta']
        self.hue_map = {
            'red': 0, 'orange': 30, 'yellow': 60, 'green': 120, 
            'aqua': 180, 'blue': 240, 'purple': 280, 'magenta': 315
        }
        self.params = {color: [0, 0, 0] for color in self.colors} # [Hue, Sat, Lum]

        # Throttle slider-driven updates to ~60 FPS
        self._param_timer = QTimer()
        self._param_timer.setSingleShot(True)
        self._param_timer.timeout.connect(self._emit_pending_params)
        self._pending_params = None
        self._syncing_ui = False

        self.init_ui()

    def init_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(5, 5, 5, 5)
        self.setLayout(layout)

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet("""
            QTabBar::tab { padding: 5px 15px; background: #222; color: #888; }
            QTabBar::tab:selected { background: #333; color: #cc6600; }
        """)

        self.hue_tab = self.create_mixer_tab(0, I18N.tr("mixer.hue", "色相"))
        self.sat_tab = self.create_mixer_tab(1, I18N.tr("mixer.saturation", "饱和度"))
        self.lum_tab = self.create_mixer_tab(2, I18N.tr("mixer.luminance", "亮度"))

        self.tabs.addTab(self.hue_tab, I18N.tr("mixer.hue", "色相"))
        self.tabs.addTab(self.sat_tab, I18N.tr("mixer.saturation", "饱和度"))
        self.tabs.addTab(self.lum_tab, I18N.tr("mixer.luminance", "亮度"))

        layout.addWidget(self.tabs)

    def create_mixer_tab(self, param_idx, label_prefix):
        tab = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        
        container = QWidget()
        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(10, 5, 10, 5)
        main_layout.setSpacing(5)

        # Reset button at top-right - Using icon_manager
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        reset_btn = QPushButton()
        reset_btn.setObjectName("reset_btn")
        reset_btn.setIcon(icon_reset())
        reset_btn.setIconSize(QSize(12, 12))
        reset_btn.setFixedSize(16, 16)
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setStyleSheet("""
            QPushButton#reset_btn {
                padding: 0px;
                border-radius: 8px;
                background-color: transparent;
                border: 1px solid #333;
            }
            QPushButton#reset_btn:hover {
                background-color: #333;
                border: 1px solid #ff8800;
            }
        """)
        reset_btn.setToolTip(f"Reset {label_prefix}")
        reset_btn.clicked.connect(lambda: self.reset_group(param_idx))
        btn_layout.addWidget(reset_btn)
        main_layout.addLayout(btn_layout)

        # Grid for two columns
        grid = QGridLayout()
        grid.setSpacing(10)
        grid.setContentsMargins(0, 0, 0, 0)
        
        for i, color in enumerate(self.colors):
            slider_info = self.create_color_row(color, param_idx)
            row = i // 2
            col = i % 2
            
            grid.addWidget(slider_info['row'], row, col)

        main_layout.addLayout(grid)
        main_layout.addStretch()
        scroll.setWidget(container)
        
        vbox = QVBoxLayout(tab)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.addWidget(scroll)
        
        return tab

    def create_color_row(self, color, param_idx):
        # 使用 I18N 翻译颜色名称
        name = I18N.tr(f"mixer.{color}", color.capitalize())

        row = ParamSliderRow(
            name, -100.0, 100.0, 0.0,
            step=1.0, decimals=0, label_width=50,
            slider_auto_hide=False)
        row.setProperty("color", color)
        row.setProperty("param_idx", param_idx)
        row.slider.enable_velocity_drag = True

        # Apply Lightroom-style gradients
        base_hue = self.hue_map.get(color, 0)
        if param_idx == 0: # Hue
            h1 = (base_hue - 25) % 360
            h2 = base_hue
            h3 = (base_hue + 25) % 360
            row.slider.set_gradient_stops([
                (0.0, QColor.fromHsv(int(h1), 200, 200)),
                (0.5, QColor.fromHsv(int(h2), 200, 200)),
                (1.0, QColor.fromHsv(int(h3), 200, 200))
            ])
        elif param_idx == 1: # Saturation
            row.slider.set_gradient_stops([
                (0.0, QColor(128, 128, 128)),
                (1.0, QColor.fromHsv(int(base_hue), 255, 200))
            ])
        elif param_idx == 2: # Luminance
            row.slider.set_gradient_stops([
                (0.0, QColor(0, 0, 0)),
                (0.5, QColor.fromHsv(int(base_hue), 200, 160)),
                (1.0, QColor(255, 255, 255))
            ])

        # 连接顺序即调用顺序：必须先 flush 尾值再发 interaction_ended，
        # 否则 on_interaction_ended（autosave/idle 渲染）先于最后 ≤16ms
        # 的拖动值到达引擎（对照 GradingPanel/ColorWheel 的先 flush 写法）。
        row.interactionStarted.connect(self.interaction_started.emit)
        row.interactionEnded.connect(self._flush_pending_params)
        row.interactionEnded.connect(self.interaction_ended.emit)
        row.valueChanged.connect(lambda val, c=color, i=param_idx: self.update_param(c, i, int(val)))
        
        return {'row': row}

    def update_param(self, color, idx, val) -> None:
        if self._syncing_ui:
            return
        self.params[color][idx] = val
        self._schedule_param_update()

    def _schedule_param_update(self) -> None:
        """Schedule a throttled parameter update (~60 FPS)."""
        if self._syncing_ui:
            return
        self._pending_params = {color: values[:] for color, values in self.params.items()}
        if not self._param_timer.isActive():
            self._param_timer.start(16)

    def _emit_pending_params(self) -> None:
        """Emit pending params (called by timer timeout)."""
        if self._pending_params is not None:
            self.mixer_changed.emit(self._pending_params)
            self._pending_params = None

    def _flush_pending_params(self) -> None:
        """Immediately emit any pending params (called on slider release)."""
        if self._param_timer.isActive():
            self._param_timer.stop()
        self._emit_pending_params()

    def _normalize_params(self, params) -> dict:
        """Normalize mixer params to ensure all 8 colors with valid [hue, sat, lum] lists.
        
        Handles:
        - Missing colors: fills with [0, 0, 0]
        - Dict format: converts {"hue": 0, "saturation": 0, "lightness": 0} to [0, 0, 0]
        - Short lists: pads to 3 values
        - Non-numeric: replaces with 0
        - Out of range: clamps to [-100, 100]
        """
        normalized = {color: [0, 0, 0] for color in self.colors}
        
        if not params:
            return normalized
            
        for color in self.colors:
            val = params.get(color)
            if val is None:
                continue
            
            if isinstance(val, dict):
                h = val.get('hue', 0)
                s = val.get('saturation', 0)
                l = val.get('lightness', val.get('luminance', 0))
                normalized[color] = [
                    self._clamp_value(h),
                    self._clamp_value(s),
                    self._clamp_value(l)
                ]
            elif isinstance(val, (list, tuple)):
                lst = list(val)
                while len(lst) < 3:
                    lst.append(0)
                normalized[color] = [
                    self._clamp_value(lst[0]),
                    self._clamp_value(lst[1]),
                    self._clamp_value(lst[2])
                ]
        
        return normalized
    
    def _clamp_value(self, v) -> int:
        """Clamp value to [-100, 100] range, handle non-numeric."""
        try:
            return max(-100, min(100, int(v)))
        except (TypeError, ValueError):
            return 0

    def set_params(self, params) -> None:
        self._syncing_ui = True
        self._param_timer.stop()
        self._pending_params = None
        self.params = self._normalize_params(params)
        try:
            self._refresh_ui()
        finally:
            self._syncing_ui = False

    def _refresh_ui(self) -> None:
        # Implementation to update all ParamSliderRows from self.params
        tabs = [self.hue_tab, self.sat_tab, self.lum_tab]
        for param_idx, tab in enumerate(tabs):
            for color in self.colors:
                for row in tab.findChildren(ParamSliderRow):
                    if row.property("color") == color and row.property("param_idx") == param_idx:
                        row.setValue(float(self.params[color][param_idx]))

    def reset_group(self, param_idx) -> None:
        # 该组是否真的有非零值（避免无变更时的无谓交互/渲染）
        changed = any(self.params[color][param_idx] != 0
                      for color in self.colors)
        if changed:
            self.interaction_started.emit()
        self._syncing_ui = True
        self._param_timer.stop()
        self._pending_params = None

        # Reset data for this param_idx only
        for color in self.colors:
            self.params[color][param_idx] = 0

        # Update UI: Find the correct tab and reset sliders
        tabs = [self.hue_tab, self.sat_tab, self.lum_tab]
        target_tab = tabs[param_idx]

        for row in target_tab.findChildren(ParamSliderRow):
            if row.property("param_idx") == param_idx:
                row.setValue(0.0)

        self._syncing_ui = False
        # 发深拷贝而非活字典 self.params：engine.set_mixer_params 会把
        # dict 挂到图层上，发活引用会让面板与图层共享同一 dict，此后
        # 拖动滑条原地改写图层状态（节流路径发的是拷贝，行为不一致）
        self.mixer_changed.emit({c: v[:] for c, v in self.params.items()})
        if changed:
            self.interaction_ended.emit()

    def reset_all(self) -> None:
        changed = any(any(v != 0 for v in values)
                      for values in self.params.values())
        if changed:
            self.interaction_started.emit()
        self._syncing_ui = True
        self._param_timer.stop()
        self._pending_params = None
        # This is a bit complex as we have sliders across 3 tabs.
        # But our state is in self.params.
        for color in self.colors:
            self.params[color] = [0, 0, 0]

        # We need to find all rows and reset them visually
        for tab in [self.hue_tab, self.sat_tab, self.lum_tab]:
            for row in tab.findChildren(ParamSliderRow):
                row.setValue(0.0)

        self._syncing_ui = False
        # 深拷贝：同 reset_group（活引用会让面板与图层共享 dict）
        self.mixer_changed.emit({c: v[:] for c, v in self.params.items()})
        self.reset_clicked.emit()
        if changed:
            self.interaction_ended.emit()

    # NOTE: 不重写 resizeEvent — mixer 用 2 列 grid 布局，
    # 每行 ParamSliderRow 宽度 ≈ 面板宽度的一半，
    # 由 ParamSliderRow.resizeEvent 根据自身实际宽度判断 slider 显隐。
