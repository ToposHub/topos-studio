"""audio_effects 共享原语（无环基础模块）。

- ``seeded_rng``：由持久 seed 派生的确定性随机流（grain/抖动的唯一
  随机来源；消耗顺序只依赖绝对事件序，块长无关）。
- ``one_pole_dc_blocker``：失真偏置后的去 DC（状态显式携带）。
- ``BiquadBank``：逐带 biquad 组（vocoder 分析/合成共用系数语义）。
- ``mix_aligned``：模块 dry/wet 插值（0/100 两端精确，调用方已对齐时延）。

只依赖 numpy；不得 import audio_dsp / timeline_vsp（防循环）。
"""

from __future__ import annotations

from typing import Tuple

import numpy as np


def seeded_rng(seed: int, stream_id: int = 0) -> np.random.Generator:
    """由 (seed, stream_id) 确定性派生独立随机流。"""
    ss = np.random.SeedSequence([int(seed) & 0xFFFFFFFF, int(stream_id) & 0xFFFF])
    return np.random.Generator(np.random.PCG64(ss))


def mix_aligned(dry: np.ndarray, wet: np.ndarray, mix_pct: float) -> np.ndarray:
    """模块 dry/wet 插值（计划 §10.2 oracle 语义；0/100 两端精确）。"""
    if mix_pct <= 0.0:
        return dry.copy()
    if mix_pct >= 100.0:
        return wet.copy()
    m = float(mix_pct) / 100.0
    return dry + m * (wet - dry)


class OnePoleDCBlocker:
    """去 DC 单极点：y[n] = x[n] - x[n-1] + a·y[n-1]（a≈0.9995@48k）。"""

    def __init__(self, sample_rate: float) -> None:
        self._a = float(np.exp(-2.0 * np.pi * 10.0 / max(1.0, sample_rate)))
        self._x1 = 0.0
        self._y1 = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        a = self._a
        # 递推向量化：y[n] = x[n] - x[n-1] + a*y[n-1] → lfilter 等价
        b = np.array([1.0, -1.0])
        aa = np.array([1.0, -a])
        from scipy.signal import lfilter
        out, zf = lfilter(b, aa, x, zi=[self._y1 * 1.0])
        self._y1 = float(zf[0])
        return out

    def reset(self) -> None:
        self._x1 = 0.0
        self._y1 = 0.0


def biquad_lowpass_coeffs(freq_hz: float, q: float,
                          sample_rate: float) -> Tuple[np.ndarray, np.ndarray]:
    """RBJ 低通（tone 滤波；q 有界防自激）。"""
    w0 = 2.0 * np.pi * float(freq_hz) / float(sample_rate)
    cw, sw = np.cos(w0), np.sin(w0)
    alpha = sw / (2.0 * max(0.05, float(q)))
    b0 = (1.0 - cw) / 2.0
    b1 = 1.0 - cw
    b2 = (1.0 - cw) / 2.0
    a0 = 1.0 + alpha
    return (np.array([b0 / a0, b1 / a0, b2 / a0]),
            np.array([1.0, (-2.0 * cw) / a0, (1.0 - alpha) / a0]))


def biquad_bandpass_coeffs(freq_hz: float, q: float,
                           sample_rate: float) -> Tuple[np.ndarray, np.ndarray]:
    """RBJ 带通（恒定 0dB 峰值口径——vocoder 分析/合成频带归一）。"""
    w0 = 2.0 * np.pi * float(freq_hz) / float(sample_rate)
    cw, sw = np.cos(w0), np.sin(w0)
    alpha = sw / (2.0 * max(0.05, float(q)))
    b0, b1, b2 = alpha, 0.0, -alpha
    a0 = 1.0 + alpha
    return (np.array([b0 / a0, 0.0, b2 / a0]),
            np.array([1.0, (-2.0 * cw) / a0, (1.0 - alpha) / a0]))


def log_spaced_bands(bands: int, f_lo: float, f_hi: float,
                     sample_rate: float) -> np.ndarray:
    """对数分布频带中心（上界受 Nyquist 限制）。"""
    f_hi = min(f_hi, sample_rate * 0.45)
    return np.geomspace(max(20.0, f_lo), max(f_lo * 1.01, f_hi), int(bands))
