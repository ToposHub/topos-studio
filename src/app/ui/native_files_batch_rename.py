"""F13 批量改名：先预览冲突，再执行并展示逐项结果。"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from src.shared.ui.style_sheet import C


class BatchRenameDialog(QDialog):
    preview_requested = pyqtSignal(str)
    execute_requested = pyqtSignal(str)

    def __init__(self, paths: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.paths = list(paths)
        self.setWindowTitle("批量改名")
        self.resize(720, 480)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)
        hint = QLabel("模板可用 {name} {ext} {seq:3} {date:taken:%Y%m%d}；先预览再执行。")
        hint.setObjectName("filesHint")
        root.addWidget(hint)
        row = QHBoxLayout()
        self.template_edit = QLineEdit("{name}_{seq:3}", self)
        self.template_edit.setAccessibleName("改名模板")
        row.addWidget(self.template_edit, 1)
        preview_button = QPushButton("预览", self)
        preview_button.clicked.connect(self._preview)
        row.addWidget(preview_button)
        root.addLayout(row)
        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["原名", "新名", "冲突"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 220)
        self.table.setColumnWidth(1, 300)
        root.addWidget(self.table, 1)
        self.summary = QLabel("尚未预览", self)
        self.summary.setWordWrap(True)
        self.summary.setObjectName("filesHint")
        root.addWidget(self.summary)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.execute_button = QPushButton("执行改名", self)
        self.execute_button.setEnabled(False)
        self.execute_button.clicked.connect(self._execute)
        actions.addWidget(self.execute_button)
        close_button = QPushButton("关闭", self)
        close_button.clicked.connect(self.reject)
        actions.addWidget(close_button)
        root.addLayout(actions)
        self.setStyleSheet(f"QDialog {{ background: {C.BG_PANEL}; color: {C.TEXT_PRIMARY}; }}")

    def _preview(self) -> None:
        template = self.template_edit.text().strip()
        if not template:
            self.summary.setText("模板不能为空")
            return
        self.execute_button.setEnabled(False)
        self.summary.setText("正在生成预览…")
        self.preview_requested.emit(template)

    def _execute(self) -> None:
        template = self.template_edit.text().strip()
        if template:
            self.execute_button.setEnabled(False)
            self.summary.setText("正在执行…")
            self.execute_requested.emit(template)

    def show_preview(self, payload: dict[str, Any]) -> None:
        plan = payload.get("plan") or []
        errors = payload.get("errors") or []
        self.table.setRowCount(0)
        conflicts = 0
        for item in plan:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(str(item.get("src_name") or "")))
            self.table.setItem(row, 1, QTableWidgetItem(str(item.get("dst_name") or "")))
            conflict = bool(item.get("conflict"))
            conflicts += 1 if conflict else 0
            cell = QTableWidgetItem("是" if conflict else "—")
            if conflict:
                cell.setForeground(Qt.GlobalColor.yellow)
            self.table.setItem(row, 2, cell)
        # 冲突由服务在目标名后加序号；预览只负责让用户看清，不代替用户决定
        self.execute_button.setEnabled(bool(plan) and not errors)
        text = f"预览 {len(plan)} 项"
        if conflicts:
            text += f"，其中 {conflicts} 项冲突（执行时自动加后缀）"
        if errors:
            text += "；" + "；".join(str(e) for e in errors[:3])
            self.execute_button.setEnabled(False)
        self.summary.setText(text)

    def show_preview_error(self, message: str) -> None:
        self.table.setRowCount(0)
        self.execute_button.setEnabled(False)
        self.summary.setText(str(message))

    def show_result(self, payload: dict[str, Any]) -> None:
        errors = payload.get("errors") or []
        processed = payload.get("processed")
        text = str(payload.get("message") or "已执行")
        if processed is not None:
            text = f"已处理 {processed} 项"
        if errors:
            text += "；失败：" + "；".join(str(e) for e in errors[:5])
        self.summary.setText(text)
        self.execute_button.setEnabled(False)
