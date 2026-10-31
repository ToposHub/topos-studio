"""M12.2 · 代理/渲染缓存管理面板（F6.6 安全清理的 UI 入口）。

接既有 ``AssetProxyManager.proxy_usage_report`` / ``purge_proxies``
（安全规则在服务层：只删 manifest 登记项、路径逃逸拒绝、活动 GENERATING
保护、原片只读）。面板只做呈现/选择/确认/结果报告——每个清理选项对应
服务层的一个安全谓词组合，不新增旁路删除路径。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)


def _format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} GB"


class ProxyCleanupPanel(QDialog):
    """代理/渲染缓存报告与安全清理（模式化对话框）。"""

    def __init__(
        self,
        manager: Any,
        *,
        report_fn: Optional[Callable[[], Dict[str, Any]]] = None,
        purge_fn: Optional[Callable[..., Dict[str, int]]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._manager = manager
        self._report_fn = report_fn or getattr(
            manager, "proxy_usage_report", None)
        self._purge_fn = purge_fn or getattr(manager, "purge_proxies", None)
        self.last_result: Optional[Dict[str, int]] = None

        self.setWindowTitle(I18N.tr("proxy_cleanup.title", "代理与渲染缓存管理"))
        layout = QVBoxLayout(self)

        self._usage_label = QLabel()
        layout.addWidget(self._usage_label)

        self._orphans_only = QCheckBox(
            I18N.tr(
                "proxy_cleanup.orphans_only",
                "只清源已不存在的孤儿代理（推荐——跨项目引用保护）"))
        self._orphans_only.setChecked(True)
        layout.addWidget(self._orphans_only)

        self._include_failed = QCheckBox(
            I18N.tr(
                "proxy_cleanup.include_failed",
                "同时清理失败（FAILED）与过期（STALE）代理"))
        layout.addWidget(self._include_failed)

        self._purge_btn = QPushButton(I18N.tr("proxy_cleanup.purge", "清理…"))
        self._purge_btn.clicked.connect(self._on_purge)
        layout.addWidget(self._purge_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._refresh()

    def _refresh(self) -> None:
        if not callable(self._report_fn):
            self._usage_label.setText(
                I18N.tr("proxy_cleanup.service_unavailable", "代理服务不可用（无报告入口）"))
            return
        try:
            report = self._report_fn() or {}
        except Exception:  # noqa: BLE001 — 报告失败明示不崩
            logger.exception("proxy usage report failed")
            self._usage_label.setText(
                I18N.tr("proxy_cleanup.usage_failed", "使用状态读取失败（见日志）"))
            return
        statuses = report.get("statuses") or {}
        status_txt = "，".join(f"{k}: {v}" for k, v in sorted(statuses.items())) \
            or "无条目"
        self._usage_label.setText(
            f"代理条目：{report.get('total', 0)}（{status_txt}）\n"
            f"磁盘占用：{_format_bytes(report.get('bytes', 0))}")

    def _on_purge(self) -> None:
        if not callable(self._purge_fn):
            QMessageBox.warning(self, "不可用", "代理服务不可用（无清理入口）")
            return
        orphans_only = self._orphans_only.isChecked()
        include_failed = self._include_failed.isChecked()
        if orphans_only:
            statuses = ("READY", "STALE", "FAILED")
            only_missing = True
            what = "源已不存在的孤儿代理"
        elif include_failed:
            statuses = ("STALE", "FAILED")
            only_missing = False
            what = "过期与失败的代理"
        else:
            statuses = ("STALE", "FAILED")
            only_missing = False
            what = "过期与失败的代理"
        confirm = QMessageBox.question(
            self, "确认清理",
            f"将清理{what}（活动生成任务受保护，绝不触碰原片/目录外文件）。\n继续？")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self._purge_fn(
                statuses=statuses, only_if_source_missing=only_missing)
        except Exception as exc:  # noqa: BLE001 — 清理失败明示
            QMessageBox.critical(self, "清理失败", f"清理执行失败：{exc}")
            return
        self.last_result = dict(result)
        QMessageBox.information(
            self, "清理完成",
            f"已删除 {result.get('removed', 0)} 个代理\n"
            f"跳过活动任务 {result.get('skipped_active', 0)}\n"
            f"跳过缺失 {result.get('skipped_not_found', 0)}\n"
            f"跳过目录外 {result.get('skipped_outside_dir', 0)}")
        self._refresh()


__all__ = ["ProxyCleanupPanel"]
