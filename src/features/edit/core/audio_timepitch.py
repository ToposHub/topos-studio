"""Speed and pitch transforms for clip regions (P2-7 of the roadmap).

Definitions (roadmap §6):

- ``ratio_pct`` is a playback speed: ``output_samples =
  round(input_samples / (ratio_pct / 100))``.
- Without pitch preservation the transform is an anti-aliased resample —
  speed and pitch move together.
- With preservation, dialogue uses the existing reviewed WSOLA
  (``wsola_time_stretch``); channel displacements are shared by that
  implementation, so stereo image stays intact.
- Pitch-only shifting resamples by ``p = 2 ** (semitones / 12)`` (duration
  becomes ≈ N / p) and then time-stretches back to the target duration.

Every function returns a fresh array and never mutates its input; input
length is only changed where the operation requires it (speed/pitch).
"""
from __future__ import annotations

import math

import numpy as np

from src.features.edit.core.audio_timestretch import wsola_time_stretch

SUPPORTED_RATIO_PCT = (25.0, 400.0)
SUPPORTED_SEMITONES = (-24.0, 24.0)


def _validate(pcm: np.ndarray, sample_rate: int) -> None:
    if pcm.ndim != 2 or pcm.shape[0] == 0 or not np.isfinite(pcm).all():
        raise ValueError("Expected finite planar PCM")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")


def _resample_ratio(pcm: np.ndarray, sample_rate: int, ratio: float) -> np.ndarray:
    """抗混叠重采样：输出长度 = round(N / ratio)。ratio>1 更快/更短。"""
    n = pcm.shape[1]
    out_len = max(1, round(n / ratio))
    try:
        from scipy.signal import resample_poly
        from math import gcd
        # up/down 有理近似（1e-6 精度足够音频变速）。
        best_up, best_down = 1, 1
        best_err = float("inf")
        for den in range(1, 513):
            num = round(ratio * den)
            if num == 0:
                continue
            err = abs(num / den - ratio)
            if err < best_err:
                best_err, best_up, best_down = err, num, den
            if err < 1e-9:
                break
        g = gcd(best_up, best_down)
        # 速度 ratio（输出 N/ratio）：对信号做 up=den, down=num 的重采样
        # （与 ratio 的分子/分母互换——播放更快 = 下采样后音高升高）。
        up, down = best_down // g, best_up // g
        out = np.stack([
            resample_poly(pcm[ch].astype(np.float64), up, down)
            for ch in range(pcm.shape[0])
        ])
        # resample_poly 长度 = ceil(N * up / down)；裁/补到合同长度。
        if out.shape[1] > out_len:
            out = out[:, :out_len]
        elif out.shape[1] < out_len:
            pad = np.zeros((pcm.shape[0], out_len - out.shape[1]))
            out = np.concatenate([out, pad], axis=1)
        return out.astype(np.float32)
    except ImportError:
        # 无 SciPy：线性插值（质量降级但仍抗长度漂移）。
        positions = np.arange(out_len) * ratio
        positions = np.clip(positions, 0.0, max(0.0, n - 1))
        lo = np.floor(positions).astype(np.int64)
        hi = np.minimum(lo + 1, n - 1)
        frac = (positions - lo).astype(np.float32)
        return (pcm[:, lo] * (1.0 - frac) + pcm[:, hi] * frac).astype(np.float32)


def retime_nopreserve(pcm: np.ndarray, sample_rate: int, ratio_pct: float) -> np.ndarray:
    """变速不保调：抗混叠重采样，音高随速度变化。"""
    _validate(pcm, sample_rate)
    if not SUPPORTED_RATIO_PCT[0] <= float(ratio_pct) <= SUPPORTED_RATIO_PCT[1]:
        raise ValueError(f"ratio_pct out of range: {ratio_pct}")
    ratio = float(ratio_pct) / 100.0
    if abs(ratio - 1.0) < 1e-9:
        return pcm.copy()
    return _resample_ratio(pcm, sample_rate, ratio)


def retime_preserve(pcm: np.ndarray, sample_rate: int, ratio_pct: float) -> np.ndarray:
    """变速保调：WSOLA（对话优先；速度=播放速度，>1 更短）。"""
    _validate(pcm, sample_rate)
    if not SUPPORTED_RATIO_PCT[0] <= float(ratio_pct) <= SUPPORTED_RATIO_PCT[1]:
        raise ValueError(f"ratio_pct out of range: {ratio_pct}")
    ratio = float(ratio_pct) / 100.0
    if abs(ratio - 1.0) < 1e-9:
        return pcm.copy()
    return wsola_time_stretch(pcm, ratio, sample_rate).astype(np.float32, copy=False)


def pitch_shift(pcm: np.ndarray, sample_rate: int, semitones: float,
                *, ratio_pct: float = 100.0) -> np.ndarray:
    """单独改音高（保时长）：先按 p 重采样，再保调伸缩回目标时长。

    ``ratio_pct`` 允许与变速叠加（组合输出长度 = round(N / ratio)）。
    """
    _validate(pcm, sample_rate)
    if not SUPPORTED_SEMITONES[0] <= semitones <= SUPPORTED_SEMITONES[1]:
        raise ValueError(f"semitones out of range: {semitones}")
    if not SUPPORTED_RATIO_PCT[0] <= float(ratio_pct) <= SUPPORTED_RATIO_PCT[1]:
        raise ValueError(f"ratio_pct out of range: {ratio_pct}")
    ratio = float(ratio_pct) / 100.0
    if abs(semitones) < 1e-6 and abs(ratio - 1.0) < 1e-9:
        return pcm.copy()
    p = 2.0 ** (semitones / 12.0)
    # 1) 变调重采样：时长 ≈ N / p（升调 → 更短）。
    shifted = _resample_ratio(pcm, sample_rate, p)
    # 2) 保调伸缩回目标时长：需要变长 p 倍 → WSOLA 速度 = 1/p。
    if abs(ratio - 1.0) < 1e-9:
        return wsola_time_stretch(
            shifted, 1.0 / p, sample_rate).astype(np.float32, copy=False)
    # 组合：目标总长 round(N / ratio)；重采样到 N/(p·ratio)（音高 ×p·ratio）
    # 后再保调拉长 p 倍 → 长度 N/ratio、音高 ×p。
    combined = _resample_ratio(pcm, sample_rate, p * ratio)
    return wsola_time_stretch(
        combined, 1.0 / p, sample_rate).astype(np.float32, copy=False)


def dominant_frequency(pcm: np.ndarray, sample_rate: int) -> float:
    """整段主频估计（测试用；FFT 峰值 bin）。"""
    mono = pcm[0] if pcm.shape[0] == 1 else pcm.mean(axis=0)
    n = 1 << int(max(8, math.log2(max(2, mono.size))))
    spectrum = np.abs(np.fft.rfft(mono[:n].astype(np.float64)))
    return float(np.fft.rfftfreq(n, d=1.0 / sample_rate)[int(np.argmax(spectrum))])
