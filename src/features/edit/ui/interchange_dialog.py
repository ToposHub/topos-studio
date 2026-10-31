"""互操作（FCPXML/OTIO）导入/导出报告对话框（P1-16）。

同一对话框承担三种角色：

- 导入预览（confirm_mode=True）：展示 InterchangeReport 分类统计、素材
  落地状态（在线/缺失）后由用户确认（TAEP §9.2 preview → commit）；
- 导入结果：展示落地摘要（新导入/复用/离线素材清单）；
- 导出结果：展示映射报告（保留/转换/丢弃/需人工确认条目）。

不包含任何业务逻辑，只渲染传入的 report / 摘要数据。
"""

from __future__ import annotations

import html
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.adapters.interchange_types import (
    InterchangeEntryCategory,
    InterchangeReport,
)
from src.shared.i18n.i18n import I18N

_CATEGORY_LABELS: dict[InterchangeEntryCategory, tuple[str, str]] = {
    InterchangeEntryCategory.PRESERVED: (
        I18N.tr("edit.interchange.cat_preserved", "保留"), "#5ad06e",
    ),
    InterchangeEntryCategory.CONVERTED: (
        I18N.tr("edit.interchange.cat_converted", "已转换"), "#7fb3ff",
    ),
    InterchangeEntryCategory.DROPPED: (
        I18N.tr("edit.interchange.cat_dropped", "已丢弃"), "#ff6b6b",
    ),
    InterchangeEntryCategory.REQUIRES_USER_ACTION: (
        I18N.tr("edit.interchange.cat_requires_action", "需人工确认"), "#ffb454",
    ),
}


class InterchangeReportDialog(QDialog):
    """渲染 InterchangeReport（+ 可选素材行 / 导入摘要）的对话框。"""

    def __init__(
        self,
        report: InterchangeReport,
        *,
        title: str = "",
        asset_rows: Optional[list[tuple[str, str, bool]]] = None,
        summary_text: str = "",
        confirm_mode: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            title
            or I18N.tr("edit.interchange.report_title", "互操作报告")
        )
        self.setMinimumSize(560, 420)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ---- 分类统计行 ----
        counts_layout = QVBoxLayout()
        if report.entries:
            parts: list[str] = []
            for category in (
                InterchangeEntryCategory.PRESERVED,
                InterchangeEntryCategory.CONVERTED,
                InterchangeEntryCategory.DROPPED,
                InterchangeEntryCategory.REQUIRES_USER_ACTION,
            ):
                count = sum(1 for e in report.entries if e.category == category)
                if count:
                    label, color = _CATEGORY_LABELS[category]
                    parts.append(f'<span style="color:{color}">{label}: {count}</span>')
            stats = QLabel("　|　".join(parts))
            stats.setTextFormat(Qt.TextFormat.RichText)
            counts_layout.addWidget(stats)
        for diag in report.diagnostics:
            severity = diag.get("severity", "info")
            color = "#ff6b6b" if severity == "error" else "#ffb454"
            msg = diag.get("message", "")
            code = diag.get("code", "")
            # AUD-20103: 诊断 message 携带外部文件异常文本（路径中可含 &/<
            # 等合法字符），QLabel AutoText 检测到 <span> 后按富文本解析，
            # 未转义即拼接会导致渲染残缺/内容被吞。先转义再入富文本。
            diag_label = QLabel(
                '<span style="color:{color}">[{severity}] {msg} ({code})</span>'.format(
                    color=color,
                    severity=html.escape(str(severity)),
                    msg=html.escape(str(msg)),
                    code=html.escape(str(code)),
                )
            )
            diag_label.setTextFormat(Qt.TextFormat.RichText)
            counts_layout.addWidget(diag_label)
        if counts_layout.count():
            layout.addLayout(counts_layout)

        # ---- 映射条目表 ----
        if report.entries:
            table = QTableWidget(len(report.entries), 3)
            table.setHorizontalHeaderLabels([
                I18N.tr("edit.interchange.col_category", "类别"),
                I18N.tr("edit.interchange.col_element", "元素"),
                I18N.tr("edit.interchange.col_description", "说明"),
            ])
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            for row, entry in enumerate(report.entries):
                label, color = _CATEGORY_LABELS.get(
                    entry.category, (entry.category.value, "#aaaaaa")
                )
                cat_item = QTableWidgetItem(label)
                cat_item.setForeground(QColor(color))
                table.setItem(row, 0, cat_item)
                table.setItem(row, 1, QTableWidgetItem(entry.element))
                table.setItem(row, 2, QTableWidgetItem(entry.description))
            header = table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            layout.addWidget(table, 1)

        # ---- 素材落地状态（导入预览/结果） ----
        if asset_rows:
            asset_table = QTableWidget(len(asset_rows), 3)
            asset_table.setHorizontalHeaderLabels([
                I18N.tr("edit.interchange.col_asset_name", "素材"),
                I18N.tr("edit.interchange.col_asset_path", "路径"),
                I18N.tr("edit.interchange.col_asset_status", "状态"),
            ])
            asset_table.verticalHeader().setVisible(False)
            asset_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            for row, (name, path, exists) in enumerate(asset_rows):
                status_item = QTableWidgetItem(
                    I18N.tr("edit.interchange.status_online", "在线")
                    if exists
                    else I18N.tr("edit.interchange.status_missing", "缺失 → 离线")
                )
                status_item.setForeground(
                    QColor("#5ad06e" if exists else "#ffb454")
                )
                asset_table.setItem(row, 0, QTableWidgetItem(name))
                asset_table.setItem(row, 1, QTableWidgetItem(path))
                asset_table.setItem(row, 2, status_item)
            aheader = asset_table.horizontalHeader()
            aheader.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            aheader.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            aheader.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            layout.addWidget(asset_table, 1)

        if summary_text:
            summary_label = QLabel(summary_text)
            summary_label.setWordWrap(True)
            layout.addWidget(summary_label)

        # ---- 按钮 ----
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | (
                QDialogButtonBox.StandardButton.Cancel
                if confirm_mode
                else QDialogButtonBox.StandardButton.NoButton
            )
        )
        buttons.accepted.connect(self.accept)
        if confirm_mode:
            buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
