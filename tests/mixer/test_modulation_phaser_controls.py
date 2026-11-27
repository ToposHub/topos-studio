from __future__ import annotations

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

from src.features.edit.core.audio_dsp import ALGORITHM_VERSION, EffectChain
from src.features.edit.core.audio_effects.modulation_analysis import (
    modulation_delay_response,
    phaser_response,
)
from src.features.edit.core.audio_effects.modulation_tools import PhaserEffect
from src.features.edit.core.timeline_v2 import EffectChainParams, PhaserParams, TrackEffectInstance
from src.features.mixer.effects.production_dialog import ProductionEffectDialog
from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget


def test_stereo_phaser_preserves_antiphase_audio_and_stream_history() -> None:
    mono = np.random.default_rng(112).normal(0, 0.1, 4491).astype(np.float32)
    anti = np.stack((mono, -mono))
    assert PhaserParams.default().stereo_processing
    assert not PhaserParams.from_dict({}).stereo_processing

    params = {"mix": 1.0, "feedback": -0.45, "stages": 9}
    old = PhaserEffect()
    old.set_params(params)
    np.testing.assert_array_equal(old.process(anti), np.zeros_like(anti))

    wide = PhaserEffect()
    wide.set_params(params | {"stereo_processing": True})
    whole = wide.process(anti)
    assert np.max(np.abs(whole)) > 0.05
    np.testing.assert_array_equal(whole[0], -whole[1])
    wide.reset_state()
    chunks = np.split(anti, [1, 256, 541, 2150, 4013], axis=1)
    np.testing.assert_allclose(
        np.concatenate([wide.process(part) for part in chunks], axis=1),
        whole, atol=1e-7, rtol=0)


@pytest.mark.parametrize("version", [4, ALGORITHM_VERSION])
def test_cancel_sparse_phaser_restores_engine_defaults(version: int) -> None:
    data = np.random.default_rng(299).normal(0.0, 0.12, (2, 2000)).astype(np.float32)
    original = EffectChain()
    original.set_params([("phaser", {}, True, "fx", version)])
    dry_snapshot = original.process(data)
    edited = {"stages": 2, "feedback": 0.9, "mix": 1.0, "stereo_processing": True}
    original.set_params([("phaser", edited, True, "fx", version)])
    assert np.max(np.abs(original.process(data) - dry_snapshot)) > 0.01
    original.set_params([("phaser", {}, True, "fx", version)])
    original.reset_state()
    np.testing.assert_array_equal(original.process(data), dry_snapshot)


def test_static_phaser_response_matches_measured_impulse_fft() -> None:
    sr, n = 48000, 8192
    params = {"base_hz": 900.0, "sweep_octaves": 0.0, "stages": 9,
              "feedback": -0.45, "mix": 0.65}
    pulse = np.zeros((1, n), dtype=np.float32)
    pulse[0, 0] = 1.0
    effect = PhaserEffect(sr)
    effect.set_params(params)
    measured = 20 * np.log10(np.abs(np.fft.rfft(effect.process(pulse)[0])))
    estimate = phaser_response(np.fft.rfftfreq(n, 1 / sr), sr, 1.7, **params)
    np.testing.assert_allclose(estimate, measured, rtol=0, atol=2e-5)


def test_fractional_modulation_response_matches_static_delay() -> None:
    sr, n = 48000, 4096
    params = {"base_ms": 1.07, "depth_ms": 0.0, "voices": 2, "feedback": -0.45, "mix": 0.35}
    pulse = np.zeros((1, n), dtype=np.float32)
    pulse[0, 0] = 1.0
    chain = EffectChain(sr)
    chain.set_params([("chorus_flanger", params, True, "fx", ALGORITHM_VERSION)])
    measured = 20 * np.log10(np.abs(np.fft.rfft(chain.process(pulse)[0])))
    estimate = modulation_delay_response(np.fft.rfftfreq(n, 1 / sr), sr, 0.0, **params)
    np.testing.assert_allclose(estimate, measured, rtol=0, atol=2e-5)


def test_phaser_allpass_tail_budget_with_zero_feedback() -> None:
    effect = PhaserEffect()
    effect.set_params({"base_hz": 60.0, "sweep_octaves": 0, "stages": 12, "feedback": 0.0})
    samples = np.zeros((1, 48000), dtype=np.float32)
    samples[0, 0] = 1.0
    response = effect.process(samples)[0]
    tail = effect.tail_estimate_samples()
    assert tail > 512
    assert np.max(np.abs(response[tail:])) < np.max(np.abs(response)) * 1e-3


def test_phaser_rack_controls_preview_and_plot_current_sample_rate(qtbot: QtBot) -> None:
    strip = TrackMixerWidget("fx", "Audio")
    qtbot.addWidget(strip)
    strip._effect_add("phaser")
    instance = strip._effect_chain.effects[0]
    assert instance.params["stereo_processing"]
    strip.set_engine_sample_rate(44100)
    strip._open_insert_effect(instance)
    _, _, dialog = next(strip._iter_insert_dialogs())
    try:
        graph = dialog.panel.mod_visualizer
        assert graph._sample_rate == 44100
        assert graph._norm_to_rate(1.0) == 20.0
        assert graph._norm_to_rate(0.0) == 0.01

        card = dialog.panel.phaser_card
        with qtbot.waitSignal(dialog.preview, timeout=1000):
            card.stereo_button.click()
        assert dialog.current_params()["stereo_processing"] is False
        card.stage_btns[12].click()
        assert dialog.current_params()["stages"] == 12
        with qtbot.waitSignal(dialog.committed, timeout=1000):
            dialog.apply()
        chain = EffectChain(44100)
        current = strip._effect_chain.effects[0]
        chain.set_params([("phaser", current.params, True, instance.instance_id, ALGORITHM_VERSION)])
        chain.process(np.full((2, 512), 0.1, dtype=np.float32))
        taps = chain.tap_levels()
        strip.update_track_telemetry({"taps": taps})
        assert graph._phase == taps[instance.instance_id]["analysis"]["phase"] > 0.0
        assert not graph.grab().isNull()
        assert max(graph._comb_curve_db) - min(graph._comb_curve_db) > 10
    finally:
        dialog.close()


def test_phaser_legacy_panel_keeps_saved_values_and_uses_legacy_control_limits(qtbot: QtBot) -> None:
    params = {"stages": 8, "feedback": -0.25, "mix": 0.7, "stereo_processing": True}
    dialog = ProductionEffectDialog("phaser", "Audio", params, algorithm_version=4)
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.current_params() == params
    card = dialog.panel.phaser_card
    assert card.stereo_button.isHidden()
    assert card.stage_btns[8].isHidden()
    assert card.stage_btns[4].isChecked()
    assert dialog.panel.get_state()["phaser"]["feedback"] == 0.0
    dialog.set_algorithm_version(ALGORITHM_VERSION)
    dialog.set_current_params(params)
    assert dialog.current_params() == params
    assert not card.stereo_button.isHidden()
    assert card.stereo_button.isChecked()
    assert card.stage_btns[8].isChecked()
    assert dialog.panel.get_state()["phaser"]["feedback"] == -25.0
    dialog.set_algorithm_version(4)
    assert card.stereo_button.isHidden()
    assert card.stage_btns[4].isChecked()
    dialog.close()


def test_phaser_empty_saved_rack_snapshot_does_not_enable_stereo(qtbot: QtBot) -> None:
    strip = TrackMixerWidget("fx", "Audio")
    qtbot.addWidget(strip)
    instance = TrackEffectInstance("phaser", {}, algorithm_version=ALGORITHM_VERSION)
    strip.set_effect_chain(EffectChainParams((instance,)))
    strip._open_insert_effect(instance)
    _, _, dialog = next(strip._iter_insert_dialogs())
    try:
        assert not dialog.panel.phaser_card.stereo_button.isChecked()
        state = dialog.panel.get_state()
        state["phaser"]["mix"] = 65.0
        dialog.panel.set_state(state)
        dialog.panel.stateChanged.emit(dialog.panel.get_state())
        assert dialog.current_params() == {"mix": 0.65}
        dialog.apply()
        assert strip._effect_chain.effects[0].params == {"mix": 0.65}
    finally:
        dialog.close()
