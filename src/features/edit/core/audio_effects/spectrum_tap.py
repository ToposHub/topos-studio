"""Bounded instance PCM windows; FFT runs on a dedicated analysis worker."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import numpy as np

_ANALYSIS_WORKERS = ThreadPoolExecutor(max_workers=1, thread_name_prefix='topos-fx-spectrum')


class InstanceSpectrumTap:
    """A subscription owns two preallocated windows and one pending analysis."""

    def __init__(self, sample_rate: float, channels: int = 8, size: int = 2048) -> None:
        self._rate = float(sample_rate)
        self._size = size
        self._input = np.zeros((channels, size), np.float32)
        self._output = np.zeros_like(self._input)
        self._position = 0
        self._channels = 0
        self._lock = threading.Lock()
        self._pending = False
        self._next = 0.0
        self._generation = 0
        self._snapshot: dict = {}

    def clear(self) -> None:
        with self._lock:
            self._generation += 1
            self._input.fill(0)
            self._output.fill(0)
            self._position = 0
            self._snapshot = {}

    def push(self, before: np.ndarray, after: np.ndarray) -> None:
        if before.shape != after.shape or before.ndim != 2 or not before.size:
            return
        if not self._lock.acquire(blocking=False):
            return
        try:
            channels = min(before.shape[0], self._input.shape[0])
            if channels != self._channels:
                self._input.fill(0)
                self._output.fill(0)
                self._channels = channels
            count = min(before.shape[-1], self._size)
            position = self._position
            first = min(count, self._size - position)
            for target, source in ((self._input, before), (self._output, after)):
                target[:channels, position:position + first] = source[:channels, -count:-count + first or None]
                if first < count:
                    target[:channels, :count-first] = source[:channels, -(count-first):]
            self._position = (position + count) % self._size
            now = time.monotonic()
            if not self._pending and now >= self._next:
                self._pending = True
                self._next = now + 1 / 30
                _ANALYSIS_WORKERS.submit(self._analyze, self._generation)
        finally:
            self._lock.release()

    def snapshot(self) -> dict:
        return self._snapshot

    def _analyze(self, generation: int) -> None:
        try:
            with self._lock:
                if generation != self._generation:
                    return
                before = np.roll(self._input[:self._channels], -self._position, axis=-1)
                after = np.roll(self._output[:self._channels], -self._position, axis=-1)
            window = np.hanning(self._size)
            bins = np.fft.rfftfreq(self._size, 1 / self._rate)
            freqs = np.geomspace(20, min(20000, self._rate * .49), 96)
            indices = np.searchsorted(bins, freqs).clip(0, bins.size - 1)
            snapshot = {'freqs': freqs.tolist(), 'sample_rate': self._rate,
                        'generation': generation}
            for name, samples in (('input_db', before), ('output_db', after)):
                power = np.mean(np.abs(np.fft.rfft(samples * window)) ** 2, axis=0)
                magnitude = np.sqrt(power) * 2 / max(window.sum(), 1)
                values = [float(20 * np.log10(max(float(magnitude[max(0, i-1):i+2].max()), 1e-6)))
                          for i in indices]
                snapshot[name] = values
            with self._lock:
                if generation == self._generation:
                    self._snapshot = snapshot
        finally:
            self._pending = False
