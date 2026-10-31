"""C5: 章节标记导出对话框（YouTube 章节文本 / FFMetadata）。

从当前时间线的 chapter 标记生成两种通用交付格式，预览 + 复制/保存。
纯展示层；格式转换在 core/chapter_export.py（无 Qt，可独立单测）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from src.features.edit.core.chapter_export import (
    format_ffmetadata,
    format_youtube_chapters,
)
from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)

__all__ = ["ChapterExportDialog"]

_FORMAT_YOUTUBE = "youtube"
_FORMAT_FFMETADATA = "ffmetadata"


class ChapterExportDialog(QDialog):
    """章节标记 → YouTube 章节文本 / FFMetadata 的预览与导出。"""

    def __init__(
        self,
        sequence: Any,
        parent: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        self._sequence = sequence
        self.setWindowTitle(I18N.tr("edit.chapter_export.title", "导出章节标记"))
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)

        self._format_combo = QComboBox()
        self._format_combo.addItem(
            I18N.tr("edit.chapter_export.format_youtube",
                    "YouTube 章节文本 (.txt)"),
            _FORMAT_YOUTUBE,
        )
        self._format_combo.addItem(
            I18N.tr("edit.chapter_export.format_ffmetadata",
                    "FFMetadata (.ffmeta)"),
            _FORMAT_FFMETADATA,
        )
        self._format_combo.currentIndexChanged.connect(self._refresh_preview)
        layout.addWidget(self._format_combo)

        self._preview = QTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setMinimumHeight(260)
        layout.addWidget(self._preview)

        hint = QLabel(I18N.tr(
            "edit.chapter_export.hint",
            "章节来自时间线上类型为\"章节\"的标记（Alt+M 添加）；"
            "FFMetadata 可经 ffmpeg -map_chapters 封装进 MP4/MOV。",
        ))
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        copy_btn = QPushButton(I18N.tr("edit.chapter_export.copy", "复制到剪贴板"))
        copy_btn.clicked.connect(self._copy_to_clipboard)
        buttons.addWidget(copy_btn)
        save_btn = QPushButton(I18N.tr("edit.chapter_export.save", "保存…"))
        save_btn.clicked.connect(self._save_file)
        buttons.addWidget(save_btn)
        buttons.addStretch()
        close_btn = QPushButton(I18N.tr("edit.chapter_export.close", "关闭"))
        close_btn.clicked.connect(self.reject)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self._refresh_preview()

    # ------------------------------------------------------------------

    def _fps(self) -> float:
        fps = getattr(self._sequence, "fps", 24.0) or 24.0
        try:
            value = float(fps)
        except (TypeError, ValueError):
            value = 24.0
        return value if value > 0 else 24.0

    def _markers(self) -> list:
        return list(getattr(self._sequence, "markers", ()) or ())

    def _duration_frames(self) -> int:
        return int(getattr(self._sequence, "duration_frames", 0) or 0)

    def _current_text(self) -> str:
        markers = self._markers()
        if self._format_combo.currentData() == _FORMAT_FFMETADATA:
            return format_ffmetadata(
                markers, self._fps(), self._duration_frames())
        return format_youtube_chapters(markers, self._fps())

    def _refresh_preview(self) -> None:
        text = self._current_text()
        if not text:
            text = I18N.tr(
                "edit.chapter_export.empty",
                "当前时间线没有章节标记。在时间线上按 Alt+M 添加章节。",
            )
        self._preview.setPlainText(text)

    def _copy_to_clipboard(self) -> None:
        text = self._current_text()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        QMessageBox.information(
            self,
            I18N.tr("edit.chapter_export.title", "导出章节标记"),
            I18N.tr("edit.chapter_export.copied", "已复制到剪贴板。"),
        )

    def _save_file(self) -> None:
        is_ffmeta = (
            self._format_combo.currentData() == _FORMAT_FFMETADATA
        )
        suffix = ".ffmeta" if is_ffmeta else ".txt"
        title_key = (
            "edit.chapter_export.save_ffmeta_title"
            if is_ffmeta else "edit.chapter_export.save_txt_title"
        )
        title_default = (
            "保存 FFMetadata 文件" if is_ffmeta else "保存 YouTube 章节文本"
        )
        path, _ = QFileDialog.getSaveFileName(
            self,
            I18N.tr(title_key, title_default),
            f"chapters{suffix}",
            f"{suffix[1:].upper()} (*{suffix})",
        )
        if not path:
            return
        if not path.endswith(suffix):
            path += suffix
        text = self._current_text()
        if not text:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            QMessageBox.critical(
                self,
                I18N.tr("edit.chapter_export.title", "导出章节标记"),
                str(exc),
            )
            return
        QMessageBox.information(
            self,
            I18N.tr("edit.chapter_export.title", "导出章节标记"),
            I18N.tr(
                "edit.chapter_export.saved",
                "已保存到:\n{path}",
                path=path,
            ),
        )
