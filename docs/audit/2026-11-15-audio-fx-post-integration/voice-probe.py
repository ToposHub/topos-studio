"""Independent numerical probes for the post-integration Voice Changer review.

Run using the repository's Python interpreter. No audio device or persistent
settings are touched. Reported failures are observations, not passing tests.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from src.features.edit.core.audio_effects.voice_changer import (
    VoiceChangerEffect, _estimate_f0,
)
from src.features.edit.core.timeline_v2 import VoiceChangerParams

SR = 48000.0


def render(params: VoiceChangerParams, samples: np.ndarray,
           block: int | None = None) -> tuple[np.ndarray, VoiceChangerEffect]:
    effect = VoiceChangerEffect(SR)
    effect.set_params(params)
    if block is None:
        return effect.process(samples.copy()), effect
    output = np.concatenate([
        effect.process(samples[:, start:start + block].copy())
        for start in range(0, samples.shape[-1], block)
    ], axis=-1)
    return output, effect


def main() -> None:
    result = {}
    rng = np.random.default_rng(44)
    signal = np.zeros((2, 4096))
    signal[:, 0] = 1.0
    output, effect = render(VoiceChangerParams(), signal)
    result["neutral_impulse_at_start"] = {
        "input_peak": 1.0, "output_peak": float(np.max(np.abs(output))),
        "output_energy": float(np.sum(output * output)),
        "reported_latency_samples": effect.latency_samples(),
    }

    params = VoiceChangerParams(formant_mix_pct=0.0)
    effect = VoiceChangerEffect(SR)
    effect.set_params(params)
    effect.process(np.full((2, 2048), 0.4))
    effect.reset_state()
    output = effect.process(np.zeros((2, 2048)))
    result["reset_dry_leak"] = {
        "first_1024_peak": float(np.max(np.abs(output[:, :1024]))),
        "later_peak": float(np.max(np.abs(output[:, 1024:]))),
    }

    signal = rng.normal(0.0, 0.2, (2, 12000))
    for carrier in ("noise", "saw"):
        params = VoiceChangerParams(
            formant_enabled=False, vocoder_enabled=True,
            vocoder_unvoiced_pct=0.0, vocoder_carrier=carrier,
        )
        whole, _ = render(params, signal)
        split, _ = render(params, signal, 480)
        result[f"vocoder_{carrier}_whole_vs_480_max_error"] = float(
            np.max(np.abs(whole - split)))

    signal = np.zeros((2, 12000))
    signal[0] = 0.4 * np.sin(2.0 * np.pi * 440.0 * np.arange(12000) / SR)
    params = VoiceChangerParams(formant_enabled=False, pitch_enabled=True,
                                pitch_semitones=3.0, pitch_formant_link_pct=50.0)
    output, _ = render(params, signal, 1024)
    result["pitch_link_left_to_silent_right"] = {
        "left_peak": float(np.max(np.abs(output[0]))),
        "right_peak": float(np.max(np.abs(output[1]))),
    }

    signal = rng.normal(0.0, 0.15, (2, 6000))
    cases = (
        ("pitch_tracking", "vocal", "wide", VoiceChangerParams(
            formant_enabled=False, pitch_enabled=True, pitch_semitones=3.0)),
        ("vocoder_formant_st", -12.0, 12.0, VoiceChangerParams(
            formant_enabled=False, vocoder_enabled=True, vocoder_carrier="noise")),
        ("pitch_glide_ms", 0.0, 200.0, VoiceChangerParams(
            formant_enabled=False, pitch_enabled=True, pitch_semitones=3.0)),
    )
    for field, lower, upper, base in cases:
        a, _ = render(replace(base, **{field: lower}), signal, 480)
        b, _ = render(replace(base, **{field: upper}), signal, 480)
        result[f"{field}_endpoint_max_difference"] = float(np.max(np.abs(a - b)))

    effect = VoiceChangerEffect(SR)
    effect.set_params(VoiceChangerParams(formant_enabled=False,
                                        pitch_enabled=True, pitch_semitones=3.0))
    sizes = []
    for index in range(100):
        effect.process(np.zeros((2, 480)))
        if index in (0, 9, 49, 99):
            sizes.append({"processed": (index + 1) * 480,
                          "allocated_samples_per_channel": len(effect._pitch._bufs[0])})
    result["pitch_buffer_growth"] = sizes

    signal = np.zeros((2, 24000))
    signal[:, 0] = 0.5
    params = VoiceChangerParams(formant_enabled=False, pitch_enabled=True,
                                pitch_semitones=0.0, pitch_mix_pct=50.0)
    output, effect = render(params, signal)
    indices = np.flatnonzero(np.abs(output[0]) > 1e-6)
    result["pitch_zero_50_percent_mix"] = {
        "nonzero_indices": indices.tolist(), "values": output[0, indices].tolist(),
        "reported_latency_samples": effect.latency_samples(),
    }

    f0 = {}
    for frequency in (220.0, 440.0, 880.0, 1760.0):
        signal = 0.4 * np.sin(2.0 * np.pi * frequency * np.arange(2400) / SR)
        estimate, confidence = _estimate_f0(signal, SR)
        f0[str(frequency)] = {"estimate_hz": estimate, "confidence": confidence}
    result["f0_pure_tones"] = f0

    effect = VoiceChangerEffect(SR)
    effect.set_params(VoiceChangerParams())
    signal = np.zeros((2, 1920))
    signal[:, -1] = 0.5
    first = effect.process(signal)
    tail = effect.process(np.zeros_like(signal))
    result["neutral_ending_impulse"] = {
        "first_block_peak": float(np.max(np.abs(first))),
        "next_zero_block_peak": float(np.max(np.abs(tail))),
        "reported_tail_samples": effect.tail_estimate_samples(),
    }

    signal = np.stack([0.4 * np.sin(2.0 * np.pi * 20000.0 * np.arange(48000) / SR)] * 2)
    output, _ = render(VoiceChangerParams(
        formant_enabled=False, pitch_enabled=True, pitch_semitones=12.0,
    ), signal)
    segment = output[0, 24000:]
    spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment))))
    frequencies = np.fft.rfftfreq(len(segment), 1.0 / SR)
    result["pitch_out_of_band_alias"] = {
        "input_frequency_hz": 20000.0, "pitch_semitones": 12.0,
        "target_frequency_hz": 40000.0,
        "actual_loudest_hz": float(frequencies[np.argmax(spectrum)]),
        "output_rms": float(np.sqrt(np.mean(segment * segment))),
    }
    signal = np.zeros((2, 12000))
    signal[1] = 0.4 * np.sin(2.0 * np.pi * 220.0 * np.arange(12000) / SR)
    _, effect = render(VoiceChangerParams(), signal)
    result["right_only_voice_analysis"] = effect.collect_telemetry()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
