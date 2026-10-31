"""Scene Outliner panel for imported Model3D hierarchies.

The panel deliberately consumes only the plain metadata dictionary emitted by
Scene3D.  It never receives GL objects or SceneGraph instances, which keeps
the render/UI boundary explicit and makes the widget safe to refresh from a
queued metadata signal.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.shared.i18n.i18n import I18N


class SceneOutlinerPanel(QWidget):
    """Hierarchical Model3D outliner with selection and visibility signals."""

    scene_node_selected = pyqtSignal(str)
    visibility_changed = pyqtSignal(str, bool)
    lock_changed = pyqtSignal(str, bool)
    solo_changed = pyqtSignal(str)  # empty string clears solo

    _ID_ROLE = Qt.ItemDataRole.UserRole
    _TYPE_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._updating = False
        self._items: Dict[str, QTreeWidgetItem] = {}
        self._hierarchy: Optional[dict] = None
        self._hidden_ids: set[str] = set()
        self._locked_ids: set[str] = set()
        self._solo_id = ""
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        title = QLabel(I18N.tr("outliner.title", "场景 Outliner"))
        title.setStyleSheet("color: #ddd; font-weight: bold; font-size: 12px;")
        layout.addWidget(title)

        filter_row = QHBoxLayout()
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(I18N.tr("outliner.search_placeholder", "搜索对象…"))
        self._filter_edit.setClearButtonEnabled(True)
        self._filter_edit.setStyleSheet(
            "QLineEdit { background: #292929; color: #ccc; "
            "border: 1px solid #444; padding: 2px 4px; }"
        )
        self._filter_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self._filter_edit, 1)

        expand_button = QPushButton(I18N.tr("outliner.expand", "展开"))
        expand_button.setFixedWidth(46)
        expand_button.clicked.connect(lambda: self._tree.expandAll())
        filter_row.addWidget(expand_button)
        collapse_button = QPushButton(I18N.tr("outliner.collapse", "收起"))
        collapse_button.setFixedWidth(46)
        collapse_button.clicked.connect(self._tree_collapse_all)
        filter_row.addWidget(collapse_button)
        layout.addLayout(filter_row)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(
            ["对象", "类型", "三角面", "材质", "锁定", "独显"])
        self._tree.setRootIsDecorated(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self._tree.setColumnWidth(0, 170)
        self._tree.setColumnWidth(1, 72)
        self._tree.setColumnWidth(4, 48)
        self._tree.setColumnWidth(5, 48)
        self._tree.itemSelectionChanged.connect(self._on_selection_changed)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.setStyleSheet(
            "QTreeWidget { background: #202024; color: #ccc; "
            "border: 1px solid #333; }"
            "QTreeWidget::item:selected { background: #36506b; }"
        )
        layout.addWidget(self._tree, 1)

        self._summary = QLabel(I18N.tr("outliner.no_hierarchy", "无模型层级"))
        self._summary.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(self._summary)

    def _tree_collapse_all(self) -> None:
        self._tree.collapseAll()

    def clear(self) -> None:
        """Clear the hierarchy and reset transient selection state."""
        self._updating = True
        try:
            self._tree.clear()
            self._items.clear()
            self._hierarchy = None
            self._hidden_ids.clear()
            self._locked_ids.clear()
            self._solo_id = ""
            self._summary.setText(I18N.tr("outliner.no_hierarchy", "无模型层级"))
        finally:
            self._updating = False

    def set_hierarchy(self, summary: Optional[dict],
                      hidden_ids: Iterable[str] = (),
                      locked_ids: Iterable[str] = (),
                      solo_id: str = "") -> None:
        """Replace the tree from a render-thread-safe hierarchy summary."""
        self._updating = True
        try:
            self._tree.clear()
            self._items.clear()
            self._hierarchy = summary if isinstance(summary, dict) else None
            self._hidden_ids = {str(value) for value in hidden_ids if str(value)}
            self._locked_ids = {str(value) for value in locked_ids if str(value)}
            self._solo_id = str(solo_id or "")
            if self._hierarchy is None:
                self._summary.setText(I18N.tr("outliner.no_hierarchy", "无模型层级"))
                return
            root_item = self._build_item(self._hierarchy, None)
            self._tree.addTopLevelItem(root_item)
            root_item.setExpanded(True)
            meshes = self._count_meshes(self._hierarchy)
            triangles = self._count_triangles(self._hierarchy)
            self._summary.setText(
                f"{meshes} 个 Mesh · {triangles:,} 三角面 · "
                f"{len(self._hidden_ids)} 个隐藏"
            )
        finally:
            self._updating = False
        self._apply_filter(self._filter_edit.text())

    def set_selected_node(self, node_id: Optional[str]) -> None:
        """Select a scene node without emitting a selection signal."""
        self._updating = True
        try:
            self._tree.clearSelection()
            item = self._items.get(str(node_id)) if node_id else None
            if item is not None:
                item.setSelected(True)
                self._tree.scrollToItem(item)
        finally:
            self._updating = False

    def hierarchy(self) -> Optional[dict]:
        """Return the latest plain hierarchy summary."""
        return self._hierarchy

    def hidden_mesh_ids(self) -> tuple[str, ...]:
        """Return the current hidden mesh IDs as a stable tuple."""
        return tuple(sorted(self._hidden_ids))

    def _build_item(self, data: dict,
                    parent: Optional[QTreeWidgetItem]) -> QTreeWidgetItem:
        node_id = str(data.get("node_id", "") or "")
        node_type = str(data.get("node_type", "empty") or "empty")
        name = str(data.get("name", "") or "(unnamed)")
        item = QTreeWidgetItem(parent, [
            name,
            node_type,
            f"{int(data.get('triangle_count', 0) or 0):,}",
            str(data.get("material_name", "") or "—"),
            "",
            "",
        ])
        item.setData(0, self._ID_ROLE, node_id)
        item.setData(0, self._TYPE_ROLE, node_type)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        visible = bool(data.get("visible", True)) and node_id not in self._hidden_ids
        item.setCheckState(0, Qt.CheckState.Checked if visible
                           else Qt.CheckState.Unchecked)
        locked = bool(data.get("locked", False)) or node_id in self._locked_ids
        item.setCheckState(4, Qt.CheckState.Checked if locked
                           else Qt.CheckState.Unchecked)
        item.setCheckState(5, Qt.CheckState.Checked
                           if node_id and node_id == self._solo_id
                           else Qt.CheckState.Unchecked)
        if node_id:
            self._items[node_id] = item
        for child in data.get("children", ()):
            if isinstance(child, dict):
                self._build_item(child, item)
        return item

    @staticmethod
    def _count_meshes(data: dict) -> int:
        own = 1 if str(data.get("node_type", "")) in ("mesh", "skinned") else 0
        return own + sum(
            SceneOutlinerPanel._count_meshes(child)
            for child in data.get("children", ()) if isinstance(child, dict)
        )

    @staticmethod
    def _count_triangles(data: dict) -> int:
        own = int(data.get("triangle_count", 0) or 0)
        return own + sum(
            SceneOutlinerPanel._count_triangles(child)
            for child in data.get("children", ()) if isinstance(child, dict)
        )

    @staticmethod
    def _descendants(item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
        result: list[QTreeWidgetItem] = []
        for index in range(item.childCount()):
            child = item.child(index)
            result.append(child)
            result.extend(SceneOutlinerPanel._descendants(child))
        return result

    def _on_selection_changed(self) -> None:
        if self._updating:
            return
        items = self._tree.selectedItems()
        if items:
            node_id = str(items[0].data(0, self._ID_ROLE) or "")
            if node_id:
                self.scene_node_selected.emit(node_id)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating or column not in (0, 4, 5):
            return
        node_id = str(item.data(0, self._ID_ROLE) or "")
        if not node_id:
            return
        if column == 4:
            locked = item.checkState(4) == Qt.CheckState.Checked
            if locked:
                self._locked_ids.add(node_id)
            else:
                self._locked_ids.discard(node_id)
            self.lock_changed.emit(node_id, locked)
            return
        if column == 5:
            solo = item.checkState(5) == Qt.CheckState.Checked
            self._updating = True
            try:
                if solo:
                    previous = self._items.get(self._solo_id)
                    if previous is not None and previous is not item:
                        previous.setCheckState(5, Qt.CheckState.Unchecked)
                    self._solo_id = node_id
                elif self._solo_id == node_id:
                    self._solo_id = ""
            finally:
                self._updating = False
            self.solo_changed.emit(self._solo_id)
            return
        visible = item.checkState(0) == Qt.CheckState.Checked
        if self._item_or_parent_is_locked(item):
            self._updating = True
            try:
                item.setCheckState(
                    0, Qt.CheckState.Unchecked
                    if node_id in self._hidden_ids else Qt.CheckState.Checked)
            finally:
                self._updating = False
            return
        node_type = str(item.data(0, self._TYPE_ROLE) or "")
        targets = [item]
        if node_type not in ("mesh", "skinned"):
            targets = [child for child in self._descendants(item)
                       if str(child.data(0, self._TYPE_ROLE) or "")
                       in ("mesh", "skinned")
                       and not self._item_or_parent_is_locked(child)]
        self._updating = True
        try:
            for target in targets:
                target_id = str(target.data(0, self._ID_ROLE) or "")
                if not target_id:
                    continue
                target.setCheckState(
                    0, Qt.CheckState.Checked if visible
                    else Qt.CheckState.Unchecked)
                if visible:
                    self._hidden_ids.discard(target_id)
                else:
                    self._hidden_ids.add(target_id)
        finally:
            self._updating = False
        for target in targets:
            target_id = str(target.data(0, self._ID_ROLE) or "")
            if target_id:
                self.visibility_changed.emit(target_id, visible)
        self._summary.setText(
            f"{self._count_meshes(self._hierarchy or {})} 个 Mesh · "
            f"{self._count_triangles(self._hierarchy or {}):,} 三角面 · "
            f"{len(self._hidden_ids)} 个隐藏"
        )

    def _item_or_parent_is_locked(self, item: QTreeWidgetItem) -> bool:
        """Return whether an item or one of its Outliner parents is locked."""
        current: Optional[QTreeWidgetItem] = item
        while current is not None:
            current_id = str(current.data(0, self._ID_ROLE) or "")
            if current_id in self._locked_ids:
                return True
            current = current.parent()
        return False

    def _apply_filter(self, text: str) -> None:
        needle = str(text or "").strip().casefold()
        # AUD-10816: the old implementation ran a full descendant DFS for
        # every item — O(n²) on each keystroke of the search box. A single
        # top-down walk computes each item's match once and folds the
        # "any descendant matches" answer bottom-up in O(n) total.
        def _visit(item: QTreeWidgetItem) -> tuple[bool, bool]:
            """Return (visible, subtree_has_match) for ``item``."""
            own_hit = bool(needle) and needle in item.text(0).casefold()
            child_hit = False
            for index in range(item.childCount()):
                _, sub_hit = _visit(item.child(index))
                child_hit = child_hit or sub_hit
            visible = own_hit or child_hit or not needle
            item.setHidden(not visible)
            # Reveal the ancestors of a match (the item itself stays at its
            # current expansion so a plain leaf match doesn't blow the tree
            # open). Original code expanded the parent chain too, but off by
            # one level — the direct parent of a match stayed collapsed,
            # which hid the very row the user searched for.
            if child_hit:
                item.setExpanded(True)
            return visible, (own_hit or child_hit)

        tree = self._tree
        for top_index in range(tree.topLevelItemCount()):
            _visit(tree.topLevelItem(top_index))
