"""Native Files browsing UI; all directory data comes from the Files service."""
from __future__ import annotations

import math
import os
import unicodedata
from collections import OrderedDict
from typing import Any

from PyQt6.QtCore import (
    QAbstractTableModel, QBuffer, QByteArray, QDir, QEvent, QIODevice, QObject,
    QItemSelectionModel, QMimeData, QModelIndex, QPoint, QSignalBlocker,
    QSize, QSortFilterProxyModel, Qt, QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import QColor, QDrag, QImage, QImageReader, QIcon, QKeySequence, QPainter, QPen, QPixmap, QResizeEvent
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QBoxLayout, QFileDialog, QHBoxLayout, QInputDialog,
    QFrame, QFormLayout, QHeaderView, QLabel, QLayout, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QStackedWidget, QStyle, QTreeView,
    QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.app.ui.native_files_gallery import FilesCardDelegate, FilesListDelegate
from src.app.ui.native_files_gallery_view import FilesGalleryView
from src.app.ui.native_files_locations import HomeDirectoryTree
from src.shared.ui.panel_splitter import PanelSplitter
from src.app.ui.native_files_preview import NativeFilesPreview, PreviewDecoder
from src.shared.playback.player_host import NativePlayerHost
from src.features.files.contracts import (
    Availability, Entry, FilesError, format_bytes, format_duration,
    normalize_path, parse_entry,
)


def _tr(key: str, default: str) -> str:
    return I18N.tr(f"files.native.{key}", default)


def _number(value: Any) -> float:
    """Keep malformed optional service fields out of Qt paint callbacks."""
    try:
        result = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) else 0.0


KIND_LABELS = {"dir": "文件夹", "image": "图片", "video": "视频", "audio": "音频",
               "doc": "文档", "other": "文件"}


def _kind_label(kind: str) -> str:
    return _tr(f"kind.{kind}", KIND_LABELS.get(kind, "文件"))


class FilesEntryModel(QAbstractTableModel):
    """Presentation snapshot, never a second source of filesystem truth."""
    ENTRY_ROLE = int(Qt.ItemDataRole.UserRole) + 1
    HEADERS = ("名称", "类型", "大小", "修改日期", "评分", "尺寸", "扩展名")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entries: list[Entry] = []
        self._icons: dict[str, QIcon] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.isValid() and not self.entries[index.row()].is_dir:
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
            return entry.path
        if role == Qt.ItemDataRole.DisplayRole:
            return (entry.name, _kind_label(entry.kind), entry.size_label,
                    entry.mtime_label or "—", "★" * entry.user.rating,
                    entry.technical.resolution or "—",
                    entry.name.rsplit(".", 1)[-1].upper() if "." in entry.name and not entry.is_dir else "—")[index.column()]
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 0:
            kind = entry.kind
            if kind not in self._icons:
                icon = QStyle.StandardPixmap.SP_DirIcon if entry.is_dir else (
                    QStyle.StandardPixmap.SP_MediaPlay if kind in ("video", "audio")
                    else QStyle.StandardPixmap.SP_FileIcon)
                pixmap = QApplication.style().standardIcon(icon).pixmap(56, 56)
                painter = QPainter(pixmap)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
                painter.fillRect(pixmap.rect(), QColor(C.ORANGE if entry.is_dir else C.TEXT_SECONDARY))
                painter.end()
                self._icons[kind] = QIcon(pixmap)
            return self._icons[kind]
        return None

    def set_entries(self, entries: list[Any]) -> None:
        self.beginResetModel()
        self.entries = [_coerce_entry(entry) for entry in entries]
        self.endResetModel()

    def append_entries(self, entries: list[Any]) -> None:
        """分页续页：增量插入，不重置模型（选择/滚动/展开状态保留）。"""
        fresh = [_coerce_entry(entry) for entry in entries
                 if _coerce_entry(entry).path not in
                 {existing.path for existing in self.entries}]
        if not fresh:
            return
        self.beginInsertRows(QModelIndex(), len(self.entries),
                             len(self.entries) + len(fresh) - 1)
        self.entries.extend(fresh)
        self.endInsertRows()

    def apply_patch(self, paths: list[str], patch: dict[str, Any]) -> None:
        """按服务确认结果更新受影响行：不重建模型，保滚动与选择。"""
        from dataclasses import replace
        from src.features.files.contracts import split_tags
        targets = set(paths)
        changed_rows = False
        for row, entry in enumerate(self.entries):
            if entry.path not in targets:
                continue
            changes: dict[str, Any] = {}
            if "rating" in patch:
                changes["rating"] = max(0, min(5, int(patch["rating"])))
            if "color" in patch:
                changes["color"] = max(0, min(5, int(patch["color"])))
            if "flag" in patch:
                changes["flag"] = max(0, min(2, int(patch["flag"])))
            if "tags" in patch:
                changes["tags"] = split_tags(patch["tags"])
            if "comments" in patch:
                changes["comments"] = str(patch["comments"])
            if not changes:
                continue
            self.entries[row] = entry.with_user(replace(entry.user, **changes))
            changed_rows = True
        if changed_rows:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self.entries) - 1, len(self.HEADERS) - 1), [])


def _coerce_entry(value: Any) -> Entry:
    """接受 DTO 或旧 dict；校验失败的行直接丢弃，不让半条数据进视图。"""
    if isinstance(value, Entry):
        return value
    try:
        return parse_entry(value)
    except FilesError:
        raise


class FilesFilterModel(QSortFilterProxyModel):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.query = ""
        self.kind = "all"
        self.flag = -1
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        entry = self.sourceModel().index(row, 0, parent).data(FilesEntryModel.ENTRY_ROLE)
        if entry is None:
            return False
        in_kind = self.kind == "all" or entry.is_dir or entry.kind == self.kind
        if not in_kind:
            return False
        if self.flag >= 0 and not entry.is_dir and entry.user.flag != self.flag:
            return False
        if not self.query:
            return True
        haystack = " ".join((entry.name, entry.user.tag_text, entry.user.comments)).casefold()
        return self.query in haystack

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        a = left.data(FilesEntryModel.ENTRY_ROLE)
        b = right.data(FilesEntryModel.ENTRY_ROLE)
        if a is None or b is None:
            return False
        if a.is_dir != b.is_dir:
            # 目录永远置顶：降序时 lessThan 会被反转，因此这里必须返回
            # "目录较大"，反转后才仍然排在前面（与旧 Files 行为一致）。
            return a.is_dir if self.sortOrder() == Qt.SortOrder.AscendingOrder else b.is_dir
        if left.column() == 2:
            return _number(a.size_bytes) < _number(b.size_bytes)
        if left.column() == 3:
            return _number(a.mtime_ts) < _number(b.mtime_ts)
        if left.column() == 4:
            return a.user.rating < b.user.rating
        if left.column() == 1:
            return a.kind.casefold() < b.kind.casefold()
        if left.column() == 5:
            return (_number(a.technical.width) * _number(a.technical.height)
                    < _number(b.technical.width) * _number(b.technical.height))
        if left.column() == 6:
            return str(left.data()).casefold() < str(right.data()).casefold()
        return a.name.casefold() < b.name.casefold()


class _DragView:
    """Shared native drag and scoped keyboard behavior for the two views."""
    panel: NativeFilesPanel

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        mime = self.panel.mime_data_for_selection()
        if mime.hasUrls():
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.CopyAction)

    def mouseMoveEvent(self, event: Any) -> None:
        super().mouseMoveEvent(event)
        if not event.buttons():
            self.panel.hover_index(self.indexAt(event.position().toPoint()))
        else:
            self.panel.clear_hover()

    def leaveEvent(self, event: Any) -> None:
        self.panel.clear_hover()
        super().leaveEvent(event)

    def keyPressEvent(self, event: Any) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.panel.copy_files()
            event.accept()
        elif event.matches(QKeySequence.StandardKey.Paste):
            self.panel.paste_files()
            event.accept()
        elif event.key() == Qt.Key.Key_Space:
            # Space 只做预览；Enter 仍走原来的打开语义（目录进入 / 文件打开）。
            self.panel.preview_selected()
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.panel.activate_current()
            event.accept()
        elif event.key() == Qt.Key.Key_Backspace:
            self.panel.go_up()
            event.accept()
        elif event.key() == Qt.Key.Key_Escape:
            if isinstance(self, FilesGalleryView):
                self.cancelSelectionDrag()
            self.panel.handle_escape()
            event.accept()
        elif event.key() == Qt.Key.Key_Delete:
            self.panel.request_trash_selection()
            event.accept()
        elif event.key() == Qt.Key.Key_F2:
            self.panel.request_rename()
            event.accept()
        else:
            super().keyPressEvent(event)


class FilesIconView(_DragView, FilesGalleryView):
    pass


class FilesDetailView(_DragView, QTreeView):
    pass


class NativeFilesPanel(QWidget):
    directory_requested = pyqtSignal(str)
    file_activated = pyqtSignal(str)
    legacy_requested = pyqtSignal()
    plugins_requested = pyqtSignal()
    plugin_center_requested = pyqtSignal()
    thumbnail_requested = pyqtSignal(str, int)
    thumbnail_released = pyqtSignal(str)
    thumbnail_dispatch_paused = pyqtSignal(bool)
    tree_requested = pyqtSignal(str)
    # paths, patch —— 只包含用户真正改过的字段（混合值不清空）
    metadata_patch_requested = pyqtSignal(list, dict, str)
    file_task_requested = pyqtSignal(str, list, dict)
    library_search_requested = pyqtSignal(str, int, int)
    directory_more_requested = pyqtSignal(str, int)  # D3：目录分页续页
    collections_requested = pyqtSignal()
    collection_requested = pyqtSignal(int, str)
    # D2：集合写入 + 回收站（意图经 transport，写入全部走 B/C 服务）
    collection_create_requested = pyqtSignal(str, list)
    collection_rename_requested = pyqtSignal(int, str)
    collection_add_members_requested = pyqtSignal(int, list)
    collection_remove_members_requested = pyqtSignal(int, list)
    collection_delete_requested = pyqtSignal(int)
    trash_requested = pyqtSignal()
    jobs_requested = pyqtSignal()  # D5：文件任务列表
    maintenance_requested = pyqtSignal()
    libraries_requested = pyqtSignal()
    library_pin_requested = pyqtSignal(str)
    library_unpin_requested = pyqtSignal(str)
    scan_requested = pyqtSignal()
    scan_cancel_requested = pyqtSignal()
    thumbnail_retry_requested = pyqtSignal(str, int)
    batch_rename_requested = pyqtSignal(list)
    compare_requested = pyqtSignal(list)
    strip_requested = pyqtSignal(str, float)
    preview_requested = pyqtSignal(str)
    # 音频/模型等尚未原生化的媒体仍交给既有查看器；图片/视频走原生浮层。
    preview_external_requested = pyqtSignal(str)
    preview_closed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None, *, home_path: str | None = None,
                 preferences: Any = None) -> None:
        super().__init__(parent)
        self.home_path = unicodedata.normalize("NFC", QDir.cleanPath(home_path or QDir.homePath()))
        self.current_path = ""
        self._requested_path = ""
        self._history: list[str] = []
        self._history_index = -1
        self._history_target: int | None = None
        self._ready = False
        self._images: OrderedDict[str, QImage] = OrderedDict()
        self._deferred_thumbnails: OrderedDict[str, bytes] = OrderedDict()
        self._thumbnail_delivery_timer = QTimer(self)
        self._thumbnail_delivery_timer.setInterval(16)
        self._thumbnail_delivery_timer.timeout.connect(self._deliver_deferred_thumbnail)
        self._density_timer = QTimer(self)
        self._density_timer.setSingleShot(True)
        self._density_timer.setInterval(32)
        self._density_timer.timeout.connect(self._apply_slider_density)
        self._list_layout_dirty = False
        self._thumbnail_pending: set[str] = set()
        self._thumbnail_failed: set[str] = set()
        self._entry_paths: set[str] = set()
        self._entry_rows: dict[str, int] = {}
        self._thumbnail_timers: dict[str, QTimer] = {}
        self._thumbnail_attempts: dict[str, int] = {}
        self._thumbnail_generation = 0
        self._sidebar_preference: bool | None = None
        self._inspector_preference: bool | None = None
        self._info_lock = False
        self._pending_patch: tuple[list[str], dict[str, Any]] | None = None
        self._tag_display = ""
        self._comment_display = ""
        self._library_active = False
        self._collection_active = False
        # D3 分页：当前视图的 total/truncated 与续页来源（offset=已消费条数）
        self._view_total: int = 0
        self._view_truncated: bool = False
        self._append_next_page: bool = False
        # [FR-08] 统一 Location：directory / library-search / collection。
        # 虚拟位置（搜索/集合）绝不"假装"还在上一个真实目录——目录写入类
        # 操作必须显式拒绝，而不是落到 _requested_path 指向的旧目录。
        self._view_location: dict[str, Any] = {"kind": "directory", "path": "",
                                               "writable": True}
        self._last_search_query = ""
        self._search_debounce = QTimer(self)  # [FR-09] 连续输入防抖
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(250)
        self._search_debounce.timeout.connect(self._emit_debounced_search)
        self._collections: list[dict[str, Any]] = []
        self._pending_collection_name = ""
        self._libraries: list[dict[str, Any]] = []
        self._scan_state: dict[str, Any] = {}
        self._strips: OrderedDict[str, QImage] = OrderedDict()
        self._hover_path = ""
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(350)
        self._hover_timer.timeout.connect(self._request_hover_strip)
        self.model = FilesEntryModel(self)
        self.proxy = FilesFilterModel(self)
        self.proxy.setSourceModel(self.model)
        self.selection = QItemSelectionModel(self.proxy, self)
        self._capabilities: dict[str, dict[str, Any]] = {}
        self._build_ui()
        self._prefs = preferences
        self._apply_preferences()
        self._preview_path = ""
        self._preview = NativeFilesPreview(
            self,
            bg=C.BG_DARKEST,
            fg=C.TEXT_PRIMARY,
            muted=C.TEXT_HINT,
        )
        self._preview_decoder = PreviewDecoder(self)
        self._video_host: NativePlayerHost | None = None
        self._preview_decoder.image_ready.connect(self._on_preview_image)
        self._preview_decoder.decode_failed.connect(self._on_preview_decode_failed)
        self._preview.closed.connect(self._on_preview_closed)
        self._preview.next_requested.connect(lambda: self._step_preview(1))
        self._preview.previous_requested.connect(lambda: self._step_preview(-1))
        self._preview.open_in_legacy_requested.connect(self.preview_external_requested)
        self.selection.selectionChanged.connect(self._update_info)
        self.proxy.rowsInserted.connect(self._update_info)
        self.proxy.rowsRemoved.connect(self._update_info)
        self.proxy.sort(0)

    def _preferences(self) -> "NativeFilesPreferences":
        """惰性创建：不注入时用真实 QSettings；测试注入临时实例避免污染本机。"""
        if self._prefs is None:
            from src.app.ui.native_files_prefs import NativeFilesPreferences
            self._prefs = NativeFilesPreferences()
        return self._prefs

    def _apply_preferences(self) -> None:
        """恢复上次的视图/密度/侧栏/信息栏/排序；不触发回写。"""
        prefs = self._preferences()
        with QSignalBlocker(self.view_combo):
            self.view_combo.setCurrentIndex(self.view_combo.findData(prefs.layout_mode))
        self.card_delegate.grid_fill = prefs.grid_fill
        self._change_layout(self.view_combo.currentIndex(), persist=False)
        with QSignalBlocker(self.density_slider):
            self.density_slider.setValue(max(self.density_slider.minimum(),
                                             min(self.density_slider.maximum(), prefs.density)))
        self._resize_cards(self.density_slider.value())
        with QSignalBlocker(self.sort_combo):
            self.sort_combo.setCurrentIndex(min(prefs.sort_index, self.sort_combo.count() - 1))
        self._sort_changed(self.sort_combo.currentIndex())
        self._sidebar_preference = prefs.sidebar_preference
        if self._sidebar_preference is not None:
            with QSignalBlocker(self.sidebar_button):
                self.sidebar_button.setChecked(self._sidebar_preference)
            self.sidebar.setVisible(self._sidebar_preference)
        self._inspector_preference = prefs.inspector_preference

    def _button(self, text: str, tooltip: str, callback: Any) -> QPushButton:
        button = QPushButton(text, self)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.clicked.connect(callback)
        return button

    def _toolbar_combo(self, label: str) -> ChevronComboBox:
        combo = ChevronComboBox(self.gallery_toolbar)
        combo.setAccessibleName(label)
        combo.setToolTip(label)
        combo.setStyleSheet(f"""
            QComboBox {{
                background: transparent; color: {C.TEXT_SECONDARY};
                border: 1px solid transparent; border-radius: 4px;
                padding: 2px 21px 2px 6px; min-height: 18px; font-size: 12px;
            }}
            QComboBox:hover, QComboBox:on {{ background: {C.BG_ACTIVE}; color: {C.TEXT_PRIMARY}; }}
            QComboBox:focus {{ border-bottom-color: {C.ORANGE}; }}
            QComboBox::drop-down {{ border: none; width: 0px; }}
            QComboBox::down-arrow {{ image: none; border: none; }}
        """)
        return combo

    def _build_ui(self) -> None:
        self.setObjectName("nativeFiles")
        self.setStyleSheet(f"""
            QWidget#nativeFiles, QWidget#filesGalleryToolbar {{ background: {C.BG_PANEL}; color: {C.TEXT_PRIMARY}; }}
            QLabel {{ background: transparent; color: {C.TEXT_SECONDARY}; }}
            QLabel#filesSection {{ color: {C.TEXT_PRIMARY}; font-size: 13px; font-weight: 600; }}
            QLabel#filesSidebarHeading {{ color: {C.TEXT_DIM}; font-size: 11px; font-weight: 600; padding-left: 5px; }}
            QLabel#filesHint {{ color: {C.TEXT_HINT}; font-size: 11px; }}
            QWidget#filesSidebar, QTreeView#filesHomeTree, QListWidget#filesFavorites {{ background: #222226; }}
            QWidget#filesInspector {{ background: {C.BG_DARKEST}; }}
            QScrollArea#filesInspectorScroll {{ background: transparent; border: none; }}
            QWidget#filesInspectorBody {{ background: {C.BG_DARKEST}; }}
            QAbstractItemView {{ background: transparent; color: {C.TEXT_PRIMARY}; border: none; outline: none; }}
            QTreeView#filesDetails::item {{ border: none; }}
            QTreeView#filesDetails {{ alternate-background-color: {C.BG_HOVER}; }}
            QTreeView#filesDetails::item:selected {{ background: {C.BG_ACTIVE}; color: {C.ORANGE}; }}
            QTreeView#filesHomeTree {{ show-decoration-selected: 0; }}
            QTreeView#filesHomeTree::item, QListWidget#filesFavorites::item {{ height: 24px; border: none; border-radius: 4px; }}
            QTreeView#filesHomeTree::item {{ border-left: 2px solid transparent; }}
            QTreeView#filesHomeTree::item:selected {{
                background: {C.BG_ACTIVE}; color: {C.TEXT_PRIMARY}; border-left-color: {C.ORANGE};
            }}
            QTreeView#filesHomeTree::item:hover:!selected, QListWidget#filesFavorites::item:hover:!selected {{ background: {C.BG_HOVER}; }}
            QListWidget#filesFavorites::item {{ padding-left: 5px; }}
            QListWidget#filesFavorites::item:selected {{ background: {C.BG_ACTIVE}; color: {C.ORANGE}; }}
            QHeaderView::section {{ background: {C.BG_PANEL}; color: {C.TEXT_HINT}; border: none; padding: 8px; font-size: 11px; }}
            QLineEdit {{ background: {C.BG_INPUT}; color: {C.TEXT_PRIMARY}; border: 1px solid {C.BORDER}; border-radius: 4px; padding: 3px 7px; }}
            QLineEdit:focus {{ border-color: {C.ORANGE_PRESS}; }}
            QPushButton {{ background: transparent; color: {C.TEXT_SECONDARY}; border: 1px solid transparent; border-radius: 5px; padding: 5px 9px; }}
            QPushButton[filesFavoriteAction="true"] {{ padding: 0; font-size: 15px; font-weight: 600; }}
            QPushButton:hover, QPushButton:checked {{ background: {C.BG_ACTIVE}; color: {C.TEXT_PRIMARY}; }}
            QPushButton[workspacePanelSwitch="true"]:checked,
            QPushButton[workspacePanelSwitch="true"]:hover {{
                background: transparent; color: {C.TEXT_PRIMARY};
            }}
            QPushButton[workspacePanelSwitch="true"]:focus {{ border-bottom-color: {C.TEXT_PRIMARY}; }}
            QPushButton:disabled {{ color: {C.TEXT_HINT}; }}
            QPushButton#filesBreadcrumb {{ text-align: left; color: {C.TEXT_SECONDARY}; }}
            QFrame#filesRule {{ background: {C.PANEL_DIVIDER}; border: none; }}
            QSlider::groove:horizontal {{ height: 3px; border: none; border-radius: 1px; background: {C.BORDER}; }}
            QSlider::handle:horizontal {{ background: {C.TEXT_SECONDARY}; border: none; width: 9px; margin: -3px 0; border-radius: 4px; }}
            QSlider::handle:horizontal:hover, QSlider::handle:horizontal:focus {{ background: {C.ORANGE}; }}
        """)
        root = QVBoxLayout(self)
        # A visible inspector must not impose its wide layout minimum on the
        # dock: let resizeEvent hide secondary panes as the user narrows it.
        root.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText(_tr("search", "搜索名称、标签或备注…"))
        self.search_edit.setAccessibleName(_tr("search_label", "搜索当前目录"))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._filter_changed)
        self.search_edit.setMinimumWidth(80)
        self.search_edit.setMaximumWidth(200)
        self.search_edit.setFixedHeight(26)
        self.search_edit.installEventFilter(self)
        self.search_edit.hide()
        navigation = QHBoxLayout()
        navigation.setContentsMargins(8, 4, 8, 4)
        navigation.setSpacing(3)
        self.sidebar_button = self._button(_tr("locations", "位置"), _tr("toggle_sidebar", "显示或隐藏位置栏"), self._toggle_sidebar)
        self.sidebar_button.setProperty("workspacePanelSwitch", True)
        self.sidebar_button.setCheckable(True)
        self.sidebar_button.setChecked(True)
        navigation.addWidget(self.sidebar_button)
        navigation.addSpacing(6)
        self.back_button = self._button("‹", _tr("back", "后退"), lambda: self._travel(-1))
        self.forward_button = self._button("›", _tr("forward", "前进"), lambda: self._travel(1))
        for button in (self.back_button, self.forward_button):
            button.setFixedWidth(28)
            navigation.addWidget(button)
        self.up_button = self._button("↑", _tr("up", "上一级"), self.go_up)
        navigation.addWidget(self.up_button)
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
        navigation.addWidget(self.search_edit)
        self.search_button = self._button("", _tr("search_label", "搜索当前目录"), self._toggle_search)
        self.search_button.setCheckable(True)
        self.search_button.setFixedSize(28, 26)
        search_pixmap = QPixmap(20, 20)
        search_pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(search_pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(C.TEXT_SECONDARY), 1.5))
        painter.drawEllipse(3, 3, 10, 10)
        painter.drawLine(12, 12, 17, 17)
        painter.end()
        self.search_button.setIcon(QIcon(search_pixmap))
        navigation.addWidget(self.search_button)
        self.library_button = self._button(
            _tr("library_scope", "全库"), _tr("library_tooltip", "在全部素材库中搜索"),
            self._library_toggle)
        self.library_button.setCheckable(True)
        self.library_button.setFixedHeight(26)
        navigation.addWidget(self.library_button)
        self.collections_button = self._button(
            _tr("collections", "集合"), _tr("collections_tip", "浏览素材集合"),
            self.request_collections)
        self.collections_button.setFixedHeight(26)
        navigation.addWidget(self.collections_button)
        self.trash_button = self._button(
            _tr("trash_bin", "回收站"), _tr("trash_bin_tip", "查看/恢复已删除的素材"),
            self.trash_requested.emit)
        self.trash_button.setFixedHeight(26)
        navigation.addWidget(self.trash_button)
        self.plugins_button = self._button(_tr("plugins", "插件"), _tr("plugins_tip", "打开插件启动器（Ctrl+Alt+P）"), self.plugins_requested.emit)
        navigation.addWidget(self.plugins_button)
        self.refresh_button = self._button("↻", _tr("refresh", "刷新"), self.refresh)
        navigation.addWidget(self.refresh_button)
        self._navigation_tools = (self.collections_button, self.trash_button,
                                  self.plugins_button, self.refresh_button)
        self.inspector_button = self._button(_tr("inspector_short", "信息"), _tr("toggle_inspector", "显示或隐藏文件信息"), self._toggle_inspector)
        self.inspector_button.setProperty("workspacePanelSwitch", True)
        self.inspector_button.setCheckable(True)
        navigation.addWidget(self.inspector_button)
        self.more_button = self._button("•••", _tr("more", "更多工具"), self._show_tools)
        navigation.addWidget(self.more_button)
        root.addLayout(navigation)
        root.addWidget(self._rule())
        self.splitter = PanelSplitter(Qt.Orientation.Horizontal, self)
        self.sidebar = QWidget(self.splitter)
        self.sidebar.setObjectName("filesSidebar")
        self.sidebar.setMinimumWidth(144)
        side_layout = QVBoxLayout(self.sidebar)
        side_layout.setContentsMargins(8, 8, 8, 6)
        side_layout.setSpacing(4)
        library_heading = QLabel(_tr("favorites_heading", "常用位置"))
        library_heading.setObjectName("filesSidebarHeading")
        library_heading.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        favorites_heading = QHBoxLayout()
        favorites_heading.setContentsMargins(0, 0, 0, 0)
        favorites_heading.setSpacing(2)
        favorites_heading.addWidget(library_heading, 1)
        pin_button = self._button("+", _tr("pin_tip", "把当前目录固定为常用位置"),
                                  self.pin_current_directory)
        pin_button.setAccessibleName(_tr("pin_here", "固定当前"))
        unpin_button = self._button("−", _tr("unpin_tip", "取消固定选中的常用位置"),
                                    self.unpin_selected_library)
        unpin_button.setAccessibleName(_tr("unpin", "移除"))
        for button in (pin_button, unpin_button):
            button.setProperty("filesFavoriteAction", True)
            button.setFixedSize(24, 24)
            favorites_heading.addWidget(button)
        side_layout.addLayout(favorites_heading)
        self.library_list = QListWidget(self.sidebar)
        self.library_list.setObjectName("filesFavorites")
        self.library_list.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.library_list.setAccessibleName(_tr("favorites_heading", "常用位置"))
        self.library_list.itemActivated.connect(self._open_list_path)
        self.library_list.itemClicked.connect(self._open_list_path)
        self.library_list.hide()
        side_layout.addWidget(self.library_list)
        directory_heading = QLabel(_tr("folders_heading", "文件夹"), self.sidebar)
        directory_heading.setObjectName("filesSidebarHeading")
        side_layout.addWidget(directory_heading)
        self.locations = HomeDirectoryTree(self.home_path, self._is_visible_entry, self.sidebar)
        self.locations.setMinimumWidth(112)
        self.locations.directory_requested.connect(self.navigate)
        self.locations.tree_requested.connect(self.tree_requested)
        side_layout.addWidget(self.locations, 1)
        content = QWidget(self.splitter)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        self.gallery_toolbar = QWidget(content)
        self.gallery_toolbar.setObjectName("filesGalleryToolbar")
        self.gallery_toolbar.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.gallery_toolbar_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self.gallery_toolbar)
        self.gallery_toolbar_layout.setContentsMargins(12, 5, 10, 5)
        self.gallery_toolbar_layout.setSpacing(6)
        self.gallery_title_row = QHBoxLayout()
        self.gallery_title_row.setSpacing(10)
        self.folder_title = QLabel(_tr("browse", "浏览素材"), self.gallery_toolbar)
        self.folder_title.setObjectName("filesSection")
        self.folder_title.setTextFormat(Qt.TextFormat.PlainText)
        self.folder_title.setMinimumWidth(0)
        self.folder_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.gallery_title_row.addWidget(self.folder_title, 1)
        size_label = _tr("thumbnail_size", "缩略图大小")
        self.density_label = QLabel(size_label, self.gallery_toolbar)
        self.density_label.setObjectName("filesHint")
        self.gallery_title_row.addWidget(self.density_label)
        self.density_slider = QSlider(Qt.Orientation.Horizontal, self.gallery_toolbar)
        self.density_slider.setRange(96, 320)
        self.density_slider.setValue(180)
        self.density_slider.setMinimumWidth(44)
        self.density_slider.setMaximumWidth(92)
        self.density_slider.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.density_slider.setAccessibleName(size_label)
        self.density_slider.setToolTip(size_label)
        self.density_slider.valueChanged.connect(self._density_changed)
        self.density_slider.sliderPressed.connect(self._begin_density_drag)
        self.density_slider.sliderReleased.connect(self._finish_density_drag)
        self.density_slider.installEventFilter(self)
        self.gallery_title_row.addWidget(self.density_slider)
        self.gallery_toolbar_layout.addLayout(self.gallery_title_row, 1)
        self.gallery_options = QHBoxLayout()
        self.gallery_options.setSpacing(2)
        self.gallery_options.addStretch()
        self.type_combo = self._toolbar_combo(_tr("type_filter", "文件类型筛选"))
        for label, kind in (("全部类型", "all"), ("图片", "image"), ("视频", "video"), ("音频", "audio"), ("文档", "doc")):
            self.type_combo.addItem(_tr(f"type.{kind}", label), kind)
        self.type_combo.currentIndexChanged.connect(self._filter_changed)
        self.gallery_options.addWidget(self.type_combo)
        self.flag_combo = self._toolbar_combo(_tr("flag_filter", "旗标筛选"))
        for label, flag in (("全部旗标", -1), ("已标旗", 1), ("已拒收", 2)):
            self.flag_combo.addItem(_tr(f"flag.{flag}", label), flag)
        self.flag_combo.currentIndexChanged.connect(self._filter_changed)
        self.gallery_options.addWidget(self.flag_combo)
        self.sort_combo = self._toolbar_combo(_tr("sort", "排序方式"))
        for label, key in (("名称 A–Z", "name"), ("最近修改", "date"), ("最大文件", "size"), ("最高评分", "rating")):
            self.sort_combo.addItem(_tr(f"sort.{key}", label))
        self.sort_combo.currentIndexChanged.connect(self._sort_changed)
        self.sort_combo.currentIndexChanged.connect(
            lambda index: setattr(self._preferences(), "sort_index", index))
        self.gallery_options.addWidget(self.sort_combo)
        self.view_combo = self._toolbar_combo(_tr("view", "文件视图"))
        for mode, label in (("adaptive", "自适应"), ("masonry", "瀑布流"),
                            ("grid", "网格"), ("list", "列表")):
            self.view_combo.addItem(_tr(f"layout.{mode}", label), mode)
        self.gallery_options.addWidget(self.view_combo)
        self.gallery_toolbar_layout.addLayout(self.gallery_options)
        self.gallery_toolbar.installEventFilter(self)
        content_layout.addWidget(self.gallery_toolbar)
        content_layout.addWidget(self._rule())
        self.pages = QStackedWidget(content)
        self.icon_view = FilesIconView(self.pages)
        self.icon_view.setMouseTracking(True)
        self.icon_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.card_delegate = FilesCardDelegate(
            FilesEntryModel.ENTRY_ROLE, self._image_for_entry, self.icon_view,
            strip_for_entry=self.strip_for_entry)
        self.icon_view.setItemDelegate(self.card_delegate)
        self.icon_view.setThumbnailSize(self.card_delegate.card_width)
        self.detail_view = FilesDetailView(self.pages)
        self.detail_view.setObjectName("filesDetails")
        self.detail_view.setRootIsDecorated(False)
        self.detail_view.setItemsExpandable(False)
        self.detail_view.setUniformRowHeights(True)
        self.detail_view.setAlternatingRowColors(True)
        self.list_delegate = FilesListDelegate(FilesEntryModel.ENTRY_ROLE, self._image_for_entry,
                                                self.detail_view)
        self.detail_view.setItemDelegate(self.list_delegate)
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
        self.detail_view.setColumnWidth(4, 80)
        self.detail_view.setColumnWidth(5, 110)
        self.detail_view.setColumnWidth(6, 65)
        self.detail_view.header().moveSection(self.detail_view.header().visualIndex(5), 1)
        self.detail_view.header().moveSection(self.detail_view.header().visualIndex(6), 2)
        self.view_combo.currentIndexChanged.connect(self._change_layout)
        content_layout.addWidget(self.pages, 1)
        # D3 分页续页：目录过大时按页追加，不截断也不假称全量
        self.load_more_button = QPushButton(
            _tr("load_more", "加载更多"), content)
        self.load_more_button.setObjectName("filesLoadMore")
        self.load_more_button.clicked.connect(self._load_more)
        self.load_more_button.hide()
        content_layout.addWidget(self.load_more_button)
        self.message = QLabel(content)
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.hide()
        content_layout.addWidget(self.message)
        self.inspector = QWidget(self.splitter)
        self.inspector.setObjectName("filesInspector")
        self.inspector.setMinimumWidth(200)
        inspector_layout = QVBoxLayout(self.inspector)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        inspector_layout.setSpacing(0)
        self.inspector_scroll = QScrollArea(self.inspector)
        self.inspector_scroll.setObjectName("filesInspectorScroll")
        self.inspector_scroll.setWidgetResizable(True)
        self.inspector_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inspector_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.inspector_body = QWidget(self.inspector_scroll)
        self.inspector_body.setObjectName("filesInspectorBody")
        info_layout = QVBoxLayout(self.inspector_body)
        info_layout.setContentsMargins(12, 10, 12, 14)
        info_layout.setSpacing(7)
        info_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.inspector_scroll.setWidget(self.inspector_body)
        inspector_layout.addWidget(self.inspector_scroll)
        info_heading = QLabel(_tr("inspector", "文件信息"))
        info_heading.setObjectName("filesSection")
        info_layout.addWidget(info_heading)
        info_layout.addWidget(self._rule())
        self.preview_cover = QLabel(self.inspector)
        self.preview_cover.setFixedHeight(180)
        self.preview_cover.setMinimumWidth(0)
        self.preview_cover.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.preview_cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_cover.installEventFilter(self)
        self.preview_cover.setStyleSheet(f"background: {C.BG_INPUT_DARK}; border-radius: 7px;")
        info_layout.addWidget(self.preview_cover)
        self.info = QLabel(_tr("select", "选择素材查看信息"), self.inspector)
        self.info.setObjectName("filesSection")
        self.info.setTextFormat(Qt.TextFormat.PlainText)
        self.info.setWordWrap(True)
        self.info.setMinimumWidth(0)
        self.info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        info_layout.addSpacing(6)
        info_layout.addWidget(self.info)
        self.info_path = QLabel(self.inspector)
        self.info_path.setObjectName("filesHint")
        self.info_path.setTextFormat(Qt.TextFormat.PlainText)
        self.info_path.setWordWrap(True)
        info_layout.addWidget(self.info_path)
        form = QFormLayout()
        self.technical_form = form
        form.setVerticalSpacing(6)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.info_fields: dict[str, QLabel] = {}
        for key, label in (("type", "类型"), ("size", "大小"), ("mtime", "修改日期"),
                           ("duration", "时长"), ("resolution", "分辨率"),
                           ("fps", "帧率"), ("codec", "编码")):
            value = QLabel("—", self.inspector)
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.info_fields[key] = value
            form.addRow(_tr(f"field.{key}", label), value)
        # 可编辑元数据：只提交用户真正改过的字段（混合值不清空）
        info_layout.addSpacing(4)
        meta_heading = QLabel(_tr("metadata", "标记"))
        meta_heading.setObjectName("filesSection")
        info_layout.addWidget(meta_heading)
        info_layout.addWidget(self._rule())
        meta_form = QFormLayout()
        meta_form.setVerticalSpacing(7)
        self.rating_combo = ChevronComboBox(self.inspector)
        self.rating_combo.setAccessibleName(_tr("rating", "评分"))
        for label, value in (("未评分", 0), ("1★", 1), ("2★", 2), ("3★", 3), ("4★", 4), ("5★", 5),
                             (_tr("mixed", "混合"), -1)):
            self.rating_combo.addItem(label, value)
        self.rating_combo.currentIndexChanged.connect(self._rating_changed)
        meta_form.addRow(_tr("rating", "评分"), self.rating_combo)
        self.color_combo = ChevronComboBox(self.inspector)
        self.color_combo.setAccessibleName(_tr("color_label", "颜色标签"))
        for label, value in (("无", 0), ("红", 1), ("黄", 2), ("绿", 3), ("蓝", 4), ("紫", 5),
                             (_tr("mixed", "混合"), -1)):
            self.color_combo.addItem(label, value)
        self.color_combo.currentIndexChanged.connect(self._color_changed)
        meta_form.addRow(_tr("color_label", "颜色标签"), self.color_combo)
        self.tag_edit = QLineEdit(self.inspector)
        self.tag_edit.setPlaceholderText(_tr("tags_hint", "逗号分隔，例如 采访, 夜景"))
        self.tag_edit.setAccessibleName(_tr("tags", "标签"))
        self.tag_edit.editingFinished.connect(self._tags_committed)
        meta_form.addRow(_tr("tags", "标签"), self.tag_edit)
        self.comment_edit = QLineEdit(self.inspector)
        self.comment_edit.setPlaceholderText(_tr("comment_hint", "备注"))
        self.comment_edit.setAccessibleName(_tr("comments", "备注"))
        self.comment_edit.editingFinished.connect(self._comment_committed)
        meta_form.addRow(_tr("comments", "备注"), self.comment_edit)
        info_layout.addLayout(meta_form)
        self.meta_status = QLabel("", self.inspector)
        self.meta_status.setObjectName("filesHint")
        self.meta_status.setTextFormat(Qt.TextFormat.PlainText)
        self.meta_status.setWordWrap(True)
        info_layout.addWidget(self.meta_status)
        self.info_tags = QLabel("", self.inspector)
        self.info_tags.hide()
        info_layout.addSpacing(8)
        technical_heading = QLabel(_tr("basic_info", "基本信息"), self.inspector_body)
        technical_heading.setObjectName("filesSection")
        info_layout.addWidget(technical_heading)
        info_layout.addWidget(self._rule())
        info_layout.addLayout(form)
        info_layout.addStretch()
        self.inspector.hide()
        self.splitter.setSizes([184, 680, 260])
        self.splitter.setStretchFactor(1, 1)
        root.addWidget(self.splitter, 1)
        root.addWidget(self._rule())
        footer = QHBoxLayout()
        footer.setContentsMargins(10, 4, 10, 4)
        self.status = QLabel(self)
        self.status.setObjectName("filesHint")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        footer.addWidget(self.status, 1)
        self.scan_label = QLabel(self)
        self.scan_label.setObjectName("filesHint")
        self.scan_label.setTextFormat(Qt.TextFormat.PlainText)
        self.scan_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.scan_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.scan_label.hide()
        footer.addWidget(self.scan_label, 1)
        self.scan_button = self._button(_tr("scan_start", "开始索引"), _tr("scan_cancel_tip", "取消本轮索引"), self.toggle_scan)
        self.scan_button.hide()
        footer.addWidget(self.scan_button)
        root.addLayout(footer)
        self._update_navigation()
        self._update_info()

    def _rule(self) -> QFrame:
        rule = QFrame(self)
        rule.setObjectName("filesRule")
        rule.setFixedHeight(1)
        return rule

    def _toggle_search(self, expanded: bool) -> None:
        with QSignalBlocker(self.search_button):
            self.search_button.setChecked(expanded)
        self.search_edit.setVisible(expanded)
        self._layout_navigation()
        if expanded:
            self.search_edit.setFocus()
            self.search_edit.selectAll()
        else:
            self.search_edit.clear()
            # 清空/收起搜索=放弃本次全库检索（新意图作废旧上下文）：
            # 退出搜索虚拟视图回目录，而不是留一份旧结果冒充当前视图
            if self.library_button.isChecked():
                with QSignalBlocker(self.library_button):
                    self.library_button.setChecked(False)
                if self._library_active:
                    self._library_active = False
                    self._view_location = {
                        "kind": "directory",
                        "path": self._requested_path or self.current_path,
                        "writable": True}
                    self.refresh()
            self.pages.currentWidget().setFocus()

    def _layout_navigation(self) -> None:
        compact_search = self.search_button.isChecked() and self.width() < 640
        self.path_stack.setVisible(not compact_search)
        self.sidebar_button.setVisible(not compact_search)
        self.inspector_button.setVisible(not compact_search)
        for button in self._navigation_tools:
            button.setVisible(self.width() >= 560)

    def _layout_gallery_toolbar(self) -> None:
        layout = self.gallery_toolbar_layout
        margins = layout.contentsMargins()
        available = self.gallery_toolbar.width() - margins.left() - margins.right()
        spacing = self.gallery_options.spacing()
        primary_width = self.type_combo.sizeHint().width() + self.view_combo.sizeHint().width() + spacing
        filter_width = primary_width + self.flag_combo.sizeHint().width() + spacing
        self.view_combo.setVisible(available >= primary_width)
        self.flag_combo.setVisible(available >= filter_width)
        self.sort_combo.setVisible(self.width() >= 800 and
                                   available >= filter_width + self.sort_combo.sizeHint().width() + spacing)
        zoom_width = (self.density_label.sizeHint().width() + self.density_slider.maximumWidth()
                      + 2 * self.gallery_title_row.spacing())
        self.density_label.setVisible(available >= zoom_width + 56)
        required = (layout.spacing() + self.gallery_title_row.minimumSize().width()
                    + self.gallery_options.sizeHint().width())
        # Keep enough space for the folder name before sharing a row with the controls.
        direction = (QBoxLayout.Direction.LeftToRight if available >= required + 72
                     else QBoxLayout.Direction.TopToBottom)
        if layout.direction() != direction:
            layout.setDirection(direction)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is getattr(self, "preview_cover", None) and event.type() == QEvent.Type.Resize:
            self._refresh_inspector_cover()
        if watched is getattr(self, "gallery_toolbar", None) and event.type() in (QEvent.Type.Resize, QEvent.Type.LayoutRequest):
            self._layout_gallery_toolbar()
        if watched is self.search_edit and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self._toggle_search(False)
            return True
        if watched is getattr(self, "density_slider", None) and event.type() == QEvent.Type.Hide:
            self.density_slider.setSliderDown(False)
        return super().eventFilter(watched, event)

    def _add_combo_menu(self, menu: QMenu, label: str, combo: ChevronComboBox) -> QMenu:
        submenu = menu.addMenu(label)
        for index in range(combo.count()):
            action = submenu.addAction(combo.itemText(index))
            action.setCheckable(True)
            action.setChecked(combo.currentIndex() == index)
            action.triggered.connect(lambda checked=False, i=index: combo.setCurrentIndex(i))
        return submenu

    def _show_tools(self) -> None:
        menu = QMenu(self)
        for button in self._navigation_tools:
            if button.isHidden():
                label = button.accessibleName() if button is self.refresh_button else button.text()
                menu.addAction(label, button.click)
        if menu.actions():
            menu.addSeparator()
        menu.addAction(_tr("choose", "选择文件夹"), self._choose_folder)
        recent_menu = menu.addMenu(_tr("recent_heading", "最近访问"))
        recent_menu.setToolTipsVisible(True)
        for path in self._preferences().history[:8]:
            name = os.path.basename(path.rstrip("/")) or path
            action = recent_menu.addAction(name.replace("&", "&&"),
                                           lambda checked=False, p=path: self.navigate(p))
            action.setToolTip(path)
        recent_menu.setEnabled(bool(recent_menu.actions()))
        scan_action = (_tr("scan_cancel_tip", "取消本轮索引") if self._scan_state.get("running")
                       else _tr("scan_start", "开始索引"))
        menu.addAction(scan_action, self.toggle_scan)
        menu.addAction(_tr("jobs", "文件任务…"), self.jobs_requested.emit)
        if self.flag_combo.isHidden():
            self._add_combo_menu(menu, self.flag_combo.accessibleName(), self.flag_combo)
        layout_menu = self._add_combo_menu(menu, _tr("layout_heading", "布局方式"), self.view_combo)
        fill_menu = layout_menu.addMenu(_tr("grid_fill_heading", "网格填充方式"))
        for fill, label in ((False, "适应"), (True, "填充")):
            action = fill_menu.addAction(_tr("grid_fill.fill" if fill else "grid_fill.fit", label))
            action.setCheckable(True)
            action.setChecked(self.card_delegate.grid_fill == fill)
            action.triggered.connect(lambda checked=False, value=fill: self._set_grid_fill(value))
        self._add_combo_menu(menu, self.sort_combo.accessibleName(), self.sort_combo)
        menu.addSeparator()
        menu.addAction(_tr("plugin_center", "素材插件中心…"), self.plugin_center_requested.emit)
        menu.addAction(_tr("maintenance", "素材库备份与恢复…"), self.maintenance_requested.emit)
        menu.exec(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))

    def _edit_path(self) -> None:
        self.path_stack.setCurrentIndex(1)
        self.path_edit.setFocus()
        self.path_edit.selectAll()

    def _update_path_label(self, path: str) -> None:
        display_path = "Home" + path[len(self.home_path):] if path == self.home_path or path.startswith(self.home_path + "/") else path
        parts = [part for part in display_path.replace("\\", "/").split("/") if part]
        label = "  ›  ".join(parts[-3:]) if parts else path
        self.breadcrumb_button.setText(label)
        self.breadcrumb_button.setToolTip(path)
        self.folder_title.setText(QDir(path).dirName() or path)
        self.path_stack.setCurrentIndex(0)

    def _toggle_sidebar(self, checked: bool) -> None:
        self._sidebar_preference = checked
        self.sidebar.setVisible(checked)
        self._preferences().sidebar_preference = checked

    def _toggle_inspector(self, checked: bool) -> None:
        self._inspector_preference = checked
        self.inspector.setVisible(checked)
        self._preferences().inspector_preference = checked
        if checked:
            self.splitter.setSizes([184, max(200, self.width() - 446), 260])

    def _sort_changed(self, index: int) -> None:
        column = (0, 3, 2, 4)[index]
        self.proxy.sort(column, Qt.SortOrder.AscendingOrder if index == 0 else Qt.SortOrder.DescendingOrder)

    def _change_layout(self, index: int, *, persist: bool = True) -> None:
        mode = str(self.view_combo.itemData(index) or "adaptive")
        previous = self.pages.currentWidget()
        page_index = 1 if mode == "list" else 0
        changing_page = self.pages.currentIndex() != page_index
        anchor = QModelIndex()
        if changing_page:
            if previous is self.icon_view:
                visible = self.icon_view.visibleIndexes()
                anchor = min(visible, key=lambda item: self.icon_view.visualRect(item).top()) if visible else anchor
            else:
                anchor = previous.indexAt(QPoint(4, 4))
        self.clear_hover()
        self.card_delegate.layout_mode = mode
        if mode != "list":
            self.icon_view.setThumbnailSize(self.card_delegate.card_width)
            self.icon_view.setLayoutMode(mode)
        self.pages.setCurrentIndex(page_index)
        if mode == "list":
            self._refresh_list_density()
        if changing_page and anchor.isValid():
            self.pages.currentWidget().scrollTo(anchor, QAbstractItemView.ScrollHint.PositionAtTop)
        if persist:
            self._preferences().layout_mode = mode

    def _set_grid_fill(self, fill: bool) -> None:
        self.card_delegate.grid_fill = fill
        self._preferences().grid_fill = fill
        self.icon_view.viewport().update()

    def _resize_cards(self, width: int) -> None:
        self.card_delegate.card_width = width
        row_height = max(36, min(68, round(width / 4)))
        if row_height != self.list_delegate.row_height:
            self.list_delegate.row_height = row_height
            self._list_layout_dirty = True
        if self.pages.currentWidget() is self.detail_view:
            self._refresh_list_density()
        else:
            self.icon_view.setThumbnailSize(width)

    def _refresh_list_density(self) -> None:
        if self._list_layout_dirty:
            self.detail_view.doItemsLayout()
            self._list_layout_dirty = False

    def _density_changed(self, width: int) -> None:
        if self.density_slider.isSliderDown():
            # A full relayout of a large result set can exceed one frame.
            limit = 1000 if self.pages.currentWidget() is self.detail_view else 4000
            if self.proxy.rowCount() < limit and not self._density_timer.isActive():
                self._density_timer.start()
            return
        self._resize_cards(width)
        self._preferences().density = width

    def _apply_slider_density(self) -> None:
        self._resize_cards(self.density_slider.value())

    def _begin_density_drag(self) -> None:
        self.clear_hover()
        self._thumbnail_delivery_timer.stop()
        self.thumbnail_dispatch_paused.emit(True)

    def _finish_density_drag(self) -> None:
        self._density_timer.stop()
        self._apply_slider_density()
        prefs = self._preferences()
        if prefs.density != self.density_slider.value():
            prefs.density = self.density_slider.value()
        if self._deferred_thumbnails:
            self._thumbnail_delivery_timer.start()
        else:
            self.thumbnail_dispatch_paused.emit(False)
        self.pages.currentWidget().viewport().update()
        if self.inspector.isVisible():
            self._refresh_inspector_cover()

    def _deliver_deferred_thumbnail(self) -> None:
        path, data = self._deferred_thumbnails.popitem(last=False)
        self.set_thumbnail(path, data)
        if not self._deferred_thumbnails:
            self._thumbnail_delivery_timer.stop()
            self.thumbnail_dispatch_paused.emit(False)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if not hasattr(self, "sidebar"):
            return
        show_sidebar = self._sidebar_preference if self._sidebar_preference is not None else self.width() >= 720
        self.sidebar.setVisible(show_sidebar)
        self._layout_gallery_toolbar()
        self._layout_navigation()
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
            self.splitter.setSizes([184, max(240, self.width() - 446), 260])

    def _image_for_entry(self, entry: Any) -> QImage | None:
        if not isinstance(entry, Entry):
            entry = parse_entry(entry)
        path = entry.path
        image = self._images.get(path)
        if image is not None:
            self._images.move_to_end(path)
            return image
        if (self._ready and not self.density_slider.isSliderDown()
                and entry.kind in ("image", "video", "doc")
                and path not in self._thumbnail_pending and path not in self._thumbnail_failed):
            self._thumbnail_pending.add(path)
            self.thumbnail_requested.emit(path, entry.thumb_version)
        return None

    def set_thumbnail(self, path: str, data: bytes) -> None:
        if not self._ready or path not in self._entry_paths:
            return
        if self.density_slider.isSliderDown():
            self._deferred_thumbnails[path] = data
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
        self.icon_view.setAspectRatio(path, image.width() / image.height())
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
        view = self.pages.currentWidget()
        view.viewport().update(view.visualRect(index))
        if self.selection.isSelected(index):
            self._refresh_inspector_cover()

    def _thumbnail_capacity(self) -> int:
        grid = self.icon_view.gridSize()
        if not self.icon_view.isVisible() or grid.width() <= 0 or grid.height() <= 0:
            return 128
        viewport = self.icon_view.viewport()
        return max(128, math.ceil(viewport.width() / grid.width()) * (math.ceil(viewport.height() / grid.height()) + 2) + 16)

    def _entry_visible(self, path: str) -> bool:
        if self.inspector.isVisible() and any(entry.path == path for entry in self._selected_entries()):
            return True
        row = self._entry_rows.get(path)
        if row is None:
            return False
        index = self.proxy.mapFromSource(self.model.index(row, 0))
        view = self.pages.currentWidget()
        return (index.isValid() and view.isVisible()
                and view.visualRect(index).intersects(view.viewport().rect()))

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
        self.icon_view.clearAspectRatios()
        self._thumbnail_pending.clear()
        self._thumbnail_failed.clear()
        self._deferred_thumbnails.clear()
        if self._thumbnail_delivery_timer.isActive():
            self._thumbnail_delivery_timer.stop()
            self.thumbnail_dispatch_paused.emit(False)

    def thumbnail_failed(self, path: str) -> None:
        self._stop_thumbnail_timer(path)
        self._thumbnail_pending.discard(path)
        if path in self._entry_paths:
            self._thumbnail_failed.add(path)

    @staticmethod
    def _path_key(path: str) -> str:
        return unicodedata.normalize("NFC", QDir.cleanPath(path.replace("\\", "/"))).rstrip("/").casefold()

    def is_browsable_path(self, path: str) -> bool:
        """Presentation policy, not a replacement for service permissions."""
        key = self._path_key(path)
        home = self._path_key(self.home_path)
        if not key or key in ("/", ".", "/volumes") or home.startswith(key + "/"):
            return False
        system_roots = ("/Applications", "/System", "/Library", "/bin", "/sbin", "/usr", "/private",
                        "/etc", "/var", "/dev", "/proc", "/sys", "/opt",
                        "/Volumes/Macintosh HD", self.home_path + "/Library", self.home_path + "/Applications")
        if not key.startswith("/run/media/"):
            system_roots += ("/run",)
        if any(key == self._path_key(root) or key.startswith(self._path_key(root) + "/") for root in system_roots):
            return False
        # Keep Windows system locations out of native media browsing too.
        if len(key) >= 3 and key[1:3] == ":/":
            suffix = key[3:]
            if any(suffix == root or suffix.startswith(root + "/") for root in ("windows", "program files", "program files (x86)", "programdata", "$recycle.bin")):
                return False
        return not any(part.endswith(".app") for part in key.split("/"))

    def _is_visible_entry(self, entry: Any) -> bool:
        """UI 展示策略：隐藏文件 + 系统位置；不替代服务端权限/软锁。"""
        try:
            item = entry if isinstance(entry, Entry) else parse_entry(entry)
        except FilesError:
            return False
        return not item.name.startswith(".") and self.is_browsable_path(item.path)

    def start_home_tree(self) -> None:
        self.locations.start()

    def navigate(self, path: str) -> None:
        if not path or not path.strip():
            return
        if "://" in path:
            self.message.setText(_tr("virtual", "此位置不能作为普通目录打开，请使用集合或搜索入口。"))
            self.message.show()
            return
        if not self.is_browsable_path(path):
            self.message.setText(_tr("system_location", "此位置不用于素材浏览，请选择 Home 或素材文件夹。"))
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
        if path and self._path_key(path) != self._path_key(self.home_path):
            parent = QDir(path)
            if parent.cdUp() and self.is_browsable_path(parent.absolutePath()):
                self.navigate(parent.absolutePath())

    def refresh(self) -> None:
        """[FR-08/09] 按当前位置重放：目录重新列出；搜索/集合按原请求重发。

        虚拟视图此前刷新会退回上一个真实目录——位置身份被偷换。
        """
        kind = self._view_location.get("kind")
        if kind == "library-search":
            query = self._view_location.get("query") or ""
            if query:
                self.library_search_requested.emit(query, 200, 0)
            return
        if kind == "collection":
            collection_id = self._view_location.get("collection_id")
            if collection_id is not None:
                self.collection_requested.emit(int(collection_id),
                                               str(self._view_location.get("name") or ""))
            return
        path = self._requested_path or self.current_path
        if path:
            self.locations.refresh(path)
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
        self._view_total = 0
        self._view_truncated = False
        self._append_next_page = False
        self.load_more_button.hide()
        self.path_edit.setText(path)
        self._update_path_label(path)
        self.model.set_entries([])
        self.pages.setEnabled(False)
        self.message.setText(_tr("loading", "正在读取目录…"))
        self.message.show()
        self._update_info()

    def set_entries(self, path: str, entries: list[Any]) -> None:
        if not self.is_browsable_path(path):
            self.show_error(path, _tr("system_location", "此位置不用于素材浏览，请选择 Home 或素材文件夹。"))
            return
        self._view_total = 0
        self._view_truncated = False
        self._append_next_page = False
        try:
            parsed = [entry if isinstance(entry, Entry) else parse_entry(entry) for entry in entries]
        except FilesError as error:
            self.show_error(path, error.message)
            return
        entries = [entry for entry in parsed if self._is_visible_entry(entry)]
        if self._history_target is not None and self._history[self._history_target] == path:
            self._history_index = self._history_target
        elif path != self.current_path:
            self._history = self._history[:self._history_index + 1] + [path]
            self._history_index = len(self._history) - 1
        self._history_target = None
        self.current_path = self._requested_path = path
        self.path_edit.setText(path)
        self._update_path_label(path)
        try:
            self._preferences().remember_location(path)
        except Exception:
            pass
        self._clear_thumbnail_work()
        if self._preview.is_open and (self._preview_path not in {entry.path for entry in entries}):
            # 目录切换后旧图已不属于当前目录：关浮层，别留一张对不上路径的图
            self.close_preview()
        self._entry_paths = {entry.path for entry in entries}
        self._entry_rows = {entry.path: row for row, entry in enumerate(entries)}
        self._ready = True
        # [FR-08] 目录位置：真实、可写；退出虚拟视图后恢复本地过滤语义
        self._view_location = {"kind": "directory", "path": path, "writable": True}
        self._library_active = False
        self._collection_active = False
        self.model.set_entries(entries)
        self.pages.setEnabled(True)
        self.message.setText("" if entries else _tr("empty", "此文件夹为空。"))
        self.message.setVisible(not entries)
        self.locations.reveal(path)
        self._update_navigation()
        self._update_info()

    # ------------------------------------------------------------ 分页（D3）
    def set_page_meta(self, path: str, total: int, truncated: bool) -> None:
        """目录分页元信息（transport page_meta）：只认当前导航目标。"""
        if path not in (self._requested_path, self.current_path):
            return  # 晚到的旧目录：视图已换，不覆盖
        self._view_total = int(total)
        self._view_truncated = bool(truncated)
        self._update_page_status()

    def show_directory_page(self, path: str, entries: list[Any]) -> None:
        """目录页送达：首页重建视图；续页增量追加（选择/滚动保留）。"""
        if self._append_next_page and path == self.current_path:
            self._append_next_page = False
            parsed = [entry if isinstance(entry, Entry) else parse_entry(entry)
                      for entry in entries]
            self.model.append_entries(parsed)
            fresh_paths = {entry.path for entry in parsed}
            self._entry_paths.update(fresh_paths)
            for offset, entry in enumerate(parsed, start=len(self._entry_rows)):
                self._entry_rows[entry.path] = offset
            self._update_page_status()
            return
        self.set_entries(path, entries)

    def _update_page_status(self) -> None:
        shown = len(self.model.entries)
        kind = self._view_location.get("kind")
        if not self._view_truncated or kind not in ("directory", "library-search"):
            self.load_more_button.hide()
            return
        remaining = max(self._view_total - shown, 0)
        self.load_more_button.setText(
            _tr("load_more_with", "加载更多（还有 {n} 项）").format(n=remaining))
        self.load_more_button.show()

    def _load_more(self) -> None:
        """续页意图：offset = 已消费条数（模型累计，过滤不影响）。"""
        kind = self._view_location.get("kind")
        offset = len(self.model.entries)
        if kind == "directory":
            target = self._requested_path or self.current_path
            if not target or self._append_next_page:
                return
            self._append_next_page = True
            self.directory_more_requested.emit(target, offset)
        elif kind == "library-search" and self._last_search_query:
            self._append_next_page = True
            self.library_search_requested.emit(self._last_search_query, 200, offset)

    def show_error(self, path: str, message: str) -> None:
        self.set_loading(path)
        self._history_target = None
        self.message.setText(message)
        self._update_navigation()

    def _update_navigation(self) -> None:
        self.back_button.setEnabled(self._history_index > 0)
        self.forward_button.setEnabled(0 <= self._history_index < len(self._history) - 1)
        path = self.current_path or self._requested_path
        parent = QDir(path or self.home_path)
        self.up_button.setEnabled(bool(path) and self._path_key(path) != self._path_key(self.home_path)
                                  and parent.cdUp() and self.is_browsable_path(parent.absolutePath()))

    def _filter_changed(self, *_: Any) -> None:
        text = self.search_edit.text()
        # [FR-10] 本地整句 substring 过滤只用于目录视图；搜索/集合结果
        # 的查询语义在服务端，二次过滤会丢跨字段命中
        if self._view_location.get("kind") == "directory":
            self.proxy.query = text.casefold().strip()
        else:
            self.proxy.query = ""
        self.proxy.kind = self.type_combo.currentData()
        self.proxy.flag = int(self.flag_combo.currentData())
        self.proxy.invalidateFilter()
        self._update_info()
        # [FR-09] 连续输入防抖：每次按键立即全库查询会堆在途请求，
        # 旧响应与新结果竞争覆盖
        if self.library_button.isChecked() and text.strip():
            self._last_search_query = text.strip()
            self._search_debounce.start()

    def _emit_debounced_search(self) -> None:
        query = self._last_search_query.strip()
        text = self.search_edit.text().strip()
        if not query or not self.library_button.isChecked():
            return
        if text != query:
            self._last_search_query = text  # 防抖期间输入又变了：以最新为准
            self._search_debounce.start()
            return
        self.library_search_requested.emit(query, 200, 0)

    def _selected_entries(self) -> list[Entry]:
        return [index.data(FilesEntryModel.ENTRY_ROLE)
                for index in sorted(self.selection.selectedRows(0), key=lambda index: index.row())]

    def selected_file_urls(self) -> list[QUrl]:
        if not self._ready:
            return []
        return [QUrl.fromLocalFile(entry.path) for entry in self._selected_entries() if not entry.is_dir]

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
            self._describe_single(entries[0])
        else:
            self._describe_many(entries)
        self._sync_inspector_visibility()

    def _describe_single(self, entry: Entry) -> None:
        self._set_technical_rows({entry.kind})
        self.info.setText(entry.name)
        self.info_path.setText(self.fontMetrics().elidedText(entry.path, Qt.TextElideMode.ElideMiddle, 205))
        self.info_path.setToolTip(entry.path)
        technical = entry.technical
        field_values = {
            "type": _kind_label(entry.kind),
            "size": entry.size_label,
            "mtime": entry.mtime_label or "—",
            "duration": technical.duration_label if entry.has_known_media else "—",
            "resolution": technical.resolution or "—",
            "fps": technical.fps_label if technical.fps else "—",
            "codec": technical.codec or "—",
        }
        for key, label in self.info_fields.items():
            label.setText(field_values.get(key, "—"))
        availability = entry.availability
        if availability == Availability.OFFLINE:
            self.info_path.setText(_tr("offline_hint", "磁盘上已失联，仅保留索引记录"))
        self._refresh_inspector_cover()
        self._fill_metadata_controls([entry])

    def _refresh_inspector_cover(self) -> None:
        """只缩放选中项封面，分隔线/窗口变化不回填正在编辑的元数据。"""
        entries = self._selected_entries()
        if len(entries) != 1:
            self.preview_cover.clear()
            return
        entry = entries[0]
        image = self._image_for_entry(entry) if self._ready else None
        if image is not None:
            cover_size = self.preview_cover.contentsRect().size()
            self.preview_cover.setPixmap(QPixmap.fromImage(image).scaled(cover_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            source = self.model.index(self._entry_rows.get(entry.path, 0), 0)
            pixmap = source.data(Qt.ItemDataRole.DecorationRole)
            self.preview_cover.setPixmap(pixmap.pixmap(40, 40) if pixmap else QPixmap())

    def _describe_many(self, entries: list[Entry]) -> None:
        self._set_technical_rows({entry.kind for entry in entries})
        if not entries:
            self.info.setText(_tr("select", "选择素材查看信息"))
            self.info_path.setText("")
            self.preview_cover.clear()
            for label in self.info_fields.values():
                label.setText("—")
            self._enable_metadata_controls(False)
            return
        self.info.setText(_tr("selected", "已选择 {count} 项").format(count=len(entries)))
        folders = sum(1 for entry in entries if entry.is_dir)
        media = [entry for entry in entries if entry.has_known_media]
        total_bytes = sum(entry.size_bytes or 0 for entry in entries if not entry.is_dir)
        detail = _tr("multi_detail", "{files} 个文件 · {size}").format(
            files=len(entries) - folders, size=format_bytes(total_bytes) if total_bytes else "—")
        if folders:
            detail += _tr("multi_folders", " · {count} 个文件夹").format(count=folders)
        self.info_path.setText(detail)
        self.info_path.setToolTip("\n".join(entry.path for entry in entries[:40]))
        if media:
            seconds = sum(entry.technical.duration_seconds or 0 for entry in media)
            self.info_fields["duration"].setText(format_duration(seconds) if seconds else "—")
        else:
            self.info_fields["duration"].setText("—")
        for key in ("type", "size", "mtime", "resolution", "fps", "codec"):
            self.info_fields[key].setText("—")
        self.info_fields["type"].setText(_tr("multi_types", "{count} 种类型").format(
            count=len({entry.kind for entry in entries})))
        self.info_fields["size"].setText(format_bytes(total_bytes) if total_bytes else "—")
        self.preview_cover.clear()
        self._fill_metadata_controls(entries)

    def _set_technical_rows(self, kinds: set[str]) -> None:
        """只展示当前素材适用的技术信息，照片不占用时长/帧率空行。"""
        for key, applicable in (("duration", {"video", "audio"}),
                                ("resolution", {"image", "video"}),
                                ("fps", {"video"}),
                                ("codec", {"image", "video", "audio"})):
            self.technical_form.setRowVisible(self.info_fields[key], bool(kinds & applicable))

    def _enable_metadata_controls(self, enabled: bool) -> None:
        """[FR-12] 启用条件 = 有选择 **且** 服务能力可用，二者缺一不可。

        此前能力清单禁用后，选择变化/信息回填又按"有选择"重新启用，
        提交入口也不复核——用户会看到能改的控件，保存却永远失败。
        """
        allowed = enabled and self._capability_available("metadata.patch")
        for widget in (self.rating_combo, self.color_combo, self.tag_edit, self.comment_edit):
            widget.setEnabled(allowed)
        if not self._capability_available("metadata.patch"):
            reason = self._capability_reason("metadata.patch")
            self.meta_status.setText(reason or _tr("metadata_unavailable", "当前服务不支持保存标记"))

    def _fill_metadata_controls(self, entries: list[Entry]) -> None:
        """回填不触发写入：整体加锁，避免程序化回填再次发起元数据提交。"""
        self._enable_metadata_controls(bool(entries))
        if not entries:
            self._tag_display = self._comment_display = ""
            return
        ratings = {entry.user.rating for entry in entries}
        colors = {entry.user.color for entry in entries}
        tags = {entry.user.tag_text for entry in entries}
        comments = {entry.user.comments for entry in entries}
        self._info_lock = True
        try:
            self._select_data(self.rating_combo, next(iter(ratings)) if len(ratings) == 1 else -1)
            self._select_data(self.color_combo, next(iter(colors)) if len(colors) == 1 else -1)
            if len(tags) == 1:
                self._tag_display = next(iter(tags))
                self.tag_edit.setText(self._tag_display)
                self.tag_edit.setPlaceholderText(_tr("tags_hint", "逗号分隔，例如 采访, 夜景"))
            else:
                self._tag_display = ""
                self.tag_edit.clear()
                self.tag_edit.setPlaceholderText(_tr("mixed_tags", "混合标签，输入以统一"))
            if len(comments) == 1:
                self._comment_display = next(iter(comments))
                self.comment_edit.setText(self._comment_display)
                self.comment_edit.setPlaceholderText(_tr("comment_hint", "备注"))
            else:
                self._comment_display = ""
                self.comment_edit.clear()
                self.comment_edit.setPlaceholderText(_tr("mixed_comments", "混合备注，输入以统一"))
        finally:
            self._info_lock = False

    @staticmethod
    def _select_data(combo: ChevronComboBox, value: int) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _rating_changed(self, _index: int) -> None:
        if self._info_lock:
            return
        value = self.rating_combo.currentData()
        if value is None or int(value) < 0:
            return
        self._submit_patch({"rating": int(value)})

    def _color_changed(self, _index: int) -> None:
        if self._info_lock:
            return
        value = self.color_combo.currentData()
        if value is None or int(value) < 0:
            return
        self._submit_patch({"color": int(value)})

    def _tags_committed(self) -> None:
        if self._info_lock:
            return
        text = self.tag_edit.text().strip()
        if not text and not self._has_single_tag_value():
            # 混合值 + 空输入：用户没有表达"清空"，不写。
            return
        if text == self._tag_display:
            return
        self._submit_patch({"tags": text})

    def _comment_committed(self) -> None:
        if self._info_lock:
            return
        text = self.comment_edit.text()
        if text == self._comment_display:
            return
        self._submit_patch({"comments": text})

    def _has_single_tag_value(self) -> bool:
        entries = self._selected_entries()
        return bool(entries) and len({entry.user.tag_text for entry in entries}) == 1

    def _submit_patch(self, patch: dict[str, Any]) -> None:
        entries = self._selected_entries()
        if not entries or not patch:
            return
        # [FR-12] 提交入口复核能力：禁用状态下的写入请求一律不发
        if not self._capability_available("metadata.patch"):
            self._enable_metadata_controls(True)  # 复用其中的禁用+原因提示
            return
        paths = [entry.path for entry in entries]
        self._pending_patch = (paths, dict(patch))
        self.meta_status.setText(_tr("saving", "正在保存…"))
        # [FR-11] 标签显式 replace 语义：输入即最终值（空输入=清空），
        # 服务返回最终快照后按快照回显
        tags_op = "clear" if ("tags" in patch and not str(patch.get("tags") or "").strip()) \
            else "replace"
        self.metadata_patch_requested.emit(paths, dict(patch), tags_op)

    def apply_metadata_snapshot(self, snapshots: dict[str, dict[str, Any]]) -> None:
        """[FR-11] 服务端最终快照回显：模型与控件都按落库状态更新。

        此前 UI 提交后显示自己的推算值（替换），服务实际追加——刷新后
        标签"又变回去"。现在 UI 不推算，一律应用服务快照。
        """
        if not isinstance(snapshots, dict):
            return
        touched = False
        for path, snapshot in snapshots.items():
            if not isinstance(snapshot, dict):
                continue
            patch = {key: snapshot[key] for key in
                     ("tags", "rating", "comments", "color", "flag") if key in snapshot}
            if not patch:
                continue
            self.model.apply_patch([str(path)], patch)
            touched = True
        if touched:
            selected = self._selected_entries()
            if selected:
                self._fill_metadata_controls(selected)

    def apply_metadata_result(self, paths: list[str], patch: dict[str, Any], message: str = "") -> None:
        """服务确认后才更新模型；失败不留下"已保存"的假像。"""
        self._pending_patch = None
        self.model.apply_patch(paths, patch)
        self.meta_status.setText(message or _tr("saved", "已保存"))
        self._update_info()

    def metadata_failed(self, paths: list[str], patch: dict[str, Any], message: str) -> None:
        self._pending_patch = None
        self.meta_status.setText(_tr("save_failed", "保存失败：{message}").format(message=message))
        self._fill_metadata_controls(self._selected_entries())

    def preview_selected(self) -> None:
        """Space / 预览入口：图片与视频走原生浮层，其余媒体暂交旧查看器。"""
        entries = self._selected_entries()
        if len(entries) == 1 and self._ready:
            entry = entries[0]
            if entry.kind == "image" and entry.availability != Availability.OFFLINE:
                self._start_preview(entry)
                return
            if entry.kind == "video" and entry.availability != Availability.OFFLINE:
                self._start_video_preview(entry)
                return
            if entry.has_known_media:
                # 音频/模型：音频页在 M2 接入；模型仍交给既有查看器。
                self.preview_external_requested.emit(entry.path)
                return
            if entry.is_dir:
                self.navigate(entry.path)
                return
        self.activate_current()

    # -- U03 预览会话 --------------------------------------------------------
    def _cache_opened_thumbnail(self, entry: Entry) -> None:
        if entry.path in self._entry_paths:
            self._image_for_entry(entry)
        elif entry.kind in ("image", "video", "doc"):
            self.thumbnail_requested.emit(entry.path, entry.thumb_version)

    def _start_preview(self, entry: Entry) -> None:
        if self._video_host is not None:
            self._video_host.stop()
        self._preview_path = entry.path
        self._preview.open_for(entry.path, entry.name)
        self._preview.set_steppers_enabled(len(self._previewable_paths()) > 1)
        self.preview_requested.emit(entry.path)
        self._cache_opened_thumbnail(entry)

    def open_path_native(self, path: str) -> bool:
        """Open supported media through the registered native preview host."""
        entry = self._entry_for_path(path)
        if entry is None:
            # Vault results can be outside the current page. The reader still
            # validates the path and reports missing or unsupported media.
            from src.features.files.services.query import classify
            name = os.path.basename(path)
            if not os.path.isabs(path) or not name:
                return False
            entry = Entry(path=path, name=name, kind=classify(name))
        if entry.availability == Availability.OFFLINE:
            return False
        if entry.kind == "image":
            self._start_preview(entry)
            return True
        if entry.kind == "video":
            self._start_video_preview(entry)
            return True
        return False

    def _start_video_preview(self, entry: Entry) -> None:
        """视频预览走应用内共享播放器，不再切回旧 Files WebView。"""
        if self._video_host is not None:
            self._video_host.stop()
        if self._preview.is_open:
            self._preview.close_preview()
        self._preview_path = entry.path
        self._preview.open_video(entry.path, entry.name)
        host = self._ensure_video_host()
        host.play(entry.path)
        self._cache_opened_thumbnail(entry)

    def _ensure_video_host(self) -> NativePlayerHost:
        if self._video_host is None:
            host = NativePlayerHost(self._preview.video_slot, self)
            host.closed.connect(self._on_video_player_closed)
            host.prev_requested.connect(lambda: self._step_video_preview(-1))
            host.next_requested.connect(lambda: self._step_video_preview(1))
            host.failed.connect(self._on_video_player_failed)
            host.notice.connect(lambda text: self.status.setText(str(text)))
            self._video_host = host
        return self._video_host

    def _on_video_player_closed(self) -> None:
        if self._preview.is_open:
            self._preview.close_preview()

    def _on_video_player_failed(self, message: str) -> None:
        self.status.setText(str(message or "视频播放失败"))
        if self._preview.is_open:
            self._preview.show_message(
                self._preview_path, str(message or "视频播放失败"),
                open_in_legacy=False)

    def _video_paths(self) -> list[str]:
        paths: list[str] = []
        for row in range(self.proxy.rowCount()):
            index = self.proxy.mapToSource(self.proxy.index(row, 0))
            if not index.isValid():
                continue
            entry = self.model.entries[index.row()]
            if entry.kind == "video" and entry.availability != Availability.OFFLINE:
                paths.append(entry.path)
        return paths

    def _step_video_preview(self, delta: int) -> None:
        paths = self._video_paths()
        if not paths:
            return
        try:
            index = paths.index(self._preview_path)
        except ValueError:
            index = 0
        target = paths[(index + delta) % len(paths)]
        entry = self._entry_for_path(target)
        if entry is not None:
            self._select_path(target)
            self._start_video_preview(entry)

    def _select_path(self, path: str) -> None:
        row = self._entry_rows.get(path)
        if row is None:
            return
        index = self.proxy.mapFromSource(self.model.index(row, 0))
        if index.isValid():
            self.selection.select(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows)
            self.selection.setCurrentIndex(
                index, QItemSelectionModel.SelectionFlag.NoUpdate)

    def show_preview_bytes(self, path: str, data: bytes) -> None:
        """transport 拿到原始字节后交给解码线程；与当前会话不匹配一律丢弃。"""
        if not self._preview.is_open or path != self._preview_path:
            return
        self._preview_decoder.start(path, data)

    def preview_load_failed(self, path: str, message: str) -> None:
        if not self._preview.is_open or path != self._preview_path:
            return
        self._preview.show_message(path, message)

    def _on_preview_image(self, path: str, image: Any) -> None:
        if path != self._preview_path or not self._preview.is_open:
            return
        self._preview.show_image(path, image)

    def _on_preview_decode_failed(self, path: str, message: str) -> None:
        if path != self._preview_path or not self._preview.is_open:
            return
        self._preview.show_message(path, f"无法解码：{message}")

    def _on_preview_closed(self) -> None:
        self._preview_path = ""
        self._preview_decoder.cancel()
        self.preview_closed.emit()

    def close_preview(self) -> None:
        if self._video_host is not None:
            self._video_host.stop()
        if self._preview.is_open:
            self._preview.close_preview()

    def _previewable_paths(self) -> list[str]:
        """可见顺序里的图片集合：前后切换只在能真预览的条目之间走。"""
        paths: list[str] = []
        for row in range(self.proxy.rowCount()):
            index = self.proxy.mapToSource(self.proxy.index(row, 0))
            if not index.isValid():
                continue
            entry = self.model.entries[index.row()]
            if entry.kind == "image" and entry.availability != Availability.OFFLINE:
                paths.append(entry.path)
        return paths

    def _step_preview(self, delta: int) -> None:
        paths = self._previewable_paths()
        if not paths:
            return
        try:
            index = paths.index(self._preview_path)
        except ValueError:
            index = 0
        target = paths[(index + delta) % len(paths)]
        entry = self._entry_for_path(target)
        if entry is not None:
            self._start_preview(entry)

    def _entry_for_path(self, path: str) -> Entry | None:
        row = self._entry_rows.get(path)
        if row is None or row >= len(self.model.entries):
            return None
        return self.model.entries[row]

    # -- B01 能力清单 --------------------------------------------------------
    def apply_capabilities(self, items: list[Any]) -> None:
        """按服务声明的能力启用/禁用标记控件，不做"看起来能存但存不进"。"""
        table: dict[str, dict[str, Any]] = {}
        for item in items or []:
            if isinstance(item, dict):
                key = str(item.get("key") or "")
            else:
                key = str(getattr(item, "key", "") or "")
            if key:
                table[key] = item if isinstance(item, dict) else {
                    "available": getattr(item, "available", True),
                    "reason": getattr(item, "reason", ""),
                }
        self._capabilities = table
        # [FR-12] 能力变化后立即按当前选择重算启用状态（禁用优先），
        # 而不是等下一次选择变化才生效
        self._enable_metadata_controls(bool(self._selected_entries()))

    def _capability_available(self, key: str) -> bool:
        if not self._capabilities:
            return True  # 未拿到清单前不误禁
        entry = self._capabilities.get(key)
        if entry is None:
            return True
        return bool(entry.get("available", True))

    def _capability_reason(self, key: str) -> str:
        entry = self._capabilities.get(key) or {}
        return str(entry.get("reason") or "")

    def handle_escape(self) -> None:
        """Esc 只取消本面板的局部交互，不误取消其他面板任务。"""
        if self.search_button.isChecked():
            self._toggle_search(False)
            return
        if self._pending_patch is not None:
            self.meta_status.setText("")
            self._pending_patch = None
        self.selection.clear()

    def report_unavailable_operation(self, key: int) -> None:
        name = "删除" if key == Qt.Key.Key_Delete else "重命名"
        self.meta_status.setText(_tr("not_wired", "{name}暂不可用。").format(name=name))

    # -- 文件任务（B07 / F10） --------------------------------------------
    def _current_directory(self) -> str:
        """[FR-08] 目录写入类操作的真实落点：仅在目录位置返回路径。

        虚拟位置（搜索/集合）返回空串并提示——此前 mkdir/粘贴会把文件
        静默写进上一个真实目录。
        """
        if self._view_location.get("kind") != "directory":
            return ""
        return self._requested_path or self.current_path or ""

    def _refuse_virtual_location(self, action: str) -> bool:
        """虚拟位置上的目录写入统一拒绝；返回 True 表示已被拒绝并提示。"""
        if self._view_location.get("kind") == "directory":
            return False
        where = (_tr("library_results", "素材库搜索结果")
                 if self._view_location.get("kind") == "library-search"
                 else _tr("collection_title", "集合 · {name}").format(
                     name=self._view_location.get("name") or ""))
        self.meta_status.setText(
            _tr("virtual_readonly", "{action}需要先进入一个真实文件夹（当前是{where}）。").format(
                action=action, where=where))
        return True

    def _task_selection(self) -> list[Entry]:
        return [entry for entry in self._selected_entries()]

    def request_trash_selection(self) -> None:
        """移入回收站（可恢复）：不做永久删除，冲突交给服务逐项判定。"""
        targets = [entry.path for entry in self._task_selection()]
        if not targets:
            self.meta_status.setText(_tr("pick_first", "先选择要移入回收站的文件"))
            return
        self.meta_status.setText(_tr("trash_started", "正在移入回收站 {n} 项…").format(n=len(targets)))
        self.file_task_requested.emit("trash", targets, {"policy": "error", "options": {}})

    def request_rename(self) -> None:
        entries = self._task_selection()
        if len(entries) != 1:
            self.meta_status.setText(_tr("rename_single", "重命名一次只能处理一个文件"))
            return
        entry = entries[0]
        new_name, accepted = QInputDialog.getText(
            self, _tr("rename_title", "重命名"),
            _tr("rename_prompt", "新名称："), text=entry.name)
        if not accepted or not new_name.strip() or new_name.strip() == entry.name:
            return
        self.meta_status.setText(_tr("rename_started", "正在重命名…"))
        self.file_task_requested.emit("rename", [entry.path],
                                      {"policy": "error", "options": {"new_name": new_name.strip()}})

    def request_new_folder(self) -> None:
        if self._refuse_virtual_location(_tr("new_folder_title", "新建文件夹")):
            return
        directory = self._current_directory()
        if not directory:
            return
        name, accepted = QInputDialog.getText(
            self, _tr("new_folder_title", "新建文件夹"),
            _tr("new_folder_prompt", "文件夹名称："))
        if not accepted or not name.strip():
            return
        self.meta_status.setText(_tr("mkdir_started", "正在新建文件夹…"))
        self.file_task_requested.emit("mkdir", [directory],
                                      {"policy": "error", "options": {"name": name.strip()}})

    def apply_task_progress(self, job: Dict[str, Any]) -> None:
        total = int(job.get("total") or 0)
        completed = int(job.get("completed") or 0)
        if total:
            self.meta_status.setText(
                _tr("task_progress", "文件操作进行中：{done}/{total}").format(done=completed, total=total))

    def apply_task_result(self, job: Dict[str, Any]) -> None:
        """逐项结果说话：成功数 / 失败原因，刷新受影响目录，不宣布不存在的成功。"""
        operation = str(job.get("operation") or "")
        successes = job.get("successes") or []
        failures = job.get("failures") or []
        cancelled = bool(job.get("cancelled"))
        labels = {"mkdir": "新建文件夹", "rename": "重命名", "copy": "复制",
                  "move": "移动", "trash": "移入回收站", "restore": "恢复",
                  "purge": "彻底删除"}
        label = labels.get(operation, "文件操作")
        if cancelled:
            self.meta_status.setText(_tr("task_cancelled", "{label}已取消").format(label=label))
        elif failures:
            reasons = "；".join(str(item.get("error") or "失败") for item in failures[:3])
            self.meta_status.setText(
                _tr("task_partial", "{label}：成功 {ok} · 失败 {bad}（{reasons}）").format(
                    label=label, ok=len(successes), bad=len(failures), reasons=reasons))
        else:
            self.meta_status.setText(
                _tr("task_done", "{label}完成：{ok} 项").format(label=label, ok=len(successes)))
        if operation in ("mkdir", "rename", "trash", "restore", "copy", "move", "purge"):
            self.refresh()

    def task_failed(self, message: str) -> None:
        self.meta_status.setText(str(message or "文件操作失败"))

    # -- 全库搜索（B05） ---------------------------------------------------
    def _library_toggle(self, checked: bool) -> None:
        if checked and not self.search_button.isChecked():
            self.search_button.setChecked(True)
            self._toggle_search(True)
        query = self.search_edit.text().strip()
        if checked and query:
            self._last_search_query = query  # [FR-08] 刷新按当前位置重放的依据
            self.library_search_requested.emit(query, 200, 0)
        elif not checked and self._library_active:
            self._library_active = False
            self._view_location = {"kind": "directory",
                                   "path": self._requested_path or self.current_path,
                                   "writable": True}
            self.refresh()

    def show_library_results(self, payload: Dict[str, Any]) -> None:
        raw_entries = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(raw_entries, list):
            return
        if self._append_next_page and self._view_location.get("kind") == "library-search":
            # 搜索续页：增量追加（选择/滚动保留），不重建虚拟视图
            self._append_next_page = False
            parsed = []
            for raw in raw_entries:
                try:
                    parsed.append(parse_entry(raw))
                except Exception:
                    continue
            self.model.append_entries(parsed)
            self._entry_paths.update(entry.path for entry in parsed)
            for offset, entry in enumerate(parsed, start=len(self._entry_rows)):
                self._entry_rows[entry.path] = offset
            self._view_total = int(payload.get("total") or 0)
            self._view_truncated = bool(payload.get("truncated"))
            self._update_page_status()
            self.status.setText(
                _tr("library_status", "{shown} / {total} 项{more}").format(
                    shown=len(self.model.entries), total=self._view_total,
                    more="（结果已截断）" if self._view_truncated else ""))
            self._update_info()
            return
        self._view_total = int(payload.get("total") or 0)
        self._view_truncated = bool(payload.get("truncated"))
        self._commit_virtual_view(
            "library-search", payload,
            title=_tr("library_results", "素材库搜索 · {n} 项"))
        entries = self.model.entries
        total = int(payload.get("total") or len(entries))
        truncated = bool(payload.get("truncated"))
        self._update_page_status()
        self._update_info()
        self.status.setText(
            _tr("library_status", "{shown} / {total} 项{more}").format(
                shown=len(entries), total=total, more="（结果已截断）" if truncated else ""))
        if not entries:
            self.meta_status.setText(_tr("library_empty", "没有命中素材；试试名称、标签或备注的其他关键词。"))

    def _commit_virtual_view(self, kind: str, payload: Dict[str, Any], *,
                             title: str) -> None:
        """[FR-08] 搜索/集合统一经 Location + QueryResult 提交视图。

        与目录 set_entries 同级的完整提交：重建路径索引（封面/预览身份
        不再错位）、关闭不属于本视图的预览浮层、显式标记虚拟位置
        （目录写入操作据此拒绝，而不是落到上一个真实目录）。
        [FR-10] 虚拟视图只有一份查询语义：不再叠加本地整句 substring 过滤
        （服务端已按多词 AND 命中；本地二次过滤会丢跨字段命中）。
        """
        raw_entries = payload.get("files") if isinstance(payload, dict) else None
        entries: list[Entry] = []
        for raw in raw_entries or []:
            try:
                entries.append(parse_entry(raw))
            except Exception:
                continue
        self._clear_thumbnail_work()
        if self._preview.is_open and (self._preview_path not in {e.path for e in entries}):
            self.close_preview()
        self._entry_paths = {entry.path for entry in entries}
        self._entry_rows = {entry.path: row for row, entry in enumerate(entries)}
        self._library_active = kind == "library-search"
        self._collection_active = kind == "collection"
        self._view_location = {
            "kind": kind,
            "query": str(payload.get("query") or self._last_search_query or ""),
            "collection_id": getattr(self, "_pending_collection_id", None)
                             if kind == "collection" else None,
            "name": self._pending_collection_name if kind == "collection" else "",
            "writable": False,  # 虚拟位置不允许隐式目录写入
        }
        self._ready = True
        self.model.set_entries(entries)
        # 退出目录的本地过滤语义：保留类型/旗标筛选，跳过 query 二次过滤
        self.proxy.query = ""
        self.proxy.invalidateFilter()
        self.breadcrumb_button.setText(title.format(
            n=int(payload.get("total") or len(entries))))
        self.pages.setEnabled(True)
        self.message.setVisible(not entries)
        self.locations.reveal("")

    # -- 悬停胶片条（F14） -------------------------------------------------
    def hover_index(self, index: QModelIndex) -> None:
        entry = index.data(FilesEntryModel.ENTRY_ROLE) if index.isValid() else None
        if not isinstance(entry, Entry) or entry.kind != "video" or entry.is_dir:
            self.clear_hover()
            return
        if entry.path == self._hover_path:
            return
        self._hover_path = entry.path
        self._hover_timer.start()

    def clear_hover(self) -> None:
        self._hover_timer.stop()
        self._hover_path = ""

    def _request_hover_strip(self) -> None:
        if not self._hover_path:
            return
        entry = next((e for e in self.model.entries if e.path == self._hover_path), None)
        if entry is None or entry.path in self._strips:
            return
        self.strip_requested.emit(entry.path, float(entry.technical.duration_seconds or 0.0))

    def set_strip(self, path: str, data: bytes) -> None:
        if path not in self._entry_paths:
            return
        image = QImage.fromData(data)
        if image.isNull():
            return
        self._strips[path] = image
        self._strips.move_to_end(path)
        while len(self._strips) > 24:
            self._strips.popitem(last=False)
        self.icon_view.viewport().update()

    def strip_for_entry(self, entry: Entry) -> QImage | None:
        return self._strips.get(entry.path)

    # -- 粘贴（F10） -------------------------------------------------------
    def paste_files(self) -> None:
        """Ctrl/Cmd+V：剪贴板本地文件 → 当前目录复制，默认不覆盖（rename 策略）。"""
        if self._refuse_virtual_location(_tr("paste_title", "粘贴")):
            return
        directory = self._current_directory()
        mime = QApplication.clipboard().mimeData()
        if not directory or mime is None or not mime.hasUrls():
            self.meta_status.setText(_tr("paste_empty", "剪贴板里没有可粘贴的文件"))
            return
        targets = [url.toLocalFile() for url in mime.urls()
                   if url.isLocalFile() and url.toLocalFile()]
        if not targets:
            self.meta_status.setText(_tr("paste_empty", "剪贴板里没有可粘贴的文件"))
            return
        self.meta_status.setText(_tr("paste_started", "正在粘贴 {n} 项…").format(n=len(targets)))
        self.file_task_requested.emit("copy", targets,
                                      {"policy": "rename", "options": {"destination": directory}})

    # -- 常用位置 / 索引（F11 / B10） -------------------------------------
    def request_libraries(self) -> None:
        self.libraries_requested.emit()

    def show_libraries(self, items: list[Any]) -> None:
        self._libraries = [item for item in items if isinstance(item, dict)]
        self.library_list.clear()
        for item in self._libraries:
            path = str(item.get("path") or "")
            if not path:
                continue
            name = str(item.get("name") or os.path.basename(path.rstrip("/")) or path)
            row = QListWidgetItem(name)
            row.setToolTip(path)
            row.setData(Qt.ItemDataRole.UserRole, path)
            self.library_list.addItem(row)
        self.library_list.setFixedHeight(min(3, self.library_list.count()) * 24)
        self.library_list.setVisible(self.library_list.count() > 0)

    def show_library_change(self, ok: bool, message: str) -> None:
        self.meta_status.setText(str(message or ("已更新常用位置" if ok else "常用位置更新失败")))

    def pin_current_directory(self) -> None:
        if self._refuse_virtual_location(_tr("pin_title", "固定常用位置")):
            return
        directory = self._current_directory()
        if directory:
            self.library_pin_requested.emit(directory)

    def unpin_selected_library(self) -> None:
        row = self.library_list.currentItem()
        if row is None:
            self.meta_status.setText(_tr("unpin_pick", "先选中要移除的常用位置"))
            return
        path = row.data(Qt.ItemDataRole.UserRole)
        if path:
            self.library_unpin_requested.emit(str(path))

    def _open_list_path(self, row: QListWidgetItem) -> None:
        path = row.data(Qt.ItemDataRole.UserRole) if row is not None else None
        if path:
            self.navigate(str(path))

    def toggle_scan(self) -> None:
        if self._scan_state.get("running"):
            self.scan_cancel_requested.emit()
        else:
            self.scan_requested.emit()

    def show_scan_status(self, state: dict[str, Any]) -> None:
        self._scan_state = dict(state or {})
        running = bool(self._scan_state.get("running"))
        phase = str(self._scan_state.get("phase") or "")
        scanned = int(self._scan_state.get("scanned") or 0)
        total = int(self._scan_state.get("total") or 0)
        if running:
            text = _tr("index_running", "索引：{phase} {done}/{total}").format(
                phase=phase or "扫描中", done=scanned, total=total or "?")
        else:
            last = self._scan_state.get("last_result") or {}
            suffix = ""
            if isinstance(last, dict) and last.get("added") is not None:
                suffix = _tr("index_last", "（上次新增 {n}）").format(n=last.get("added"))
            text = (_tr("index_done", "索引：{phase}{suffix}").format(phase=phase or "空闲", suffix=suffix)
                    if phase or suffix else "")
        self._set_scan_feedback(text, running=running)

    def _set_scan_feedback(self, text: str, *, running: bool = False) -> None:
        self.scan_label.setText(text)
        self.scan_label.setToolTip(text)
        self.scan_label.setVisible(bool(text))
        self.scan_button.setText(_tr("scan_cancel", "取消") if running else _tr("scan_start", "开始索引"))
        self.scan_button.setVisible(running)

    def scan_failed(self, message: str) -> None:
        self._scan_state = {}
        self._set_scan_feedback(_tr("index_failed", "索引：{message}").format(message=message))

    def retry_selected_thumbnail(self) -> None:
        entries = self._task_selection()
        if len(entries) != 1:
            self.meta_status.setText(_tr("retry_single", "重试缩略图一次只能处理一个文件"))
            return
        entry = entries[0]
        self.meta_status.setText(_tr("retry_thumb", "正在重试缩略图…"))
        self.thumbnail_retry_requested.emit(entry.path, int(entry.thumb_version))

    # -- 集合浏览（B11） ---------------------------------------------------
    def request_collections(self) -> None:
        if not self._collections:
            self.collections_requested.emit()
            return
        self._show_collections_menu()

    def show_collections(self, items: list[Any]) -> None:
        self._collections = [item for item in items if isinstance(item, dict)]
        if not self._collections:
            self._create_collection_from_selection(prompt_hint=True)
            return
        self._show_collections_menu()

    def show_collection_change(self, ok: bool, message: str) -> None:
        self.meta_status.setText(str(message or ("集合已更新" if ok else "集合操作失败")))

    def _create_collection_from_selection(self, *, prompt_hint: bool = False) -> None:
        """从所选新建集合：名字必填；没选素材也允许先建空集合。"""
        selection = [entry.path for entry in self._task_selection() if not entry.is_dir]
        hint = (f"（将加入所选 {len(selection)} 项）" if selection
                else "（先建空集合，稍后可加入素材）")
        if prompt_hint and not selection:
            self.meta_status.setText(
                _tr("collections_create_hint",
                    "还没有集合；选择素材后点「集合」即可从所选新建。"))
            return
        name, accepted = QInputDialog.getText(
            self, _tr("collection_new", "新建集合"),
            _tr("collection_new_prompt", "集合名称：{hint}").format(hint=hint))
        if not accepted or not name.strip():
            return
        self.collection_create_requested.emit(name.strip(), selection)

    def _rename_collection(self, collection_id: int, current: str) -> None:
        name, accepted = QInputDialog.getText(
            self, _tr("collection_rename", "集合改名"),
            _tr("collection_rename_prompt", "新的集合名称："), text=current)
        if accepted and name.strip() and name.strip() != current:
            self.collection_rename_requested.emit(collection_id, name.strip())

    def _add_selection_to_collection(self, collection_id: int, name: str) -> None:
        selection = [entry.path for entry in self._task_selection() if not entry.is_dir]
        if not selection:
            self.meta_status.setText(_tr("collection_add_pick", "先选择要加入「{name}」的素材").format(name=name))
            return
        self.meta_status.setText(_tr("collection_adding", "正在加入「{name}」…").format(name=name))
        self.collection_add_members_requested.emit(collection_id, selection)

    def _remove_selection_from_collection(self) -> None:
        """集合视图内：把所选移出当前集合（不删文件本体）。"""
        if not self._collection_active:
            return
        collection_id = self._view_location.get("collection_id")
        if collection_id is None:
            return
        selection = [entry.path for entry in self._task_selection()]
        if not selection:
            self.meta_status.setText(_tr("collection_remove_pick", "先选择要移除的成员"))
            return
        self.collection_remove_members_requested.emit(int(collection_id), selection)

    def _show_collections_menu(self) -> None:
        menu = QMenu(self)
        selection = [e for e in self._task_selection() if not e.is_dir]
        new_label = (_tr("collection_new_with", "从所选新建集合（{n} 项）").format(n=len(selection))
                     if selection else _tr("collection_new_empty", "新建空集合…"))
        menu.addAction(new_label, lambda: self._create_collection_from_selection())
        if self._collections:
            menu.addSeparator()
        for item in self._collections:
            name = str(item.get("name") or "未命名集合")
            count = item.get("item_count")
            label = f"{name}（{count}）" if count is not None else name
            try:
                collection_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            submenu = menu.addMenu(label)
            submenu.addAction(_tr("open", "打开"), lambda cid=collection_id, n=name:
                              self._open_collection(cid, n))
            if selection:
                submenu.addAction(
                    _tr("collection_add_selected", "加入所选 {n} 项").format(n=len(selection)),
                    lambda cid=collection_id: self._add_selection_to_collection(cid, name))
            submenu.addAction(_tr("rename", "改名…"),
                              lambda cid=collection_id, n=name: self._rename_collection(cid, n))
            submenu.addAction(_tr("collection_delete", "删除集合…"),
                              lambda cid=collection_id: self._confirm_delete_collection(cid, name))
        menu.exec(self.collections_button.mapToGlobal(self.collections_button.rect().bottomLeft()))

    def _confirm_delete_collection(self, collection_id: int, name: str) -> None:
        from PyQt6.QtWidgets import QMessageBox
        confirm = QMessageBox.question(
            self, _tr("collection_delete", "删除集合"),
            _tr("collection_delete_confirm",
                "删除集合「{name}」不会删除素材文件本身。继续？").format(name=name))
        if confirm == QMessageBox.StandardButton.Yes:
            self.collection_delete_requested.emit(collection_id)

    def _open_collection(self, collection_id: int, name: str) -> None:
        self._pending_collection_name = name
        self._pending_collection_id = collection_id  # [FR-08] 刷新重放的身份依据
        self.meta_status.setText(_tr("collection_loading", "正在读取集合「{name}」…").format(name=name))
        self.collection_requested.emit(collection_id, name)

    def show_collection_results(self, payload: Dict[str, Any]) -> None:
        raw_entries = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(raw_entries, list):
            return
        name = self._pending_collection_name or "集合"
        payload = dict(payload or {})
        payload.setdefault("total", len(raw_entries))
        self._commit_virtual_view(
            "collection", payload,
            title=_tr("collection_title", f"集合 · {name} · {{n}} 项"))
        self._update_info()
        self.status.setText(_tr("collection_status", "集合 · {name} · {n} 项").format(
            name=name, n=len(self.model.entries)))

    def collection_failed(self, message: str) -> None:
        self.meta_status.setText(_tr("collection_failed", "读取集合失败：{message}").format(message=message))

    def library_search_failed(self, message: str) -> None:
        self._library_active = False
        self.meta_status.setText(_tr("library_failed", "全库搜索失败：{message}").format(message=message))

    def activate_current(self) -> None:
        if self._ready and len(self.selection.selectedRows(0)) == 1:
            self._activate(self.selection.selectedRows(0)[0])

    def _activate(self, index: QModelIndex) -> None:
        if self._ready and index.isValid():
            entry = index.data(FilesEntryModel.ENTRY_ROLE)
            if entry.is_dir:
                self.navigate(entry.path)
            else:
                self._cache_opened_thumbnail(entry)
                self.file_activated.emit(entry.path)

    def _context_menu(self, view: QAbstractItemView, pos: Any) -> None:
        menu = self._build_context_menu(view, pos)
        menu.exec(view.viewport().mapToGlobal(pos))

    def _build_context_menu(self, view: QAbstractItemView, pos: Any) -> QMenu:
        menu = QMenu(self)
        entry = view.indexAt(pos)
        if entry.isValid() and not self.selection.isSelected(entry):
            self.selection.select(entry, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        if self.selection.selectedRows(0):
            if len(self.selection.selectedRows(0)) == 1:
                menu.addAction(_tr("open", "打开"), self.activate_current)
            copy_action = menu.addAction(_tr("copy", "复制文件"), self.copy_files)
            copy_action.setEnabled(bool(self.selected_file_urls()))
            if len(self.selection.selectedRows(0)) == 1:
                menu.addAction(_tr("rename", "重命名…"), self.request_rename)
            if len(self.selection.selectedRows(0)) >= 2:
                menu.addAction(_tr("batch_rename", "批量改名…"),
                               lambda: self.batch_rename_requested.emit(
                                   [e.path for e in self._selected_entries() if not e.is_dir]))
            images = [e for e in self._selected_entries() if e.kind == "image" and not e.is_dir]
            if len(self.selection.selectedRows(0)) >= 2 and 2 <= len(images) <= 4:
                menu.addAction(_tr("compare", "并排比较…"),
                               lambda: self.compare_requested.emit([e.path for e in images]))
            menu.addAction(_tr("trash", "移入回收站"), self.request_trash_selection)
            if self._collection_active:
                menu.addAction(_tr("collection_remove_selected", "从集合移除"),
                               self._remove_selection_from_collection)
            menu.addAction(_tr("copy_path", "复制路径"), lambda: QApplication.clipboard().setText("\n".join(e.path for e in self._selected_entries())))
            menu.addSeparator()
        if self._task_selection():
            failed = [entry for entry in self._task_selection()
                      if entry.path in self._thumbnail_failed]
            if len(self._task_selection()) == 1 and failed:
                menu.addAction(_tr("retry_thumb", "重试缩略图"), self.retry_selected_thumbnail)
        mime = QApplication.clipboard().mimeData()
        if mime is not None and mime.hasUrls():
            menu.addAction(_tr("paste", "粘贴"), self.paste_files)
        menu.addAction(_tr("new_folder", "新建文件夹…"), self.request_new_folder)
        menu.addAction(_tr("plugin_tools", "使用插件处理…"), self.plugins_requested.emit)
        menu.addAction(_tr("native_tools", "素材工具…"), self.legacy_requested.emit)
        return menu

    def shutdown(self) -> None:
        self._ready = False
        self._density_timer.stop()
        if self._video_host is not None:
            self._video_host.shutdown()
            self._video_host = None
        self.close_preview()
        self._preview_decoder.shutdown()
        self._clear_thumbnail_work()
