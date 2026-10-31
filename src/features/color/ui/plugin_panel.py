"""
Plugin Panel for Topos DCTL System (Phase 3)

Effect Controls panel that displays a vertical stack of plugin instances
attached to the currently selected layer - similar to After Effects.
"""

import logging

logger = logging.getLogger(__name__)

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QScrollArea, QFrame, QPushButton, QGroupBox,
                             QMenu)
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import (
    PANEL_TITLE_LABEL_STYLE, ENABLE_LABEL_STYLE, SEPARATOR_LINE_STYLE,
    SCROLL_AREA_TRANSPARENT_STYLE, TRANSPARENT_STYLE,
    EMPTY_STATE_LABEL_STYLE, ERROR_GROUP_STYLE, ERROR_LABEL_STYLE,
    AI_EFFECT_GROUP_STYLE, AI_PROCESS_BTN_STYLE, SCALE_LABEL_STYLE)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction
from src.features.color.core.plugin_manager import (
    format_capability_summary, get_plugin_manager,
)
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.ui.widgets.color_picker_control import (
    ColorPickerControl, qcolor_to_linear_rgba,
)
from src.shared.assets.icon_manager import icon_reset


class PluginPanel(QWidget):
    """
    Dynamic panel that generates widgets based on plugin parameters.
    Supports multiple plugin instances per layer (Effect Stack).
    """

    # Signals
    params_changed = pyqtSignal()
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    add_effect_requested = pyqtSignal(str, str)  # plugin_id, plugin_name
    ai_process_requested = pyqtSignal(str)  # instance_id
    
    # Slider resolution (number of steps for float mapping)
    SLIDER_STEPS = 1000
    
    def __init__(self, engine, parent=None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.current_layer = None
        self._slider_updating = False  # Prevent recursive updates
        
        self.setup_ui()
        self.hide()  # Initially hidden when no layer is selected
    
    def get_style_sheet(self):
        """Return panel-specific style sheet.
        
        Generic QGroupBox/QCheckBox/QDoubleSpinBox/QPushButton/QLabel styles
        are inherited from global DARK_THEME — only panel-level and
        object-name-specific overrides remain here.
        """
        return """
            PluginPanel {
                background-color: #1a1a1a;
            }
            QPushButton#delete_btn {
                padding: 0px;
                border-radius: 3px;
                background-color: #333;
                border: 1px solid #444;
                min-width: 20px;
                max-width: 20px;
                min-height: 20px;
                max-height: 20px;
            }
            QPushButton#delete_btn:hover {
                background-color: #552222;
                border: 1px solid #ff4444;
                color: #ff4444;
            }
            QPushButton#reset_btn {
                padding: 0px;
                border-radius: 8px;
                background-color: transparent;
                border: 1px solid #333;
                min-width: 16px;
                max-width: 16px;
                min-height: 16px;
                max-height: 16px;
            }
            QPushButton#reset_btn:hover {
                background-color: #333;
                border: 1px solid #ff8800;
            }
        """
    
    def setup_ui(self) -> None:
        """Initialize the panel layout."""
        self.setStyleSheet(self.get_style_sheet())
        
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(4, 4, 4, 4)
        self.main_layout.setSpacing(4)
        
        # Header with title
        header_layout = QHBoxLayout()

        self.title_label = QLabel(I18N.tr("plugin.title_label", "效果控制"))
        self.title_label.setStyleSheet(PANEL_TITLE_LABEL_STYLE)
        header_layout.addWidget(self.title_label)
        header_layout.addStretch()
        self.main_layout.addLayout(header_layout)
        
        # Separator
        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet(SEPARATOR_LINE_STYLE)
        self.main_layout.addWidget(separator)
        
        # Scroll area for dynamic content
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setStyleSheet(SCROLL_AREA_TRANSPARENT_STYLE)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        # Content widget
        self.content_widget = QWidget()
        self.content_widget.setStyleSheet(TRANSPARENT_STYLE)
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(0, 4, 0, 4)
        self.content_layout.setSpacing(4)
        
        self.scroll_area.setWidget(self.content_widget)
        self.main_layout.addWidget(self.scroll_area)

        # Enable context menu for adding effects
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos) -> None:
        """Show context menu with available effects/plugins.

        AUD-25510: 右键菜单不再每次都在 UI 线程同步重扫插件目录（文件
        遍历 + 全部 .tglsl/.tai 清单解析，插件目录大或位于网络卷时菜单
        明显延迟）。改为：注册表为空时惰性扫描一次；菜单底部提供显式
        "Reload Effects" 动作触发 reload_plugins()（重扫以整体替换方式
        进行，见 PluginManager.reload_plugins）。
        """
        menu = QMenu(I18N.tr("effects.add_effect", "Add Effect"), self)

        manager = get_plugin_manager()
        if not manager.get_all_plugins():
            # 惰性初始化：仅注册表为空时扫描一次
            manager.scan_plugins()
        plugins = manager.get_all_plugins()

        # Separate DCTL and AI plugins
        dctl_plugins = {k: v for k, v in plugins.items() if v.get('type', 'dctl') == 'dctl'}
        ai_plugins = {k: v for k, v in plugins.items() if v.get('type') == 'ai'}

        if not plugins:
            no_effects_act = QAction(I18N.tr("effects.no_effects", "No effects available"), self)
            no_effects_act.setEnabled(False)
            menu.addAction(no_effects_act)
        else:
            # DCTL Shader Effects
            if dctl_plugins:
                dctl_menu = menu.addMenu(I18N.tr("effects.shader_effects", "Shader Effects"))
                for plugin_id, plugin_data in dctl_plugins.items():
                    plugin_name = plugin_data.get('name', plugin_id)
                    effect_act = QAction(plugin_name, self)
                    effect_act.triggered.connect(lambda checked, pid=plugin_id, pname=plugin_name:
                                                self.add_effect_requested.emit(pid, pname))
                    dctl_menu.addAction(effect_act)

            # AI Effects
            if ai_plugins:
                if dctl_plugins:
                    menu.addSeparator()
                ai_menu = menu.addMenu(I18N.tr("effects.ai_effects", "AI Effects"))
                for plugin_id, plugin_data in ai_plugins.items():
                    plugin_name = plugin_data.get('name', plugin_id)
                    effect_act = QAction(f"{plugin_name}", self)
                    effect_act.triggered.connect(lambda checked, pid=plugin_id, pname=plugin_name:
                                                self.add_effect_requested.emit(pid, pname))
                    ai_menu.addAction(effect_act)

        # AUD-25510: 显式重扫入口（替代右键自动重扫）。触发后菜单关闭，
        # 重新打开即见新插件。
        menu.addSeparator()
        reload_act = QAction(I18N.tr("effects.reload_effects", "Reload Effects"), self)
        reload_act.triggered.connect(self._reload_plugins)
        menu.addAction(reload_act)

        menu.exec(self.mapToGlobal(pos))

    def _reload_plugins(self) -> None:
        """显式重扫插件目录（AUD-25510：替代右键菜单每次自动重扫）。

        reload_plugins 在局部快照上完成解析后整体替换注册表；失败留痕
        不中断（槽内异常会打断菜单事件循环）。
        """
        try:
            get_plugin_manager().reload_plugins()
        except Exception as exc:  # noqa: BLE001 —— 槽内兜底，失败需留痕
            logger.warning("PluginPanel: reload_plugins failed: %s", exc,
                           exc_info=True)

    def set_layer(self, layer) -> None:
        """
        Update the panel for the given layer.
        Displays all plugin instances attached to the layer.
        If no plugins, shows an empty state message.
        """
        self.current_layer = layer
        
        # Clear existing widgets
        self._clear_content()
        
        if layer is None:
            self._show_empty_state("No layer selected")
            return
        
        # Check if layer has plugins
        if not hasattr(layer, 'plugins') or not layer.plugins:
            self._show_empty_state("No effects applied")
            return
        
        self.show()
        
        # Update title with layer name
        self.title_label.setText(f"EFFECT CONTROLS - {layer.name}")
        
        # Create effect blocks for each plugin instance
        for idx, plugin_inst in enumerate(layer.plugins):
            self._create_effect_block(plugin_inst, idx)
        
        # Add stretch at the end
        self.content_layout.addStretch()
    
    def _show_empty_state(self, message) -> None:
        """Show an empty state message when no plugin is active."""
        self.show()
        
        # Update title
        self.title_label.setText(I18N.tr("plugin.effect_controls", "EFFECT CONTROLS"))
        
        # Clear existing widgets
        self._clear_content()
        
        # Add empty state message
        empty_label = QLabel(f"{message}\n\nAdd effects from the\nPlugins menu.")
        empty_label.setStyleSheet(EMPTY_STATE_LABEL_STYLE)
        empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.content_layout.addWidget(empty_label)
        self.content_layout.addStretch()
    
    def _clear_content(self) -> None:
        """Remove all widgets from the content layout."""
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
    
    def _create_effect_block(self, plugin_inst, index) -> None:
        """
        Create a visual block for a plugin instance.
        Routes to DCTL or AI block creator based on plugin type.
        """
        plugin_id = plugin_inst['plugin_id']

        # Fetch plugin metadata
        plugin_data = get_plugin_manager().get_plugin(plugin_id)
        if not plugin_data:
            error_group = QGroupBox(I18N.tr("plugin.missing_plugin", "Missing Plugin"))
            error_group.setStyleSheet(ERROR_GROUP_STYLE)
            error_layout = QVBoxLayout(error_group)
            error_layout.setContentsMargins(8, 8, 8, 8)
            error_label = QLabel(f"Plugin '{plugin_id}' not found")
            error_label.setStyleSheet(ERROR_LABEL_STYLE)
            error_layout.addWidget(error_label)
            self.content_layout.addWidget(error_group)
            return

        plugin_type = plugin_data.get('type', 'dctl')
        if plugin_type == 'ai':
            self._create_ai_effect_block(plugin_inst, index, plugin_data)
        else:
            self._create_dctl_effect_block(plugin_inst, index, plugin_data)

    def _create_dctl_effect_block(self, plugin_inst, index, plugin_data) -> None:
        """Create a visual block for a DCTL (shader) plugin instance."""
        instance_id = plugin_inst['instance_id']
        plugin_id = plugin_inst['plugin_id']
        enabled = plugin_inst.get('enabled', True)
        plugin_name = plugin_data.get('name', plugin_id)

        group = QGroupBox(plugin_name.upper())
        group_layout = QVBoxLayout()
        group_layout.setContentsMargins(4, 4, 4, 4)
        group_layout.setSpacing(4)
        group.setLayout(group_layout)
        # P1-COLOR-15: capability manifest 摘要（版本/色彩域/精度/导出支持）
        capability_tip = format_capability_summary(plugin_data)
        if capability_tip:
            group.setToolTip(capability_tip)

        # Header Row
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        enable_check = ToggleSwitch(checked=enabled)
        enable_check.toggled.connect(
            lambda checked, iid=instance_id: self._toggle_plugin(iid, checked)
        )
        header_layout.addWidget(enable_check)

        enable_label = QLabel(I18N.tr("plugin.enabled", "Enabled"))
        enable_label.setStyleSheet(ENABLE_LABEL_STYLE)
        header_layout.addWidget(enable_label)
        header_layout.addStretch()

        reset_btn = QPushButton()
        reset_btn.setObjectName("reset_btn")
        reset_btn.setIcon(icon_reset())
        reset_btn.setFixedSize(16, 16)
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setToolTip(I18N.tr("plugin.reset_tooltip", "Reset parameters"))
        reset_btn.clicked.connect(
            lambda checked, iid=instance_id, pid=plugin_id: self._reset_plugin(iid, pid)
        )
        header_layout.addWidget(reset_btn)

        delete_btn = QPushButton("X")
        delete_btn.setObjectName("delete_btn")
        delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_btn.setToolTip(I18N.tr("plugin.remove_effect_tooltip", "Remove effect"))
        delete_btn.clicked.connect(
            lambda checked, iid=instance_id: self._delete_plugin(iid)
        )
        header_layout.addWidget(delete_btn)

        group_layout.addLayout(header_layout)

        # Parameter Widgets
        params_widget = QWidget()
        params_widget.setStyleSheet(TRANSPARENT_STYLE)
        params_layout = QVBoxLayout(params_widget)
        params_layout.setContentsMargins(0, 0, 0, 0)
        params_layout.setSpacing(2)
        for param in plugin_data.get('params', []):
            self._create_param_widget(param, plugin_inst, params_layout)
        group_layout.addWidget(params_widget)
        self.content_layout.addWidget(group)

    def _create_ai_effect_block(self, plugin_inst, index, plugin_data) -> None:
        """Create a visual block for an AI plugin with Process button."""
        instance_id = plugin_inst['instance_id']
        plugin_id = plugin_inst['plugin_id']
        enabled = plugin_inst.get('enabled', True)
        plugin_name = plugin_data.get('name', plugin_id)
        category = plugin_data.get('category', '')
        scale = plugin_data.get('scale', 1.0)

        display_name = f"[AI] {plugin_name.upper()}"
        group = QGroupBox(display_name)
        group.setStyleSheet(AI_EFFECT_GROUP_STYLE)
        group_layout = QVBoxLayout()
        group_layout.setContentsMargins(4, 4, 4, 4)
        group_layout.setSpacing(4)
        group.setLayout(group_layout)

        # Header Row
        header_layout = QHBoxLayout()
        header_layout.setSpacing(8)

        enable_check = ToggleSwitch(checked=enabled)
        enable_check.toggled.connect(
            lambda checked, iid=instance_id: self._toggle_plugin(iid, checked)
        )
        header_layout.addWidget(enable_check)

        enable_label = QLabel(I18N.tr("plugin.enabled", "Enabled"))
        enable_label.setStyleSheet(ENABLE_LABEL_STYLE)
        header_layout.addWidget(enable_label)
        header_layout.addStretch()

        # Scale badge
        scale_label = QLabel(f"{scale}x" if scale > 1 else category)
        scale_label.setStyleSheet(SCALE_LABEL_STYLE)
        header_layout.addWidget(scale_label)

        delete_btn = QPushButton("X")
        delete_btn.setObjectName("delete_btn")
        delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_btn.setToolTip(I18N.tr("plugin.remove_effect_tooltip", "Remove effect"))
        delete_btn.clicked.connect(
            lambda checked, iid=instance_id: self._delete_plugin(iid)
        )
        header_layout.addWidget(delete_btn)

        group_layout.addLayout(header_layout)

        # Parameter Widgets (blend, quality, etc.)
        params_widget = QWidget()
        params_widget.setStyleSheet(TRANSPARENT_STYLE)
        params_layout = QVBoxLayout(params_widget)
        params_layout.setContentsMargins(0, 0, 0, 0)
        params_layout.setSpacing(2)
        for param in plugin_data.get('params', []):
            self._create_param_widget(param, plugin_inst, params_layout)
        group_layout.addWidget(params_widget)

        # Process Button
        process_btn = QPushButton(I18N.tr("ai_plugin.process", "Process"))
        process_btn.setObjectName("ai_process_btn")
        process_btn.setMinimumHeight(28)
        process_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        process_btn.setStyleSheet(AI_PROCESS_BTN_STYLE)
        process_btn.clicked.connect(
            lambda checked, iid=instance_id: self.ai_process_requested.emit(iid)
        )
        group_layout.addWidget(process_btn)

        self.content_layout.addWidget(group)
    
    def _create_param_widget(self, param, plugin_inst, layout) -> None:
        """
        Create a widget for the given parameter.
        Supports: slider, checkbox, select

        Args:
            param: Parameter metadata dict
            plugin_inst: The plugin instance dict
            layout: The layout to add the widget to
        """
        param_type = param['type']
        instance_id = plugin_inst['instance_id']
        params = plugin_inst['params']

        # AUD-25508：不再把 instance_id 写入共享插件元数据 param dict
        # （同一插件的多个实例共享 manager 缓存的同一批 dict，覆写会
        # 导致 select 回调错路由并泄漏进新实例）。instance_id 一律
        # 通过 lambda 默认参数在创建时捕获。

        if param_type == 'slider':
            self._create_slider_widget(param, instance_id, params, layout)
        elif param_type == 'checkbox':
            self._create_checkbox_widget(param, instance_id, params, layout)
        elif param_type == 'color':
            self._create_color_widget(param, instance_id, params, layout)
        elif param_type == 'select':
            self._create_select_widget(param, instance_id, params, layout)
        else:
            # P1-COLOR-14 复查：未知参数类型不再静默不渲染——占位行 +
            # 日志让作者/用户都能发现能力边界。
            logger.warning(
                "Plugin param '%s' has unsupported type '%s' (plugin %s)",
                param.get('var_name', '?'), param_type, instance_id)
            row = QLabel(
                f"{param.get('label', param.get('var_name', '?'))} — "
                f"{param_type} (unsupported)")
            row.setStyleSheet("color: gray; font-style: italic;")
            layout.addWidget(row)
    
    def _create_slider_widget(self, param, instance_id, params, layout) -> None:
        """Create a ParamSliderRow for slider parameters."""
        var_name = param['var_name']
        label = param['label']
        min_val = param.get('min', 0.0)
        max_val = param.get('max', 1.0)
        default_val = param.get('default', 0.0)
        
        # Get current value from instance params or use default
        current_val = params.get(var_name, default_val)
        
        # Calculate step for reasonable precision
        step = (max_val - min_val) / 100.0
        
        row = ParamSliderRow(
            label, min_val, max_val, current_val,
            step=step, decimals=2, label_width=90)
        row.interactionStarted.connect(self.interaction_started.emit)
        row.interactionEnded.connect(self.interaction_ended.emit)
        row.valueChanged.connect(
            lambda val, iid=instance_id, vn=var_name: self._on_param_changed(iid, vn, val))
        
        # Store metadata on the row
        row._instance_id = instance_id
        row._var_name = var_name
        
        layout.addWidget(row)
    
    def _create_checkbox_widget(self, param, instance_id, params, layout) -> None:
        """Create a checkbox widget for boolean parameters."""
        var_name = param['var_name']
        label = param['label']
        default_val = param.get('default', 0)
        
        # Get current value
        current_val = params.get(var_name, default_val)
        
        # Horizontal layout for alignment
        param_layout = QHBoxLayout()
        param_layout.setSpacing(4)
        
        # Spacer to align with sliders
        spacer = QLabel("")
        spacer.setFixedWidth(90)
        param_layout.addWidget(spacer)
        
        checkbox = QCheckBox(label)
        checkbox.setChecked(bool(current_val))
        # Use global DARK_THEME QCheckBox style
        
        # Store metadata
        checkbox._instance_id = instance_id
        checkbox._var_name = var_name
        
        # Connect signal
        checkbox.toggled.connect(self._on_checkbox_toggled)
        
        param_layout.addWidget(checkbox)
        param_layout.addStretch()
        
        layout.addLayout(param_layout)
    
    def _create_color_widget(self, param, instance_id, params, layout) -> None:
        """Create a ColorPickerControl row for color parameters (P1-COLOR-14).

        插件 color 参数是 scene-linear RGB [0,1]（直接进 vec3 uniform），
        ColorPickerControl 本身工作在 scene-linear，因此无需额外转换。
        """
        var_name = param['var_name']
        label = param['label']
        default = param.get('default') or [1.0, 1.0, 1.0]
        current = params.get(var_name)
        if not isinstance(current, (list, tuple)) or len(current) < 3:
            current = default

        param_layout = QHBoxLayout()
        param_layout.setSpacing(4)

        lbl = QLabel(label)
        lbl.setFixedWidth(90)
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        param_layout.addWidget(lbl)

        picker = ColorPickerControl(
            (float(current[0]), float(current[1]), float(current[2]), 1.0))
        picker.color_changed.connect(
            lambda color, iid=instance_id, vn=var_name:
                self._on_color_changed(iid, vn, color))
        param_layout.addWidget(picker)
        param_layout.addStretch()

        layout.addLayout(param_layout)

    def _on_color_changed(self, instance_id, var_name, color) -> None:
        """Commit a confirmed color-picker result to the plugin params."""
        if not self.current_layer:
            return
        r, g, b, _alpha = qcolor_to_linear_rgba(color)
        # 单次离散修改：用交互边界包住，保证恰好一次 undo 快照
        self.interaction_started.emit()
        plugin_inst = self.current_layer.get_plugin_instance(instance_id)
        if plugin_inst:
            plugin_inst['params'][var_name] = [r, g, b]
        self.current_layer.is_dirty = True
        self.params_changed.emit()
        self.interaction_ended.emit()
    
    def _create_select_widget(self, param, instance_id, params, layout) -> None:
        """Create a ChevronComboBox for select parameters.

        回显实例当前值（保存过的选择），声明 default 只作缺省。
        AUD-25508：instance_id 在创建时捕获（lambda 默认参数），不写入
        共享 param dict。
        """
        label = param['label']
        options = param.get('options', [])
        default_value = param.get('default', 0)
        current = int(params.get(param['var_name'], default_value))

        param_layout = QHBoxLayout()
        param_layout.setSpacing(4)

        lbl = QLabel(label)
        lbl.setFixedWidth(90)
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        param_layout.addWidget(lbl)

        combo = ChevronComboBox()
        combo.setMinimumHeight(18)
        combo.addItems(options)
        # setCurrentIndex 会触发 currentIndexChanged；构建期先 blockSignals
        combo.blockSignals(True)
        combo.setCurrentIndex(max(0, min(current, len(options) - 1)))
        combo.blockSignals(False)

        # Store parameter info for callback
        combo.setProperty('param_var_name', param['var_name'])
        combo.setProperty('instance_id', instance_id)

        # Connect signal（默认参数捕获创建时的实例 id，避免闭包晚绑定）
        combo.currentIndexChanged.connect(
            lambda idx, iid=instance_id, vn=param['var_name']:
                self._on_select_changed(iid, vn, idx))

        param_layout.addWidget(combo)
        param_layout.addStretch()

        layout.addLayout(param_layout)
    
    # === Callback Methods ===
    
    def _toggle_plugin(self, instance_id, state) -> None:
        """Toggle a plugin instance's enabled state.

        AUD-25509：离散操作同样用交互边界包住，保证恰好一次 undo 快照
        （对齐 _on_color_changed 的约定；undo 压栈发生在控制器的
        on_interaction_started）。
        """
        if not self.current_layer:
            return

        self.interaction_started.emit()
        self.current_layer.set_plugin_enabled(instance_id, state)
        # Emit signal to request render
        self.params_changed.emit()
        self.interaction_ended.emit()

    def _reset_plugin(self, instance_id, plugin_id) -> None:
        """Reset all parameters of a plugin instance to their default values.

        AUD-25509：重置是离散操作，用交互边界包住以产生恰好一次
        undo 快照（重置前的参数值可撤销恢复）。
        """
        if not self.current_layer:
            return

        # Get the plugin instance
        plugin_inst = self.current_layer.get_plugin_instance(instance_id)
        if not plugin_inst:
            return

        # Fetch plugin metadata to get default values
        plugin_data = get_plugin_manager().get_plugin(plugin_id)
        if not plugin_data:
            return

        self.interaction_started.emit()

        # Reset all parameters to their default values
        for param in plugin_data.get('params', []):
            var_name = param['var_name']
            default_val = param.get('default', 0.0)
            plugin_inst['params'][var_name] = default_val

        # Mark layer as dirty
        self.current_layer.is_dirty = True

        # Refresh the UI to show reset values
        self.set_layer(self.current_layer)

        # Emit signal to request render
        self.params_changed.emit()
        self.interaction_ended.emit()
    
    def _delete_plugin(self, instance_id) -> None:
        """Remove a plugin instance from the layer.

        AUD-25509：删除是离散操作，用交互边界包住以产生恰好一次
        undo 快照（删除前的效果栈可撤销恢复）。
        """
        if not self.current_layer:
            return

        self.interaction_started.emit()

        # Find layer index and delegate to engine for unified resource cleanup
        layer_index = -1
        if hasattr(self.engine, 'layers'):
            for i, l in enumerate(self.engine.layers):
                if l.id == self.current_layer.id:
                    layer_index = i
                    break

        if layer_index >= 0:
            self.engine.remove_plugin(layer_index, instance_id)
        else:
            # Fallback: just remove from layer if engine not available
            self.current_layer.remove_plugin(instance_id)

        # Refresh the UI stack
        self.set_layer(self.current_layer)

        # Refresh layer panel to update fx indicator
        if hasattr(self.engine, 'layers') and hasattr(self, 'parent'):
            main_window = self._find_main_window()
            if main_window and hasattr(main_window, 'layer_panel'):
                main_window.layer_panel.update_layers(
                    self.engine.layers,
                    getattr(self.engine, 'active_layer_index', 0)
                )

        # Emit signal to request render
        self.params_changed.emit()
        self.interaction_ended.emit()
    
    def _find_main_window(self):
        """Find the main window parent."""
        widget = self.parent()
        while widget:
            if widget.__class__.__name__ == 'MainWindow':
                return widget
            widget = widget.parent()
        return None
    
    def _on_slider_pressed(self) -> None:
        """Handle slider press - start interaction."""
        self.interaction_started.emit()
    
    def _on_slider_released(self) -> None:
        """Handle slider release - end interaction."""
        self.interaction_ended.emit()
    
    def _on_param_changed(self, instance_id, var_name, value) -> None:
        """Handle parameter value change."""
        if not self.current_layer:
            return
        
        # Update the plugin instance params
        plugin_inst = self.current_layer.get_plugin_instance(instance_id)
        if plugin_inst:
            plugin_inst['params'][var_name] = value
        
        # Mark layer as dirty
        self.current_layer.is_dirty = True
        
        # Emit signal to request render
        self.params_changed.emit()
    
    def _on_checkbox_toggled(self, checked) -> None:
        """Handle checkbox toggle."""
        if not self.current_layer:
            return

        checkbox = self.sender()
        if not checkbox or not hasattr(checkbox, '_instance_id'):
            return

        instance_id = checkbox._instance_id
        var_name = checkbox._var_name

        # Update the plugin instance params
        plugin_inst = self.current_layer.get_plugin_instance(instance_id)
        if plugin_inst:
            plugin_inst['params'][var_name] = 1 if checked else 0

        # Mark layer as dirty
        self.current_layer.is_dirty = True

        # Emit signal to request render
        self.params_changed.emit()

    def _on_select_changed(self, instance_id, var_name, index) -> None:
        """Handle select dropdown change.

        AUD-25508：instance_id 由创建时的 lambda 默认参数传入，不再从
        共享 param dict 读取（该 dict 被多实例共享，读取会被最后一个
        实例的覆写污染，导致回调错路由）。
        """
        if not self.current_layer:
            return

        # Update the plugin instance params
        plugin_inst = self.current_layer.get_plugin_instance(instance_id)
        if plugin_inst:
            plugin_inst['params'][var_name] = index

        # Mark layer as dirty
        self.current_layer.is_dirty = True

        # Emit signal to request render
        self.params_changed.emit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        ParamSliderRow.apply_visibility_to_all(
            ParamSliderRow.collect_rows(self), self.width())

