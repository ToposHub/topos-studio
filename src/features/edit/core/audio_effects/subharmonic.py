"""Confidence-gated octave synthesis for a single low-frequency source.

The focused processor analyzes the selected source range on a causal hop
grid. Projects without an explicit detector mode keep the original mix and
tracking behavior. Neither processor delays its dry samples.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np

from src.features.edit.core.audio_dsp import AudioEffect, _clamp_finite

try:  # pragma: no cover - the processor is disabled without SciPy
    from scipy.fft import irfft, next_fast_len, rfft
    from scipy.signal import butter, lfilter, sosfilt
    _SCIPY = True
except ImportError:  # pragma: no cover
    from numpy.fft import irfft, rfft
    _SCIPY = False

_F0_LOW = 45.0
_F0_HIGH = 180.0
_ANALYSIS_HIGH = 1000.0
_DETECT_WIN = 4800
_HOP = 512
_ENTER_CONF = 0.8
_EXIT_CONF = 0.55
_MIN_LOCK_FRAMES = 3

_DEFAULT_VALUES = {
    "enabled": True, "detection_mode": "legacy",
    "source_center_hz": 90.0, "source_bandwidth_hz": 60.0,
    "octave1_gain_db": -6.0, "octave2_gain_db": -12.0,
    "sub_lpf_hz": 120.0, "attack_ms": 20.0, "release_ms": 150.0,
    "mix": 0.5, "output_gain_db": 0.0,
}


@lru_cache(maxsize=16)
def _correlation_plan(length: int, rate: float, low_hz: float,
                      high_hz: float) -> tuple[np.ndarray, int]:
    first = max(2, int(np.floor(rate / high_hz)))
    last = int(np.ceil(rate / low_hz))
    lags = np.arange(first - 1, last + 2, dtype=np.intp)
    fft_size = next_fast_len(length + last + 1) if _SCIPY else length + last + 1
    return lags, fft_size


def _normalized_correlation(
    frame: np.ndarray, sample_rate: float, high_hz: float, min_rms: float,
    low_hz: float = _F0_LOW,
) -> tuple[np.ndarray, np.ndarray] | None:
    """FFT autocorrelation normalized by the two overlapping energies."""
    length = frame.size
    if length < 3 * int(sample_rate / _F0_LOW):
        return None
    x = np.asarray(frame, dtype=np.float64)
    centered = x - np.mean(x)
    energy = np.empty(length + 1, dtype=np.float64)
    energy[0] = 0.0
    np.cumsum(centered * centered, out=energy[1:])
    if energy[-1] < length * min_rms * min_rms:
        return None
    lags, fft_size = _correlation_plan(length, float(sample_rate), low_hz, high_hz)
    if lags[-1] + 1 >= length:
        return None
    fft = rfft(centered, n=fft_size)
    corr = irfft(fft.real ** 2 + fft.imag ** 2, n=fft_size)[lags]
    norm = np.sqrt(energy[length - lags] * (energy[-1] - energy[lags]))
    return lags, corr / np.maximum(norm, 1e-12)


def _peak_indices(corr: np.ndarray, confidence: float) -> np.ndarray:
    mid = corr[1:-1]
    return np.flatnonzero((mid > confidence) & (mid > corr[:-2]) & (mid >= corr[2:])) + 1


def _legacy_f0(frame: np.ndarray, sample_rate: float) -> tuple[float, float] | None:
    data = _normalized_correlation(frame, sample_rate, _F0_HIGH, 0.01)
    if data is not None:
        lags, corr = data
        peaks = _peak_indices(corr, _ENTER_CONF)
        if peaks.size:
            i = int(peaks[0])
            return sample_rate / int(lags[i]), float(corr[i])
    return None


def estimate_subharmonic_f0(frame: np.ndarray, sample_rate: float) -> float | None:
    """Return the original 45–180 Hz detector result for a full frame."""
    result = _legacy_f0(frame, sample_rate)
    return result[0] if result is not None else None


@lru_cache(maxsize=8)
def _focused_peak_window(rate: float) -> np.ndarray:
    # Average out adjacent-lag fluctuations due to broadband noise. A short
    # window retains the broad bass-period peak and its location.
    window = np.hanning(2 * max(2, int(rate / 2600.0)) + 1)
    return window / window.sum()


@lru_cache(maxsize=8)
def _channel_probe_window(frames: int) -> tuple[np.ndarray, float]:
    window = np.hanning(frames)
    return window, 2.0 / (frames * float(np.sum(window * window)))


def _focused_f0(frame: np.ndarray, rate: float, lower: float, upper: float,
                threshold: float, reference: float) -> tuple[float, float] | None:
    data = _normalized_correlation(frame, rate, _ANALYSIS_HIGH, 0.006, low_hz=40.0)
    if data is None:
        return None
    lags, corr = data
    window = _focused_peak_window(rate)
    if corr.size >= window.size:
        radius = window.size // 2
        lags = lags[radius:-radius]
        corr = np.convolve(corr, window, mode="valid")
    peaks = _peak_indices(corr, threshold)
    if not peaks.size:
        return None
    left, mid, right = corr[peaks - 1], corr[peaks], corr[peaks + 1]
    delta = 0.5 * (left - right) / np.minimum(left - 2.0 * mid + right, -1e-10)
    f0 = rate / (lags[peaks] + np.clip(delta, -0.5, 0.5))
    # Interpolation can be a few cents outside a mathematically exact edge.
    in_band = (f0 >= lower - 0.15) & (f0 <= upper + 0.15)
    # A confident first period above the source band must not lock as an
    # integer multiple of that period. A markedly stronger later peak can be
    # the true fundamental of a bass note with a prominent upper harmonic.
    in_band[1:] &= mid[1:] >= mid[0] + 0.08
    if reference:
        in_band &= np.abs(f0 - reference) <= reference * 0.25
    valid = np.flatnonzero(in_band)
    if not valid.size:
        return None
    i = int(valid[0])
    return float(f0[i]), min(1.0, float(mid[i]))


class SubharmonicEffect(AudioEffect):
    """Causal mono carrier mixed into the original channel layout."""

    effect_type = "subharmonic"

    _KEY_ALIASES = {f"sh_{key}": key for key in _DEFAULT_VALUES if key != "enabled"}

    def __init__(self, sample_rate: float = 48000.0) -> None:
        super().__init__(sample_rate)
        self._params = dict(_DEFAULT_VALUES)
        self._focused = False
        self._channels = 0
        self._detect_ring: np.ndarray | None = None
        self._sub_sos: np.ndarray | None = None
        self._filter_key: tuple[float, float] | None = None
        self._filter_tail = 0
        self._monitoring = True
        self.reset_state()
        self._configure()

    def _configure(self) -> None:
        p = self._params
        rate = self._sample_rate
        self._win = max(1024, int(_DETECT_WIN * rate / 48000.0))
        self._hop = max(1, round(_HOP * rate / 48000.0)) if self._focused else _HOP
        self._source_low = _F0_LOW
        self._source_high = _F0_HIGH
        if self._focused:
            center = _clamp_finite(p["source_center_hz"], _F0_LOW, _F0_HIGH, 90.0)
            width = _clamp_finite(p["source_bandwidth_hz"], 5.0, 270.0, 60.0)
            self._source_low = max(_F0_LOW, center - width * 0.5)
            self._source_high = min(_F0_HIGH, center + width * 0.5)
        self._mix = _clamp_finite(p["mix"], 0.0, 1.0, 0.5)
        if self._focused:
            attack = _clamp_finite(p["attack_ms"], 0.5, 80.0, 20.0)
            self._release = _clamp_finite(p["release_ms"], 20.0, 1000.0, 150.0)
            lpf_max = min(400.0, 0.45 * rate)
            gain_limit = 0.0
            self._attack_coeff = float(np.exp(np.log(0.05) / (attack * rate / 1000.0)))
            self._release_coeff = float(np.exp(np.log(0.001) / (self._release * rate / 1000.0)))
        else:
            attack = _clamp_finite(p["attack_ms"], 1.0, 60000.0, 20.0)
            self._release = _clamp_finite(p["release_ms"], 1.0, 60000.0, 150.0)
            lpf_max = 0.45 * rate
            gain_limit = 60.0
            self._attack_coeff = float(np.exp(-1.0 / (attack * rate / 1000.0)))
            self._release_coeff = float(np.exp(-1.0 / (self._release * rate / 1000.0)))
        gain1 = _clamp_finite(p["octave1_gain_db"], -120.0, gain_limit, -6.0)
        gain2 = _clamp_finite(p["octave2_gain_db"], -120.0, gain_limit, -12.0)
        self._g1 = 0.0 if self._focused and gain1 == -120.0 else 10.0 ** (gain1 / 20.0)
        self._g2 = 0.0 if self._focused and gain2 == -120.0 else 10.0 ** (gain2 / 20.0)
        trim_range = 12.0 if self._focused else 60.0
        trim = _clamp_finite(p["output_gain_db"], -trim_range, trim_range, 0.0)
        self._out_gain = 10.0 ** (trim / 20.0)
        lpf = _clamp_finite(p["sub_lpf_hz"], min(40.0, lpf_max), lpf_max, 120.0)
        if _SCIPY and (rate, lpf) != self._filter_key:
            self._filter_key = (rate, lpf)
            self._sub_sos = butter(2, lpf, btype="lowpass", fs=rate, output="sos")
            radius = float(np.max(np.abs(np.roots(self._sub_sos[0, 3:]))))
            self._filter_tail = int(np.ceil(np.log(0.001) / np.log(radius))) if 0.0 < radius < 1.0 else 0

    def set_params(self, params: object, sample_rate: float | None = None,
                   algorithm_version: int | None = None) -> None:
        snapshot = dict(_DEFAULT_VALUES)
        if isinstance(params, dict):
            snapshot.update((self._KEY_ALIASES.get(k, k), value) for k, value in params.items())
        focused = snapshot["detection_mode"] == "focused"
        rate = self._sample_rate if sample_rate is None else float(sample_rate)
        disabling_focused = focused and self._params["enabled"] and not snapshot["enabled"]
        if focused != self._focused or rate != self._sample_rate or disabling_focused:
            self._sample_rate = rate
            self._focused = focused
            self.reset_state()
        self._params = snapshot
        self._configure()
        if not self._focused:
            # Preserve the legacy filter restart on a new parameter snapshot.
            self._sub_zi = None

    def reset_state(self) -> None:
        self._detect_ring = None
        self._ring_pos = 0
        self._pos = 0
        self._next_hop = 0
        self._locked = False
        self._lock_frames = 0
        self._candidate = 0.0
        self._focus_channel: int | None = None
        self._confidence = 0.0
        self._f0 = 0.0
        self._env = 0.0
        self._phase1 = 0.0
        self._phase2 = 0.0
        self._sub_zi: np.ndarray | None = None
        self._sub_peak = 0.0

    @property
    def is_active(self) -> bool:
        return bool(self._params["enabled"]) and _SCIPY

    @property
    def monitoring_active(self) -> bool:
        return self._monitoring

    def set_monitoring(self, enabled: bool) -> None:
        """Collect audible generated peak only when the host needs it."""
        self._monitoring = enabled
        if not enabled:
            self._sub_peak = 0.0

    def latency_samples(self) -> int:
        # The detector needs acquisition time, while the insert passes dry
        # samples immediately and starts audible synthesis when it has a lock.
        return 0

    def tail_estimate_samples(self) -> int:
        release = int(self._release * self._sample_rate / 1000.0)
        # Once input stops, the two-hop level gate may take another hop to
        # observe its empty window. Allow the output poles to decay after release.
        return release + 3 * self._hop + self._filter_tail if self._focused else release

    def _write_input(self, samples: np.ndarray) -> None:
        n = samples.shape[-1]
        start = self._ring_pos
        first = min(n, self._win - start)
        self._detect_ring[:, start:start + first] = samples[:, :first]
        if first < n:
            self._detect_ring[:, :n - first] = samples[:, first:]
        self._ring_pos = (start + n) % self._win

    def _synthesize(self, sub: np.ndarray, env_data: np.ndarray) -> None:
        count = sub.size
        if not count:
            return
        locked = self._locked
        if not locked and self._env == 0.0:
            sub.fill(0.0)
            env_data.fill(0.0)
            return
        coefficient = self._attack_coeff if locked else self._release_coeff
        target = (1.0 - coefficient) if locked else 0.0
        env, _ = lfilter((1.0,), (1.0, -coefficient), np.full(count, target),
                         zi=(self._env * coefficient,))
        self._env = float(env[-1])
        env_data[:] = env
        release_finished = False
        if self._focused and not locked:
            # The envelope falls monotonically within this span. Stop the
            # oscillator at the first inaudible sample on every block grid.
            audible = int(np.count_nonzero(env >= 1e-7))
            release_finished = audible < count
            if release_finished:
                env_data[audible:] = 0.0
                sub[audible:] = 0.0
                count = audible
                sub, env_data = sub[:audible], env_data[:audible]
        step1 = self._f0 / 2.0 / self._sample_rate if locked or self._focused else 0.0
        step2 = step1 * 0.5
        if step1:
            # Retain the original double-precision phase recurrence. Only
            # the transcendental and envelope work need vectorized dispatch.
            p1, p2 = self._phase1, self._phase2
            phases1 = np.empty(count)
            phases2 = np.empty(count)
            for i in range(count):
                phases1[i] = p1
                phases2[i] = p2
                p1 = (p1 + step1) % 1.0
                p2 = (p2 + step2) % 1.0
            self._phase1, self._phase2 = p1, p2
            sub[:] = (self._g1 * np.sin(2.0 * np.pi * phases1)
                      + self._g2 * np.sin(2.0 * np.pi * phases2)) * env_data
        else:
            carrier = (self._g1 * float(np.sin(2.0 * np.pi * self._phase1))
                       + self._g2 * float(np.sin(2.0 * np.pi * self._phase2)))
            sub[:] = carrier * env_data
        if release_finished:
            self._env = self._f0 = self._phase1 = self._phase2 = 0.0

    def process(self, samples: np.ndarray) -> np.ndarray:
        if not self.is_active or samples is None or samples.ndim != 2 or not samples.size:
            return samples
        channels, n = samples.shape
        if self._focused and self._channels and channels != self._channels:
            self.reset_state()
        if channels != self._channels or self._detect_ring is None:
            self._channels = channels
            self._detect_ring = np.zeros((channels, self._win), dtype=np.float64)
            self._ring_pos = 0
            self._sub_zi = None
        sub = np.empty(n, dtype=np.float64)
        env = np.empty(n, dtype=np.float64)
        written = rendered = 0
        end = self._pos + n
        while self._next_hop < end:
            i = self._next_hop - self._pos
            self._synthesize(sub[rendered:i], env[rendered:i])
            self._write_input(samples[:, written:i + 1])
            self._detect_at_hop(self._next_hop + 1)
            self._next_hop += self._hop
            rendered, written = i, i + 1
        self._write_input(samples[:, written:])
        self._synthesize(sub[rendered:], env[rendered:])
        self._pos = end
        if self._sub_zi is None:
            self._sub_zi = np.zeros((self._sub_sos.shape[0], 2), dtype=np.float64)
        filtered, self._sub_zi = sosfilt(self._sub_sos, sub, zi=self._sub_zi)
        mix = self._mix if not self._focused or (self._g1 or self._g2) else 0.0
        if mix == 0.0:
            self._sub_peak = 0.0
            dry = samples if self._out_gain == 1.0 else samples.astype(np.float64) * self._out_gain
            return np.ascontiguousarray(dry, dtype=np.float32)
        filtered *= mix
        if self._monitoring:
            self._sub_peak = float(np.max(np.abs(filtered))) * self._out_gain
        dry_mix = 1.0 - mix * env if self._focused else 1.0 - mix
        out = samples.astype(np.float64, copy=False) * dry_mix + filtered
        return np.ascontiguousarray(out * self._out_gain, dtype=np.float32)

    def _focused_channels(self, frame: np.ndarray, powers: np.ndarray) -> list[int]:
        """Bound full-rate detection to likely low-frequency source channels."""
        # Retain the original stereo selection and fallback unchanged.
        if frame.shape[0] <= 2:
            return np.argsort(powers)[::-1].tolist()
        group = max(1, round(self._sample_rate / 1000.0))
        frames = frame.shape[-1] // group
        resolution = self._sample_rate / (group * frames)
        averaged = frame[:, :frames * group].reshape(frame.shape[0], frames, group).mean(axis=-1)
        window, normalize = _channel_probe_window(frames)
        spectrum = rfft((averaged - averaged.mean(axis=-1, keepdims=True)) * window, axis=-1)
        # A one-bin guard keeps this inexpensive probe tolerant at band edges.
        lo = max(1, int(np.floor(self._source_low / resolution)) - 1)
        hi = int(np.ceil(self._source_high / resolution)) + 2
        band = spectrum[:, lo:hi]
        score = normalize * np.sum(band.real ** 2 + band.imag ** 2, axis=-1) / np.maximum(powers, 1e-12)
        # Require a small share of the input power in the source band.
        # Broadband noise can be loud without occupying a candidate slot.
        worthy = np.flatnonzero(score >= 0.04)
        ordered = worthy[np.argsort(score[worthy])[::-1]]
        channels = ordered[:2].tolist()
        previous = self._focus_channel
        if self._locked and previous is not None and score[previous] >= 0.04:
            if previous in channels:
                channels.remove(previous)
            channels.insert(0, previous)
        return channels[:2]

    def _detect_at_hop(self, samples_seen: int) -> None:
        ring = self._detect_ring
        if not self._focused:
            powers = np.mean(ring * ring, axis=1)
            result = _legacy_f0(ring[int(np.argmax(powers))], self._sample_rate)
        else:
            result = None
            if samples_seen >= self._win:
                chronological = np.concatenate((ring[:, self._ring_pos:], ring[:, :self._ring_pos]), axis=1)
                if self._channels <= 2:
                    powers = np.mean(chronological * chronological, axis=1)
                else:
                    # Ranking only needs channel energy. Avoid a full-sized
                    # squared copy on high-channel-count buses.
                    powers = np.einsum("ct,ct->c", chronological, chronological) / self._win
                gate = 0.006 if self._locked else 0.01
                for ch in self._focused_channels(chronological, powers):
                    frame = chronological[ch]
                    recent = frame[-2 * self._hop:]
                    if np.mean(recent * recent) < gate * gate:
                        continue
                    result = _focused_f0(
                        frame, self._sample_rate, self._source_low, self._source_high,
                        _EXIT_CONF if self._locked else _ENTER_CONF,
                        self._f0 if self._locked else 0.0,
                    )
                    if result is not None:
                        self._focus_channel = ch
                        break
        self._confidence = result[1] if result is not None else 0.0
        f0 = result[0] if result is not None else None
        if self._locked:
            if f0 is None or abs(f0 - self._f0) > 0.25 * self._f0:
                self._lock_frames = 0
                self._candidate = 0.0
                self._locked = False
            else:
                self._f0 = 0.9 * self._f0 + 0.1 * f0
        elif f0 is not None:
            if self._focused and self._candidate and abs(f0 - self._candidate) > self._candidate * 0.12:
                self._lock_frames = 0
            self._candidate = f0
            self._lock_frames += 1
            if self._lock_frames >= _MIN_LOCK_FRAMES:
                self._locked = True
                self._f0 = f0
        else:
            self._lock_frames = 0
            self._candidate = 0.0

    def collect_telemetry(self) -> dict:
        return {
            "locked": bool(self._locked),
            "f0_hz": round(self._f0, 2),
            "lock_frames": self._lock_frames,
            "env": round(self._env, 4),
            "confidence": min(1.0, self._confidence),
            "detection_mode": "focused" if self._focused else "legacy",
            "source_low_hz": self._source_low,
            "source_high_hz": self._source_high,
            "sub_peak_db": 20.0 * float(np.log10(max(self._sub_peak, 1e-5))),
        }
