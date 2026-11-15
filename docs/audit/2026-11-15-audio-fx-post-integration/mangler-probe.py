"""Independent Spectral Mangler measurements from the implementation review.

Print actual/expected data; this is not a passing pytest verdict. Run with the
repository Python. --revision 22e6e1c8 loads the reviewed historical source in
a private module without changing the worktree. Default: current source.
No audio device, UI, project or user preset is touched.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from src.features.edit.core.timeline_v2 import SpectralManglerParams

SR = 48000.0


def load_source(revision: str | None) -> ModuleType:
    if revision is None:
        return importlib.import_module("src.features.edit.core.audio_effects.spectral_mangler")
    relative = "src/features/edit/core/audio_effects/spectral_mangler.py"
    source = subprocess.check_output(["git", "show", f"{revision}:{relative}"],
                                     cwd=ROOT, text=True)
    module = ModuleType("topos_audit_mangler_history")
    sys.modules[module.__name__] = module
    exec(compile(source, f"git:{revision}:{relative}", "exec"), module.__dict__)
    return module


def effect(module: ModuleType, **values: object) -> object:
    processor = module.SpectralManglerEffect(SR)
    processor.set_params(SpectralManglerParams(**values))
    return processor


def split(processor: object, samples: np.ndarray,
          sizes: tuple[int, ...] = (256,)) -> np.ndarray:
    chunks = []
    cursor = 0
    index = 0
    while cursor < samples.shape[-1]:
        count = min(sizes[index % len(sizes)], samples.shape[-1] - cursor)
        chunks.append(processor.process(samples[:, cursor:cursor + count]))
        cursor += count
        index += 1
    return np.concatenate(chunks, axis=-1)


def measurements(module: ModuleType) -> dict:
    samples = np.random.default_rng(421).normal(0.0, 0.12, (2, 48000))
    result = {}
    for mode in ("sine", "dual", "fm_ring", "rectify"):
        values = dict(ring_enabled=True, ring_mode=mode,
                      ring_carrier_hz=321.23, ring_lfo_depth_pct=0.0,
                      ring_mix_pct=100.0, global_mix_pct=100.0)
        whole = effect(module, **values).process(samples)
        chunked = split(effect(module, **values), samples)
        result[f"ring_{mode}"] = {
            "max_block_delta": float(np.max(np.abs(whole - chunked))),
            "expected_max_block_delta": 1e-8,
        }
    for curve in ("tape", "fold", "hard_clip", "bitcrush"):
        values = dict(dist_enabled=True, dist_curve=curve, dist_mix_pct=100.0,
                      dist_drive_db=8.0, dist_bias_pct=0.0, global_mix_pct=100.0)
        whole = effect(module, **values).process(samples)
        chunked = split(effect(module, **values), samples)
        t = np.arange(2048) / SR
        left_only = np.stack([0.3 * np.sin(2.0 * np.pi * 1000.0 * t), np.zeros(2048)])
        output = effect(module, **{**values, "dist_drive_db": 0.0}).process(left_only)
        result[f"dist_{curve}"] = {
            "max_block_delta": float(np.max(np.abs(whole - chunked))),
            "expected_max_block_delta": 1e-8,
            "silent_right_peak": float(np.max(np.abs(output[1]))),
            "expected_silent_right_peak": 0.0,
        }
    dist = module._DistModule(SR)
    tone = 0.3 * np.sin(2.0 * np.pi * 1000.0 * np.arange(256) / SR)
    raw = dist._oversample(tone, "tape", 0.0)
    result["fir_block_tail"] = {
        "last_31_samples_peak": float(np.max(np.abs(raw[-31:]))),
        "expected": "Steady input must not acquire 31 padded zeros per process block.",
    }
    for mode in ("cloud", "reverse", "scatter"):
        values = dict(grain_enabled=True, grain_mode=mode, grain_size_ms=500.0,
                      grain_density_hz=80.0, grain_pitch_st=24.0,
                      grain_pitch_jitter_pct=100.0, grain_feedback_pct=0.0,
                      grain_mix_pct=100.0, global_mix_pct=100.0, random_seed=42)
        whole = effect(module, **values).process(samples)
        chunked = split(effect(module, **values), samples)
        result[f"grain_{mode}"] = {
            "max_block_delta": float(np.max(np.abs(whole - chunked))),
            "expected_max_block_delta": 1e-5,
        }
    values = dict(grain_enabled=True, grain_feedback_pct=0.0,
                  grain_mix_pct=100.0, random_seed=43)
    processor = effect(module, **values)
    original = split(processor, samples)
    processor.reset_state()
    replay = split(processor, samples)
    result["reset_seed_replay"] = {
        "max_delta": float(np.max(np.abs(original - replay))), "expected_max_delta": 0.0,
    }
    values = dict(grain_enabled=True, grain_feedback_pct=0.0,
                  grain_mix_pct=100.0, random_seed=7)
    cloud = split(effect(module, **values, grain_mode="cloud"), samples)
    scatter = split(effect(module, **values, grain_mode="scatter"), samples)
    result["cloud_scatter_mode"] = {
        "max_delta": float(np.max(np.abs(cloud - scatter))),
        "expected": "Two exposed processing modes must have defined distinct behavior.",
    }
    values = dict(grain_enabled=True, grain_mix_pct=100.0,
                  grain_pitch_st=0.0, grain_density_hz=40.0, random_seed=43)
    no_feedback = split(effect(module, **values, grain_feedback_pct=0.0), samples, (255, 257))
    feedback = split(effect(module, **values, grain_feedback_pct=95.0), samples, (255, 257))
    result["variable_block_feedback"] = {
        "feedback_0_vs_95_max_delta": float(np.max(np.abs(no_feedback - feedback))),
        "expected": "Feedback must not disappear whenever consecutive block lengths differ.",
    }
    params = SpectralManglerParams(
        grain_enabled=True, grain_pitch_st=12.0, grain_size_ms=85.0,
        grain_position_pct=0.0, grain_spray_pct=0.0, grain_pitch_jitter_pct=0.0,
    )
    grain = module._GrainModule(SR)
    grain._written = 100000
    size = int(params.grain_size_ms * SR / 1000.0)
    grain._trigger(params, size, 2.0, 0.0, 2, writer_now=grain._written)
    voice = next(voice for voice in grain._voices if voice.active)
    positions = voice.read_pos + np.arange(size) * voice.ratio
    result["forward_grain_read_head"] = {
        "initial_lookback_samples": float(grain._written - voice.read_pos),
        "pinned_to_max_position_fraction": float(np.mean(positions > voice.max_pos)),
        "expected": "The complete forward grain should read valid varying source positions, not clamp to one sample.",
    }
    grain = module._GrainModule(SR)
    counters = {"triggers": 0, "full_pool_at_trigger": 0}
    trigger = grain._trigger

    def observe_trigger(*args: object, **kwargs: object) -> None:
        counters["triggers"] += 1
        if not any(not voice.active for voice in grain._voices):
            counters["full_pool_at_trigger"] += 1
        trigger(*args, **kwargs)

    grain._trigger = observe_trigger
    params = SpectralManglerParams(
        grain_enabled=True, grain_density_hz=80.0, grain_size_ms=500.0,
        grain_pitch_st=0.0, grain_feedback_pct=0.0,
    )
    grain.process(np.zeros((2, 48000)), params, 0)
    result["grain_scheduler_pool"] = {
        **counters, "pool_size": len(grain._voices),
        "maximum_physical_overlap": 40,
        "expected_full_pool_at_trigger": 0,
    }
    grain = module._GrainModule(SR)
    grain._written = 400000
    params = SpectralManglerParams(
        grain_enabled=True, grain_mode="reverse", grain_size_ms=500.0,
        grain_pitch_st=24.0, grain_pitch_jitter_pct=100.0,
        grain_position_pct=100.0, grain_spray_pct=0.0,
    )
    maximum_age = 0.0
    maximum_seed = None
    for seed in range(32):
        grain.set_seed(seed)
        for voice in grain._voices:
            voice.active = False
        grain._trigger(params, 24000, 4.0, 600.0, 2, writer_now=grain._written)
        voice = next(voice for voice in grain._voices if voice.active)
        age = grain._written - voice.read_pos + (abs(voice.ratio) + 1.0) * (voice.length - 1)
        if age > maximum_age:
            maximum_age, maximum_seed = age, seed
    result["reverse_grain_history"] = {
        "maximum_required_history_seconds": maximum_age / SR,
        "capacity_seconds": grain._cap / SR,
        "seed": maximum_seed,
        "expected": "Every requested historical sample remains inside the ring throughout the voice lifetime.",
    }
    result["limiter_release"] = {}
    for block in (1, 64, 256, 2000, 48000):
        limiter = module._MiniLimiter(SR)
        limiter.process(np.full((2, 1), 2.0))
        quiet = split(limiter, np.full((2, 48000), 0.1), (block,))
        result["limiter_release"][str(block)] = float(quiet[0, -1] / 0.1)
    result["limiter_release_expected"] = "After 1 second, the same 50ms release envelope for every block size."
    result["limiter_only_active"] = {
        "actual": bool(effect(module, output_limiter_enabled=True).is_active), "expected": True,
    }
    values = dict(ring_enabled=True, ring_mix_pct=0.0, global_mix_pct=0.0)
    processor = effect(module, **values)
    processor.process(np.full((2, 256), 0.2))
    processor.set_params({**SpectralManglerParams(**values).to_dict(), "routing": "parallel"})
    transitioned = processor.process(np.full((2, 64), 0.2))
    result["dry_routing_transition"] = {
        "max_delta_from_dry": float(np.max(np.abs(transitioned - 0.2))), "expected": 0.0,
    }
    processor = effect(module, **values)
    processor.set_params({**SpectralManglerParams(**values).to_dict(), "routing": "parallel"})
    try:
        processor.process(np.full((2, 1), 0.2))
        error = None
    except ValueError as exc:
        error = str(exc)
    result["one_sample_routing_error"] = {"actual": error, "expected": None}
    params = SpectralManglerParams(grain_enabled=True, grain_feedback_pct=0.0,
                                   grain_mix_pct=100.0, random_seed=43)
    changed = effect(module, **params.to_dict())
    changed.set_params(params, sample_rate=44100.0)
    fresh = module.SpectralManglerEffect(44100.0)
    fresh.set_params(params)
    signal = np.random.default_rng(19).normal(0.0, 0.1, (2, 22050))
    result["rate_change_seed"] = {
        "max_delta_from_fresh": float(np.max(np.abs(split(changed, signal) - split(fresh, signal)))),
        "expected": 0.0,
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", default=None)
    args = parser.parse_args()
    print(json.dumps({"source": args.revision or "working-tree", "sample_rate": SR,
                      "measurements": measurements(load_source(args.revision))}, indent=2))


if __name__ == "__main__":
    main()
