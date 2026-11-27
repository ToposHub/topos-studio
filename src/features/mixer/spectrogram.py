"""Display-only STFT analysis for the mixer clip editor."""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

FREQUENCY_MIN = 20.0
FREQUENCY_KNEE = 500.0
DB_FLOOR = -96.0


def hold_spectrum_peaks(current: list[float], previous: list[float]) -> list[float]:
    """Track per-bin maxima until the display is cleared or its bins change."""
    if len(current) != len(previous) or not previous:
        return current
    return list(map(max, current, previous))


def frequency_fraction(frequency: float, nyquist: float) -> float:
    offset = max(0.0, min(nyquist, frequency) - FREQUENCY_MIN)
    return math.log1p(offset / FREQUENCY_KNEE) / _frequency_span(nyquist)


def fraction_frequency(fraction: float, nyquist: float) -> float:
    return FREQUENCY_MIN + FREQUENCY_KNEE * math.expm1(fraction * _frequency_span(nyquist))


def _frequency_span(nyquist: float) -> float:
    return math.log1p((nyquist - FREQUENCY_MIN) / FREQUENCY_KNEE)


@dataclass(frozen=True, eq=False)
class Spectrogram:
    """8-bit dBFS image indices [channel, high-to-low frequency, time]."""

    pixels: np.ndarray
    start_sample: int
    end_sample: int
    total_samples: int
    nfft: int

    @property
    def samples_per_column(self) -> float:
        return (self.end_sample - self.start_sample) / self.pixels.shape[2]


@lru_cache(maxsize=16)
def _spectrogram_basis(sample_rate: int, nfft: int, rows: int) -> tuple[np.ndarray, ...]:
    window = np.hanning(nfft).astype(np.float32)
    amplitude_scale = np.full(nfft // 2 + 1, 2.0 / window.sum(), dtype=np.float32)
    amplitude_scale[[0, -1]] *= 0.5
    span = _frequency_span(sample_rate * 0.5)
    row_freq = FREQUENCY_MIN + FREQUENCY_KNEE * np.expm1((np.arange(rows) + 0.5) / rows * span)
    row_bins = row_freq * (nfft / sample_rate)
    lower = row_bins.astype(np.intp)
    weight = (row_bins - lower).astype(np.float32)
    fft_freq = np.fft.rfftfreq(nfft, 1.0 / sample_rate)
    fft_rows = np.minimum(rows - 1, (np.log1p(
        np.maximum(0, fft_freq - FREQUENCY_MIN) / FREQUENCY_KNEE
    ) / span * rows).astype(np.intp))
    peak_rows, peak_starts = np.unique(fft_rows, return_index=True)
    return window, amplitude_scale, lower, weight, peak_rows, peak_starts


def analyze_spectrogram(
    pcm: np.ndarray, sample_rate: int, *, nfft: int, columns: int,
    start: int = 0, end: int | None = None, rows: int = 320,
    cancelled: Callable[[], bool] = lambda: False,
) -> Spectrogram | None:
    """Keep per-channel peaks when reducing STFT frames to display pixels.

    Center the Hann windows on the indicated times, including at clip and
    viewport boundaries. Favor short hops for the magnified view while keeping
    both the working set and the returned image bounded.
    """
    channels, total = pcm.shape
    overview = start == 0 and end is None
    start = max(0, start)
    end = min(total, end) if end is not None else total
    if not channels or end <= start or cancelled():
        return None

    hop = nfft // 4 if overview else min(256, nfft // 4)
    frames = math.ceil((end - start) / hop)
    width = min(frames, columns)
    frame_edges = np.ceil(np.arange(width + 1) * (frames / width) - 0.5).astype(np.intp)
    pixels = np.empty((channels, rows, width), dtype=np.uint8)
    window, amplitude_scale, lower, weight, peak_rows, peak_starts = _spectrogram_basis(
        sample_rate, nfft, rows)
    half = nfft // 2
    windows = np.lib.stride_tricks.sliding_window_view(pcm, nfft, axis=1) if total >= nfft else None
    block_columns = max(1, 64 // math.ceil(frames / width))
    for first in range(0, width, block_columns):
        if cancelled():
            return None
        last = min(width, first + block_columns)
        frame_first, frame_last = frame_edges[first], frame_edges[last]
        centers = start + ((np.arange(frame_first, frame_last) + 0.5)
                           * ((end - start) / frames)).astype(np.intp)
        # Only the batches touching the clip boundary need padding. Detail
        # windows can still use real samples outside the current viewport.
        left, right = int(centers[0]) - half, int(centers[-1]) - half + nfft
        if windows is not None and left >= 0 and right <= total:
            batch = windows[:, centers - half]
        else:
            data = pcm[:, max(0, left):min(total, right)]
            data = np.pad(data, ((0, 0), (max(0, -left), max(0, right - total))))
            padded = np.lib.stride_tricks.sliding_window_view(data, nfft, axis=1)
            batch = padded[:, centers - centers[0]]
        amplitude = np.abs(np.fft.rfft(batch * window, axis=-1))
        amplitude *= amplitude_scale
        peaks = np.maximum.reduceat(amplitude, frame_edges[first:last] - frame_first, axis=1)
        # Interpolate sparse low-frequency bins, and retain peaks in rows
        # covering multiple FFT bins at higher frequencies.
        bands = peaks[..., lower] * (1.0 - weight) + peaks[..., lower + 1] * weight
        bands[..., peak_rows] = np.maximum(
            bands[..., peak_rows], np.maximum.reduceat(peaks, peak_starts, axis=-1))
        power_db = 20.0 * np.log10(np.maximum(bands, 10 ** (DB_FLOOR / 20.0)))
        levels = np.clip(np.rint((power_db / -DB_FLOOR + 1.0) * 255.0), 0, 255)
        pixels[:, :, first:last] = levels[..., ::-1].swapaxes(1, 2)
    pixels.setflags(write=False)
    return None if cancelled() else Spectrogram(pixels, start, end, total, nfft)
