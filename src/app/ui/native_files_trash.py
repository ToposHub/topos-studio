"""D2 回收站入口：清单/恢复（含冲突策略）/彻底删除。

列表是 manifest 的如实投影；恢复与彻底删除经 transport 的文件任务
（restore/purge）执行——逐项结果由服务决定，对话框不宣布假成功。
"""
from __future__ import annotations

import time
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from src.shared.ui.style_sheet import C


def _when(timestamp: float | None) -> str:
    if not timestamp:
        return "—"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


class TrashDialog(QDialog):
    """非模态回收站：每次打开即拉清单，操作后由调用方触发刷新。"""

    refresh_requested = pyqtSignal()
    restore_requested = pyqtSignal(int, str)   # record_id, policy(error|rename)
    purge_requested = pyqtSignal(int)          # record_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("回收站")
        self.resize(760, 420)
        self._records: list[dict[str, Any]] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)
        self.hint = QLabel("恢复失败（目标已存在）时可用「改名恢复」。", self)
        self.hint.setObjectName("filesHint")
        root.addWidget(self.hint)
        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["名称", "原位置", "类型", "删除时间", "到期清理"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        root.addWidget(self.table, 1)
        self.summary = QLabel("正在读取回收站…", self)
        self.summary.setObjectName("filesHint")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        actions = QHBoxLayout()
        self.restore_button = QPushButton("恢复", self)
        self.rename_restore_button = QPushButton("改名恢复", self)
        self.purge_button = QPushButton("彻底删除…", self)
        refresh_button = QPushButton("刷新", self)
        close_button = QPushButton("关闭", self)
        self.restore_button.clicked.connect(self._restore_selected)
        self.rename_restore_button.clicked.connect(self._rename_restore_selected)
        self.purge_button.clicked.connect(self._purge_selected)
        refresh_button.clicked.connect(self.refresh_requested.emit)
        close_button.clicked.connect(self.reject)
        for button in (self.restore_button, self.rename_restore_button,
                       self.purge_button):
            button.setEnabled(False)
        self.table.itemSelectionChanged.connect(self._update_actions)
        actions.addWidget(self.restore_button)
        actions.addWidget(self.rename_restore_button)
        actions.addWidget(self.purge_button)
        actions.addStretch(1)
        actions.addWidget(refresh_button)
        actions.addWidget(close_button)
        root.addLayout(actions)
        self.setStyleSheet(
            f"QDialog {{ background: {C.BG_PANEL}; color: {C.TEXT_PRIMARY}; }}")

    # ------------------------------------------------------------ 数据/操作
    def _selected_record(self) -> dict[str, Any] | None:
        row = self.table.currentRow()
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    def _update_actions(self) -> None:
        record = self._selected_record()
        for button in (self.restore_button, self.rename_restore_button,
                       self.purge_button):
            button.setEnabled(record is not None)

    def _restore_selected(self) -> None:
        record = self._selected_record()
        if record is not None:
            self.restore_requested.emit(int(record["id"]), "error")

    def _rename_restore_selected(self) -> None:
        record = self._selected_record()
        if record is not None:
            self.restore_requested.emit(int(record["id"]), "rename")

    def _purge_selected(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        from PyQt6.QtWidgets import QMessageBox
        confirm = QMessageBox.question(
            self, "彻底删除",
            f"「{record['item_name']}」将被永久删除，无法恢复。继续？")
        if confirm == QMessageBox.StandardButton.Yes:
            self.purge_requested.emit(int(record["id"]))

    # ------------------------------------------------------------ 展示槽
    def show_items(self, records: list[dict[str, Any]]) -> None:
        self._records = [dict(record) for record in records]
        self.table.setRowCount(0)
        for record in self._records:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(record.get("item_name") or "")))
            self.table.setItem(row, 1, QTableWidgetItem(str(record.get("original_path") or "")))
            self.table.setItem(row, 2, QTableWidgetItem(
                "文件夹" if record.get("is_dir") else "文件"))
            self.table.setItem(row, 3, QTableWidgetItem(_when(record.get("deleted_at"))))
            self.table.setItem(row, 4, QTableWidgetItem(_when(record.get("expire_at"))))
        if not self._records:
            self.summary.setText("回收站是空的。")
        else:
            self.summary.setText(f"共 {len(self._records)} 项。")
        self._update_actions()

    def show_error(self, message: str) -> None:
        self.summary.setText(str(message or "回收站读取失败"))

    def show_task_result(self, job: dict[str, Any]) -> None:
        """restore/purge 的逐项结果：冲突显式指出可用改名恢复。"""
        successes = job.get("successes") or []
        failures = job.get("failures") or []
        label = "恢复" if job.get("operation") == "restore" else "彻底删除"
        parts = [f"{label}完成：{len(successes)} 项"]
        for failure in failures[:3]:
            reason = str(failure.get("error") or "失败")
            if failure.get("code") == "DEST_EXISTS":
                reason += "（可改用「改名恢复」）"
            parts.append(reason)
        self.summary.setText("；".join(parts))
