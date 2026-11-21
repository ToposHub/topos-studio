"""P2 · 激励器（FX-15 · 2026-11-21 计划）。

首版：两段（low/high，减法互补分频——与 FX-04/FX-14 同基础），
每段可调 Amount 的谐波支路并回干声：

- ``even``：``hp(x²)``——x = A·sin 时 2f 分量 = amount·A²/2（计划
  实验口径：2.5 kHz 输入、amount 0.5 → 5 kHz ≈ 0.01）；
- ``odd``：``hp(x³)``——3f 分量 = amount·A³/4（2.5 kHz → 7.5 kHz）；
- 谐波支路经 1200 Hz 高通消除偶次整形的 DC（计划示例拐角）；
- Amount=0 / wet=0 / disabled → **精确干声**（逐样本一致）；
- 过采样（2×）用与 FX-14 相同的流式因果 FIR 模式（声明延迟 20）；
  1× 无延迟。Solo 属监听语义，DSP 不消费。

注册：``exciter``（Clip/Track 首批；Stereo Bus/Master 后续批次）。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np

from src.features.edit.core.audio_dsp import AudioEffect

try:  # pragma: no cover - 环境探测
    from scipy.signal import butter, firwin, lfilter, sosfilt, upfirdn
    _SCIPY = True
except ImportError:  # pragma: no cover
    _SCIPY = False

OVERSAMPLE_LATENCY = 10
_HARMONIC_HP_HZ = 1200.0
_MODES = ("even", "odd")


def _db(x: float) -> float:
    return 10.0 ** (float(x) / 20.0)


class ExciterEffect(AudioEffect):
    """两段激励器（谐波支路并回干声；FX-15 首版）。"""

    effect_type = "exciter"

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params: dict = {
            "enabled": True,
            "crossover_hz": 2000.0,
            "bands": (
                {"enabled": False, "mode": "even", "amount": 0.0,
                 "mix": 1.0},
                {"enabled": False, "mode": "odd", "amount": 0.0,
                 "mix": 1.0},
            ),
            "oversampling": 1,
            "wet": 1.0,
            "output_gain_db": 0.0,
        }
        self._channels = 0
        self._oversampling = 1
        self._lp_sos = None
        self._zi_low = None
        self._hp_b = None
        self._hp_a = None
        self._hp_zi: List[Optional[np.ndarray]] = []
        self._up_taps = None
        self._down_taps = None
        self._up_hist: Optional[np.ndarray] = None
        self._down_hist: Optional[np.ndarray] = None
        self._dry_delay: Optional[np.ndarray] = None
        self._crossover_key: tuple = ()
        self._rebuild()

    # ── 拓扑 ─────────────────────────────────────────────────────
    def _rebuild(self) -> None:
        if not self._channels:
            return
        oversampling = int(self._params.get("oversampling", 1))
        oversampling = oversampling if oversampling in (1, 2) else 1
        fs = float(self._sample_rate) * oversampling
        xover = float(np.clip(self._params.get("crossover_hz", 2000.0),
                              100.0, 0.45 * fs))
        key = (self._channels, oversampling, float(self._sample_rate),
               round(xover, 6))
        if key == self._crossover_key:
            return
        self._crossover_key = key
        self._oversampling = oversampling
        self._lp_sos = np.asarray(
            butter(4, xover, btype="lowpass", fs=fs, output="sos"),
            dtype=np.float64)
        self._zi_low = np.zeros((self._lp_sos.shape[0], self._channels, 2),
                                dtype=np.float64)
        self._hp_b, self._hp_a = butter(
            2, _HARMONIC_HP_HZ, btype="highpass", fs=fs, output="ba")
        self._hp_zi = [None] * 2
        r = oversampling
        if r > 1:
            proto = firwin(2 * 10 * r + 1, 1.0 / r, window=("kaiser", 5.0))
            self._up_taps = np.asarray(proto * r, dtype=np.float64)
            self._down_taps = np.asarray(proto, dtype=np.float64)
            self._up_hist = np.zeros((self._channels, 20), dtype=np.float64)
            self._down_hist = np.zeros((self._channels, 20 * r),
                                       dtype=np.float64)
        else:
            self._up_taps = None
            self._down_taps = None
            self._up_hist = None
            self._down_hist = None
        self._dry_delay = np.zeros(
            (self._channels, self.latency_samples()), dtype=np.float32)

    # ── 参数与状态 ───────────────────────────────────────────────
    def set_params(self, params: object,
                   sample_rate: Optional[float] = None,
                   algorithm_version: Optional[int] = None) -> None:
        if sample_rate is not None and sample_rate != self._sample_rate:
            self._sample_rate = float(sample_rate)
            self._crossover_key = ()
        merged = dict(self._params)
        if isinstance(params, dict):
            for key, value in params.items():
                merged[self._KEY_ALIASES.get(key, key)] = value
        self._params = merged
        self._oversampling = (int(self._params.get("oversampling", 1))
                              if int(self._params.get("oversampling", 1)) in (1, 2)
                              else 1)
        self._crossover_key = ()
        self._rebuild()

    _KEY_ALIASES = {
        "ex_crossover_hz": "crossover_hz",
        "ex_bands": "bands",
        "ex_oversampling": "oversampling",
        "ex_wet": "wet",
        "ex_output_gain_db": "output_gain_db",
    }

    def reset_state(self) -> None:
        if self._zi_low is not None:
            self._zi_low[:] = 0.0
        self._hp_zi = [None] * 2
        if self._up_hist is not None:
            self._up_hist[:] = 0.0
        if self._down_hist is not None:
            self._down_hist[:] = 0.0
        if self._dry_delay is not None:
            self._dry_delay[:] = 0.0

    @property
    def is_active(self) -> bool:
        return bool(self._params.get("enabled", True)) and _SCIPY

    def latency_samples(self) -> int:
        return 2 * OVERSAMPLE_LATENCY if self._oversampling > 1 else 0

    def tail_estimate_samples(self) -> int:
        return 0

    # ── 处理 ─────────────────────────────────────────────────────
    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2:
            return samples
        n = samples.shape[-1]
        if n == 0:
            return samples
        bands = self._params.get("bands", ())
        all_neutral = all(
            not bool(band.get("enabled", False))
            or float(band.get("amount", 0.0)) <= 0.0
            for band in bands)
        if all_neutral:
            return np.ascontiguousarray(samples, dtype=np.float32)
        channels = samples.shape[0]
        if channels != self._channels:
            self._channels = channels
            self._crossover_key = ()
            self._rebuild()

        r = self._oversampling
        if r > 1:
            x = self._upsample(samples)
        else:
            x = samples.astype(np.float64)

        low, self._zi_low = sosfilt(self._lp_sos, x, axis=-1, zi=self._zi_low)
        high = x - low
        band_signals = (low, high)

        wet = np.zeros_like(x)
        for band_idx in range(2):
            band = bands[band_idx] if band_idx < len(bands) else {}
            if (not bool(band.get("enabled", False))
                    or float(band.get("amount", 0.0)) <= 0.0):
                wet = wet + band_signals[band_idx]
                continue
            amount = float(np.clip(float(band.get("amount", 0.0)),
                                   0.0, 1.0))
            mode = str(band.get("mode", "even") or "even")
            if mode not in _MODES:
                mode = "even"
            source = band_signals[band_idx]
            shaped = source ** 2 if mode == "even" else source ** 3
            harmonics = self._highpass(shaped, band_idx, r)
            band_wet = source + amount * harmonics
            band_mix = float(np.clip(float(band.get("mix", 1.0)), 0.0, 1.0))
            wet = wet + (source * (1.0 - band_mix)
                         + band_wet * band_mix)

        if r > 1:
            wet = self._downsample(wet, n)
        global_wet = float(np.clip(self._params.get("wet", 1.0), 0.0, 1.0))
        dry = self._delay_dry(samples, n)
        out = dry * (1.0 - global_wet) + wet * global_wet
        out *= _db(self._params.get("output_gain_db", 0.0))
        return np.ascontiguousarray(out.astype(np.float32), dtype=np.float32)

    def _highpass(self, x: np.ndarray, band_idx: int, r: int) -> np.ndarray:
        if band_idx >= len(self._hp_zi):
            self._hp_zi.extend([None] * (band_idx + 1 - len(self._hp_zi)))
        zi = self._hp_zi[band_idx]
        if zi is None or zi.shape != (x.shape[0], len(self._hp_a) - 1):
            zi = np.zeros((x.shape[0], len(self._hp_a) - 1),
                          dtype=np.float64)
        out = np.empty_like(x)
        zi_out = np.zeros_like(zi)
        for ch in range(x.shape[0]):
            out[ch], zi_out[ch] = lfilter(self._hp_b, self._hp_a, x[ch],
                                          zi=zi[ch])
        self._hp_zi[band_idx] = zi_out
        return out

    # ── 过采样（FX-14 同款流式 FIR） ─────────────────────────────
    def _upsample(self, x: np.ndarray) -> np.ndarray:
        r = self._oversampling
        n = x.shape[-1]
        stream = np.concatenate([self._up_hist, x.astype(np.float64)],
                                axis=-1)
        y_full = upfirdn(self._up_taps, stream, up=r, axis=-1)
        out = y_full[..., 20 * r:20 * r + n * r]
        self._up_hist = np.ascontiguousarray(stream[..., -20:])
        return out

    def _downsample(self, u: np.ndarray, n: int) -> np.ndarray:
        r = self._oversampling
        stream = np.concatenate([self._down_hist, u], axis=-1)
        y_full = upfirdn(self._down_taps, stream, up=1, down=r, axis=-1)
        out = y_full[..., 20:20 + n]
        self._down_hist = np.ascontiguousarray(stream[..., -20 * r:])
        return out

    def _delay_dry(self, samples: np.ndarray, n: int) -> np.ndarray:
        latency = self.latency_samples()
        if latency <= 0:
            return samples.astype(np.float64)
        stream = np.concatenate([self._dry_delay, samples], axis=1)
        delayed = stream[:, :n].astype(np.float64)
        self._dry_delay = np.ascontiguousarray(
            stream[:, n:n + latency], dtype=np.float32)
        return delayed
