"""Voice Changer 对话框适配器（NF-02，计划 §6.3–§6.4/§7.1）。

- 构造契约与其他 FX 对话框一致：
  ``(track_id, track_name, instance_id, initial_params, parent)``。
- 扁平 ``VoiceChangerParams`` ↔ 用户 Widget 嵌套 state 的**唯一映射表**
  在本文件 ``ui_state_from_params`` / ``params_from_ui_state``——
  UI／自动化／DSP 不各自猜字段。
- 手势边界（计划 §6.2-4）：控件连续变化 → 节流 preview；停手 400ms →
  恰好一次 committed（拖动合成单命令）。
- 外部回填 ``set_current_params``（R06 链路）：Params 校验后锁内
  set_state，不回发信号。
- 遥测入口 ``update_telemetry``（NF-04）：F0/置信度/模块活动；无可靠
  CLR 指标不伪造（计划 §6.2-6）。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import QDialog, QVBoxLayout

from src.shared.ui.audio_mixer.voice_changer_widget import (
    StudioVoiceChangerWidget,
)

logger = logging.getLogger(__name__)

# 扁平 Params 键 → (嵌套段, 嵌套键)。seed/版本为元数据，不入 UI state。
_VC_FIELD_MAP = {
    "enabled": (None, "enabled"),
    "output_gain_db": (None, "output_gain"),
    "formant_enabled": ("formant", "enabled"),
    "formant_mode": ("formant", "mode"),
    "formant_shift_st": ("formant", "shift"),
    "formant_throat_pct": ("formant", "throat"),
    "formant_gender_pct": ("formant", "gender"),
    "formant_preserve_pct": ("formant", "preserve"),
    "formant_timbre_pct": ("formant", "timbre"),
    "formant_mix_pct": ("formant", "mix"),
    "pitch_enabled": ("pitch", "enabled"),
    "pitch_tracking": ("pitch", "tracking"),
    "pitch_semitones": ("pitch", "semitones"),
    "pitch_fine_cents": ("pitch", "fine"),
    "pitch_formant_link_pct": ("pitch", "formant_link"),
    "pitch_glide_ms": ("pitch", "glide"),
    "pitch_jitter_pct": ("pitch", "jitter"),
    "pitch_mix_pct": ("pitch", "mix"),
    "vocoder_enabled": ("vocoder", "enabled"),
    "vocoder_carrier": ("vocoder", "carrier"),
    "vocoder_bands": ("vocoder", "bands"),
    "vocoder_carrier_hz": ("vocoder", "carrier_hz"),
    "vocoder_formant_st": ("vocoder", "formant"),
    "vocoder_unvoiced_pct": ("vocoder", "unvoiced"),
    "vocoder_release_ms": ("vocoder", "release"),
    "vocoder_mix_pct": ("vocoder", "mix"),
}

# UI 枚举显示值 ↔ 稳定模型 id（保存 id 不保存显示文案）
_VC_ENUM_TO_ID = {
    "Natural": "natural", "Crisp": "crisp",
    "Vocal": "vocal", "Wide": "wide",
    "Saw": "saw", "Pulse": "pulse", "Choir": "choir", "Noise": "noise",
}
_VC_ID_TO_ENUM = {v: k for k, v in _VC_ENUM_TO_ID.items()}
_VC_ENUM_KEYS = ("formant_mode", "pitch_tracking", "vocoder_carrier")


def ui_state_from_params(params: dict) -> dict:
    """扁平 VoiceChangerParams dict → Widget 嵌套 state（映射表唯一来源）。"""
    from src.features.edit.core.timeline_v2 import VoiceChangerParams
    p = VoiceChangerParams.from_dict(params or {})
    state: dict = {"enabled": p.enabled, "output_gain": p.output_gain_db,
                   "formant": {}, "pitch": {}, "vocoder": {}}
    for flat, (section, ui_key) in _VC_FIELD_MAP.items():
        if section is None:
            continue
        value = getattr(p, flat)
        if flat in _VC_ENUM_KEYS:
            value = _VC_ID_TO_ENUM.get(value, value)
        state[section][ui_key] = value
    return state


def params_from_ui_state(state: dict) -> dict:
    """Widget 嵌套 state → 扁平 VoiceChangerParams dict（经 Params 校验）。"""
    st = state or {}
    flat: dict = {}
    for flat_key, (section, ui_key) in _VC_FIELD_MAP.items():
        if section is None:
            if ui_key in st:
                flat[flat_key] = st[ui_key]
            continue
        section_data = st.get(section) or {}
        if ui_key not in section_data:
            continue
        value = section_data[ui_key]
        if flat_key in _VC_ENUM_KEYS:
            value = _VC_ENUM_TO_ID.get(value, value)
        flat[flat_key] = value
    from src.features.edit.core.timeline_v2 import VoiceChangerParams
    return VoiceChangerParams.from_dict(flat).to_dict()


def _to_db(rms: float) -> float:
    return 20.0 * math.log10(max(float(rms), 1e-9))


class VoiceChangerDialog(QDialog):
    """Voice Changer 编辑窗口（用户原创 UI 的宿主适配）。"""

    voice_changer_preview = pyqtSignal(str, object)
    voice_changer_committed = pyqtSignal(str, object)

    def __init__(self, track_id: str, track_name: str, instance_id: str = "",
                 initial_params: Optional[dict] = None, parent=None,
                 sample_rate: float = 48000.0) -> None:
        super().__init__(parent)
        self._track_id = track_id
        self._instance_id = instance_id
        self._updating = False
        self.setWindowTitle(f"Voice Changer — {track_name}")
        self.resize(980, 690)

        self._widget = StudioVoiceChangerWidget(track_name, demo=False,
                                                parent=self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._widget)

        # 手势 → preview（60ms 节流）/ commit（停手 400ms 单次）
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
        return params_from_ui_state(self._widget.get_state())

    def _emit_preview(self) -> None:
        if not self._updating:
            self.voice_changer_preview.emit(self._track_id,
                                            self._current_params())

    def _emit_commit(self) -> None:
        if not self._updating:
            self.voice_changer_committed.emit(self._track_id,
                                              self._current_params())

    def _on_widget_state(self, _state: dict) -> None:
        if self._updating:
            return
        self._preview_timer.start()
        self._commit_timer.start()

    # ── 外部回填（R06：timeline/undo/preset 同步；锁内静默） ──
    def set_current_params(self, params: Any) -> None:
        if params is None:
            return
        data = params.to_dict() if hasattr(params, "to_dict") else dict(params)
        self._updating = True
        try:
            self._widget.set_state(ui_state_from_params(data))
        finally:
            self._updating = False

    def update_sample_rate(self, sample_rate: float) -> None:
        """F02 采样率通知（本效果 UI 无采样率相关曲线；占位）。"""

    # ── 遥测（NF-04）──
    def update_telemetry(self, telemetry: Optional[dict]) -> None:
        if not telemetry:
            return
        freqs = telemetry.get("spectrum_hz")
        in_spec = telemetry.get("input_spectrum_db")
        out_spec = telemetry.get("output_spectrum_db")
        f0_in = telemetry.get("f0_in_hz")
        f0_out = telemetry.get("f0_out_hz")
        conf = float(telemetry.get("confidence", 0.0) or 0.0)
        if freqs and in_spec and out_spec:
            self._widget.update_analysis(
                freqs, in_spec, out_spec,
                float(f0_in) if f0_in else 0.0,
                float(f0_out) if f0_out else 0.0,
                clarity_pct=max(0.0, min(100.0, conf * 100.0)))
        in_rms = telemetry.get("in_rms", 0.0)
        out_rms = telemetry.get("out_rms", 0.0)
        # CLR 不沿用原型合成百分比（§6.2-6）——voiced=false 时 0（N/A）
        self._widget.update_meters(
            _to_db(in_rms) if in_rms else -60.0,
            _to_db(out_rms) if out_rms else -60.0,
            formant_activity=100.0 if telemetry.get("formant_active") else 0.0,
            pitch_activity=100.0 if telemetry.get("pitch_active") else 0.0,
            vocoder_activity=100.0 if telemetry.get("vocoder_active") else 0.0,
            clarity_pct=0.0,
        )
