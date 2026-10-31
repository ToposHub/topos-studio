# DORMANT(2026-10-20 第五轮审计 E22): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""AI Timeline Diff 面板 + 审批 dialog（Phase 9 UI-4）。

按视频/音频/字幕轨显示 operation；高风险项置顶（TAEP §11.6 步骤 10）。
置顶的实际构成（AUD-19620 澄清）：``removed`` 条目（delete；替换在 diff
中表现为 removed+added 对，其 removed 半边置顶）+ 预检 ``warnings``
（音乐版权、降级 effect 等，AUD-19619 起以高风险样式渲染在列表最前）。
“应用方案”前强制 validate，base revision 改变时切到 stale 禁止提交。
确认 dialog 显示 undo group 名、影响 clip 数、删除数、范围、预检 warning。

约束（TAEP §11.3 TimelineDiffPanel）：

- 只读 proposed diff；“应用”交由 controller 的审批流程。
- 只发 ``apply_requested`` signal（携带 variant_id / approval 标识）。
- 存在 error 时禁用“应用方案”。
- controller 仅订阅 transaction result；失败则保留原 timeline 不变。
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.shared.i18n.i18n import tr


def _trf(key: str, default: str, **kwargs: object) -> str:
    """``tr`` + 保证 default 兜底时也做插值（i18n 未加载时数字仍显示）。"""
    result = tr(key, default, **kwargs)
    # 若返回值仍含未替换占位符，对 default 再做一次 format 兜底
    if "{" in result and "}" in result:
        try:
            return default.format(**kwargs)
        except (KeyError, IndexError):
            return result
    return result


class _DiffEntryWidget(QFrame):
    """单条 diff 条目（高风险项置顶样式）。"""

    def __init__(self, change: str, clip_id: str, track_id: str,
                 detail: str = "", high_risk: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.Box)
        bg = "#fdecea" if high_risk else "#f8f9fa"
        border = "#e74c3c" if high_risk else "#ddd"
        self.setStyleSheet(
            f"QFrame {{ background: {bg}; border: 1px solid {border}; "
            f"border-radius: 3px; margin: 1px; padding: 3px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        label_txt = f"[{change}] {clip_id}"
        if track_id:
            label_txt += f"  ({track_id})"
        if high_risk:
            label_txt = "⚠ " + label_txt
        lbl = QLabel(label_txt)
        lbl.setStyleSheet("font-weight: bold;")
        layout.addWidget(lbl)
        if detail:
            detail_lbl = QLabel(detail)
            detail_lbl.setStyleSheet("color: gray;")
            detail_lbl.setWordWrap(True)
            layout.addWidget(detail_lbl)


class AiTimelineDiffPanel(QWidget):
    """Timeline Diff 只读视图 + 应用入口。

    回填 diff dict（来自 ``transaction_service.compute_diff(...).to_dict()``）与
    validation 状态；高风险项置顶；error 时禁用应用按钮。
    """

    apply_requested = pyqtSignal(str)  # variant_id（或 plan_id）

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updating = False
        self._variant_id: str = ""
        # AUD-19619：预检警告随 diff 一并保存，供确认对话框（
        # AiApplyConfirmationDialog(warnings=...)）与外部读取。
        self._current_warnings: tuple[str, ...] = ()
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        title = QLabel(tr("ai_diff.title", "Timeline 变更"))
        title.setStyleSheet("font-weight: bold;")
        layout.addWidget(title)

        self._summary_label = QLabel()
        self._summary_label.setStyleSheet("padding: 2px;")
        layout.addWidget(self._summary_label)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._container = QWidget()
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.addStretch()
        self._scroll.setWidget(self._container)
        layout.addWidget(self._scroll)

        self._stale_label = QLabel()
        self._stale_label.setStyleSheet("color: #e67e22; padding: 2px;")
        self._stale_label.setVisible(False)
        layout.addWidget(self._stale_label)

        from PyQt6.QtWidgets import QPushButton
        self._apply_btn = QPushButton(tr("ai_diff.apply", "应用方案…"))
        self._apply_btn.clicked.connect(self._on_apply)
        layout.addWidget(self._apply_btn)

    def set_diff(self, diff: dict[str, Any], *, variant_id: str = "",
                 has_errors: bool = False, stale: bool = False) -> None:
        """回填 diff。``diff`` 是 ``TimelineDiff.to_dict()`` 的输出。

        - 高风险项（removed）置顶。
        - has_errors / stale 时禁用应用按钮。
        """
        self._updating = True
        try:
            self._variant_id = variant_id
            # 清空旧条目
            while self._container_layout.count() > 1:
                item = self._container_layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()

            added = diff.get("added", [])
            removed = diff.get("removed", [])
            moved = diff.get("moved", [])
            trimmed = diff.get("trimmed", [])
            raw_warnings = diff.get("warnings", [])
            # AUD-19619：warnings 此前读入即弃，预检警告（音乐版权、降级
            # effect 等）在只读 diff 视图中被整体丢弃。现在保存并以高风险
            # 样式置顶渲染，同时暴露给确认对话框链路。
            if isinstance(raw_warnings, (list, tuple)):
                warnings = tuple(str(w) for w in raw_warnings if w)
            else:
                warnings = ()
            self._current_warnings = warnings
            for warning in warnings:
                self._add_warning_entry(warning)

            # 高风险项置顶（AUD-19620：removed 条目在前；预检 warnings 已在
            # 上方以高风险样式置顶，覆盖音乐版权/降级 effect；替换表现为
            # removed+added 对，其 removed 半边随 removed 置顶）。
            for entry in removed:
                self._add_entry(entry, high_risk=True)
            for entry in added:
                self._add_entry(entry, high_risk=False)
            for entry in moved:
                self._add_entry(entry, high_risk=False)
            for entry in trimmed:
                self._add_entry(entry, high_risk=False)

            total = diff.get("total_changes", 0)
            dur_before = diff.get("duration_before", 0)
            dur_after = diff.get("duration_after", 0)
            summary = tr("ai_diff.summary",
                         "共 {n} 处变更 | 时长 {before}→{after} 帧 | 删除 {removed}",
                         n=total, before=dur_before, after=dur_after, removed=len(removed))
            if warnings:
                summary += _trf("ai_diff.warning_count", " | 预检警告 {count} 项",
                                count=len(warnings))
            self._summary_label.setText(summary)

            self._stale_label.setVisible(stale)
            if stale:
                self._stale_label.setText(
                    tr("ai_diff.stale", "快照已过期（base revision 改变）；请重新校验后再应用。")
                )
            # 禁用条件：有 error 或 stale
            self._apply_btn.setEnabled(not has_errors and not stale)
        finally:
            self._updating = False

    def _add_entry(self, entry: dict[str, Any], *, high_risk: bool) -> None:
        change = entry.get("change", "?")
        clip_id = entry.get("clip_id", "?")
        track_id = entry.get("track_id", "")
        detail = ""
        before = entry.get("before")
        after = entry.get("after")
        if before or after:
            detail = f"{before} → {after}"
        w = _DiffEntryWidget(change, clip_id, track_id, detail, high_risk=high_risk)
        self._container_layout.insertWidget(self._container_layout.count() - 1, w)

    def _add_warning_entry(self, warning: str) -> None:
        """AUD-19619：预检警告条目（高风险样式，置顶区）。"""
        w = _DiffEntryWidget(
            tr("ai_diff.warning_tag", "warning"), warning, "", "",
            high_risk=True,
        )
        self._container_layout.insertWidget(self._container_layout.count() - 1, w)

    @property
    def current_warnings(self) -> tuple[str, ...]:
        """当前 diff 携带的预检警告（供确认对话框透传展示）。"""
        return self._current_warnings

    def _on_apply(self) -> None:
        if self._updating:
            return
        self.apply_requested.emit(self._variant_id)

    @property
    def is_updating(self) -> bool:
        return self._updating


class AiApplyConfirmationDialog(QDialog):
    """应用方案的确认对话框（TAEP §11.6 步骤 11）。

    显示 undo group 名、影响 clip 数、删除数、范围、预检 warning；
    用户确认后 controller 向 transaction service 提供一次性 token。
    """

    def __init__(
        self, undo_label: str, affected_clips: int, removed_clips: int,
        warnings: tuple[str, ...] = (), parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("ai_diff.confirm_title", "确认应用 AI 方案"))
        self._approved = False

        layout = QVBoxLayout(self)

        info = QTextEdit()
        info.setReadOnly(True)
        warning_txt = "\n".join(f"⚠ {w}" for w in warnings) if warnings else tr("ai_diff.no_warnings", "无预检警告。")
        info.setPlainText(
            _trf("ai_diff.confirm_body",
               "Undo 组: {label}\n影响片段: {affected}\n将删除: {removed}\n\n预检:\n{warnings}",
               label=undo_label, affected=affected_clips, removed=removed_clips,
               warnings=warning_txt)
        )
        info.setMaximumHeight(200)
        layout.addWidget(info)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            tr("ai_diff.confirm_apply", "应用（可一次撤销）")
        )
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            tr("ai_diff.cancel", "取消")
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        self._approved = True
        self.accept()

    @property
    def approved(self) -> bool:
        return self._approved
