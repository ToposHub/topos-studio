"""
Delay Dialog — 延迟器参数编辑对话框（含 ping-pong）。

点击 mixer 内嵌效果器列表中的 Delay 条目时弹出。包含：
- 延迟时间（time_ms, 1..2000 ms）
- 反馈量（feedback, 0..90 %）
- 湿信号比例（mix, 0..100 %）
- 低切频率（low_cut_hz, 20..1000 Hz，反馈回路去低频隆隆）
- 乒乓模式（ping_pong, 复选框，立体声反馈交叉）
- 预设下拉（默认/回声/空间回响/乒乓/节奏/Dub）
- 三段式节流：拖动期 emit delay_preview（实时听感），松手 emit delay_committed（命令）

移植自 audio_pro 的 Delay 参数。

信号链：
  对话框 → TrackMixerWidget._on_delay_edited → track_effects_preview/committed
"""

from __future__ import annotations

from typing import Optional, Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QDial,
    QPushButton, QSizePolicy, QWidget, QCheckBox,
)

from src.features.edit.core.timeline_v2 import DelayParams
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.ui.style_sheet import PANEL_TITLE_LABEL_STYLE, PARAM_LABEL_STYLE


# 延迟预设（移植自 audio_pro PresetManager.presets.delay）
DELAY_PRESETS: list[dict[str, Any]] = [
    {"name": "默认",     "params": {"time_ms": 300.0, "feedback": 40.0, "mix": 30.0, "ping_pong": False, "low_cut_hz": 100.0}},
    {"name": "回声",     "params": {"time_ms": 80.0,  "feedback": 10.0, "mix": 25.0, "ping_pong": False, "low_cut_hz": 200.0}},
    {"name": "空间回响", "params": {"time_ms": 450.0, "feedback": 55.0, "mix": 40.0, "ping_pong": False, "low_cut_hz": 150.0}},
    {"name": "乒乓延迟", "params": {"time_ms": 350.0, "feedback": 45.0, "mix": 35.0, "ping_pong": True,  "low_cut_hz": 100.0}},
    {"name": "节奏延迟", "params": {"time_ms": 250.0, "feedback": 35.0, "mix": 25.0, "ping_pong": False, "low_cut_hz": 300.0}},
    {"name": "Dub 回响", "params": {"time_ms": 500.0, "feedback": 70.0, "mix": 50.0, "ping_pong": True,  "low_cut_hz": 80.0}},
]


class DelayDialog(QDialog):
    """延迟器参数编辑对话框（4 个旋钮 + ping-pong 复选框 + 预设）。"""

    # 信号（track_id, DelayParams）
    delay_preview = pyqtSignal(str, object)    # 拖动期实时听感
    delay_committed = pyqtSignal(str, object)  # 松手/关闭，进命令栈

    def __init__(
        self,
        track_id: str,
        track_name: str,
        instance_id: str,
        initial_params: Optional[DelayParams] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._instance_id = instance_id
        self._current: DelayParams = initial_params or DelayParams.default()
        self._dragging = {"active": False}

        self.setWindowTitle(I18N.tr("audio.fx.delay", "Delay") + f" - {track_name or track_id}")
        self.setModal(False)
        self.setMinimumSize(560, 280)

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # 标题 + 预设
        top = QHBoxLayout()
        self._enable_toggle = ToggleSwitch(checked=self._current.enabled)
        self._enable_toggle.toggled.connect(self._on_enabled_toggled)
        top.addWidget(self._enable_toggle)
        title = QLabel(I18N.tr("audio.fx.delay", "Delay"))
        title.setStyleSheet(PANEL_TITLE_LABEL_STYLE)
        top.addWidget(title)
        top.addStretch()
        top.addWidget(QLabel(I18N.tr("audio.fx.preset", "Preset")))
        self._preset_combo = ChevronComboBox()
        self._preset_combo.addItem("—", None)
        for p in DELAY_PRESETS:
            self._preset_combo.addItem(p["name"], p["params"])
        self._preset_combo.activated.connect(self._on_preset_selected)
        top.addWidget(self._preset_combo)
        layout.addLayout(top)

        # 4 个旋钮
        knobs_layout = QHBoxLayout()
        knobs_layout.setSpacing(8)
        c = self._current
        self._time_w = self._make_knob_spin(
            I18N.tr("audio.fx.time", "Time"),
            c.time_ms, 1.0, 2000.0, 1.0, " ms", "time_ms",
        )
        self._feedback_w = self._make_knob_spin(
            I18N.tr("audio.fx.feedback", "Feedback"),
            c.feedback, 0.0, 90.0, 1.0, " %", "feedback",
        )
        self._mix_w = self._make_knob_spin(
            I18N.tr("audio.fx.mix", "Mix"),
            c.mix, 0.0, 100.0, 1.0, " %", "mix",
        )
        self._lowcut_w = self._make_knob_spin(
            I18N.tr("audio.fx.low_cut", "Low Cut"),
            c.low_cut_hz, 20.0, 1000.0, 10.0, " Hz", "low_cut_hz",
        )
        for w in (self._time_w, self._feedback_w, self._mix_w, self._lowcut_w):
            knobs_layout.addWidget(w, 1)
        layout.addLayout(knobs_layout)

        # ping-pong 复选框
        pp_layout = QHBoxLayout()
        pp_layout.addStretch()
        self._pingpong_check = QCheckBox(I18N.tr("audio.fx.ping_pong", "Ping-Pong (stereo cross-feedback)"))
        self._pingpong_check.setChecked(c.ping_pong)
        self._pingpong_check.toggled.connect(self._on_pingpong_toggled)
        pp_layout.addWidget(self._pingpong_check)
        pp_layout.addStretch()
        layout.addLayout(pp_layout)

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
        self, label: str, value: float, minimum: float, maximum: float,
        step: float, suffix: str, param_key: str,
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
        dial_range = int(round((maximum - minimum) / step)) if step > 0 else 1
        dial.setMinimum(0)
        dial.setMaximum(max(dial_range, 1))
        dial.setValue(int(round((value - minimum) / step)) if step > 0 else 0)
        dial.setNotchesVisible(True)
        dial.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        spin = DragValue(
            "", min_v=minimum, max_v=maximum, default=value,
            decimals=0, suffix=suffix,
        )
        spin.setSingleStep(step)
        spin.setFixedValueWidth(100)

        def _on_dial_changed(dv: int) -> None:
            fv = minimum + dv * step
            spin.blockSignals(True)
            spin.setValue(fv)
            spin.blockSignals(False)
            self._on_param_changed(param_key, fv)

        def _on_spin_changed(fv: float) -> None:
            dial.blockSignals(True)
            dial.setValue(int(round((fv - minimum) / step)) if step > 0 else 0)
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

        container._drag = spin
        container._dial = dial
        container._minimum = minimum
        container._step = step
        return container

    # ────────────────────────── 事件处理 ──────────────────────────

    def _collect_params(self) -> DelayParams:
        return DelayParams(
            time_ms=float(self._time_w._drag.value()),
            feedback=float(self._feedback_w._drag.value()),
            mix=float(self._mix_w._drag.value()),
            low_cut_hz=float(self._lowcut_w._drag.value()),
            ping_pong=self._pingpong_check.isChecked(),
            enabled=self._enable_toggle.isChecked(),
        )

    def _on_param_changed(self, param_key: str, value: float) -> None:
        d = self._collect_params().to_dict()
        d[param_key] = value
        self._current = DelayParams.from_dict(d)
        if self._dragging["active"]:
            self.delay_preview.emit(self._track_id, self._current)
        else:
            self.delay_committed.emit(self._track_id, self._current)

    def _on_pingpong_toggled(self, checked: bool) -> None:
        d = self._collect_params().to_dict()
        d["ping_pong"] = checked
        self._current = DelayParams.from_dict(d)
        # 切换立即提交
        self.delay_committed.emit(self._track_id, self._current)

    def _on_enabled_toggled(self, checked: bool) -> None:
        """开启/关闭开关：立即提交（不走拖拽节流）。"""
        d = self._collect_params().to_dict()
        d["enabled"] = checked
        self._current = DelayParams.from_dict(d)
        self.delay_committed.emit(self._track_id, self._current)

    def _on_drag_released(self) -> None:
        if not self._dragging["active"]:
            return
        self._dragging["active"] = False
        self._current = self._collect_params()
        self.delay_committed.emit(self._track_id, self._current)

    def _on_preset_selected(self, combo_idx: int) -> None:
        if combo_idx <= 0:
            return
        params_data = self._preset_combo.itemData(combo_idx)
        if params_data is None:
            return
        d = dict(params_data)
        d["enabled"] = self._enable_toggle.isChecked()  # 保留当前开关状态
        self._current = DelayParams.from_dict(d)
        self._load_params_to_ui()
        self._preset_combo.setCurrentIndex(0)
        self.delay_committed.emit(self._track_id, self._current)

    def _load_params_to_ui(self) -> None:
        c = self._current
        # 回填开关（setChecked 不发 toggled，天然防回环）
        self._enable_toggle.setChecked(c.enabled)
        mapping = [
            (self._time_w, c.time_ms),
            (self._feedback_w, c.feedback),
            (self._mix_w, c.mix),
            (self._lowcut_w, c.low_cut_hz),
        ]
        for container, value in mapping:
            spin = container._drag
            dial = container._dial
            spin.blockSignals(True)
            dial.blockSignals(True)
            try:
                spin.setValue(value)
                minimum = container._minimum
                step = container._step
                dial.setValue(int(round((value - minimum) / step)) if step > 0 else 0)
            finally:
                spin.blockSignals(False)
                dial.blockSignals(False)
        self._pingpong_check.blockSignals(True)
        self._pingpong_check.setChecked(c.ping_pong)
        self._pingpong_check.blockSignals(False)

    def _on_reset(self) -> None:
        """重置为默认参数（保留当前开关状态）。"""
        was_enabled = self._enable_toggle.isChecked()
        self._current = DelayParams.default()
        d = self._current.to_dict()
        d["enabled"] = was_enabled
        self._current = DelayParams.from_dict(d)
        self._load_params_to_ui()
        self.delay_committed.emit(self._track_id, self._current)
