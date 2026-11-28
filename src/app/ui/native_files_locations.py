"""Lazy directory navigation for Home and mounted data volumes."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import posixpath
import sys
from typing import Any
from uuid import uuid4

from PyQt6.QtCore import QItemSelectionModel, QLocale, QModelIndex, QPoint, QRect, QSignalBlocker, QSize, QStorageInfo, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QHideEvent, QIcon, QKeyEvent, QKeySequence, QPainter, QResizeEvent, QShowEvent, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import QAbstractItemView, QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeView, QWidget

from src.features.files.contracts import normalize_path
from src.shared.i18n.i18n import I18N
from src.shared.media.format_registry import is_image
from src.shared.ui.style_sheet import C


def location_icon_path(name: str) -> str:
    return (Path(__file__).resolve().parents[2] / "shared" / "assets" / "icons" / "locations"
            / f"{name}.svg").as_posix()


def location_icon(name: str) -> QIcon:
    path = location_icon_path(name)
    icon = QIcon(path)
    # Supply explicit modes so Qt never applies the OS accent tint to the SVG.
    icon.addFile(path, mode=QIcon.Mode.Selected)
    icon.addFile(path, mode=QIcon.Mode.Active)
    return icon


@dataclass(frozen=True)
class MountedVolume:
    path: str
    name: str
    device: bytes


def mounted_data_volumes() -> tuple[MountedVolume, ...]:
    """Read the OS mount table on a worker, without scanning directory contents."""
    if sys.platform == "darwin":
        from src.shared.io.macos_mount_table import cached_mounts

        volumes = [MountedVolume(normalize_path(path), path.rsplit("/", 1)[-1], device)
                   for path, device in cached_mounts() if path.rpartition("/")[0] == "/Volumes"]
        return tuple(sorted(volumes, key=lambda volume: (volume.name.casefold(), volume.path)))

    volumes = []
    for storage in QStorageInfo.mountedVolumes():
        if not storage.isValid() or not storage.isReady() or storage.isRoot():
            continue
        path = normalize_path(storage.rootPath())
        if sys.platform.startswith("linux") and not (
            path == "/mnt" or path.startswith(("/mnt/", "/media/", "/run/media/"))
        ):
            continue
        name = storage.displayName() or path.rsplit("/", 1)[-1] or path
        if path and name:
            volumes.append(MountedVolume(path, name, bytes(storage.device())))
    return tuple(sorted(volumes, key=lambda volume: (volume.name.casefold(), volume.path)))


class _LocationDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        count = index.data(HomeDirectoryTree.COUNT_ROLE)
        if count is None:
            super().paint(painter, option, index)
            return
        item = QStyleOptionViewItem(option)
        self.initStyleOption(item, index)
        style = item.widget.style() if item.widget is not None else QApplication.style()
        name_rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, item, item.widget)
        count_text = QLocale(I18N.get_current_locale()).toString(count)
        count_font = QFont(item.font)
        count_font.setPixelSize(10)
        count_font.setWeight(QFont.Weight.Normal)
        painter.save()
        painter.setFont(count_font)
        count_width = painter.fontMetrics().horizontalAdvance(count_text)
        if name_rect.width() < count_width + 24:
            painter.restore()
            super().paint(painter, option, index)
            return
        item.text = item.fontMetrics.elidedText(item.text, Qt.TextElideMode.ElideRight,
                                                name_rect.width() - count_width - 18)
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, item, painter, item.widget)
        painter.setFont(count_font)
        painter.setPen(QColor(C.TEXT_DIM))
        painter.drawText(option.rect.adjusted(0, 0, -8, 0),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, count_text)
        painter.restore()


class HomeDirectoryTree(QTreeView):
    directory_requested = pyqtSignal(str)
    paste_requested = pyqtSignal(str)
    tree_requested = pyqtSignal(str)
    tree_reload_requested = pyqtSignal(str)
    tree_path_removed = pyqtSignal(str)
    tree_root_removed = pyqtSignal(str)
    volumes_requested = pyqtSignal()
    folder_counts_requested = pyqtSignal(str, list)
    grade_set_requested = pyqtSignal(int)
    grade_set_images_dropped = pyqtSignal(int, list)
    PATH_ROLE = int(Qt.ItemDataRole.UserRole)
    BRANCH_ROLE = PATH_ROLE + 1
    LIBRARY_ROLE = PATH_ROLE + 2
    LOADED_ROLE = PATH_ROLE + 3
    GRADE_SET_ROLE = PATH_ROLE + 4
    COUNT_ROLE = PATH_ROLE + 5

    def __init__(
        self, home: str, visible_entry: Callable[[dict[str, Any]], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("filesHomeTree")
        self.home = normalize_path(home)
        self._visible_entry = visible_entry
        self._nodes: dict[str, dict[str, QStandardItem]] = {}
        self._pending: set[str] = set()
        self._folder_counts: dict[str, int | None] = {}
        self._count_batch: tuple[str, set[str]] | None = None
        self._count_timer = QTimer(self, singleShot=True)
        self._count_timer.timeout.connect(self._request_visible_folder_counts)
        self._reveal_target: str | None = None
        self._active_branch: str | None = None
        self._volumes: dict[str, MountedVolume] = {}
        self._volume_roots: dict[str, QStandardItem] = {}
        self._library_roots: dict[str, QStandardItem] = {}
        self._library_group: QStandardItem | None = None
        self._grade_set_group: QStandardItem | None = None
        self.item_model = QStandardItemModel(self)
        self.setModel(self.item_model)
        self.setItemDelegate(_LocationDelegate(self))
        self.setHeaderHidden(True)
        self.setIndentation(14)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setUniformRowHeights(True)
        self.setIconSize(QSize(16, 16))
        self._icons = {name: location_icon(name) for name in ("folder", "home", "library", "drive", "grade-set")}
        self.home_item = self._make_item(I18N.tr("files.native.home_root", "Home"), self.home,
                                         icon="home", heading=True, branch="home")
        self.item_model.appendRow(self.home_item)
        self._folders_heading = self._location_heading(I18N.tr("files.native.folders_heading", "文件夹"), "folder")
        self._volume_group = self._location_heading(I18N.tr("files.native.volumes_heading", "磁盘"), "drive")
        self._volume_poll = QTimer(self)
        self._volume_poll.setInterval(2500)
        self._volume_poll.timeout.connect(self.volumes_requested)
        self.expanded.connect(self._expanded)
        self.clicked.connect(self._navigate)
        self.expanded.connect(self._schedule_folder_counts)
        self.collapsed.connect(self._schedule_folder_counts)
        self.item_model.rowsInserted.connect(self._schedule_folder_counts)
        self.item_model.rowsRemoved.connect(self._schedule_folder_counts)
        self.verticalScrollBar().valueChanged.connect(self._schedule_folder_counts)

    def _style_heading(self, item: QStandardItem) -> None:
        item.setForeground(QColor(C.TEXT_PRIMARY))
        font = self.font()
        font.setBold(True)
        item.setFont(font)

    def _location_heading(self, title: str, icon: str) -> QStandardItem:
        item = QStandardItem(self._icons[icon], title)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self._style_heading(item)
        return item

    def enable_grade_sets(self) -> None:
        """Use matching sidebar groups in the image Library workspace."""
        if self._grade_set_group is not None:
            return
        self._library_group = self._location_heading(I18N.tr("files.native.libraries_heading", "素材库"), "library")
        self._grade_set_group = self._location_heading(I18N.tr("grade_sets.title", "调色集"), "grade-set")
        for _ in self._library_roots:
            self._library_group.appendRow(self.item_model.takeRow(0))
        if self._folders_heading.index().isValid():
            self.item_model.takeRow(self._folders_heading.row())
        self.item_model.insertRow(0, self._library_group)
        self.item_model.insertRow(1, self._grade_set_group)
        if not self._volume_group.index().isValid():
            self.item_model.insertRow(self.home_item.row(), self._volume_group)
            self.setExpanded(self._volume_group.index(), True)
        self.setExpanded(self._library_group.index(), True)
        self.setExpanded(self._grade_set_group.index(), True)
        self.collapsed.connect(self._grade_set_group_collapsed)

    def set_grade_sets(self, rows: tuple[tuple[int, str, int], ...]) -> None:
        group = self._grade_set_group
        if group is None:
            return
        previous_ids = tuple(group.child(row).data(self.GRADE_SET_ROLE) for row in range(group.rowCount()))
        if previous_ids != tuple(collection_id for collection_id, _, _ in rows):
            group.removeRows(0, group.rowCount())
            font = self.font()
            font.setWeight(QFont.Weight.Normal)
            for collection_id, _, _ in rows:
                item = QStandardItem(self._icons["grade-set"], "")
                item.setForeground(QColor(C.TEXT_SECONDARY))
                item.setFont(font)
                item.setEditable(False)
                item.setData(collection_id, self.GRADE_SET_ROLE)
                group.appendRow(item)
        for row, (_, name, count) in enumerate(rows):
            item = group.child(row)
            item.setText(name)
            item.setData(count, self.COUNT_ROLE)
            item.setToolTip(name)

    def is_grade_set_group(self, index: QModelIndex) -> bool:
        return self._grade_set_group is not None and self._grade_set_group.index() == index

    def _grade_set_group_collapsed(self, index: QModelIndex) -> None:
        if self.is_grade_set_group(index) and self.currentIndex().parent() == index:
            self.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)

    def select_grade_set(self, collection_id: int | None) -> bool:
        group = self._grade_set_group
        if collection_id is None or group is None:
            return False
        for row in range(group.rowCount()):
            item = group.child(row)
            if item.data(self.GRADE_SET_ROLE) == collection_id:
                self._reveal_target = None
                if self.isExpanded(group.index()):
                    self.setCurrentIndex(item.index())
                return True
        return False

    def reference_drop_target(self, pos: QPoint, paths: list[str]) -> tuple[QRect, Callable[[], None] | None] | None:
        if self._grade_set_group is None:
            return None
        index = self.indexAt(pos)
        collection_id = index.data(self.GRADE_SET_ROLE)
        if collection_id is None and not self.is_grade_set_group(index):
            return None
        images = [path for path in paths if is_image(path)]
        if not images:
            return None
        rect = self.visualRect(index)
        rect.setWidth(self.viewport().width() - rect.left())
        if collection_id is None:
            return rect, None  # Allow hover expansion of the section while dragging.
        return rect, lambda: self.grade_set_images_dropped.emit(collection_id, images)

    def _make_item(self, name: str, path: str, *, branch: str, library_id: str = "",
                   icon: str = "folder", heading: bool = False) -> QStandardItem:
        item = QStandardItem(self._icons[icon], name)
        item.setEditable(False)
        item.setData(path, self.PATH_ROLE)
        item.setData(branch, self.BRANCH_ROLE)
        item.setData(library_id, self.LIBRARY_ROLE)
        item.setForeground(QColor(C.TEXT_SECONDARY if icon != "folder" else C.TEXT_DIM))
        if heading:
            self._style_heading(item)
        else:
            font = self.font()
            font.setWeight(QFont.Weight.Light if icon == "folder" else QFont.Weight.Normal)
            item.setFont(font)
        item.setData(self._folder_counts.get(path), self.COUNT_ROLE)
        self._nodes.setdefault(path, {})[branch] = item
        self._set_placeholder(item, I18N.tr("files.native.tree_expand", "展开查看"))
        return item

    @staticmethod
    def _set_placeholder(item: QStandardItem, text: str) -> None:
        placeholder = QStandardItem(text)
        placeholder.setEnabled(False)
        item.appendRow(placeholder)

    def set_libraries(self, entries: list[dict[str, str]]) -> None:
        registered = {entry["library_id"]: entry for entry in entries}
        parent = self._library_group if self._library_group is not None else self.item_model.invisibleRootItem()
        for library_id, item in tuple(self._library_roots.items()):
            entry = registered.get(library_id)
            if entry is None or item.data(self.PATH_ROLE) != normalize_path(entry["path"]):
                self._forget_item(item)
                parent.removeRow(item.row())
                del self._library_roots[library_id]

        for row, (library_id, entry) in enumerate(registered.items()):
            item = self._library_roots.get(library_id)
            if item is None:
                item = self._make_item(entry["name"], normalize_path(entry["path"]),
                                       branch=f"library:{library_id}", library_id=library_id, icon="library")
                self._library_roots[library_id] = item
                parent.insertRow(row, item)
            else:
                item.setText(entry["name"])
        if self._library_group is not None:
            return
        heading_visible = self._folders_heading.index().isValid()
        if self._library_roots and not heading_visible:
            self.item_model.insertRow(len(self._library_roots), self._folders_heading)
        elif not self._library_roots and heading_visible:
            self.item_model.takeRow(self._folders_heading.row())

    def library_root(self, index: QModelIndex) -> bool:
        root = self._library_roots.get(index.data(self.LIBRARY_ROLE))
        return root is not None and root.index() == index

    def is_library_heading(self, index: QModelIndex) -> bool:
        return self._library_group is not None and self._library_group.index() == index

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.volumes_requested.emit()
        self._volume_poll.start()
        self._folder_counts.clear()
        if self._count_batch is not None:
            self._count_batch[1].clear()
        self._schedule_folder_counts()

    def hideEvent(self, event: QHideEvent) -> None:
        self._volume_poll.stop()
        self._count_timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._schedule_folder_counts()

    def _schedule_folder_counts(self, *_: Any) -> None:
        if self.isVisible() and self._count_batch is None and not self._count_timer.isActive():
            self._count_timer.start(35)

    def _request_visible_folder_counts(self) -> None:
        if not self.isVisible() or self._count_batch is not None:
            return
        viewport = self.viewport()
        index = self.indexAt(QPoint(max(0, viewport.width() - 2), 0))
        paths: dict[str, None] = {}
        while index.isValid() and self.visualRect(index).top() < viewport.height():
            path = index.data(self.PATH_ROLE)
            if path and path not in self._folder_counts:
                paths[path] = None
                if len(paths) == 8:
                    break
            index = self.indexBelow(index)
        if paths:
            request_id = uuid4().hex
            self._count_batch = (request_id, set(paths))
            self.folder_counts_requested.emit(request_id, list(paths))

    def apply_folder_counts(self, request_id: str, counts: dict[str, int | None] | None) -> None:
        if self._count_batch is None or self._count_batch[0] != request_id:
            return
        _, paths = self._count_batch
        self._count_batch = None
        if counts is None:
            if self.isVisible():
                self._count_timer.start(250)
            return
        for path in paths:
            items = self._nodes.get(path)
            if items:
                count = counts.get(path)
                self._folder_counts[path] = count
                for item in items.values():
                    item.setData(count, self.COUNT_ROLE)
        self._schedule_folder_counts()

    def invalidate_folder_count(self, path: str) -> None:
        path = normalize_path(path)
        self._folder_counts.pop(path, None)
        if self._count_batch is not None:
            self._count_batch[1].discard(path)
        if path in self._nodes:
            self._schedule_folder_counts()

    def set_volumes(self, volumes: tuple[MountedVolume, ...]) -> None:
        visible = {volume.path: volume for volume in volumes if volume.path != self.home and
                   self._visible_entry({"name": volume.name, "path": volume.path, "type": "dir"})}
        if visible == self._volumes:
            return

        group_attached = self._volume_group.index().isValid()
        for path, old in self._volumes.items():
            updated = visible.get(path)
            if updated is None or updated.device != old.device:
                item = self._volume_roots.pop(path)
                self._forget_item(item)
                self._volume_group.removeRow(item.row())

        changed = {path for path in self._volumes.keys() | visible.keys()
                   if path not in self._volumes or path not in visible
                   or self._volumes[path].device != visible[path].device}
        for path in changed:
            self._invalidate_mount(path)

        for row, (path, volume) in enumerate(visible.items()):
            if path in self._volume_roots:
                self._volume_roots[path].setText(volume.name)
            else:
                item = self._make_item(volume.name, path, branch=f"volume:{path}", icon="drive")
                self._volume_roots[path] = item
                self._volume_group.insertRow(row, item)
        self._volumes = visible

        if visible and not group_attached:
            self.item_model.insertRow(self.home_item.row(), self._volume_group)
            self.setExpanded(self._volume_group.index(), True)
        elif not visible and group_attached and self._library_group is None:
            self.item_model.takeRow(self._volume_group.row())
        for path in changed & visible.keys():
            for item in (*self._nodes.get(path, {}).values(), *self._library_roots.values()):
                root = item.data(self.PATH_ROLE)
                if self.isExpanded(item.index()) and (root == path or root.startswith(path.rstrip("/") + "/")):
                    self._request_children(item)
        if self._reveal_target:
            self.reveal(self._reveal_target)

    def _invalidate_mount(self, path: str) -> None:
        prefix = path.rstrip("/") + "/"
        self._pending.difference_update(
            {pending for pending in self._pending if pending == path or pending.startswith(prefix)})
        for cached in self._nodes:
            if cached == path or cached.startswith(prefix):
                self.invalidate_folder_count(cached)
        self.tree_root_removed.emit(path)
        roots = list(self._nodes.get(path, {}).values())
        roots.extend(item for item in self._library_roots.values() if item.data(self.PATH_ROLE).startswith(prefix))
        for item in roots:
            self._clear_children(item)
            item.setData(False, self.LOADED_ROLE)
            self._set_placeholder(item, I18N.tr("files.native.tree_expand", "展开查看"))

    def start(self) -> None:
        self.setExpanded(self.home_item.index(), True)
        self._request_children(self.home_item)

    def refresh(self, path: str) -> None:
        path = normalize_path(path)
        items = self._nodes.get(path)
        if not items:
            return
        self.invalidate_folder_count(path)
        if path in self._pending:
            self.tree_reload_requested.emit(path)
        else:
            self._pending.add(path)
            self.tree_requested.emit(path)

    def _expanded(self, index: QModelIndex) -> None:
        item = self.item_model.itemFromIndex(index)
        if item is not None:
            self._request_children(item)

    def _request_children(self, item: QStandardItem) -> None:
        path = item.data(self.PATH_ROLE)
        if path and not item.data(self.LOADED_ROLE) and path not in self._pending:
            self._pending.add(path)
            self.tree_requested.emit(path)

    def _navigate(self, index: QModelIndex) -> None:
        item = self.item_model.itemFromIndex(index)
        if item is not None and (path := item.data(self.PATH_ROLE)):
            self._active_branch = item.data(self.BRANCH_ROLE)
            self._request_children(item)
            self.directory_requested.emit(path)
        elif item is not None and (collection_id := item.data(self.GRADE_SET_ROLE)) is not None:
            self.grade_set_requested.emit(collection_id)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.matches(QKeySequence.StandardKey.Paste):
            if (path := self.currentIndex().data(self.PATH_ROLE)):
                self.paste_requested.emit(path)
            event.accept()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.currentIndex().isValid():
            self._navigate(self.currentIndex())
            event.accept()
        else:
            super().keyPressEvent(event)

    def set_children(self, path: str, entries: list[dict[str, Any]]) -> None:
        self._pending.discard(path)
        # Only nodes immediately below this requested directory are accepted.
        children: dict[str, str] = {}
        for entry in sorted(entries, key=lambda entry: entry["name"].casefold()):
            child_path = normalize_path(entry["path"])
            if posixpath.dirname(child_path).rstrip("/") == path.rstrip("/") and self._visible_entry(
                {**entry, "path": child_path, "type": "dir"}
            ):
                children[child_path] = entry["name"]
        for branch, item in tuple(self._nodes.get(path, {}).items()):
            item.setData(True, self.LOADED_ROLE)
            item.setToolTip("")
            existing: set[str] = set()
            for row in reversed(range(item.rowCount())):
                child = item.child(row)
                child_path = child.data(self.PATH_ROLE)
                if child_path in children:
                    child.setText(children[child_path])
                    existing.add(child_path)
                else:
                    self._forget_item(child)
                    item.removeRow(row)
            for row, (child_path, name) in enumerate(children.items()):
                if child_path not in existing:
                    item.insertRow(row, self._make_item(
                        name, child_path, branch=branch, library_id=item.data(self.LIBRARY_ROLE)))
        if self._reveal_target:
            self.reveal(self._reveal_target)

    def set_error(self, path: str, message: str) -> None:
        self._pending.discard(path)
        for item in tuple(self._nodes.get(path, {}).values()):
            item.setData(False, self.LOADED_ROLE)
            item.setToolTip(message)
            self._clear_children(item)
            self._set_placeholder(item, I18N.tr("files.native.tree_retry", "读取失败，点击目录重试"))

    def _clear_children(self, item: QStandardItem) -> None:
        for row in range(item.rowCount()):
            self._forget_item(item.child(row))
        item.removeRows(0, item.rowCount())

    def _forget_item(self, item: QStandardItem) -> None:
        descendants = [item]
        while descendants:
            node = descendants.pop()
            path = node.data(self.PATH_ROLE)
            if path:
                branches = self._nodes[path]
                branches.pop(node.data(self.BRANCH_ROLE))
                if not branches:
                    del self._nodes[path]
                    self.invalidate_folder_count(path)
                    if path in self._pending:
                        self._pending.remove(path)
                        self.tree_path_removed.emit(path)
            descendants.extend(node.child(row) for row in range(node.rowCount()))

    def reveal(self, path: str) -> None:
        path = normalize_path(path)
        self._reveal_target = path
        items = (*self._library_roots.values(), self.home_item, *self._volume_roots.values())
        roots = ((item.data(self.PATH_ROLE), item.data(self.BRANCH_ROLE), item) for item in items)
        match = max(((root, branch, item) for root, branch, item in roots
                     if path == root or path.startswith(root.rstrip("/") + "/")),
                    key=lambda entry: (entry[1] == self._active_branch, len(entry[0])), default=None)
        if match is None:
            self.setCurrentIndex(QModelIndex())
            return
        root, branch, current = match
        current_path = root
        group = current.parent()
        if (self._library_group is not None and group in (self._library_group, self._volume_group)
                and not self.isExpanded(group.index())):
            self._reveal_target = None
            self.setCurrentIndex(QModelIndex())
            return
        if group is self._volume_group:
            self.setExpanded(self._volume_group.index(), True)
        relative = path[len(root):].strip("/")
        parts = relative.split("/") if relative else []
        for part in parts:
            self.setExpanded(current.index(), True)
            if not current.data(self.LOADED_ROLE):
                self._request_children(current)
                return
            current_path = current_path.rstrip("/") + "/" + part
            current = self._nodes.get(current_path, {}).get(branch)
            if current is None:
                self._reveal_target = None
                return
        with QSignalBlocker(self.selectionModel()):
            self.setCurrentIndex(current.index())
        self.scrollTo(current.index())
        self._reveal_target = None
