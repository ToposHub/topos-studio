"""Asset dependency report panel for the Composite page.

The widget is intentionally a thin UI boundary.  It displays the immutable
``AssetDependencyReport`` produced by the core collector and emits requests
for page-level operations.  It never resolves paths, mutates the graph, or
touches render-thread resources itself.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QColor, QBrush
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.features.composite.core.asset_dependency import (
    AssetDependencyReport,
    AssetReference,
    STATUS_MISSING,
    STATUS_MISSING_PATH,
    STATUS_RELINK_CANDIDATE,
    STATUS_RESOLVED,
    STATUS_UNVERIFIED,
)
from src.shared.i18n.i18n import I18N


_STATUS_LABELS = {
    STATUS_MISSING_PATH: "未设置",
    STATUS_UNVERIFIED: "未验证",
    STATUS_MISSING: "缺失",
    STATUS_RESOLVED: "已解析",
    STATUS_RELINK_CANDIDATE: "可重链接",
}

_STATUS_COLORS = {
    STATUS_MISSING_PATH: "#e88",
    STATUS_UNVERIFIED: "#e8c078",
    STATUS_MISSING: "#e88",
    STATUS_RESOLVED: "#9c9",
    STATUS_RELINK_CANDIDATE: "#8cc8e8",
}


class AssetDependencyPanel(QWidget):
    """Show Composite asset dependencies and request safe page operations."""

    refresh_requested = pyqtSignal()
    relink_requested = pyqtSignal(str, str)
    collect_requested = pyqtSignal()
    search_root_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._report: Optional[AssetDependencyReport] = None
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        title = QLabel(I18N.tr("panel.asset_dependency", "资产依赖"))
        title.setStyleSheet("color: #ddd; font-weight: bold; font-size: 12px;")
        layout.addWidget(title)

        toolbar = QHBoxLayout()
        self._refresh_button = QPushButton(I18N.tr("asset_deps.refresh", "刷新"))
        self._refresh_button.setToolTip(
            I18N.tr("asset_deps.refresh_tooltip", "重新扫描当前合成图中的显式资产路径"))
        self._refresh_button.clicked.connect(self.refresh_requested.emit)
        toolbar.addWidget(self._refresh_button)

        self._collect_button = QPushButton(I18N.tr("asset_deps.collect", "收集到目录…"))
        self._collect_button.setToolTip(
            I18N.tr("asset_deps.collect_tooltip", "复制已解析依赖并将图中的路径写回为可移植路径"))
        self._collect_button.clicked.connect(self.collect_requested.emit)
        toolbar.addWidget(self._collect_button)

        self._search_root_button = QPushButton(I18N.tr("asset_deps.search_root", "搜索目录…"))
        self._search_root_button.setToolTip(
            I18N.tr("asset_deps.search_root_tooltip", "添加离线素材搜索目录并保存到当前合成图"))
        self._search_root_button.clicked.connect(self.search_root_requested.emit)
        toolbar.addWidget(self._search_root_button)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self._search_roots_label = QLabel(
            I18N.tr("asset_deps.search_roots_unset", "搜索目录：未设置"))
        self._search_roots_label.setStyleSheet("color: #777; font-size: 10px;")
        self._search_roots_label.setWordWrap(False)
        layout.addWidget(self._search_roots_label)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["节点", "参数", "角色", "状态", "路径", "操作"]
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(False)
        self._table.setColumnWidth(0, 92)
        self._table.setColumnWidth(1, 120)
        self._table.setColumnWidth(2, 92)
        self._table.setColumnWidth(3, 72)
        self._table.setColumnWidth(5, 84)
        self._table.setStyleSheet(
            "QTableWidget { background: #202024; color: #ccc; "
            "border: 1px solid #333; }"
            "QTableWidget::item:selected { background: #36506b; }"
        )
        layout.addWidget(self._table, 1)

        self._summary = QLabel(I18N.tr("asset_deps.not_scanned", "尚未扫描资产依赖"))
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet("color: #888; font-size: 10px;")
        layout.addWidget(self._summary)

    @property
    def report(self) -> Optional[AssetDependencyReport]:
        """Return the latest immutable report shown by the panel."""
        return self._report

    def update_report(self, report: Optional[AssetDependencyReport]) -> None:
        """Replace the table contents with a core dependency report."""
        self._report = report
        self._table.setRowCount(0)
        if report is None:
            self._summary.setText(I18N.tr("asset_deps.not_scanned", "尚未扫描资产依赖"))
            return

        for reference in report.references:
            self._add_reference_row(reference)
        self._summary.setText(
            f"共 {len(report.references)} 项 · "
            f"已解析 {len(report.resolved)} · "
            f"缺失 {len(report.missing)} · "
            f"可重链接 {len(report.relink_candidates)}"
        )

    def set_operation_message(self, message: str, *, error: bool = False) -> None:
        """Show the last page-level operation result below the table."""
        color = "#e88" if error else "#9c9"
        self._summary.setStyleSheet(f"color: {color}; font-size: 10px;")
        self._summary.setText(str(message))

    def set_search_roots(self, roots: tuple[str, ...] | list[str]) -> None:
        """Display configured search roots without exposing full paths in rows."""
        normalized = tuple(str(root) for root in roots if str(root))
        if not normalized:
            self._search_roots_label.setText(
                I18N.tr("asset_deps.search_roots_unset", "搜索目录：未设置"))
            self._search_roots_label.setToolTip("")
            return
        import os
        self._search_roots_label.setText(
            "搜索目录：" + "、".join(os.path.basename(root) or root
                                     for root in normalized)
        )
        self._search_roots_label.setToolTip("\n".join(normalized))

    def _add_reference_row(self, reference: AssetReference) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        values = (
            reference.node_id,
            reference.parameter,
            reference.role,
            _STATUS_LABELS.get(reference.status, reference.status),
            reference.resolved_path or reference.stored_path or "—",
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            if column == 3:
                item.setForeground(QBrush(QColor(
                    _STATUS_COLORS.get(reference.status, "#ccc"))))
            if column == 4:
                item.setToolTip(
                    f"保存路径: {reference.stored_path or '—'}\n"
                    f"解析路径: {reference.resolved_path or '—'}"
                )
            self._table.setItem(row, column, item)

        action = QPushButton(I18N.tr("asset_deps.choose_replacement", "选择替换"))
        action.setEnabled(bool(reference.is_missing or
                               reference.is_relink_candidate))
        action.setToolTip(
            I18N.tr(
                "asset_deps.choose_replacement_tooltip",
                "从文件系统选择路径；页面会再次执行候选校验和撤销记录")
        )
        action.clicked.connect(
            lambda _checked=False, node_id=reference.node_id,
            parameter=reference.parameter: self.relink_requested.emit(
                node_id, parameter)
        )
        self._table.setCellWidget(row, 5, action)
