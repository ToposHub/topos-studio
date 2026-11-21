"""AI 上下文摘要子组件（Phase 9 UI-2）。

显示素材数量、分析状态和工程 revision
（TAEP §11.6 步骤 4）。

约束：写“上下文选择”与 brief，**不**写 timeline（TAEP §11.3 ContextSummaryPanel）。
只发 signal；状态回填用 ``_updating`` 锁。
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QLabel,
    QVBoxLayout,
    QWidget,
)

from src.features.edit.ai_editing.assistant_events import ContextSummary
from src.shared.i18n.i18n import tr


def _trf(key: str, default: str, **kwargs: object) -> str:
    """在翻译目录尚未加载时，也正确格式化默认文案。"""
    text = tr(key, default, **kwargs)
    if "{" in default and kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


class AiContextSummary(QWidget):
    """上下文摘要：素材/分析/音乐/锁定计数。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updating = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(0)

        self._summary_label = QLabel()
        self._summary_label.setWordWrap(True)
        self._summary_label.setStyleSheet(
            "color: #c9c9d0; font-size: 11px; padding: 1px 2px;"
        )
        layout.addWidget(self._summary_label)

    def set_summary(self, summary: ContextSummary) -> None:
        """回填上下文摘要。"""
        self._updating = True
        try:
            stale = summary.analyzed_count > 0 and not summary.analysis_fresh
            self._summary_label.setText(
                _trf(
                    "ai_context.analysis_count", "已分析 {done}/{assets} 项",
                    assets=summary.asset_count, done=summary.analyzed_count,
                )
                if summary.asset_count else tr("ai_context.no_assets", "没有项目素材")
            )
            if stale:
                self._summary_label.setText(
                    self._summary_label.text() + " · " + tr("ai_context.stale_short", "分析待更新")
                )
            color = "#e1ad78" if stale else "#c9c9d0"
            self._summary_label.setToolTip(
                _trf(
                    "ai_context.summary_details", "素材 {assets} · 已分析 {done} · 工程版本 {rev}",
                    assets=summary.asset_count,
                    done=summary.analyzed_count,
                    rev=summary.snapshot_revision,
                )
            )
            self._summary_label.setStyleSheet(
                f"color: {color}; font-size: 11px; padding: 1px 2px;"
            )
        finally:
            self._updating = False

    @property
    def is_updating(self) -> bool:
        return self._updating
