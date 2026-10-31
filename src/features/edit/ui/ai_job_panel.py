# DORMANT(2026-10-20 第五轮审计 E22): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""AI Job 面板子组件（Phase 9 UI-5）。

显示模型/分析/预览/导出任务的进度，支持取消/重试（TAEP §11.3 AiJobPanel）。

约束：只经 job service 控制任务（发 ``cancel_requested`` / ``retry_requested`` signal）；
状态回填用 ``_updating`` 锁。
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.ai_editing.assistant_events import JobCard
from src.shared.i18n.i18n import tr


class _JobRowWidget(QFrame):
    """单条 job 进度行。"""

    cancel_clicked = pyqtSignal(str)
    retry_clicked = pyqtSignal(str)

    def __init__(self, card: JobCard, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._job_id = card.job_id
        self.setFrameShape(QFrame.Shape.Box)
        self.setStyleSheet("QFrame { border: 1px solid #eee; border-radius: 3px; margin: 1px; padding: 3px; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        header = QHBoxLayout()
        kind_label = QLabel(f"[{card.kind}]")
        kind_label.setStyleSheet("font-weight: bold; color: #555;")
        header.addWidget(kind_label)
        label = QLabel(card.label or card.job_id)
        header.addWidget(label)
        header.addStretch()
        self._status_label = QLabel(card.status)
        header.addWidget(self._status_label)
        layout.addLayout(header)

        self._progress_label = QLabel(tr("ai_job.progress", "进度: {p}%", p=int(card.progress * 100)))
        layout.addWidget(self._progress_label)

        btn_row = QHBoxLayout()
        self._cancel_btn = QPushButton(tr("ai_job.cancel", "取消"))
        self._cancel_btn.clicked.connect(lambda: self.cancel_clicked.emit(self._job_id))
        self._retry_btn = QPushButton(tr("ai_job.retry", "重试"))
        self._retry_btn.clicked.connect(lambda: self.retry_clicked.emit(self._job_id))
        # 终态才显示重试；运行态才显示取消
        is_terminal = card.status in ("succeeded", "failed", "cancelled")
        is_running = card.status in ("queued", "running", "cancelling")
        self._cancel_btn.setVisible(is_running)
        self._retry_btn.setVisible(is_terminal and card.status == "failed")
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._retry_btn)
        btn_row.addStretch()
        btn_w = QWidget()
        btn_w.setLayout(btn_row)
        layout.addWidget(btn_w)


class AiJobPanel(QWidget):
    """job 进度面板。"""

    cancel_requested = pyqtSignal(str)
    retry_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updating = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        title = QLabel(tr("ai_job.title", "任务"))
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.addStretch()
        self._scroll.setWidget(self._container)
        layout.addWidget(self._scroll)

        self._empty_label = QLabel(tr("ai_job.empty", "空闲"))
        self._empty_label.setStyleSheet("color: gray; padding: 4px;")
        layout.addWidget(self._empty_label)

    def set_jobs(self, jobs: tuple[JobCard, ...]) -> None:
        self._updating = True
        try:
            while self._container_layout.count() > 1:
                item = self._container_layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
            self._empty_label.setVisible(len(jobs) == 0)
            for card in jobs:
                row = _JobRowWidget(card)
                row.cancel_clicked.connect(self.cancel_requested.emit)
                row.retry_clicked.connect(self.retry_requested.emit)
                self._container_layout.insertWidget(self._container_layout.count() - 1, row)
        finally:
            self._updating = False

    @property
    def is_updating(self) -> bool:
        return self._updating
