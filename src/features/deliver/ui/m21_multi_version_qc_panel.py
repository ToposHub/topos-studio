"""M21.2c · 多版本 QC 面板（逐版本 LUFS / 字幕质量的用户呈现）。

接 M21.2b 的两个批量报告（``measure_version_lufs`` /
``subtitle_qc_summary``）做只读呈现：每版本一行——LUFS 实测/偏差/
未测、字幕 blocking/warning、逐版本判定；未测字段显示"未测"
（不冒充 0），测量失败显示错误。面板不发起测量（producer 在
工作流侧），只呈现已测结果，保证"未测标 unknown"的语义在 UI 不走样。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from PyQt6.QtWidgets import (
    QDialog,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)

_HEADERS = ["版本/产物", "LUFS", "偏差", "音频", "字幕阻塞", "字幕警告", "判定"]


def _fmt_num(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "未测"


class MultiVersionQcPanel(QDialog):
    """逐版本 QC 结果呈现（只读对话框）。

    Args:
        lufs_report: ``measure_version_lufs`` 输出（{path: entry}）；
            entry 含 input/error/integrated_lufs?/lufs_deviation?。
        subtitle_summary: ``subtitle_qc_summary`` 输出
            （all_ok/total_blocking/versions[{version_index,…}]）。
    """

    def __init__(
        self,
        lufs_report: Optional[Dict[str, Any]] = None,
        subtitle_summary: Optional[Dict[str, Any]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(I18N.tr("deliver.qc_title", "多版本交付 QC"))
        layout = QVBoxLayout(self)

        self._summary_label = QLabel()
        layout.addWidget(self._summary_label)

        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels(_HEADERS)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table)

        close_btn = QPushButton(I18N.tr("deliver.close", "关闭"))
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

        self.set_reports(lufs_report or {}, subtitle_summary or {})

    # ── 数据呈现 ─────────────────────────────────────────────────────

    def set_reports(
        self,
        lufs_report: Dict[str, Any],
        subtitle_summary: Dict[str, Any],
    ) -> None:
        """替换呈现数据（行数随报告重建）。"""
        lufs_report = lufs_report or {}
        subtitle_summary = subtitle_summary or {}
        paths = list(lufs_report.keys())
        sub_versions = {
            int(v.get("version_index", i)): v
            for i, v in enumerate(subtitle_summary.get("versions") or [])
        }

        all_ok = bool(subtitle_summary.get("all_ok", True))
        total_blocking = int(subtitle_summary.get("total_blocking", 0) or 0)
        if not paths:
            summary = "无版本测量数据（面板只呈现已测结果，不在此发起测量）"
        elif not sub_versions:
            # 字幕段未测（批队列入口只测 LUFS）——未测 ≠ 通过
            summary = f"共 {len(paths)} 个版本：LUFS 已测；字幕段未测"
        elif all_ok and total_blocking == 0:
            summary = f"共 {len(paths)} 个版本：字幕 QC 全部通过"
        else:
            summary = (f"共 {len(paths)} 个版本：字幕阻塞 {total_blocking} 条"
                       "（详见下表——未测/失败字段如实显示）")
        self._summary_label.setText(summary)

        self._table.setRowCount(len(paths))
        for row, path in enumerate(paths):
            entry = lufs_report.get(path) or {}
            inp = entry.get("input")
            error = str(entry.get("error", "") or "")
            sub = sub_versions.get(row, {})
            sub_measured = bool(sub)
            values = [
                os.path.basename(str(path)),
                # 未测（None/缺失）不冒充数值；测量失败显式标出
                ("测量失败" if error else
                 _fmt_num(entry.get("integrated_lufs"))),
                _fmt_num(entry.get("lufs_deviation"), 2),
                ("有" if getattr(inp, "audio_present", None) else
                 "无" if getattr(inp, "audio_present", None) is False
                 else "未测"),
                str(int(sub.get("blocking", 0) or 0)) if sub_measured
                else "未测",
                str(int(sub.get("warning", 0) or 0)) if sub_measured
                else "未测",
                "✓" if (not error and sub.get("ok", True)) else "✗",
            ]
            for col, text in enumerate(values):
                self._table.setItem(row, col, QTableWidgetItem(text))
        self._table.resizeColumnsToContents()
        logger.debug("MultiVersionQcPanel: %d versions presented",
                     len(paths))


__all__ = ["MultiVersionQcPanel"]
