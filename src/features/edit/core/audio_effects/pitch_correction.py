"""P2 · 音高校正（FX-08 · 2026-11-21 计划）。

首个可听里程碑：Clip 级、单声部、固定时长自动校正（离线纯函数）。

- **F0 追踪**：等长差分窗 + 累积均值归一化 + 阈值选取（小型 YIN
  基线，计划实验口径）；帧列 {sample_center, f0_hz, confidence,
  voiced}；静音/清音标记无有效 F0；
- **量化**：连续 F0 → 浮点 MIDI → 启用的 12 音集合（C 大调白键缺省）
  挑目标；``bypass_notes`` 使当前音名直通；cents 偏移输出；
- **合成**：完整稳定音符（连续同目标帧段）施加固定偏移
  （``pitch_shift`` 保时长），段边界 10 ms 等长交叉淡化；静音/清音
  直通；**长度严格保持**；
- 参数：``ref_a4_hz``（400–480，默认 440）、``retune_ms``（0–400，
  默认 45——段边界淡化宽度的语义载体）、``strength``（0–100%）、
  ``scale_notes``/``bypass_notes``（音名集合）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

from src.features.edit.core.audio_timepitch import pitch_shift

_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A",
               "A#", "B")
_DEFAULT_SCALE = (0, 2, 4, 5, 7, 9, 11)  # C 大调（计划实验口径）


@dataclass(frozen=True)
class F0Frame:
    sample_center: int
    f0_hz: Optional[float]
    confidence: float
    voiced: bool


def track_f0(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    window_ms: float = 50.0,
    hop_ms: float = 10.0,
    f0_low: float = 65.0,
    f0_high: float = 1000.0,
    silence_rms: float = 1e-4,
) -> List[F0Frame]:
    """单声部 F0 帧列（mono 求和；纯函数）。"""
    mono = (pcm.mean(axis=0) if pcm.ndim > 1 else pcm).astype(np.float64)
    win = max(256, int(window_ms * sample_rate / 1000.0))
    hop = max(64, int(hop_ms * sample_rate / 1000.0))
    lag_min = max(2, int(sample_rate / f0_high))
    lag_max = min(win - 2, int(sample_rate / f0_low))
    frames: List[F0Frame] = []
    for center in range(win // 2, max(win // 2, mono.size - win // 2), hop):
        lo = center - win // 2
        hi = min(mono.size, lo + win)
        frame = mono[lo:hi]
        rms = float(np.sqrt(np.mean(frame ** 2))) if frame.size else 0.0
        if rms < silence_rms or frame.size < lag_max + 4:
            frames.append(F0Frame(center, None, 0.0, False))
            continue
        size = frame.size - lag_max
        if size <= 0:
            frames.append(F0Frame(center, None, 0.0, False))
            continue
        diff = np.array([
            float(np.sum((frame[:size] - frame[lag:lag + size]) ** 2))
            for lag in range(1, lag_max + 1)])
        cumulative = np.maximum(np.cumsum(diff), 1e-12)
        score = np.concatenate((
            [1.0],
            diff * np.arange(1, lag_max + 1) / cumulative))
        f0: Optional[float] = None
        conf = 0.0
        for lag in range(lag_min, lag_max):
            if (score[lag] < 0.15
                    and score[lag - 1] >= score[lag] <= score[lag + 1]):
                left, mid, right = score[lag - 1:lag + 2]
                denom = max(left - 2.0 * mid + right, 1e-12)
                correction = 0.5 * (left - right) / denom
                f0 = sample_rate / (lag + correction)
                conf = float(np.clip(1.0 - score[lag], 0.0, 1.0))
                break
        frames.append(F0Frame(center, f0, conf, f0 is not None))
    return frames


def quantize_cents(
    f0_hz: float,
    *,
    ref_a4_hz: float = 440.0,
    scale_notes: Sequence[int] = _DEFAULT_SCALE,
    bypass_notes: Sequence[str] = (),
) -> Tuple[int, float]:
    """连续 F0 → 音阶量化（目标 MIDI 音名 + cents 偏移，计划实验口径）。"""
    midi_float = 69.0 + 12.0 * np.log2(f0_hz / float(ref_a4_hz))
    note_name = _NOTE_NAMES[int(round(midi_float)) % 12]
    if note_name in set(bypass_notes):
        return int(round(midi_float)), 0.0
    allowed = [note for note in range(128)
               if note % 12 in set(int(n) for n in scale_notes)]
    target = min(allowed, key=lambda note: (abs(note - midi_float), note))
    cents = 100.0 * (target - midi_float)
    return target, cents


def _apply_segment(
    pcm: np.ndarray, sample_rate: int, start: int, end: int, cents: float,
) -> np.ndarray:
    """段内固定偏移（pitch_shift 保时长 + 裁齐），段外不变。"""
    seg = pcm[:, start:end]
    shifted = pitch_shift(seg.astype(np.float32), int(sample_rate),
                          semitones=cents / 100.0)
    if shifted.shape[-1] > seg.shape[-1]:
        shifted = shifted[:, :seg.shape[-1]]
    elif shifted.shape[-1] < seg.shape[-1]:
        shifted = np.pad(shifted, ((0, 0), (0, seg.shape[-1]
                                            - shifted.shape[-1])))
    out = pcm.copy()
    fade = min(480, max(1, int(0.010 * sample_rate)), seg.shape[-1] // 2)
    ramp = np.linspace(0.0, 1.0, fade, dtype=np.float64)
    window = np.ones(seg.shape[-1], dtype=np.float64)
    window[:fade] = ramp
    window[-fade:] = ramp[::-1]
    mixed = (seg.astype(np.float64) * (1.0 - window)
             + shifted.astype(np.float64) * window)
    out[:, start:end] = mixed.astype(np.float32)
    return out


def render_pitch_correction(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    ref_a4_hz: float = 440.0,
    strength: float = 100.0,
    retune_ms: float = 45.0,
    scale_notes: Sequence[int] = _DEFAULT_SCALE,
    bypass_notes: Sequence[str] = (),
    min_note_frames: int = 3,
) -> np.ndarray:
    """自动音高校正（离线、长度严格保持、不修改输入）。

    稳定音符 = 连续 ``min_note_frames`` 个 voiced 帧且量化目标一致；
    短/不一致段直通；``strength`` 0–100% 缩放校正量；``retune_ms``
    记录为段边界淡化宽度的语义载体（0 = 硬切换）。
    """
    if pcm is None or pcm.ndim != 2 or pcm.shape[-1] == 0:
        return pcm
    strength = float(np.clip(strength, 0.0, 100.0)) / 100.0
    if strength <= 0.0:
        return np.ascontiguousarray(pcm, dtype=np.float32)
    frames = track_f0(pcm, sample_rate)
    hop = max(64, int(0.010 * sample_rate))
    out = pcm.astype(np.float32).copy()
    run: List[F0Frame] = []
    segments: List[Tuple[int, int, float]] = []
    for frame in frames + [F0Frame(-1, None, 0.0, False)]:
        target_note = None
        cents = None
        if frame.voiced:
            try:
                target_note, cents = quantize_cents(
                    float(frame.f0_hz), ref_a4_hz=ref_a4_hz,
                    scale_notes=scale_notes, bypass_notes=bypass_notes)
            except (TypeError, ValueError):
                target_note = None
        if (frame.voiced and target_note is not None and run
                and target_note == run[-1][0]):
            run.append((target_note, frame))
            continue
        if len(run) >= min_note_frames:
            start = run[0][1].sample_center - hop // 2
            end = run[-1][1].sample_center + hop // 2
            mean_cents = float(np.mean([cents_value for _, f in run
                                        for cents_value in
                                        [quantize_cents(
                                            float(f.f0_hz),
                                            ref_a4_hz=ref_a4_hz,
                                            scale_notes=scale_notes,
                                            bypass_notes=bypass_notes)[1]]]))
            segments.append((max(0, start),
                             min(pcm.shape[-1], end), mean_cents))
        run = []
        if frame.voiced and target_note is not None:
            run = [(target_note, frame)]
    for start, end, mean_cents in segments:
        if abs(mean_cents) < 1.0:
            continue
        cents = mean_cents * strength
        out = _apply_segment(out, sample_rate, start, end, cents)
    return np.ascontiguousarray(out, dtype=np.float32)
