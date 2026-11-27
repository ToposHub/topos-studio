"""Frequency snapshots for the time-varying modulation processors."""
from __future__ import annotations

import math
import numpy as np


def phaser_response(
    freqs_hz: np.ndarray, sample_rate: float, phase: float, *, base_hz: float,
    sweep_octaves: float, stages: int, feedback: float, mix: float,
) -> np.ndarray:
    """Mixed response of the all-pass cascade with its LFO held at one phase."""
    center = float(np.clip(base_hz * 2.0 ** (sweep_octaves * math.sin(phase)),
                           10.0, sample_rate * 0.45))
    tangent = math.tan(math.pi * center / sample_rate)
    pole = (tangent - 1.0) / (tangent + 1.0)
    z = np.exp(-2j * np.pi * freqs_hz / sample_rate)
    cascade = ((pole + z) / (1.0 + pole * z)) ** stages
    wet = cascade / (1.0 - feedback * z * cascade)
    return _mixed_response_db(wet, mix)


def modulation_delay_response(
    freqs_hz: np.ndarray, sample_rate: float, phase: float, *,
    base_ms: float, depth_ms: float, voices: int, feedback: float, mix: float,
) -> np.ndarray:
    """Mixed response of the fractional delay taps held at one LFO phase."""
    phases = phase + 2.0 * np.pi * np.arange(voices) / voices
    delays = np.maximum(1.0, (base_ms + depth_ms * np.sin(phases)) * sample_rate / 1000.0)
    whole = np.floor(delays)
    frac = delays - whole
    omega = -2j * np.pi * freqs_hz / sample_rate
    z = np.exp(omega)
    taps = np.exp(np.outer(whole, omega)) * (1.0 - frac[:, None] + frac[:, None] * z)
    combined = taps.mean(axis=0)
    return _mixed_response_db(combined / (1.0 - feedback * combined), mix)


def _mixed_response_db(wet: np.ndarray, mix: float) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(1e-6, np.abs(1.0 - mix + mix * wet)))
