"""P1 · De-Click / De-Hum（2026-11-19 计划第三阶段 · 效果器接入 #2）。

纯函数（预渲染栈/Clip Editor 共用）：

- ``remove_hum``：50/60 Hz 基频与所选谐波的窄 notch（butter 带阻）；
  自动模式在安静窗上用 PSD 邻域显著性判断基频（50 vs 60）与谐波
  频漂（± 容差内跟随实测峰）；
- ``detect_clicks``：高通残差 + 滑窗 MAD 检测短离群；跨声道置信度
  （多声道同时超限提高置信）与宽带瞬态 veto（保护鼓点/爆破音——
  瞬态能量分散在全带时不过检）；
- ``remove_clicks``：候选点短区间双侧线性插值修复；长缺失（> 阈值）
  转频谱修复（``audio_repair.repair_spectral``）。
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from typing import List, Optional, Sequence, Tuple

import numpy as np

try:  # pragma: no cover - 环境探测
    from scipy.fft import next_fast_len, rfft
    from scipy.ndimage import uniform_filter1d
    from scipy.signal import butter, lfilter, resample_poly, sosfiltfilt
    _SCIPY = True
except ImportError:  # pragma: no cover
    _SCIPY = False


def scipy_available() -> bool:
    return _SCIPY


# ─────────────────────────────────────────────────────────────────────────────
# De-Hum
# ─────────────────────────────────────────────────────────────────────────────

def _hum_spectrum(
    pcm: np.ndarray, sample_rate: int, max_frequency: float, quiet_quantile: float,
) -> tuple[np.ndarray, float, float]:
    """Average the power of intact quiet windows, with independent channels."""
    frame_size = min(sample_rate, pcm.shape[-1])
    frames = np.atleast_2d(pcm)[..., :pcm.shape[-1] // frame_size * frame_size]
    frames = frames.reshape(frames.shape[0], -1, frame_size)
    keep = min(8, max(1, int(np.ceil(frames.shape[1] * quiet_quantile))))
    if keep < frames.shape[1]:
        energies = np.einsum("cfj,cfj->cf", frames, frames, dtype=np.float64)
        quiet = np.argpartition(energies, keep - 1, axis=1)[:, :keep]
        frames = np.take_along_axis(frames, quiet[..., None], axis=1)
    decimation = max(1, int(sample_rate / max(4096.0, 4.0 * max_frequency)))
    if decimation > 1:
        frames = resample_poly(frames, 1, decimation, axis=-1)
    size = next_fast_len(frames.shape[-1] * 8, real=True)
    spectrum = rfft(frames * np.hanning(frames.shape[-1]), n=size, axis=-1)
    power = np.mean(spectrum.real ** 2 + spectrum.imag ** 2, axis=1)
    # A single unaveraged spectrum needs more headroom over the local noise floor.
    return power, sample_rate / (decimation * size), 4.0 / keep


def _hum_peak(
    spectrum: np.ndarray, spacing: float, noise_margin_db: float, nominal: float,
    tolerance: float, prominence_db: float,
) -> tuple[float, float] | None:
    """Find a localized tonal peak and refine its frequency between FFT bins."""
    radius = max(5.0, tolerance + 2.0)
    start = max(1, int(np.ceil((nominal - radius) / spacing)))
    stop = min(spectrum.shape[-1] - 1, int((nominal + radius) / spacing) + 1)
    if stop - start < 3:
        return None
    near_start = max(0, int((nominal - 30.0) / spacing))
    near_stop = min(spectrum.shape[-1], int((nominal + 30.0) / spacing) + 1)
    best = None
    for channel in spectrum:
        index = start + int(np.argmax(channel[start:stop]))
        if not start < index < stop - 1 or channel[index] <= channel.max() * 1e-7:
            continue
        left, center, right = np.log(np.maximum(channel[index - 1:index + 2], 1e-30))
        curvature = left - 2.0 * center + right
        if curvature >= 0.0:
            continue
        frequency = float(spacing * (index + 0.5 * (left - right) / curvature))
        background = max(float(np.median(channel[near_start:near_stop])), 1e-30)
        prominence = float(10.0 * np.log10(channel[index] / background))
        if (abs(frequency - nominal) <= tolerance
                and prominence >= prominence_db + noise_margin_db
                and (best is None or prominence > best[1])):
            best = (frequency, prominence)
    return best


def _choose_hum_base(
    spectrum: np.ndarray, spacing: float, noise_margin_db: float, prominence_db: float,
) -> Optional[float]:
    best_base = None
    best_prominence = -np.inf
    for nominal in (50.0, 60.0):
        peak = _hum_peak(spectrum, spacing, noise_margin_db, nominal, 1.25, prominence_db)
        if peak is not None and peak[1] > best_prominence:
            best_base, best_prominence = nominal, peak[1]
    return best_base


def detect_hum_base(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    quiet_quantile: float = 0.2,
    prominence_db: float = 8.0,
) -> Optional[float]:
    """自动检测哼声基频（50 vs 60）：安静窗 PSD 邻域显著性。

    返回 50.0 / 60.0 / None（无可信峰）。安静窗 = 短时能量最低分位的
    分析窗（语音活跃段不参与，避免把基频误判成哼声）。
    """
    if (not _SCIPY or sample_rate <= 0 or pcm is None or pcm.ndim not in (1, 2)
            or not pcm.size or pcm.shape[-1] < 0.4 * sample_rate):
        return None
    analysis = _hum_spectrum(pcm, sample_rate, 90.0, quiet_quantile)
    return _choose_hum_base(*analysis, prominence_db)


def remove_hum(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    base_hz: Optional[float] = None,
    harmonics: Sequence[int] = (1, 2, 3, 4, 5, 6, 7, 8),
    auto_detect: bool = True,
    drift_hz: float = 0.6,
    q: float = 30.0,
    reduction_db: float = 48.0,
    region: Optional[Tuple[int, int]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> np.ndarray:
    """窄 notch 移除哼声及谐波（零相位 filtfilt——不引入相位偏移）。

    ``base_hz`` 为 None 且 auto_detect 时先检测基频（检测不到保留原声）；
    ``drift_hz`` 容差内用实测 PSD 峰跟随频漂（电网 50.02/59.94 等）。
    有用频带 ≤0.5 dB 扰动由窄带高 Q 保证。

    FX-07（2026-11-21 计划）：
    - ``reduction_db``（0..48）：湿声衰减受限——完全 notch 后与原声
      按 ``(1−k)·notched + k·original`` 混合（k = 10^(−red/20)），目标
      频点保留 k 幅度、其余频点增益中性；
    - ``region=(start, end)``：短选区借**整段**上下文滤波，最后只写回
      目标区间（半开区间；选区外逐样本与输入一致）。
    """
    if not _SCIPY or pcm is None or pcm.ndim != 2 or not pcm.size or sample_rate <= 0:
        return pcm
    sample_count = pcm.shape[-1]
    lo, hi = (0, sample_count) if region is None else (
        max(0, int(region[0])), min(sample_count, int(region[1])))
    reduction_db = float(np.clip(reduction_db, 0.0, 48.0))
    harmonics = tuple(harmonic for harmonic in harmonics if harmonic > 0)
    if (hi <= lo or reduction_db == 0.0 or not harmonics or sample_count <= 15
            or (cancel_check is not None and cancel_check())):
        return pcm

    needs_detection = base_hz is None and auto_detect
    if base_hz is None and not needs_detection:
        base_hz = 50.0
    analysis = None
    nyquist = sample_rate / 2.0
    if sample_count >= 0.4 * sample_rate and (needs_detection or drift_hz > 0.0):
        max_frequency = min(nyquist, (base_hz or 60.0) * float(max(harmonics)) + 30.0)
        analysis = _hum_spectrum(pcm, sample_rate, max_frequency, 0.2)
        if cancel_check is not None and cancel_check():
            return pcm
    if needs_detection and analysis is not None:
        base_hz = _choose_hum_base(*analysis, 8.0)
    if base_hz is None or not np.isfinite(base_hz) or base_hz <= 0.0:
        return pcm
    followed_base = base_hz
    if analysis is not None and drift_hz > 0.0:
        peak = _hum_peak(*analysis, base_hz, drift_hz, 8.0)
        if peak is not None:
            followed_base = peak[0]

    sections = []
    narrowest_bandwidth = np.inf
    for harmonic in harmonics:
        if cancel_check is not None and cancel_check():
            return pcm
        target = followed_base * harmonic
        if target >= nyquist * 0.95:
            continue
        if harmonic != 1 and analysis is not None and drift_hz > 0.0:
            peak = _hum_peak(*analysis, target, drift_hz, 8.0)
            if peak is not None:
                target = peak[0]
        bw = target / max(1.0, q)
        low = max(1.0, target - bw / 2.0)
        high = min(nyquist * 0.99, target + bw / 2.0)
        if high <= low:
            continue
        narrowest_bandwidth = min(narrowest_bandwidth, high - low)
        sections.append(butter(2, [low, high], btype="bandstop", fs=sample_rate, output="sos"))
    if not sections:
        return pcm

    sos = np.concatenate(sections)
    # Narrow notches settle slowly. Reflect some real context before the pass
    # so edge ringing from the initial filter state stays out of the clip.
    settling_pad = round(sample_rate * min(0.5, 0.5 / narrowest_bandwidth))
    padlen = min(sample_count - 1, max(3 * (2 * len(sos) + 1), settling_pad))
    k = 10.0 ** (-reduction_db / 20.0)
    out = np.empty(pcm.shape, dtype=np.float32) if region is None else pcm.copy(order="C")
    for row, channel in enumerate(pcm):
        if cancel_check is not None and cancel_check():
            return pcm
        wet = sosfiltfilt(sos, channel, padlen=padlen)
        mixed = (1.0 - k) * wet[lo:hi] + k * channel[lo:hi]
        out[row, lo:hi] = mixed.astype(np.float32)
    if cancel_check is not None and cancel_check():
        return pcm
    return out


# 语义别名（detect_hum_base 的简短名）。
detect_hum = detect_hum_base


# ─────────────────────────────────────────────────────────────────────────────
# De-Click
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClickCandidate:
    """一个检测到的点击（样本位置 + 置信度）。"""

    position: int
    width: int                 # 建议修复窗（样本，双侧）
    confidence: float          # 0..1（跨声道同时超限提高）
    channels: tuple[int, ...] | None = None  # None preserves all-channel repairs requested by callers


def detect_clicks(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    hp_hz: float = 3000.0,
    threshold_sigma: float = 8.0,
    min_gap_ms: float = 1.0,
    max_width: int = 128,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> List[ClickCandidate]:
    """高通残差 + 滑窗 MAD：短离群候选；宽带瞬态 veto。

    - 残差 = 各声道高通（butter 一阶）；200 ms 窗内的中位绝对
      残差估计各声道噪声标准差，超过阈值的连通段为候选；
    - 宽带瞬态 veto：候选段在全带能量同时大幅超限时判为真实瞬态
      （鼓点/爆破音），不作为点击修复；
    - 跨声道置信度：多声道候选重叠比例。
    """
    if (not _SCIPY or pcm is None or pcm.ndim != 2 or not pcm.size or sample_rate <= 0
            or (cancel_check is not None and cancel_check())):
        return []
    b, a = butter(1, min(hp_hz, sample_rate * 0.45), btype="highpass",
                  fs=sample_rate)
    residual = lfilter(b, a, pcm, axis=-1)
    np.abs(residual, out=residual)
    abs_pcm = np.abs(pcm)
    win = max(64, int(0.2 * sample_rate))
    floor = 1e-4 * np.median(abs_pcm, axis=-1) + 1e-12
    crossings = np.empty(residual.shape, dtype=np.bool_)
    for start in range(0, residual.shape[-1], win):
        if cancel_check is not None and cancel_check():
            return []
        end = min(residual.shape[-1], start + win)
        segment = residual[:, start:end]
        # A high-passed residual is centred on zero. Its median absolute
        # amplitude estimates Gaussian sigma after dividing by 0.67449.
        # Measuring each channel avoids a loud channel masking a quiet one.
        context_start = min(start, max(0, end - win))
        sigma = np.maximum(np.median(residual[:, context_start:end], axis=-1) * 1.482602, floor)
        crossings[:, start:end] = segment > (threshold_sigma * sigma[:, None])
    exceed = crossings.any(axis=0)
    runs = np.flatnonzero(np.diff(exceed, prepend=False, append=False)).reshape(-1, 2)
    if not len(runs) or (cancel_check is not None and cancel_check()):
        return []
    # 宽带包络（10 ms 滑动 |mono| 均值）：鼓点/爆破音的持续宽带事件
    # 显著抬高包络；6 样本点击几乎不动包络——以此 veto 保护辅音与鼓点。
    mono_abs = abs_pcm.max(axis=0)
    env_win = max(8, int(0.01 * sample_rate))
    broadband_env = uniform_filter1d(mono_abs, env_win, mode="constant", output=np.float64)
    env_baseline = np.median(broadband_env) + 1e-12
    candidates: List[ClickCandidate] = []
    min_gap = max(0, int(min_gap_ms * sample_rate / 1000.0))
    skip_until = 0
    look_back = int(0.040 * sample_rate)
    look_ahead = int(0.010 * sample_rate)
    for begin, end in runs:
        if cancel_check is not None and cancel_check():
            return []
        position = max(int(begin), skip_until)
        end = int(end)
        if position >= end:
            continue
        skip_until = end + min_gap
        width = min(max_width, max(8, (end - position) * 3))
        # veto 回望事件起始：段前 40 ms 到段后 10 ms 内的包络峰值
        # （鼓击尾段的候选回望 onset 峰；孤立点击无前后宽带抬升）。
        lo_env = max(0, position - look_back)
        hi_env = min(exceed.size, end + look_ahead)
        broadband_peak = float(np.max(broadband_env[lo_env:hi_env]))
        # An isolated impulse in digital silence should still be repairable.
        # Compare its short-window average with its instantaneous peak too.
        impulse_peak = float(np.max(mono_abs[position:end]))
        if broadband_peak < max(3.0 * env_baseline, 0.025 * impulse_peak):
            # 跨声道置信度：各声道独立超限的占比。
            per_channel = crossings[:, position:end].any(axis=1)
            confidence = 0.5 + 0.5 * float(per_channel.mean())
            candidates.append(ClickCandidate(
                position=position, width=width, confidence=confidence,
                channels=tuple(int(channel) for channel in np.flatnonzero(per_channel))))
    return [] if cancel_check is not None and cancel_check() else candidates


def _interpolate_repair(
    pcm: np.ndarray, start: int, end: int, *, out: np.ndarray | None = None,
    channels: tuple[int, ...] | None = None,
) -> np.ndarray:
    """Interpolate between the samples bordering the repair window."""
    if out is None:
        out = pcm.copy()
    sample_count = pcm.shape[-1]
    lo, hi = max(0, start), min(sample_count, end)
    if hi <= lo:
        return out
    rows = slice(None) if channels is None else list(channels)
    if sample_count - (hi - lo) < 2:
        out[rows, lo:hi] = 0.0
    elif lo == 0 or hi == sample_count:
        boundary = hi if lo == 0 else lo - 1
        out[rows, lo:hi] = pcm[rows, boundary:boundary + 1]
    else:
        positions = np.arange(lo, hi)
        for row in range(pcm.shape[0]) if channels is None else channels:
            channel = pcm[row]
            out[row, lo:hi] = np.interp(positions, (lo - 1, hi), (channel[lo - 1], channel[hi]))
    return out


def remove_clicks(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    candidates: Optional[List[ClickCandidate]] = None,
    spectral_fallback_ms: float = 25.0,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Tuple[np.ndarray, List[ClickCandidate]]:
    """移除点击：短区间双侧插值；长缺失转频谱修复。

    返回 (修复后 PCM, 实际处理的候选)。长缺失（候选宽 > 阈值）调用
    ``audio_repair.repair_spectral``（衰减模式填补），保持与频谱修复
    工具同一语义。
    """
    if (pcm is None or pcm.ndim != 2 or not pcm.size or sample_rate <= 0
            or (cancel_check is not None and cancel_check())):
        return pcm, []
    if candidates is None:
        candidates = detect_clicks(pcm, sample_rate, cancel_check=cancel_check)
    if not candidates:
        return pcm, []
    out = pcm
    handled: List[ClickCandidate] = []
    spectral_threshold = int(spectral_fallback_ms * sample_rate / 1000.0)
    # Merge overlapping windows before taking context from the original input.
    windows: list[tuple[int, int, list[ClickCandidate]]] = []
    for candidate in sorted(candidates, key=lambda c: c.position - max(4, c.width)):
        half = max(4, candidate.width)
        lo, hi = max(0, candidate.position - half), min(pcm.shape[-1], candidate.position + half)
        if hi <= lo or not 0 <= candidate.position < pcm.shape[-1]:
            continue
        if windows and lo <= windows[-1][1]:
            previous_lo, previous_hi, group = windows[-1]
            group.append(candidate)
            windows[-1] = (previous_lo, max(previous_hi, hi), group)
        else:
            windows.append((lo, hi, [candidate]))
    for lo, hi, group in windows:
        if cancel_check is not None and cancel_check():
            return pcm, []
        if lo == 0 or hi == pcm.shape[-1]:
            continue  # No bilateral context: preserve audio and omit from handled.
        channels = None if any(click.channels is None for click in group) else tuple(sorted({
            channel for click in group for channel in (click.channels or ())
            if 0 <= channel < pcm.shape[0]
        }))
        if channels == ():
            continue
        if not handled:
            out = pcm.copy()
        if hi - lo > spectral_threshold:
            from src.features.edit.core.audio_repair import repair_spectral
            high_hz = sample_rate * 0.45
            low_hz = min(20.0, high_hz / 2.0)
            nfft = 2048
            block_start = max(0, lo - nfft)
            block_end = min(pcm.shape[-1], hi + nfft)
            target = (lo - block_start, hi - block_start)
            repaired = repair_spectral(
                np.ascontiguousarray(pcm[:, block_start:block_end], dtype=np.float32), sample_rate,
                [{"sample": (target[0] + target[1]) // 2, "hz": float(np.sqrt(low_hz * high_hz)),
                  "time_radius": max(1, (hi - lo) // 2),
                  "bandwidth_oct": float(np.log2(high_hz / low_hz))}],
                target, amount_db=24.0, mode="heal", nfft=nfft, channels=channels)
            out[:, lo:hi] = repaired[:, target[0]:target[1]]
        else:
            _interpolate_repair(pcm, lo, hi, out=out, channels=channels)
        handled.extend(group)
    if cancel_check is not None and cancel_check():
        return pcm, []
    return out, handled
