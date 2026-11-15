"""Independent Voice regressions from the post-integration audio review."""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_effects.voice_changer import VoiceChangerEffect
from src.features.edit.core.timeline_v2 import VoiceChangerParams

SR = 48000.0


def _effect(params: VoiceChangerParams) -> VoiceChangerEffect:
    effect = VoiceChangerEffect(SR)
    effect.set_params(params)
    return effect


def _split(effect: VoiceChangerEffect, samples: np.ndarray,
           block_sizes: tuple[int, ...]) -> np.ndarray:
    chunks = []
    position = 0
    index = 0
    while position < samples.shape[-1]:
        count = block_sizes[index % len(block_sizes)]
        chunks.append(effect.process(samples[:, position:position + count]))
        position += count
        index += 1
    return np.concatenate(chunks, axis=-1)


@pytest.mark.parametrize("mix", [0.0, 50.0])
def test_voice_reset_clears_queued_dry_audio_and_analysis(mix: float) -> None:
    params = VoiceChangerParams(formant_mix_pct=mix)
    effect = _effect(params)
    effect.process(np.full((2, 2048), 0.4))
    effect.reset_state()
    telemetry = effect.collect_telemetry()
    assert telemetry["in_rms"] == 0.0
    assert telemetry["out_rms"] == 0.0
    assert telemetry["confidence"] == 0.0
    assert telemetry["f0_in_hz"] is None
    silence = np.zeros((2, 2048))
    np.testing.assert_array_equal(effect.process(silence),
                                   _effect(params).process(silence))


@pytest.mark.parametrize("source_channel", [0, 1])
def test_pitch_link_keeps_silent_opposite_channel_silent(source_channel: int) -> None:
    samples = np.zeros((2, 12288))
    samples[source_channel] = 0.4 * np.sin(
        2.0 * np.pi * 440.0 * np.arange(samples.shape[-1]) / SR)
    effect = _effect(VoiceChangerParams(
        formant_enabled=False, pitch_enabled=True, pitch_semitones=3.0,
        pitch_formant_link_pct=50.0,
    ))
    output = _split(effect, samples, (1024,))
    assert np.max(np.abs(output[source_channel])) > 0.01
    assert np.max(np.abs(output[1 - source_channel])) == 0.0


@pytest.mark.parametrize("carrier", ["saw", "pulse", "choir", "noise"])
@pytest.mark.parametrize("unvoiced", [0.0, 72.0])
def test_vocoder_output_does_not_depend_on_audio_block_size(
    carrier: str, unvoiced: float,
) -> None:
    rng = np.random.default_rng(44)
    samples = rng.normal(0.0, 0.2, (2, 9000))
    # Include release tails, independent channel content, and a clear boundary
    # transient so all three filter-state paths have meaningful history.
    samples[:, 5000:] = 0.0
    samples[0, 1200] = 0.8
    params = VoiceChangerParams(
        formant_enabled=False, vocoder_enabled=True, vocoder_bands=8,
        vocoder_carrier=carrier, vocoder_unvoiced_pct=unvoiced,
        vocoder_release_ms=170.0, random_seed=93,
    )
    expected = _effect(params).process(samples.copy())
    actual = _split(_effect(params), samples.copy(), (17, 257, 480, 1153, 2048))
    assert np.max(np.abs(expected)) > 1e-4
    np.testing.assert_allclose(actual, expected, rtol=0.0, atol=1e-9)
