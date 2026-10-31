"""
Dynamics Dialog — 动态处理器子面板（Compressor + Noise Gate + Limiter）。

点击 mixer 的 DY 胶囊时弹出。用 QTabWidget 整合三种动态效果器：
- Compressor tab：6 旋钮（threshold/ratio/attack/release/knee/makeup）+ 预设
- Noise Gate tab：3 旋钮（threshold/attack/release）+ 预设
- Limiter tab：2 旋钮（threshold/release）

每种动态效果器在轨道 effect_chain 中是单例（最多 1 个实例）。
面板打开时从 chain 找对应实例（找不到则用默认参数显示，但不自动创建——
仅在用户实际调参时才提交到 chain）。

三段式节流：旋钮拖动期发 dynamics_preview（实时听感），松手发 dynamics_committed（命令）。

信号链：
  对话框 → TrackMixerWidget._on_dynamics_edited → track_effects_preview/committed
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QDial,
    QPushButton, QSizePolicy, QWidget,
    QTabWidget,
)

from src.features.edit.core.timeline_v2 import (
    EffectChainParams, TrackEffectInstance,
    CompressorParams, NoiseGateParams, LimiterParams,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.widgets.toggle_switch import ToggleSwitch
from src.shared.ui.style_sheet import PANEL_TITLE_LABEL_STYLE, PARAM_LABEL_STYLE


# 动态效果器类型（chain 中的 effect_type）
_DYN_COMPRESSOR = "compressor"
_DYN_NOISE_GATE = "noise_gate"
_DYN_LIMITER = "limiter"


# ─────────────────────────────────────────────────────────────────────────────
# 旋钮控件工厂（dial + DragValue 联动，统一项目数值框样式）
# ─────────────────────────────────────────────────────────────────────────────

def _make_knob(
    label: str, value: float, minimum: float, maximum: float,
    step: float, suffix: str, on_changed, on_drag_pressed, on_drag_released,
) -> QWidget:
    """创建 dial + DragValue 联动控件。

    Args:
        on_changed: 回调 (float)，参数变化时调用
        on_drag_pressed: 回调 ()，拖动开始
        on_drag_released: 回调 ()，拖动结束
    """
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
    spin.setFixedValueWidth(90)

    def _on_dial_changed(dv: int) -> None:
        fv = minimum + dv * step
        spin.blockSignals(True)
        spin.setValue(fv)
        spin.blockSignals(False)
        on_changed(fv)

    def _on_spin_changed(fv: float) -> None:
        dial.blockSignals(True)
        dial.setValue(int(round((fv - minimum) / step)) if step > 0 else 0)
        dial.blockSignals(False)
        on_changed(fv)

    dial.valueChanged.connect(_on_dial_changed)
    spin.valueChanged.connect(_on_spin_changed)
    # 三段式节流：dial 拖拽 + DragValue 拖拽都走 preview→committed
    dial.sliderPressed.connect(on_drag_pressed)
    dial.sliderReleased.connect(on_drag_released)
    spin.interaction_started.connect(on_drag_pressed)
    spin.interaction_ended.connect(on_drag_released)

    lay.addWidget(dial, 0, Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(spin, 0, Qt.AlignmentFlag.AlignCenter)

    # 把控件引用存到 container 上，便于程序化设值
    # （_spin 实为 DragValue，与 QDoubleSpinBox 的 value()/setValue()/blockSignals() 兼容）
    container._dial = dial
    container._spin = spin
    container._minimum = minimum
    container._step = step
    return container


def _set_knob_value(container: QWidget, value: float) -> None:
    """程序化设置 knob 值（blockSignals 防回环）。"""
    spin = container._spin
    dial = container._dial
    minimum = container._minimum
    step = container._step
    spin.blockSignals(True)
    dial.blockSignals(True)
    try:
        spin.setValue(value)
        dial.setValue(int(round((value - minimum) / step)) if step > 0 else 0)
    finally:
        spin.blockSignals(False)
        dial.blockSignals(False)


# ─────────────────────────────────────────────────────────────────────────────
# 单个动态 tab（Compressor / Noise Gate / Limiter 通用）
# ─────────────────────────────────────────────────────────────────────────────

class _DynamicsTab(QWidget):
    """单个动态效果器的 tab 页面。

    持有一组旋钮控件 + 预设下拉。参数变化时通过 on_params_changed 回调通知父对话框。
    """

    def __init__(
        self,
        effect_type: str,
        param_fields: list,  # [(label, key, minimum, maximum, step, suffix), ...]
        presets: list,       # [{"name", "params": {key: value, ...}}, ...]
        initial_params,      # XxxParams 或 None
        on_params_changed,   # 回调 (effect_type, params_dict, commit: bool)
        on_drag_pressed,
        on_drag_released,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._effect_type = effect_type
        self._param_fields = param_fields
        self._on_params_changed = on_params_changed
        self._on_drag_pressed = on_drag_pressed
        self._on_drag_released = on_drag_released
        self._dragging = False
        # 当前参数（dict 形式）
        self._params: dict = self._params_to_dict(initial_params)
        # 旋钮控件引用 {key: container}
        self._knobs: dict = {}

        self._init_ui(presets)

    @staticmethod
    def _params_to_dict(params) -> dict:
        """把 XxxParams 转 dict（None 用默认）。"""
        if params is None:
            return {}
        return params.to_dict()

    def _init_ui(self, presets: list) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # 开启/关闭开关（始终显示，独立一行）
        enable_row = QHBoxLayout()
        enable_row.setContentsMargins(0, 0, 0, 0)
        self._enable_toggle = ToggleSwitch(checked=bool(self._params.get("enabled", True)))
        self._enable_toggle.toggled.connect(self._on_enable_toggled)
        enable_row.addWidget(self._enable_toggle)
        enable_row.addStretch()
        layout.addLayout(enable_row)

        # 预设下拉
        if presets:
            top = QHBoxLayout()
            top.addStretch()
            top.addWidget(QLabel(I18N.tr("audio.fx.preset", "Preset")))
            self._preset_combo = ChevronComboBox()
            self._preset_combo.addItem("—", None)
            for p in presets:
                self._preset_combo.addItem(p["name"], p["params"])
            self._preset_combo.activated.connect(self._on_preset_selected)
            top.addWidget(self._preset_combo)
            top.addStretch()
            layout.addLayout(top)
        else:
            self._preset_combo = None
            layout.addStretch()

        # 旋钮行
        knobs_layout = QHBoxLayout()
        knobs_layout.setSpacing(8)
        for (label, key, minimum, maximum, step, suffix) in self._param_fields:
            value = float(self._params.get(key, minimum))
            knob = _make_knob(
                label, value, minimum, maximum, step, suffix,
                on_changed=lambda fv, _k=key: self._on_knob_changed(_k, fv),
                on_drag_pressed=self._on_drag_pressed_cb,
                on_drag_released=self._on_drag_released_cb,
            )
            self._knobs[key] = knob
            knobs_layout.addWidget(knob, 1)
        layout.addLayout(knobs_layout)
        layout.addStretch()

    def _on_knob_changed(self, key: str, value: float) -> None:
        """某个旋钮变化。"""
        self._params[key] = value
        self._on_params_changed(self._effect_type, dict(self._params), commit=not self._dragging)

    def _on_enable_toggled(self, checked: bool) -> None:
        """开启/关闭开关：立即提交（不走拖拽节流）。"""
        self._params["enabled"] = checked
        self._on_params_changed(self._effect_type, dict(self._params), commit=True)

    def _on_drag_pressed_cb(self) -> None:
        self._dragging = True
        self._on_drag_pressed()

    def _on_drag_released_cb(self) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self._on_drag_released()

    def _on_preset_selected(self, combo_idx: int) -> None:
        if self._preset_combo is None or combo_idx <= 0:
            return
        params_data = self._preset_combo.itemData(combo_idx)
        if params_data is None:
            return
        # 应用预设：合并到当前 params（保留 enabled）
        enabled = self._params.get("enabled", True)
        self._params = dict(params_data)
        self._params["enabled"] = enabled
        # 更新所有旋钮显示
        for (label, key, minimum, maximum, step, suffix) in self._param_fields:
            if key in self._params:
                _set_knob_value(self._knobs[key], float(self._params[key]))
        self._preset_combo.setCurrentIndex(0)  # 复位
        self._on_params_changed(self._effect_type, dict(self._params), commit=True)

    def load_params(self, params) -> None:
        """外部回填参数（程序化更新旋钮 + 开关，不发信号）。"""
        self._params = self._params_to_dict(params) if params is not None else self._params
        for (label, key, minimum, maximum, step, suffix) in self._param_fields:
            if key in self._params:
                _set_knob_value(self._knobs[key], float(self._params[key]))
        # 回填开关（setChecked 不发 toggled，天然防回环）
        self._enable_toggle.setChecked(bool(self._params.get("enabled", True)))


# ─────────────────────────────────────────────────────────────────────────────
# DynamicsDialog 主面板
# ─────────────────────────────────────────────────────────────────────────────

class DynamicsDialog(QDialog):
    """动态处理器子面板（Compressor + Noise Gate + Limiter 三 tab）。

    信号：
        dynamics_preview(str, EffectChainParams)   — 拖动期实时听感
        dynamics_committed(str, EffectChainParams) — 松手/预设/关闭时提交（命令栈）
    """

    dynamics_preview = pyqtSignal(str, object)
    dynamics_committed = pyqtSignal(str, object)

    def __init__(
        self,
        track_id: str,
        track_name: str,
        chain: Optional[EffectChainParams],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._track_name = track_name
        self._chain = chain  # 当前 effect_chain（只读快照，编辑时由父组件回填）
        self._dragging = False

        self.setWindowTitle(I18N.tr("audio.fx.dynamics", "Dynamics") + f" - {track_name or track_id}")
        self.setModal(False)
        self.setMinimumSize(600, 320)

        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # 标题
        title = QLabel(I18N.tr("audio.fx.dynamics", "Dynamics"))
        title.setStyleSheet(PANEL_TITLE_LABEL_STYLE)
        layout.addWidget(title)

        # Tab 页
        self._tabs = QTabWidget()

        # 从 chain 找各动态效果的当前实例；找不到时用类默认参数显示
        # （AUD-37706: 此前传 None → tab 内取范围最小值显示，与模块
        # docstring "找不到则用默认参数显示" 的承诺相反）
        comp_inst = self._find_effect(_DYN_COMPRESSOR)
        ng_inst = self._find_effect(_DYN_NOISE_GATE)
        lim_inst = self._find_effect(_DYN_LIMITER)

        # Compressor tab
        from src.shared.ui.audio_mixer.compressor_dialog import COMPRESSOR_PRESETS
        comp_params = CompressorParams.from_dict(comp_inst.params) if comp_inst else CompressorParams()
        self._comp_tab = _DynamicsTab(
            effect_type=_DYN_COMPRESSOR,
            param_fields=[
                (I18N.tr("audio.fx.threshold", "Threshold"), "threshold_db", -60.0, 0.0, 0.5, " dB"),
                (I18N.tr("audio.fx.ratio", "Ratio"), "ratio", 1.0, 20.0, 0.5, ":1"),
                (I18N.tr("audio.fx.attack", "Attack"), "attack_ms", 0.0, 1000.0, 1.0, " ms"),
                (I18N.tr("audio.fx.release", "Release"), "release_ms", 10.0, 5000.0, 10.0, " ms"),
                (I18N.tr("audio.fx.knee", "Knee"), "knee_db", 0.0, 40.0, 1.0, " dB"),
                (I18N.tr("audio.fx.makeup", "Makeup"), "makeup_gain", 0.0, 24.0, 0.5, " dB"),
            ],
            presets=COMPRESSOR_PRESETS,
            initial_params=comp_params,
            on_params_changed=self._on_tab_params_changed,
            on_drag_pressed=self._on_drag_pressed,
            on_drag_released=self._on_drag_released,
        )
        self._tabs.addTab(self._comp_tab, I18N.tr("audio.fx.compressor", "Compressor"))

        # Noise Gate tab
        from src.shared.ui.audio_mixer.noise_gate_dialog import NOISE_GATE_PRESETS
        ng_params = NoiseGateParams.from_dict(ng_inst.params) if ng_inst else NoiseGateParams()
        self._ng_tab = _DynamicsTab(
            effect_type=_DYN_NOISE_GATE,
            param_fields=[
                (I18N.tr("audio.fx.threshold", "Threshold"), "threshold_db", -60.0, 0.0, 0.5, " dB"),
                (I18N.tr("audio.fx.attack", "Attack"), "attack_ms", 0.0, 50.0, 0.5, " ms"),
                (I18N.tr("audio.fx.release", "Release"), "release_ms", 10.0, 1000.0, 10.0, " ms"),
            ],
            presets=NOISE_GATE_PRESETS,
            initial_params=ng_params,
            on_params_changed=self._on_tab_params_changed,
            on_drag_pressed=self._on_drag_pressed,
            on_drag_released=self._on_drag_released,
        )
        self._tabs.addTab(self._ng_tab, I18N.tr("audio.fx.noise_gate", "Noise Gate"))

        # Limiter tab（无预设）
        lim_params = LimiterParams.from_dict(lim_inst.params) if lim_inst else LimiterParams()
        self._lim_tab = _DynamicsTab(
            effect_type=_DYN_LIMITER,
            param_fields=[
                (I18N.tr("audio.fx.threshold", "Threshold"), "threshold_db", -20.0, 0.0, 0.5, " dB"),
                (I18N.tr("audio.fx.release", "Release"), "release_ms", 10.0, 1000.0, 10.0, " ms"),
            ],
            presets=[],  # Limiter 无预设
            initial_params=lim_params,
            on_params_changed=self._on_tab_params_changed,
            on_drag_pressed=self._on_drag_pressed,
            on_drag_released=self._on_drag_released,
        )
        self._tabs.addTab(self._lim_tab, I18N.tr("audio.fx.limiter", "Limiter"))

        layout.addWidget(self._tabs, 1)

        # 底部按钮
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        close_btn = QPushButton(I18N.tr("common.close", "Close"))
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    # ─── chain 查找 ───

    def _find_effect(self, effect_type: str) -> Optional[TrackEffectInstance]:
        """从当前 chain 找 DY 管理的实例（panel="dynamics"，取第一个）。

        通过 panel 区分 FX 列表里同类型的实例，避免误命中用户在 FX 列表单独添加的
        compressor/noise_gate/limiter。
        """
        if self._chain is None:
            return None
        return next(
            (e for e in self._chain.effects
             if e.effect_type == effect_type and getattr(e, "panel", "fx") == "dynamics"),
            None,
        )

    # ─── 参数变化处理 ───

    def _on_tab_params_changed(self, effect_type: str, params_dict: dict, commit: bool) -> None:
        """某个 tab 的参数变化：更新 chain 中 DY 管理的实例 + emit 信号。

        通过 panel="dynamics" 匹配 DY 自己的实例（不误碰 FX 列表里同类型实例）。
        若 chain 中没有 DY 管理的该类型实例，自动创建一个（追加到链尾，标记 panel="dynamics"）。
        """
        from src.features.edit.core.timeline_v2 import TrackEffectInstance
        effects = list(self._chain.effects) if self._chain is not None else []
        # 找 DY 管理的现有实例（panel="dynamics"）
        idx = next(
            (i for i, e in enumerate(effects)
             if e.effect_type == effect_type and getattr(e, "panel", "fx") == "dynamics"),
            -1,
        )
        if idx >= 0:
            old = effects[idx]
            effects[idx] = TrackEffectInstance(
                effect_type=effect_type, params=dict(params_dict),
                enabled=old.enabled, order=old.order, instance_id=old.instance_id,
                panel="dynamics",
            )
        else:
            # 自动创建（追加到链尾，DY 管理单例）
            new_order = max((e.order for e in effects), default=-1) + 1
            effects.append(TrackEffectInstance(
                effect_type=effect_type, params=dict(params_dict),
                enabled=True, order=new_order, panel="dynamics",
            ))
        self._chain = EffectChainParams(effects=tuple(effects))
        if commit:
            self.dynamics_committed.emit(self._track_id, self._chain)
        else:
            self.dynamics_preview.emit(self._track_id, self._chain)

    def _on_drag_pressed(self) -> None:
        self._dragging = True

    def _on_drag_released(self) -> None:
        if not self._dragging:
            return
        self._dragging = False
        # 松手提交（chain 已在 _on_tab_params_changed 中更新）
        self.dynamics_committed.emit(self._track_id, self._chain)

    def update_chain(self, chain: Optional[EffectChainParams]) -> None:
        """外部回填 chain（timeline 变化后同步显示，blockSignals 防回环）。"""
        self._chain = chain
        # 更新各 tab 显示（不触发信号）
        for effect_type, tab in (
            (_DYN_COMPRESSOR, self._comp_tab),
            (_DYN_NOISE_GATE, self._ng_tab),
            (_DYN_LIMITER, self._lim_tab),
        ):
            inst = self._find_effect(effect_type)
            tab.load_params(_params_from_inst(effect_type, inst))


def _params_from_inst(effect_type: str, inst: Optional[TrackEffectInstance]):
    """从 TrackEffectInstance 取出对应的 XxxParams（供 tab.load_params）。"""
    if inst is None or not inst.params:
        return None
    if effect_type == _DYN_COMPRESSOR:
        return CompressorParams.from_dict(inst.params)
    elif effect_type == _DYN_NOISE_GATE:
        return NoiseGateParams.from_dict(inst.params)
    elif effect_type == _DYN_LIMITER:
        return LimiterParams.from_dict(inst.params)
    return None
