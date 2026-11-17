"""D 表第 5 项：原生文件任务列表——进度/逐项错误/取消/重试失败项。

`FilesJobsRegistry` 由 FilesPanel 持有并挂在 transport 信号上：切目录、
关任务窗口都不丢归属（任务在 facade 服务执行，注册表只记账）。
`JobsDialog` 只是注册表的投影，可随时关/重开。
"""
from __future__ import annotations

from typing import Any

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from src.shared.ui.style_sheet import C

JOB_LABELS = {"mkdir": "新建文件夹", "rename": "重命名", "copy": "复制",
              "move": "移动", "trash": "移入回收站", "restore": "恢复",
              "purge": "彻底删除"}


def _job_label(operation: str) -> str:
    return JOB_LABELS.get(str(operation), str(operation or "文件操作"))


class FilesJobsRegistry(QObject):
    """任务台账：只记事实（进度快照/逐项结果），不宣布假成功。"""

    jobs_changed = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._jobs: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ 记账槽
    def on_task_started(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("job_id") or "")
        if not job_id:
            return
        entry = self._jobs.setdefault(job_id, {
            "job_id": job_id, "operation": str(job.get("operation") or ""),
            "targets": list(job.get("targets") or []),
            "policy": str(job.get("policy") or "error"),
            "options": dict(job.get("options") or {}),
            "total": 0, "completed": 0, "state": "running",
            "successes": [], "failures": [],
        })
        entry["operation"] = str(job.get("operation") or entry["operation"])
        if job.get("targets"):
            entry["targets"] = list(job["targets"])
        if job.get("policy"):
            entry["policy"] = str(job["policy"])
        if job.get("options"):
            entry["options"] = dict(job["options"])
        entry["total"] = int(job.get("total") or entry["total"])
        entry["completed"] = int(job.get("completed") or 0)
        self.jobs_changed.emit()

    def on_task_finished(self, job: dict[str, Any]) -> None:
        job_id = str(job.get("job_id") or "")
        entry = self._jobs.get(job_id)
        if entry is None:
            self.on_task_started(job)
            entry = self._jobs.get(job_id)
            if entry is None:
                return
        entry["state"] = "cancelled" if job.get("cancelled") else \
            ("failed" if job.get("failures") else "done")
        entry["completed"] = int(job.get("completed") or 0)
        entry["total"] = int(job.get("total") or entry["total"])
        entry["successes"] = list(job.get("successes") or [])
        entry["failures"] = list(job.get("failures") or [])
        self.jobs_changed.emit()

    def on_task_failed(self, message: str) -> None:
        # 提交失败没有任务号：记一条匿名失败事实，可见可解释
        self._jobs[f"failed-{len(self._jobs)}"] = {
            "job_id": "", "operation": "", "targets": [], "policy": "error",
            "options": {}, "total": 0, "completed": 0, "state": "failed",
            "successes": [], "failures": [{"path": "", "error": str(message),
                                           "code": ""}],
        }
        self.jobs_changed.emit()

    # ------------------------------------------------------------ B3 中断恢复
    def on_recovery_loaded(self, records: list[dict[str, Any]]) -> None:
        """上次运行遗留的未终态任务：入账为待恢复。"""
        for record in records or []:
            job_id = str(record.get("job_id") or "")
            if not job_id or job_id in self._jobs:
                continue
            self._jobs[job_id] = {
                "job_id": job_id,
                "operation": str(record.get("operation") or ""),
                "targets": [str(record.get("source") or "")],
                "policy": "error", "options": {},
                "total": 1, "completed": 0, "state": "pending_recovery",
                "successes": [],
                "failures": [{"path": str(record.get("source") or ""),
                              "error": "上次运行中断；按磁盘证据恢复",
                              "code": "INTERRUPTED"}],
            }
        self.jobs_changed.emit()

    def on_recovery_applied(self, verdict: dict[str, Any]) -> None:
        """恢复判定落账：completed→done；failed→failed；证据不足保持待恢复。"""
        job_id = str(verdict.get("job_id") or "")
        job = self._jobs.get(job_id)
        if job is None:
            return
        state = str(verdict.get("state") or "")
        classification = str(verdict.get("classification") or "")
        if state == "completed":
            job["state"] = "done"
            job["successes"] = [{"path": job["targets"][0] if job["targets"] else ""}]
            job["failures"] = []
            job["completed"] = 1
        elif state == "failed":
            job["state"] = "failed"
            job["failures"] = [{"path": job["targets"][0] if job["targets"] else "",
                                "error": f"中断可重试（{classification}）",
                                "code": "INTERRUPTED_RETRIABLE"}]
        else:  # 证据不足：保持待恢复并展示分类原因
            job["failures"] = [{"path": job["targets"][0] if job["targets"] else "",
                                "error": f"证据不足（{classification}），保持待恢复",
                                "code": "PENDING_RECOVERY"}]
        self.jobs_changed.emit()

    # ------------------------------------------------------------ 查询
    def jobs(self) -> list[dict[str, Any]]:
        return [dict(job) for job in self._jobs.values()]

    def job(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get(str(job_id))
        return dict(job) if job is not None else None

    def running_count(self) -> int:
        return sum(1 for job in self._jobs.values() if job["state"] == "running")

    def clear_finished(self) -> None:
        self._jobs = {job_id: job for job_id, job in self._jobs.items()
                      if job["state"] == "running"}
        self.jobs_changed.emit()

    # ------------------------------------------------------------ 意图
    def retry_request(self, job_id: str) -> tuple[str, list[str], str, dict[str, Any]] | None:
        """重试失败项的完整调用参数；没有失败项返回 None。"""
        job = self._jobs.get(str(job_id))
        if job is None or not job["failures"]:
            return None
        return (job["operation"],
                [str(item.get("path") or "") for item in job["failures"
                                                              ] if item.get("path")],
                job["policy"], dict(job["options"]))


class JobsDialog(QDialog):
    """任务台账投影：进度、逐项错误、取消、重试失败项。"""

    cancel_requested = pyqtSignal(str)
    retry_requested = pyqtSignal(str, list, str, dict)  # operation, targets, policy, options
    recover_requested = pyqtSignal(str)                 # B3 待恢复任务的 job_id

    def __init__(self, registry: FilesJobsRegistry,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._registry = registry
        self.setWindowTitle("文件任务")
        self.resize(720, 380)
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["任务", "进度", "状态", "操作"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        root.addWidget(self.table, 1)
        self.summary = QLabel("没有文件任务。", self)
        self.summary.setObjectName("filesHint")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)
        actions = QHBoxLayout()
        self.cancel_button = QPushButton("取消任务", self)
        self.retry_button = QPushButton("重试失败项", self)
        self.recover_button = QPushButton("恢复…", self)
        clear_button = QPushButton("清空已完成", self)
        close_button = QPushButton("关闭", self)
        self.cancel_button.clicked.connect(self._cancel_selected)
        self.retry_button.clicked.connect(self._retry_selected)
        self.recover_button.clicked.connect(self._recover_selected)
        clear_button.clicked.connect(registry.clear_finished)
        close_button.clicked.connect(self.reject)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.retry_button)
        actions.addWidget(self.recover_button)
        actions.addStretch(1)
        actions.addWidget(clear_button)
        actions.addWidget(close_button)
        root.addLayout(actions)
        self.setStyleSheet(
            f"QDialog {{ background: {C.BG_PANEL}; color: {C.TEXT_PRIMARY}; }}")
        registry.jobs_changed.connect(self._refresh)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self._refresh()

    # ------------------------------------------------------------ 渲染/意图
    def _jobs_view(self) -> list[dict[str, Any]]:
        # 新任务在前；同键稳定
        return sorted(self._registry.jobs(),
                      key=lambda job: (job["state"] == "running", job["job_id"]),
                      reverse=True)

    def _refresh(self) -> None:
        jobs = self._jobs_view()
        self.table.setRowCount(0)
        state_labels = {"running": "进行中", "done": "完成", "failed": "有失败",
                        "cancelled": "已取消", "pending_recovery": "待恢复"}
        for job in jobs:
            row = self.table.rowCount()
            self.table.insertRow(row)
            first = (job["targets"] or [""])[0]
            name = first.rsplit("/", 1)[-1] if first else "（提交失败）"
            self.table.setItem(row, 0, QTableWidgetItem(
                f"{_job_label(job['operation'])} · {name}"
                + (f" 等 {len(job['targets'])} 项" if len(job["targets"] or []) > 1 else "")))
            total = int(job.get("total") or 0)
            done = int(job.get("completed") or 0)
            self.table.setItem(row, 1, QTableWidgetItem(
                f"{done}/{total}" if total else "—"))
            self.table.setItem(row, 2, QTableWidgetItem(
                state_labels.get(job["state"], str(job["state"]))))
            self.table.setItem(row, 3, QTableWidgetItem(
                "" if job["state"] == "running" else
                f"成功 {len(job.get('successes') or [])} · "
                f"失败 {len(job.get('failures') or [])}"))
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, job["job_id"])
        self._update_actions()

    def _selected_job_id(self) -> str:
        row = self.table.currentRow()
        if row < 0:  # selectRow 只改选择不改 current：回退到选中项
            selected = self.table.selectedItems()
            row = selected[0].row() if selected else -1
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""

    def _update_actions(self) -> None:
        job = self._registry.job(self._selected_job_id()) if self._selected_job_id() else None
        self.cancel_button.setEnabled(bool(job and job["state"] == "running"))
        self.retry_button.setEnabled(bool(job and job["failures"]))
        self.recover_button.setEnabled(
            bool(job and job["state"] == "pending_recovery"))
        if job and job["failures"]:
            reasons = "；".join(str(item.get("error") or "失败")
                               for item in job["failures"][:3])
            self.summary.setText(f"失败 {len(job['failures'])} 项：{reasons}")
        elif job:
            self.summary.setText(
                f"共 {len(self._jobs_view())} 个任务，运行中 "
                f"{self._registry.running_count()}。")
        else:
            self.summary.setText("没有文件任务。")

    def _cancel_selected(self) -> None:
        job_id = self._selected_job_id()
        if job_id:
            self.cancel_requested.emit(job_id)

    def _recover_selected(self) -> None:
        job_id = self._selected_job_id()
        job = self._registry.job(job_id) if job_id else None
        if job is not None and job["state"] == "pending_recovery":
            self.recover_requested.emit(job_id)

    def _retry_selected(self) -> None:
        job_id = self._selected_job_id()
        request = self._registry.retry_request(job_id) if job_id else None
        if request is None:
            return
        operation, targets, policy, options = request
        if targets:
            self.retry_requested.emit(operation, targets, policy, options)
