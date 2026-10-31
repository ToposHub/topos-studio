"""M15.1 · 工程快照比较面板（compare_snapshots 的 UI 入口）。

接既有 :func:`compare_snapshots`（结构化只读比较，M02.1 已适配真实
payload schema）：列出两快照的差异条目（片段移动/修剪/换源/调色/
音频/时间线增删），供版本比较/备份检查入口使用。只读——恢复操作走
RestoreSnapshotCommand（M02.1），面板不直接改工程。
"""

from __future__ import annotations


from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from src.features.edit.core.project_snapshots import compare_snapshots
from src.shared.i18n.i18n import I18N


class SnapshotComparePanel(QDialog):
    """两命名快照的结构化比较（只读）。"""

    def __init__(
        self,
        snapshot_payloads: dict[str, dict],
        *,
        from_name: str = "",
        to_name: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._payloads = snapshot_payloads
        self._result = None
        self.setWindowTitle(I18N.tr("snapshot.title", "工程快照比较"))
        layout = QVBoxLayout(self)

        from PyQt6.QtWidgets import QComboBox, QHBoxLayout
        row = QHBoxLayout()
        row.addWidget(QLabel(I18N.tr("snapshot.from_label", "从：")))
        self._from_combo = QComboBox()
        self._from_combo.addItems(sorted(snapshot_payloads))
        if from_name in snapshot_payloads:
            self._from_combo.setCurrentText(from_name)
        row.addWidget(self._from_combo, 1)
        row.addWidget(QLabel(I18N.tr("snapshot.to_label", "到：")))
        self._to_combo = QComboBox()
        self._to_combo.addItems(sorted(snapshot_payloads))
        if to_name in snapshot_payloads:
            self._to_combo.setCurrentText(to_name)
        row.addWidget(self._to_combo, 1)
        layout.addLayout(row)

        self._compare_btn = QPushButton(I18N.tr("ai_variant.compare", "比较"))
        self._compare_btn.clicked.connect(self._on_compare)
        layout.addWidget(self._compare_btn)

        self._status = QLabel(I18N.tr("snapshot.hint", "选择两个快照后点击「比较」"))
        layout.addWidget(self._status)

        self._list = QListWidget()
        layout.addWidget(self._list, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _on_compare(self) -> None:
        from_name = self._from_combo.currentText()
        to_name = self._to_combo.currentText()
        if not from_name or not to_name or from_name == to_name:
            self._status.setText(I18N.tr("snapshot.need_two_diff", "需要两个不同的快照"))
            self._result = None
            return
        from_payload = self._payloads.get(from_name) or {}
        to_payload = self._payloads.get(to_name) or {}
        self._result = compare_snapshots(
            from_payload, to_payload,
            from_name=from_name, to_name=to_name)
        self._list.clear()
        for entry in self._result.entries:
            self._list.addItem(QListWidgetItem(
                f"[{entry.kind}] {entry.timeline_id}: {entry.detail}"))
        summary = self._result.summary
        if not summary:
            self._status.setText(I18N.tr("snapshot.no_diff", "两快照无差异"))
        else:
            parts = "，".join(f"{k}×{v}" for k, v in sorted(summary.items()))
            self._status.setText(f"{len(self._result.entries)} 项差异（{parts}）")

    @property
    def result(self):
        """最近一次比较结果（测试/控制器断言用）。"""
        return self._result


__all__ = ["SnapshotComparePanel"]
