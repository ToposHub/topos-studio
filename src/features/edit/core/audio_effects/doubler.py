"""P2 · 加倍/和声（FX-20 · 2026-11-21 计划）。

第一版：片段预渲染的**固定音程双声**（长度保持）：

- 每声部：``interval_st + detune_cents/100`` 调一次既有保时长变调
  （``audio_timepitch.pitch_shift``，总偏移须落在
  ``SUPPORTED_SEMITONES``）；``delay_ms`` 作创作延迟（整数样本，
  前置补零）；等功率声像（pan_pct −100..+100 → ±45°）；dB 增益；
- 全局：``wet_mix_pct``（0..100，湿声组整体电平）+ ``dry_gain_db``
  + ``output_gain_db``；Solo/Mute 属宿主语义（本函数消费 mute）；
- 声部叠加不做重归一化（计划：固定参照增益，两个相同声部峰值可超
  满幅，实际峰值由调用方控制）；
- 增删一个声部不影响其余声部的参照增益与随机序列。
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

from src.features.edit.core.audio_timepitch import (
    SUPPORTED_SEMITONES,
    pitch_shift_fixed_length,
)


@dataclass(frozen=True)
class DoublerVoice:
    """一个效果声部（稳定 ID 由调用方维护；本函数按顺序消费）。"""

    interval_st: float = 0.0
    detune_cents: float = 0.0
    delay_ms: float = 0.0
    pan_pct: float = 0.0
    gain_db: float = 0.0
    mute: bool = False
    enabled: bool = True


def _equal_power_pan(pan_pct: float) -> Tuple[float, float]:
    """pan −100..+100 → 等功率 (L, R)（+100 = 全右）。"""
    pan = float(np.clip(pan_pct, -100.0, 100.0)) / 100.0
    angle = (pan + 1.0) * np.pi / 4.0
    return float(np.cos(angle)), float(np.sin(angle))


def render_doubler(
    pcm: np.ndarray,
    sample_rate: int,
    voices: Sequence[DoublerVoice],
    *,
    dry_gain_db: float = 0.0,
    wet_mix_pct: float = 100.0,
    output_gain_db: float = 0.0,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> np.ndarray:
    """固定音程双声渲染（长度保持；不修改输入）。

    返回 (channels, n) float32：``dry·dry_gain + Σvoice·wet_mix``；
    mono 声部保持单声道参照增益；立体声声像仅作用于 L/R，多声道其余通道保持布局。
    """
    if pcm is None or pcm.ndim != 2 or pcm.shape[-1] == 0:
        return pcm
    out_channels, n = pcm.shape
    wet_level = float(np.clip(wet_mix_pct, 0.0, 100.0)) / 100.0
    dry_gain = 10.0 ** (float(np.clip(dry_gain_db, -120.0, 6.0)) / 20.0)
    output_gain = 10.0 ** (float(np.clip(output_gain_db, -24.0, 24.0)) / 20.0)
    active_voices: list[tuple[DoublerVoice, float, int]] = []
    has_enabled_voice = False
    for voice in voices:
        if cancel_check is not None and cancel_check():
            return pcm
        if voice.mute or not voice.enabled:
            continue
        has_enabled_voice = True
        semitones = float(voice.interval_st) + float(voice.detune_cents) / 100.0
        if not (SUPPORTED_SEMITONES[0] <= semitones <= SUPPORTED_SEMITONES[1]):
            raise ValueError(
                f"voice interval+detune {semitones:+.2f} st outside "
                f"SUPPORTED_SEMITONES {SUPPORTED_SEMITONES}")
        delay = int(round(float(np.clip(voice.delay_ms, 0.0, 100.0))
                          * sample_rate / 1000.0))
        if wet_level > 0.0 and delay < n:
            active_voices.append((voice, semitones, delay))

    if cancel_check is not None and cancel_check():
        return pcm
    pitch_input = pcm
    if has_enabled_voice:
        pitch_input = np.ascontiguousarray(pcm, dtype=np.float32)
        if not out_channels or not np.isfinite(pitch_input).all():
            raise ValueError("Expected finite planar PCM")
        if int(sample_rate) <= 0:
            raise ValueError("sample_rate must be positive")
    if not active_voices and dry_gain == output_gain == 1.0:
        return np.array(pcm, dtype=np.float32, order="C", copy=True)

    total = np.zeros(pcm.shape, dtype=np.float64) if active_voices else None
    remaining = Counter(semitones for _, semitones, _ in active_voices)
    cached: dict[float, np.ndarray] = {}
    for voice, semitones, delay in active_voices:
        if cancel_check is not None and cancel_check():
            return pcm
        shifted = cached.pop(semitones, None)
        if shifted is None:
            shifted = pitch_input if abs(semitones) < 1e-6 else pitch_shift_fixed_length(
                pitch_input, int(sample_rate), semitones=semitones)
        remaining[semitones] -= 1
        if remaining[semitones]:
            cached[semitones] = shifted
        # Crop at the clip boundary and add only the audible delayed samples.
        take = n - delay
        delayed = total[:, delay:]
        shifted = shifted[:, :take]
        pan_l, pan_r = _equal_power_pan(voice.pan_pct)
        gain = 10.0 ** (float(np.clip(voice.gain_db, -60.0, 6.0)) / 20.0)
        if out_channels == 1:
            # Pan/width require stereo; mono voices retain their reference gain.
            delayed[0] += shifted[0] * gain
        else:
            delayed[0] += shifted[0] * pan_l * gain
            delayed[1] += shifted[1] * pan_r * gain
            if out_channels > 2:
                delayed[2:] += shifted[2:] * gain
    if cancel_check is not None and cancel_check():
        return pcm
    result = pcm.astype(np.float64)
    result *= dry_gain
    if total is not None:
        total *= wet_level
        result += total
    result *= output_gain
    return np.ascontiguousarray(result, dtype=np.float32)


def doubles_from_params(params: dict) -> List[DoublerVoice]:
    """参数 dict（doubler 步骤 params）→ 声部列表（缺省承接）。"""
    raw = params.get("voices") or ()
    voices: List[DoublerVoice] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        voices.append(DoublerVoice(
            interval_st=float(item.get("interval_st", 0.0)),
            detune_cents=float(item.get("detune_cents", 0.0)),
            delay_ms=float(item.get("delay_ms", 0.0)),
            pan_pct=float(item.get("pan_pct", 0.0)),
            gain_db=float(item.get("gain_db", 0.0)),
            mute=bool(item.get("mute", False)),
            enabled=bool(item.get("enabled", True)),
        ))
    return voices
