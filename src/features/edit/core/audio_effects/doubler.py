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

from dataclasses import dataclass
from collections.abc import Callable
from typing import List, Optional, Sequence, Tuple

import numpy as np

from src.features.edit.core.audio_timepitch import (
    SUPPORTED_SEMITONES,
    pitch_shift,
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
    n = pcm.shape[-1]
    mono_input = pcm.shape[0] == 1
    out_channels = pcm.shape[0]
    wet_level = float(np.clip(wet_mix_pct, 0.0, 100.0)) / 100.0
    total = np.zeros((out_channels, n), dtype=np.float64)
    for voice in voices:
        if cancel_check is not None and cancel_check():
            return pcm
        if voice.mute or not voice.enabled:
            continue
        semitones = float(voice.interval_st) + float(voice.detune_cents) / 100.0
        if not (SUPPORTED_SEMITONES[0] <= semitones <= SUPPORTED_SEMITONES[1]):
            raise ValueError(
                f"voice interval+detune {semitones:+.2f} st outside "
                f"SUPPORTED_SEMITONES {SUPPORTED_SEMITONES}")
        shifted = pitch_shift(pcm.astype(np.float32), int(sample_rate),
                              semitones=semitones)
        # 变调输出长度与输入可能相差 1-2 样本（短输入更明显）：裁齐。
        if shifted.shape[-1] > n:
            shifted = shifted[:, :n]
        elif shifted.shape[-1] < n:
            shifted = np.pad(
                shifted, ((0, 0), (0, n - shifted.shape[-1])))
        delay = int(round(float(np.clip(voice.delay_ms, 0.0, 100.0))
                          * sample_rate / 1000.0))
        if delay > 0:
            # 前置补零式延迟；delay ≥ n 时该声部在本缓冲内无内容
            # （旧实现 n−delay 为负 → 负索引从尾部取，广播崩溃）。
            take = max(0, n - delay)
            padded = np.zeros((shifted.shape[0], n), dtype=np.float32)
            if take > 0:
                padded[:, delay:] = shifted[:, :take]
            shifted = padded
        pan_l, pan_r = _equal_power_pan(voice.pan_pct)
        gain = 10.0 ** (float(np.clip(voice.gain_db, -60.0, 6.0)) / 20.0)
        if mono_input:
            # Pan/width require stereo; mono voices retain their reference gain.
            total[0] += shifted[0] * gain
        else:
            total[0] += shifted[0] * pan_l * gain
            total[1] += shifted[1] * pan_r * gain
            if out_channels > 2:
                total[2:] += shifted[2:] * gain
    dry_gain = 10.0 ** (float(np.clip(dry_gain_db, -120.0, 6.0)) / 20.0)
    if mono_input:
        result = np.stack([
            pcm[0].astype(np.float64) * dry_gain + total[0] * wet_level])
    else:
        result = (pcm.astype(np.float64) * dry_gain
                  + total * wet_level)
    result *= 10.0 ** (float(np.clip(output_gain_db, -24.0, 24.0)) / 20.0)
    return np.ascontiguousarray(result.astype(np.float32), dtype=np.float32)


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
