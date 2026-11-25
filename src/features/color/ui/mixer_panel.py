from pathlib import Path

from PyQt6.QtCore import QSize, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QButtonGroup, QGridLayout, QLabel, QScrollArea, QStackedWidget, QToolButton,
    QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.features.color.ui.panel_chrome import (
    ColorParameterRow, SECTION_TITLE_STYLE,
    create_panel_layout, style_header_button, style_reset_button,
)

_MODE_ICON_DIR = Path(__file__).with_name("mixer_icons")
_MODE_ICON_SIZE = 20


class MixerPanel(QWidget):
    # Emits dict of parameters
    mixer_changed = pyqtSignal(dict)
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    reset_clicked = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.colors = ['red', 'orange', 'yellow', 'green', 'aqua', 'blue', 'purple', 'magenta']
        self.params = {color: [0, 0, 0] for color in self.colors} # [Hue, Sat, Lum]

        # Throttle slider-driven updates to ~60 FPS
        self._param_timer = QTimer()
        self._param_timer.setSingleShot(True)
        self._param_timer.timeout.connect(self._emit_pending_params)
        self._pending_params = None
        self._syncing_ui = False

        self.init_ui()

    def init_ui(self) -> None:
        header, body = create_panel_layout(self, (6, 6, 0), 4)
        body.setContentsMargins(0, 0, 0, 0)
        self._mode_title = QLabel()
        self._mode_title.setStyleSheet(SECTION_TITLE_STYLE)
        header.addWidget(self._mode_title)
        header.addStretch()

        self._mode_group = QButtonGroup(self)
        modes = (
            (I18N.tr("mixer.hue", "Hue"), "hue.svg"),
            (I18N.tr("mixer.saturation", "Saturation"), "saturation.svg"),
            (I18N.tr("mixer.luminance", "Luminance"), "luminance.svg"),
        )
        for index, (label, filename) in enumerate(modes):
            button = QToolButton()
            button.setCheckable(True)
            button.setToolTip(label)
            button.setAccessibleName(label)
            style_header_button(button, width=30, icon=QIcon(str(_MODE_ICON_DIR / filename)))
            button.setIconSize(QSize(_MODE_ICON_SIZE, _MODE_ICON_SIZE))
            self._mode_group.addButton(button, index)
            header.addWidget(button)

        self.reset_btn = QToolButton()
        self.reset_btn.setObjectName("reset_btn")
        style_reset_button(self.reset_btn, global_reset=True)
        self.reset_btn.clicked.connect(lambda: self.reset_group(self._pages.currentIndex()))
        header.addWidget(self.reset_btn)

        self._pages = QStackedWidget()
        self.hue_tab = self._create_mode_page(0)
        self.sat_tab = self._create_mode_page(1)
        self.lum_tab = self._create_mode_page(2)
        for page in (self.hue_tab, self.sat_tab, self.lum_tab):
            self._pages.addWidget(page)
        body.addWidget(self._pages)
        self._mode_group.idToggled.connect(self._on_mode_toggled)
        self._mode_group.button(0).setChecked(True)

    def _on_mode_toggled(self, index: int, checked: bool) -> None:
        if not checked:
            return
        self._pages.setCurrentIndex(index)
        label = self._mode_group.button(index).toolTip()
        self._mode_title.setText(label)
        tooltip = I18N.tr("mixer.reset_mode", "Reset {mode}", mode=label)
        self.reset_btn.setToolTip(tooltip)
        self.reset_btn.setAccessibleName(tooltip)

    def _create_mode_page(self, param_idx: int) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(10, 16, 10, 5)
        main_layout.setSpacing(0)

        # Grid for two columns
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(7)
        grid.setContentsMargins(0, 0, 0, 0)

        for i, color in enumerate(self.colors):
            grid.addWidget(self.create_color_row(color, param_idx), i // 2, i % 2)

        main_layout.addLayout(grid)
        main_layout.addStretch()
        scroll.setWidget(container)
        return scroll

    def create_color_row(self, color: str, param_idx: int) -> ColorParameterRow:
        # 使用 I18N 翻译颜色名称
        name = I18N.tr(f"mixer.{color}", color.capitalize())

        row = ColorParameterRow(
            name, -100.0, 100.0, 0.0,
            step=1.0, decimals=0, label_width=50,
            slider_auto_hide=False)
        row.setProperty("color", color)
        row.setProperty("param_idx", param_idx)
        row.slider.enable_velocity_drag = True

        # 连接顺序即调用顺序：必须先 flush 尾值再发 interaction_ended，
        # 否则 on_interaction_ended（autosave/idle 渲染）先于最后 ≤16ms
        # 的拖动值到达引擎（对照 GradingPanel/ColorWheel 的先 flush 写法）。
        row.interactionStarted.connect(self.interaction_started.emit)
        row.interactionEnded.connect(self._flush_pending_params)
        row.interactionEnded.connect(self.interaction_ended.emit)
        row.valueChanged.connect(lambda val, c=color, i=param_idx: self.update_param(c, i, round(val)))

        return row

    def update_param(self, color, idx, val) -> None:
        if self._syncing_ui or self.params[color][idx] == val:
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
