# DORMANT(2026-10-20 第五轮审计 E22): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""AI Decision Inspector 子组件（Phase 9 UI-3）。

展示每条 decision 的证据、备选项；用户可锁定/拒绝/替换（TAEP §11.3
DecisionInspector / §11.6 步骤 9）。

约束：写 ``EditDecision`` 或 ``UserOverride``，不直接 edit clip（TAEP §11.3）。
只发 signal；状态回填用 ``_updating`` 锁。音乐/字幕 decision 有试听/拒绝入口。
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

from src.features.edit.ai_editing.assistant_events import DecisionCard
from src.shared.i18n.i18n import tr


class _DecisionRowWidget(QFrame):
    """单条 decision 行。"""

    locked = pyqtSignal(str)
    rejected = pyqtSignal(str)
    replaced = pyqtSignal(str)
    audition_clicked = pyqtSignal(str)  # 试听（音乐）

    def __init__(self, card: DecisionCard, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._decision_id = card.decision_id
        self.setFrameShape(QFrame.Shape.Box)
        # 状态色：accepted=green, rejected=red, locked=blue
        state_color = {
            "accepted": "#27ae60", "locked": "#2980b9",
            "rejected": "#c0392b", "proposed": "#ccc",
        }.get(card.state, "#ccc")
        self.setStyleSheet(
            f"QFrame {{ border: 1px solid {state_color}; border-radius: 3px; margin: 1px; padding: 3px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        header = QHBoxLayout()
        beat = QLabel(card.beat or card.decision_id)
        beat.setStyleSheet("font-weight: bold;")
        header.addWidget(beat)
        header.addStretch()
        state_icon = {"accepted": "✓", "locked": "🔒", "rejected": "✗", "proposed": "?"}.get(card.state, "?")
        header.addWidget(QLabel(state_icon))
        layout.addLayout(header)

        layout.addWidget(QLabel(card.description))
        if card.evidence_summary:
            ev = QLabel(tr("ai_decision.evidence", "证据: {e}", e=card.evidence_summary))
            ev.setStyleSheet("color: gray;")
            ev.setWordWrap(True)
            layout.addWidget(ev)

        btn_row = QHBoxLayout()
        # AUD-19412: 按钮按 decision 当前状态门控 —— 已锁定不能再锁定，
        # 已拒绝不能再拒绝；替换在任何状态下都保留（改选备选项的合法后续）。
        lock_btn = QPushButton(tr("ai_decision.lock", "锁定"))
        lock_btn.setEnabled(card.state != "locked")
        lock_btn.clicked.connect(lambda: self.locked.emit(self._decision_id))
        reject_btn = QPushButton(tr("ai_decision.reject", "拒绝"))
        reject_btn.setEnabled(card.state != "rejected")
        reject_btn.clicked.connect(lambda: self.rejected.emit(self._decision_id))
        replace_btn = QPushButton(tr("ai_decision.replace", "替换"))
        replace_btn.clicked.connect(lambda: self.replaced.emit(self._decision_id))
        btn_row.addWidget(lock_btn)
        btn_row.addWidget(reject_btn)
        btn_row.addWidget(replace_btn)

        # 音乐：试听
        if card.is_music:
            audition_btn = QPushButton(tr("ai_decision.audition", "试听"))
            audition_btn.clicked.connect(lambda: self.audition_clicked.emit(self._decision_id))
            btn_row.addWidget(audition_btn)

        btn_row.addStretch()
        btn_w = QWidget()
        btn_w.setLayout(btn_row)
        layout.addWidget(btn_w)


class AiDecisionInspector(QWidget):
    """decision 检查器：证据/备选项/锁定/拒绝/替换。"""

    decision_locked = pyqtSignal(str)
    decision_rejected = pyqtSignal(str)
    decision_replaced = pyqtSignal(str)
    music_audition_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updating = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        title = QLabel(tr("ai_decision.title", "决策检查器"))
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

    def set_decisions(self, decisions: tuple[DecisionCard, ...]) -> None:
        self._updating = True
        try:
            while self._container_layout.count() > 1:
                item = self._container_layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()
            for card in decisions:
                row = _DecisionRowWidget(card)
                row.locked.connect(self.decision_locked.emit)
                row.rejected.connect(self.decision_rejected.emit)
                row.replaced.connect(self.decision_replaced.emit)
                row.audition_clicked.connect(self.music_audition_requested.emit)
                self._container_layout.insertWidget(self._container_layout.count() - 1, row)
        finally:
            self._updating = False

    @property
    def is_updating(self) -> bool:
        return self._updating
