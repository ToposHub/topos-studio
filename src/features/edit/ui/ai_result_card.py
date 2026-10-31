"""AI 运行结果卡片（plan §10.3 P1.5）。

统一渲染 :class:`ResultCardState`（evidence / 脚本 / 字幕草稿 / Variant /
生成素材 / QA / 错误）+ 可执行按钮（查看依据 / 重试 / 调整参数 / 预览 diff /
审批 / 应用 / 丢弃 / 查看诊断 / 定位 / 打开编辑器）。

核心约束（plan §10.3 P1.5）：

- **不把未审批 proposal 显示成已应用**：按钮 enabled 严格跟随
  ``ResultAction.enabled``；审批前 apply 禁用并 tooltip 说明。
- **不可信结果标注**：``trust != trusted`` 时显示"待核/不可信"徽标；untrusted
  禁用直接 apply（state 已决定，UI 只渲染）。
- 只发 signal（``action_triggered(result_id, action_kind)``），不执行 / 不写
  timeline / 不调 provider。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.features.ai_harness.result_card import ResultCardState
from src.shared.i18n.i18n import tr


_TRUST_COLORS = {
    "trusted": "#27ae60",
    "unverified": "#f39c12",
    "untrusted": "#c0392b",
}

_KIND_COLORS = {
    "variant": "#2980b9",
    "subtitle_draft": "#8e44ad",
    "script": "#8e44ad",
    "evidence": "#27ae60",
    "generated_media": "#16a085",
    "qa_report": "#7f8c8d",
    "error": "#c0392b",
    "info": "#7f8c8d",
}

_KIND_LABELS = {
    "variant": "方案",
    "subtitle_draft": "字幕",
    "script": "脚本",
    "evidence": "证据",
    "generated_media": "生成素材",
    "qa_report": "QA",
    "error": "错误",
    "info": "信息",
}


class AiResultCard(QWidget):
    """单个运行结果卡（plan §10.3 P1.5）。

    Signals：

    - ``action_triggered(str, str)``：(result_id, action_kind)。
    """

    action_triggered = pyqtSignal(str, str)

    def __init__(self, state: ResultCardState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._build_ui()

    def _build_ui(self) -> None:
        frame = QFrame(self)
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        accent = _KIND_COLORS.get(self._state.kind, "#7f8c8d")
        frame.setStyleSheet(
            "QFrame{background: palette(window);"
            f" border-left: 3px solid {accent};"
            " border-top: 1px solid palette(mid);"
            " border-right: 1px solid palette(mid);"
            " border-bottom: 1px solid palette(mid);"
            " border-radius: 3px;}"
        )
        v = QVBoxLayout(frame)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(4)

        # 标题行：kind 徽标 + 标题 + 状态 + 信任
        title_row = QHBoxLayout()
        kind_lbl = QLabel(_KIND_LABELS.get(self._state.kind, self._state.kind))
        kind_lbl.setStyleSheet(
            f"background: {accent}; color: white; padding: 1px 6px;"
            " border-radius: 3px; font-size: small;"
        )
        title_row.addWidget(kind_lbl)
        title = QLabel(f"<b>{self._state.title}</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(title, stretch=1)
        # 状态
        status_color = "#27ae60" if self._state.applied else (
            "#2980b9" if self._state.approved else "#7f8c8d"
        )
        if self._state.kind == "error":
            status_color = "#c0392b"
        status = QLabel(
            f"<span style='color:{status_color}'>{self._state.status_label}</span>"
        )
        status.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(status)
        # 信任徽标（非 trusted 才显示）
        if self._state.trust != "trusted":
            tcolor = _TRUST_COLORS.get(self._state.trust, "gray")
            trust_lbl = QLabel(
                f"<span style='color:{tcolor}'>⚠ {self._state.trust_label}</span>"
            )
            trust_lbl.setTextFormat(Qt.TextFormat.RichText)
            title_row.addWidget(trust_lbl)
        v.addLayout(title_row)

        # 摘要
        if self._state.summary:
            summary = QLabel(self._state.summary)
            summary.setWordWrap(True)
            summary.setStyleSheet("color: palette(text);")
            v.addWidget(summary)

        # 详情
        if self._state.detail and self._state.detail != self._state.summary:
            detail = QLabel(self._state.detail)
            detail.setWordWrap(True)
            detail.setStyleSheet("color: gray;")
            v.addWidget(detail)

        # 错误码
        if self._state.error_code:
            err = QLabel(
                f"<span style='color:#c0392b'>⚠ {self._state.error_code}</span>"
            )
            err.setTextFormat(Qt.TextFormat.RichText)
            v.addWidget(err)

        # 证据计数（提示可"查看依据"）
        if self._state.evidence_refs:
            n = len(self._state.evidence_refs)
            ev_lbl = QLabel(tr("ai_result.evidence_count", f"引用 {n} 条证据"))
            ev_lbl.setStyleSheet("color: gray; font-size: small;")
            v.addWidget(ev_lbl)

        # 操作按钮
        if self._state.available_actions:
            ops = QHBoxLayout()
            ops.setSpacing(4)
            for action in self._state.available_actions:
                btn = QPushButton(action.label)
                btn.setStyleSheet("padding: 3px 10px;")
                btn.setEnabled(action.enabled)
                if not action.enabled and action.disabled_reason:
                    btn.setToolTip(action.disabled_reason)
                elif action.requires_permission:
                    btn.setToolTip(
                        tr("ai_result.needs_perm", "需要权限：") + action.requires_permission
                    )
                # 闭包捕获 action.kind
                btn.clicked.connect(
                    lambda _, ak=action.kind: self.action_triggered.emit(
                        self._state.result_id, ak
                    )
                )
                ops.addWidget(btn)
            ops.addStretch(1)
            v.addLayout(ops)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)

    @property
    def result_id(self) -> str:
        return self._state.result_id

    @property
    def state(self) -> ResultCardState:
        return self._state


__all__ = ["AiResultCard"]
