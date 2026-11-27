"""P2 · 混响响应分析（FX-10 · 2026-11-21 计划）。

- ``measure_rt60``：逆向积分能量斜率（−5 至 −35 dB 拟合外推 60 dB）
  ——计划给出的独立验算口径；时长不足或无衰减时明确报错；
- ``analyze_reverb_snapshot``：以参数全量快照建立**全新**
  ``ReverbEffect``（生产引擎的环形/FDN 状态不参与），单声道单位脉冲
  测合成 IR 的 onset / 长度 / RT60——图上"预测"与"实测"共用同一
  数据源（标注依据为模型与当前采样率）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import numpy as np


def predict_reverb_reflections(
    params: Dict[str, Any], sample_rate: float,
) -> tuple[tuple[tuple[float, float], ...], float]:
    """Return unfiltered stereo tap positions/gains and late-network onset in ms."""
    from src.features.edit.core.audio_dsp import ReverbEffect, _FDN8_DELAY_MS

    left, right, gain_l, gain_r, late_l, late_r = ReverbEffect.predict_geometry(
        params, sample_rate)
    ms_per_sample = 1000.0 / sample_rate
    taps = tuple(
        (int(offset) * ms_per_sample, float(gain) / 6.0)
        for offsets, gains in ((left, gain_l), (right, gain_r))
        for offset, gain in zip(offsets, gains)
    )
    first_line = max(2, round(min(_FDN8_DELAY_MS) / ms_per_sample))
    return taps, (min(late_l, late_r) + first_line) * ms_per_sample


def measure_rt60(
    ir: np.ndarray,
    sample_rate: int,
    *,
    fit_high_db: float = -5.0,
    fit_low_db: float = -35.0,
) -> float:
    """逆向积分能量斜率测 RT60（秒）。

    ``energy[k] = Σ_{n≥k} ir[n]²``（ Schroeder 逆积分）；dB 曲线在
    ``[fit_low_db, fit_high_db]`` 区间线性拟合，斜率外推到 −60 dB。
    无衰减或拟合窗过短时抛 ``ValueError``（计划：注明无法测量的原因）。
    """
    data = np.asarray(ir, dtype=np.float64).reshape(-1)
    if data.size == 0:
        raise ValueError("empty impulse response")
    energy = np.cumsum(np.square(data)[::-1])[::-1]
    if energy[0] <= 0.0:
        raise ValueError("no measurable decay (silent impulse response)")
    db = 10.0 * np.log10(np.maximum(energy / energy[0], 1e-20))
    fit = (db <= fit_high_db) & (db >= fit_low_db)
    if np.count_nonzero(fit) < 2 or db[-1] > fit_low_db:
        raise ValueError(
            "decay window too short for RT60 fit "
            f"(tail at {db[-1]:.1f} dB)")
    time = np.arange(db.size) / float(sample_rate)
    slope, _ = np.polyfit(time[fit], db[fit], 1)
    if slope >= 0.0:
        raise ValueError("no decaying slope (non-decaying response)")
    return float(-60.0 / slope)


def measure_t30_band_limited(
    response: np.ndarray,
    sample_rate: int,
    *,
    band: Tuple[float, float] = (100.0, 1000.0),
    onset_skip: int = 0,
) -> Optional[float]:
    """带限包络 T30（项目 RV-03 验收口径）：100–1000 Hz 能量包络
    从峰 −5 dB 到 −35 dB 的时长 ×2。包络未跨过 −35 dB 时返回 None。"""
    data = np.asarray(response, dtype=np.float64)
    if data.ndim == 1:
        data = data[None, :]
    summed = data.sum(axis=0)[onset_skip:]
    win = max(1, int(0.05 * sample_rate))
    if summed.size <= win:
        return None
    spec = np.fft.rfft(summed)
    freqs = np.fft.rfftfreq(summed.size, 1.0 / sample_rate)
    spec[(freqs < band[0]) | (freqs > band[1])] = 0.0
    filtered = np.abs(np.fft.irfft(spec, summed.size))
    # Centered boxcar with the same zero padding as convolve(mode="same").
    # Running sums avoid multiplying each sample by the entire 50 ms window.
    pad = win - 1
    energy = np.pad(filtered ** 2, (pad - pad // 2, pad // 2))
    integral = np.cumsum(np.concatenate(([0.0], energy)))
    env = (integral[win:] - integral[:-win]) / win
    edb = 10.0 * np.log10(np.maximum(env, 1e-16))
    peak = int(np.argmax(edb))
    decay = edb[peak:-1] - edb[peak]
    t5 = np.flatnonzero(decay < -5.0)
    t35 = np.flatnonzero(decay < -35.0)
    if not t5.size or not t35.size:
        return None
    return float((t35[0] - t5[0]) / sample_rate * 2.0)


def analyze_reverb_snapshot(
    params: Optional[Dict[str, Any]] = None,
    sample_rate: float = 48000.0,
    *,
    probe_seconds: float = 6.0,
) -> Dict[str, Any]:
    """以参数快照分析混响合成 IR（Direct/ER/Late 合成路径）。

    探针 = 立体声单位脉冲（项目 RV-03 驱动口径）；RT60 用带限 T30。
    返回 ``{'onset_samples', 'ir_length_samples', 'rt60_seconds' |
    None, 'rt60_error': str | None}``——RT60 无法测量时带原因。
    """
    from src.features.edit.core.audio_dsp import ReverbEffect

    effect = ReverbEffect(float(sample_rate))
    if params:
        effect.set_params(dict(params), sample_rate=sample_rate)
    n = int(probe_seconds * sample_rate)
    impulse = np.zeros((2, n), dtype=np.float32)
    impulse[:, 100] = 1.0
    response = effect.process(impulse).astype(np.float64)
    onset = int(np.flatnonzero(np.abs(response).max(axis=0) > 1e-6)[0]) \
        if np.any(np.abs(response) > 1e-6) else -1
    # 干信号 / 无尾音守卫：onset 之后 100 ms 无可见能量 → 无法测 RT60
    # （计划 FX-10：只有干信号时注明原因）。
    tail_start = max(0, onset) + int(0.1 * sample_rate)
    tail_energy = float(np.max(np.abs(response[:, tail_start:]))) \
        if tail_start < response.shape[-1] else 0.0
    if tail_energy < 1e-6:
        return {
            "onset_samples": onset,
            "ir_length_samples": int(response.shape[-1] - max(0, onset)),
            "rt60_seconds": None,
            "rt60_error": "dry-only or no reverb tail (no energy after "
                          "onset + 100 ms)",
            "sample_rate": float(sample_rate),
        }
    rt60 = measure_t30_band_limited(response, int(sample_rate),
                                    onset_skip=max(0, onset))
    error = None if rt60 is not None else \
        "decay envelope did not cross -35 dB (tail too short)"
    return {
        "onset_samples": onset,
        "ir_length_samples": int(response.shape[-1] - max(0, onset)),
        "rt60_seconds": rt60,
        "rt60_error": error,
        "sample_rate": float(sample_rate),
    }
