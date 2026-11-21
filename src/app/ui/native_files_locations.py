"""Lazy directory navigation for Home and mounted data volumes."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import sys
from typing import Any

from PyQt6.QtCore import QModelIndex, QSignalBlocker, QStorageInfo, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QHideEvent, QShowEvent, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import QAbstractItemView, QStyle, QTreeView, QWidget

from src.features.files.contracts import normalize_path
from src.shared.i18n.i18n import I18N


@dataclass(frozen=True)
class MountedVolume:
    path: str
    name: str
    device: bytes


def mounted_data_volumes() -> tuple[MountedVolume, ...]:
    """Read the OS mount table on a worker, without scanning directory contents."""
    volumes = []
    for storage in QStorageInfo.mountedVolumes():
        if not storage.isValid() or not storage.isReady() or storage.isRoot():
            continue
        path = normalize_path(storage.rootPath())
        if sys.platform == "darwin" and path.rpartition("/")[0] != "/Volumes":
            continue
        if sys.platform.startswith("linux") and not (
            path == "/mnt" or path.startswith(("/mnt/", "/media/", "/run/media/"))
        ):
            continue
        name = storage.displayName() or path.rsplit("/", 1)[-1] or path
        if path and name:
            volumes.append(MountedVolume(path, name, bytes(storage.device())))
    return tuple(sorted(volumes, key=lambda volume: (volume.name.casefold(), volume.path)))


class HomeDirectoryTree(QTreeView):
    directory_requested = pyqtSignal(str)
    tree_requested = pyqtSignal(str)
    tree_root_removed = pyqtSignal(str)
    volumes_requested = pyqtSignal()
    PATH_ROLE = int(Qt.ItemDataRole.UserRole)

    def __init__(
        self, home: str, visible_entry: Callable[[dict[str, Any]], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("filesHomeTree")
        self.home = normalize_path(home)
        self._visible_entry = visible_entry
        self._nodes: dict[str, QStandardItem] = {}
        self._loaded: set[str] = set()
        self._pending: set[str] = set()
        self._reveal_target: str | None = None
        self._volumes: dict[str, MountedVolume] = {}
        self.item_model = QStandardItemModel(self)
        self.setModel(self.item_model)
        self.setHeaderHidden(True)
        self.setIndentation(14)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setUniformRowHeights(True)
        self.home_item = self._make_item(I18N.tr("files.native.home_root", "Home"), self.home,
                                         QStyle.StandardPixmap.SP_DirHomeIcon)
        self.item_model.appendRow(self.home_item)
        self._volume_group = QStandardItem(I18N.tr("files.native.volumes_heading", "磁盘"))
        self._volume_group.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self._volume_poll = QTimer(self)
        self._volume_poll.setInterval(2500)
        self._volume_poll.timeout.connect(self.volumes_requested)
        self.expanded.connect(self._expanded)
        self.clicked.connect(self._navigate)

    def _make_item(self, name: str, path: str,
                   icon: QStyle.StandardPixmap = QStyle.StandardPixmap.SP_DirIcon) -> QStandardItem:
        item = QStandardItem(name)
        item.setEditable(False)
        item.setData(path, self.PATH_ROLE)
        item.setToolTip(path)
        item.setIcon(self.style().standardIcon(icon))
        self._nodes[path] = item
        placeholder = QStandardItem(I18N.tr("files.native.tree_expand", "展开查看"))
        placeholder.setEnabled(False)
        item.appendRow(placeholder)
        return item

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.volumes_requested.emit()
        self._volume_poll.start()

    def hideEvent(self, event: QHideEvent) -> None:
        self._volume_poll.stop()
        super().hideEvent(event)

    def set_volumes(self, volumes: tuple[MountedVolume, ...]) -> None:
        visible = {volume.path: volume for volume in volumes if volume.path != self.home and
                   self._visible_entry({"name": volume.name, "path": volume.path, "type": "dir"})}
        if visible == self._volumes:
            return

        group_attached = self._volume_group.index().isValid()
        for path, old in self._volumes.items():
            updated = visible.get(path)
            if updated is None or updated.device != old.device:
                item = self._nodes.pop(path)
                self._clear_children(item)
                self._loaded.discard(path)
                self._pending.discard(path)
                self._volume_group.removeRow(item.row())
                self.tree_root_removed.emit(path)

        for row, (path, volume) in enumerate(visible.items()):
            if path in self._volumes and path in self._nodes:
                self._nodes[path].setText(volume.name)
            else:
                existing = self._nodes.pop(path, None)
                if existing is not None:
                    self._clear_children(existing)
                    self._loaded.discard(path)
                    self._pending.discard(path)
                    existing.parent().removeRow(existing.row())
                    self.tree_root_removed.emit(path)
                item = self._make_item(volume.name, path, QStyle.StandardPixmap.SP_DriveHDIcon)
                self._volume_group.insertRow(row, item)
        self._volumes = visible

        if visible and not group_attached:
            self.item_model.insertRow(0, self._volume_group)
            self.setExpanded(self._volume_group.index(), True)
        elif not visible and group_attached:
            self.item_model.takeRow(self._volume_group.row())
        if self._reveal_target:
            self.reveal(self._reveal_target)

    def start(self) -> None:
        self.setExpanded(self.home_item.index(), True)
        self._request_children(self.home)

    def refresh(self, path: str) -> None:
        if path in self._nodes:
            self._loaded.discard(path)
            self._request_children(path)

    def _expanded(self, index: QModelIndex) -> None:
        path = index.data(self.PATH_ROLE)
        if path:
            self._request_children(path)

    def _request_children(self, path: str) -> None:
        if path not in self._loaded and path not in self._pending:
            self._pending.add(path)
            self.tree_requested.emit(path)

    def _navigate(self, index: QModelIndex) -> None:
        path = index.data(self.PATH_ROLE)
        if path:
            self._request_children(path)
            self.directory_requested.emit(path)

    def set_children(self, path: str, entries: list[dict[str, Any]]) -> None:
        item = self._nodes.get(path)
        if item is None:
            return
        self._pending.discard(path)
        # Only nodes immediately below this requested directory are accepted.
        children = [entry for entry in entries
                    if entry["path"].replace("\\", "/").rstrip("/").rsplit("/", 1)[0] == path.rstrip("/")
                    and self._visible_entry({**entry, "type": "dir"})]
        self._clear_children(item)
        self._loaded.add(path)
        for entry in sorted(children, key=lambda entry: entry["name"].casefold()):
            child_path = normalize_path(entry["path"])
            if child_path not in self._nodes:
                item.appendRow(self._make_item(entry["name"], child_path))
        if self._reveal_target:
            self.reveal(self._reveal_target)

    def set_error(self, path: str, message: str) -> None:
        self._pending.discard(path)
        self._loaded.discard(path)
        item = self._nodes.get(path)
        if item is not None:
            item.setToolTip(f"{path}\n{message}")
            self._clear_children(item)
            placeholder = QStandardItem(I18N.tr("files.native.tree_retry", "读取失败，点击目录重试"))
            placeholder.setEnabled(False)
            item.appendRow(placeholder)

    def _clear_children(self, item: QStandardItem) -> None:
        children = [item.child(row) for row in range(item.rowCount())]
        while children:
            child = children.pop()
            path = child.data(self.PATH_ROLE)
            if path:
                self._nodes.pop(path, None)
                self._loaded.discard(path)
                self._pending.discard(path)
            children.extend(child.child(row) for row in range(child.rowCount()))
        item.removeRows(0, item.rowCount())

    def reveal(self, path: str) -> None:
        path = normalize_path(path)
        self._reveal_target = path
        root = max((root for root in (self.home, *self._volumes)
                    if path == root or path.startswith(root.rstrip("/") + "/")), key=len, default=None)
        if root is None:
            self.clearSelection()
            return
        current = self._nodes[root]
        current_path = root
        if root != self.home:
            self.setExpanded(self._volume_group.index(), True)
        relative = path[len(root):].strip("/")
        parts = relative.split("/") if relative else []
        for part in parts:
            self.setExpanded(current.index(), True)
            if current_path not in self._loaded:
                self._request_children(current_path)
                return
            current_path = current_path.rstrip("/") + "/" + part
            current = self._nodes.get(current_path)
            if current is None:
                self._reveal_target = None
                return
        with QSignalBlocker(self.selectionModel()):
            self.setCurrentIndex(current.index())
        self.scrollTo(current.index())
        self._reveal_target = None
