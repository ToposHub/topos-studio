"""Local-only effect browser and ordered insertion rack for the mixer UI."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QPushButton, QVBoxLayout, QWidget,
)

from src.shared.ui.widgets.chevron_combo import ChevronComboBox

from .catalog import EFFECT_CATALOG, EffectInstance, get_effect
from .windows import EffectWindowManager


class EffectBrowserWidget(QWidget):
    effectChosen = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scope: str | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        title = QLabel("音频效果器", self)
        title.setStyleSheet("font-weight:600; color:#E4E4EA;")
        layout.addWidget(title)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("搜索效果器 / Search effects")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refresh)
        layout.addWidget(self.search)
        self.group_combo = ChevronComboBox(self)
        self.group_combo.addItem("全部分类")
        self.group_combo.addItems(list(dict.fromkeys(entry.group for entry in EFFECT_CATALOG)))
        self.group_combo.currentTextChanged.connect(self._refresh)
        layout.addWidget(self.group_combo)
        self.list_widget = QListWidget(self)
        self.list_widget.setAlternatingRowColors(False)
        self.list_widget.itemDoubleClicked.connect(self._choose_item)
        layout.addWidget(self.list_widget, 1)
        self.add_button = QPushButton("插入选中的效果器", self)
        self.add_button.clicked.connect(self._choose_selected)
        layout.addWidget(self.add_button)
        note = QLabel("参数界面已就绪 · DSP / 导出后端待接入", self)
        note.setWordWrap(True)
        note.setStyleSheet("color:#92929E;font-size:10px;background:transparent;")
        layout.addWidget(note)
        self._refresh()

    def set_scope(self, scope: str | None) -> None:
        self._scope = scope
        self._refresh()

    def _refresh(self, _value: str = "") -> None:
        query = self.search.text().strip().casefold()
        group = self.group_combo.currentText()
        self.list_widget.clear()
        for descriptor in EFFECT_CATALOG:
            if self._scope and self._scope not in descriptor.scope:
                continue
            if group != "全部分类" and descriptor.group != group:
                continue
            if query and query not in f"{descriptor.name_zh} {descriptor.name_en} {descriptor.effect_id}".casefold():
                continue
            item = QListWidgetItem(f"{descriptor.name_zh}\n{descriptor.name_en}")
            item.setData(Qt.ItemDataRole.UserRole, descriptor.effect_id)
            item.setToolTip(f"{descriptor.group} · UI 参数面板\n音频预览 / 导出处理未连接")
            self.list_widget.addItem(item)
        self.add_button.setEnabled(self.list_widget.count() > 0)
        if self.list_widget.count():
            self.list_widget.setCurrentRow(0)

    def _choose_item(self, item: QListWidgetItem) -> None:
        effect_id = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(effect_id, str):
            self.effectChosen.emit(effect_id)

    def _choose_selected(self) -> None:
        item = self.list_widget.currentItem()
        if item is not None:
            self._choose_item(item)


class EffectRackWidget(QWidget):
    changed = pyqtSignal(str, list)

    def __init__(self, manager: EffectWindowManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self._owner_id = ""
        self._owner_name = ""
        self._scope = "track"
        self._instances: list[EffectInstance] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.title = QLabel("插入效果", self)
        layout.addWidget(self.title)
        self.list_widget = QListWidget(self)
        self.list_widget.setMinimumHeight(72)
        self.list_widget.itemDoubleClicked.connect(lambda _item: self.open_selected())
        layout.addWidget(self.list_widget, 1)
        row = QHBoxLayout()
        row.setSpacing(3)
        self.add_button = QPushButton("＋", self)
        self.add_button.setToolTip("插入效果器")
        self.add_button.clicked.connect(self._show_add_menu)
        row.addWidget(self.add_button)
        for text, tip, callback in (("编辑", "打开参数面板", self.open_selected),
                                    ("旁路", "切换选中效果旁路", self.toggle_selected),
                                    ("↑", "向前移动", lambda: self.move_selected(-1)),
                                    ("↓", "向后移动", lambda: self.move_selected(1)),
                                    ("−", "移除选中的效果", self.remove_selected)):
            button = QPushButton(text, self)
            button.setToolTip(tip)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        manager.instanceChanged.connect(self._parameter_changed)
        self.setEnabled(False)

    def set_owner(self, owner_id: str, name: str, instances: list[EffectInstance], scope: str = "track") -> None:
        if any(instance.owner_id != owner_id for instance in instances):
            raise ValueError("Rack instances must belong to the selected owner")
        self._owner_id, self._owner_name, self._scope = owner_id, name, scope
        self._instances = list(instances)
        self.title.setText(f"{name} · 插入效果")
        self.setEnabled(bool(owner_id))
        self._refresh()

    def instances(self) -> list[EffectInstance]:
        return list(self._instances)

    def insert_effect(self, effect_id: str) -> EffectInstance:
        if not self._owner_id:
            raise ValueError("Select an owner before inserting an effect")
        descriptor = get_effect(effect_id)
        if self._scope not in descriptor.scope:
            raise ValueError(f"{descriptor.name_zh} does not support {self._scope} owners")
        instance = EffectInstance.create(effect_id, self._owner_id)
        self._instances.append(instance)
        self._refresh(len(self._instances) - 1)
        self._emit_changed()
        return instance

    def _refresh(self, selected: int = 0) -> None:
        self.list_widget.clear()
        for instance in self._instances:
            descriptor = get_effect(instance.effect_id)
            prefix = "●" if instance.enabled else "○"
            item = QListWidgetItem(f"{prefix}  {descriptor.name_zh}")
            item.setData(Qt.ItemDataRole.UserRole, instance.instance_id)
            item.setToolTip(f"{descriptor.name_en} · {'启用' if instance.enabled else '旁路'}\nUI 参数状态；音频处理未连接")
            self.list_widget.addItem(item)
        if self._instances:
            self.list_widget.setCurrentRow(max(0, min(selected, len(self._instances) - 1)))

    def _emit_changed(self) -> None:
        self.changed.emit(self._owner_id, list(self._instances))

    def _parameter_changed(self, instance: EffectInstance) -> None:
        if instance.owner_id == self._owner_id and any(item is instance for item in self._instances):
            self._refresh(self.list_widget.currentRow())
            self._emit_changed()

    def open_selected(self) -> None:
        index = self.list_widget.currentRow()
        if 0 <= index < len(self._instances):
            self.manager.open(self._instances[index], self._owner_name)

    def toggle_selected(self) -> None:
        index = self.list_widget.currentRow()
        if 0 <= index < len(self._instances):
            instance = self._instances[index]
            self.manager.set_enabled(instance, not instance.enabled)
            self._refresh(index)
            self._emit_changed()

    def move_selected(self, delta: int) -> None:
        index = self.list_widget.currentRow()
        target = index + delta
        if 0 <= index < len(self._instances) and 0 <= target < len(self._instances):
            self._instances[index], self._instances[target] = self._instances[target], self._instances[index]
            self._refresh(target)
            self._emit_changed()

    def remove_selected(self) -> None:
        index = self.list_widget.currentRow()
        if 0 <= index < len(self._instances):
            instance = self._instances.pop(index)
            self.manager.close_instance(instance.instance_id, instance.owner_id)
            self._refresh(index)
            self._emit_changed()

    def _show_add_menu(self) -> None:
        menu = QMenu(self)
        groups: dict[str, QMenu] = {}
        for descriptor in EFFECT_CATALOG:
            if self._scope not in descriptor.scope:
                continue
            if descriptor.group not in groups:
                groups[descriptor.group] = menu.addMenu(descriptor.group)
            action = groups[descriptor.group].addAction(descriptor.name_zh)
            action.triggered.connect(lambda _checked=False, effect_id=descriptor.effect_id: self.insert_effect(effect_id))
        menu.exec(self.add_button.mapToGlobal(self.add_button.rect().bottomLeft()))
