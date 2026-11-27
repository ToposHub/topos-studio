"""Voice Changer native channels, stream continuity, and scoped monitoring."""
from __future__ import annotations

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

from src.features.edit.core.audio_dsp import EffectChain
from src.features.edit.core.audio_effects.voice_changer import VoiceChangerEffect, _PitchModule
from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance, VoiceChangerParams
from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel


@pytest.mark.parametrize("channels", [1, 6])
def test_voice_changer_keeps_every_native_channel_independent(channels: int) -> None:
    pcm = np.zeros((channels, 9600), dtype=np.float32)
    pcm[-1] = 0.25 * np.sin(2 * np.pi * 180.0 * np.arange(pcm.shape[-1]) / 48000.0)
    effect = VoiceChangerEffect()
    effect.set_params({"formant_enabled": True, "formant_shift_st": 2.0,
                       "pitch_enabled": True, "pitch_semitones": -4.0,
                       "vocoder_enabled": True, "vocoder_carrier": "noise",
                       "vocoder_bands": 8, "vocoder_unvoiced_pct": 0.0})
    output = effect.process(pcm)
    assert output.shape == pcm.shape
    assert output.dtype == pcm.dtype
    assert np.max(np.abs(output[-1])) > 0.001
    assert np.all(output[:-1] == 0)


def test_voice_pitch_advances_its_read_head_for_single_sample_blocks() -> None:
    params = VoiceChangerParams(formant_enabled=False, pitch_enabled=True, pitch_formant_link_pct=100.0)
    signal = np.random.default_rng(8).normal(0.0, 0.2, 320)
    full = _PitchModule(1000)
    stream = _PitchModule(1000)
    expected = full._process_mono(signal, params, 0)
    actual = np.concatenate([stream._process_mono(signal[i:i + 1], params, i) for i in range(signal.size)])
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(actual[stream.latency:], signal[:-stream.latency])


def test_near_maximum_pitch_link_respects_the_reported_dry_wet_latency() -> None:
    effect = VoiceChangerEffect()
    effect.set_params({"formant_enabled": False, "pitch_enabled": True,
                       "pitch_formant_link_pct": 99.95, "pitch_mix_pct": 50.0})
    signal = np.zeros((1, 10000), dtype=np.float32)
    signal[0, 0] = 0.5
    output = effect.process(signal)
    assert effect.latency_samples() == 5280
    np.testing.assert_array_equal(np.flatnonzero(output[0]), [5280])
    assert output[0, 5280] == 0.5


def test_voice_glide_retargets_from_its_current_pitch() -> None:
    module = _PitchModule(48000)
    start = VoiceChangerParams(pitch_semitones=0.0, pitch_glide_ms=100.0)
    up = VoiceChangerParams(pitch_semitones=12.0, pitch_glide_ms=100.0)
    down = VoiceChangerParams(pitch_semitones=-12.0, pitch_glide_ms=100.0)
    module._ratio(start, 1, 0)
    module._ratio(up, 100, 100)
    boundary = module._ratio(up, 1, 2500)[0]
    assert module._ratio(down, 1, 2500)[0] == pytest.approx(boundary)
    assert module._ratio(down, 1, 7300)[0] == pytest.approx(0.5)


def test_voice_sample_rate_change_reseeds_and_resets_stream_position() -> None:
    params = {"formant_enabled": False, "pitch_enabled": True, "pitch_semitones": -4.0,
              "pitch_jitter_pct": 80.0, "vocoder_enabled": True, "vocoder_carrier": "noise",
              "vocoder_bands": 8, "random_seed": 272752}
    pcm = np.random.default_rng(14).normal(0.0, 0.1, (2, 9600)).astype(np.float32)
    rebuilt = VoiceChangerEffect(48000)
    rebuilt.set_params(params)
    rebuilt.process(pcm)
    rebuilt.set_params(params, sample_rate=44100)
    fresh = VoiceChangerEffect(44100)
    fresh.set_params(params)
    np.testing.assert_array_equal(rebuilt.process(pcm), fresh.process(pcm))
    assert rebuilt.collect_telemetry()["sample_position"] == pcm.shape[-1]


def test_voice_vocoder_release_reports_a_tail_without_adding_latency() -> None:
    effect = VoiceChangerEffect()
    effect.set_params({"formant_enabled": False, "vocoder_enabled": True, "vocoder_release_ms": 60.0})
    assert effect.latency_samples() == 0
    assert effect.tail_estimate_samples() >= 48000 * 0.06 * 6.0


def test_voice_f0_tracking_works_for_short_blocks_and_the_wide_output_range() -> None:
    for frequency in (90.0, 880.0):
        effect = VoiceChangerEffect()
        effect.set_params({"formant_enabled": False, "output_gain_db": -3.0, "pitch_tracking": "wide"})
        for offset in range(0, 9600, 480):
            wave = 0.2 * np.sin(2 * np.pi * frequency * (offset + np.arange(480)) / 48000.0)
            effect.process(np.stack((np.zeros_like(wave), wave)))
        telemetry = effect.collect_telemetry()
        assert telemetry["f0_in_hz"] == pytest.approx(frequency, rel=0.03)
        assert telemetry["f0_out_hz"] == pytest.approx(frequency, rel=0.03)
        assert telemetry["ref_channel_in"] == telemetry["ref_channel_out"] == 1


def test_voice_monitoring_is_bound_to_visible_instance_and_clears(qtbot: QtBot) -> None:
    panel = AudioMixerPanel()
    qtbot.addWidget(panel)
    panel.register_track("track", "Track", "A1")
    strip = panel._track_mixers["track"]
    chain_params = EffectChainParams((
        TrackEffectInstance("voice_changer", {"formant_enabled": False}, instance_id="voice"),
        TrackEffectInstance("voice_changer", {"formant_enabled": True}, instance_id="other", order=1),
    ))
    strip.set_effect_chain(chain_params)
    chain = EffectChain(48000, realtime_resources=True)
    chain.set_params(chain_params.to_dsp_entries())
    time = np.arange(4800) / 48000.0
    signal = np.stack([0.2 * np.sin(2 * np.pi * 220.0 * time)] * 2).astype(np.float32)
    hidden = chain.process(signal)
    assert all("analysis" not in tap for tap in chain.tap_levels().values())
    chain.reset_state()

    strip._open_voice_changer_editor(chain_params.effects[0])
    dialog = strip._voice_changer_dialogs["voice"]
    try:
        assert panel.spectrum_targets() == {"track": frozenset({"voice"})}
        assert dialog.panel.voice_visualizer._f0_in_hz == 0.0
        chain.set_spectrum_instances(panel.spectrum_targets()["track"])
        np.testing.assert_array_equal(chain.process(signal), hidden)
        qtbot.waitUntil(lambda: bool(chain.tap_levels()["voice"]["spectrum"]), timeout=2000)
        taps = chain.tap_levels()
        assert "analysis" not in taps["other"]
        strip.update_track_telemetry({"taps": taps})
        graph = dialog.panel.voice_visualizer
        assert graph._freqs == taps["voice"]["spectrum"]["freqs"]
        assert graph._f0_in_hz == pytest.approx(220.0, abs=2.0)
        assert graph._f0_out_hz == pytest.approx(220.0, abs=2.0)
        strip.update_track_telemetry({"silent": True})
        assert graph._freqs == []
        assert graph._f0_in_hz == 0.0
        assert dialog.panel.meter_input._peak == -55.0
    finally:
        dialog.close()
    assert panel.spectrum_targets() == {}
    chain.set_spectrum_instances(frozenset())
    chain.process(signal)
    assert all("analysis" not in tap for tap in chain.tap_levels().values())
