"""Frame-accurate numeric controls for trimming the selected edit point."""

from __future__ import annotations

from typing import Callable, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QHBoxLayout, QLabel, QSpinBox, QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N


class TrimNumericDialog(QDialog):
    """数值 trim（P1-04）。

    对话框保持"哑"：范围/余量由调用方通过 ``bounds_for`` 回调提供
    （随 edge/mode 变化实时刷新），执行由调用方读取 values() 完成。
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        bounds_for: Optional[Callable[[str, str], Tuple[int, int]]] = None,
        headroom: Tuple[int, int] = (0, 0),
        duration_frames: int = 0,
        can_roll: bool = False,
        markers_available: bool = False,
    ) -> None:
        super().__init__(parent)
        self._bounds_for = bounds_for or (lambda _edge, _mode: (-99, 99))
        self.setWindowTitle(I18N.tr("edit.trim.numeric_title", "Numeric Trim"))
        self.setModal(True)
        self.setMinimumWidth(360)

        root = QVBoxLayout(self)
        form = QFormLayout()
        root.addLayout(form)

        self._edge_combo = QComboBox(self)
        self._edge_combo.addItem(I18N.tr("edit.trim.edge_out", "Out point"), "out")
        self._edge_combo.addItem(I18N.tr("edit.trim.edge_in", "In point"), "in")
        form.addRow(I18N.tr("edit.trim.edge", "Edge"), self._edge_combo)

        self._mode_combo = QComboBox(self)
        self._mode_combo.addItem(I18N.tr("edit.trim.mode_trim", "Trim"), "trim")
        self._mode_combo.addItem(I18N.tr("edit.trim.mode_ripple", "Ripple"), "ripple")
        if can_roll:
            self._mode_combo.addItem(I18N.tr("edit.trim.mode_roll", "Roll"), "roll")
        form.addRow(I18N.tr("edit.trim.mode", "Mode"), self._mode_combo)

        self._delta_spin = QSpinBox(self)
        self._delta_spin.setRange(-9999, 9999)
        self._delta_spin.setSuffix(I18N.tr("edit.frames_suffix", " f"))
        form.addRow(I18N.tr("edit.trim.delta", "Delta"), self._delta_spin)

        self._align_marker_check = QCheckBox(
            I18N.tr("edit.trim.align_marker", "Align to nearest marker"), self)
        self._align_marker_check.setEnabled(markers_available)
        form.addRow("", self._align_marker_check)

        # 诊断信息：手柄余量 / 合法范围 / 结果时长
        self._headroom_label = QLabel(self._headroom_text(headroom), self)
        self._range_label = QLabel("", self)
        self._result_label = QLabel("", self)
        for label in (self._headroom_label, self._range_label, self._result_label):
            root.addWidget(label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._duration = int(duration_frames)
        # AUD-21500: 程序化更新 SpinBox 上下限的状态锁——setMinimum/
        # setMaximum 钳取数值会同步发射 valueChanged 重入 _refresh，
        # 必须防递归（"程序化更新控件须状态锁"铁律）。
        self._updating = False
        self._edge_combo.currentIndexChanged.connect(lambda _i: self._refresh())
        self._mode_combo.currentIndexChanged.connect(lambda _i: self._refresh())
        self._delta_spin.valueChanged.connect(lambda _v: self._refresh())
        self._refresh()

    @staticmethod
    def _headroom_text(headroom: Tuple[int, int]) -> str:
        return I18N.tr(
            "edit.trim.headroom", "Handles: {head} f head / {tail} f tail",
        ).format(head=headroom[0], tail=headroom[1])

    def current_edge(self) -> str:
        return str(self._edge_combo.currentData() or "out")

    def current_mode(self) -> str:
        return str(self._mode_combo.currentData() or "trim")

    def _bounds(self) -> Tuple[int, int]:
        if self.current_mode() == "roll":
            return self._bounds_for("out", "roll")
        return self._bounds_for(self.current_edge(), self.current_mode())

    def _refresh(self) -> None:
        # AUD-21500: 程序化范围更新期间的钳取重入直接忽略——第二遍执行的
        # bounds_for 回调（遍历 track.clips）与标签刷新都是重复副作用。
        if self._updating:
            return
        self._updating = True
        try:
            self._refresh_locked()
        finally:
            self._updating = False

    def _refresh_locked(self) -> None:
        low, high = self._bounds()
        roll = self.current_mode() == "roll"
        self._edge_combo.setEnabled(not roll)
        minimum, maximum = sorted((low, high))
        self._delta_spin.setMinimum(minimum)
        self._delta_spin.setMaximum(max(maximum, minimum))
        self._range_label.setText(I18N.tr(
            "edit.trim.range", "Valid range: {min} … {max} f",
        ).format(min=low, max=high))
        delta = self._delta_spin.value()
        if roll:
            result = I18N.tr("edit.trim.result_roll",
                             "Cut point moves {delta:+d} f")
        else:
            new_duration = self._duration - delta if self.current_edge() == "in" \
                else self._duration + delta
            result = I18N.tr("edit.trim.result_duration",
                             "New duration: {dur} f")
            result = result.format(dur=max(1, new_duration))
        self._result_label.setText(result.format(delta=delta)
                                   if roll else result)

    def values(self) -> Tuple[str, str, int, bool]:
        """(edge, mode, delta_frames, align_marker)。"""
        return (
            self.current_edge(),
            self.current_mode(),
            int(self._delta_spin.value()),
            self._align_marker_check.isChecked(),
        )


__all__ = ["TrimNumericDialog"]
