"""Spectral Mangler 对话框适配器（NF-02，计划 §6.3–§6.4/§8.1）。

契约同 VoiceChangerDialog：扁平 ``SpectralManglerParams`` ↔ Widget 嵌套
state 的唯一映射表；preview 节流 + 停手单次 commit；``set_current_params``
锁内静默回填（R06）；``update_telemetry`` 消费真实波形/颗粒/Freeze 指示
（NF-04；画布真实数据分支在 Widget 的 setTelemetry）。LIM 开关入状态
（计划 §6.2-3：原型 `_limiter` 私有状态 → ``output_limiter_enabled``）。
"""

from __future__ import annotations

import logging
import math
import copy
from typing import Any, Optional

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import QDialog, QVBoxLayout

from src.shared.ui.audio_mixer.spectral_mangler_widget import (
    StudioSpectralManglerWidget,
)

logger = logging.getLogger(__name__)

_SM_FIELD_MAP = {
    "enabled": (None, "enabled"),
    "routing": (None, "routing"),
    "global_mix_pct": (None, "global_mix"),
    "output_gain_db": (None, "output_gain"),
    "output_limiter_enabled": (None, "limiter"),
    "ring_enabled": ("ring", "enabled"),
    "ring_mode": ("ring", "mode"),
    "ring_carrier_hz": ("ring", "carrier"),
    "ring_lfo_rate_hz": ("ring", "lfo_rate"),
    "ring_lfo_depth_pct": ("ring", "lfo_depth"),
    "ring_warp_pct": ("ring", "warp"),
    "ring_spread_pct": ("ring", "spread"),
    "ring_mix_pct": ("ring", "mix"),
    "grain_enabled": ("grain", "enabled"),
    "grain_freeze": ("grain", "freeze"),
    "grain_mode": ("grain", "mode"),
    "grain_size_ms": ("grain", "size"),
    "grain_density_hz": ("grain", "density"),
    "grain_pitch_st": ("grain", "pitch"),
    "grain_pitch_jitter_pct": ("grain", "pitch_jitter"),
    "grain_position_pct": ("grain", "position"),
    "grain_spray_pct": ("grain", "spray"),
    "grain_feedback_pct": ("grain", "feedback"),
    "grain_mix_pct": ("grain", "mix"),
    "dist_enabled": ("dist", "enabled"),
    "dist_curve": ("dist", "curve"),
    "dist_drive_db": ("dist", "drive"),
    "dist_fold_pct": ("dist", "fold"),
    "dist_bias_pct": ("dist", "bias"),
    "dist_tone_hz": ("dist", "tone"),
    "dist_resonance_pct": ("dist", "resonance"),
    "dist_mix_pct": ("dist", "mix"),
}

# UI 显示文案 ↔ 稳定模型 id
_SM_ROUTING_TO_ID = {
    "Ring → Grain → Dist": "ring_grain_dist",
    "Grain → Ring → Dist": "grain_ring_dist",
    "Dist → Grain → Ring": "dist_grain_ring",
    "Parallel Blend": "parallel",
}
_SM_ROUTING_TO_UI = {v: k for k, v in _SM_ROUTING_TO_ID.items()}
_SM_ENUM_TO_ID = {
    "Sine": "sine", "Dual": "dual", "FM-Ring": "fm_ring",
    "Rectify": "rectify",
    "Cloud": "cloud", "Reverse": "reverse", "Scatter": "scatter",
    "Tape": "tape", "Fold": "fold", "Hard Clip": "hard_clip",
    "BitCrush": "bitcrush",
}
_SM_ID_TO_ENUM = {v: k for k, v in _SM_ENUM_TO_ID.items()}
_SM_ENUM_KEYS = ("ring_mode", "grain_mode", "dist_curve")


def ui_state_from_params(params: dict) -> dict:
    from src.features.edit.core.timeline_v2 import SpectralManglerParams
    p = SpectralManglerParams.from_dict(params or {})
    state: dict = {
        "enabled": p.enabled,
        "routing": _SM_ROUTING_TO_UI.get(p.routing, p.routing),
        "global_mix": p.global_mix_pct,
        "output_gain": p.output_gain_db,
        "limiter": p.output_limiter_enabled,
        "ring": {}, "grain": {}, "dist": {},
    }
    for flat, (section, ui_key) in _SM_FIELD_MAP.items():
        if section is None:
            continue
        value = getattr(p, flat)
        if flat in _SM_ENUM_KEYS:
            value = _SM_ID_TO_ENUM.get(value, value)
        state[section][ui_key] = value
    return state


def params_from_ui_state(state: dict) -> dict:
    st = state or {}
    flat: dict = {}
    for flat_key, (section, ui_key) in _SM_FIELD_MAP.items():
        if section is None:
            if ui_key == "routing":
                if "routing" in st:
                    flat[flat_key] = _SM_ROUTING_TO_ID.get(
                        st["routing"], st["routing"])
            elif ui_key in st:
                flat[flat_key] = st[ui_key]
            continue
        section_data = st.get(section) or {}
        if ui_key not in section_data:
            continue
        value = section_data[ui_key]
        if flat_key in _SM_ENUM_KEYS:
            value = _SM_ENUM_TO_ID.get(value, value)
        flat[flat_key] = value
    from src.features.edit.core.timeline_v2 import SpectralManglerParams
    return SpectralManglerParams.from_dict(flat).to_dict()


def _to_db(rms: float) -> float:
    return 20.0 * math.log10(max(float(rms), 1e-9))


class SpectralManglerDialog(QDialog):
    """Spectral Mangler 编辑窗口（用户原创 UI 的宿主适配）。"""

    spectral_mangler_preview = pyqtSignal(str, object)
    spectral_mangler_committed = pyqtSignal(str, object)

    def __init__(self, track_id: str, track_name: str, instance_id: str = "",
                 initial_params: Optional[dict] = None, parent=None,
                 sample_rate: float = 48000.0) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._instance_id = instance_id
        self._updating = False
        self._base_params = copy.deepcopy(initial_params or {})
        self._has_pending_edit = False
        self.setWindowTitle(f"Spectral Mangler — {track_name}")
        self.resize(1000, 690)

        self._widget = StudioSpectralManglerWidget(track_name, demo=False,
                                                   parent=self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._widget)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(60)
        self._preview_timer.timeout.connect(self._emit_preview)
        self._commit_timer = QTimer(self)
        self._commit_timer.setSingleShot(True)
        self._commit_timer.setInterval(400)
        self._commit_timer.timeout.connect(self._emit_commit)

        self._widget.stateChanged.connect(self._on_widget_state)
        self._updating = True
        try:
            self._widget.set_state(ui_state_from_params(initial_params or {}))
        finally:
            self._updating = False

    def _current_params(self) -> dict:
        edited = params_from_ui_state(self._widget.get_state())
        params = {**edited, **copy.deepcopy(self._base_params)}
        for key in _SM_FIELD_MAP:
            params[key] = edited[key]
        return params

    def _emit_preview(self) -> None:
        if not self._updating:
            self.spectral_mangler_preview.emit(
                self._track_id, self._current_params())

    def _emit_commit(self) -> None:
        if not self._updating:
            self._preview_timer.stop()
            self._commit_timer.stop()
            self._has_pending_edit = False
            params = self._current_params()
            self._base_params = copy.deepcopy(params)
            self.spectral_mangler_committed.emit(
                self._track_id, params)

    def _on_widget_state(self, _state: dict) -> None:
        if self._updating:
            return
        self._has_pending_edit = True
        if not self._preview_timer.isActive():
            self._preview_timer.start()
        self._commit_timer.start()

    def done(self, result: int) -> None:
        """Keep the final user edit before releasing this modeless editor."""
        if self._has_pending_edit:
            self._emit_commit()
        self._preview_timer.stop()
        self._commit_timer.stop()
        super().done(result)

    def set_current_params(self, params: Any) -> None:
        if params is None:
            return
        data = params.to_dict() if hasattr(params, "to_dict") else dict(params)
        self._preview_timer.stop()
        self._commit_timer.stop()
        self._has_pending_edit = False
        self._base_params = copy.deepcopy(data)
        self._updating = True
        try:
            self._widget.set_state(ui_state_from_params(data))
        finally:
            self._updating = False

    def update_sample_rate(self, sample_rate: float) -> None:
        """F02 采样率通知（占位）。"""

    def update_telemetry(self, telemetry: Optional[dict]) -> None:
        """NF-04：真实波形/模块活动/削波指示（demo 定时器自动停）。"""
        if not telemetry:
            self._widget.reset_telemetry()
            return
        wave = telemetry.get("waveform")
        grains = telemetry.get("grains")
        in_rms = telemetry.get("in_rms", 0.0)
        out_rms = telemetry.get("out_rms", 0.0)
        self._widget.update_telemetry(
            float(telemetry.get("input_peak_db", _to_db(in_rms) if in_rms else -60.0)),
            float(telemetry.get("output_peak_db", _to_db(out_rms) if out_rms else -60.0)),
            wave_samples=wave, grains=grains)
