"""
Reverb Dialog — 混响器参数编辑对话框（Schroeder FDN）。

点击 mixer 内嵌效果器列表中的 Reverb 条目时弹出。包含：
- 湿信号比例（mix, 0..100 %）
- 混响时间（decay, 0.1..10 s，RT60）
- 预延迟（pre_delay_ms, 0..200 ms）
- 高频阻尼（damping_hz, 1000..20000 Hz）
- 预设下拉（默认/小房间/大音乐厅/大教堂/板式/弹簧）
- 三段式节流：拖动期 emit reverb_preview（实时听感），松手 emit reverb_committed（命令）

移植自 audio_pro 的 Reverb 参数。

信号链：
  对话框 → TrackMixerWidget._on_reverb_edited → track_effects_preview/committed
"""

from __future__ import annotations

from typing import Optional, Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QDial,
    QPushButton, QSizePolicy, QWidget,
)

from src.features.edit.core.timeline_v2 import ReverbParams
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.ui.style_sheet import PANEL_TITLE_LABEL_STYLE, PARAM_LABEL_STYLE


# 混响预设（移植自 audio_pro PresetManager.presets.reverb）
REVERB_PRESETS: list[dict[str, Any]] = [
    {"name": "默认",     "params": {"mix": 30.0, "decay": 2.0,  "pre_delay_ms": 0.0,  "damping_hz": 5000.0}},
    {"name": "小房间",   "params": {"mix": 20.0, "decay": 0.8,  "pre_delay_ms": 5.0,  "damping_hz": 7000.0}},
    {"name": "大音乐厅", "params": {"mix": 45.0, "decay": 4.0,  "pre_delay_ms": 30.0, "damping_hz": 4000.0}},
    {"name": "大教堂",   "params": {"mix": 60.0, "decay": 8.0,  "pre_delay_ms": 60.0, "damping_hz": 2500.0}},
    {"name": "板式混响", "params": {"mix": 35.0, "decay": 2.5,  "pre_delay_ms": 0.0,  "damping_hz": 6000.0}},
    {"name": "弹簧混响", "params": {"mix": 40.0, "decay": 1.5,  "pre_delay_ms": 0.0,  "damping_hz": 3500.0}},
]


class ReverbDialog(QDialog):
    """混响器参数编辑对话框（4 个旋钮 + 预设）。"""

    # 信号（track_id, ReverbParams）
    reverb_preview = pyqtSignal(str, object)    # 拖动期实时听感
    reverb_committed = pyqtSignal(str, object)  # 松手/关闭，进命令栈

    def __init__(
        self,
        track_id: str,
        track_name: str,
        instance_id: str,
        initial_params: Optional[ReverbParams] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._instance_id = instance_id
        self._current: ReverbParams = initial_params or ReverbParams.default()
        self._dragging = {"active": False}

        self.setWindowTitle(I18N.tr("audio.fx.reverb", "Reverb") + f" - {track_name or track_id}")
        self.setModal(False)
        self.setMinimumSize(480, 240)

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
        title = QLabel(I18N.tr("audio.fx.reverb", "Reverb"))
        title.setStyleSheet(PANEL_TITLE_LABEL_STYLE)
        top.addWidget(title)
        top.addStretch()
        top.addWidget(QLabel(I18N.tr("audio.fx.preset", "Preset")))
        self._preset_combo = ChevronComboBox()
        self._preset_combo.addItem("—", None)
        for p in REVERB_PRESETS:
            self._preset_combo.addItem(p["name"], p["params"])
        self._preset_combo.activated.connect(self._on_preset_selected)
        top.addWidget(self._preset_combo)
        layout.addLayout(top)

        # 4 个旋钮
        knobs_layout = QHBoxLayout()
        knobs_layout.setSpacing(8)
        c = self._current
        self._mix_w = self._make_knob_spin(
            I18N.tr("audio.fx.mix", "Mix"),
            c.mix, 0.0, 100.0, 1.0, " %", "mix",
        )
        self._decay_w = self._make_knob_spin(
            I18N.tr("audio.fx.decay", "Decay"),
            c.decay, 0.1, 10.0, 0.1, " s", "decay",
        )
        self._predelay_w = self._make_knob_spin(
            I18N.tr("audio.fx.pre_delay", "Pre-Delay"),
            c.pre_delay_ms, 0.0, 200.0, 1.0, " ms", "pre_delay_ms",
        )
        self._damping_w = self._make_knob_spin(
            I18N.tr("audio.fx.damping", "Damping"),
            c.damping_hz, 1000.0, 20000.0, 100.0, " Hz", "damping_hz",
        )
        for w in (self._mix_w, self._decay_w, self._predelay_w, self._damping_w):
            knobs_layout.addWidget(w, 1)
        layout.addLayout(knobs_layout)

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
            decimals=(1 if step < 1 else 0), suffix=suffix,
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

    def _collect_params(self) -> ReverbParams:
        return ReverbParams(
            mix=float(self._mix_w._drag.value()),
            decay=float(self._decay_w._drag.value()),
            pre_delay_ms=float(self._predelay_w._drag.value()),
            damping_hz=float(self._damping_w._drag.value()),
            enabled=self._enable_toggle.isChecked(),
        )

    def _on_param_changed(self, param_key: str, value: float) -> None:
        d = self._collect_params().to_dict()
        d[param_key] = value
        self._current = ReverbParams.from_dict(d)
        if self._dragging["active"]:
            self.reverb_preview.emit(self._track_id, self._current)
        else:
            self.reverb_committed.emit(self._track_id, self._current)

    def _on_enabled_toggled(self, checked: bool) -> None:
        """开启/关闭开关：立即提交（不走拖拽节流）。"""
        d = self._collect_params().to_dict()
        d["enabled"] = checked
        self._current = ReverbParams.from_dict(d)
        self.reverb_committed.emit(self._track_id, self._current)

    def _on_drag_released(self) -> None:
        if not self._dragging["active"]:
            return
        self._dragging["active"] = False
        self._current = self._collect_params()
        self.reverb_committed.emit(self._track_id, self._current)

    def _on_preset_selected(self, combo_idx: int) -> None:
        if combo_idx <= 0:
            return
        params_data = self._preset_combo.itemData(combo_idx)
        if params_data is None:
            return
        d = dict(params_data)
        d["enabled"] = self._enable_toggle.isChecked()  # 保留当前开关状态
        self._current = ReverbParams.from_dict(d)
        self._load_params_to_ui()
        self._preset_combo.setCurrentIndex(0)
        self.reverb_committed.emit(self._track_id, self._current)

    def _load_params_to_ui(self) -> None:
        c = self._current
        # 回填开关（setChecked 不发 toggled，天然防回环）
        self._enable_toggle.setChecked(c.enabled)
        mapping = [
            (self._mix_w, c.mix),
            (self._decay_w, c.decay),
            (self._predelay_w, c.pre_delay_ms),
            (self._damping_w, c.damping_hz),
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

    def _on_reset(self) -> None:
        """重置为默认参数（保留当前开关状态）。"""
        was_enabled = self._enable_toggle.isChecked()
        self._current = ReverbParams.default()
        d = self._current.to_dict()
        d["enabled"] = was_enabled
        self._current = ReverbParams.from_dict(d)
        self._load_params_to_ui()
        self.reverb_committed.emit(self._track_id, self._current)
