"""Independent DSP regressions from the 2026-11-15 maturity review.

Exercise audible output and block-size invariance, rather than only checking
that implementation buffers or parameter coefficients still exist.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import EffectChain, ReverbEffect


SR = 48000.0


def _reverb(**overrides: float) -> tuple[ReverbEffect, dict]:
    params = ReverbEffect._default_params_dict()
    params.update({
        "enabled": True, "modulation": 0.0, "dry_wet": 100.0,
        "direct_db": -60.0, "er_db": 0.0, "reverb_db": 0.0,
        "er_lo_gain": 0.0, "er_hi_gain": 0.0,
        "rev_lo_gain": 0.0, "rev_hi_gain": 0.0,
    })
    params.update(overrides)
    effect = ReverbEffect(SR)
    effect.set_params(params)
    return effect, params


def _chunks(effect: ReverbEffect, samples: np.ndarray,
            sizes: tuple[int, ...]) -> np.ndarray:
    pieces = []
    position = 0
    index = 0
    while position < samples.shape[-1]:
        count = sizes[index % len(sizes)]
        pieces.append(effect.process(samples[:, position:position + count]))
        position += count
        index += 1
    return np.concatenate(pieces, axis=-1)


def test_long_room_reflections_do_not_wrap_into_early_audio() -> None:
    effect, _ = _reverb(
        height=2.0, length=3.0, width=80.0, pre_delay=200.0,
        distance=30.0, reverb_db=-60.0,
    )
    impulse = np.zeros((2, 48000), dtype=np.float64)
    impulse[:, 0] = 1.0
    output = _chunks(effect, impulse, (256,))

    # All ER paths include the source-to-listener propagation plus pre-delay.
    # Allow the model's +/-2% tap decorrelation. A reflection at sample 697,
    # produced by modulo wrapping in the old buffer, violates this lower bound.
    earliest = int((0.2 + 30.0 / 343.0) * SR * 0.97)
    assert np.max(np.abs(output[:, :earliest])) < 1e-12
    assert np.max(np.abs(output[:, earliest:])) > 0.01


@pytest.mark.parametrize("room", [
    {"height": 30.0, "length": 80.0, "width": 80.0},
    {"height": 2.0, "length": 3.0, "width": 80.0},
])
@pytest.mark.parametrize("sizes", [(256,), (2048,), (71, 817, 4093, 113, 2048)])
def test_reverb_large_room_output_is_block_size_invariant(
    room: dict[str, float], sizes: tuple[int, ...],
) -> None:
    rng = np.random.default_rng(29)
    samples = rng.normal(0.0, 0.1, (2, 48000))
    whole, _ = _reverb(**room, pre_delay=200.0, distance=30.0,
                       modulation=30.0)
    split, _ = _reverb(**room, pre_delay=200.0, distance=30.0,
                       modulation=30.0)
    expected = whole.process(samples.copy())
    actual = _chunks(split, samples.copy(), sizes)
    np.testing.assert_allclose(actual, expected, rtol=0.0, atol=1e-9)


@pytest.mark.parametrize("room", [
    {"height": 2.0, "length": 3.0, "width": 3.0,
     "pre_delay": 0.0, "distance": 0.0},
    {"height": 10.0, "length": 30.0, "width": 40.0,
     "pre_delay": 200.0, "distance": 30.0},
])
def test_increasing_block_size_preserves_warmed_reverb_history(
    room: dict[str, float],
) -> None:
    rng = np.random.default_rng(41)
    prefix = rng.normal(0.0, 0.1, (2, 24000))
    large, _ = _reverb(**room, reverb_db=-60.0)
    small, _ = _reverb(**room, reverb_db=-60.0)
    # 239-sample blocks exercise a nonzero write pointer, including non-power-of-
    # two capacities determined by actual reflection paths.
    _chunks(large, prefix.copy(), (239,))
    _chunks(small, prefix.copy(), (239,))
    silence = np.zeros((2, 24000))
    expected = _chunks(small, silence.copy(), (239,))
    actual = large.process(silence.copy())
    assert np.max(np.abs(expected)) > 0.01
    np.testing.assert_allclose(actual, expected, rtol=0.0, atol=1e-10)


def test_geometry_growth_preserves_recent_input_for_new_er_paths() -> None:
    small, params = _reverb(
        height=2.0, length=3.0, width=3.0, pre_delay=0.0,
        distance=0.0, reverb_db=-60.0,
    )
    reference, _ = _reverb(
        height=2.0, length=3.0, width=3.0, pre_delay=0.0,
        distance=0.0, reverb_db=-60.0,
    )
    prefix = np.zeros((2, 48000))
    # Meaningful history is entirely recent, so no unavailable older samples
    # need to be reconstructed when the small ring grows.
    prefix[:, -1024:] = 0.1
    _chunks(small, prefix.copy(), (239,))
    reference.process(prefix.copy())  # Already has capacity for a large block.
    updated = {
        **params, "height": 30.0, "length": 3.0, "width": 80.0,
        "pre_delay": 200.0, "distance": 30.0,
    }
    small.set_params(updated)
    reference.set_params(updated)
    silence = np.zeros((2, 32768))
    actual = _chunks(small, silence.copy(), (256,))
    expected = _chunks(reference, silence.copy(), (256,))
    assert np.max(np.abs(expected)) > 0.01
    np.testing.assert_allclose(actual, expected, rtol=0.0, atol=1e-10)


def test_lower_sample_rate_does_not_reuse_old_rate_ring_history() -> None:
    effect, params = _reverb(reverb_db=-60.0)
    _chunks(effect, np.full((2, 24000), 0.1), (239,))
    effect.set_params(params, sample_rate=24000.0)
    output = _chunks(effect, np.zeros((2, 24000)), (256,))
    np.testing.assert_array_equal(output, 0.0)


@pytest.mark.parametrize("enabled,makeup_db,expected_gain", [
    (True, 6.0, 10.0 ** (6.0 / 20.0)),
    (True, 0.0, 1.0),
    (False, 6.0, 1.0),
])
def test_dynamics_makeup_remains_independent_of_submodule_bypass(
    enabled: bool, makeup_db: float, expected_gain: float,
) -> None:
    params = {
        "enabled": enabled,
        "eg": {"enabled": False},
        "comp": {"enabled": False, "makeup_gain": makeup_db},
        "lim": {"enabled": False},
    }
    chain = EffectChain(SR)
    chain.set_params([("dynamics", params, True, "dynamics-1")])
    samples = np.full((2, 128), 0.1)
    np.testing.assert_allclose(chain.process(samples.copy()),
                               samples * expected_gain, rtol=0.0, atol=1e-12)


@pytest.mark.parametrize("chain_enabled,params_enabled", [
    (False, True), (True, False), (False, False),
])
def test_either_bypass_level_clears_gain_reduction_and_detector(
    chain_enabled: bool, params_enabled: bool,
) -> None:
    params = {
        "enabled": True, "threshold_db": -30.0, "ratio": 4.0,
        "knee": 0.0, "mix": 100.0,
    }
    chain = EffectChain(SR)
    chain.set_params([("compressor", params, True, "comp-1")])
    chain.process(np.full((2, 4096), 0.9))
    assert chain.collect_gain_reduction()["comp-1"] > 10.0
    assert chain.collect_detector_levels()["comp-1"] > -10.0
    chain.set_params([("compressor", {**params, "enabled": params_enabled},
                       chain_enabled, "comp-1")])
    # Query both immediately after bypass and after processing another block;
    # bypassed processors are skipped and do not reset their own last values.
    assert chain.collect_gain_reduction()["comp-1"] == 0.0
    assert chain.collect_detector_levels()["comp-1"] == -100.0
    chain.process(np.zeros((2, 128)))
    assert chain.collect_gain_reduction()["comp-1"] == 0.0
    assert chain.collect_detector_levels()["comp-1"] == -100.0


def test_serial_delay_tail_budget_includes_both_delays() -> None:
    params = {
        "enabled": True, "left_delay": 80.0, "right_delay": 80.0,
        "left_feedback": 0.0, "right_feedback": 0.0,
        "dry_wet": 100.0,
    }
    chain = EffectChain(SR)
    chain.set_params([
        ("delay", params, True, "delay-a"),
        ("delay", params, True, "delay-b"),
    ])
    assert int(0.160 * SR) <= chain.tail_estimate_samples() < int(0.161 * SR)
    chain.set_params([
        ("delay", params, True, "delay-a"),
        ("delay", params, False, "delay-b"),
    ])
    assert int(0.080 * SR) <= chain.tail_estimate_samples() < int(0.081 * SR)
    chain.set_params([])
    assert chain.tail_estimate_samples() == 0


def test_serial_tail_estimates_ignore_nonfinite_or_invalid_nodes() -> None:
    class _TailNode:
        def __init__(self, value: object) -> None:
            self.value = value

        def tail_estimate_samples(self) -> object:
            return self.value

    chain = EffectChain(SR)
    values = [13, 27, np.nan, np.inf, -np.inf, -5, "invalid", 0.5, 1000]
    chain._effects = [_TailNode(value) for value in values]
    chain._enabled_flags = [True] * (len(values) - 1) + [False]
    # Two serial tails and a conservative round-up of a fractional estimate;
    # invalid values and the disabled node do not enter the scheduling budget.
    assert chain.tail_estimate_samples() == 41


def test_mixer_drains_serial_delays_until_the_160ms_echo() -> None:
    from src.features.edit.core.audio_playback import AudioMixer
    from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance

    class _ImpulseDecoder:
        _channels = 2

        def decode_samples_for_frame(self, frame: int, fps: float,
                                     num_samples: int) -> np.ndarray | None:
            if frame != 0:
                return None
            block = np.zeros((2, num_samples), dtype=np.float32)
            block[:, 0] = 0.5
            return block

        def close(self) -> None:
            pass

    params = {
        "enabled": True, "left_delay": 80.0, "right_delay": 80.0,
        "left_feedback": 0.0, "right_feedback": 0.0,
        "dry_wet": 100.0, "level_db": 0.0,
        "low_cut": 20.0, "high_cut": 18000.0,
        "high_ratio": 0.0, "stereo_blend": 0.0,
    }
    mixer = AudioMixer()
    mixer.set_sample_rate(SR)
    mixer.add_track("track-1", _ImpulseDecoder())
    mixer.set_track_effect_chain("track-1", EffectChainParams(effects=tuple(
        TrackEffectInstance(effect_type="delay", params=dict(params),
                            enabled=True, order=index, instance_id=f"delay-{index}")
        for index in range(2)
    )))
    blocks = [mixer.mix_samples(frame, 100.0, 480, 2)[0]
              for frame in range(24)]
    output = np.concatenate(blocks, axis=-1)
    first_echo = int(0.160 * SR)
    assert np.max(np.abs(output[:, :first_echo])) < 1e-8
    assert np.max(np.abs(output[:, first_echo:first_echo + 64])) > 0.05
