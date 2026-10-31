"""P1-21 序列级字幕交付对话框：格式 + 轨道选择（语言/角色/cue 数）。"""

from __future__ import annotations

from typing import List, Sequence, Tuple

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QVBoxLayout,
)

from src.features.edit.core.captions.delivery import SubtitleTrackSummary
from src.shared.i18n.i18n import I18N

#: 交付场景常用格式子集（SCC 等广播格式保留在轨道级右键导出）
DELIVERY_FORMATS: Tuple[str, ...] = ("srt", "vtt", "ttml", "ass")

_FORMAT_LABELS = {
    "srt": "SRT（通用字幕，毫秒时基）",
    "vtt": "WebVTT（网页/流媒体）",
    "ttml": "TTML（广播/IMSC 基础，含 xml:lang）",
    "ass": "ASS/SSA（样式+卡拉OK）",
}


class SubtitleDeliveryDialog(QDialog):
    """选择交付格式与要导出的字幕轨。

    每轨一行复选框：``标签 [语言码] (角色) — N 条 cue``，默认勾选
    非空轨道；空轨道默认不勾（导出时跳过）。
    """

    def __init__(
        self,
        tracks: Sequence[SubtitleTrackSummary],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            I18N.tr("edit.subtitle_delivery.title", "导出字幕轨（序列级）")
        )
        self._track_boxes: List[Tuple[str, QCheckBox]] = []

        layout = QVBoxLayout(self)

        form = QFormLayout()
        self._format_combo = QComboBox()
        for name in DELIVERY_FORMATS:
            label = _FORMAT_LABELS.get(name, name.upper())
            self._format_combo.addItem(label, name)
        form.addRow(
            I18N.tr("edit.subtitle_delivery.format", "交付格式:"), self._format_combo
        )
        layout.addLayout(form)

        tracks_box = QGroupBox(
            I18N.tr("edit.subtitle_delivery.tracks", "字幕轨")
        )
        tracks_layout = QVBoxLayout(tracks_box)
        for summary in tracks:
            lang = summary.language if summary.language != "und" else "—"
            check = QCheckBox(
                I18N.tr(
                    "edit.subtitle_delivery.track_row",
                    "{label}  [{lang}]  ({role}) — {n} cues",
                    label=summary.label, lang=lang, role=summary.role,
                    n=summary.cue_count,
                )
            )
            check.setChecked(summary.cue_count > 0)
            self._track_boxes.append((summary.track_id, check))
            tracks_layout.addWidget(check)
        if not self._track_boxes:
            placeholder = QLabel(
                I18N.tr(
                    "edit.subtitle_delivery.no_tracks",
                    "当前时间线没有字幕轨。",
                )
            )
            tracks_layout.addWidget(placeholder)
        layout.addWidget(tracks_box)

        hint = QLabel(
            I18N.tr(
                "edit.subtitle_delivery.hint",
                "文件按 <时间线名>.<语言码>.<扩展名> 写入所选目录，"
                "并生成 subtitle_manifest.json（语言/角色/cue 数）。",
            )
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> Tuple[str, Tuple[str, ...]]:
        """返回 (format_name, 选中的 track_id 元组)。"""
        selected = tuple(
            track_id for track_id, check in self._track_boxes if check.isChecked()
        )
        return (self._format_combo.currentData() or "srt", selected)


__all__ = ["DELIVERY_FORMATS", "SubtitleDeliveryDialog"]
