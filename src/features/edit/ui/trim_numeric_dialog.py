"""P1-04 数值 trim 对话框与 Trim Monitor。

- TrimNumericDialog：输入帧增量修剪选中片段入/出点（Trim/Ripple/Roll），
  实时显示手柄余量与合法范围，支持"对齐到最近标记"。
- TrimMonitorDialog：roll trim 拖拽期间的轻量双画面监视器（cut 两侧
  时间码 A/B + 位移量 + 手柄余量）。帧渲染走既有 program viewer /
  scrub 管线；本对话框负责 trim 上下文的数值显示。
"""

from __future__ import annotations

from typing import Callable, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
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


class TrimMonitorDialog(QWidget):
    """Trim Monitor（P1-04/F2.3）：trim/roll 期间的双画面上下文显示。

    A = cut 左侧最后一帧（真实保留边界帧 + 时间码），B = cut 右侧首帧；
    Δ = 当前位移（帧），下方显示两侧手柄余量诊断。F2.3：上半区升级为
    真实素材帧双画面（经 TrimDualFrameProvider 后台解码，generation
    校验后投递）；解码中显示占位，无素材侧显示灰面。

    窗口仍是被动上下文显示（Tool + 不抢焦点，N15），帧到达只刷新画面。
    """

    _FRAME_PANEL_MIN_H = 180

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            parent, Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setWindowTitle(I18N.tr("edit.trim.monitor_title", "Trim Monitor"))
        # N15/T3-07：Trim Monitor 是 Roll 拖拽期间被动的上下文显示窗，
        # 不是编辑目标。show() 绝不允许激活/抢焦点——此前 Tool 窗口首
        # 次 show 会成为 activeWindow，正在 ROLLING 的 TimelineView 收
        # 到 ActiveWindowFocusReason 的 FocusOut，拖拽在用户松手前就被
        # 取消、正常 Roll 无法提交。
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        layout = QVBoxLayout(self)

        # F2.3：双画面帧区（左=左片保留边界帧，右=右片首帧）
        frames_row = QHBoxLayout()
        self._frame_a = QLabel(self)
        self._frame_b = QLabel(self)
        for frame_label in (self._frame_a, self._frame_b):
            frame_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            frame_label.setMinimumHeight(self._FRAME_PANEL_MIN_H)
            frame_label.setStyleSheet(
                "background: #101014; color: #666; border-radius: 4px;")
        frames_row.addWidget(self._frame_a, stretch=1)
        frames_row.addWidget(self._frame_b, stretch=1)
        layout.addLayout(frames_row)

        tc_row = QHBoxLayout()
        self._label_a = QLabel("—", self)
        self._label_b = QLabel("—", self)
        for label in (self._label_a, self._label_b):
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet(
                "font-family: monospace; font-size: 16px; font-weight: bold;"
                "background: #1c1c20; color: #e8e8e8; padding: 6px;"
                "border-radius: 4px;")
        tc_row.addWidget(self._label_a, stretch=1)
        tc_row.addWidget(self._label_b, stretch=1)
        layout.addLayout(tc_row)

        self._delta_label = QLabel("", self)
        self._delta_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._delta_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self._delta_label)

        self._info_label = QLabel("", self)
        self._info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._info_label.setStyleSheet("color: #9a9a9a;")
        layout.addWidget(self._info_label)

    # ── F2.3 双帧画面 ──────────────────────────────────────────────────

    def set_frames_pending(self) -> None:
        """新 cut 位置已请求、帧解码中——两侧显示占位（保留旧画面会被
        误读为当前边界，占位更诚实；generation 校验失败的迟到帧由调用
        方丢弃，不会覆盖占位后的新帧）。"""
        for frame_label in (self._frame_a, self._frame_b):
            frame_label.setPixmap(QPixmap())
            frame_label.setText(I18N.tr("edit.trim.monitor_decoding", "…"))

    def update_frame(self, side: str, image) -> None:
        """显示一侧的真实边界帧（side ∈ {"left", "right"}，QImage）。"""
        target = self._frame_a if side == "left" else self._frame_b
        target.setText("")
        scaled = QPixmap.fromImage(image).scaled(
            target.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        target.setPixmap(scaled)

    def update_roll(self, tc_a: str, tc_b: str, delta_frames: int,
                    headroom: Tuple[int, int] = (0, 0)) -> None:
        """roll trim 拖拽刷新：cut 两侧 TC + 位移 + 手柄。"""
        self._label_a.setText(tc_a or "—")
        self._label_b.setText(tc_b or "—")
        self._delta_label.setText(I18N.tr(
            "edit.trim.monitor_delta", "Δ {delta:+d} f").format(delta=delta_frames))
        self._info_label.setText(I18N.tr(
            "edit.trim.monitor_headroom",
            "Left tail {tail} f / right head {head} f",
        ).format(tail=headroom[0], head=headroom[1]))

    def update_trim(self, tc: str, delta_frames: int,
                    headroom: Tuple[int, int] = (0, 0)) -> None:
        """普通/波纹 trim 刷新：单边 TC + 位移（帧区右半留给邻片/空）。"""
        self._label_a.setText(tc or "—")
        self._label_b.setText("—")
        self._frame_b.setText(I18N.tr("edit.trim.monitor_no_neighbor", "—"))
        self._frame_b.setPixmap(QPixmap())
        self._delta_label.setText(I18N.tr(
            "edit.trim.monitor_delta", "Δ {delta:+d} f").format(delta=delta_frames))
        self._info_label.setText(I18N.tr(
            "edit.trim.monitor_handles",
            "Handles: head {head} f / tail {tail} f",
        ).format(head=headroom[0], tail=headroom[1]))


__all__ = ["TrimMonitorDialog", "TrimNumericDialog"]
