"""Native Files browsing UI; all directory data comes from the Files service."""
from __future__ import annotations

import math
from collections import OrderedDict
from typing import Any

from PyQt6.QtCore import (
    QAbstractTableModel, QBuffer, QByteArray, QDir, QIODevice,
    QItemSelectionModel, QMimeData, QModelIndex, QSignalBlocker,
    QSize, QSortFilterProxyModel, Qt, QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import QColor, QDrag, QImage, QImageReader, QIcon, QKeySequence, QPainter, QPixmap, QResizeEvent
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QHBoxLayout,
    QFormLayout, QHeaderView, QLabel, QLayout, QLineEdit, QListView, QListWidget, QListWidgetItem,
    QMenu, QPushButton, QSizePolicy, QSlider, QSplitter, QStackedWidget, QStyle, QTreeView,
    QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.app.ui.native_files_gallery import FilesCardDelegate


def _tr(key: str, default: str) -> str:
    return I18N.tr(f"files.native.{key}", default)


def _number(value: Any) -> float:
    """Keep malformed optional service fields out of Qt paint callbacks."""
    try:
        result = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) else 0.0


class FilesEntryModel(QAbstractTableModel):
    """Presentation snapshot, never a second source of filesystem truth."""
    ENTRY_ROLE = int(Qt.ItemDataRole.UserRole) + 1
    HEADERS = ("名称", "类型", "大小", "修改日期", "评分")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entries: list[dict[str, Any]] = []
        self._icons: dict[str, QIcon] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.isValid() and self.entries[index.row()]["type"] != "dir":
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        return flags

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = int(Qt.ItemDataRole.DisplayRole)) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _tr(f"column.{section}", self.HEADERS[section])
        return None

    def data(self, index: QModelIndex, role: int = int(Qt.ItemDataRole.DisplayRole)) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]
        if role == self.ENTRY_ROLE:
            return entry
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry["path"]
        if role == Qt.ItemDataRole.DisplayRole:
            kind = entry["type"]
            kind_label = _tr(f"kind.{kind}", {"dir": "文件夹", "image": "图片", "video": "视频", "audio": "音频", "doc": "文档"}.get(kind, "文件"))
            return (entry["name"], kind_label, entry.get("size", "—"), entry.get("mtime", "—"), "★" * max(0, min(5, int(_number(entry.get("rating"))))))[index.column()]
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 0:
            kind = entry["type"]
            if kind not in self._icons:
                icon = QStyle.StandardPixmap.SP_DirIcon if kind == "dir" else (
                    QStyle.StandardPixmap.SP_MediaPlay if kind in ("video", "audio") else QStyle.StandardPixmap.SP_FileIcon)
                pixmap = QApplication.style().standardIcon(icon).pixmap(56, 56)
                painter = QPainter(pixmap)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
                painter.fillRect(pixmap.rect(), QColor(C.ORANGE if kind == "dir" else C.TEXT_SECONDARY))
                painter.end()
                self._icons[kind] = QIcon(pixmap)
            return self._icons[kind]
        return None

    def set_entries(self, entries: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self.entries = [dict(entry) for entry in entries]
        self.endResetModel()


class FilesFilterModel(QSortFilterProxyModel):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.query = ""
        self.kind = "all"
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        entry = self.sourceModel().index(row, 0, parent).data(FilesEntryModel.ENTRY_ROLE)
        return (self.kind == "all" or entry["type"] == "dir" or entry["type"] == self.kind) and (
            not self.query or self.query in " ".join(str(entry.get(k) or "") for k in ("name", "tags", "comments")).casefold())

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        a, b = left.data(FilesEntryModel.ENTRY_ROLE), right.data(FilesEntryModel.ENTRY_ROLE)
        if (a["type"] == "dir") != (b["type"] == "dir"):
            return a["type"] == "dir" if self.sortOrder() == Qt.SortOrder.AscendingOrder else b["type"] == "dir"
        key = {2: "size_bytes", 3: "mtime_ts", 4: "rating"}.get(left.column())
        if key:
            return _number(a.get(key)) < _number(b.get(key))
        return str(a.get("name" if left.column() == 0 else "type", "")).casefold() < str(b.get("name" if left.column() == 0 else "type", "")).casefold()


class _DragView:
    """Shared native drag and scoped keyboard behavior for the two views."""
    panel: NativeFilesPanel

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        mime = self.panel.mime_data_for_selection()
        if mime.hasUrls():
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.CopyAction)

    def keyPressEvent(self, event: Any) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.panel.copy_files()
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.panel.activate_current()
            event.accept()
        elif event.key() == Qt.Key.Key_Backspace:
            self.panel.go_up()
            event.accept()
        else:
            super().keyPressEvent(event)


class FilesIconView(_DragView, QListView):
    pass


class FilesDetailView(_DragView, QTreeView):
    pass


class NativeFilesPanel(QWidget):
    directory_requested = pyqtSignal(str)
    file_activated = pyqtSignal(str)
    legacy_requested = pyqtSignal()
    thumbnail_requested = pyqtSignal(str, int)
    thumbnail_released = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_path = ""
        self._requested_path = ""
        self._history: list[str] = []
        self._history_index = -1
        self._history_target: int | None = None
        self._ready = False
        self._images: OrderedDict[str, QImage] = OrderedDict()
        self._thumbnail_pending: set[str] = set()
        self._thumbnail_failed: set[str] = set()
        self._entry_paths: set[str] = set()
        self._entry_rows: dict[str, int] = {}
        self._thumbnail_timers: dict[str, QTimer] = {}
        self._thumbnail_attempts: dict[str, int] = {}
        self._thumbnail_generation = 0
        self._sidebar_preference: bool | None = None
        self._inspector_preference: bool | None = None
        self.model = FilesEntryModel(self)
        self.proxy = FilesFilterModel(self)
        self.proxy.setSourceModel(self.model)
        self.selection = QItemSelectionModel(self.proxy, self)
        self._build_ui()
        self.selection.selectionChanged.connect(self._update_info)
        self.proxy.rowsInserted.connect(self._update_info)
        self.proxy.rowsRemoved.connect(self._update_info)
        self.proxy.sort(0)

    def _button(self, text: str, tooltip: str, callback: Any) -> QPushButton:
        button = QPushButton(text, self)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.clicked.connect(callback)
        return button

    def _build_ui(self) -> None:
        self.setObjectName("nativeFiles")
        self.setStyleSheet(f"""
            QWidget#nativeFiles {{ background: {C.BG_DARK}; color: {C.TEXT_PRIMARY}; }}
            QLabel {{ background: transparent; color: {C.TEXT_SECONDARY}; }}
            QLabel#filesTitle {{ color: {C.TEXT_PRIMARY}; font-size: 18px; font-weight: 600; }}
            QLabel#filesSection {{ color: {C.TEXT_PRIMARY}; font-size: 13px; font-weight: 600; }}
            QLabel#filesHint {{ color: {C.TEXT_HINT}; font-size: 11px; }}
            QWidget#filesSidebar, QWidget#filesInspector {{ background: {C.BG_DARKEST}; }}
            QAbstractItemView {{ background: transparent; color: {C.TEXT_PRIMARY}; border: none; outline: none; }}
            QListWidget::item {{ border-radius: 5px; padding: 7px 6px; }}
            QListWidget::item:selected {{ background: {C.BG_ACTIVE}; color: {C.TEXT_BRIGHT}; }}
            QTreeView::item {{ height: 30px; border-bottom: 1px solid {C.BORDER_SUBTLE}; }}
            QTreeView::item:selected {{ background: {C.BG_ACTIVE}; color: {C.ORANGE}; }}
            QHeaderView::section {{ background: {C.BG_DARK}; color: {C.TEXT_HINT}; border: none; padding: 8px; font-size: 11px; }}
            QLineEdit {{ background: {C.BG_INPUT}; color: {C.TEXT_PRIMARY}; border: 1px solid {C.BORDER}; border-radius: 7px; padding: 7px 10px; }}
            QLineEdit:focus {{ border-color: {C.ORANGE_PRESS}; }}
            QPushButton {{ background: transparent; color: {C.TEXT_SECONDARY}; border: 1px solid transparent; border-radius: 5px; padding: 5px 9px; }}
            QPushButton:hover, QPushButton:checked {{ background: {C.BG_ACTIVE}; color: {C.TEXT_PRIMARY}; }}
            QPushButton:disabled {{ color: {C.TEXT_HINT}; }}
            QPushButton#filesBreadcrumb {{ text-align: left; color: {C.TEXT_SECONDARY}; }}
            QSplitter::handle {{ background: {C.BORDER_SUBTLE}; }}
            QSlider::groove:horizontal {{ height: 3px; border-radius: 1px; background: {C.BORDER}; }}
            QSlider::handle:horizontal {{ background: {C.TEXT_SECONDARY}; width: 9px; margin: -3px 0; border-radius: 4px; }}
        """)
        root = QVBoxLayout(self)
        # A visible inspector must not impose its wide layout minimum on the
        # dock: let resizeEvent hide secondary panes as the user narrows it.
        root.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QHBoxLayout()
        header.setContentsMargins(16, 14, 16, 10)
        title = QLabel(_tr("title", "素材库"), self)
        title.setObjectName("filesTitle")
        header.addWidget(title)
        header.addSpacing(20)
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText(_tr("search", "搜索名称、标签或备注…"))
        self.search_edit.setAccessibleName(_tr("search_label", "搜索当前目录"))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._filter_changed)
        header.addWidget(self.search_edit, 1)
        header.addSpacing(8)
        self.open_folder_button = self._button(_tr("choose_short", "打开文件夹"), _tr("choose", "选择文件夹"), self._choose_folder)
        header.addWidget(self.open_folder_button)
        self.more_button = self._button("•••", _tr("more", "更多工具"), self._show_tools)
        header.addWidget(self.more_button)
        root.addLayout(header)
        navigation = QHBoxLayout()
        navigation.setContentsMargins(12, 0, 12, 10)
        self.sidebar_button = self._button(_tr("locations", "位置"), _tr("toggle_sidebar", "显示或隐藏位置栏"), self._toggle_sidebar)
        self.sidebar_button.setCheckable(True)
        self.sidebar_button.setChecked(True)
        navigation.addWidget(self.sidebar_button)
        navigation.addSpacing(6)
        self.back_button = self._button("‹", _tr("back", "后退"), lambda: self._travel(-1))
        self.forward_button = self._button("›", _tr("forward", "前进"), lambda: self._travel(1))
        for button in (self.back_button, self.forward_button):
            button.setFixedWidth(28)
            navigation.addWidget(button)
        navigation.addWidget(self._button("↑", _tr("up", "上一级"), self.go_up))
        self.path_stack = QStackedWidget(self)
        self.breadcrumb_button = self._button("", _tr("edit_path", "点击编辑完整路径"), self._edit_path)
        self.breadcrumb_button.setObjectName("filesBreadcrumb")
        self.breadcrumb_button.setMinimumWidth(0)
        self.breadcrumb_button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.path_stack.setMinimumWidth(50)
        self.path_stack.addWidget(self.breadcrumb_button)
        self.path_edit = QLineEdit(self)
        self.path_edit.setPlaceholderText(_tr("path", "输入文件夹路径"))
        self.path_edit.setAccessibleName(_tr("path", "文件夹路径"))
        self.path_edit.returnPressed.connect(lambda: self.navigate(self.path_edit.text()))
        self.path_edit.editingFinished.connect(lambda: self.path_stack.setCurrentIndex(0))
        self.path_stack.addWidget(self.path_edit)
        navigation.addWidget(self.path_stack, 1)
        navigation.addWidget(self._button("↻", _tr("refresh", "刷新"), self.refresh))
        self.inspector_button = self._button(_tr("inspector_short", "信息"), _tr("toggle_inspector", "显示或隐藏文件信息"), self._toggle_inspector)
        self.inspector_button.setCheckable(True)
        navigation.addWidget(self.inspector_button)
        root.addLayout(navigation)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setHandleWidth(1)
        self.sidebar = QWidget(self.splitter)
        self.sidebar.setObjectName("filesSidebar")
        side_layout = QVBoxLayout(self.sidebar)
        side_layout.setContentsMargins(12, 16, 10, 12)
        side_label = QLabel(_tr("location_heading", "浏览位置"))
        side_label.setObjectName("filesSection")
        side_layout.addWidget(side_label)
        side_hint = QLabel(_tr("location_hint", "个人目录与最近访问"))
        side_hint.setObjectName("filesHint")
        side_layout.addWidget(side_hint)
        self.locations = QListWidget(self.sidebar)
        self.locations.setSpacing(3)
        self.locations.setMinimumWidth(112)
        home = QListWidgetItem(_tr("home", "个人文件夹"))
        home.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirHomeIcon))
        home.setData(Qt.ItemDataRole.UserRole, QDir.homePath())
        self.locations.addItem(home)
        self.locations.itemClicked.connect(lambda item: self.navigate(item.data(Qt.ItemDataRole.UserRole)))
        side_layout.addWidget(self.locations, 1)
        side_footer = QLabel(_tr("side_footer", "从素材库拖入媒体池或时间线"))
        side_footer.setObjectName("filesHint")
        side_footer.setWordWrap(True)
        side_layout.addWidget(side_footer)
        content = QWidget(self.splitter)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(14, 12, 14, 6)
        content_layout.setSpacing(8)
        toolbar = QHBoxLayout()
        self.folder_title = QLabel(_tr("browse", "浏览素材"), content)
        self.folder_title.setObjectName("filesSection")
        self.folder_title.setTextFormat(Qt.TextFormat.PlainText)
        self.folder_title.setMinimumWidth(0)
        self.folder_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(self.folder_title, 1)
        self.type_combo = ChevronComboBox(self)
        self.type_combo.setAccessibleName(_tr("type_filter", "文件类型筛选"))
        for label, kind in (("全部类型", "all"), ("图片", "image"), ("视频", "video"), ("音频", "audio"), ("文档", "doc")):
            self.type_combo.addItem(_tr(f"type.{kind}", label), kind)
        self.type_combo.currentIndexChanged.connect(self._filter_changed)
        toolbar.addWidget(self.type_combo)
        self.sort_combo = ChevronComboBox(self)
        self.sort_combo.setAccessibleName(_tr("sort", "排序方式"))
        for label, key in (("名称 A–Z", "name"), ("最近修改", "date"), ("最大文件", "size"), ("最高评分", "rating")):
            self.sort_combo.addItem(_tr(f"sort.{key}", label))
        self.sort_combo.currentIndexChanged.connect(self._sort_changed)
        toolbar.addWidget(self.sort_combo)
        self.view_combo = ChevronComboBox(self)
        self.view_combo.setAccessibleName(_tr("view", "文件视图"))
        self.view_combo.addItems([_tr("gallery", "卡片"), _tr("details", "列表")])
        toolbar.addWidget(self.view_combo)
        content_layout.addLayout(toolbar)
        self.pages = QStackedWidget(content)
        self.icon_view = FilesIconView(self.pages)
        self.icon_view.setViewMode(QListView.ViewMode.IconMode)
        self.icon_view.setResizeMode(QListView.ResizeMode.Adjust)
        self.icon_view.setMovement(QListView.Movement.Static)
        self.icon_view.setMouseTracking(True)
        self.icon_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.card_delegate = FilesCardDelegate(FilesEntryModel.ENTRY_ROLE, self._image_for_entry, self.icon_view)
        self.icon_view.setItemDelegate(self.card_delegate)
        self.icon_view.setGridSize(self.card_delegate.sizeHint(None, QModelIndex()))
        self.detail_view = FilesDetailView(self.pages)
        self.detail_view.setRootIsDecorated(False)
        self.detail_view.setItemsExpandable(False)
        self.detail_view.setUniformRowHeights(True)
        self.detail_view.setSortingEnabled(True)
        self.detail_view.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for view in (self.icon_view, self.detail_view):
            view.panel = self
            view.setModel(self.proxy)
            view.setSelectionModel(self.selection)
            view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            view.setDragEnabled(True)
            view.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
            view.doubleClicked.connect(self._activate)
            view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda pos, v=view: self._context_menu(v, pos))
            self.pages.addWidget(view)
        self.detail_view.setColumnWidth(0, 230)
        self.detail_view.setColumnWidth(1, 62)
        self.detail_view.setColumnWidth(2, 80)
        self.detail_view.setColumnWidth(3, 110)
        self.view_combo.currentIndexChanged.connect(self.pages.setCurrentIndex)
        content_layout.addWidget(self.pages, 1)
        self.message = QLabel(content)
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.hide()
        content_layout.addWidget(self.message)
        self.inspector = QWidget(self.splitter)
        self.inspector.setObjectName("filesInspector")
        self.inspector.setMinimumWidth(200)
        info_layout = QVBoxLayout(self.inspector)
        info_layout.setContentsMargins(16, 16, 16, 16)
        info_heading = QLabel(_tr("inspector", "文件信息"))
        info_heading.setObjectName("filesSection")
        info_layout.addWidget(info_heading)
        info_layout.addSpacing(10)
        self.preview_cover = QLabel(self.inspector)
        self.preview_cover.setFixedHeight(130)
        self.preview_cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_cover.setStyleSheet(f"background: {C.BG_INPUT_DARK}; border-radius: 7px;")
        info_layout.addWidget(self.preview_cover)
        self.info = QLabel(_tr("select", "选择素材查看信息"), self.inspector)
        self.info.setObjectName("filesSection")
        self.info.setTextFormat(Qt.TextFormat.PlainText)
        self.info.setWordWrap(True)
        self.info.setMinimumWidth(0)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        info_layout.addSpacing(6)
        info_layout.addWidget(self.info)
        self.info_path = QLabel(self.inspector)
        self.info_path.setObjectName("filesHint")
        self.info_path.setTextFormat(Qt.TextFormat.PlainText)
        self.info_path.setWordWrap(True)
        info_layout.addWidget(self.info_path)
        info_layout.addSpacing(12)
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.info_fields = {}
        for key, label in (("type", "类型"), ("size", "大小"), ("mtime", "修改日期"), ("duration", "时长"), ("tags", "标签")):
            value = QLabel("—", self.inspector)
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setWordWrap(True)
            self.info_fields[key] = value
            form.addRow(_tr(f"field.{key}", label), value)
        info_layout.addLayout(form)
        info_layout.addStretch()
        self.inspector.hide()
        self.splitter.setSizes([164, 680, 240])
        self.splitter.setStretchFactor(1, 1)
        root.addWidget(self.splitter, 1)
        footer = QHBoxLayout()
        footer.setContentsMargins(16, 8, 16, 10)
        self.status = QLabel(self)
        self.status.setObjectName("filesHint")
        footer.addWidget(self.status, 1)
        footer.addWidget(QLabel(_tr("density", "卡片大小")))
        self.density_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.density_slider.setRange(148, 232)
        self.density_slider.setValue(180)
        self.density_slider.setFixedWidth(90)
        self.density_slider.setAccessibleName(_tr("density", "卡片大小"))
        self.density_slider.valueChanged.connect(self._resize_cards)
        footer.addWidget(self.density_slider)
        root.addLayout(footer)
        self._update_navigation()
        self._update_info()

    def _show_tools(self) -> None:
        menu = QMenu(self)
        menu.addAction(_tr("choose", "选择文件夹"), self._choose_folder)
        sort_menu = menu.addMenu(_tr("sort", "排序方式"))
        for index in range(self.sort_combo.count()):
            action = sort_menu.addAction(self.sort_combo.itemText(index))
            action.setCheckable(True)
            action.setChecked(self.sort_combo.currentIndex() == index)
            action.triggered.connect(lambda checked=False, i=index: self.sort_combo.setCurrentIndex(i))
        menu.addSeparator()
        menu.addAction(_tr("full_tools", "更多素材工具（旧版）…"), self.legacy_requested.emit)
        menu.exec(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))

    def _edit_path(self) -> None:
        self.path_stack.setCurrentIndex(1)
        self.path_edit.setFocus()
        self.path_edit.selectAll()

    def _update_path_label(self, path: str) -> None:
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        label = "  ›  ".join(parts[-3:]) if parts else path
        self.breadcrumb_button.setText(label)
        self.breadcrumb_button.setToolTip(path)
        self.folder_title.setText(QDir(path).dirName() or path)
        self.path_stack.setCurrentIndex(0)

    def _toggle_sidebar(self, checked: bool) -> None:
        self._sidebar_preference = checked
        self.sidebar.setVisible(checked)

    def _toggle_inspector(self, checked: bool) -> None:
        self._inspector_preference = checked
        self.inspector.setVisible(checked)
        if checked:
            self.splitter.setSizes([164, max(200, self.width() - 440), 240])

    def _sort_changed(self, index: int) -> None:
        column = (0, 3, 2, 4)[index]
        self.proxy.sort(column, Qt.SortOrder.AscendingOrder if index == 0 else Qt.SortOrder.DescendingOrder)

    def _resize_cards(self, width: int) -> None:
        self.card_delegate.card_width = width
        self.icon_view.setGridSize(self.card_delegate.sizeHint(None, QModelIndex()))
        self.icon_view.doItemsLayout()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if not hasattr(self, "sidebar"):
            return
        show_sidebar = self._sidebar_preference if self._sidebar_preference is not None else self.width() >= 720
        self.sidebar.setVisible(show_sidebar)
        self.open_folder_button.setVisible(self.width() >= 760)
        self.sort_combo.setVisible(self.width() >= 800)
        with QSignalBlocker(self.sidebar_button):
            self.sidebar_button.setChecked(show_sidebar)
        self._sync_inspector_visibility()

    def minimumSizeHint(self) -> QSize:
        return QSize(380, 280)

    def _sync_inspector_visibility(self) -> None:
        show = self._inspector_preference if self._inspector_preference is not None else self.width() >= 1000 and bool(self.selection.selectedRows(0))
        changed = self.inspector.isHidden() == show
        self.inspector.setVisible(show)
        with QSignalBlocker(self.inspector_button):
            self.inspector_button.setChecked(show)
        if show and changed:
            self.splitter.setSizes([164, max(240, self.width() - 440), 240])

    def _image_for_entry(self, entry: dict[str, Any]) -> QImage | None:
        path = entry["path"]
        image = self._images.get(path)
        if image is not None:
            self._images.move_to_end(path)
            return image
        if (self._ready and entry["type"] in ("image", "video", "doc")
                and path not in self._thumbnail_pending and path not in self._thumbnail_failed):
            self._thumbnail_pending.add(path)
            self.thumbnail_requested.emit(path, int(_number(entry.get("t_ver"))))
        return None

    def set_thumbnail(self, path: str, data: bytes) -> None:
        if not self._ready or path not in self._entry_paths:
            return
        self._thumbnail_pending.discard(path)
        self._stop_thumbnail_timer(path)
        self._thumbnail_attempts.pop(path, None)
        buffer = QBuffer(self)
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        size = reader.size()
        if not size.isValid() or size.width() > 8192 or size.height() > 8192:
            buffer.deleteLater()
            self.thumbnail_failed(path)
            return
        if size.width() > 480 or size.height() > 300:
            reader.setScaledSize(size.scaled(QSize(480, 300), Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        buffer.deleteLater()
        if image.isNull():
            self.thumbnail_failed(path)
            return
        self._images[path] = image
        self._images.move_to_end(path)
        while len(self._images) > self._thumbnail_capacity():
            evicted = next((key for key in self._images if not self._entry_visible(key)), None)
            if evicted is None:
                # Visible entries must not evict one another and cause an
                # endless request/decode/repaint cycle on large displays.
                break
            self._images.pop(evicted)
            self.thumbnail_released.emit(evicted)
        index = self.proxy.mapFromSource(self.model.index(self._entry_rows[path], 0))
        self.icon_view.viewport().update(self.icon_view.visualRect(index))
        if any(entry["path"] == path for entry in self._selected_entries()):
            self._update_info()

    def _thumbnail_capacity(self) -> int:
        grid = self.icon_view.gridSize()
        if not self.icon_view.isVisible() or grid.width() <= 0 or grid.height() <= 0:
            return 128
        viewport = self.icon_view.viewport()
        return max(128, math.ceil(viewport.width() / grid.width()) * (math.ceil(viewport.height() / grid.height()) + 2) + 16)

    def _entry_visible(self, path: str) -> bool:
        if self.inspector.isVisible() and any(entry["path"] == path for entry in self._selected_entries()):
            return True
        row = self._entry_rows.get(path)
        if row is None or not self.icon_view.isVisible():
            return False
        index = self.proxy.mapFromSource(self.model.index(row, 0))
        return index.isValid() and self.icon_view.visualRect(index).intersects(self.icon_view.viewport().rect())

    def _stop_thumbnail_timer(self, path: str) -> None:
        timer = self._thumbnail_timers.pop(path, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def thumbnail_pending(self, path: str) -> None:
        """Cold-cache generation is pending, distinct from a broken image."""
        if not self._ready or path not in self._entry_paths or path in self._thumbnail_timers:
            return
        attempt = self._thumbnail_attempts.get(path, 0)
        delays = (500, 1500, 3000, 6000, 12000)
        if attempt >= len(delays):
            self.thumbnail_failed(path)
            return
        self._thumbnail_attempts[path] = attempt + 1
        self._thumbnail_pending.add(path)
        generation = self._thumbnail_generation
        timer = QTimer(self)
        timer.setSingleShot(True)
        self._thumbnail_timers[path] = timer

        def retry() -> None:
            self._stop_thumbnail_timer(path)
            if generation != self._thumbnail_generation or not self._ready or path not in self._entry_paths:
                return
            self._thumbnail_pending.discard(path)
            self.thumbnail_released.emit(path)
            if self._entry_visible(path):
                self._image_for_entry(self.model.entries[self._entry_rows[path]])

        timer.timeout.connect(retry)
        timer.start(delays[attempt])

    def _clear_thumbnail_work(self) -> None:
        self._thumbnail_generation += 1
        for path in list(self._thumbnail_timers):
            self._stop_thumbnail_timer(path)
        self._thumbnail_attempts.clear()
        self._images.clear()
        self._thumbnail_pending.clear()
        self._thumbnail_failed.clear()

    def thumbnail_failed(self, path: str) -> None:
        self._stop_thumbnail_timer(path)
        self._thumbnail_pending.discard(path)
        if path in self._entry_paths:
            self._thumbnail_failed.add(path)

    def navigate(self, path: str) -> None:
        if not path or not path.strip():
            return
        if "://" in path:
            self.message.setText(_tr("virtual", "此位置请在完整 Files 中打开。"))
            self.message.show()
            return
        self._history_target = None
        self.directory_requested.emit(path)

    def _travel(self, offset: int) -> None:
        target = self._history_index + offset
        if 0 <= target < len(self._history):
            self._history_target = target
            self.directory_requested.emit(self._history[target])

    def go_up(self) -> None:
        path = self.current_path or self._requested_path
        if path:
            parent = QDir(path)
            if parent.cdUp():
                self.navigate(parent.absolutePath())

    def refresh(self) -> None:
        path = self._requested_path or self.current_path
        if path:
            self.directory_requested.emit(path)

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, _tr("choose", "选择文件夹"), self.current_path or QDir.homePath())
        if folder:
            self.navigate(folder)

    def set_loading(self, path: str) -> None:
        self._requested_path = path
        self._ready = False
        self._clear_thumbnail_work()
        self._entry_paths.clear()
        self._entry_rows.clear()
        self.path_edit.setText(path)
        self._update_path_label(path)
        self.model.set_entries([])
        self.pages.setEnabled(False)
        self.message.setText(_tr("loading", "正在读取目录…"))
        self.message.show()
        self._update_info()

    def set_entries(self, path: str, entries: list[dict[str, Any]]) -> None:
        if self._history_target is not None and self._history[self._history_target] == path:
            self._history_index = self._history_target
        elif path != self.current_path:
            self._history = self._history[:self._history_index + 1] + [path]
            self._history_index = len(self._history) - 1
        self._history_target = None
        self.current_path = self._requested_path = path
        self.path_edit.setText(path)
        self._update_path_label(path)
        self._clear_thumbnail_work()
        self._entry_paths = {entry["path"] for entry in entries}
        self._entry_rows = {entry["path"]: row for row, entry in enumerate(entries)}
        self._ready = True
        self.model.set_entries(entries)
        self.pages.setEnabled(True)
        self.message.setText("" if entries else _tr("empty", "此文件夹为空。"))
        self.message.setVisible(not entries)
        if not any(self.locations.item(i).data(Qt.ItemDataRole.UserRole) == path for i in range(self.locations.count())):
            item = QListWidgetItem(QDir(path).dirName() or path)
            item.setToolTip(path)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.locations.addItem(item)
            if self.locations.count() > 13:
                self.locations.takeItem(1)
        self._update_navigation()
        self._update_info()

    def show_error(self, path: str, message: str) -> None:
        self.set_loading(path)
        self._history_target = None
        self.message.setText(message)
        self._update_navigation()

    def _update_navigation(self) -> None:
        self.back_button.setEnabled(self._history_index > 0)
        self.forward_button.setEnabled(0 <= self._history_index < len(self._history) - 1)

    def _filter_changed(self, *_: Any) -> None:
        self.proxy.query = self.search_edit.text().casefold().strip()
        self.proxy.kind = self.type_combo.currentData()
        self.proxy.invalidateFilter()
        self._update_info()

    def _selected_entries(self) -> list[dict[str, Any]]:
        return [index.data(FilesEntryModel.ENTRY_ROLE) for index in sorted(self.selection.selectedRows(0), key=lambda index: index.row())]

    def selected_file_urls(self) -> list[QUrl]:
        if not self._ready:
            return []
        return [QUrl.fromLocalFile(entry["path"]) for entry in self._selected_entries() if entry["type"] != "dir"]

    def mime_data_for_selection(self) -> QMimeData:
        mime = QMimeData()
        urls = self.selected_file_urls()
        if urls:
            mime.setUrls(urls)
        return mime

    def copy_files(self) -> None:
        mime = self.mime_data_for_selection()
        if mime.hasUrls():
            QApplication.clipboard().setMimeData(mime)

    def _update_info(self, *_: Any) -> None:
        entries = self._selected_entries()
        self.status.setText(_tr("counts", "{visible} / {total} 项 · 已选 {selected} 项").format(visible=self.proxy.rowCount(), total=self.model.rowCount(), selected=len(entries)))
        if len(entries) == 1:
            entry = entries[0]
            self.info.setText(entry["name"])
            self.info_path.setText(self.fontMetrics().elidedText(entry["path"], Qt.TextElideMode.ElideMiddle, 205))
            self.info_path.setToolTip(entry["path"])
            kind = entry["type"]
            values = dict(entry)
            values["type"] = _tr(f"kind.{kind}", {"dir": "文件夹", "image": "图片", "video": "视频", "audio": "音频", "doc": "文档"}.get(kind, "文件"))
            duration = _number(entry.get("duration"))
            values["duration"] = f"{int(duration) // 60}:{int(duration) % 60:02d}" if duration > 0 else "—"
            for key, label in self.info_fields.items():
                label.setText(str(values.get(key) or "—"))
            image = self._image_for_entry(entry) if self._ready else None
            if image is not None:
                self.preview_cover.setPixmap(QPixmap.fromImage(image).scaled(QSize(210, 130), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            else:
                source = self.model.index(next(i for i, item in enumerate(self.model.entries) if item["path"] == entry["path"]), 0)
                self.preview_cover.setPixmap(source.data(Qt.ItemDataRole.DecorationRole).pixmap(40, 40))
        else:
            self.info.setText(_tr("selected", "已选择 {count} 项").format(count=len(entries)) if entries else _tr("select", "选择素材查看信息"))
            self.info_path.setText(_tr("multi_hint", "可将所选文件一起拖入编辑页面") if entries else "")
            self.preview_cover.clear()
            for label in self.info_fields.values():
                label.setText("—")
        self._sync_inspector_visibility()

    def activate_current(self) -> None:
        if self._ready and len(self.selection.selectedRows(0)) == 1:
            self._activate(self.selection.selectedRows(0)[0])

    def _activate(self, index: QModelIndex) -> None:
        if self._ready and index.isValid():
            entry = index.data(FilesEntryModel.ENTRY_ROLE)
            if entry["type"] == "dir":
                self.navigate(entry["path"])
            else:
                self.file_activated.emit(entry["path"])

    def _context_menu(self, view: QAbstractItemView, pos: Any) -> None:
        menu = QMenu(self)
        entry = view.indexAt(pos)
        if entry.isValid() and not self.selection.isSelected(entry):
            self.selection.select(entry, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        if self.selection.selectedRows(0):
            if len(self.selection.selectedRows(0)) == 1:
                menu.addAction(_tr("open", "打开"), self.activate_current)
            copy_action = menu.addAction(_tr("copy", "复制文件"), self.copy_files)
            copy_action.setEnabled(bool(self.selected_file_urls()))
            menu.addAction(_tr("copy_path", "复制路径"), lambda: QApplication.clipboard().setText("\n".join(e["path"] for e in self._selected_entries())))
            menu.addSeparator()
        menu.addAction(_tr("full", "完整 Files…"), self.legacy_requested.emit)
        menu.exec(view.viewport().mapToGlobal(pos))

    def shutdown(self) -> None:
        self._ready = False
        self._clear_thumbnail_work()
