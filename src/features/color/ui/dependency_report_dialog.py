"""工程依赖报告对话框（P1-COLOR-23）。

展示 ``DependencyReport``：每行一个外部依赖（LUT / 插件 / AI 模型），
绿=可用（含 SHA-256 短哈希）、红=缺失；支持导出 JSON 基线，用于工程
移动到另一台机器后的 ``compare_reports`` 一致性校验。

用法（File 菜单「工程依赖报告...」）：
    report = build_dependency_report(project, base_dir, ...)
    DependencyReportDialog(parent, report).exec()
"""

from __future__ import annotations

import json
from typing import Optional

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QFileDialog, QDialog, QDialogButtonBox, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from src.features.color.core.project_dependencies import (
    STATUS_OK, DependencyReport,
)
from src.shared.i18n.i18n import I18N


class DependencyReportDialog(QDialog):
    """展示工程依赖清单；可导出 JSON 基线报告。"""

    def __init__(self, parent: Optional[QWidget],
                 report: DependencyReport) -> None:
        super().__init__(parent)
        self._report = report
        self.setWindowTitle(I18N.tr("deps.title", "工程依赖报告"))
        self.setMinimumWidth(640)
        self.setMinimumHeight(360)

        layout = QVBoxLayout(self)

        header = QLabel(I18N.tr(
            "deps.header",
            "工程引用的外部依赖（{ok} 可用 / {total} 总计）。"
            "缺失项需在新机器上重新链接或导入；导出基线 JSON 可在迁移后"
            "校验同名文件内容一致。",
            ok=report.ok_count, total=len(report.entries)))
        header.setWordWrap(True)
        layout.addWidget(header)

        table = QTableWidget(len(report.entries), 5, self)
        table.setHorizontalHeaderLabels([
            I18N.tr("deps.col_kind", "类别"),
            I18N.tr("deps.col_key", "依赖"),
            I18N.tr("deps.col_status", "状态"),
            I18N.tr("deps.col_hash", "SHA-256"),
            I18N.tr("deps.col_detail", "说明"),
        ])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)

        ok_color = QColor("#4caf50")
        missing_color = QColor("#f44336")
        for row, entry in enumerate(report.entries):
            kind_item = QTableWidgetItem(entry.kind)
            key_item = QTableWidgetItem(entry.key)
            status_item = QTableWidgetItem(
                I18N.tr("deps.ok", "可用") if entry.status == STATUS_OK
                else I18N.tr("deps.missing", "缺失"))
            status_item.setForeground(
                ok_color if entry.status == STATUS_OK else missing_color)
            hash_item = QTableWidgetItem(
                (entry.sha256 or "")[:12])
            detail_item = QTableWidgetItem(entry.detail)
            for col, item in enumerate(
                    (kind_item, key_item, status_item, hash_item,
                     detail_item)):
                table.setItem(row, col, item)

        table.horizontalHeader().setStretchLastSection(True)
        table.resizeColumnToContents(0)
        table.resizeColumnToContents(2)
        layout.addWidget(table, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        export_btn = QPushButton(I18N.tr("deps.export_baseline", "导出基线 JSON"))
        buttons.addButton(export_btn, QDialogButtonBox.ButtonRole.ActionRole)
        export_btn.clicked.connect(self._export_baseline)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _export_baseline(self) -> None:
        default = "dependency_baseline.json"
        path, _ = QFileDialog.getSaveFileName(
            self, I18N.tr("deps.export_baseline", "导出基线 JSON"), default,
            "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self._report.to_dict(), handle,
                          ensure_ascii=False, indent=2)
            QMessageBox.information(
                self, I18N.tr("deps.title", "工程依赖报告"),
                I18N.tr("deps.exported", "基线报告已导出到：{path}", path=path))
        except OSError as exc:
            QMessageBox.critical(
                self, I18N.tr("deps.title", "工程依赖报告"), str(exc))
