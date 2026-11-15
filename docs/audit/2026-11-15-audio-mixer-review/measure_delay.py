"""Measure Delay parameter discontinuity and near-zero processing cost.

Timing is machine/load dependent; the signal discontinuity is deterministic.
No audio device, project, user preset, or persistent setting is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from time import perf_counter

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from src.features.edit.core.audio_dsp import DelayEffect

SR = 48000.0
PARAMS = {
    "enabled": True, "left_delay": 10.0, "right_delay": 10.0,
    "left_feedback": 0.0, "right_feedback": 0.0,
    "low_cut": 20.0, "high_cut": 20000.0, "high_ratio": 0.0,
    "stereo_blend": 0.0, "dry_wet": 100.0, "level_db": 0.0,
}


def main() -> None:
    signal = np.stack([0.5 * np.sin(2.0 * np.pi * 1000.0 * np.arange(49024) / SR)] * 2)
    effect = DelayEffect(SR)
    effect.set_params(PARAMS)
    before = np.concatenate([
        effect.process(signal[:, index:min(index + 256, 48000)])
        for index in range(0, 48000, 256)
    ], axis=-1)
    effect.set_params({**PARAMS, "left_delay": 10.25, "right_delay": 10.25})
    after = effect.process(signal[:, 48000:])
    timings = []
    noise = np.random.default_rng(3).normal(0.0, 0.1, (2, 2000))
    for milliseconds in (0.0, 0.01, 0.1, 1.0, 10.0, 121.0):
        effect = DelayEffect(SR)
        effect.set_params({**PARAMS, "left_delay": milliseconds,
                           "right_delay": milliseconds, "left_feedback": 30.0,
                           "right_feedback": 30.0, "dry_wet": 30.0})
        effect.process(noise)
        elapsed = []
        for _ in range(5):
            start = perf_counter()
            effect.process(noise)
            elapsed.append((perf_counter() - start) * 1000.0)
        timings.append({"delay_ms": milliseconds, "median_process_ms": float(np.median(elapsed))})
    print(json.dumps({
        "sample_rate": SR, "block_samples": 2000,
        "block_budget_ms": 2000.0 / SR * 1000.0,
        "parameter_change": "10 -> 10.25 ms, 1 kHz, amplitude 0.5",
        "boundary_step": float(abs(after[0, 0] - before[0, -1])),
        "steady_max_step": float(np.max(np.abs(np.diff(before[0, -1000:])))),
        "timings": timings,
    }, indent=2))


if __name__ == "__main__":
    main()
