"""B7: 占位符片段创建对话框（粗剪定结构工作流入口）。"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLineEdit,
    QVBoxLayout,
)

from src.shared.i18n.i18n import I18N

__all__ = ["PlaceholderDialog"]


class PlaceholderDialog(QDialog):
    """填写占位片段的名称/备注/建议时长，确认后创建并落轨。"""

    def __init__(self, default_fps: float = 24.0, parent: Optional[object] = None) -> None:
        super().__init__(parent)
        # 25/30/60fps 时间线上按真实 fps 折算秒 → 帧（此前写死 24 导致
        # 时长缩水）。default_fps 无效时回退 24。
        try:
            self._fps = float(default_fps)
        except (TypeError, ValueError):
            self._fps = 24.0
        if not self._fps > 0.0:
            self._fps = 24.0
        self.setWindowTitle(I18N.tr("edit.placeholder.create_title", "新建占位片段"))
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText(I18N.tr(
            "edit.placeholder.name_placeholder", "例如：航拍空镜 [待补]"))
        form.addRow(I18N.tr("edit.placeholder.name_label", "名称:"), self._name_edit)

        self._note_edit = QLineEdit()
        self._note_edit.setPlaceholderText(I18N.tr(
            "edit.placeholder.note_placeholder", "备注（如：此处待补：航拍空镜）"))
        form.addRow(I18N.tr("edit.placeholder.note_label", "备注:"), self._note_edit)

        self._duration_spin = QDoubleSpinBox()
        self._duration_spin.setRange(0.5, 600.0)
        self._duration_spin.setValue(4.0)
        self._duration_spin.setSuffix(I18N.tr("edit.seconds_suffix", " s"))
        form.addRow(I18N.tr("edit.placeholder.duration_label", "建议时长:"), self._duration_spin)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            I18N.tr("edit.placeholder.create_ok", "创建"))
        buttons.accepted.connect(self._validate_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _validate_accept(self) -> None:
        if not self._name_edit.text().strip():
            self._name_edit.setFocus()
            return
        self.accept()

    @property
    def placeholder_name(self) -> str:
        return self._name_edit.text().strip()

    @property
    def note(self) -> str:
        return self._note_edit.text().strip()

    @property
    def duration_frames(self) -> int:
        return max(1, int(round(self._duration_spin.value() * self._fps)))
