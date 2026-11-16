"""F13 批量改名：先预览冲突，再执行并展示逐项结果。

[FR-13] 会话安全：每个窗口持有唯一 session_id，请求与响应双向携带；
广播结果不属于本会话一律忽略（多个非模态窗口互不串台）。执行绑定
**已确认**的模板快照——预览后修改模板会使确认失效，必须重新预览。
"""
from __future__ import annotations

import uuid
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from src.shared.ui.style_sheet import C


class BatchRenameDialog(QDialog):
    preview_requested = pyqtSignal(str, str)   # template, preview request token
    execute_requested = pyqtSignal(str, str)   # confirmed template, session_id

    def __init__(self, paths: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.paths = list(paths)
        self.session_id = uuid.uuid4().hex
        self._preview_request_id: str | None = None
        self._pending_template: str | None = None
        self._confirmed_template: str | None = None  # 预览确认过的模板快照
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
        self.template_edit.textChanged.connect(self._invalidate_confirmed_template)
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

    def _invalidate_confirmed_template(self, *_: Any) -> None:
        """[FR-13] 模板一旦改动，既有预览即失效：执行入口关闭。"""
        pending = self._preview_request_id is not None
        self._preview_request_id = None
        self._pending_template = None
        if self._confirmed_template is None and not pending:
            return
        self._confirmed_template = None
        self.execute_button.setEnabled(False)
        self.summary.setText("模板已修改，请重新预览后再执行。")

    def _preview(self) -> None:
        self._preview_request_id = None
        self._pending_template = None
        self._confirmed_template = None
        self.execute_button.setEnabled(False)
        template = self.template_edit.text().strip()
        if not template:
            self.summary.setText("模板不能为空")
            return
        self._pending_template = template
        self._preview_request_id = uuid.uuid4().hex
        self.summary.setText("正在生成预览…")
        self.preview_requested.emit(template, self._preview_request_id)

    def _execute(self) -> None:
        template = self._confirmed_template
        if template is None:
            # 防线：预览失效后按钮应已禁用，这里再拦一次
            self.summary.setText("模板已修改，请重新预览后再执行。")
            return
        self.execute_button.setEnabled(False)
        self.summary.setText("正在执行…")
        self.execute_requested.emit(template, self.session_id)

    def show_preview(self, payload: dict[str, Any]) -> None:
        if str(payload.get("session_id") or "") != self._preview_request_id:
            return  # 其他窗口或本窗口旧请求的预览结果：忽略
        template = self._pending_template
        self._preview_request_id = None
        self._pending_template = None
        if template is None or template != self.template_edit.text().strip():
            return
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
        if plan and not errors:
            self._confirmed_template = template
            self.execute_button.setEnabled(True)
        else:
            self._confirmed_template = None
            self.execute_button.setEnabled(False)
        text = f"预览 {len(plan)} 项"
        if conflicts:
            text += f"，其中 {conflicts} 项冲突（执行时自动加后缀）"
        if errors:
            text += "；" + "；".join(str(e) for e in errors[:3])
        self.summary.setText(text)

    def show_preview_error(self, message: str, session_id: str = "") -> None:
        if session_id != self._preview_request_id:
            return  # 其他窗口或本窗口旧请求的失败提示：忽略
        self._preview_request_id = None
        self._pending_template = None
        self.table.setRowCount(0)
        self.execute_button.setEnabled(False)
        self._confirmed_template = None
        self.summary.setText(str(message))

    def show_result(self, payload: dict[str, Any]) -> None:
        if str(payload.get("session_id") or "") != self.session_id:
            return  # [FR-13] 其他窗口的执行结果：忽略
        errors = payload.get("errors") or []
        processed = payload.get("processed")
        text = str(payload.get("message") or "已执行")
        if processed is not None:
            text = f"已处理 {processed} 项"
        if errors:
            text += "；失败：" + "；".join(str(e) for e in errors[:5])
        self.summary.setText(text)
        self.execute_button.setEnabled(False)
        self._confirmed_template = None
