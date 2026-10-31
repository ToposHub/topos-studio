from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QListWidget, QListWidgetItem, QLabel, QFrame,
                             QLineEdit, QSlider, QMenu, QToolButton)
from src.shared.i18n.i18n import I18N
from PyQt6.QtCore import pyqtSignal, Qt, QSize
from src.shared.ui.style_sheet import LAYER_LIST_STYLE
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.assets.icon_manager import (
    icon_add, icon_delete, icon_reset, icon_up, icon_down,
    icon_visible, icon_hidden, icon_mask, icon_fx, IconColors,
    icon_copy, icon_paste, icon_duplicate, icon_grading, icon_ai,
    icon_settings
)

# AUD-25421: UI key → 规范混合模式名（Layer.BLEND_MODES 显示名）。
# 下拉框显示本地化名（I18N），但只有规范名可以发给引擎 / 持久化：
# composition_uniforms 通过 Layer.BLEND_MODES.index(name) 反查索引，
# 反查失败会静默回退 Normal（渲染错误 + from_dict 持久化丢状态）。
# 注意 "saturation_blend" 的规范名是 "Saturation"（不是标题化结果
# "Saturation Blend"，后者不在 BLEND_MODES 中）。
_BLEND_MODE_KEY_TO_CANONICAL = {
    "normal": "Normal",
    "multiply": "Multiply",
    "screen": "Screen",
    "overlay": "Overlay",
    "soft_light": "Soft Light",
    "hard_light": "Hard Light",
    "color_dodge": "Color Dodge",
    "color_burn": "Color Burn",
    "lighten": "Lighten",
    "darken": "Darken",
    "difference": "Difference",
    "exclusion": "Exclusion",
    "hue": "Hue",
    "saturation_blend": "Saturation",
    "color": "Color",
    "luminosity": "Luminosity",
}


class LayerItemWidget(QWidget):
    visibility_changed = pyqtSignal(bool)
    delete_clicked = pyqtSignal()
    reset_clicked = pyqtSignal()
    name_changed = pyqtSignal(str)
    bake_clicked = pyqtSignal()  # AI Baking: bake/unbake layer

    def __init__(self, name, visible=True, has_fx=False, has_mask=False, has_grading=False, is_baked=False, is_offline_mode=False, has_source_bake=False, parent=None) -> None:
        super().__init__(parent)
        self.original_name = name
        self.is_editing = False
        self._is_baked = is_baked
        self._is_offline_mode = is_offline_mode  # Offline Media Handling
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 1, 4, 1)
        layout.setSpacing(2)

        self.visible_btn = QPushButton()
        self.visible_btn.setCheckable(True)
        self.visible_btn.setChecked(visible)
        self.visible_btn.setFixedSize(20, 20)
        self._update_eye_icon(visible)
        self.visible_btn.setStyleSheet("QPushButton { border: none; background: transparent; }")
        self.visible_btn.toggled.connect(self._on_toggled)

        self.name_edit = QLineEdit(name)
        self.name_edit.setMinimumWidth(80)  # 最小宽度，允许自动扩展
        self.name_edit.setMaximumWidth(200)  # 最大宽度限制
        self.name_edit.setReadOnly(True)  # 只读，只能右键重命名
        self.name_edit.setStyleSheet("""
            QLineEdit {
                background: transparent;
                border: none;
                color: #eee;
                font-size: 11px;
                padding-left: 8px;
            }
        """)
        self.name_edit.editingFinished.connect(self._on_name_edited)
        self.name_edit.returnPressed.connect(self._finish_editing)
        # 允许鼠标事件穿透到父级 QListWidget，以支持选择和拖拽
        self.name_edit.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        # Grading indicator icon (调色图标)
        self.grading_icon = QLabel()
        self.grading_icon.setPixmap(icon_grading().pixmap(16, 16))
        self.grading_icon.setFixedSize(18, 18)
        self.grading_icon.setStyleSheet("background: transparent;")
        self.grading_icon.setToolTip(I18N.tr(
            "tooltip.layer_has_grading", "Layer has grading adjustments applied"))
        self.grading_icon.setVisible(has_grading)

        # FX indicator icon
        self.fx_icon = QLabel()
        self.fx_icon.setPixmap(icon_fx().pixmap(16, 16))
        self.fx_icon.setFixedSize(18, 18)
        self.fx_icon.setStyleSheet("background: transparent;")
        self.fx_icon.setToolTip(I18N.tr(
            "tooltip.layer_has_fx", "Layer has effects applied"))
        self.fx_icon.setVisible(has_fx)

        # Mask indicator icon
        self.mask_icon = QLabel()
        self.mask_icon.setPixmap(icon_mask().pixmap(16, 16))
        self.mask_icon.setFixedSize(18, 18)
        self.mask_icon.setStyleSheet("background: transparent;")
        self.mask_icon.setToolTip(I18N.tr(
            "tooltip.layer_has_masks", "Layer has active masks"))
        self.mask_icon.setVisible(has_mask)

        # AI Source Bake indicator icon (denoise/deblur applied to source image)
        self.ai_source_icon = QLabel()
        self.ai_source_icon.setPixmap(icon_ai().pixmap(16, 16))
        self.ai_source_icon.setFixedSize(18, 18)
        self.ai_source_icon.setStyleSheet("background: transparent;")
        self.ai_source_icon.setToolTip(I18N.tr(
            "tooltip.layer_ai_bake", "AI source bake active"))
        self.ai_source_icon.setVisible(has_source_bake)

        # AI Baking: Bake/Freeze button (雪花图标表示冻结/缓存)
        self.bake_btn = QPushButton()
        self.bake_btn.setFixedSize(20, 20)
        self.bake_btn.setToolTip(I18N.tr("tooltip.bake_layer", "栅格化图层 (缓存AI计算)"))
        self._update_bake_icon(is_baked)
        self.bake_btn.setStyleSheet("""
            QPushButton { border: none; background: transparent; }
            QPushButton:hover { background: rgba(100, 180, 255, 50); }
        """)
        self.bake_btn.clicked.connect(self._on_bake_clicked)

        self.reset_btn = QPushButton()
        self.reset_btn.setIcon(icon_reset())
        self.reset_btn.setIconSize(QSize(14, 14))
        self.reset_btn.setFixedSize(20, 20)
        self.reset_btn.setToolTip(I18N.tr(
            "tooltip.reset_layer_adjustments", "Reset Layer Adjustments"))
        self.reset_btn.setStyleSheet("QPushButton { border: none; background: transparent; } QPushButton:hover { background: rgba(255, 136, 0, 30); }")
        self.reset_btn.clicked.connect(self.reset_clicked.emit)

        self.del_btn = QPushButton()
        self.del_btn.setIcon(icon_delete())
        self.del_btn.setIconSize(QSize(14, 14))
        self.del_btn.setFixedSize(20, 20)
        self.del_btn.setToolTip(I18N.tr("tooltip.remove_layer", "Remove Layer"))
        self.del_btn.setStyleSheet("QPushButton { border: none; background: transparent; } QPushButton:hover { background: rgba(255, 0, 0, 30); }")
        self.del_btn.clicked.connect(self.delete_clicked.emit)

        layout.addWidget(self.visible_btn)
        layout.addWidget(self.name_edit)
        layout.addWidget(self.grading_icon)
        layout.addWidget(self.fx_icon)
        layout.addWidget(self.mask_icon)
        layout.addWidget(self.ai_source_icon)
        layout.addStretch() # Push everything else to the right
        layout.addWidget(self.bake_btn)
        layout.addWidget(self.reset_btn)
        layout.addWidget(self.del_btn)
        
        # Enable context menu on the widget
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        
        # Make all child widgets also propagate context menu events
        for child in self.findChildren(QWidget):
            child.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            child.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos) -> None:
        """Forward context menu request to parent layer panel."""
        # Find the parent LayerPanel and show its context menu
        parent = self.parent()
        while parent:
            if isinstance(parent, LayerPanel):
                # AUD-25423：pos 位于信号发送者（子控件）坐标系，
                # 必须以 sender 为原点映射到全局坐标；此前统一用
                # self（LayerItemWidget）映射，右键行内按钮时菜单
                # 位置偏移该子控件在行内的坐标。
                sender = self.sender()
                origin = sender if isinstance(sender, QWidget) else self
                global_pos = origin.mapToGlobal(pos)
                parent.context_menu.exec_(global_pos)
                break
            parent = parent.parent()

    def start_editing(self) -> None:
        """进入编辑模式，允许用户修改图层名称。"""
        self.is_editing = True
        # 临时禁用鼠标穿透，以便用户可以编辑文本
        self.name_edit.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.name_edit.setReadOnly(False)
        self.name_edit.selectAll()
        self.name_edit.setFocus()
        self.name_edit.setCursorPosition(0)
        self.name_edit.setCursorPosition(len(self.name_edit.text()))

    def _finish_editing(self) -> None:
        """完成编辑，保存更改并退出编辑模式。"""
        if self.is_editing:
            self.is_editing = False
            self.name_edit.setReadOnly(True)
            self.name_edit.clearFocus()
            # 恢复鼠标穿透，以便拖拽和选择图层
            self.name_edit.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def _on_name_edited(self) -> None:
        new_name = self.name_edit.text().strip()
        if new_name and new_name != self.original_name:
            self.original_name = new_name
            self.name_changed.emit(new_name)
        elif not new_name:
            self.name_edit.setText(self.original_name)
        self._finish_editing()

    def _on_toggled(self, checked) -> None:
        self._update_eye_icon(checked)
        self.visibility_changed.emit(checked)

    def _update_eye_icon(self, visible) -> None:
        self.visible_btn.setIcon(icon_visible() if visible else icon_hidden())
        self.visible_btn.setToolTip("Hide Layer" if visible else "Show Layer")

    def _update_bake_icon(self, is_baked) -> None:
        """Update the bake button icon based on baked state.

        In offline mode, baked layers cannot be unbaked - the button is disabled
        to prevent destroying the only surviving cache of the original image.
        """
        self._is_baked = is_baked

        # Offline Mode Protection: Disable unbake button for baked layers
        if self._is_offline_mode and is_baked:
            self.bake_btn.setText("❄")
            self.bake_btn.setDisabled(True)
            self.bake_btn.setStyleSheet("""
                QPushButton { 
                    border: none; 
                    background: rgba(255, 100, 100, 60); 
                    color: #ff6666;
                    font-size: 12px;
                    border-radius: 3px;
                }
                QPushButton:disabled { 
                    background: rgba(255, 100, 100, 40); 
                    color: #ff9999;
                }
            """)
            self.bake_btn.setToolTip(I18N.tr("tooltip.offline_unbake_blocked",
                "原始素材已离线，无法解冻 (Original media offline, cannot unbake)"))
            return

        # Normal mode
        self.bake_btn.setDisabled(False)
        if is_baked:
            # Baked: show snowflake with blue highlight
            self.bake_btn.setText("❄")  # Snowflake unicode
            self.bake_btn.setStyleSheet("""
                QPushButton { 
                    border: none; 
                    background: rgba(100, 180, 255, 80); 
                    color: #64b4ff;
                    font-size: 12px;
                    border-radius: 3px;
                }
                QPushButton:hover { background: rgba(100, 180, 255, 120); }
            """)
            self.bake_btn.setToolTip(I18N.tr("tooltip.unbake_layer", "解冻图层 (重新计算)"))
        else:
            # Not baked: show empty/dim snowflake
            self.bake_btn.setText("❄")
            self.bake_btn.setStyleSheet("""
                QPushButton { 
                    border: none; 
                    background: transparent; 
                    color: #666;
                    font-size: 12px;
                }
                QPushButton:hover { 
                    background: rgba(100, 180, 255, 50); 
                    color: #888;
                }
            """)
            self.bake_btn.setToolTip(I18N.tr("tooltip.bake_layer", "栅格化图层 (缓存AI计算)"))

    def _on_bake_clicked(self) -> None:
        """Handle bake button click."""
        self.bake_clicked.emit()

    def set_baked_state(self, is_baked) -> None:
        """Update the baked state of this layer item."""
        self._update_bake_icon(is_baked)

    def set_offline_mode(self, is_offline_mode: bool) -> None:
        """Update the offline mode state of this layer item.

        In offline mode, baked layers cannot be unbaked.

        Args:
            is_offline_mode: True if operating in offline cache mode
        """
        self._is_offline_mode = is_offline_mode
        # Re-apply bake icon style with new offline mode state
        self._update_bake_icon(self._is_baked)


class LayerPanel(QWidget):
    layer_selected = pyqtSignal(int)
    layer_added = pyqtSignal()
    layer_removed = pyqtSignal(int)
    layer_reset = pyqtSignal(int)
    layer_moved = pyqtSignal(int, int)
    layer_visibility_changed = pyqtSignal(int, bool)
    layer_renamed = pyqtSignal(int, str)
    layer_blend_mode_changed = pyqtSignal(str)
    layer_opacity_changed = pyqtSignal(float)

    # Layer Copy/Paste/Duplicate signals
    layer_copy = pyqtSignal(int)
    layer_paste = pyqtSignal(int)
    layer_duplicate = pyqtSignal(int)

    # AI Baking signal
    layer_bake = pyqtSignal(int)  # layer_index
    layer_unbake = pyqtSignal(int)  # layer_index - 解冻/取消栅格化

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._source_bake_type = None  # "denoise" | "deblur" | None
        self.init_ui()
        self.setup_context_menu()

    def init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QFrame()
        header.setFixedHeight(30)
        header.setStyleSheet("background: #2f2f33; border-bottom: 1px solid #222;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 0, 5, 0)

        title = QLabel(I18N.tr("panel.layers", "图层"))
        title.setStyleSheet("font-weight: bold; font-size: 10px; color: #aaa;")
        header_layout.addWidget(title)
        header_layout.addStretch()

        # Settings button (gear icon) for layer menu
        self.settings_btn = QPushButton()
        self.settings_btn.setIcon(icon_settings(IconColors.DEFAULT))
        self.settings_btn.setFixedSize(22, 22)
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent; border: none; border-radius: 3px;
            }
            QPushButton:hover { background-color: #3d3d3d; }
            QPushButton:pressed { background-color: #2d2d2d; }
        """)
        self.settings_btn.clicked.connect(self._show_settings_menu)
        header_layout.addWidget(self.settings_btn)
        layout.addWidget(header)

        # Offline Mode Warning Banner
        self.offline_warning_banner = QFrame()
        self.offline_warning_banner.setFixedHeight(28)
        self.offline_warning_banner.setStyleSheet("""
            QFrame {
                background: rgba(255, 80, 80, 0.15);
                border-bottom: 1px solid rgba(255, 80, 80, 0.3);
            }
        """)
        warning_layout = QHBoxLayout(self.offline_warning_banner)
        warning_layout.setContentsMargins(8, 0, 8, 0)
        warning_icon = QLabel("⚠️")
        warning_icon.setStyleSheet("font-size: 12px;")
        warning_text = QLabel(I18N.tr("warning.offline_mode",
            "离线缓存模式：找不到原始图片，部分 AI 功能已锁定"))
        warning_text.setStyleSheet("color: #ff6666; font-size: 10px;")
        warning_layout.addWidget(warning_icon)
        warning_layout.addWidget(warning_text)
        warning_layout.addStretch()
        self.offline_warning_banner.setVisible(False)  # Hidden by default
        layout.addWidget(self.offline_warning_banner)

        # Layer Properties (Blend Mode & Opacity)
        props_frame = QFrame()
        props_frame.setFixedHeight(65)
        props_frame.setStyleSheet("background: #27272b; border-bottom: 1px solid #222;")
        props_layout = QVBoxLayout(props_frame)
        props_layout.setContentsMargins(8, 5, 8, 5)
        props_layout.setSpacing(4)

        # Row 1: Blend Mode
        blend_layout = QHBoxLayout()
        blend_label = QLabel(I18N.tr("layer.mode", "模式:"))
        blend_label.setStyleSheet("font-size: 10px; color: #888;")
        self.blend_combo = ChevronComboBox()
        # AUD-25421: 显示本地化名，UserRole data 绑定规范名；emit 只发规范名。
        for key, canonical in _BLEND_MODE_KEY_TO_CANONICAL.items():
            display = I18N.tr(f"layer.{key}", canonical)
            self.blend_combo.addItem(display, canonical)
        self.blend_combo.currentIndexChanged.connect(self._on_blend_mode_changed)
        blend_layout.addWidget(blend_label)
        blend_layout.addWidget(self.blend_combo, stretch=1)
        props_layout.addLayout(blend_layout)

        # Row 2: Opacity
        opacity_layout = QHBoxLayout()
        opacity_label = QLabel(I18N.tr("layer.opacity", "不透明度:"))
        opacity_label.setStyleSheet("font-size: 10px; color: #888;")
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(100)
        self.opacity_slider.setFixedHeight(15)
        self.opacity_slider.valueChanged.connect(lambda v: self.layer_opacity_changed.emit(v / 100.0))
        
        self.opacity_val_label = QLabel("100%")
        self.opacity_val_label.setFixedWidth(30)
        self.opacity_val_label.setStyleSheet("font-size: 10px; color: #aaa;")
        self.opacity_slider.valueChanged.connect(lambda v: self.opacity_val_label.setText(f"{v}%"))

        opacity_layout.addWidget(opacity_label)
        opacity_layout.addWidget(self.opacity_slider, stretch=1)
        opacity_layout.addWidget(self.opacity_val_label)
        props_layout.addLayout(opacity_layout)

        layout.addWidget(props_frame)

        # List
        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet(LAYER_LIST_STYLE)
        self.list_widget.setDragEnabled(True)
        self.list_widget.setAcceptDrops(True)
        self.list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.currentRowChanged.connect(self._on_row_changed)
        self.list_widget.model().rowsMoved.connect(self._on_rows_moved)
        layout.addWidget(self.list_widget)

        # Footer Toolbar
        footer = QFrame()
        footer.setFixedHeight(35)
        footer.setStyleSheet("background: #2f2f33; border-top: 1px solid #222;")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(5, 0, 5, 0)
        footer_layout.setSpacing(2)

        # Match the Edit page timeline toolbar: compact icon-only controls with
        # transparent backgrounds and subtle hover/pressed feedback.
        footer_btn_style = """
            QToolButton {
                background: transparent;
                border: none;
                border-radius: 4px;
                padding: 3px;
                margin: 1px;
            }
            QToolButton:hover {
                background-color: rgba(255, 255, 255, 18);
            }
            QToolButton:pressed {
                background-color: rgba(255, 255, 255, 10);
            }
            QToolButton:disabled {
                background: transparent;
            }
        """
        footer_btn_size = 26
        footer_icon_size = 15

        self.add_btn = QToolButton()
        self.add_btn.setIcon(icon_add())
        self.add_btn.setToolTip(I18N.tr("tooltip.add_layer", "添加图层"))
        self.del_btn = QToolButton()
        self.del_btn.setIcon(icon_delete())
        self.del_btn.setToolTip(I18N.tr("tooltip.delete_layer", "删除选中"))
        self.up_btn = QToolButton()
        self.up_btn.setIcon(icon_up())
        self.up_btn.setToolTip(I18N.tr("tooltip.move_up", "上移"))
        self.down_btn = QToolButton()
        self.down_btn.setIcon(icon_down())
        self.down_btn.setToolTip(I18N.tr("tooltip.move_down", "下移"))
        
        # Copy/Paste/Duplicate buttons
        self.copy_btn = QToolButton()
        self.copy_btn.setIcon(icon_copy())
        self.copy_btn.setToolTip(I18N.tr("tooltip.copy_layer", "复制图层参数 (Ctrl+Shift+C)"))
        self.copy_btn.setStyleSheet(footer_btn_style)
        self.copy_btn.clicked.connect(self._on_copy_clicked)
        
        self.paste_btn = QToolButton()
        self.paste_btn.setIcon(icon_paste())
        self.paste_btn.setToolTip(I18N.tr("tooltip.paste_layer", "粘贴图层参数 (Ctrl+V)"))
        self.paste_btn.setStyleSheet(footer_btn_style)
        self.paste_btn.clicked.connect(self._on_paste_clicked)
        
        self.dup_btn = QToolButton()
        self.dup_btn.setIcon(icon_duplicate())
        self.dup_btn.setToolTip(I18N.tr("tooltip.duplicate_layer", "复制为新图层 (Ctrl+D)"))
        self.dup_btn.setStyleSheet(footer_btn_style)
        self.dup_btn.clicked.connect(self._on_duplicate_clicked)

        footer_layout.addStretch()
        for btn in [self.add_btn, self.del_btn, self.up_btn, self.down_btn]:
            btn.setFixedSize(footer_btn_size, footer_btn_size)
            btn.setIconSize(QSize(footer_icon_size, footer_icon_size))
            btn.setStyleSheet(footer_btn_style)
            footer_layout.addWidget(btn)
        
        footer_layout.addSpacing(8)
        for btn in [self.copy_btn, self.paste_btn, self.dup_btn]:
            btn.setFixedSize(footer_btn_size, footer_btn_size)
            btn.setIconSize(QSize(footer_icon_size, footer_icon_size))
            footer_layout.addWidget(btn)

        self.add_btn.clicked.connect(self.layer_added.emit)
        # AUD-25424：底部删除按钮复用带无选择守护的 _delete_current_layer。
        # 此前 currentRow()==-1 时 _ui_to_model(-1) 返回 -1 直接 emit，
        # 下游 engine.remove_layer(-1) 负索引命中顶层图层并触发自动保存。
        self.del_btn.clicked.connect(self._delete_current_layer)
        self.up_btn.clicked.connect(self._move_up)
        self.down_btn.clicked.connect(self._move_down)
        layout.addWidget(footer)

    def _show_settings_menu(self) -> None:
        """显示图层设置菜单（标题栏齿轮按钮）。"""
        menu = QMenu(self)

        add_act = menu.addAction(I18N.tr("action.new_layer", "New Layer"))
        add_act.triggered.connect(lambda: self.layer_added.emit())

        del_act = menu.addAction(I18N.tr("action.delete_layer", "Delete Current Layer"))
        del_act.triggered.connect(lambda: self._delete_current_layer())

        menu.addSeparator()

        rename_act = menu.addAction(I18N.tr("action.rename_layer", "Rename Layer..."))
        rename_act.triggered.connect(self._on_rename_clicked)

        toggle_vis_act = menu.addAction(I18N.tr("action.toggle_visibility", "Toggle Layer Visibility"))
        toggle_vis_act.triggered.connect(self._toggle_current_visibility)

        disable_all_act = menu.addAction(I18N.tr("action.disable_all_layers", "Disable All Layers"))
        disable_all_act.triggered.connect(self._disable_all_layers)

        menu.addSeparator()

        reset_act = menu.addAction(I18N.tr("action.reset_layer", "Reset Current Layer"))
        reset_act.triggered.connect(lambda: self._reset_current_layer())

        menu.addSeparator()

        copy_act = menu.addAction(I18N.tr("action.copy_layer", "Copy Layer Parameters"))
        copy_act.triggered.connect(lambda: self._on_copy_clicked())

        paste_act = menu.addAction(I18N.tr("action.paste_layer", "Paste Layer Parameters"))
        paste_act.triggered.connect(lambda: self._on_paste_clicked())

        dup_act = menu.addAction(I18N.tr("action.duplicate_layer", "Duplicate Layer"))
        dup_act.triggered.connect(lambda: self._on_duplicate_clicked())

        # 在按钮下方弹出菜单
        btn = self.settings_btn
        pos = btn.mapToGlobal(btn.rect().bottomLeft())
        menu.exec(pos)

    def _delete_current_layer(self) -> None:
        """删除当前选中图层。"""
        idx = self.list_widget.currentRow()
        if idx >= 0:
            model_idx = self._ui_to_model(idx)
            self.layer_removed.emit(model_idx)

    def _on_blend_mode_changed(self, _index: int) -> None:
        """用户切换混合模式：只把规范名（data）发给引擎，绝不发本地化显示名。"""
        canonical = self.blend_combo.currentData()
        if isinstance(canonical, str) and canonical:
            self.layer_blend_mode_changed.emit(canonical)

    def _toggle_current_visibility(self) -> None:
        """切换当前图层可见性。"""
        idx = self.list_widget.currentRow()
        if idx >= 0:
            model_idx = self._ui_to_model(idx)
            item = self.list_widget.item(idx)
            widget = self.list_widget.itemWidget(item)
            if widget and isinstance(widget, LayerItemWidget):
                # AUD-25420: LayerItemWidget 没有显式 _is_visible 属性，
                # 可见性真值存储在 visible_btn 的 check 态（见 __init__）。
                new_state = not widget.visible_btn.isChecked()
                self.layer_visibility_changed.emit(model_idx, new_state)

    def _disable_all_layers(self) -> None:
        """禁用所有图层。"""
        main_win = self.window()
        if hasattr(main_win, 'disable_all_layers'):
            main_win.disable_all_layers()

    def _reset_current_layer(self) -> None:
        """重置当前图层。"""
        idx = self.list_widget.currentRow()
        if idx >= 0:
            model_idx = self._ui_to_model(idx)
            self.layer_reset.emit(model_idx)

    def setup_context_menu(self) -> None:
        """设置图层列表的右键菜单。"""
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)

        # 创建右键菜单
        self.context_menu = QMenu(self)

        rename_act = self.context_menu.addAction(I18N.tr("layer.rename", "重命名图层"))
        rename_act.triggered.connect(self._on_rename_clicked)

        self.context_menu.addSeparator()

        # 栅格化菜单项 - 根据图层状态动态显示
        self.bake_act = self.context_menu.addAction(I18N.tr("layer.bake", "栅格化图层"))
        self.bake_act.triggered.connect(self._on_bake_clicked)

        self.unbake_act = self.context_menu.addAction(I18N.tr("layer.unbake", "解冻图层"))
        self.unbake_act.triggered.connect(self._on_unbake_clicked)

        self.context_menu.addSeparator()

        copy_act = self.context_menu.addAction(I18N.tr("layer.copy", "复制图层参数"))
        copy_act.triggered.connect(lambda: self._on_copy_clicked())

        paste_act = self.context_menu.addAction(I18N.tr("layer.paste", "粘贴图层参数"))
        paste_act.triggered.connect(lambda: self._on_paste_clicked())

        self.context_menu.addSeparator()

        dup_act = self.context_menu.addAction(I18N.tr("layer.duplicate", "复制为新图层"))
        dup_act.triggered.connect(lambda: self._on_duplicate_clicked())

        self.context_menu.addSeparator()

        add_act = self.context_menu.addAction(I18N.tr("layer.add", "添加新图层"))
        add_act.triggered.connect(lambda: self.layer_added.emit())

    def _on_rename_clicked(self) -> None:
        """处理右键菜单重命名命令。"""
        item = self.list_widget.currentItem()
        if item:
            widget = self.list_widget.itemWidget(item)
            if widget and isinstance(widget, LayerItemWidget):
                widget.start_editing()

    def _show_context_menu(self, pos) -> None:
        """显示右键菜单，根据图层状态动态显示栅格化选项。"""
        item = self.list_widget.itemAt(pos)
        if item:
            
            # 从外部获取图层状态（需要调用者提供 layers 数据）
            # 这里我们通过检查 LayerItemWidget 的状态来判断
            widget = self.list_widget.itemWidget(item)
            is_baked = False
            if widget and isinstance(widget, LayerItemWidget):
                is_baked = widget._is_baked
            
            # 动态显示/隐藏栅格化菜单项
            self.bake_act.setVisible(not is_baked)
            self.unbake_act.setVisible(is_baked)
            
            self.context_menu.exec_(self.list_widget.viewport().mapToGlobal(pos))

    def _on_bake_clicked(self) -> None:
        """处理栅格化图层菜单项点击。"""
        index = self._get_current_layer_index()
        if index >= 0:
            self.layer_bake.emit(index)

    def _on_unbake_clicked(self) -> None:
        """处理解冻图层菜单项点击。"""
        index = self._get_current_layer_index()
        if index >= 0:
            self.layer_unbake.emit(index)

    def _on_rows_moved(self, parent, start, end, dest, row) -> None:
        """处理拖拽改变图层顺序。"""
        # 由于 UI 是倒序显示，需要转换索引
        count = self.list_widget.count()
        if count <= 1:
            return
        old_model_idx = count - 1 - start
        # Clamp row to valid range: Qt can pass row == count when dropping after last item
        clamped_row = max(0, min(row, count - 1))
        new_model_idx = count - 1 - clamped_row

        # Clamp model indices to valid range
        old_model_idx = max(0, min(old_model_idx, count - 1))
        new_model_idx = max(0, min(new_model_idx, count - 1))

        if old_model_idx != new_model_idx:
            self.layer_moved.emit(old_model_idx, new_model_idx)

    def _get_current_layer_index(self):
        """获取当前选中图层的索引（模型索引）。"""
        ui_row = self.list_widget.currentRow()
        if ui_row < 0:
            return -1
        return self._ui_to_model(ui_row)

    def _on_copy_clicked(self) -> None:
        """处理复制图层参数按钮点击。"""
        index = self._get_current_layer_index()
        if index >= 0:
            self.layer_copy.emit(index)

    def _on_paste_clicked(self) -> None:
        """处理粘贴图层参数按钮点击。"""
        index = self._get_current_layer_index()
        if index >= 0:
            self.layer_paste.emit(index)

    def _on_duplicate_clicked(self) -> None:
        """处理复制图层按钮点击。"""
        index = self._get_current_layer_index()
        if index >= 0:
            self.layer_duplicate.emit(index)

    def _ui_to_model(self, ui_row):
        if ui_row < 0: return -1
        count = self.list_widget.count()
        return count - 1 - ui_row

    def _model_to_ui(self, model_idx):
        if model_idx < 0: return -1
        count = self.list_widget.count()
        return count - 1 - model_idx

    def _on_row_changed(self, row) -> None:
        model_idx = self._ui_to_model(row)
        if model_idx >= 0:
            self.layer_selected.emit(model_idx)

    def _move_up(self) -> None:
        ui_row = self.list_widget.currentRow()
        if ui_row > 0:
            model_idx = self._ui_to_model(ui_row)
            # In inverted UI, "Move Up" means increasing model index (Top)
            self.layer_moved.emit(model_idx, model_idx + 1)

    def _move_down(self) -> None:
        ui_row = self.list_widget.currentRow()
        count = self.list_widget.count()
        if ui_row >= 0 and ui_row < count - 1:
            model_idx = self._ui_to_model(ui_row)
            # Inverted UI, "Move Down" means decreasing model index (Bottom)
            self.layer_moved.emit(model_idx, model_idx - 1)

    def update_layers(self, layers, active_index, is_offline_mode=False) -> None:
        """Update the layer list UI.

        Args:
            layers: List of Layer objects
            active_index: Index of the currently active layer
            is_offline_mode: True if operating in offline cache mode (original media unavailable)
        """
        self.list_widget.blockSignals(True)
        self.blend_combo.blockSignals(True)
        self.opacity_slider.blockSignals(True)

        # Update offline mode warning banner visibility
        self._update_offline_warning(is_offline_mode)

        # Store scroll position if needed
        # current_scroll = self.list_widget.verticalScrollBar().value()

        self.list_widget.clear()
        # Reverse iterate layers for UI (Top layer at index N shows at row 0)
        for i in range(len(layers) - 1, -1, -1):
            layer = layers[i]
            # Check if layer has effects (plugins)
            has_fx = len(getattr(layer, 'plugins', [])) > 0

            # Check if layer has any shape masks or AI matte enabled
            has_mask = len(getattr(layer, 'mask_shapes', [])) > 0 or (getattr(layer, 'ai_matte', None) is not None and getattr(layer, 'ai_matte_enabled', False))
            
            # Check if layer has grading adjustments
            has_grading = layer.has_grading_effects() if hasattr(layer, 'has_grading_effects') else False

            # Check if layer is baked (AI Baking)
            is_baked = getattr(layer, 'is_baked', False)

            # Check if source has AI bake (denoise/deblur)
            has_source_bake = self._source_bake_type is not None

            item = QListWidgetItem(self.list_widget)
            item_widget = LayerItemWidget(layer.name, layer.visible, has_fx, has_mask, has_grading, is_baked, is_offline_mode, has_source_bake)
            item.setSizeHint(item_widget.sizeHint())
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, item_widget)

            # Connect using model index 'i'
            item_widget.visibility_changed.connect(lambda v, idx=i: self.layer_visibility_changed.emit(idx, v))
            item_widget.delete_clicked.connect(lambda idx=i: self.layer_removed.emit(idx))
            item_widget.reset_clicked.connect(lambda idx=i: self.layer_reset.emit(idx))
            item_widget.name_changed.connect(lambda name, idx=i: self.layer_renamed.emit(idx, name))
            item_widget.bake_clicked.connect(lambda idx=i: self.layer_bake.emit(idx))

        if 0 <= active_index < len(layers):
            active_layer = layers[active_index]
            # AUD-25421: 用规范名（data）定位下拉项；面板未收录的模式
            # （如 "Addition"）回退 Normal，与引擎 [R3-1] 的未知模式回退一致。
            blend_idx = self.blend_combo.findData(active_layer.blend_mode)
            self.blend_combo.setCurrentIndex(blend_idx if blend_idx >= 0 else 0)
            op_val = int(active_layer.opacity * 100)
            self.opacity_slider.setValue(op_val)
            self.opacity_val_label.setText(f"{op_val}%")

        # Map model active_index to UI row
        ui_row = self._model_to_ui(active_index)
        self.list_widget.setCurrentRow(ui_row)

        self.opacity_slider.blockSignals(False)
        self.blend_combo.blockSignals(False)
        self.list_widget.blockSignals(False)

    def _update_offline_warning(self, is_offline_mode: bool) -> None:
        """Update the visibility of the offline mode warning banner.

        Args:
            is_offline_mode: True if operating in offline cache mode
        """
        self.offline_warning_banner.setVisible(is_offline_mode)

    def update_layer_icons(self, model_index, layer) -> None:
        """Lightweight update of indicator icons for a single layer without rebuilding the list."""
        ui_row = self._model_to_ui(model_index)
        if ui_row < 0 or ui_row >= self.list_widget.count():
            return
        item = self.list_widget.item(ui_row)
        if not item:
            return
        widget = self.list_widget.itemWidget(item)
        if not widget or not isinstance(widget, LayerItemWidget):
            return

        has_grading = layer.has_grading_effects() if hasattr(layer, 'has_grading_effects') else False
        has_fx = len(getattr(layer, 'plugins', [])) > 0
        has_mask = len(getattr(layer, 'mask_shapes', [])) > 0 or (getattr(layer, 'ai_matte', None) is not None and getattr(layer, 'ai_matte_enabled', False))

        widget.grading_icon.setVisible(has_grading)
        widget.fx_icon.setVisible(has_fx)
        widget.mask_icon.setVisible(has_mask)

        # AI source bake icon
        has_source_bake = self._source_bake_type is not None
        widget.ai_source_icon.setVisible(has_source_bake)
        if has_source_bake:
            widget.ai_source_icon.setToolTip(
                I18N.tr(f"tooltip.source_bake_{self._source_bake_type}",
                        f"AI {self._source_bake_type} applied to source"))

    def set_source_bake_type(self, bake_type) -> None:
        """设置当前 source bake 类型，影响所有图层的 AI 图标显示。"""
        self._source_bake_type = bake_type
