"""C4: 多机位波形同步结果确认对话框。

worker 完成后先展示各角度偏移/置信度，用户"应用"才写回组 metadata
（可撤销靠后续切换命令的既有语义）；取消则丢弃本次结果。

出界警告：|offset| 超过该角度素材总帧数时行内标注 —— 对齐后该角度
源区间将出界（读取端会钳制到首帧，画面静止）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)

__all__ = ["MulticamSyncResultRow", "MulticamSyncResultDialog"]


@dataclass(frozen=True)
class MulticamSyncResultRow:
    """确认对话框中的一行（一个角度）。"""

    asset_id: str
    label: str
    offset_frames: int
    lag_seconds: float
    confidence: float
    error: str = ""
    #: True = |offset| 超过素材总帧数（对齐后源区间出界）
    out_of_range: bool = False


class MulticamSyncResultDialog(QDialog):
    """展示每角度同步结果；``应用`` 才真正写回。"""

    def __init__(
        self,
        rows: Sequence[MulticamSyncResultRow],
        parent: Optional[QDialog] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(I18N.tr(
            "edit.multicam.sync_result_title", "波形同步结果"))
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)

        hint = QLabel(I18N.tr(
            "edit.multicam.sync_result_hint",
            "各角度相对参考角度的偏移。正偏移 = 该角度开录更早。"
            "确认后写回角度偏移（影响后续角度切换）。",
        ))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        table = QTableWidget(len(rows), 4, self)
        table.setHorizontalHeaderLabels([
            I18N.tr("edit.multicam.sync_col_angle", "角度"),
            I18N.tr("edit.multicam.sync_col_offset", "偏移"),
            I18N.tr("edit.multicam.sync_col_lag", "滞后(秒)"),
            I18N.tr("edit.multicam.sync_col_conf", "置信度"),
        ])
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.verticalHeader().setVisible(False)

        for row_index, rowdata in enumerate(rows):
            if rowdata.error:
                angle_text = I18N.tr(
                    "edit.multicam.sync_failed_angle",
                    "{name}（失败：{err}）",
                    name=rowdata.label, err=rowdata.error,
                )
                offset_text = "—"
                lag_text = "—"
                conf_text = "—"
            else:
                angle_text = rowdata.label
                if rowdata.offset_frames == 0:
                    offset_text = I18N.tr(
                        "edit.multicam.sync_aligned", "已对齐")
                else:
                    sign = "+" if rowdata.offset_frames > 0 else "−"
                    offset_text = f"{sign}{abs(rowdata.offset_frames)}f"
                lag_text = f"{rowdata.lag_seconds:+.2f}"
                conf_text = f"{rowdata.confidence:.2f}"
            if rowdata.out_of_range:
                angle_text += " " + I18N.tr(
                    "edit.multicam.sync_out_of_range_warn",
                    "⚠ 对齐后源区间出界",
                )
            angle_item = QTableWidgetItem(angle_text)
            if rowdata.error or rowdata.out_of_range:
                angle_item.setForeground(Qt.GlobalColor.red)
            table.setItem(row_index, 0, angle_item)
            table.setItem(row_index, 1, QTableWidgetItem(offset_text))
            table.setItem(row_index, 2, QTableWidgetItem(lag_text))
            table.setItem(row_index, 3, QTableWidgetItem(conf_text))
        layout.addWidget(table)

        self._button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._button_box.button(
            QDialogButtonBox.StandardButton.Ok
        ).setText(I18N.tr("edit.multicam.sync_apply", "应用"))
        self._button_box.accepted.connect(self.accept)
        self._button_box.rejected.connect(self.reject)
        layout.addWidget(self._button_box)
