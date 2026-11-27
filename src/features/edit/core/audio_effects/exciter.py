"""Two-band parallel harmonic exciter with streaming 2× oversampling."""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from src.features.edit.core.audio_dsp import AudioEffect

try:
    from scipy.signal import butter, firwin, lfilter, sosfilt, upfirdn
    _SCIPY = True
except ImportError:  # pragma: no cover - optional runtime
    _SCIPY = False

OVERSAMPLE_LATENCY = 10
_FIR_HISTORY = 2 * OVERSAMPLE_LATENCY
_HARMONIC_HP_HZ = 1200.0


def _finite(value: object, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _unit(value: object, default: float) -> float:
    return min(1.0, max(0.0, _finite(value, default)))


class ExciterEffect(AudioEffect):
    """Add even/odd harmonics from complementary low and high bands."""

    effect_type = "exciter"
    _KEY_ALIASES = {
        "ex_crossover_hz": "crossover_hz",
        "ex_bands": "bands",
        "ex_oversampling": "oversampling",
        "ex_wet": "wet",
        "ex_output_gain_db": "output_gain_db",
        "ex_quality": "quality",
    }

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._channels = 0
        self._enabled = True
        self._clean = False
        self._oversampling = 1
        self._crossover = 2000.0
        self._wet = 1.0
        self._gain = 1.0
        self._bands: tuple[tuple[str, float, float], ...] = ()
        self._shaping = False
        self._filter_key: tuple[float, int, float] | None = None
        self._stream_key: tuple[int, float, int] | None = None
        self._lp_sos: np.ndarray | None = None
        self._zi_low: np.ndarray | None = None
        self._hp_b: np.ndarray | None = None
        self._hp_a: np.ndarray | None = None
        self._hp_zi: list[np.ndarray | None] = [None, None]
        self._up_taps: np.ndarray | None = None
        self._down_taps: np.ndarray | None = None
        self._up_hist: np.ndarray | None = None
        self._down_hist: np.ndarray | None = None
        self._dry_delay: np.ndarray | None = None
        self._filter_tail = 0

    def set_params(
        self, params: object, sample_rate: float | None = None,
        algorithm_version: int | None = None,
    ) -> None:
        if sample_rate is not None:
            self._sample_rate = float(sample_rate)
        p: dict[str, Any] = {self._KEY_ALIASES.get(key, key): value for key, value in params.items()
                             } if isinstance(params, dict) else {}
        self._enabled = bool(p.get("enabled", True))
        clean = p.get("quality") == "clean"
        quality_changed = clean != self._clean
        self._clean = clean
        self._oversampling = 2 if _finite(p.get("oversampling"), 1.0) == 2.0 else 1
        crossover_rate = self._sample_rate if self._clean else self._sample_rate * self._oversampling
        self._crossover = min(crossover_rate * 0.45, max(100.0, _finite(p.get("crossover_hz"), 2000.0)))
        self._wet = _unit(p.get("wet"), 1.0)
        gain_db = min(24.0, max(-24.0, _finite(p.get("output_gain_db"), 0.0)))
        self._gain = 10.0 ** (gain_db / 20.0)
        raw = p.get("bands")
        raw_bands = raw[:2] if isinstance(raw, (list, tuple)) else ()
        bands = []
        for index in range(2):
            band = raw_bands[index] if index < len(raw_bands) else None
            config = band if isinstance(band, dict) else {}
            amount = _unit(config.get("amount"), 0.0) if config.get("enabled") else 0.0
            mix = _unit(config.get("mix"), 1.0)
            mode = "odd" if config.get("mode") == "odd" else "even"
            if self._clean and not mix:
                amount = 0.0
            if index < len(self._bands):
                old_mode, old_amount, _ = self._bands[index]
                if mode != old_mode or bool(amount) != bool(old_amount):
                    self._hp_zi[index] = None
            bands.append((mode, amount, mix))
        self._bands = tuple(bands)
        shaping = self._wet > 0.0 and any(amount for _, amount, _ in bands)
        if not self._enabled:
            self.reset_state()
        elif quality_changed or shaping != self._shaping:
            self._reset_harmonics()
        self._shaping = shaping
        if self.is_active:
            self._configure()

    def _configure(self) -> None:
        stream_key = (self._channels, self._sample_rate, self._oversampling)
        if stream_key != self._stream_key:
            self._stream_key = stream_key
            self.reset_state()
        if not self._shaping:
            return
        rate = self._sample_rate * self._oversampling
        hp_hz = min(_HARMONIC_HP_HZ, 0.45 * rate)
        # Saved profiles accepted the old high-rate crossover clamp.
        filter_key = (rate, self._oversampling, self._crossover)
        if filter_key != self._filter_key:
            self._lp_sos = butter(4, self._crossover, btype="lowpass", fs=rate, output="sos")
            self._hp_b, self._hp_a = butter(2, hp_hz, btype="highpass", fs=rate, output="ba")
            poles = [abs(complex(pole)) for section in self._lp_sos for pole in np.roots(section[3:])]
            poles.extend(abs(complex(pole)) for pole in np.roots(self._hp_a))
            # Budget both cascaded filters settling by 100 dB.
            self._filter_tail = math.ceil(2 * math.log(1e-5) / math.log(max(poles))
                                          / self._oversampling)
            self._filter_key = filter_key
            self._zi_low = None
            self._hp_zi = [None, None]
        if self._oversampling > 1 and self._up_taps is None:
            proto = firwin(2 * _FIR_HISTORY + 1, 0.5, window=("kaiser", 5.0))
            self._up_taps, self._down_taps = proto * 2, proto
    def reset_state(self) -> None:
        self._reset_harmonics()
        self._dry_delay = None

    def _reset_harmonics(self) -> None:
        self._zi_low = None
        self._hp_zi = [None, None]
        self._up_hist = None
        self._down_hist = None

    @property
    def monitoring_active(self) -> bool:
        return self._enabled and _SCIPY

    @property
    def is_active(self) -> bool:
        return self.monitoring_active and (self._shaping or self._gain != 1.0 or self._oversampling == 2)

    def latency_samples(self) -> int:
        # Keep graph PDC fixed across Wet/Amount automation, including zero.
        return _FIR_HISTORY if self.monitoring_active and self._oversampling == 2 else 0

    def tail_estimate_samples(self) -> int:
        latency = self.latency_samples()
        return self._filter_tail + 2 * latency if self.is_active and self._shaping else latency

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2 or not samples.size:
            return samples
        channels, count = samples.shape
        if channels != self._channels:
            self._channels = channels
            self._configure()

        oversampled = self._oversampling == 2
        if not self._shaping:
            dry = self._delay_dry(samples) if oversampled else samples
            if self._gain != 1.0:
                dry = np.multiply(dry, self._gain, dtype=np.float32)
            return np.ascontiguousarray(dry, dtype=np.float32)
        signal = self._upsample(samples) if oversampled else samples.astype(np.float64)
        if self._zi_low is None:
            self._zi_low = np.zeros((self._lp_sos.shape[0], channels, 2))
        low, self._zi_low = sosfilt(self._lp_sos, signal, axis=-1, zi=self._zi_low)
        wet = np.zeros_like(signal)
        for index, ((mode, amount, mix), band) in enumerate(zip(self._bands, (low, signal - low))):
            if self._clean:
                if amount:
                    wet += (amount * mix) * self._harmonics(band, mode, index)
            elif amount:
                harmonic = self._harmonics(band, mode, index)
                wet += band * (1.0 - mix) + (band + amount * harmonic) * mix
            else:
                wet += band

        if oversampled:
            wet = self._downsample(wet, count)
        dry = self._delay_dry(samples).astype(np.float64) if oversampled else signal
        if self._clean:
            out = dry + wet * self._wet
        else:
            out = dry * (1.0 - self._wet) + wet * self._wet
        out *= self._gain
        return np.ascontiguousarray(out, dtype=np.float32)

    def _harmonics(self, band: np.ndarray, mode: str, index: int) -> np.ndarray:
        if mode == "even":
            shaped = np.square(band)
        elif self._clean:
            shaped = np.square(band) * band
        else:
            shaped = band ** 3
        zi = self._hp_zi[index]
        if zi is None:
            zi = np.zeros((band.shape[0], 2))
        filtered, self._hp_zi[index] = lfilter(self._hp_b, self._hp_a, shaped, axis=-1, zi=zi)
        return filtered

    def _upsample(self, samples: np.ndarray) -> np.ndarray:
        if self._up_hist is None:
            self._up_hist = np.zeros((self._channels, _FIR_HISTORY))
        start = 2 * _FIR_HISTORY
        stream = np.concatenate((self._up_hist, samples), axis=-1)
        result = upfirdn(self._up_taps, stream, up=2, axis=-1)
        self._up_hist = stream[..., -_FIR_HISTORY:].copy()
        return result[..., start:start + 2 * samples.shape[-1]]

    def _downsample(self, signal: np.ndarray, count: int) -> np.ndarray:
        history = 2 * _FIR_HISTORY
        if self._down_hist is None:
            self._down_hist = np.zeros((self._channels, history))
        stream = np.concatenate((self._down_hist, signal), axis=-1)
        result = upfirdn(self._down_taps, stream, down=2, axis=-1)
        self._down_hist = stream[..., -history:].copy()
        return result[..., _FIR_HISTORY:_FIR_HISTORY + count]

    def _delay_dry(self, samples: np.ndarray) -> np.ndarray:
        latency = self.latency_samples()
        if not latency:
            return samples
        count = samples.shape[-1]
        if self._dry_delay is None:
            self._dry_delay = np.zeros((self._channels, latency), dtype=samples.dtype)
        stream = np.concatenate((self._dry_delay, samples), axis=-1)
        self._dry_delay = stream[..., count:].copy()
        return stream[..., :count]
