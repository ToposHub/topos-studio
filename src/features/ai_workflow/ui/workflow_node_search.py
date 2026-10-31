"""P10.2b WorkflowNodeSearchPopup —— Tab 节点搜索弹窗（plan §10 P10.2）。

> 借鉴 Composite ``NodeSearchPopup`` 但精简（无 favorites/recent/aliases/多 tab）。
> 数据源是 :class:`NodeSpecRegistry` 的 ``all_specs()``；用户选中 spec_id 后通过
> ``node_selected`` signal 通知 host（WorkflowPanel）加节点。

设计：

- :class:`WorkflowNodeSearchPopup`（``QDialog``）：上方 ``QLineEdit`` 过滤 + 下方
  ``QListWidget`` 列所有 spec（display_name 显示，UserRole 存 spec_id）。
- 选中（双击或 Enter）→ emit ``node_selected(spec_id)`` + ``accept()``。
- 过滤：按 display_name / spec_id / description 子串匹配（大小写不敏感）。

红线（plan §1.4.1 / §2.2）：

- 不引入 ``moderngl`` / ``composite.core``（纯 Qt 弹窗）。
- 不传 GPU texture / OpenGL context。
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
)

from src.shared.i18n.i18n import I18N

UserRole = Qt.ItemDataRole.UserRole
# AUD-24001: 完整 description 存独立 role——显示层截断到 48 字符仅为排版，
# 过滤必须按文档承诺匹配完整描述。
DescriptionRole = Qt.ItemDataRole.UserRole + 1


class WorkflowNodeSearchPopup(QDialog):
    """Tab 节点搜索弹窗（从 NodeSpecRegistry 选 spec 加节点）。

    Signals:
        node_selected(str): 用户选中的 spec_id。
    """

    node_selected = pyqtSignal(str)

    def __init__(self, registry: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self._registry = registry
        self.setWindowTitle(I18N.tr("node_search.add_node", "Add Node"))
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Popup
        )
        self.resize(420, 460)
        self._build_ui()
        self._populate()

    # ---------------- 构建 ---------------- #

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(I18N.tr("workflow.node_search.placeholder", "Search nodes…"))
        self.search_edit.textChanged.connect(self._filter_items)
        layout.addWidget(self.search_edit)

        self.list_widget = QListWidget()
        self.list_widget.itemActivated.connect(self._on_item_activated)
        layout.addWidget(self.list_widget, 1)

    def _populate(self) -> None:
        """从 registry.all_specs() 填充列表。"""
        self.list_widget.clear()
        specs = self._registry.all_specs() if hasattr(self._registry, "all_specs") else ()
        # 按 display_name 排序（稳定）
        ordered = sorted(specs, key=lambda s: getattr(s, "display_name", "") or "")
        for spec in ordered:
            display = getattr(spec, "display_name", "") or getattr(spec, "spec_id", "")
            desc = getattr(spec, "description", "") or ""
            label = display
            if desc:
                label = f"{display}  —  {desc[:48]}"
            item = QListWidgetItem(label)
            item.setData(UserRole, getattr(spec, "spec_id", ""))
            item.setData(DescriptionRole, desc)
            self.list_widget.addItem(item)

    # ---------------- 过滤 ---------------- #

    def _filter_items(self, text: str = "") -> None:
        """按 text 过滤（display_name / spec_id / 完整 description 子串，大小写不敏感）。"""
        if not text:
            text = self.search_edit.text()
        needle = (text or "").lower().strip()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            display = item.text().lower()
            sid = (item.data(UserRole) or "").lower()
            # AUD-24001: 匹配完整 description（role 存储），而非截断到
            # 48 字符后的显示文案。
            desc = (item.data(DescriptionRole) or "").lower()
            match = (
                (not needle) or needle in display or needle in sid
                or needle in desc
            )
            item.setHidden(not match)

    # ---------------- 选中 ---------------- #

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        """双击/Enter → 确认选中。"""
        self.list_widget.setCurrentItem(item)
        self._confirm_selection()

    def _confirm_selection(self) -> None:
        """确认当前选中项 → emit node_selected(spec_id) + accept。"""
        item = self.list_widget.currentItem()
        if item is None:
            return
        sid = item.data(UserRole)
        if sid:
            self.node_selected.emit(sid)
            self.accept()


__all__ = ["WorkflowNodeSearchPopup"]
