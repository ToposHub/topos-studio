"""
Limiter Dialog (W26) — 限幅器参数编辑对话框。

点击 mixer 内嵌效果器列表中的 Limiter 条目时弹出。包含：
- 阈值（threshold_db, -20..0 dB）
- 释放时间（release_ms, 10..1000 ms）
- 三段式节流：拖动期 emit limiter_preview（实时听感），松手 emit limiter_committed（命令）

信号链：
  对话框 → TrackMixerWidget._on_limiter_dialog_* → track_effects_preview/committed
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QDial,
    QPushButton, QSizePolicy, QWidget,
)

from src.features.edit.core.timeline_v2 import LimiterParams
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.ui.style_sheet import PANEL_TITLE_LABEL_STYLE, PARAM_LABEL_STYLE


class LimiterDialog(QDialog):
    """限幅器参数编辑对话框（阈值 + 释放）。"""

    # 信号（track_id, LimiterParams）
    limiter_preview = pyqtSignal(str, object)    # 拖动期实时听感
    limiter_committed = pyqtSignal(str, object)  # 松手/关闭，进命令栈

    def __init__(
        self,
        track_id: str,
        track_name: str,
        instance_id: str,
        initial_params: Optional[LimiterParams] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._instance_id = instance_id  # 对应 TrackEffectInstance.instance_id
        self._current: LimiterParams = initial_params or LimiterParams.default()
        self._dragging = {"active": False}

        self.setWindowTitle(I18N.tr("audio.fx.limiter", "Limiter") + f" - {track_name or track_id}")
        self.setModal(False)
        self.setMinimumSize(320, 200)

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # 标题 + 开关
        top = QHBoxLayout()
        self._enable_toggle = ToggleSwitch(checked=self._current.enabled)
        self._enable_toggle.toggled.connect(self._on_enabled_toggled)
        top.addWidget(self._enable_toggle)
        title = QLabel(I18N.tr("audio.fx.limiter", "Limiter"))
        title.setStyleSheet(PANEL_TITLE_LABEL_STYLE)
        top.addWidget(title)
        top.addStretch()
        layout.addLayout(top)

        # 参数控件行
        params_layout = QHBoxLayout()
        params_layout.setSpacing(16)

        # 阈值
        self._thresh_dial, self._thresh_spin = self._make_knob_spin(
            label=I18N.tr("audio.fx.threshold", "Threshold"),
            value=self._current.threshold_db, minimum=-20.0, maximum=0.0, step=0.5,
            suffix=" dB", param_key="threshold_db",
        )
        params_layout.addWidget(self._thresh_dial, 1)

        # 释放
        self._rel_dial, self._rel_spin = self._make_knob_spin(
            label=I18N.tr("audio.fx.release", "Release"),
            value=self._current.release_ms, minimum=10.0, maximum=1000.0, step=10.0,
            suffix=" ms", param_key="release_ms",
        )
        params_layout.addWidget(self._rel_dial, 1)

        layout.addLayout(params_layout)

        # 底部按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        reset_btn = QPushButton(I18N.tr("audio.fx.reset", "Reset"))
        reset_btn.clicked.connect(self._on_reset)
        btn_layout.addWidget(reset_btn)
        close_btn = QPushButton(I18N.tr("common.close", "Close"))
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    def _make_knob_spin(
        self, label: str, value: float, minimum: float, maximum: float, step: float,
        suffix: str, param_key: str,
    ):
        """创建 dial + DragValue 联动控件（统一项目数值框样式）。"""
        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        lbl = QLabel(label)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(PARAM_LABEL_STYLE)
        lay.addWidget(lbl)

        dial = QDial()
        dial.setFixedSize(56, 56)
        dial_range = int((maximum - minimum) / step)
        dial.setMinimum(0)
        dial.setMaximum(max(dial_range, 1))
        dial.setValue(int(round((value - minimum) / step)))
        dial.setNotchesVisible(True)
        dial.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        spin = DragValue(
            "", min_v=minimum, max_v=maximum, default=value,
            decimals=1, suffix=suffix,
        )
        spin.setSingleStep(step)
        spin.setFixedValueWidth(90)

        def _on_dial_changed(dv: int) -> None:
            fv = minimum + dv * step
            spin.blockSignals(True)
            spin.setValue(fv)
            spin.blockSignals(False)
            self._on_param_changed(param_key, fv)

        def _on_spin_changed(fv: float) -> None:
            dial.blockSignals(True)
            dial.setValue(int(round((fv - minimum) / step)))
            dial.blockSignals(False)
            self._on_param_changed(param_key, fv)

        dial.valueChanged.connect(_on_dial_changed)
        spin.valueChanged.connect(_on_spin_changed)
        # 三段式节流：dial 拖拽 + DragValue 拖拽都走 preview→committed
        dial.sliderPressed.connect(lambda: self._dragging.update(active=True))
        dial.sliderReleased.connect(self._on_drag_released)
        spin.interaction_started.connect(lambda: self._dragging.update(active=True))
        spin.interaction_ended.connect(self._on_drag_released)

        lay.addWidget(dial, 0, Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(spin, 0, Qt.AlignmentFlag.AlignCenter)

        return container, spin

    # ────────────────────────── 事件处理 ──────────────────────────

    def _collect_params(self) -> LimiterParams:
        return LimiterParams(
            threshold_db=float(self._thresh_spin.value()),
            release_ms=float(self._rel_spin.value()),
            enabled=self._enable_toggle.isChecked(),
        )

    def _on_param_changed(self, param_key: str, value: float) -> None:
        """参数变化（dial/spin 联动）。"""
        params_dict = self._collect_params().to_dict()
        params_dict[param_key] = value
        self._current = LimiterParams.from_dict(params_dict)
        if self._dragging["active"]:
            self.limiter_preview.emit(self._track_id, self._current)
        else:
            self.limiter_committed.emit(self._track_id, self._current)

    def _on_enabled_toggled(self, checked: bool) -> None:
        """开启/关闭开关：立即提交（不走拖拽节流）。"""
        params_dict = self._collect_params().to_dict()
        params_dict["enabled"] = checked
        self._current = LimiterParams.from_dict(params_dict)
        self.limiter_committed.emit(self._track_id, self._current)

    def _on_drag_released(self) -> None:
        """dial 松手：发一次 committed。"""
        if not self._dragging["active"]:
            return
        self._dragging["active"] = False
        self._current = self._collect_params()
        self.limiter_committed.emit(self._track_id, self._current)

    def _on_reset(self) -> None:
        """重置为默认参数（保留当前开关状态）。

        AUD-37712: 程序化回填旋钮必须 blockSignals —— 此前 setValue 未阻塞，
        threshold 变化触发的 _on_param_changed 会用"回填到一半"的旧 release
        值重建参数并提前 committed（一次点击多次提交，且中间态命令带旧值）。
        阻塞后一次点击恰好一次 committed，两个旋钮都被完整重置。
        """
        was_enabled = self._enable_toggle.isChecked()
        self._current = LimiterParams.default()
        d = self._current.to_dict()
        d["enabled"] = was_enabled
        self._current = LimiterParams.from_dict(d)
        self._thresh_spin.blockSignals(True)
        self._rel_spin.blockSignals(True)
        try:
            self._thresh_spin.setValue(self._current.threshold_db)
            self._rel_spin.setValue(self._current.release_ms)
        finally:
            self._thresh_spin.blockSignals(False)
            self._rel_spin.blockSignals(False)
        self.limiter_committed.emit(self._track_id, self._current)
