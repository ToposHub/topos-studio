"""Read-only library browsing within the Edit media pool."""
from __future__ import annotations

import os
import uuid
from typing import Any

from PyQt6.QtCore import QEvent, QMimeData, QModelIndex, QObject, QPoint, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDrag, QHideEvent, QIcon, QImage, QKeyEvent, QPainter, QPixmap, QResizeEvent
from PyQt6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget,
    QListWidgetItem, QMenu, QPushButton, QStackedWidget, QStyleOptionViewItem, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from src.app.ui.native_files_transport import NativeFilesTransport
from src.features.edit.core.library_reference import LIBRARY_REFERENCE_MIME, serialize_reference
from src.features.edit.core.project import MediaAsset
from src.features.edit.ui.media_pool_icons import pool_icon
from src.features.edit.ui.media_pool_panel import AssetListModel, ViewMode
from src.features.edit.ui.media_pool_presentation import (
    ResolvePoolDelegate, configure_pool_columns, configure_pool_posters, configure_pool_shelf,
    pool_shelf_height, resize_pool_posters,
)
from src.features.files.contracts import Availability, Entry
from src.shared.assets.icon_manager import get_icon, icon_folder
from src.shared.i18n.i18n import tr

_ROLE = Qt.ItemDataRole.UserRole
_POSTER_ROLE = Qt.ItemDataRole.UserRole + 1
_CARD_ROLE = Qt.ItemDataRole.UserRole + 2
_KINDS = ("image", "video", "audio")


def _tr(key: str, default: str, **values: Any) -> str:
    return tr(f"edit.media_pool.library_browser.{key}", default, **values)


def _start_library_drag(view: QAbstractItemView, hit: dict[str, Any], icon: QIcon) -> None:
    if hit["online"] and hit["kind"] in _KINDS:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(hit["path"])])
        mime.setData(LIBRARY_REFERENCE_MIME, serialize_reference(hit))
        drag = QDrag(view)
        drag.setMimeData(mime)
        drag.setPixmap(icon.pixmap(QSize(100, 56)))
        drag.exec(Qt.DropAction.CopyAction)


class LibraryMediaList(QListWidget):
    def keyPressEvent(self, event: QKeyEvent) -> None:
        item = self.currentItem()
        if item is not None and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.itemActivated.emit(item)
            event.accept()
            return
        super().keyPressEvent(event)

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        item = self.currentItem()
        if item is not None:
            _start_library_drag(self, item.data(_ROLE), item.icon())


class LibraryMediaRows(QTreeWidget):
    poster_requested = pyqtSignal(str)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        item = self.currentItem()
        if item is not None and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.itemActivated.emit(item, self.currentColumn())
            event.accept()
            return
        super().keyPressEvent(event)

    def drawRow(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().drawRow(painter, option, index)
        hit = index.data(_ROLE)
        if hit and not index.data(_POSTER_ROLE):
            self.poster_requested.emit(hit["path"])

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        item = self.currentItem()
        if item is not None:
            _start_library_drag(self, item.data(0, _ROLE), item.icon(0))


class LibraryPosterDelegate(ResolvePoolDelegate):
    poster_requested = pyqtSignal(str)

    def _request_poster(self, path: str) -> None:
        self.poster_requested.emit(path)


class MediaPoolLibraryBrowser(QWidget):
    browse_requested = pyqtSignal()
    project_requested = pyqtSignal()
    source_preview_requested = pyqtSignal(object, QImage, bool)

    def __init__(self, parent: QWidget | None = None, *, transport: NativeFilesTransport | None = None) -> None:
        super().__init__(parent)
        # An independent session keeps navigation and preview cancellation local.
        self.transport = transport or NativeFilesTransport(lambda: "", None, self, facade_workers=3)
        self.transport.PAGE_SIZE = 200
        # Leave one facade worker available for navigation and source opens.
        self.transport.MAX_THUMBNAIL_CONCURRENCY = 2
        self._library_id: str | None = None
        self._folder: str | None = None
        self._roots: dict[str, tuple[str, str]] = {}
        self._nodes: dict[tuple[str, str], QTreeWidgetItem] = {}
        self._offset = 0
        self._folder_rows = 0
        self._request_id = ""
        self._offline_snapshot = False
        self._registry_requested = False
        self._focus_source = False
        self._preview_error = ""
        self._items: dict[str, list[QListWidgetItem]] = {}
        self._thumbnail_requests: dict[str, None] = {}
        self._view_mode: ViewMode | None = None
        self._thumbnail_width = 180
        self._icons = {
            "dir": icon_folder(), "image": get_icon("fa5s.image"),
            "video": get_icon("fa5s.film"), "audio": get_icon("fa5s.music"),
            "library": get_icon("fa5s.database"), "offline": get_icon("fa5s.exclamation-circle"),
        }

        self.tree = QTreeWidget(self)
        self.tree.setObjectName("mediaPoolLibraryTree")
        self.tree.setAccessibleName(_tr("title", "Media Libraries"))
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(13)
        self.heading = QTreeWidgetItem(self.tree, [_tr("title", "Media Libraries")])
        self.heading.setIcon(0, self._icons["library"])
        self.heading.setExpanded(True)
        self.tree.currentItemChanged.connect(self._on_tree_current)
        self.tree.itemClicked.connect(self._on_tree_clicked)
        self.tree.itemExpanded.connect(self._expand)

        self.setObjectName("mediaPoolContent")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        breadcrumb = QFrame(self)
        breadcrumb.setObjectName("mediaPoolBreadcrumb")
        breadcrumb.setFixedHeight(29)
        header = QHBoxLayout(breadcrumb)
        header.setContentsMargins(0, 0, 5, 0)
        header.setSpacing(6)
        back = QToolButton(self)
        back.setObjectName("mediaPoolPathBack")
        back.setFixedSize(29, 29)
        back.setIcon(pool_icon("back"))
        back.setIconSize(QSize(16, 16))
        back.setToolTip(_tr("project_media", "Project Media").lstrip("‹ "))
        back.setAccessibleName(back.toolTip())
        back.clicked.connect(self.project_requested)
        header.addWidget(back)
        self.up = QPushButton(get_icon("fa5s.arrow-up"), "", self)
        self.up.setToolTip(_tr("up", "Enclosing Folder"))
        self.up.setAccessibleName(self.up.toolTip())
        self.up.clicked.connect(self._go_up)
        header.addWidget(self.up)
        self.location = QLabel(_tr("title", "Media Libraries"), self)
        self.location.setTextFormat(Qt.TextFormat.PlainText)
        self.location.setMinimumWidth(0)
        header.addWidget(self.location, 1)
        self.more = QPushButton(_tr("more", "Load More"), self)
        self.more.clicked.connect(self._load_more)
        self.more.hide()
        header.addWidget(self.more)
        refresh = QPushButton(get_icon("fa5s.sync-alt"), "", self)
        refresh.setToolTip(_tr("refresh", "Refresh"))
        refresh.setAccessibleName(refresh.toolTip())
        refresh.clicked.connect(self._refresh)
        header.addWidget(refresh)
        layout.addWidget(breadcrumb)

        self.search_bar = QWidget(self)
        search_row = QHBoxLayout(self.search_bar)
        search_row.setContentsMargins(8, 4, 8, 0)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText(_tr("search_prompt", "Search tags or describe a shot"))
        self.search.setClearButtonEnabled(True)
        self.search.setMaxLength(1000)
        self.search.returnPressed.connect(self._run_search)
        self.search.textChanged.connect(self._on_query_changed)
        search_row.addWidget(self.search, 1)
        find = QPushButton(_tr("search", "Search"), self)
        find.clicked.connect(self._run_search)
        search_row.addWidget(find)
        layout.addWidget(self.search_bar)
        self.status = QLabel(self)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setContentsMargins(8, 3, 8, 3)
        layout.addWidget(self.status)
        self._set_status(_tr("choose_library", "Choose a library to browse."))

        self.list = LibraryMediaList(self)
        self.list.setObjectName("mediaPoolLibraryResults")
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(lambda point: self._show_context_menu(self.list, point))
        self.list.currentItemChanged.connect(self.cancel_source_preview)
        self.list.itemClicked.connect(lambda item: self._request_source(item.data(_ROLE), focus=False))
        self.list.itemActivated.connect(lambda item: self._activate(item.data(_ROLE)))
        self._delegate = LibraryPosterDelegate(self.list)
        self._delegate.view = self.list
        self._delegate.card_data_role = _CARD_ROLE
        self._delegate.show_clip_information = False
        self._delegate.poster_requested.connect(self._queue_thumbnail)
        self.list.setItemDelegate(self._delegate)

        self.rows = LibraryMediaRows(self)
        configure_pool_columns(self.rows)
        self.rows.setDragEnabled(True)
        self.rows.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.rows.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.rows.customContextMenuRequested.connect(lambda point: self._show_context_menu(self.rows, point))
        self.rows.currentItemChanged.connect(self.cancel_source_preview)
        self.rows.itemClicked.connect(lambda item, _column: self._request_source(item.data(0, _ROLE), focus=False))
        self.rows.itemActivated.connect(lambda item, _column: self._activate(item.data(0, _ROLE)))
        self.rows.poster_requested.connect(self._queue_thumbnail)

        self.shelf = QListView(self)
        self.shelf.setModel(self.list.model())
        self.shelf.setSelectionModel(self.list.selectionModel())
        shelf_delegate = ResolvePoolDelegate(self.shelf)
        shelf_delegate.card_data_role = _CARD_ROLE
        configure_pool_shelf(self.shelf, shelf_delegate)
        self.shelf.activated.connect(lambda index: self._activate(index.data(_ROLE)))
        self.poster_page = QWidget(self)
        poster_layout = QVBoxLayout(self.poster_page)
        poster_layout.setContentsMargins(0, 0, 0, 0)
        poster_layout.setSpacing(0)
        poster_layout.addWidget(self.shelf)
        poster_layout.addWidget(self.list, 1)
        self.results = QStackedWidget(self)
        self.results.addWidget(self.poster_page)
        self.results.addWidget(self.rows)
        layout.addWidget(self.results, 1)

        self.up.setEnabled(False)

        self._thumb_timer = QTimer(self, singleShot=True, interval=35)
        self._thumb_timer.timeout.connect(self._request_visible_thumbnails)
        self.list.viewport().installEventFilter(self)
        self.tree.installEventFilter(self)
        self.transport.libraries_loaded.connect(self._set_libraries)
        self.transport.libraries_failed.connect(self._set_status)
        self.transport.tree_loaded.connect(self._add_tree_children)
        self.transport.loaded.connect(self._show_directory)
        self.transport.failed.connect(self._directory_failed)
        self.transport.page_meta.connect(self._page_meta)
        self.transport.ai_library_results.connect(self._show_search)
        self.transport.ai_library_search_note.connect(self._show_search_note)
        self.transport.thumbnail_loaded.connect(self._set_thumbnail)
        self.transport.source_preview_loaded.connect(self._show_source_preview)
        self.transport.source_preview_failed.connect(self._show_preview_error)
        self.set_presentation(ViewMode.GRID, self._thumbnail_width)

    def set_presentation(self, mode: ViewMode, thumbnail_width: int) -> None:
        """Follow the media pool's display mode and thumbnail size."""
        self._thumbnail_width = thumbnail_width
        if mode != self._view_mode:
            row = (self.rows.indexOfTopLevelItem(self.rows.currentItem())
                   if self._view_mode == ViewMode.LIST and self.rows.currentItem() is not None
                   else self.list.currentRow())
            self._view_mode = mode
            if mode == ViewMode.LIST:
                self.rows.setCurrentItem(self.rows.topLevelItem(row) if row >= 0 else None)
                self.results.setCurrentWidget(self.rows)
            else:
                configure_pool_posters(self.list, self._delegate, metadata=mode == ViewMode.METADATA)
                # Qt changes the drag policy when the icon/list layout is applied.
                self.list.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
                self.list.setCurrentRow(row)
                self.results.setCurrentWidget(self.poster_page)
                self.list.doItemsLayout()
            self._sync_folder_shelf()
        if mode == ViewMode.GRID:
            resize_pool_posters(self.list, self._delegate, thumbnail_width)

    def set_search_visible(self, visible: bool) -> None:
        self.search_bar.setVisible(visible)
        if not visible:
            self.search.clear()

    def _sync_folder_shelf(self) -> None:
        metadata = self._view_mode == ViewMode.METADATA
        for row in range(self.list.count()):
            folder = row < self._folder_rows
            self.list.item(row).setHidden(metadata and folder)
            self.shelf.setRowHidden(row, not folder)
        self.shelf.setVisible(metadata and self._folder_rows > 0)
        self.shelf.setFixedHeight(pool_shelf_height(self.width(), self._folder_rows))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "shelf"):
            self.shelf.setFixedHeight(pool_shelf_height(self.width(), self._folder_rows))

    def refresh_registry(self) -> None:
        self._registry_requested = True
        self.transport.load_libraries()

    def show_import_status(self, message: str) -> None:
        if self.isVisible() and message:
            self.setToolTip(message)

    def _set_status(self, message: str) -> None:
        self.status.setText(message)
        self.status.setVisible(bool(message))

    def _set_libraries(self, records: list[dict[str, str]]) -> None:
        roots = {row["library_id"]: (row["name"], row["path"]) for row in records}
        if roots == self._roots:
            if not roots:
                self._set_status(_tr("no_libraries", "Add a library in the Files workspace to browse it here."))
            return
        previous_id, previous_folder = self._library_id, self._folder
        self._roots = roots
        self._nodes.clear()
        expanded = self.heading.isExpanded()
        previous = self.tree.blockSignals(True)
        try:
            self.heading.takeChildren()
            for library_id, (name, path) in roots.items():
                self._add_node(self.heading, library_id, path, name, "library")
            if previous_id in roots:
                node = self._nodes[previous_id, roots[previous_id][1]]
                self.tree.setCurrentItem(node)
            else:
                self.tree.setCurrentItem(self.heading)
            self.heading.setExpanded(expanded)
        finally:
            self.tree.blockSignals(previous)
        if previous_id is not None:
            folder = (previous_folder if previous_id in roots
                      and self._contains(roots[previous_id][1], previous_folder) else None)
            self._navigate(previous_id if previous_id in roots else None, folder)
        elif self._request_id:
            self._run_search()
        else:
            self._set_status(_tr("choose_library", "Choose a library to browse.") if roots else
                                _tr("no_libraries", "Add a library in the Files workspace to browse it here."))

    @staticmethod
    def _contains(root: str, folder: str | None) -> bool:
        if folder is None:
            return False
        try:
            return os.path.commonpath((root, folder)) == root
        except ValueError:
            return False

    def _add_node(
        self, parent: QTreeWidgetItem, library_id: str, path: str, name: str, kind: str,
    ) -> QTreeWidgetItem:
        node = QTreeWidgetItem(parent, [name])
        node.setIcon(0, self._icons[kind])
        node.setToolTip(0, path)
        node.setData(0, _ROLE, (library_id, path))
        node.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        self._nodes[library_id, path] = node
        return node

    def _on_tree_current(self, item: QTreeWidgetItem | None, previous: QTreeWidgetItem | None) -> None:
        if item is not None:
            library_id, folder = item.data(0, _ROLE) or (None, None)
            self.browse_requested.emit()
            self._navigate(library_id, folder)

    def _on_tree_clicked(self, item: QTreeWidgetItem, column: int) -> None:
        _, folder = item.data(0, _ROLE) or (None, None)
        if self.isHidden() or folder != self._folder:
            self._on_tree_current(item, None)

    def _expand(self, item: QTreeWidgetItem) -> None:
        scope = item.data(0, _ROLE)
        if scope:
            self.transport.load_tree(scope[1], follow_symlinks=False)

    def _add_tree_children(self, path: str, entries: list[dict[str, str]]) -> None:
        children = {row["path"]: row["name"] for row in entries if os.path.dirname(row["path"]) == path}
        previous = self.tree.blockSignals(True)
        try:
            for library_id, folder in tuple(self._nodes):
                if folder != path:
                    continue
                parent = self._nodes[library_id, path]
                for index in reversed(range(parent.childCount())):
                    child = parent.child(index)
                    if child.data(0, _ROLE)[1] in children:
                        continue
                    obsolete = [parent.takeChild(index)]
                    while obsolete:
                        node = obsolete.pop()
                        self._nodes.pop(tuple(node.data(0, _ROLE)), None)
                        obsolete.extend(node.takeChildren())
                for child_path, name in children.items():
                    if (library_id, child_path) not in self._nodes:
                        self._add_node(parent, library_id, child_path, name, "dir")
        finally:
            self.tree.blockSignals(previous)

    def _navigate(self, library_id: str | None, folder: str | None) -> None:
        if folder is not None:
            owners = [(len(root), owner) for owner, (_, root) in self._roots.items()
                      if self._contains(root, folder)]
            if owners:
                library_id = max(owners)[1]
        self._library_id = library_id
        self._folder = (folder or self._roots[library_id][1]) if library_id is not None else None
        node = self._nodes.get((self._library_id, self._folder))
        if node is not None and node is not self.tree.currentItem():
            expanded = self.heading.isExpanded()
            previous = self.tree.blockSignals(True)
            try:
                self.tree.setCurrentItem(node)
                self.heading.setExpanded(expanded)
            finally:
                self.tree.blockSignals(previous)
        self._update_location()
        self._run_search()

    def _update_location(self) -> None:
        label = _tr("title", "Media Libraries")
        parent_available = False
        if self._library_id is not None and self._folder is not None:
            label, root = self._roots[self._library_id]
            if self._folder != root:
                label = f"{label} / {os.path.relpath(self._folder, root)}"
                parent_available = True
        self.location.setText(label)
        self.location.setToolTip(self._folder or label)
        self.up.setEnabled(parent_available)

    def _go_up(self) -> None:
        if self.up.isEnabled() and self._folder is not None:
            self._navigate(self._library_id, os.path.dirname(self._folder))

    def _refresh(self) -> None:
        self.refresh_registry()
        if self._folder is not None:
            self.transport.load_tree(self._folder, follow_symlinks=False)
        self._run_search()

    def _on_query_changed(self, query: str) -> None:
        if not query.strip() and self._request_id:
            self._run_search()

    def _run_search(self) -> None:
        self.transport.cancel_preview()
        self.transport.cancel_ai_library_search()
        self.transport.cancel()
        self._request_id = ""
        self._offline_snapshot = False
        self._offset = 0
        self._folder_rows = 0
        self.list.clear()
        self.rows.clear()
        self._items.clear()
        self._thumbnail_requests.clear()
        self._thumb_timer.stop()
        self._sync_folder_shelf()
        self.more.hide()
        self.search.setToolTip("")
        query = self.search.text().strip()
        if query:
            self._request_id = uuid.uuid4().hex
            self._set_status(_tr("searching", "Searching libraries…"))
            self.transport.search_ai_library(self._request_id, query, self._library_id,
                                             folder=self._folder, kinds=_KINDS,
                                             include_catalog_metadata=True)
        elif self._folder is not None:
            self._set_status(_tr("loading", "Reading folder…"))
            self.transport.load_directory(self._folder)
        else:
            self._set_status(_tr("choose_library", "Choose a library to browse.") if self._roots else
                                _tr("no_libraries", "Add a library in the Files workspace to browse it here."))

    def _show_directory(self, path: str, entries: list[Entry]) -> None:
        if path != self._folder or self._request_id:
            return
        self._set_scope_icon(online=True)
        self._offset += len(entries)
        for entry in entries:
            if entry.is_dir or entry.has_known_media:
                self._append({"path": entry.path, "name": entry.name, "kind": entry.kind,
                              "online": entry.availability != Availability.OFFLINE,
                              "tags": entry.user.tags, "text": entry.user.comments,
                              "thumb_version": entry.thumb_version}, entry.technical.to_dict())
        self._sync_folder_shelf()

    def _page_meta(self, path: str, total: int, truncated: bool) -> None:
        if path == self._folder and not self._request_id:
            self.more.setVisible(truncated)
            self.more.setEnabled(True)
            self._set_status("" if self.list.count() or truncated else
                                _tr("empty_folder", "This folder has no supported media."))

    def _load_more(self) -> None:
        if self._folder is not None and self._offset:
            self.more.setEnabled(False)
            self.transport.load_directory(self._folder, offset=self._offset)

    def _directory_failed(self, path: str, message: str) -> None:
        if path != self._folder or self._request_id:
            return
        self._set_status(message)
        self.more.setEnabled(True)
        if not self._offset:
            self._set_scope_icon(online=False)
            self._offline_snapshot = True
            self._request_id = uuid.uuid4().hex
            self.transport.search_ai_library(self._request_id, "", self._library_id, folder=path,
                                             kinds=_KINDS, recursive=False, semantic=False)

    def _set_scope_icon(self, *, online: bool) -> None:
        node = self._nodes.get((self._library_id, self._folder))
        if node is not None:
            kind = "library" if node.parent() is self.heading else "dir"
            node.setIcon(0, self._icons[kind if online else "offline"])

    def _show_search(self, request_id: str, hits: list[dict[str, Any]], error: str) -> None:
        if request_id != self._request_id:
            return
        for hit in hits:
            self._append(hit, hit.get("technical"))
        self._sync_folder_shelf()
        if not self._offline_snapshot:
            self._set_status(error or ("" if hits else
                                _tr("no_matches", "No matching indexed material.")))

    def _show_search_note(self, request_id: str, note: str) -> None:
        if request_id == self._request_id and note:
            self.search.setToolTip(note)

    def _append(self, hit: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
        label = hit["name"]
        start, end = hit.get("start_seconds"), hit.get("end_seconds")
        if start is not None and end is not None:
            label += f"  ·  {start:.2f}–{end:.2f} s"
        path, kind, online = hit["path"], hit["kind"], hit["online"]
        icon = self._icons[kind if online else "offline"]
        item = QListWidgetItem(icon, label)
        item.setData(_ROLE, hit)
        item.setData(AssetListModel.ITEM_TYPE_ROLE, "bin" if kind == "dir" else "asset")
        item.setData(AssetListModel.ITEM_ID_ROLE, path)
        item.setToolTip("\n".join(value for value in (
            path, " · ".join(hit.get("tags", ())), hit.get("text", ""),
            "" if online else _tr("offline", "Offline"),
        ) if value))
        row = self._folder_rows if kind == "dir" else self.list.count()
        if kind == "dir":
            self._folder_rows += 1
        columns = [label]
        if kind in _KINDS:
            # This adapter supplies the shared painter with media fields. The
            # library payload remains the authority for drag and import.
            asset = MediaAsset(path, path, kind, label, metadata or {}, is_offline=not online)
            item.setData(_CARD_ROLE, asset)
            duration = (self._delegate._format_duration_short(asset.duration_seconds)
                        if asset.duration_seconds is not None else "-")
            fps = f"{asset.fps:.2f}" if asset.fps is not None else "-"
            columns.extend((asset.reel or "-", asset.production_info_str, duration, fps,
                            asset.resolution_str, asset.codec_str, asset.audio_channels_str, path))
        row_item = QTreeWidgetItem(columns)
        row_item.setData(0, _ROLE, hit)
        row_item.setIcon(0, icon)
        row_item.setToolTip(0, item.toolTip())
        row_item.setToolTip(8, path)
        if not online:
            for column in range(len(columns)):
                row_item.setForeground(column, QColor(self._delegate.BADGE_COLORS["offline"]))
        self.rows.insertTopLevelItem(row, row_item)
        self.list.insertItem(row, item)
        self._items.setdefault(path, []).append(item)

    def _queue_thumbnail(self, path: str) -> None:
        items = self._items.get(path)
        if items and items[0].data(_ROLE)["online"] and items[0].data(_ROLE)["kind"] in ("image", "video"):
            self._thumbnail_requests[path] = None
        if self._thumbnail_requests and not self._thumb_timer.isActive():
            self._thumb_timer.start()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.tree and event.type() == QEvent.Type.Show and not self._registry_requested:
            QTimer.singleShot(0, self._load_first_registry)
        elif (watched is self.list.viewport() and event.type() in (QEvent.Type.Resize, QEvent.Type.Show)
              and self._view_mode == ViewMode.GRID):
            resize_pool_posters(self.list, self._delegate, self._thumbnail_width)
        return super().eventFilter(watched, event)

    def _load_first_registry(self) -> None:
        if not self._registry_requested:
            self.refresh_registry()

    def _request_visible_thumbnails(self) -> None:
        paths, self._thumbnail_requests = self._thumbnail_requests, {}
        if self.isVisible():
            for path in paths:
                hit = self._items[path][0].data(_ROLE)
                self.transport.request_thumbnail(path, hit.get("thumb_version", 0))

    def _set_thumbnail(self, path: str, raw: bytes) -> None:
        items = self._items.get(path)
        if not items:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(raw):
            icon = QIcon(pixmap)
            for item in items:
                item.setData(_POSTER_ROLE, pixmap)
                item.setIcon(icon)
                row = self.rows.topLevelItem(self.list.row(item))
                row.setData(0, _POSTER_ROLE, True)
                row.setIcon(0, icon)

    def _current_media(self) -> dict[str, Any] | None:
        if self._view_mode == ViewMode.LIST:
            item = self.rows.currentItem()
            hit = item.data(0, _ROLE) if item is not None else None
        else:
            item = self.list.currentItem()
            hit = item.data(_ROLE) if item is not None else None
        return hit if hit and hit["online"] and hit["kind"] in _KINDS else None

    def _activate(self, hit: dict[str, Any]) -> None:
        if hit["kind"] == "dir" and hit["online"]:
            self._navigate(self._library_id, hit["path"])
        else:
            self._request_source(hit, focus=True)

    def _request_source(self, hit: dict[str, Any], *, focus: bool) -> None:
        if hit["online"] and hit["kind"] in _KINDS:
            if self._preview_error and self.status.text() == self._preview_error:
                self._set_status("")
            self._preview_error = ""
            self._focus_source = focus
            self.transport.load_source_preview(hit["path"])

    def _show_preview_error(self, message: str) -> None:
        self._preview_error = message
        self._set_status(message)

    def _show_context_menu(self, view: LibraryMediaList | LibraryMediaRows, point: QPoint) -> None:
        item = view.itemAt(point)
        if item is None:
            return
        view.setCurrentItem(item)
        hit = self._current_media()
        if hit is None:
            return
        menu = QMenu(self)
        menu.addAction(_tr("preview", "Preview"), lambda: self._activate(hit))
        menu.exec(view.viewport().mapToGlobal(point))

    def _show_source_preview(self, asset: MediaAsset) -> None:
        items = self._items.get(asset.path)
        poster = items[0].data(_POSTER_ROLE) if items else None
        self.source_preview_requested.emit(
            asset, poster.toImage() if isinstance(poster, QPixmap) else QImage(), self._focus_source)

    def hideEvent(self, event: QHideEvent) -> None:
        self.cancel_source_preview()
        super().hideEvent(event)

    def cancel_source_preview(self) -> None:
        self.transport.cancel_preview()

    def shutdown(self) -> None:
        self.transport.shutdown()
