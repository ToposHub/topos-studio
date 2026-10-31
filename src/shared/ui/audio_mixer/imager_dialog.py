"""
Imager Dialog — 立体声成像器参数编辑对话框（Mid/Side width + Pan）。

点击 mixer 内嵌效果器列表中的 Imager 条目时弹出。包含：
- 立体声宽度（width, 0..200 %，0=mono, 100=normal, 200=超宽）
- 声像（pan, -100..100，-100=全左, 0=中, 100=全右）
- 预设下拉（Mono/Narrow/Normal/Wide/Super Wide/偏左/偏右）
- 三段式节流：拖动期 emit imager_preview（实时听感），松手 emit imager_committed（命令）

移植自 audio_pro 的 Imager 参数。

信号链：
  对话框 → TrackMixerWidget._on_imager_edited → track_effects_preview/committed
"""

from __future__ import annotations

from typing import Optional, Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QDial,
    QPushButton, QSizePolicy, QWidget,
)

from src.features.edit.core.timeline_v2 import ImagerParams
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.ui.style_sheet import PANEL_TITLE_LABEL_STYLE, PARAM_LABEL_STYLE


# Imager 预设（audio_pro 原版无预设，此处按常见成像设置自定义）
IMAGER_PRESETS: list[dict[str, Any]] = [
    {"name": "Mono",      "params": {"width": 0.0,   "pan": 0.0}},
    {"name": "窄",        "params": {"width": 60.0,  "pan": 0.0}},
    {"name": "正常",      "params": {"width": 100.0, "pan": 0.0}},
    {"name": "宽",        "params": {"width": 140.0, "pan": 0.0}},
    {"name": "超宽",      "params": {"width": 180.0, "pan": 0.0}},
    {"name": "偏左",      "params": {"width": 100.0, "pan": -40.0}},
    {"name": "偏右",      "params": {"width": 100.0, "pan": 40.0}},
]


class ImagerDialog(QDialog):
    """立体声成像器参数编辑对话框（width + pan 旋钮 + 预设）。"""

    # 信号（track_id, ImagerParams）
    imager_preview = pyqtSignal(str, object)    # 拖动期实时听感
    imager_committed = pyqtSignal(str, object)  # 松手/关闭，进命令栈

    def __init__(
        self,
        track_id: str,
        track_name: str,
        instance_id: str,
        initial_params: Optional[ImagerParams] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._instance_id = instance_id
        self._current: ImagerParams = initial_params or ImagerParams.default()
        self._dragging = {"active": False}

        self.setWindowTitle(I18N.tr("audio.fx.imager", "Imager") + f" - {track_name or track_id}")
        self.setModal(False)
        self.setMinimumSize(420, 240)

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
        title = QLabel(I18N.tr("audio.fx.imager", "Imager"))
        title.setStyleSheet(PANEL_TITLE_LABEL_STYLE)
        top.addWidget(title)
        top.addStretch()
        top.addWidget(QLabel(I18N.tr("audio.fx.preset", "Preset")))
        self._preset_combo = ChevronComboBox()
        self._preset_combo.addItem("—", None)
        for p in IMAGER_PRESETS:
            self._preset_combo.addItem(p["name"], p["params"])
        self._preset_combo.activated.connect(self._on_preset_selected)
        top.addWidget(self._preset_combo)
        layout.addLayout(top)

        # 2 个旋钮
        knobs_layout = QHBoxLayout()
        knobs_layout.setSpacing(16)
        c = self._current
        self._width_w = self._make_knob_spin(
            I18N.tr("audio.fx.width", "Width"),
            c.width, 0.0, 200.0, 1.0, " %", "width",
        )
        self._pan_w = self._make_knob_spin(
            I18N.tr("audio.fx.pan", "Pan"),
            c.pan, -100.0, 100.0, 1.0, "", "pan",
        )
        knobs_layout.addWidget(self._width_w, 1)
        knobs_layout.addWidget(self._pan_w, 1)
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
        dial.setFixedSize(64, 64)
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

    def _collect_params(self) -> ImagerParams:
        return ImagerParams(
            width=float(self._width_w._drag.value()),
            pan=float(self._pan_w._drag.value()),
            enabled=self._enable_toggle.isChecked(),
        )

    def _on_param_changed(self, param_key: str, value: float) -> None:
        d = self._collect_params().to_dict()
        d[param_key] = value
        self._current = ImagerParams.from_dict(d)
        if self._dragging["active"]:
            self.imager_preview.emit(self._track_id, self._current)
        else:
            self.imager_committed.emit(self._track_id, self._current)

    def _on_enabled_toggled(self, checked: bool) -> None:
        """开启/关闭开关：立即提交（不走拖拽节流）。"""
        d = self._collect_params().to_dict()
        d["enabled"] = checked
        self._current = ImagerParams.from_dict(d)
        self.imager_committed.emit(self._track_id, self._current)

    def _on_drag_released(self) -> None:
        if not self._dragging["active"]:
            return
        self._dragging["active"] = False
        self._current = self._collect_params()
        self.imager_committed.emit(self._track_id, self._current)

    def _on_preset_selected(self, combo_idx: int) -> None:
        if combo_idx <= 0:
            return
        params_data = self._preset_combo.itemData(combo_idx)
        if params_data is None:
            return
        d = dict(params_data)
        d["enabled"] = self._enable_toggle.isChecked()  # 保留当前开关状态
        self._current = ImagerParams.from_dict(d)
        self._load_params_to_ui()
        self._preset_combo.setCurrentIndex(0)
        self.imager_committed.emit(self._track_id, self._current)

    def _load_params_to_ui(self) -> None:
        c = self._current
        # 回填开关（setChecked 不发 toggled，天然防回环）
        self._enable_toggle.setChecked(c.enabled)
        for container, value in ((self._width_w, c.width), (self._pan_w, c.pan)):
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
        self._current = ImagerParams.default()
        d = self._current.to_dict()
        d["enabled"] = was_enabled
        self._current = ImagerParams.from_dict(d)
        self._load_params_to_ui()
        self.imager_committed.emit(self._track_id, self._current)
