"""Home-rooted lazy directory navigation populated by the existing service."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QModelIndex, QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtGui import QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import QAbstractItemView, QStyle, QTreeView, QWidget

from src.shared.i18n.i18n import I18N


class HomeDirectoryTree(QTreeView):
    directory_requested = pyqtSignal(str)
    tree_requested = pyqtSignal(str)
    PATH_ROLE = int(Qt.ItemDataRole.UserRole)

    def __init__(
        self, home: str, visible_entry: Callable[[dict[str, Any]], bool],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("filesHomeTree")
        self.home = home.rstrip("/")
        self._visible_entry = visible_entry
        self._nodes: dict[str, QStandardItem] = {}
        self._loaded: set[str] = set()
        self._pending: set[str] = set()
        self._reveal_target: str | None = None
        self.item_model = QStandardItemModel(self)
        self.setModel(self.item_model)
        self.setHeaderHidden(True)
        self.setIndentation(14)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setUniformRowHeights(True)
        self.home_item = self._make_item(I18N.tr("files.native.home_root", "Home"), self.home, True)
        self.item_model.appendRow(self.home_item)
        self.expanded.connect(self._expanded)
        self.clicked.connect(self._navigate)

    def _make_item(self, name: str, path: str, home: bool = False) -> QStandardItem:
        item = QStandardItem(name)
        item.setEditable(False)
        item.setData(path, self.PATH_ROLE)
        item.setToolTip(path)
        item.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DirHomeIcon if home else QStyle.StandardPixmap.SP_DirIcon))
        self._nodes[path] = item
        placeholder = QStandardItem(I18N.tr("files.native.tree_expand", "展开查看"))
        placeholder.setEnabled(False)
        item.appendRow(placeholder)
        return item

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
        self._clear_children(path, item)
        self._loaded.add(path)
        for entry in sorted(children, key=lambda entry: entry["name"].casefold()):
            item.appendRow(self._make_item(entry["name"], entry["path"]))
        if self._reveal_target:
            self.reveal(self._reveal_target)

    def set_error(self, path: str, message: str) -> None:
        self._pending.discard(path)
        self._loaded.discard(path)
        item = self._nodes.get(path)
        if item is not None:
            item.setToolTip(f"{path}\n{message}")
            self._clear_children(path, item)
            placeholder = QStandardItem(I18N.tr("files.native.tree_retry", "读取失败，点击目录重试"))
            placeholder.setEnabled(False)
            item.appendRow(placeholder)

    def _clear_children(self, path: str, item: QStandardItem) -> None:
        removed = {key for key in self._nodes if key.startswith(path.rstrip("/") + "/")}
        for key in removed:
            self._nodes.pop(key)
        self._loaded.difference_update(removed)
        self._pending.difference_update(removed)
        item.removeRows(0, item.rowCount())

    def reveal(self, path: str) -> None:
        if path != self.home and not path.startswith(self.home + "/"):
            self._reveal_target = None
            self.clearSelection()
            return
        self._reveal_target = path
        current = self.home_item
        current_path = self.home
        parts = path[len(self.home):].strip("/").split("/") if path != self.home else []
        for part in parts:
            self.setExpanded(current.index(), True)
            if current_path not in self._loaded:
                self._request_children(current_path)
                return
            current_path += "/" + part
            current = self._nodes.get(current_path)
            if current is None:
                self._reveal_target = None
                return
        with QSignalBlocker(self.selectionModel()):
            self.setCurrentIndex(current.index())
        self.scrollTo(current.index())
        self._reveal_target = None
