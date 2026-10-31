from typing import List, Optional, Set, Dict, Any
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QLabel, QSizePolicy,
                             QTreeWidget, QTreeWidgetItem, QCheckBox, QGroupBox,
                             QMenu)
from src.shared.i18n.i18n import I18N
from PyQt6.QtCore import pyqtSignal, Qt, QSize, QTimer
from src.features.color.core.layer import MaskGroup
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.assets.icon_manager import (
    icon_delete, icon_visible, icon_hidden, icon_invert,
    icon_operation_add, icon_operation_subtract, icon_operation_intersect,
    icon_circle, icon_rect, icon_pen, icon_gradient, icon_ai, icon_brush,
    icon_folder, icon_reset, icon_eraser, icon_cut, icon_smart)


class MaskTreeWidget(QTreeWidget):
    """自定义树形控件，在拖拽操作完成后发射层级变更信号。"""

    hierarchy_changed = pyqtSignal()

    def dropEvent(self, event) -> None:
        """重写拖拽放下事件，在父类处理完成后发射信号。"""
        super().dropEvent(event)
        self.hierarchy_changed.emit()


class ShapeItemWidget(QWidget):
    """遮罩项的 UI 组件，显示可见性、反转、布尔运算和删除按钮。"""
    visibility_toggled = pyqtSignal(bool)
    invert_toggled = pyqtSignal(bool)
    lock_toggled = pyqtSignal(bool)
    operation_changed = pyqtSignal(str)
    delete_requested = pyqtSignal()

    def __init__(self, name: str, visible: bool, invert: bool,
                 operation: str = "add", shape_type: str = "rect",
                 locked: bool = False, parent=None) -> None:
        super().__init__(parent)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)  # 去掉上下间距，完全由 TreeWidget 行高接管
        layout.setSpacing(4)

        # 1. 眼睛图标 (可见性) - 放最左侧
        self.visible_btn = QPushButton()
        self.visible_btn.setCheckable(True)
        self.visible_btn.setChecked(visible)
        self.visible_btn.setFixedSize(18, 18)
        self.visible_btn.setIconSize(QSize(14, 14))
        self._update_eye_icon(visible)
        self.visible_btn.setStyleSheet("QPushButton { border: none; background: transparent; padding: 0px; margin: 0px; }")
        self.visible_btn.toggled.connect(self._on_visibility_toggled)

        # 2. 图层类型图标
        self.type_icon = QLabel()
        icon_map = {
            "circle": icon_circle, "rect": icon_rect, "curve": icon_pen,
            "gradient": icon_gradient, "group": icon_folder, "bitmap": icon_ai, "brush": icon_brush
        }
        # 使用 icon_manager 获取图标
        icon_func = icon_map.get(shape_type, icon_rect)
        self.type_icon.setPixmap(icon_func().pixmap(12, 12))
        self.type_icon.setFixedSize(18, 18)
        self.type_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.type_icon.setStyleSheet("QLabel { padding: 0px; margin: 0px; }")

        # 3. 标签名称
        self.label = QLabel(name)
        self.label.setStyleSheet("font-size: 10px; color: #dddddd; font-weight: bold;")
        self.label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)

        # 4. 反转按钮
        self.invert_btn = QPushButton()
        self.invert_btn.setIcon(icon_invert())
        self.invert_btn.setIconSize(QSize(12, 12))
        self.invert_btn.setCheckable(True)
        self.invert_btn.setChecked(invert)
        self.invert_btn.setFixedSize(18, 18)
        self.invert_btn.setToolTip(I18N.tr("mask.invert_tooltip", "Invert Mask"))
        self.invert_btn.setStyleSheet("QPushButton { border: none; background: transparent; padding: 0px; margin: 0px; } QPushButton:checked { background: rgba(255, 165, 0, 180); border-radius: 3px; }")
        self.invert_btn.toggled.connect(self.invert_toggled.emit)

        # 4.5 锁定按钮 (P1-COLOR-09: 锁定后画布禁止拖动/改形)
        self.lock_btn = QPushButton()
        self.lock_btn.setCheckable(True)
        self.lock_btn.setChecked(locked)
        self.lock_btn.setFixedSize(18, 18)
        self._update_lock_icon(locked)
        self.lock_btn.setStyleSheet("QPushButton { border: none; background: transparent; padding: 0px; margin: 0px; } QPushButton:checked { background: rgba(100, 160, 255, 140); border-radius: 3px; }")
        self.lock_btn.toggled.connect(self._on_lock_toggled)

        # 5. 运算方式按钮
        self.op_btn = QPushButton()
        self.op_btn.setFixedSize(18, 18)
        self.op_btn.setStyleSheet("QPushButton { border: none; background: transparent; padding: 0px; margin: 0px; } QPushButton:hover { background: rgba(255, 255, 255, 20); border-radius: 3px; }")
        self.current_op = operation
        self._update_op_icon()
        self.op_btn.clicked.connect(self._cycle_operation)

        # 6. 删除按钮
        self.del_btn = QPushButton()
        self.del_btn.setIcon(icon_delete())
        self.del_btn.setIconSize(QSize(12, 12))
        self.del_btn.setFixedSize(18, 18)
        self.del_btn.setToolTip(I18N.tr("mask.delete_tooltip", "Delete Mask Window"))
        self.del_btn.setStyleSheet("QPushButton { border: none; background: transparent; padding: 0px; margin: 0px; } QPushButton:hover { background: rgba(255, 0, 0, 30); }")
        self.del_btn.clicked.connect(self.delete_requested.emit)

        # 按顺序添加到布局中（恢复默认的 QHBoxLayout 居中行为）
        layout.addWidget(self.visible_btn)
        layout.addWidget(self.type_icon)
        layout.addWidget(self.label, stretch=1)
        layout.addWidget(self.invert_btn)
        layout.addWidget(self.lock_btn)
        layout.addWidget(self.op_btn)
        layout.addWidget(self.del_btn)

    def _on_lock_toggled(self, checked: bool) -> None:
        self._update_lock_icon(checked)
        self.lock_toggled.emit(checked)

    def _update_lock_icon(self, locked: bool) -> None:
        # emoji 图标避免新增 icon_manager 依赖；锁定=实心锁，未锁=开放锁
        self.lock_btn.setText("🔒" if locked else "🔓")
        self.lock_btn.setToolTip(
            I18N.tr("mask.lock_tooltip_on", "已锁定：解锁以允许画布编辑")
            if locked else
            I18N.tr("mask.lock_tooltip_off", "锁定窗口（画布禁止拖动/改形）"))

    def _on_visibility_toggled(self, checked: bool) -> None:
        self._update_eye_icon(checked)
        self.visibility_toggled.emit(checked)

    def _update_eye_icon(self, visible: bool) -> None:
        self.visible_btn.setIcon(icon_visible() if visible else icon_hidden())
        self.visible_btn.setToolTip("Disable Mask" if visible else "Enable Mask")

    def _cycle_operation(self) -> None:
        ops = ["add", "subtract", "intersect"]
        idx = (ops.index(self.current_op) + 1) % len(ops)
        self.current_op = ops[idx]
        self._update_op_icon()
        self.operation_changed.emit(self.current_op)

    def _update_op_icon(self) -> None:
        icons = {"add": icon_operation_add, "subtract": icon_operation_subtract, "intersect": icon_operation_intersect}
        self.op_btn.setIcon(icons.get(self.current_op, icon_operation_add)())
        self.op_btn.setIconSize(QSize(12, 12))
        self.op_btn.setToolTip(f"Mode: {self.current_op.capitalize()}")


class MaskPanel(QWidget):
    """遮罩控制面板，支持树形结构的遮罩群组管理。

    Performance Optimization:
    - Uses QTimer throttling (16ms ≈ 60 FPS) for drag value changes
    - UI updates are immediate, engine sync is rate-limited
    - Prevents main thread blocking during rapid dragging
    """

    shape_added = pyqtSignal(str)
    shape_removed = pyqtSignal(str)
    shape_selected = pyqtSignal(str)
    shape_updated = pyqtSignal(str, dict)  # id, {visible, invert, feather, opacity}
    interaction_started = pyqtSignal()
    interaction_ended = pyqtSignal()
    ai_birefnet_requested = pyqtSignal()
    ai_sam_toggled = pyqtSignal(bool)
    all_masks_cleared = pyqtSignal()
    brush_toggled = pyqtSignal(bool)
    brush_params_changed = pyqtSignal(dict)
    group_added = pyqtSignal(list)  # 新增：群组添加信号，传递选中的 ID 列表
    hierarchy_updated = pyqtSignal(list)  # 新增：层级变更信号，传递嵌套字典列表
    send_to_tracker_requested = pyqtSignal()  # 阶段5：把当前选中窗口发送到跟踪器
    # P1-COLOR-09: 批量复制窗口（duplicate=原地复制；copy_to_layer=复制到其他图层）
    duplicate_shape_requested = pyqtSignal(str)  # shape_id
    copy_shapes_to_layer_requested = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._brush_mode_active = False
        self._pending_shapes: List = []
        # AUD-25501：最近一次显式选中请求（select_shape），延迟树重建
        # 时优先满足，避免被原选中恢复/自动选末项覆盖
        self._explicit_selection_request: Optional[str] = None

        # Performance: Throttle parameter updates to 60 FPS during drag
        self._param_timer = QTimer()
        self._param_timer.setSingleShot(True)
        self._param_timer.timeout.connect(self._emit_pending_params)
        self._pending_params: Dict[str, Any] = {}
        self._pending_shape_id: Optional[str] = None

        self.init_ui()

    def get_style_sheet(self) -> str:
        return """
            MaskPanel {
                background-color: #1a1a1a;
            }
            QGroupBox {
                border: 1px solid #333;
                border-radius: 6px;
                margin-top: 8px;
                font-weight: bold;
                font-size: 9px;
                color: #aaa;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
            QPushButton {
                background-color: #333;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 4px 10px;
                color: #bbb;
                font-size: 10px;
            }
            QPushButton:hover {
                background-color: #3d3d3d;
                color: #eee;
                border: 1px solid #555;
            }
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
            QLabel {
                color: #bbbbbb;
                font-size: 11px;
            }
            QTreeWidget {
                background-color: #1a1a1a;
                border: 1px solid #333;
                border-radius: 4px;
                color: #eee;
                outline: none;
            }
            QTreeWidget::item {
                padding: 0px 4px;
                margin: 0px;
                border-radius: 4px;
                background-color: transparent;
                border: 1px solid transparent;
                min-height: 24px;
            }
            QTreeWidget::item:hover {
                background-color: rgba(255, 255, 255, 10);
            }
            QTreeWidget::item:selected {
                background-color: rgba(255, 136, 0, 25);
                border: 1px solid rgba(255, 136, 0, 150);
                color: #fff;
            }
            QTreeWidget::branch:has-children:!has-siblings:closed,
            QTreeWidget::branch:closed:has-children:has-siblings {
                border-image: none;
                image: none;
            }
        """

    def init_ui(self) -> None:
        self.setStyleSheet(self.get_style_sheet())

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.setSpacing(4)
        self.setLayout(main_layout)

        # Header with title and reset button
        header_layout = QHBoxLayout()
        self.title_label = QLabel(I18N.tr("mask.title", "遮罩控制"))
        self.title_label.setStyleSheet("""
            color: #888; font-weight: bold; font-size: 10px;
            letter-spacing: 1px;
        """)
        header_layout.addWidget(self.title_label)

        header_layout.addStretch()

        self.reset_btn = QPushButton()
        self.reset_btn.setObjectName("reset_btn")
        self.reset_btn.setIcon(icon_reset())
        self.reset_btn.setIconSize(QSize(12, 12))
        self.reset_btn.setFixedSize(16, 16)
        self.reset_btn.setToolTip(I18N.tr(
            "mask.reset_tooltip", "Reset All Mask Parameters"))
        self.reset_btn.clicked.connect(self.reset_all)
        header_layout.addWidget(self.reset_btn)
        main_layout.addLayout(header_layout)

        # Main 2-Column Container (3:2 ratio)
        columns_container = QWidget()
        columns_layout = QHBoxLayout()
        columns_layout.setContentsMargins(0, 0, 0, 0)
        columns_layout.setSpacing(6)
        columns_container.setLayout(columns_layout)
        main_layout.addWidget(columns_container)

        # ========== LEFT COLUMN (stretch=3) ==========
        left_column = QWidget()
        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)
        left_column.setLayout(left_layout)

        # Tools Group
        tools_group = QGroupBox(I18N.tr("mask.tools", "工具"))
        tools_layout = QVBoxLayout()
        tools_layout.setContentsMargins(4, 0, 4, 4)
        tools_layout.setSpacing(4)
        tools_group.setLayout(tools_layout)

        btn_style = """
            QPushButton {
                background: rgba(255, 255, 255, 10);
                border: 1px solid rgba(255, 255, 255, 30);
                border-radius: 4px;
            }
            QPushButton:hover { background: rgba(255, 255, 255, 30); border: 1px solid #ff8800; }
            QPushButton:pressed { background: #ff8800; }
        """

        # Row 1: Shapes (包含群组按钮)
        shapes_row = QWidget()
        shapes_layout = QHBoxLayout(shapes_row)
        shapes_layout.setContentsMargins(0, 0, 0, 0)
        shapes_layout.setSpacing(6)

        shapes = [
            (I18N.tr("mask.circle", "圆形"), "circle", icon_circle),
            (I18N.tr("mask.rect", "矩形"), "rect", icon_rect),
            (I18N.tr("mask.curve", "曲线"), "curve", icon_pen),
            (I18N.tr("mask.gradient", "渐变"), "gradient", icon_gradient),
            (I18N.tr("mask.group", "群组"), "group", icon_folder)
        ]

        for label, s_type, icon_func in shapes:
            btn = QPushButton(f" {label}")
            btn.setIcon(icon_func())
            btn.setIconSize(QSize(14, 14))
            btn.setMinimumWidth(60)
            btn.setFixedHeight(28)
            btn.setToolTip(I18N.tr("mask.add_window", "添加 {0} 窗口").format(label))
            btn.setStyleSheet(btn_style + "QPushButton { color: #ccc; font-size: 10px; font-weight: bold; padding: 0 4px; }")
            if s_type == "group":
                btn.clicked.connect(self._on_group_btn_clicked)
            else:
                btn.clicked.connect(lambda checked, t=s_type: self.shape_added.emit(t))
            shapes_layout.addWidget(btn)

        shapes_layout.addStretch()
        tools_layout.addWidget(shapes_row)

        # Row 2: AI Tools + Brush (combined row)
        ai_row = QWidget()
        ai_layout = QHBoxLayout(ai_row)
        ai_layout.setContentsMargins(0, 0, 0, 0)
        ai_layout.setSpacing(6)

        self.btn_birefnet = QPushButton(I18N.tr("mask.remove_bg", "移除背景"))
        self.btn_birefnet.setIcon(icon_cut())
        self.btn_birefnet.setIconSize(QSize(14, 14))
        self.btn_birefnet.setToolTip(I18N.tr(
            "mask.remove_bg_tooltip", "Auto Remove Background (BiRefNet)"))
        self.btn_birefnet.setStyleSheet(btn_style + "QPushButton { color: #8f8; }")
        self.btn_birefnet.clicked.connect(self.ai_birefnet_requested.emit)

        self.btn_sam = QPushButton(I18N.tr("mask.smart_brush", "智能画笔"))
        self.btn_sam.setIcon(icon_smart())
        self.btn_sam.setIconSize(QSize(14, 14))
        self.btn_sam.setCheckable(True)
        self.btn_sam.setToolTip(I18N.tr(
            "mask.sam_tooltip", "Interactive Segmentation (SAM)"))
        self.btn_sam.setStyleSheet(btn_style + "QPushButton:checked { background: #00aa00; border-color: #00ff00; } QPushButton { color: #8ff; }")
        self.btn_sam.toggled.connect(self.ai_sam_toggled.emit)

        # Brush button
        self.btn_brush = QPushButton(f" {I18N.tr('mask.brush', '画笔')}")
        self.btn_brush.setIcon(icon_brush())
        self.btn_brush.setIconSize(QSize(14, 14))
        self.btn_brush.setCheckable(True)
        self.btn_brush.setFixedHeight(28)
        self.btn_brush.setToolTip(I18N.tr(
            "mask.brush_tooltip",
            "Draw mask with brush (B to toggle, [ ] to resize, E for eraser)"))
        self.btn_brush.setStyleSheet(btn_style + "QPushButton:checked { background: #ff8800; border-color: #ff8800; } QPushButton { color: #ff8; font-weight: bold; }")
        self.btn_brush.toggled.connect(self._on_brush_toggled)

        # Eraser button
        self.btn_eraser = QPushButton(f" {I18N.tr('mask.eraser', '橡皮擦')}")
        self.btn_eraser.setIcon(icon_eraser())
        self.btn_eraser.setIconSize(QSize(14, 14))
        self.btn_eraser.setCheckable(True)
        self.btn_eraser.setFixedHeight(28)
        self.btn_eraser.setToolTip(I18N.tr(
            "mask.eraser_tooltip", "Toggle Eraser Mode (E) - Subtract from mask"))
        self.btn_eraser.setStyleSheet(btn_style + "QPushButton:checked { background: #0096ff; border-color: #0096ff; } QPushButton { color: #6af; font-weight: bold; }")
        self.btn_eraser.toggled.connect(self._on_eraser_toggled)

        # 发送到跟踪器入口（阶段 5）
        self.btn_send_tracker = QPushButton(I18N.tr("mask.send_tracker", "发送到跟踪器"))
        self.btn_send_tracker.setIconSize(QSize(14, 14))
        self.btn_send_tracker.setToolTip(I18N.tr(
            "mask.send_tracker_tooltip",
            "Send selected window to Tracker / 用跟踪器跟踪当前窗口"))
        self.btn_send_tracker.setStyleSheet(btn_style + "QPushButton { color: #FFD43B; }")
        self.btn_send_tracker.clicked.connect(self.send_to_tracker_requested.emit)

        ai_layout.addWidget(self.btn_birefnet)
        ai_layout.addWidget(self.btn_sam)
        ai_layout.addWidget(self.btn_brush)
        ai_layout.addWidget(self.btn_eraser)
        ai_layout.addWidget(self.btn_send_tracker)
        ai_layout.addStretch()
        tools_layout.addWidget(ai_row)

        left_layout.addWidget(tools_group)

        # Windows List Group (使用自定义 MaskTreeWidget)
        list_group = QGroupBox(I18N.tr("mask.windows_list", "窗口列表"))
        list_layout = QVBoxLayout()
        list_layout.setContentsMargins(4, 0, 4, 4)
        list_layout.setSpacing(2)
        list_group.setLayout(list_layout)

        self.tree_widget = MaskTreeWidget()
        self.tree_widget.setIndentation(16)
        self.tree_widget.setHeaderHidden(True)
        self.tree_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.tree_widget.setMinimumHeight(120)
        self.tree_widget.setDragDropMode(QTreeWidget.DragDropMode.InternalMove)
        self.tree_widget.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)  # 开启 Shift/Ctrl 多选支持
        # itemSelectionChanged（而非 itemClicked）保证键盘导航和程序化
        # setCurrentItem 同样触发选中通知；防递归路径均在 blockSignals 内
        self.tree_widget.itemSelectionChanged.connect(self.on_item_clicked)
        self.tree_widget.hierarchy_changed.connect(self._on_tree_hierarchy_changed)
        # P1-COLOR-09: 右键菜单（复制窗口到图层 / 原地复制）
        self.tree_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree_widget.customContextMenuRequested.connect(
            self._show_tree_context_menu)
        list_layout.addWidget(self.tree_widget)

        left_layout.addWidget(list_group)
        columns_layout.addWidget(left_column, stretch=3)

        # ========== RIGHT COLUMN (stretch=2) ==========
        right_column = QWidget()
        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)
        right_column.setLayout(right_layout)

        # Parameters Group — 一行两个紧凑参数
        params_group = QGroupBox(I18N.tr("mask.parameters", "参数"))
        params_layout = QVBoxLayout()
        params_layout.setContentsMargins(4, 0, 4, 4)
        params_layout.setSpacing(2)
        params_group.setLayout(params_layout)

        # Row 1: 不透明度 + 羽化
        r1 = QHBoxLayout(); r1.setSpacing(4)
        self.opacity_drag = self.create_param(I18N.tr("mask.opacity", "不透明"), 0, 100, 100, compact=True)
        self.feather_drag = self.create_param(I18N.tr("mask.feather", "羽化"), 0, 100, 50, compact=True)
        r1.addWidget(self.opacity_drag); r1.addWidget(self.feather_drag); r1.addStretch()
        params_layout.addLayout(r1)

        # Row 2: 模糊 + 净化黑
        r2 = QHBoxLayout(); r2.setSpacing(4)
        self.blur_drag = self.create_param(I18N.tr("mask.blur", "模糊"), 0, 100, 0, compact=True)
        self.clean_black_drag = self.create_param(I18N.tr("mask.clean_black", "净化黑"), 0, 100, 0, compact=True)
        r2.addWidget(self.blur_drag); r2.addWidget(self.clean_black_drag); r2.addStretch()
        params_layout.addLayout(r2)

        # Row 3: 净化白 + 占位
        r3 = QHBoxLayout(); r3.setSpacing(4)
        self.clean_white_drag = self.create_param(I18N.tr("mask.clean_white", "净化白"), 0, 100, 0, compact=True)
        r3.addWidget(self.clean_white_drag); r3.addStretch()
        params_layout.addLayout(r3)

        right_layout.addWidget(params_group)

        # Transform Group — 一行两个紧凑参数
        transform_group = QGroupBox(I18N.tr("mask.transform", "变换"))
        tf_layout = QVBoxLayout()
        tf_layout.setContentsMargins(4, 0, 4, 4)
        tf_layout.setSpacing(2)
        transform_group.setLayout(tf_layout)

        # Row 1: X + Y
        t1 = QHBoxLayout(); t1.setSpacing(4)
        self.pos_x_drag = self.create_param("X", 0.0, 1.0, 0.5, decimals=3, compact=True)
        self.pos_y_drag = self.create_param("Y", 0.0, 1.0, 0.5, decimals=3, compact=True)
        t1.addWidget(self.pos_x_drag); t1.addWidget(self.pos_y_drag); t1.addStretch()
        tf_layout.addLayout(t1)

        # Row 2: 宽 + 高
        t2 = QHBoxLayout(); t2.setSpacing(4)
        self.size_w_drag = self.create_param(I18N.tr("mask.width", "宽"), 0.001, 5.0, 0.3, decimals=3, compact=True)
        self.size_h_drag = self.create_param(I18N.tr("mask.height", "高"), 0.001, 5.0, 0.3, decimals=3, compact=True)
        t2.addWidget(self.size_w_drag); t2.addWidget(self.size_h_drag); t2.addStretch()
        tf_layout.addLayout(t2)

        # Row 3: 旋转 + 占位
        t3 = QHBoxLayout(); t3.setSpacing(4)
        self.rotation_drag = self.create_param(I18N.tr("mask.rotation", "旋转"), -360.0, 360.0, 0.0, decimals=1, suffix="°", compact=True)
        t3.addWidget(self.rotation_drag); t3.addStretch()
        tf_layout.addLayout(t3)

        self.transform_group = transform_group
        right_layout.addWidget(self.transform_group)

        # BRUSH Parameters Group
        self.brush_group = QGroupBox(I18N.tr("mask.brush", "画笔"))
        brush_layout = QVBoxLayout()
        brush_layout.setContentsMargins(4, 0, 4, 4)
        brush_layout.setSpacing(2)
        self.brush_group.setLayout(brush_layout)

        # Color Overlay
        self.brush_overlay_checkbox = QCheckBox(I18N.tr("mask.color_overlay", "颜色叠加"))
        self.brush_overlay_checkbox.setChecked(True)
        self.brush_overlay_checkbox.setStyleSheet("QCheckBox { color: #ccc; font-size: 11px; } QCheckBox::indicator { width: 14px; height: 14px; }")
        self.brush_overlay_checkbox.toggled.connect(self._on_brush_param_changed)
        brush_layout.addWidget(self.brush_overlay_checkbox)
        brush_layout.addSpacing(4)

        # Row 1: 大小 + 羽化
        b1 = QHBoxLayout(); b1.setSpacing(4)
        self.brush_size_drag = self.create_param(I18N.tr("mask.size", "大小"), 1, 500, 50, compact=True)
        self.brush_feather_drag = self.create_param(I18N.tr("mask.feather", "羽化"), 0, 100, 50, compact=True)
        b1.addWidget(self.brush_size_drag); b1.addWidget(self.brush_feather_drag); b1.addStretch()
        brush_layout.addLayout(b1)

        # Row 2: 不透明度 + 流量
        b2 = QHBoxLayout(); b2.setSpacing(4)
        self.brush_opacity_drag = self.create_param(I18N.tr("mask.opacity", "不透明"), 0, 100, 100, compact=True)
        self.brush_flow_drag = self.create_param(I18N.tr("mask.flow", "流量"), 0, 100, 100, compact=True)
        b2.addWidget(self.brush_opacity_drag); b2.addWidget(self.brush_flow_drag); b2.addStretch()
        brush_layout.addLayout(b2)

        self.brush_size_drag.valueChanged.connect(self._on_brush_param_changed)
        self.brush_feather_drag.valueChanged.connect(self._on_brush_param_changed)
        self.brush_opacity_drag.valueChanged.connect(self._on_brush_param_changed)
        self.brush_flow_drag.valueChanged.connect(self._on_brush_param_changed)

        self.brush_group.setVisible(False)
        right_layout.addWidget(self.brush_group)
        right_layout.addStretch()
        columns_layout.addWidget(right_column, stretch=2)

    def set_image_only_tools_visible(self, visible: bool) -> None:
        """显示/隐藏仅图片模式可用的单帧遮罩工具（Remove BG / 智能画笔 / 画笔 / 橡皮擦）。

        这些工具产出的 mask 无时间维度：抠像/笔迹绑定在触发时的那一帧，
        却会作用于整段视频的所有帧，因此仅在图片模式提供。
        几何形状与"发送到跟踪器"在视频模式保留。
        """
        self.btn_birefnet.setVisible(visible)
        self.btn_sam.setVisible(visible)
        self.btn_brush.setVisible(visible)
        self.btn_eraser.setVisible(visible)
        if not visible:
            # setChecked(False) 对未选中按钮无副作用；brush 取消时会级联复位 eraser
            self.btn_sam.setChecked(False)
            self.btn_brush.setChecked(False)

    def create_param(self, label_text: str, min_v: float, max_v: float, def_v: float,
                     decimals: int = 0, suffix: str = "", compact: bool = False) -> DragValue:
        """创建 DragValue 拖拽参数控件。

        设置 Maximum 水平尺寸策略，防止数值框在宽面板中拉伸变形。
        """
        drag = DragValue(label_text, min_v, max_v, def_v, decimals=decimals, suffix=suffix, compact=compact)
        drag.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        drag.valueChanged.connect(self.on_value_changed)
        # AUD-25500：转发 DragValue 交互边界信号，使拖拽遮罩参数时
        # main_window.on_interaction_started 能压入 undo 快照
        # （engine.history.push_state），拖拽修改可撤销。
        drag.interaction_started.connect(self.interaction_started.emit)
        drag.interaction_ended.connect(self.interaction_ended.emit)
        return drag

    def on_item_clicked(self, item: Optional[QTreeWidgetItem] = None) -> None:
        # itemSelectionChanged 不携带条目参数，统一从 currentItem 读取
        item = item or self.tree_widget.currentItem()
        if item is None:
            return
        shape_id = item.data(0, Qt.ItemDataRole.UserRole)
        if shape_id:
            self.shape_selected.emit(shape_id)

    def _schedule_param_update(self, shape_id: str, params: dict) -> None:
        """Schedule a throttled parameter update (max 60 FPS during drag)."""
        self._pending_shape_id = shape_id
        self._pending_params.update(params)
        if not self._param_timer.isActive():
            self._param_timer.start(16)  # 16ms ≈ 60 FPS

    def _emit_pending_params(self) -> None:
        """Emit the pending parameter update (called by timer timeout)."""
        if self._pending_shape_id and self._pending_params:
            self.shape_updated.emit(self._pending_shape_id, self._pending_params)
            self._pending_params = {}

    def _show_tree_context_menu(self, pos) -> None:
        """P1-COLOR-09: 窗口列表右键菜单（原地复制 / 复制到其他图层）。"""
        current = self.tree_widget.currentItem()
        selected_id = current.data(0, Qt.ItemDataRole.UserRole) if current else None
        menu = QMenu(self)
        dup_act = menu.addAction(
            I18N.tr("mask.duplicate_window", "复制选定窗口"))
        dup_act.setEnabled(selected_id is not None)
        copy_act = menu.addAction(
            I18N.tr("mask.copy_windows_to_layer", "复制全部窗口到图层..."))

        chosen = menu.exec(self.tree_widget.viewport().mapToGlobal(pos))
        if chosen is dup_act and selected_id:
            self.duplicate_shape_requested.emit(selected_id)
        elif chosen is copy_act:
            self.copy_shapes_to_layer_requested.emit()

    def on_value_changed(self, *args) -> None:
        """Handle drag value changes with throttled engine sync."""
        current_item = self.tree_widget.currentItem()
        if current_item:
            shape_id = current_item.data(0, Qt.ItemDataRole.UserRole)
            if shape_id:
                # Clean Black/White: UI 0-100 映射为实际效果 0-10
                params = {
                    "feather": self.feather_drag.value(),
                    "opacity": self.opacity_drag.value() / 100.0,
                    "blur": self.blur_drag.value(),
                    "clean_black": self.clean_black_drag.value() / 10.0,
                    "clean_white": self.clean_white_drag.value() / 10.0
                }
                # Transform
                if self.transform_group.isVisible():
                    params["center"] = [self.pos_x_drag.value(), self.pos_y_drag.value()]
                    params["size"] = [self.size_w_drag.value(), self.size_h_drag.value()]
                    params["rotation"] = self.rotation_drag.value()
                self._schedule_param_update(shape_id, params)

    def set_transform_params(self, center, size, rotation) -> None:
        """更新 Transform 数值（不触发信号）。"""
        drags = [self.pos_x_drag, self.pos_y_drag, self.size_w_drag, self.size_h_drag, self.rotation_drag]
        for d in drags:
            d.blockSignals(True)
        self.pos_x_drag.setValue(center[0])
        self.pos_y_drag.setValue(center[1])
        self.size_w_drag.setValue(size[0])
        self.size_h_drag.setValue(size[1])
        self.rotation_drag.setValue(rotation)
        for d in drags:
            d.blockSignals(False)

    def set_transform_visible(self, shape_type: str) -> None:
        """根据遮罩类型显示/隐藏 Transform 区域。"""
        self.transform_group.setVisible(shape_type in ('circle', 'rect', 'curve'))

    def update_shapes_list(self, shapes: List) -> None:
        """更新遮罩列表（延迟执行以避免信号冲突）。"""
        self._pending_shapes = list(shapes)
        QTimer.singleShot(0, self._do_update_shapes_list)

    def _do_update_shapes_list(self) -> None:
        """执行树形列表更新，支持递归群组结构。"""
        if not hasattr(self, '_pending_shapes'):
            return

        shapes = self._pending_shapes

        # AUD-25501：记录当前选中项 ID，重建后优先恢复（含控制器在
        # update_shapes_list 之后显式调用的 select_shape 选中的项），
        # 避免每次刷新都强制跳选最后一项并发射 shape_selected 副作用。
        current_item = self.tree_widget.currentItem()
        prev_selected_id = (current_item.data(0, Qt.ItemDataRole.UserRole)
                            if current_item is not None else None)

        # 1. 记录当前展开的节点 ID
        expanded_ids: Set[str] = self._get_expanded_ids()

        # 2. 阻塞信号并清空树
        self.tree_widget.blockSignals(True)
        self.tree_widget.clear()

        # 3. 递归构建树
        self._build_tree(shapes)

        # 4. 恢复展开状态
        self._restore_expanded_state(expanded_ids)

        self.tree_widget.blockSignals(False)

        # 5. 恢复选中项；仅在目标已不存在（删除/初次加载）时回退为
        #    自动选中最后一项
        explicit_id = self._explicit_selection_request
        self._explicit_selection_request = None
        target_id = explicit_id or prev_selected_id
        if target_id:
            # select_shape 内部 blockSignals：恢复选中不重发
            # shape_selected（引擎侧该形状本就处于选中态）
            self.select_shape(target_id)
            self._explicit_selection_request = None  # 消费掉 select_shape 的重新记录
        if self.tree_widget.currentItem() is None:
            self._auto_select_last_item()

    def _build_tree(self, shapes: List, parent_item: Optional[QTreeWidgetItem] = None) -> None:
        """递归构建树形结构。"""
        for s in shapes:
            item = QTreeWidgetItem()
            item.setData(0, Qt.ItemDataRole.UserRole, s.id)

            flags = item.flags()
            if isinstance(s, MaskGroup):
                item.setFlags(flags | Qt.ItemFlag.ItemIsDropEnabled | Qt.ItemFlag.ItemIsDragEnabled)
            else:
                item.setFlags((flags | Qt.ItemFlag.ItemIsDragEnabled) & ~Qt.ItemFlag.ItemIsDropEnabled)

            if parent_item:
                parent_item.addChild(item)
            else:
                self.tree_widget.addTopLevelItem(item)

            operation = getattr(s, 'operation', 'add')
            visible = getattr(s, 'visible', True)
            invert = getattr(s, 'invert', False)

            if isinstance(s, MaskGroup):
                display_name = s.name or "Group"
            else:
                display_name = getattr(s, 'name', None) or s.type.capitalize()

            shape_type = "group" if isinstance(s, MaskGroup) else getattr(s, 'type', 'rect')

            widget = ShapeItemWidget(display_name, visible, invert, operation,
                                     shape_type, locked=getattr(s, 'locked', False))
            self.tree_widget.setItemWidget(item, 0, widget)
            item.setSizeHint(0, widget.sizeHint())

            widget.visibility_toggled.connect(
                lambda v, sid=s.id: self.shape_updated.emit(sid, {"visible": v})
            )
            widget.invert_toggled.connect(
                lambda v, sid=s.id: self.shape_updated.emit(sid, {"invert": v})
            )
            widget.lock_toggled.connect(
                lambda v, sid=s.id: self.shape_updated.emit(sid, {"locked": v})
            )
            widget.operation_changed.connect(
                lambda v, sid=s.id: self.shape_updated.emit(sid, {"operation": v})
            )
            widget.delete_requested.connect(
                lambda sid=s.id: self.shape_removed.emit(sid)
            )

            if isinstance(s, MaskGroup):
                self._build_tree(s.children, item)
                item.setExpanded(True)

    def _get_expanded_ids(self) -> Set[str]:
        """收集当前所有展开节点的 ID。"""
        expanded_ids: Set[str] = set()

        def collect_expanded(parent: Optional[QTreeWidgetItem] = None) -> None:
            if parent is None:
                count = self.tree_widget.topLevelItemCount()
                for i in range(count):
                    item = self.tree_widget.topLevelItem(i)
                    if item and item.isExpanded():
                        sid = item.data(0, Qt.ItemDataRole.UserRole)
                        if sid:
                            expanded_ids.add(sid)
                    if item:
                        collect_expanded(item)
            else:
                count = parent.childCount()
                for i in range(count):
                    item = parent.child(i)
                    if item and item.isExpanded():
                        sid = item.data(0, Qt.ItemDataRole.UserRole)
                        if sid:
                            expanded_ids.add(sid)
                    if item:
                        collect_expanded(item)

        collect_expanded()
        return expanded_ids

    def _restore_expanded_state(self, expanded_ids: Set[str]) -> None:
        """恢复节点的展开状态。"""

        def restore_expanded(parent: Optional[QTreeWidgetItem] = None) -> None:
            if parent is None:
                count = self.tree_widget.topLevelItemCount()
                for i in range(count):
                    item = self.tree_widget.topLevelItem(i)
                    if item:
                        sid = item.data(0, Qt.ItemDataRole.UserRole)
                        if sid in expanded_ids:
                            item.setExpanded(True)
                        restore_expanded(item)
            else:
                count = parent.childCount()
                for i in range(count):
                    item = parent.child(i)
                    if item:
                        sid = item.data(0, Qt.ItemDataRole.UserRole)
                        if sid in expanded_ids:
                            item.setExpanded(True)
                        restore_expanded(item)

        restore_expanded()

    def _auto_select_last_item(self) -> None:
        """自动选中最后一个项（setCurrentItem 触发 itemSelectionChanged
        → on_item_clicked → shape_selected，无需手动补发信号）。"""
        if self.tree_widget.topLevelItemCount() > 0:
            last_top = self.tree_widget.topLevelItem(self.tree_widget.topLevelItemCount() - 1)
            if last_top:
                if last_top.childCount() > 0:
                    last_child = last_top.child(last_top.childCount() - 1)
                    if last_child:
                        self.tree_widget.setCurrentItem(last_child)
                else:
                    self.tree_widget.setCurrentItem(last_top)

    def set_params(self, feather: float, opacity: float, blur: float = 0.0,
                   clean_black: float = 0.0, clean_white: float = 0.0) -> None:
        """设置参数值。

        注意：Clean Black/White 参数传入的是实际效果值 (0-10)，会映射为 UI 值 (0-100)。
        """
        controls = [
            self.feather_drag,
            self.opacity_drag,
            self.blur_drag,
            self.clean_black_drag,
            self.clean_white_drag
        ]
        values = [
            feather,
            opacity * 100,
            blur,
            clean_black * 10,
            clean_white * 10
        ]

        for ctrl, val in zip(controls, values):
            ctrl.blockSignals(True)
            ctrl.setValue(val)
            ctrl.blockSignals(False)

    def select_shape(self, shape_id: str) -> None:
        """高亮选中指定的遮罩项。"""
        # AUD-25501：记录显式选中请求；若延迟树重建尚未执行，请求将在
        # 重建后得到满足
        self._explicit_selection_request = shape_id
        self.tree_widget.blockSignals(True)

        def find_and_select(parent: Optional[QTreeWidgetItem] = None) -> bool:
            if parent is None:
                count = self.tree_widget.topLevelItemCount()
                for i in range(count):
                    item = self.tree_widget.topLevelItem(i)
                    if item:
                        sid = item.data(0, Qt.ItemDataRole.UserRole)
                        if sid == shape_id:
                            self.tree_widget.setCurrentItem(item)
                            self.tree_widget.scrollToItem(item)
                            return True
                        if find_and_select(item):
                            return True
            else:
                count = parent.childCount()
                for i in range(count):
                    item = parent.child(i)
                    if item:
                        sid = item.data(0, Qt.ItemDataRole.UserRole)
                        if sid == shape_id:
                            self.tree_widget.setCurrentItem(item)
                            self.tree_widget.scrollToItem(item)
                            return True
                        if find_and_select(item):
                            return True
            return False

        find_and_select()
        self.tree_widget.blockSignals(False)

    def _on_brush_toggled(self, checked: bool) -> None:
        self._brush_mode_active = checked
        self.brush_group.setVisible(checked)
        self.brush_toggled.emit(checked)

        if checked:
            self.btn_sam.setChecked(False)
        else:
            self.btn_eraser.setChecked(False)

    def _on_eraser_toggled(self, checked: bool) -> None:
        params = self.get_brush_params()
        params["is_eraser"] = checked
        self.brush_params_changed.emit(params)

        if checked and not self._brush_mode_active:
            self.btn_brush.setChecked(True)

    def _on_brush_param_changed(self, *_) -> None:
        params = self.get_brush_params()
        params["is_eraser"] = self.btn_eraser.isChecked()
        self.brush_params_changed.emit(params)

    def get_brush_params(self) -> dict:
        return {
            "size": self.brush_size_drag.value(),
            "feather": self.brush_feather_drag.value(),
            "opacity": self.brush_opacity_drag.value() / 100.0,
            "flow": self.brush_flow_drag.value() / 100.0,
            "show_overlay": self.brush_overlay_checkbox.isChecked()
        }

    def set_brush_params(self, size: int, feather: int, opacity: float, flow: float) -> None:
        """设置画笔参数值。"""
        controls = [
            self.brush_size_drag,
            self.brush_feather_drag,
            self.brush_opacity_drag,
            self.brush_flow_drag
        ]
        values = [size, feather, opacity * 100, flow * 100]

        for ctrl, val in zip(controls, values):
            ctrl.blockSignals(True)
            ctrl.setValue(val)
            ctrl.blockSignals(False)

    def is_brush_active(self) -> bool:
        return self._brush_mode_active

    def _on_group_btn_clicked(self) -> None:
        """Group 按钮点击处理：获取选中的遮罩 ID 列表并发射信号。"""
        selected_ids = [item.data(0, Qt.ItemDataRole.UserRole) for item in self.tree_widget.selectedItems()]
        self.group_added.emit(selected_ids)

    def _on_tree_hierarchy_changed(self) -> None:
        """处理树形控件拖拽后的层级变更，提取结构并发射信号。"""
        structure: List[Dict[str, Any]] = []

        def extract_structure(parent: Optional[QTreeWidgetItem] = None) -> List[Dict[str, Any]]:
            result: List[Dict[str, Any]] = []

            if parent is None:
                count = self.tree_widget.topLevelItemCount()
                for i in range(count):
                    item = self.tree_widget.topLevelItem(i)
                    if item:
                        node: Dict[str, Any] = {"id": item.data(0, Qt.ItemDataRole.UserRole)}
                        if item.childCount() > 0:
                            node["children"] = extract_structure(item)
                        result.append(node)
            else:
                count = parent.childCount()
                for i in range(count):
                    item = parent.child(i)
                    if item:
                        node: Dict[str, Any] = {"id": item.data(0, Qt.ItemDataRole.UserRole)}
                        if item.childCount() > 0:
                            node["children"] = extract_structure(item)
                        result.append(node)

            return result

        structure = extract_structure()
        self.hierarchy_updated.emit(structure)

    def reset_all(self) -> None:
        """重置所有遮罩参数并清空遮罩列表。"""
        # 1. 停止待处理的参数更新定时器，防止对已删除的 shape 发射信号
        self._param_timer.stop()
        self._pending_params = {}
        self._pending_shape_id = None

        # 2. 阻塞信号后重置参数值
        for drag in [self.opacity_drag, self.feather_drag, self.blur_drag,
                     self.clean_black_drag, self.clean_white_drag]:
            drag.blockSignals(True)
            drag.setValue(drag.defaultValue())
            drag.blockSignals(False)

        self.btn_birefnet.setStyleSheet(self.btn_birefnet.styleSheet())
        self.btn_sam.setChecked(False)
        self.btn_brush.setChecked(False)

        # 3. 阻塞信号后清空树，避免逐个触发 shape_removed
        self.tree_widget.blockSignals(True)
        self.tree_widget.clear()
        self.tree_widget.blockSignals(False)

        # 4. 只发射一个全局清除信号，由 main_window 统一处理
        self.all_masks_cleared.emit()
        self.interaction_ended.emit()
