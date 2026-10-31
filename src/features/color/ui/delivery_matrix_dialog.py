"""Delivery Matrix 对话框（P1-COLOR-25）。

导出前展示调色能力报告：哪些操作完整保留、哪些烘焙、哪些会丢失；
存在有损项时必须由用户显式确认才允许继续导出。

用法：
    if report.has_loss and not DeliveryMatrixDialog.confirm(parent, report):
        return  # 用户取消导出
"""
from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from src.features.color.core.delivery_matrix import (
    DELIVERY_BAKED, DELIVERY_DROPPED, DELIVERY_FULL,
    DeliveryMatrixReport, format_report_text, row_delivery_label, row_label,
)
from src.shared.i18n.i18n import I18N

_RESULT_COLORS = {
    DELIVERY_FULL: QColor("#4caf50"),
    DELIVERY_BAKED: QColor("#ff9800"),
    DELIVERY_DROPPED: QColor("#f44336"),
}


class DeliveryMatrixDialog(QDialog):
    """展示一份 DeliveryMatrixReport；有损时按钮文案为显式确认。"""

    def __init__(self, parent: Optional[QWidget],
                 report: DeliveryMatrixReport) -> None:
        super().__init__(parent)
        self._report = report

        target_names = {
            "video": I18N.tr("delivery.target_video", "时间线视频导出"),
            "still": I18N.tr("delivery.target_still", "静态图导出"),
            "lut": I18N.tr("delivery.target_lut", "3D LUT 导出"),
        }
        self.setWindowTitle(
            I18N.tr("delivery.title", "导出能力报告 (Delivery Matrix)"))
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)

        header = QLabel(I18N.tr(
            "delivery.header",
            "以下调色操作在「{target}」中的交付结果：",
            target=target_names.get(report.target, report.target)))
        header.setWordWrap(True)
        layout.addWidget(header)

        # 摘要行：N 保留 / M 烘焙 / K 丢失
        counts = (
            f"{len(report.kept_rows)} {I18N.tr('delivery.result_full', '完整保留')}"
            f" · {len(report.baked_rows)} {I18N.tr('delivery.result_baked', '烘焙')}"
            f" · {len(report.dropped_rows)} {I18N.tr('delivery.result_dropped', '丢失')}"
        )
        summary = QLabel(counts)
        summary.setStyleSheet("font-weight: bold;")
        layout.addWidget(summary)

        table = QTableWidget(len(report.rows), 2, self)
        table.setHorizontalHeaderLabels([
            I18N.tr("delivery.col_operation", "调色操作"),
            I18N.tr("delivery.col_result", "交付结果"),
        ])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.setAlternatingRowColors(True)
        for row_idx, row in enumerate(report.rows):
            op_item = QTableWidgetItem(row_label(row, I18N.tr))
            op_item.setData(Qt.ItemDataRole.UserRole, row.operation)
            result_item = QTableWidgetItem(row_delivery_label(row, I18N.tr))
            color = _RESULT_COLORS.get(row.delivery)
            if color is not None:
                result_item.setForeground(color)
            table.setItem(row_idx, 0, op_item)
            table.setItem(row_idx, 1, result_item)
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        if report.rows:
            table.setMinimumHeight(min(60 + 26 * len(report.rows), 320))
        layout.addWidget(table)

        if report.has_loss:
            warn = QLabel(I18N.tr(
                "delivery.loss_warning",
                "标为「烘焙/丢失」的效果不会在输出中保持预览外观。\n"
                "确认继续导出前请核对以上清单。"))
            warn.setWordWrap(True)
            warn.setStyleSheet("color: #f44336;")
            layout.addWidget(warn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if report.has_loss:
            ok.setText(I18N.tr("delivery.confirm_lossy", "确认并有损导出"))
        else:
            ok.setText(I18N.tr("delivery.continue", "继续"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(
            I18N.tr("export.cancel", "取消"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box = QHBoxLayout()
        box.addStretch(1)
        box.addWidget(buttons)
        layout.addLayout(box)

    @classmethod
    def confirm(cls, parent: Optional[QWidget],
                report: DeliveryMatrixReport) -> bool:
        """有损导出前的确认门槛。无损失时直接放行（不弹窗）。"""
        if not report.has_loss:
            return True
        dialog = cls(parent, report)
        return dialog.exec() == QDialog.DialogCode.Accepted

    @classmethod
    def show_report(cls, parent: Optional[QWidget],
                    report: DeliveryMatrixReport) -> None:
        """无条件展示报告（信息性，如导出前主动查看）。"""
        dialog = cls(parent, report)
        dialog.exec()


def report_summary_line(report: DeliveryMatrixReport) -> str:
    """单行摘要（状态栏用）。"""
    return format_report_text(report, I18N.tr).replace("\n", " · ")
