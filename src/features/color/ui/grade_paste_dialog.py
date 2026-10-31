"""Paste Grade Attributes 类别选择对话框（P1-COLOR-16）。

粘贴调色前弹出，列出剪贴板实际捕获的类别（复选框，默认全选），
用户可精确选择要应用的参数组——满足「粘贴可预览变更清单」的验收
标准。对话框是纯查询 UI，不修改工程状态。

用法：
    categories = GradePasteDialog.get_categories(
        parent, clipboard.copied_categories)
    if categories is not None:      # None = 取消
        clipboard.paste_onto_layers(layers, categories=categories)
"""

from __future__ import annotations

from typing import Optional, Set

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout,
    QWidget,
)

from src.features.color.core.grade_clipboard import (
    GRADE_CATEGORY_KEYS, grade_category_label,
)

#: 注册表顺序（tracking/raw 在尾部）
_CATEGORY_ORDER = list(GRADE_CATEGORY_KEYS) + ["tracking", "raw"]


class GradePasteDialog(QDialog):
    """类别复选对话框：返回用户选择的类别集合。"""

    def __init__(self, parent: Optional[QWidget],
                 available: Set[str]) -> None:
        super().__init__(parent)
        from src.shared.i18n.i18n import I18N
        self.setWindowTitle(I18N.tr("gradepaste.title", "Paste Grade Attributes"))
        self.setMinimumWidth(320)
        self._checkboxes = {}

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(I18N.tr(
            "gradepaste.prompt", "选择要粘贴的调色属性类别：")))

        for cat in _CATEGORY_ORDER:
            if cat not in available:
                continue
            cb = QCheckBox(grade_category_label(cat))
            cb.setChecked(True)
            layout.addWidget(cb)
            self._checkboxes[cat] = cb

        select_all = QPushButton(I18N.tr("gradepaste.toggle_all", "全选 / 全不选"))
        select_all.clicked.connect(self._toggle_all)
        layout.addWidget(select_all)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _toggle_all(self) -> None:
        target = not all(cb.isChecked() for cb in self._checkboxes.values())
        for cb in self._checkboxes.values():
            cb.setChecked(target)

    def selected_categories(self) -> Set[str]:
        return {cat for cat, cb in self._checkboxes.items() if cb.isChecked()}

    @classmethod
    def get_categories(cls, parent: Optional[QWidget],
                       available: Set[str]) -> Optional[Set[str]]:
        """模态询问；返回选中的类别集合，取消返回 None。"""
        dialog = cls(parent, available)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.selected_categories()
