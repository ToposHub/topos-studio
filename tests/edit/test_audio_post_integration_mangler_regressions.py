"""Independent Spectral Mangler regressions from the integration review."""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import EffectChain
from src.features.edit.core.audio_effects.spectral_mangler import SpectralManglerEffect
from src.features.edit.core.timeline_v2 import SpectralManglerParams


SR = 48000.0


def _effect(**values: object) -> SpectralManglerEffect:
    effect = SpectralManglerEffect(SR)
    effect.set_params(SpectralManglerParams(**values))
    return effect


def _split(effect: SpectralManglerEffect, samples: np.ndarray,
           sizes: tuple[int, ...]) -> np.ndarray:
    chunks = []
    cursor = 0
    count = 0
    while cursor < samples.shape[-1]:
        size = min(sizes[count % len(sizes)], samples.shape[-1] - cursor)
        chunks.append(effect.process(samples[:, cursor:cursor + size]))
        cursor += size
        count += 1
    return np.concatenate(chunks, axis=-1)


@pytest.mark.parametrize("mode", ["dual", "fm_ring"])
@pytest.mark.parametrize("lfo_depth", [0.0, 73.0])
def test_ring_noninteger_oscillator_phases_are_block_invariant(
    mode: str, lfo_depth: float,
) -> None:
    samples = np.random.default_rng(421).normal(0.0, 0.2, (2, 16384))
    values = dict(
        ring_enabled=True, ring_mode=mode, ring_carrier_hz=347.123,
        ring_lfo_rate_hz=4.3, ring_lfo_depth_pct=lfo_depth,
        ring_warp_pct=72.0, ring_spread_pct=63.0, ring_mix_pct=100.0,
        global_mix_pct=100.0,
    )
    whole = _effect(**values).process(samples)
    split = _split(_effect(**values), samples, (1, 61, 257, 1297, 13, 503))
    assert np.max(np.abs(whole)) > 0.05
    np.testing.assert_allclose(split, whole, rtol=0.0, atol=1e-9)


@pytest.mark.parametrize("mode", ["cloud", "reverse"])
def test_reset_replays_same_seed_grain_sequence(mode: str) -> None:
    samples = np.random.default_rng(19).normal(0.0, 0.2, (2, 24000))
    effect = _effect(
        grain_enabled=True, grain_mode=mode, grain_density_hz=24.0,
        grain_size_ms=85.0, grain_pitch_st=5.0, grain_pitch_jitter_pct=80.0,
        grain_spray_pct=90.0, grain_feedback_pct=0.0, grain_mix_pct=100.0,
        global_mix_pct=100.0, random_seed=43,
    )
    first = _split(effect, samples, (256,))
    assert np.max(np.abs(first)) > 0.001
    effect.reset_state()
    replay = _split(effect, samples, (256,))
    np.testing.assert_array_equal(replay, first)


def test_limiter_only_instance_is_processed_by_effect_chain() -> None:
    params = SpectralManglerParams(output_limiter_enabled=True)
    chain = EffectChain(SR)
    chain.set_params([("spectral_mangler", params.to_dict(), True, "limit-only", 4)])
    samples = np.full((2, 256), 2.0, dtype=np.float64)
    assert chain.is_active
    limited = chain.process(samples)
    np.testing.assert_array_equal(limited, np.ones_like(samples))


def test_limiter_disabled_neutral_instance_remains_inactive() -> None:
    assert not _effect().is_active


def test_sample_rate_rebuild_retains_persisted_grain_seed() -> None:
    params = SpectralManglerParams(
        grain_enabled=True, grain_feedback_pct=0.0, grain_mix_pct=100.0,
        grain_pitch_jitter_pct=90.0, grain_spray_pct=90.0, random_seed=43,
    )
    switched = _effect(**params.to_dict())
    switched.set_params(params, sample_rate=44100.0)
    fresh = SpectralManglerEffect(44100.0)
    fresh.set_params(params)
    samples = np.random.default_rng(17).normal(0.0, 0.2, (2, 22050))
    np.testing.assert_array_equal(_split(switched, samples, (256,)),
                                  _split(fresh, samples, (256,)))


@pytest.mark.parametrize("global_mix", [0.0, 100.0])
def test_routing_transition_is_block_invariant_including_single_samples(
    global_mix: float,
) -> None:
    params = SpectralManglerParams(
        ring_enabled=True, ring_mode="sine", ring_carrier_hz=321.23,
        ring_mix_pct=100.0, global_mix_pct=global_mix,
    )
    prefix = np.full((2, 512), 0.2)
    samples = np.full((2, 1024), 0.2)

    def prepare() -> SpectralManglerEffect:
        effect = _effect(**params.to_dict())
        effect.process(prefix)
        effect.set_params({**params.to_dict(), "routing": "parallel"})
        return effect

    whole = prepare().process(samples)
    split = _split(prepare(), samples, (1, 17, 61, 3, 5))
    np.testing.assert_allclose(split, whole, rtol=0.0, atol=1e-12)
    if global_mix == 0.0:
        np.testing.assert_array_equal(whole, samples)


def test_routing_transition_accepts_params_or_dict_consistently() -> None:
    params = SpectralManglerParams(
        ring_enabled=True, ring_mode="sine", ring_mix_pct=100.0,
        global_mix_pct=100.0,
    )
    samples = np.full((2, 512), 0.2)
    outputs = []
    for typed in (False, True):
        effect = _effect(**params.to_dict())
        effect.process(samples)
        changed = {**params.to_dict(), "routing": "parallel"}
        effect.set_params(SpectralManglerParams(**changed) if typed else changed)
        outputs.append(effect.process(samples))
    np.testing.assert_array_equal(*outputs)
