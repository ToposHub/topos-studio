"""P2 · 音高校正（FX-08 · 2026-11-21 计划）。

首个可听里程碑：Clip 级、单声部、固定时长自动校正（离线纯函数）。

- **F0 追踪**：等长差分窗 + 累积均值归一化 + 阈值选取（小型 YIN
  基线，计划实验口径）；帧列 {sample_center, f0_hz, confidence,
  voiced}；静音/清音标记无有效 F0；
- **量化**：连续 F0 → 浮点 MIDI → 启用的 12 音集合（C 大调白键缺省）
  挑目标；``bypass_notes`` 使当前音名直通；cents 偏移输出；
- **合成**：完整稳定音符（连续同目标帧段）施加固定偏移
  （``pitch_shift`` 保时长），段边界按 Retune 时间等长交叉淡化；静音/清音
  直通；**长度严格保持**；
- 参数：``ref_a4_hz``（400–480，默认 440）、``retune_ms``（0–400，
  默认 45——段边界淡化宽度）、``strength``（0–100%）、
  ``scale_notes``/``bypass_notes``（音名集合）。
"""
from __future__ import annotations

from bisect import bisect_left
from collections.abc import Callable
from dataclasses import dataclass
from itertools import chain
from typing import List, Optional, Sequence, Tuple

import numpy as np

from src.features.edit.core.audio_timepitch import pitch_shift_fixed_length

_NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A",
               "A#", "B")
_DEFAULT_SCALE = (0, 2, 4, 5, 7, 9, 11)  # C 大调（计划实验口径）


@dataclass(frozen=True)
class F0Frame:
    sample_center: int
    f0_hz: Optional[float]
    confidence: float
    voiced: bool


def _yin_scores(frames: np.ndarray, lag_max: int) -> np.ndarray:
    """Cumulative mean normalized difference from one correlation per frame."""
    frame_size = frames.shape[-1]
    reference_size = frame_size - lag_max
    fft_size = 1 << (frame_size - 1).bit_length()
    spectrum = np.fft.rfft(frames, n=fft_size, axis=-1)
    reference_spectrum = np.fft.rfft(frames[:, :reference_size], n=fft_size, axis=-1)
    # Positive lags up to lag_max only touch valid samples of the frame.
    correlation = np.fft.irfft(spectrum * reference_spectrum.conj(), n=fft_size, axis=-1)
    energy = np.cumsum(frames * frames, axis=-1)
    shifted_energy = energy[:, reference_size:] - energy[:, :lag_max]
    difference = energy[:, reference_size - 1:reference_size] + shifted_energy - 2.0 * correlation[:, 1:lag_max + 1]
    np.maximum(difference, 0.0, out=difference)
    cumulative = np.maximum(np.cumsum(difference, axis=-1), 1e-12)
    scores = np.ones((len(frames), lag_max + 1), dtype=np.float64)
    scores[:, 1:] = difference * np.arange(1, lag_max + 1) / cumulative
    return scores


def track_f0(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    window_ms: float = 50.0,
    hop_ms: float = 10.0,
    f0_low: float = 65.0,
    f0_high: float = 1000.0,
    silence_rms: float = 1e-4,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> List[F0Frame]:
    """单声部 F0 帧列；逐帧选能量最大声道，避免反相抵消。"""
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    audio = np.asarray(pcm)
    if audio.ndim == 1:
        audio = audio[None, :]
    if audio.ndim != 2 or not audio.shape[0]:
        raise ValueError("PCM must have at least one channel")
    win = max(256, int(window_ms * sample_rate / 1000.0))
    hop = max(64, int(hop_ms * sample_rate / 1000.0))
    lag_min = max(2, int(sample_rate / f0_high))
    lag_max = min(win - 4, int(np.ceil(sample_rate / f0_low)) + 1)
    if audio.shape[-1] < win:
        return []
    windows = np.lib.stride_tricks.sliding_window_view(audio, win, axis=-1)[:, ::hop]
    frames: List[F0Frame] = []
    batch_size = 16
    for offset in range(0, windows.shape[1], batch_size):
        if cancel_check is not None and cancel_check():
            return []
        candidates = windows[:, offset:offset + batch_size]
        frame_indices = np.arange(candidates.shape[1])
        means = candidates.mean(axis=-1, dtype=np.float64)
        power = np.einsum("cfn,cfn->cf", candidates, candidates, dtype=np.float64) / win - means ** 2
        strongest = power.argmax(axis=0)
        active = np.flatnonzero(power[strongest, frame_indices] > max(0.0, silence_rms) ** 2)
        estimates: dict[int, tuple[float, float]] = {}
        if active.size and lag_max > lag_min:
            selected_channels = strongest[active]
            waveforms = candidates[selected_channels, active].astype(np.float64)
            waveforms -= means[selected_channels, active, None]
            scores = _yin_scores(waveforms, lag_max)
            search = scores[:, lag_min:lag_max]
            minima = (search < 0.15) & (scores[:, lag_min - 1:lag_max - 1] >= search)
            minima &= search <= scores[:, lag_min + 1:lag_max + 1]
            for index in np.flatnonzero(minima.any(axis=-1)):
                lag = lag_min + int(minima[index].argmax())
                left, mid, right = scores[index, lag - 1:lag + 2]
                denom = max(left - 2.0 * mid + right, 1e-12)
                correction = 0.5 * (left - right) / denom
                estimates[int(active[index])] = (
                    float(sample_rate / (lag + correction)), float(1.0 - mid))
        for index in range(candidates.shape[1]):
            center = win // 2 + (offset + index) * hop
            if index in estimates:
                f0, confidence = estimates[index]
                frames.append(F0Frame(center, f0, confidence, True))
            else:
                frames.append(F0Frame(center, None, 0.0, False))
    return [] if cancel_check is not None and cancel_check() else frames


def _scale_midi_notes(ref_a4_hz: float, scale_notes: Sequence[int]) -> tuple[int, ...]:
    if not np.isfinite(ref_a4_hz) or not 400.0 <= ref_a4_hz <= 480.0:
        raise ValueError("ref_a4_hz must be within [400, 480]")
    notes = set(scale_notes)
    if not notes or any(not isinstance(n, (int, np.integer)) or not 0 <= n < 12 for n in notes):
        raise ValueError("scale_notes must contain pitch classes within [0, 11]")
    return tuple(note for note in range(128) if note % 12 in notes)


def _nearest_note(midi_float: float, allowed: tuple[int, ...]) -> int:
    higher = min(bisect_left(allowed, midi_float), len(allowed) - 1)
    lower = allowed[max(0, higher - 1)]
    upper = allowed[higher]
    return lower if abs(lower - midi_float) <= abs(upper - midi_float) else upper


def quantize_cents(
    f0_hz: float,
    *,
    ref_a4_hz: float = 440.0,
    scale_notes: Sequence[int] = _DEFAULT_SCALE,
    bypass_notes: Sequence[str] = (),
) -> Tuple[int, float]:
    """连续 F0 → 音阶量化（目标 MIDI 音名 + cents 偏移，计划实验口径）。"""
    if not np.isfinite(f0_hz) or f0_hz <= 0.0:
        raise ValueError("f0_hz must be finite and positive")
    allowed = _scale_midi_notes(ref_a4_hz, scale_notes)
    midi_float = 69.0 + 12.0 * np.log2(f0_hz / float(ref_a4_hz))
    source_note = round(midi_float)
    note_name = _NOTE_NAMES[source_note % 12]
    if note_name in set(bypass_notes):
        return source_note, 0.0
    target = _nearest_note(midi_float, allowed)
    cents = 100.0 * (target - midi_float)
    return target, cents


def _apply_segment(
    pcm: np.ndarray, out: np.ndarray, sample_rate: int, start: int, end: int, cents: float,
    retune_ms: float = 45.0,
) -> None:
    """Write one fixed pitch shift and crossfade into the existing output."""
    seg = pcm[:, start:end]
    shifted = pitch_shift_fixed_length(
        np.ascontiguousarray(seg, dtype=np.float32), int(sample_rate), semitones=cents / 100.0)
    target = out[:, start:end]
    target[:] = shifted
    fade = min(round(retune_ms * sample_rate / 1000.0), seg.shape[-1] // 2)
    if fade:
        ramp = np.linspace(0.0, 1.0, fade, dtype=np.float64)
        target[:, :fade] = seg[:, :fade] * (1.0 - ramp) + shifted[:, :fade] * ramp
        target[:, -fade:] = seg[:, -fade:] * (1.0 - ramp[::-1]) + shifted[:, -fade:] * ramp[::-1]


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
    cancel_check: Optional[Callable[[], bool]] = None,
) -> np.ndarray:
    """自动音高校正（离线、长度严格保持、不修改输入）。

    稳定音符 = 连续 ``min_note_frames`` 个 voiced 帧且量化目标一致；
    短/不一致段直通；``strength`` 0–100% 缩放校正量；``retune_ms``
    指定段边界淡化宽度（0 = 硬切换）。
    """
    if pcm is None or pcm.ndim != 2 or pcm.shape[-1] == 0:
        return pcm
    # Validate once, including unvoiced clips, so bad projects never silently pass.
    allowed = _scale_midi_notes(ref_a4_hz, scale_notes)
    if not np.isfinite(retune_ms) or not 0.0 <= retune_ms <= 400.0:
        raise ValueError("retune_ms must be within [0, 400]")
    strength = float(np.clip(strength, 0.0, 100.0)) / 100.0
    if strength <= 0.0:
        return np.ascontiguousarray(pcm, dtype=np.float32)
    frames = track_f0(pcm, sample_rate, cancel_check=cancel_check)
    if cancel_check is not None and cancel_check():
        return pcm
    hop = max(64, int(0.010 * sample_rate))
    bypass = set(bypass_notes)
    run_target = None
    run_start = run_end = 0
    run_cents: list[float] = []
    segments: List[Tuple[int, int, float]] = []
    for frame in chain(frames, (F0Frame(-1, None, 0.0, False),)):
        target = None
        midi = 0.0
        if frame.voiced and frame.f0_hz is not None:
            midi = float(69.0 + 12.0 * np.log2(frame.f0_hz / ref_a4_hz))
            if _NOTE_NAMES[round(midi) % 12] not in bypass:
                target = _nearest_note(midi, allowed)
        if target != run_target:
            if run_cents and len(run_cents) >= min_note_frames:
                mean_cents = float(np.mean(run_cents))
                if abs(mean_cents) >= 1.0:
                    segments.append((max(0, run_start), min(pcm.shape[-1], run_end), mean_cents * strength))
            run_target = target
            run_start = frame.sample_center - hop // 2
            run_cents.clear()
        if target is not None:
            run_end = frame.sample_center + hop // 2
            run_cents.append(100.0 * (target - midi))
    dry = np.ascontiguousarray(pcm, dtype=np.float32)
    if not segments:
        return dry.copy()
    out = dry.copy()
    for start, end, cents in segments:
        if cancel_check is not None and cancel_check():
            return pcm
        _apply_segment(dry, out, sample_rate, start, end, cents, retune_ms)
    return pcm if cancel_check is not None and cancel_check() else out
