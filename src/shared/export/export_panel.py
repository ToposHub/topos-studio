from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                             QPushButton,
                             QLineEdit, QFileDialog, QFrame,
                             QApplication, QListWidget, QListWidgetItem, QMenu,
                             QProgressBar, QSizePolicy, QToolButton,
                             QComboBox, QSpinBox, QSlider, QCheckBox, QTabBar,
                             QTextEdit)
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import (
    C,
    apply_combo_dropdown_style,
    PROPS_TITLE_LABEL_STYLE,
    NODE_SECTION_STYLE,
    NODE_COLLAPSE_BTN_STYLE,
    NODE_CONTENT_STYLE,
    TOOLBAR_FLAT_BTN_STYLE,
)
from src.shared.ui.widgets import ChevronComboBox, DragValue, ToggleSwitch
from src.shared.ui.widgets.topos_controls import ToposClickableSlider
from src.shared.assets.icon_manager import get_icon, IconColors
from PyQt6.QtCore import Qt, pyqtSignal, QSize, QObject, QEvent
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QIcon, QPixmap, QColor
import os
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


def generate_export_filename(mode: str, original_name: str, output_ext: str,
                             index: int = 1, seq_digits: int = 3,
                             suffix: str = "", prefix: str = "",
                             custom_pattern: str = "") -> str:
    """Shared filename generation logic for export naming modes."""
    if mode == "Original Filename":
        return f"{original_name}{output_ext}"
    elif mode == "Filename + Suffix":
        s = suffix
        if "{n}" in s:
            s = s.replace("{n}", f"{index:0{seq_digits}d}")
        return f"{original_name}{s}{output_ext}"
    elif mode == "Prefix + Filename":
        p = prefix
        if "{n}" in p:
            p = p.replace("{n}", f"{index:0{seq_digits}d}")
        return f"{p}{original_name}{output_ext}"
    elif mode == "Prefix + Sequence":
        return f"{prefix}{index:0{seq_digits}d}{output_ext}"
    elif mode == "Custom Pattern":
        # AUD-33100：str.format 对非法 pattern（如单个 "{"、错误格式
        # 说明符 "{n:0dd}"）抛 ValueError，键入过程中的中间态必经；
        # textChanged 槽内未捕获异常会中止进程，故 ValueError 一并捕获
        try:
            return custom_pattern.format(name=original_name, n=index, ext=output_ext)
        except (KeyError, IndexError, ValueError):
            return f"{original_name}_{index:0{seq_digits}d}{output_ext}"
    else:
        return f"{original_name}{output_ext}"


def _create_delete_icon() -> QIcon:
    """创建一个删除图标（颜色引用全局常量 C）"""
    # efr-small-s1 删导入时移除了历史别名 QPainter as QPainter2，但函数体
    # 仍在使用——此处直接以本名导入使用。
    from PyQt6.QtGui import QPen, QBrush, QColor, QPainter

    pixmap = QPixmap(24, 24)
    pixmap.fill(QColor(0, 0, 0, 0))  # 透明背景

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)

    # 画一个圆形背景（按钮灰）
    painter.setBrush(QBrush(QColor(C.BG_BUTTON)))
    painter.drawEllipse(2, 2, 20, 20)

    # 画 X 符号（白色）
    painter.setPen(QPen(QColor("#ffffff")))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(7, 7, 17, 17)
    painter.drawLine(17, 7, 7, 17)

    painter.end()
    return QIcon(pixmap)


# 预创建的删除图标
_DELETE_ICON = None

def _get_delete_icon() -> QIcon:
    """获取删除图标（延迟创建）"""
    global _DELETE_ICON
    if _DELETE_ICON is None:
        _DELETE_ICON = _create_delete_icon()
    return _DELETE_ICON


# ================================================================
# ExportSectionWidget — 镜像 composite NodeSectionWidget 外壳（export 专用）
# ================================================================
# 与节点 section 的差异：
# - 去掉 node_id / enable_btn / color_indicator / save preset / pin（export 无概念）
# - 保留 ▼ collapse + title + reset + lock 工具栏
# - header 用固定灰色渐变（不随节点色变化）
# 与 Edit Properties 的 EditSectionWidget 同源，但 header 配色用 export 中性灰。

# 参数标签统一宽度（与节点面板一致），保证每行输入控件左缘对齐
_LABEL_MIN_WIDTH = 70
# DragValue 数值格固定宽度（与节点面板一致）
_VALUE_FIXED_WIDTH = 64


class ExportSectionWidget(QFrame):
    """可折叠 export section（外壳镜像 NodeSectionWidget，去掉节点语义）。

    Args:
        title: section 标题。
        v_expand: 内容区垂直 sizePolicy。默认 False（Maximum，只占内容高度，
            适合参数行 section）；传 True 时用 Expanding，让 section 撑满面板
            剩余垂直空间（适合内嵌 Expanding 列表的 Export Queue section）。
        header_mode: 标题栏模式。
            - ``"full"``（默认）：显示 ▼ 折叠按钮 + 标题 + 右侧重置按钮，标题栏用中性灰渐变。
              适合 Batch Export 面板（需要折叠/重置）。
            - ``"plain"``：不显示折叠/重置按钮，标题栏背景与内容区一致（无渐变），
              只作为分组的静态标题。适合 Quick Export 弹窗（无需折叠/重置）。
            - ``"none"``：完全不渲染标题栏，只有内容区。最紧凑，适合标题已由外层
              （如 TabBar / 参数行标签）暗示、无需重复分组名的场景。

    Signals:
        reset_requested: 用户点击标题栏右侧的「重置」按钮时发射（仅 header_mode="full"）。
            外层面板据此把该 section 的参数恢复默认值。
    """

    reset_requested = pyqtSignal()

    def __init__(self, title: str, v_expand: bool = False,
                 header_mode: str = "full", parent=None) -> None:
        super().__init__(parent)
        self.title = title
        self._is_collapsed = False
        self._header_mode = header_mode

        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(NODE_SECTION_STYLE)
        # section 自身的垂直策略：v_expand 时 Expanding（争取垂直空间），
        # 否则 Preferred（跟随内容，不主动撑高）。
        self.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding if v_expand else QSizePolicy.Policy.Preferred,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header bar (28px) ──（none 模式跳过，最紧凑）
        self._header = None
        self._collapse_btn = None
        self._reset_btn = None
        self._title_label = None
        if header_mode != "none":
            self._header = QWidget()
            self._header.setFixedHeight(28)
            header_layout = QHBoxLayout(self._header)
            # plain 模式标题左侧无折叠按钮，多留一点左内边距对齐内容
            header_layout.setContentsMargins(8 if header_mode == "plain" else 6, 0, 2, 0)
            header_layout.setSpacing(2)

            # Collapse toggle（仅 full 模式）
            if header_mode == "full":
                self._collapse_btn = QToolButton()
                self._collapse_btn.setText("▼")
                self._collapse_btn.setFixedSize(16, 16)
                self._collapse_btn.setStyleSheet(NODE_COLLAPSE_BTN_STYLE)
                self._collapse_btn.clicked.connect(self.toggle_collapse)
                header_layout.addWidget(self._collapse_btn)

            # Title
            self._title_label = QLabel(title)
            header_layout.addWidget(self._title_label)
            header_layout.addStretch()

            # Reset button（仅 full 模式；标题栏右侧，点击恢复该 section 默认设置）
            if header_mode == "full":
                self._reset_btn = QToolButton()
                self._reset_btn.setIcon(get_icon('fa5s.undo', IconColors.WHITE))
                self._reset_btn.setIconSize(QSize(14, 14))
                self._reset_btn.setFixedSize(24, 24)
                self._reset_btn.setStyleSheet(TOOLBAR_FLAT_BTN_STYLE)
                self._reset_btn.setToolTip(I18N.tr("export.section_reset", "Reset to defaults"))
                self._reset_btn.clicked.connect(self.reset_requested.emit)
                header_layout.addWidget(self._reset_btn)

            self._update_header_style()
            layout.addWidget(self._header)

        # ── Content area ──
        self._content_widget = QWidget()
        self._content_widget.setStyleSheet(NODE_CONTENT_STYLE)
        # 内容区垂直策略：v_expand 时 Expanding（配合内嵌的 Expanding 列表），
        # 否则 Maximum（只占内容高度，不主动撑高）。
        self._content_widget.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding if v_expand else QSizePolicy.Policy.Maximum,
        )
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(8, 6, 8, 6)
        self._content_layout.setSpacing(4)
        # v_expand 时不用 AlignTop（否则布局会把子项挤到顶部、阻止 Expanding 子项拉伸）；
        # 参数行 section 用 AlignTop 让多行紧凑顶对齐。
        if not v_expand:
            self._content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._content_widget)

    def content_layout(self) -> QVBoxLayout:
        return self._content_layout

    def _update_header_style(self) -> None:
        """标题栏配色：full 模式用中性灰渐变（文字白）；plain 模式与内容区背景一致（文字浅灰）。"""
        # none 模式无标题栏，直接返回（防御性，构造时也不会调用）
        if self._header is None:
            return
        if self._header_mode == "plain":
            # plain：背景与内容区一致（NODE_CONTENT_STYLE 的 #27272b），无渐变、无分隔线
            self._header.setStyleSheet("background-color: #27272b; border: none;")
            self._title_label.setStyleSheet(
                "color: #8E929A; font-weight: bold; font-size: 11px; border: none;"
            )
            return
        # full：中性灰渐变 + 白字
        base = QColor(C.BG_BUTTON)
        top_color = base.lighter(130)
        self._header.setStyleSheet(
            f"background-color: qlineargradient("
            f"x1:0, y1:0, x2:0, y2:1, stop:0 {top_color.name()}, stop:1 {base.name()});"
            f" border-bottom: 1px solid rgba(0,0,0,60);"
        )
        self._title_label.setStyleSheet(
            "color: #FFFFFF; font-weight: bold; font-size: 11px; border: none;"
        )
        if self._collapse_btn is not None:
            self._collapse_btn.setStyleSheet(
                "QToolButton { background: transparent; border: none;"
                " color: #FFFFFF; font-size: 9px; padding: 0; }"
                "QToolButton:hover { color: #FFFFFF; }"
            )

    def toggle_collapse(self) -> None:
        self._is_collapsed = not self._is_collapsed
        self._content_widget.setVisible(not self._is_collapsed)
        if self._collapse_btn is not None:
            self._collapse_btn.setText("▶" if self._is_collapsed else "▼")


def _make_param_label(text: str) -> QLabel:
    """统一的参数标签（与节点面板 _ParamLabel 视觉一致）。"""
    label = QLabel(text)
    label.setStyleSheet("color: #bbb; font-size: 10px;")
    label.setMinimumWidth(_LABEL_MIN_WIDTH)
    label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
    return label


def _apply_text_input_style(line_edit: QLineEdit | QTextEdit) -> None:
    """为文本输入框（路径/prefix/suffix/custom pattern）套上与 ChevronComboBox
    选择框一致的背景与边框，让文本框与下拉框视觉统一。

    ChevronComboBox 用 #232325 背景 + #151517 边框（见 chevron_combo.py），
    这里复用同一套配色，hover/focus 也对齐选择框的反馈。

    AUD-21503: 选择器同时匹配 QLineEdit 与 QTextEdit——本 helper 也被
    TTS 设置对话框的多行输入（QTextEdit）复用；单一 ``QLineEdit`` 类型
    选择器对 QTextEdit（非其子类）是静默 no-op，导致多行输入框完全
    丢失统一样式。
    """
    line_edit.setStyleSheet("""
        QLineEdit, QTextEdit {
            background-color: #232325;
            border: 1px solid #151517;
            border-radius: 6px;
            padding: 2px 8px;
            color: #ffffff;
            font-size: 12px;
            selection-background-color: #ff8800;
        }
        QLineEdit:hover, QTextEdit:hover { background-color: #2e2e34; }
        QLineEdit:focus, QTextEdit:focus { border: 1px solid #ff8800; }
    """)


def _apply_compact_button_style(btn: QPushButton) -> None:
    """为紧凑型操作按钮（Choose/Browse/...）套上与输入框/下拉框统一的视觉。

    与 _apply_text_input_style 同源配色（#232325 背景 + #151517 边框 + 6px 圆角），
    让按钮与旁边的输入框看起来是一组；hover/focus 切主题橙，给出明确交互反馈。
    按钮文字加粗、cursor 指针手型，强调"可点击"。
    """
    btn.setStyleSheet(f"""
        QPushButton {{
            background-color: #232325;
            border: 1px solid #151517;
            border-radius: 6px;
            padding: 4px 14px;
            color: {C.TEXT_BRIGHT};
            font-size: 11px;
            font-weight: bold;
        }}
        QPushButton:hover {{
            background-color: #2e2e34;
            border: 1px solid {C.ORANGE};
            color: #ffffff;
        }}
        QPushButton:pressed {{
            background-color: {C.ORANGE_PRESS};
            border: 1px solid {C.ORANGE_PRESS};
            color: #ffffff;
        }}
        QPushButton:disabled {{
            background-color: {C.BG_DARK};
            border: 1px solid {C.BORDER_DARK};
            color: {C.TEXT_HINT};
        }}
    """)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)


class _ClickDefocusFilter(QObject):
    """事件过滤器：点击目标 widget 的空白区域（非输入/下拉等可聚焦控件）时，
    让当前获得焦点的 QLineEdit 提交并失焦，退出"选中"状态。

    Qt 默认点击空白不会让 QLineEdit 失焦，导致输入框一直保持 focus 边框。
    安装到容器 widget（面板/对话框）上即可生效：点击落到非交互控件时清焦。
    """

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            # 被点击的 widget（obj）如果本身是可交互控件（输入框/按钮/下拉/滑块/spin 等），不拦截
            focus_types = (
                QLineEdit, QComboBox, QSpinBox, QPushButton, QSlider,
                QToolButton, QCheckBox, QListWidget, QTabBar,
            )
            if isinstance(obj, focus_types):
                return False
            # 否则：把焦点转走，让正在编辑的 QLineEdit 自然失焦并提交输入
            focused = QApplication.focusWidget()
            if isinstance(focused, QLineEdit):
                focused.clearFocus()
        return False


def install_click_defocus(widget: QWidget) -> None:
    """为容器 widget 安装点击失焦过滤器（面板/对话框共用）。

    覆盖 widget 自身 + 所有子 widget 的事件，点击空白即让输入框失焦。
    """
    filt = _ClickDefocusFilter(widget)
    widget.installEventFilter(filt)
    for child in widget.findChildren(QWidget):
        child.installEventFilter(filt)
    # 持有引用，防止被 GC（parent=widget 已保证，这里冗余保险）
    widget._defocus_filter = filt


class FileListItemWidget(QWidget):
    """File list item widget with filename, status indicator and delete button"""

    delete_clicked = pyqtSignal(int)  # Emits delete signal with row index

    # 状态 → 指示点颜色（与 deliver_controller 的状态常量对应，引用全局 C 常量）
    STATUS_COLORS = {
        "pending": C.TEXT_HINT,      # 灰
        "rendering": C.ORANGE,       # 橙（主题色）
        "done": C.CHANNEL_G,         # 绿
        "failed": C.CHANNEL_R,       # 红
    }

    def __init__(self, file_path: str, row_index: int, status: str = "pending", parent=None) -> None:
        super().__init__(parent)
        self._file_path = file_path
        self._row_index = row_index
        self._status = status

        # Main horizontal layout
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        # 状态指示点（最左侧，16px 圆点）
        self.status_label = QLabel("●")
        self.status_label.setFixedWidth(16)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._apply_status_color()
        layout.addWidget(self.status_label)

        # Filename label - occupies all available space
        self.name_label = QLabel(os.path.basename(file_path))
        self.name_label.setStyleSheet(f"""
            color: {C.TEXT_SECONDARY};
            background: transparent;
            padding: 0px;
        """)
        self.name_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.name_label.setToolTip(file_path)
        layout.addWidget(self.name_label)

        # Delete button - uses icon
        self.delete_btn = QPushButton()
        self.delete_btn.setFixedSize(24, 24)
        self.delete_btn.setIcon(_get_delete_icon())
        self.delete_btn.setIconSize(QSize(20, 20))
        self.delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                padding: 2px;
            }
            QPushButton:hover {
                background-color: rgba(255, 68, 68, 0.2);
                border-radius: 4px;
            }
            QPushButton:pressed {
                background-color: rgba(255, 68, 68, 0.4);
                border-radius: 4px;
            }
        """)
        self.delete_btn.clicked.connect(lambda: self.delete_clicked.emit(self._row_index))
        layout.addWidget(self.delete_btn)

        # Set fixed height
        self.setFixedHeight(28)

    def _apply_status_color(self) -> None:
        color = self.STATUS_COLORS.get(self._status, self.STATUS_COLORS["pending"])
        self.status_label.setStyleSheet(f"color: {color}; background: transparent; padding: 0px;")

    def set_status(self, status: str) -> None:
        """更新该项的状态指示（颜色点）。"""
        self._status = status
        self._apply_status_color()

    def update_row_index(self, new_index: int):
        """Update row index (called when list order changes)"""
        self._row_index = new_index

    def get_file_path(self) -> str:
        """Get file path"""
        return self._file_path

class BatchExportListWidget(QListWidget):
    """Drag-and-drop batch export list widget"""

    files_changed = pyqtSignal()  # File list changed signal
    AUTO_ROLE = Qt.ItemDataRole.UserRole + 1  # Mark if item is auto-added

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.DropOnly)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            added = False
            for url in event.mimeData().urls():
                file_path = url.toLocalFile()
                if os.path.isfile(file_path):
                    self.add_file(file_path)
                    added = True
            if added:
                self.files_changed.emit()

    def add_file(self, file_path: str, is_auto: bool = False, status: str = "pending") -> bool:
        """Add file to list, returns True if successfully added"""
        # Check if already exists
        for i in range(self.count()):
            if self.item(i).data(Qt.ItemDataRole.UserRole) == file_path:
                return False

        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, file_path)
        item.setData(self.AUTO_ROLE, is_auto)
        item.setToolTip(file_path)
        item.setSizeHint(QSize(item.sizeHint().width(), 28))  # Fixed row height
        self.addItem(item)

        # Create and set custom widget
        row_index = self.count() - 1
        item_widget = FileListItemWidget(file_path, row_index, status=status)
        item_widget.delete_clicked.connect(self._on_item_delete_clicked)
        self.setItemWidget(item, item_widget)

        return True

    def set_auto_file(self, file_path: str):
        """Set auto-tracked current file. Replaces old auto item, does not affect manually dragged items."""
        # Remove old auto item
        for i in range(self.count() - 1, -1, -1):
            if self.item(i).data(self.AUTO_ROLE):
                self.takeItem(i)
        # If file already exists as manual item, don't add duplicate
        for i in range(self.count()):
            if self.item(i).data(Qt.ItemDataRole.UserRole) == file_path:
                return
        # Add as auto item (insert at list beginning)
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, file_path)
        item.setData(self.AUTO_ROLE, True)
        item.setToolTip(file_path)
        item.setSizeHint(QSize(item.sizeHint().width(), 28))  # Fixed row height
        self.insertItem(0, item)

        # Create and set custom widget
        item_widget = FileListItemWidget(file_path, 0)
        item_widget.delete_clicked.connect(self._on_item_delete_clicked)
        self.setItemWidget(item, item_widget)

        self._refresh_all_row_indices()
        self.files_changed.emit()

    def _on_item_delete_clicked(self, row_index: int):
        """Handle delete button click in item widget"""
        if 0 <= row_index < self.count():
            self.takeItem(row_index)
            self._refresh_all_row_indices()
            self.files_changed.emit()

    def _refresh_all_row_indices(self):
        """Refresh all item widget row indices"""
        for i in range(self.count()):
            item = self.item(i)
            widget = self.itemWidget(item)
            if widget and isinstance(widget, FileListItemWidget):
                widget.update_row_index(i)

    def remove_selected(self):
        """Remove selected items"""
        for item in self.selectedItems():
            self.takeItem(self.row(item))
        self._refresh_all_row_indices()
        self.files_changed.emit()

    def clear_all(self):
        """Clear all items"""
        self.clear()
        self.files_changed.emit()

    def set_item_status(self, index: int, status: str) -> None:
        """更新指定行项的状态指示（颜色点）。供 controller 在渲染过程中回调。"""
        if 0 <= index < self.count():
            item = self.item(index)
            widget = self.itemWidget(item)
            if widget and isinstance(widget, FileListItemWidget):
                widget.set_status(status)

    def restore_items(self, items: list[dict]) -> None:
        """从持久化的队列数据重建列表（带状态标记）。

        供启动恢复时调用：清空当前列表后，按 items 重建，
        每项带其上次的 status（pending/rendering/done/failed）。
        """
        self.clear()
        for it in items:
            file_path = it.get("file_path", "")
            status = it.get("status", "pending")
            if not file_path:
                continue
            # 直接走 add_file 的去重/创建逻辑，带 status
            self.add_file(file_path, is_auto=False, status=status)
        self.files_changed.emit()

    def get_all_files(self) -> List[str]:
        files = []
        for i in range(self.count()):
            files.append(self.item(i).data(Qt.ItemDataRole.UserRole))
        return files


class ExportPanel(QWidget):
    """Batch Export Panel"""

    # Signal emitted when user clicks "Start Export" (single file or current state)
    export_requested = pyqtSignal(dict)
    # Signal emitted when user requests batch export (multiple files)
    batch_export_requested = pyqtSignal(dict)
    # Signal emitted when user cancels batch export
    batch_cancel_requested = pyqtSignal()
    # Signal emitted when user pauses batch export
    batch_pause_requested = pyqtSignal()
    # Signal emitted when user resumes (continues) a paused batch export
    batch_resume_requested = pyqtSignal()

    # AUD-33101：命名模式"i18n key → 内部模式名"单一映射。
    # 内部名是 generate_export_filename 与 deliver_controller 的逻辑键
    # （与 locale 无关）；combo 的 itemData 直接存内部名。
    _NAMING_MODE_KEY_TO_ID = {
        "export.mode_filename_suffix": "Filename + Suffix",
        "export.mode_prefix_filename": "Prefix + Filename",
        "export.mode_prefix_sequence": "Prefix + Sequence",
        "export.mode_original": "Original Filename",
        "export.mode_custom": "Custom Pattern",
    }
    #: 内部模式名全集（currentData 兜底校验用）。
    _NAMING_MODE_IDS = frozenset(_NAMING_MODE_KEY_TO_ID.values())

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.default_dir = ""
        self.default_name = "untitled"
        self.orig_w, self.orig_h = 0, 0
        self._current_file_path: Optional[str] = None  # Currently opened file path
        # AUD-33103：缩放结果尺寸以成员属性为数据源（标签文本仅展示）。
        # None = 当前无可用的缩放输出尺寸（原图尺寸无效/未勾选缩放）。
        self._last_output_size: Optional[tuple] = None

        self.setup_ui()
        # 全局 DARK_THEME 已统一覆盖 QLineEdit/QComboBox/QSpinBox/QSlider 等基础控件。
        # 面板内每个分组用 ExportSectionWidget（镜像 NodeSectionWidget 外壳），
        # 控件改用 DragValue/ChevronComboBox/ToposClickableSlider/ToggleSwitch，
        # 与 transform/blur 节点参数面板视觉与交互一致。
        # 此处只保留导出主按钮 #PrimaryButton 的橙色样式（其余控件走全局主题）。
        self.setStyleSheet(f"""
            ExportPanel {{
                background-color: {C.BG_PANEL};
                color: {C.TEXT_PRIMARY};
                font-size: 11px;
            }}
            #PrimaryButton {{
                background-color: {C.ORANGE_PRESS};
                color: white;
                font-size: 14px;
                font-weight: bold;
                border: none;
                border-radius: 3px;
            }}
            #PrimaryButton:hover {{ background-color: {C.ORANGE_HOVER}; color: white; }}
            #PrimaryButton:pressed {{ background-color: {C.ORANGE}; }}
            #PrimaryButton:disabled {{ background-color: {C.BORDER_DARK}; color: {C.TEXT_HINT}; }}
        """)
        self.on_format_changed(self.format_combo.currentText())
        self.update_resize_ui()
        # 点击面板空白区域时让输入框失焦（退出选中态）
        install_click_defocus(self)

    def set_original_size(self, w: int, h: int):
        """Set original image size"""
        self.orig_w, self.orig_h = w, h
        self.update_resize_ui()

    def set_default_path(self, path: str):
        """Set default export path"""
        if path:
            self.default_dir = os.path.dirname(path)
            self.default_name = os.path.splitext(os.path.basename(path))[0]
            self.path_input.setText(self.default_dir)
            # Update naming preview
            self.update_naming_preview()

    # ================================================================
    # P9.3 - Deliver 页面导出来源设置
    # ================================================================

    def set_export_source(self, source: str) -> None:
        """
        设置导出来源类型。

        Args:
            source: 导出来源（color, composite, timeline）
        """
        self._export_source = source
        # 更新 UI 显示
        source_label = self.__dict__.get("_source_label")
        if source_label is not None:
            source_names = {
                "color": I18N.tr("export.source_color", "Color Output"),
                "composite": I18N.tr("export.source_composite", "Composite Output"),
                "timeline": I18N.tr("export.source_timeline", "Timeline"),
            }
            source_label.setText(source_names.get(source, source))
        logger.debug(f"Export source set to: {source}")

    def set_timeline_id(self, timeline_id: str) -> None:
        """
        设置导出的时间线 ID。

        Args:
            timeline_id: 时间线 ID
        """
        self._timeline_id = timeline_id
        # 更新 UI 显示时间线信息
        timeline_label = self.__dict__.get("_timeline_label")
        if timeline_label is not None:
            timeline_label.setText(
                I18N.tr(
                    "export.timeline_label", "Timeline: {timeline_id}"
                ).format(timeline_id=timeline_id)
            )
        logger.debug(f"Export timeline set to: {timeline_id}")

    def get_export_source(self) -> str:
        """获取当前导出来源类型。"""
        return getattr(self, '_export_source', 'color')

    def get_timeline_id(self) -> Optional[str]:
        """获取当前时间线 ID。"""
        return getattr(self, '_timeline_id', None)

    def add_current_file(self, file_path: str):
        """
        Add currently opened file to export list (auto-tracking).

        Behavior:
        - Always updates default export path and filename
        - Automatically replaces previous auto-tracked file, does not affect manually dragged files

        Args:
            file_path: Currently opened image file path
        """
        self._current_file_path = file_path

        # Always update default export path and filename
        self.set_default_path(file_path)

        # Set auto-tracking item (replace old one, does not affect manual items)
        self.batch_list.set_auto_file(file_path)
        self._update_export_button()

    def setup_ui(self):
        # Main layout with compact margins
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(16, 16, 16, 16)
        self.main_layout.setSpacing(10)

        # Title (与 Properties 头部一致的暗灰标题)
        title = QLabel(I18N.tr("export.batch_title"))
        title.setStyleSheet(PROPS_TITLE_LABEL_STYLE)
        self.main_layout.addWidget(title)

        # ──────────────────────────────────────────────────────────────
        # 1. Export Queue section（文件列表，不折叠，列表为主）
        # ──────────────────────────────────────────────────────────────
        queue_section = ExportSectionWidget(I18N.tr("export.queue_label"), v_expand=True)
        # Queue 是文件列表，没有"默认设置"可重置 → 隐藏标题栏的重置按钮
        queue_section._reset_btn.hide()
        queue_layout = queue_section.content_layout()

        # 文件计数（右侧靠齐，放在 title 同行的下方一行）
        self.file_count_label = QLabel(I18N.tr("export.file_count", "0 个文件"))
        self.file_count_label.setStyleSheet(f"color: {C.TEXT_HINT}; font-size: 10px;")
        queue_layout.addWidget(self.file_count_label)

        self.batch_list = BatchExportListWidget()
        # Use Expanding size policy to let list box fill remaining vertical space
        size_policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.batch_list.setSizePolicy(size_policy)
        self.batch_list.setStyleSheet(f"""
            QListWidget {{
                background-color: {C.BG_DARK};
                border: 1px dashed {C.BORDER};
                border-radius: 4px;
                color: {C.TEXT_SECONDARY};
            }}
            QListWidget::item {{
                height: 28px;
                background-color: transparent;
                border-bottom: 1px solid {C.BG_HOVER};
            }}
            QListWidget::item:selected {{
                background-color: {C.ORANGE};
                color: white;
            }}
            QListWidget::item:hover {{
                background-color: {C.BG_HOVER};
            }}
        """)
        self.batch_list.customContextMenuRequested.connect(self._show_list_context_menu)
        self.batch_list.files_changed.connect(self._on_files_changed)
        queue_layout.addWidget(self.batch_list)

        # Hint text - fixed height
        hint_label = QLabel(I18N.tr("export.drag_drop_hint"))
        hint_label.setFixedHeight(24)
        hint_label.setStyleSheet(f"color: {C.TEXT_HINT}; font-size: 10px; font-style: italic;")
        queue_layout.addWidget(hint_label)

        self.main_layout.addWidget(queue_section, stretch=1)

        # ──────────────────────────────────────────────────────────────
        # 2. Output section（输出路径）
        # ──────────────────────────────────────────────────────────────
        output_section = ExportSectionWidget(I18N.tr("export.output_section", "Output"))
        output_section.reset_requested.connect(self._reset_output_section)
        output_layout = output_section.content_layout()

        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 2, 0, 2)
        path_row.setSpacing(4)
        path_label = _make_param_label(I18N.tr("export.output_location"))
        path_row.addWidget(path_label)
        self.path_input = QLineEdit(self.default_dir)
        self.path_input.setPlaceholderText(I18N.tr("export.select_dir_placeholder"))
        self.path_input.setSizePolicy(QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed))
        self.path_input.setMinimumWidth(120)
        _apply_text_input_style(self.path_input)
        path_row.addWidget(self.path_input, 1)
        self.path_btn = QPushButton(I18N.tr("export.browse_btn"))
        self.path_btn.clicked.connect(self._pick_directory)
        _apply_compact_button_style(self.path_btn)
        path_row.addWidget(self.path_btn)
        output_layout.addLayout(path_row)

        # Composite 应用选项（ToggleSwitch 药丸开关，默认开启）
        composite_row = QHBoxLayout()
        composite_row.setContentsMargins(0, 2, 0, 2)
        composite_row.setSpacing(4)
        composite_label = _make_param_label(I18N.tr("export.apply_composite", "应用 Composite 合成"))
        composite_row.addWidget(composite_label)
        composite_row.addStretch()
        # 用 ToggleSwitch 替代 QCheckBox，但保留 apply_composite_check 名称以维持契约
        self.apply_composite_check = ToggleSwitch(checked=True)
        self.apply_composite_check.setToolTip(
            I18N.tr(
                "export.apply_composite_tip",
                "对每个文件应用其 .topos 项目中保存的合成效果（无合成则自动用调色输出）",
            )
        )
        composite_row.addWidget(self.apply_composite_check)
        output_layout.addLayout(composite_row)

        self.main_layout.addWidget(output_section)

        # ──────────────────────────────────────────────────────────────
        # 3. Naming section（文件命名）
        # ──────────────────────────────────────────────────────────────
        naming_section = ExportSectionWidget(I18N.tr("export.naming_section", "Naming"))
        naming_section.reset_requested.connect(self._reset_naming_section)
        naming_layout = naming_section.content_layout()

        # Naming mode (ChevronComboBox)
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 2, 0, 2)
        mode_row.setSpacing(4)
        mode_label = _make_param_label(I18N.tr("export.naming_mode"))
        mode_row.addWidget(mode_label)
        self.naming_mode_combo = ChevronComboBox()
        # AUD-33101：itemData 存内部模式名（generate_export_filename 与
        # deliver_controller 消费的英文键），显示文本仅做展示——此前以
        # currentText 与硬编码英文比较，非英文 locale 下命名功能整体失效。
        for mode_key, internal in ExportPanel._NAMING_MODE_KEY_TO_ID.items():
            self.naming_mode_combo.addItem(I18N.tr(mode_key), userData=internal)
        apply_combo_dropdown_style(self.naming_mode_combo)
        self.naming_mode_combo.currentTextChanged.connect(self._on_naming_mode_changed)
        mode_row.addWidget(self.naming_mode_combo, 1)
        naming_layout.addLayout(mode_row)

        # Prefix row (visible only for prefix modes)
        self.prefix_widget = QWidget()
        prefix_row = QHBoxLayout(self.prefix_widget)
        prefix_row.setContentsMargins(0, 2, 0, 2)
        prefix_row.setSpacing(4)
        self.prefix_label = _make_param_label(I18N.tr("export.prefix"))
        prefix_row.addWidget(self.prefix_label)
        self.prefix_input = QLineEdit("img_")
        self.prefix_input.setPlaceholderText(I18N.tr("export.prefix_placeholder"))
        self.prefix_input.textChanged.connect(self.update_naming_preview)
        _apply_text_input_style(self.prefix_input)
        prefix_row.addWidget(self.prefix_input, 1)
        naming_layout.addWidget(self.prefix_widget)

        # Suffix row (visible only for suffix mode)
        self.suffix_widget = QWidget()
        suffix_row = QHBoxLayout(self.suffix_widget)
        suffix_row.setContentsMargins(0, 2, 0, 2)
        suffix_row.setSpacing(4)
        self.suffix_label = _make_param_label(I18N.tr("export.suffix"))
        suffix_row.addWidget(self.suffix_label)
        self.suffix_input = QLineEdit("_exported")
        self.suffix_input.setPlaceholderText(I18N.tr("export.suffix_placeholder"))
        self.suffix_input.textChanged.connect(self.update_naming_preview)
        _apply_text_input_style(self.suffix_input)
        suffix_row.addWidget(self.suffix_input, 1)
        naming_layout.addWidget(self.suffix_widget)

        # Sequence digits row (DragValue, visible only for sequence mode)
        # seq_widget 包裹整行以便 show/hide；seq_digits_spin 改名指向 DragValue 维持契约
        self.seq_widget = QWidget()
        seq_layout = QHBoxLayout(self.seq_widget)
        seq_layout.setContentsMargins(0, 2, 0, 2)
        seq_layout.setSpacing(4)
        self.seq_label = _make_param_label(I18N.tr("export.sequence_digits"))
        seq_layout.addWidget(self.seq_label)
        # DragValue 替代 QSpinBox：2-8 整数，suffix " digits"，decimals=0
        self.seq_digits_spin = DragValue("", min_v=2, max_v=8, default=3, decimals=0, suffix=" digits")
        self.seq_digits_spin.setFixedValueWidth(_VALUE_FIXED_WIDTH)
        self.seq_digits_spin.valueChanged.connect(self.update_naming_preview)
        seq_layout.addWidget(self.seq_digits_spin)
        seq_layout.addStretch()
        naming_layout.addWidget(self.seq_widget)

        # Custom pattern row (visible only for custom mode)
        self.custom_widget = QWidget()
        custom_row = QHBoxLayout(self.custom_widget)
        custom_row.setContentsMargins(0, 2, 0, 2)
        custom_row.setSpacing(4)
        custom_label = _make_param_label(I18N.tr("export.custom_pattern", "Pattern"))
        custom_row.addWidget(custom_label)
        self.custom_pattern_input = QLineEdit("{name}_{n:03d}")
        self.custom_pattern_input.setPlaceholderText(I18N.tr("export.custom_pattern_placeholder"))
        self.custom_pattern_input.textChanged.connect(self.update_naming_preview)
        _apply_text_input_style(self.custom_pattern_input)
        custom_row.addWidget(self.custom_pattern_input, 1)
        naming_layout.addWidget(self.custom_widget)

        # Naming preview
        self.naming_preview_label = QLabel(I18N.tr("export.preview") + ": image_exported.jpg")
        self.naming_preview_label.setStyleSheet(f"color: {C.TEXT_LABEL}; font-size: 10px;")
        naming_layout.addWidget(self.naming_preview_label)

        self.main_layout.addWidget(naming_section)

        # ──────────────────────────────────────────────────────────────
        # 4. Resize section（缩放设置）
        # ──────────────────────────────────────────────────────────────
        resize_section = ExportSectionWidget(I18N.tr("export.resize"))
        resize_section.reset_requested.connect(self._reset_resize_section)
        resize_layout = resize_section.content_layout()

        # Enable toggle (ToggleSwitch 替代 QCheckBox；resize_check 名字维持契约)
        enable_row = QHBoxLayout()
        enable_row.setContentsMargins(0, 2, 0, 2)
        enable_row.setSpacing(4)
        enable_label = _make_param_label(I18N.tr("export.resize"))
        enable_row.addWidget(enable_label)
        enable_row.addStretch()
        self.resize_check = ToggleSwitch(checked=False)
        # ToggleSwitch 用 toggled 信号（与 QCheckBox.toggled 同名同签名），维持契约
        self.resize_check.toggled.connect(self.update_resize_ui)
        enable_row.addWidget(self.resize_check)
        resize_layout.addLayout(enable_row)

        # resize_widget 包裹所有控件，enable=False 时整体禁用
        self.resize_widget = QWidget()
        resize_controls = QVBoxLayout(self.resize_widget)
        resize_controls.setContentsMargins(0, 0, 0, 0)
        resize_controls.setSpacing(4)

        # Resize mode row (ChevronComboBox)
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 2, 0, 2)
        mode_row.setSpacing(4)
        mode_param_label = _make_param_label(I18N.tr("export.resize_mode_label", "Mode"))
        mode_row.addWidget(mode_param_label)
        self.resize_mode = ChevronComboBox()
        self.resize_mode.addItems([
            I18N.tr("export.resize_long_edge"),
            I18N.tr("export.resize_short_edge"),
            I18N.tr("export.resize_width"),
            I18N.tr("export.resize_height"),
            I18N.tr("export.resize_percent")
        ])
        apply_combo_dropdown_style(self.resize_mode)
        self.resize_mode.currentTextChanged.connect(self.update_output_size)
        mode_row.addWidget(self.resize_mode, 1)
        resize_controls.addLayout(mode_row)

        # Resize value row (DragValue + ToposClickableSlider，仿节点数值行)
        value_row = QHBoxLayout()
        value_row.setContentsMargins(0, 2, 0, 2)
        value_row.setSpacing(4)
        value_param_label = _make_param_label(I18N.tr("export.resize_value_label", "Value"))
        value_row.addWidget(value_param_label)
        # resize_value 改名指向 DragValue（维持属性名契约；value() 返回 float，读取处强转 int）
        self.resize_value = DragValue("", min_v=1, max_v=10000, default=2048, decimals=0, suffix=" px")
        self.resize_value.setFixedValueWidth(_VALUE_FIXED_WIDTH)
        self.resize_value.valueChanged.connect(self.update_output_size)
        value_row.addWidget(self.resize_value)
        value_row.addStretch()
        resize_controls.addLayout(value_row)

        # Output size label
        self.output_size_label = QLabel(I18N.tr("export.result") + f": {self.orig_w} x {self.orig_h}")
        self.output_size_label.setStyleSheet(f"color: {C.TEXT_BRIGHT}; font-weight: bold; font-size: 10px;")
        resize_controls.addWidget(self.output_size_label)

        resize_layout.addWidget(self.resize_widget)
        self.main_layout.addWidget(resize_section)

        # 4b. Composite 应用选项已合并进 Output section（上方 apply_composite_check）

        # ──────────────────────────────────────────────────────────────
        # 5. Format section（格式与质量）
        # ──────────────────────────────────────────────────────────────
        format_section = ExportSectionWidget(I18N.tr("export.format"))
        format_section.reset_requested.connect(self._reset_format_section)
        # options_layout 直接复用 section 的 content_layout（on_format_changed 用它切换 EXR 显隐）
        self.options_layout = format_section.content_layout()

        # Format row (ChevronComboBox)
        format_row = QHBoxLayout()
        format_row.setContentsMargins(0, 2, 0, 2)
        format_row.setSpacing(4)
        format_param_label = _make_param_label(I18N.tr("export.format"))
        format_row.addWidget(format_param_label)
        self.format_combo = ChevronComboBox()
        self.format_combo.addItems([I18N.tr("export.fmt_jpeg"), I18N.tr("export.fmt_png"), I18N.tr("export.fmt_exr"), I18N.tr("export.fmt_tiff"), I18N.tr("export.fmt_webp")])
        apply_combo_dropdown_style(self.format_combo)
        self.format_combo.currentTextChanged.connect(self.on_format_changed)
        format_row.addWidget(self.format_combo, 1)
        self.options_layout.addLayout(format_row)

        # Quality row (ToposClickableSlider + DragValue，仿节点数值行)
        # quality_slider 改名指向 ToposClickableSlider（维持属性名契约）
        quality_row = QHBoxLayout()
        quality_row.setContentsMargins(0, 2, 0, 2)
        quality_row.setSpacing(4)
        quality_param_label = _make_param_label(I18N.tr("export.quality"))
        quality_row.addWidget(quality_param_label)
        self.quality_slider = ToposClickableSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(0, 100)
        self.quality_slider.setValue(85)
        quality_row.addWidget(self.quality_slider, 1)
        # quality_val_label 改用 DragValue（与节点面板数值格一致）
        self.quality_val_label = DragValue("", min_v=0, max_v=100, default=85, decimals=0, suffix=" %")
        self.quality_val_label.setFixedValueWidth(_VALUE_FIXED_WIDTH)
        # slider → drag 同步（blockSignals 防递归）
        self.quality_slider.valueChanged.connect(
            lambda v: (self.quality_val_label.blockSignals(True),
                       self.quality_val_label.setValue(float(v)),
                       self.quality_val_label.blockSignals(False))
        )
        # drag → slider 同步
        self.quality_val_label.valueChanged.connect(
            lambda v: (self.quality_slider.blockSignals(True),
                       self.quality_slider.setValue(int(round(v))),
                       self.quality_slider.blockSignals(False))
        )
        quality_row.addWidget(self.quality_val_label)
        self.options_layout.addLayout(quality_row)

        # EXR Specific Params (widget 包裹，按格式显隐)
        self.exr_widget = QWidget()
        exr_layout = QVBoxLayout(self.exr_widget)
        exr_layout.setContentsMargins(0, 0, 0, 0)
        exr_layout.setSpacing(4)

        # EXR precision row
        exr_type_row = QHBoxLayout()
        exr_type_row.setContentsMargins(0, 2, 0, 2)
        exr_type_row.setSpacing(4)
        exr_type_label = _make_param_label(I18N.tr("export.precision"))
        exr_type_row.addWidget(exr_type_label)
        self.exr_type_combo = ChevronComboBox()
        self.exr_type_combo.addItems([I18N.tr("export.exr_half"), I18N.tr("export.exr_full")])
        apply_combo_dropdown_style(self.exr_type_combo)
        exr_type_row.addWidget(self.exr_type_combo, 1)
        exr_layout.addLayout(exr_type_row)

        # EXR compression row
        exr_comp_row = QHBoxLayout()
        exr_comp_row.setContentsMargins(0, 2, 0, 2)
        exr_comp_row.setSpacing(4)
        exr_comp_label = _make_param_label(I18N.tr("export.compression"))
        exr_comp_row.addWidget(exr_comp_label)
        self.exr_comp_combo = ChevronComboBox()
        self.exr_comp_combo.addItems([
            I18N.tr("export.exr_comp_none"),
            I18N.tr("export.exr_comp_rle"),
            I18N.tr("export.exr_comp_zips"),
            I18N.tr("export.exr_comp_zip"),
            I18N.tr("export.exr_comp_piz"),
            I18N.tr("export.exr_comp_pxr24"),
            I18N.tr("export.exr_comp_b44"),
            I18N.tr("export.exr_comp_b44a"),
            I18N.tr("export.exr_comp_dwaa"),
            I18N.tr("export.exr_comp_dwab")
        ])
        self.exr_comp_combo.setCurrentIndex(3)
        apply_combo_dropdown_style(self.exr_comp_combo)
        exr_comp_row.addWidget(self.exr_comp_combo, 1)
        exr_layout.addLayout(exr_comp_row)

        self.options_layout.addWidget(self.exr_widget)

        self.main_layout.addWidget(format_section)

        # 6. Progress Bar
        self.batch_progress = QProgressBar()
        self.batch_progress.setMaximumHeight(12)
        self.batch_progress.setTextVisible(False)
        self.batch_progress.setStyleSheet(f"""
            QProgressBar {{
                border: 1px solid {C.BORDER};
                border-radius: 6px;
                background-color: {C.BG_DARKEST};
            }}
            QProgressBar::chunk {{
                background-color: {C.ORANGE};
                border-radius: 5px;
            }}
        """)
        self.batch_progress.hide()
        self.main_layout.addWidget(self.batch_progress)

        # Cancel button (hidden by default, shown during batch export)
        self.cancel_btn = QPushButton(I18N.tr("export.cancel_export", "Cancel Export"))
        self.cancel_btn.setMinimumHeight(36)
        self.cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #cc3333;
                color: white;
                font-size: 13px;
                font-weight: bold;
                border-radius: 4px;
                border: none;
            }}
            QPushButton:hover {{
                background-color: {C.CHANNEL_R};
            }}
            QPushButton:pressed {{
                background-color: #aa2222;
            }}
        """)
        self.cancel_btn.clicked.connect(lambda: self.batch_cancel_requested.emit())
        self.cancel_btn.hide()
        self.main_layout.addWidget(self.cancel_btn)

        # Pause/Resume button (hidden by default, shown during batch export)
        # 主题橙家族（与取消按钮的红色区分）；暂停/继续共用一个按钮，文字切换
        self.pause_btn = QPushButton(I18N.tr("export.pause", "暂停"))
        self.pause_btn.setMinimumHeight(36)
        self.pause_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {C.ORANGE_PRESS};
                color: white;
                font-size: 13px;
                font-weight: bold;
                border-radius: 4px;
                border: none;
            }}
            QPushButton:hover {{
                background-color: {C.ORANGE_HOVER};
            }}
            QPushButton:pressed {{
                background-color: {C.ORANGE};
            }}
        """)
        self.pause_btn.clicked.connect(self._on_pause_btn_clicked)
        self.pause_btn.hide()
        self.main_layout.addWidget(self.pause_btn)

        # 7. Export Button
        self.export_btn = QPushButton(I18N.tr("export.start_export"))
        self.export_btn.setObjectName("PrimaryButton")
        self.export_btn.setMinimumHeight(40)
        self.export_btn.clicked.connect(self._start_export)
        self.main_layout.addWidget(self.export_btn)

        # Initialize UI state
        self._on_naming_mode_changed(self.naming_mode_combo.currentText())
        self.update_naming_preview()
        self._update_export_button()

    # ================================================================
    # Section 重置处理（点击标题栏右侧 ↺ 按钮恢复默认）
    # ================================================================

    def _reset_output_section(self) -> None:
        """Output section 重置：路径恢复默认目录、Composite 开关恢复默认勾选。"""
        # path_input 恢复到面板初始化时的默认目录（可能是空串）
        self.path_input.setText(self.default_dir)
        # apply_composite 默认开启（ToggleSwitch.setChecked 不发 toggled，但默认值就是 True）
        self.apply_composite_check.setChecked(True)

    def _reset_naming_section(self) -> None:
        """Naming section 重置：模式/prefix/suffix/seq_digits/custom 恢复默认。"""
        # blockSignals 防止每个控件触发多次 preview 重算
        self.naming_mode_combo.blockSignals(True)
        self.naming_mode_combo.setCurrentIndex(0)
        self.naming_mode_combo.blockSignals(False)
        # 控件恢复默认值
        self.prefix_input.setText("img_")
        self.suffix_input.setText("_exported")
        # seq_digits_spin 是 DragValue，setValue 发 valueChanged（会触发 preview，无妨）
        self.seq_digits_spin.setValue(3)
        self.custom_pattern_input.setText("{name}_{n:03d}")
        # 重新按当前模式显隐行 + 刷新预览
        self._on_naming_mode_changed(self.naming_mode_combo.currentText())

    def _reset_resize_section(self) -> None:
        """Resize section 重置：关闭缩放、模式恢复 Long Edge、数值恢复 2048px。"""
        # resize_check 是 ToggleSwitch；setChecked(False) 不发 toggled，需手动刷新 UI
        self.resize_check.setChecked(False)
        self.resize_mode.blockSignals(True)
        self.resize_mode.setCurrentIndex(0)
        self.resize_mode.blockSignals(False)
        # resize_value 是 DragValue，先恢复 suffix/range 再 setValue，避免 clamp 到错误范围
        self.resize_value._suffix = " px"
        self.resize_value.setRange(1, 10000)
        self.resize_value.setValue(2048)
        self.resize_value._update_display()
        # 手动刷新 enable 态 + 尺寸标签
        self.update_resize_ui()

    def _reset_format_section(self) -> None:
        """Format section 重置：格式恢复 JPEG、质量 85、EXR 精度/压缩恢复默认。"""
        self.format_combo.blockSignals(True)
        self.format_combo.setCurrentIndex(0)  # JPEG
        self.format_combo.blockSignals(False)
        # quality_slider（ToposClickableSlider）+ quality_val_label（DragValue）双向同步
        self.quality_slider.setValue(85)
        self.quality_val_label.blockSignals(True)
        self.quality_val_label.setValue(85.0)
        self.quality_val_label.blockSignals(False)
        # EXR 默认精度 half（index 0）、压缩 ZIP（index 3）
        self.exr_type_combo.setCurrentIndex(0)
        self.exr_comp_combo.setCurrentIndex(3)
        # 触发一次格式变更以正确显隐 EXR 行
        self.on_format_changed(self.format_combo.currentText())

    def _naming_mode_id(self) -> str:
        """combo 当前项的内部命名模式名（AUD-33101）。

        显示文本随 locale 变化，不作为逻辑键；itemData 才是稳定标识。
        """
        data = self.naming_mode_combo.currentData()
        if isinstance(data, str) and data in self._NAMING_MODE_IDS:
            return data
        return "Filename + Suffix"  # combo 首项（默认模式）

    def _on_naming_mode_changed(self, mode: str):
        """Update UI when naming mode changes"""
        # AUD-33101：以 itemData 的内部模式名判别（显示文本已本地化）
        internal = self._naming_mode_id()
        show_prefix = internal in ("Prefix + Filename", "Prefix + Sequence")
        show_suffix = internal == "Filename + Suffix"
        show_seq_digits = internal == "Prefix + Sequence"
        show_custom = internal == "Custom Pattern"

        # Show/hide prefix / suffix / custom rows (整行显隐)
        self.prefix_widget.setVisible(show_prefix)
        self.suffix_widget.setVisible(show_suffix)
        self.custom_widget.setVisible(show_custom)

        # Show/hide sequence digits row
        self.seq_widget.setVisible(show_seq_digits)

        self.update_naming_preview()

    def _pick_directory(self):
        """Select export directory"""
        current_path = self.path_input.text()
        dir_path = QFileDialog.getExistingDirectory(
            self,
            I18N.tr("export.select_dir_dialog_title", "Select Export Directory"),
            current_path,
        )
        if dir_path:
            self.path_input.setText(dir_path)

    def _show_list_context_menu(self, pos):
        """Show list context menu"""
        menu = QMenu(self)

        if self.batch_list.selectedItems():
            remove_action = menu.addAction(
                I18N.tr("export.remove_selected", "Remove Selected")
            )
            remove_action.triggered.connect(self.batch_list.remove_selected)

        if self.batch_list.count() > 0:
            clear_action = menu.addAction(I18N.tr("export.clear_all", "Clear All"))
            clear_action.triggered.connect(self.batch_list.clear_all)

        menu.exec(self.batch_list.mapToGlobal(pos))

    def _on_files_changed(self):
        """Update UI when file list changes"""
        self._update_export_button()
        count = self.batch_list.count()
        self.file_count_label.setText(
            I18N.tr("export.file_count_files", "{count} files").format(count=count)
        )

    def _update_export_button(self):
        """Update export button state and text based on file count"""
        count = self.batch_list.count()
        if count == 0:
            self.export_btn.setText(I18N.tr("export.start_export", "开始导出"))
            self.export_btn.setEnabled(False)
        elif count == 1:
            self.export_btn.setText(I18N.tr("export.export_current_file", "导出当前文件"))
            self.export_btn.setEnabled(True)
        else:
            self.export_btn.setText(I18N.tr("export.batch_export_files", "批量导出 ({count} 个文件)").format(count=count))
            self.export_btn.setEnabled(True)

    def update_resize_ui(self):
        enabled = self.resize_check.isChecked()
        self.resize_widget.setEnabled(enabled)
        if not enabled:
            # AUD-33103：缩放未启用时清空缓存尺寸，get_settings 不再回读
            self._last_output_size = None
            self.output_size_label.setText(
                I18N.tr(
                    "export.result_size", "Result: {w} x {h}"
                ).format(w=self.orig_w, h=self.orig_h)
            )
        else:
            self.update_output_size()

    def update_output_size(self):
        # AUD-33103：原图尺寸无效（0/负）时不计算——此前 orig_w==0 早退
        # 后标签停留在 "Result: 0 x 0"，get_settings 解析文本得 resize=(0,0)；
        # 现在缓存属性保持 None，get_settings 据此跳过 resize 并告警。
        if not self.resize_check.isChecked() or self.orig_w <= 0 or self.orig_h <= 0:
            return

        mode = self.resize_mode.currentText()
        # AUD-33102：suffix/setRange 程序化更新必须 blockSignals——
        # setRange 收紧会 clamp 并 emit valueChanged（DragValue.setRange →
        # setValue → emit），重入本方法后外层继续用过期 val 覆盖标签，
        # 造成显示/实际值约 4 倍偏差。clamp 后重读控件实际值再计算。
        if mode == I18N.tr("export.resize_percent"):
            self.resize_value.blockSignals(True)
            self.resize_value._suffix = " %"
            self.resize_value.setRange(1, 500)
            self.resize_value.blockSignals(False)
            # resize_value 现在是 DragValue（value() 返回 float），强转 int
            val = int(round(self.resize_value.value()))
            new_w = int(self.orig_w * val / 100)
            new_h = int(self.orig_h * val / 100)
        else:
            self.resize_value.blockSignals(True)
            self.resize_value._suffix = " px"
            self.resize_value.setRange(1, 10000)
            self.resize_value.blockSignals(False)
            val = int(round(self.resize_value.value()))
            new_w, new_h = self.orig_w, self.orig_h
            if mode == I18N.tr("export.resize_width"):
                new_w = val
                new_h = int(self.orig_h * (val / self.orig_w))
            elif mode == I18N.tr("export.resize_height"):
                new_h = val
                new_w = int(self.orig_w * (val / self.orig_h))
            elif mode == I18N.tr("export.resize_long_edge"):
                if self.orig_w >= self.orig_h:
                    new_w = val
                    new_h = int(self.orig_h * (val / self.orig_w))
                else:
                    new_h = val
                    new_w = int(self.orig_w * (val / self.orig_h))
            elif mode == I18N.tr("export.resize_short_edge"):
                if self.orig_w <= self.orig_h:
                    new_w = val
                    new_h = int(self.orig_h * (val / self.orig_w))
                else:
                    new_h = val
                    new_w = int(self.orig_w * (val / self.orig_h))

        # suffix 变了要刷新显示
        self.resize_value._update_display()
        # AUD-33103：尺寸以属性为数据源写入，标签只承担展示职责
        self._last_output_size = (new_w, new_h)
        self.output_size_label.setText(
            I18N.tr("export.result_size", "Result: {w} x {h}").format(w=new_w, h=new_h)
        )

    def on_format_changed(self, fmt):
        # options_layout 现在是 QVBoxLayout（不再是 QFormLayout），用 setVisible 切换 EXR
        is_exr = fmt == I18N.tr("export.fmt_exr")
        self.exr_widget.setVisible(is_exr)

    def get_settings(self) -> Dict[str, Any]:
        """Get current export settings"""
        fmt_map = {"JPEG": ".jpg", "PNG": ".png", "OpenEXR": ".exr", "TIFF": ".tif", "WebP": ".webp"}
        fmt_display = self.format_combo.currentText()
        # format_combo 显示的是 i18n 文本，映射回内部格式名（与 fmt_map / 后续判断一致）
        fmt_map_display_to_name = {
            I18N.tr("export.fmt_jpeg"): "JPEG",
            I18N.tr("export.fmt_png"): "PNG",
            I18N.tr("export.fmt_exr"): "OpenEXR",
            I18N.tr("export.fmt_tiff"): "TIFF",
            I18N.tr("export.fmt_webp"): "WebP",
        }
        fmt_name = fmt_map_display_to_name.get(fmt_display, "JPEG")
        ext = fmt_map.get(fmt_name, ".jpg")

        # Generate filename based on naming mode
        # AUD-33101：naming_mode 下发内部模式名（deliver_controller 与
        # generate_export_filename 的逻辑键），不随 locale 变化
        naming_mode = self._naming_mode_id()
        # seq_digits_spin 现在是 DragValue（value() 返回 float），强转 int
        seq_digits = int(round(self.seq_digits_spin.value()))

        # Get suffix/prefix (shared function handles mode-specific usage)
        suffix = self.suffix_input.text().strip() or "_exported"
        prefix = self.prefix_input.text().strip() or "img_"
        custom_pattern = self.custom_pattern_input.text()

        # Get original filename (if any)
        files = self.batch_list.get_all_files()
        if files:
            orig_name = os.path.splitext(os.path.basename(files[0]))[0]
        else:
            orig_name = self.default_name or "untitled"

        # Generate filename using shared function
        filename = generate_export_filename(
            mode=naming_mode, original_name=orig_name, output_ext=ext,
            index=1, seq_digits=seq_digits, suffix=suffix, prefix=prefix,
            custom_pattern=custom_pattern
        )

        export_dir = self.path_input.text()
        settings: Dict[str, Any] = {
            "path": os.path.join(export_dir, filename),
            "format": fmt_name,
            "options": {"quality": self.quality_slider.value()},
            "naming_mode": naming_mode,
            "suffix": suffix,
            "prefix": prefix,
            "seq_digits": seq_digits,
            "custom_pattern": self.custom_pattern_input.text(),
            # 单文件导出也尊重 composite 开关（与批量导出一致）。
            # perform_panel_export 据此决定是否套用当前 composite 图。
            "apply_composite": self.apply_composite_check.isChecked(),
        }

        # AUD-33103：resize 取缓存属性并校验，不再解析显示标签文本
        # （文本仅展示，本地化/文案改动即解析错；orig 无效时属性为 None）
        resize_size = self._last_output_size
        if self.resize_check.isChecked():
            if resize_size is not None and resize_size[0] > 0 and resize_size[1] > 0:
                settings["options"]["resize"] = (int(resize_size[0]), int(resize_size[1]))
            else:
                logger.warning(
                    "ExportPanel: resize 已勾选但当前输出尺寸无效"
                    "（原图尺寸=%dx%d），本次导出不缩放",
                    self.orig_w, self.orig_h)

        if settings["format"] == "OpenEXR":
            settings["options"]["exr_type"] = self.exr_type_combo.currentIndex() + 1
            settings["options"]["exr_compression"] = self.exr_comp_combo.currentIndex()

        return settings

    def _start_export(self):
        """Unified export entry: decide export mode based on file count"""
        files = self.batch_list.get_all_files()

        if len(files) == 0:
            return
        elif len(files) == 1:
            # Single file: use current engine state to export directly
            settings = self.get_settings()
            self.export_requested.emit(settings)
        else:
            # Multiple files: trigger batch export
            self._start_batch_export(files)

    def _start_batch_export(self, files: List[str]):
        """Start batch export"""
        export_dir = self.path_input.text()
        if not export_dir or not os.path.isdir(export_dir):
            return

        # format_combo 显示 i18n 文本，映射回内部格式名（与 get_settings 一致）
        fmt_display = self.format_combo.currentText()
        fmt_map_display_to_name = {
            I18N.tr("export.fmt_jpeg"): "JPEG",
            I18N.tr("export.fmt_png"): "PNG",
            I18N.tr("export.fmt_exr"): "OpenEXR",
            I18N.tr("export.fmt_tiff"): "TIFF",
            I18N.tr("export.fmt_webp"): "WebP",
        }
        fmt_name = fmt_map_display_to_name.get(fmt_display, "JPEG")

        # Get naming settings (shared function handles mode-specific usage)
        # AUD-33101：naming_mode 下发内部模式名（逻辑键），不随 locale 变化
        naming_mode = self._naming_mode_id()

        # Collect all settings
        settings: Dict[str, Any] = {
            "files": files,
            "export_dir": export_dir,
            "format": fmt_name,
            "options": {"quality": self.quality_slider.value()},
            "naming_mode": naming_mode,
            "suffix": self.suffix_input.text().strip() or "_exported",
            "prefix": self.prefix_input.text().strip() or "img_",
            # seq_digits_spin 现在是 DragValue（float），强转 int
            "seq_digits": int(round(self.seq_digits_spin.value())),
            "custom_pattern": self.custom_pattern_input.text(),
            "apply_composite": self.apply_composite_check.isChecked(),
        }

        # AUD-33103：resize 取缓存属性并校验，不再解析显示标签文本
        resize_size = self._last_output_size
        if self.resize_check.isChecked():
            if resize_size is not None and resize_size[0] > 0 and resize_size[1] > 0:
                settings["options"]["resize"] = (int(resize_size[0]), int(resize_size[1]))
            else:
                logger.warning(
                    "ExportPanel: resize 已勾选但当前输出尺寸无效"
                    "（原图尺寸=%dx%d），本次导出不缩放",
                    self.orig_w, self.orig_h)

        if settings["format"] == "OpenEXR":
            settings["options"]["exr_type"] = self.exr_type_combo.currentIndex() + 1
            settings["options"]["exr_compression"] = self.exr_comp_combo.currentIndex()

        self.batch_export_requested.emit(settings)

    def update_naming_preview(self):
        """Update naming preview"""
        # AUD-33101：预览用内部模式名（显示文本已本地化，不参与逻辑）
        mode = self._naming_mode_id()
        sample_name = self.default_name or "image"
        # format_combo 显示 i18n 文本，取内部格式名计算扩展名
        fmt_display = self.format_combo.currentText()
        fmt_map_display_to_name = {
            I18N.tr("export.fmt_jpeg"): "JPEG",
            I18N.tr("export.fmt_png"): "PNG",
            I18N.tr("export.fmt_exr"): "OpenEXR",
            I18N.tr("export.fmt_tiff"): "TIFF",
            I18N.tr("export.fmt_webp"): "WebP",
        }
        fmt_name = fmt_map_display_to_name.get(fmt_display, "JPEG")
        ext_map = {"JPEG": ".jpg", "PNG": ".png", "OpenEXR": ".exr", "TIFF": ".tif", "WebP": ".webp"}
        sample_ext = ext_map.get(fmt_name, ".jpg")

        # AUD-33100：pattern 输入框每敲一键即触发本槽，非法 format 串
        # （半个 "{"、未知字段 "{foo}"、非法说明符 "{n:0dd}"）先在此预检，
        # 预览显示占位错误文案，不让异常逃逸出 Qt 槽。
        custom_pattern = self.custom_pattern_input.text()
        if custom_pattern:
            try:
                custom_pattern.format(name=sample_name, n=1, ext=sample_ext)
            except (KeyError, IndexError, ValueError):
                self.naming_preview_label.setText(
                    f"Preview: {I18N.tr('export.invalid_pattern', '(无效的命名模式)')}"
                )
                return

        preview = generate_export_filename(
            mode=mode, original_name=sample_name, output_ext=sample_ext,
            index=1, seq_digits=int(round(self.seq_digits_spin.value())),
            suffix=self.suffix_input.text() or "_exported",
            prefix=self.prefix_input.text() or "img_",
            custom_pattern=custom_pattern
        )

        self.naming_preview_label.setText(
            I18N.tr("export.naming_preview", "Preview: {preview}").format(preview=preview)
        )

    def set_batch_progress(self, current: int, total: int):
        """Set batch export progress"""
        if total > 0:
            self.batch_progress.setMaximum(total)
            self.batch_progress.setValue(current)

    def set_default_export_settings(self, fmt: str, quality: int):
        """Apply default export format and quality from preferences.

        fmt 是内部格式名（JPEG/PNG/OpenEXR/TIFF/WebP），需映射到 combo 显示的 i18n 文本。
        """
        name_to_display = {
            "JPEG": I18N.tr("export.fmt_jpeg"),
            "PNG": I18N.tr("export.fmt_png"),
            "OpenEXR": I18N.tr("export.fmt_exr"),
            "TIFF": I18N.tr("export.fmt_tiff"),
            "WebP": I18N.tr("export.fmt_webp"),
        }
        display = name_to_display.get(fmt, fmt)
        idx = self.format_combo.findText(display)
        if idx >= 0:
            self.format_combo.setCurrentIndex(idx)
        # quality_slider 是 ToposClickableSlider（int 域），直接 setValue
        self.quality_slider.setValue(quality)
        # quality_val_label 是 DragValue，需同步显示
        self.quality_val_label.blockSignals(True)
        self.quality_val_label.setValue(float(quality))
        self.quality_val_label.blockSignals(False)

    def start_batch_ui(self, total: int):
        """Switch UI to batch export mode"""
        self.export_btn.hide()
        self.cancel_btn.show()
        self.pause_btn.show()
        # 重置为"暂停"状态（非暂停态）
        self._batch_paused = False
        self.pause_btn.setText(I18N.tr("export.pause", "暂停"))
        self.batch_progress.setMaximum(total)
        self.batch_progress.setValue(0)
        self.batch_progress.show()

    def finish_batch_ui(self):
        """Restore UI from batch export mode"""
        self.cancel_btn.hide()
        self.pause_btn.hide()
        self.export_btn.show()
        self.export_btn.setEnabled(True)
        self.batch_progress.hide()
        self._batch_paused = False

    def _on_pause_btn_clicked(self):
        """暂停/继续按钮：根据当前状态发射对应信号。"""
        if getattr(self, '_batch_paused', False):
            self.batch_resume_requested.emit()
        else:
            self.batch_pause_requested.emit()

    def set_batch_paused(self, paused: bool):
        """供 controller 调用以同步按钮文字（暂停↔继续）。

        注意：这只是 UI 状态同步，真正的暂停/恢复逻辑在 controller 里。
        """
        self._batch_paused = paused
        if paused:
            self.pause_btn.setText(I18N.tr("export.resume", "继续"))
        else:
            self.pause_btn.setText(I18N.tr("export.pause", "暂停"))

    def set_export_button_enabled(self, enabled: bool):
        """Set export button enabled state"""
        self.export_btn.setEnabled(enabled)