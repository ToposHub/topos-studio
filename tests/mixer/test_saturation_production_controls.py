from __future__ import annotations

import numpy as np
import pytest
from PyQt6.QtCore import Qt
from pytestqt.qtbot import QtBot

from src.features.edit.core.audio_dsp import EffectChain, SaturationEffect
from src.features.mixer.effects.production_dialog import ProductionEffectDialog
from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget


def _render(params: dict, signal: np.ndarray) -> np.ndarray:
    effect = SaturationEffect()
    effect.set_params(params)
    return effect.process(signal)


def test_independent_upper_crossover_changes_high_band_and_preserves_old_snapshots() -> None:
    rate = 48000
    time = np.arange(rate, dtype=np.float64) / rate
    signal = (0.18 * np.sin(2 * np.pi * 3500.0 * time))[None, :].astype(np.float32)
    params = {"model_profile": "multiband", "mb_xover_low_hz": 300.0,
              "mb_bands": [{}, {}, {"enabled": True, "drive_db": 18.0, "model": "tape"}]}
    low = params | {"mb_xover_high_hz": 1400.0}
    high = params | {"mb_xover_high_hz": 7100.0}
    np.testing.assert_array_equal(_render(low, signal), _render(high, signal))
    below = _render(low | {"mb_crossover_mode": "dual"}, signal)
    above = _render(high | {"mb_crossover_mode": "dual"}, signal)
    assert np.sqrt(np.mean((below - above) ** 2)) > 0.08


def test_saturation_profile_switch_and_sparse_snapshot_restore() -> None:
    samples = np.random.default_rng(261).normal(0.0, 0.1, (2, 911)).astype(np.float32)
    effect = SaturationEffect()
    legacy = {"drive": 32.0}
    original = _render(legacy, samples)
    sparse = {"model_profile": "multiband"}
    effect.set_params(sparse)
    effect.process(samples)
    effect.set_params(sparse | {"mb_oversampling": 4, "mb_mix": 0.8,
                                "mb_crossover_mode": "dual", "mb_bands": [
                                    {"enabled": True, "model": "tube", "drive_db": 13.0}]})
    assert effect.latency_samples() == 20
    assert effect.tail_estimate_samples() > 20
    assert not np.array_equal(effect.process(samples), samples)
    effect.set_params(sparse)
    assert effect.latency_samples() == 0
    np.testing.assert_array_equal(effect.process(samples), samples)
    effect.set_params(legacy)
    assert effect.is_active
    assert effect.latency_samples() == 0
    np.testing.assert_array_equal(effect.process(samples), original)


def test_corrected_oversampled_crossover_is_chunk_independent() -> None:
    signal = np.random.default_rng(202).normal(0.0, 0.1, (4, 4500)).astype(np.float32)
    params = {"model_profile": "multiband", "mb_crossover_mode": "dual",
              "mb_xover_high_hz": 5500.0, "mb_oversampling": 4,
              "mb_mix": 0.66, "mb_bands": [
                  {"enabled": True, "model": "tube", "bias_pct": 72.0, "tone_db": 5.0},
                  {"enabled": True, "model": "transistor", "level_db": -4.0},
                  {"enabled": True, "drive_db": 5.0},
              ]}
    effect = SaturationEffect()
    effect.set_params(params)
    blocks = np.split(signal, (1, 3, 247, 1050, 2099, 3285), axis=1)
    np.testing.assert_array_equal(
        np.concatenate([effect.process(block) for block in blocks], axis=1),
        _render(params, signal))


@pytest.mark.parametrize("mix", [0.0, 1.0])
def test_multiband_tail_drains_oversampling_filter_and_dry_delay(mix: float) -> None:
    effect = SaturationEffect()
    effect.set_params({"model_profile": "multiband", "mb_oversampling": 4, "mb_mix": mix})
    pulse = np.zeros((1, 129), dtype=np.float32)
    pulse[0, 0] = 1.0
    response = effect.process(pulse)[0]
    assert effect.latency_samples() == 20
    assert np.flatnonzero(np.abs(response) > 1e-8)[-1] <= effect.tail_estimate_samples()
    assert np.argmax(np.abs(response)) == 20


def test_saturation_rack_mode_switch_controls_and_spec_subscription(qtbot: QtBot) -> None:
    strip = TrackMixerWidget("tone", "Track")
    qtbot.addWidget(strip)
    strip._effect_add("saturation")
    instance = strip._effect_chain.effects[0]
    strip._open_insert_effect(instance)
    _, _, dialog = next(strip._iter_insert_dialogs())
    panel = dialog.panel
    try:
        graph = panel.sat_graph
        assert panel.band_cards[0].isHidden()
        assert instance.instance_id in strip.spectrum_targets()
        assert not graph.grab().isNull()
        qtbot.mouseClick(graph, Qt.MouseButton.LeftButton, pos=graph._btn_spec_rect.center().toPoint())
        assert instance.instance_id not in strip.spectrum_targets()
        with qtbot.waitSignal(dialog.preview, timeout=1000):
            panel.profile_combo.setCurrentIndex(1)
        assert dialog.current_params()["model_profile"] == "multiband"
        assert dialog.current_params()["mb_crossover_mode"] == "dual"
        assert not panel.band_cards[0].isHidden()
        assert not panel.os_combo.isHidden()

        card = panel.band_cards[1]
        card.band_btn.click()
        card.drive_pill.valueEdited.emit("36 dB")
        card.tone_pill.valueEdited.emit("-9 dB")
        card.level_pill.valueEdited.emit("-24 dB")
        graph.crossoverDragged.emit(260.0, 5300.0)
        with qtbot.waitSignal(dialog.committed, timeout=1000):
            dialog.apply()

        stored = strip._effect_chain.effects[0].params
        assert stored["mb_xover_high_hz"] == 5300.0
        assert stored["mb_bands"][1] == {
            "enabled": True, "model": "tape", "drive_db": 36.0,
            "tone_db": -9.0, "bias_pct": 0.0, "level_db": -24.0,
        }
        assert not graph.grab().isNull()
        with qtbot.waitSignal(dialog.preview, timeout=1000):
            panel.profile_combo.setCurrentIndex(0)
        assert panel.band_cards[0].isHidden()
        assert dialog.current_params()["model_profile"] == "legacy"
        dialog.reject()
        assert strip._effect_chain.effects[0].params == stored
    finally:
        strip.close_all_dialogs()


def test_old_multiband_preview_preserves_saved_compatibility_until_upgrade(qtbot: QtBot) -> None:
    saved = {"model_profile": "multiband", "mb_bands": [{"enabled": True, "future": "keep"}]}
    dialog = ProductionEffectDialog("saturation", "Track", saved)
    qtbot.addWidget(dialog)
    dialog.show()
    panel = dialog.panel
    assert dialog.current_params() == saved
    assert not panel.crossover_upgrade.isHidden()
    panel.profile_combo.setCurrentIndex(0)
    panel.profile_combo.setCurrentIndex(1)
    assert dialog.current_params() == saved
    panel.crossover_upgrade.click()
    assert dialog.current_params() == saved | {"mb_crossover_mode": "dual"}
    assert panel.crossover_upgrade.isHidden()
    dialog.reject()


def test_zero_drive_spectrum_only_runs_when_requested(qtbot: QtBot) -> None:
    signal = np.random.default_rng(119).normal(0.0, 0.1, (2, 512)).astype(np.float32)
    chain = EffectChain()
    chain.set_params([("saturation", {"drive": 0.0}, True, "sat")])
    chain.set_spectrum_instances(frozenset())
    assert chain.process(signal) is signal
    assert chain.tap_levels()["sat"]["bypassed"]
    chain.set_spectrum_instances(frozenset({"sat"}))

    def received_spectrum() -> bool:
        assert chain.process(signal) is signal
        return "output_db" in chain.tap_levels()["sat"]["spectrum"]

    qtbot.waitUntil(received_spectrum, timeout=2000)
    tap = chain.tap_levels()["sat"]
    assert not tap["bypassed"]
    assert tap["out_db"] == tap["in_db"]
    assert tap["spectrum"]["output_db"] == tap["spectrum"]["input_db"]


def test_saturation_window_displays_safe_controls_for_malformed_saved_bands(qtbot: QtBot) -> None:
    saved = {"model_profile": "multiband", "mb_xover_low_hz": -10,
             "mb_xover_high_hz": 0, "mb_bands": [None, {"tone_db": float("inf")}]}
    dialog = ProductionEffectDialog("saturation", "Track", saved)
    qtbot.addWidget(dialog)
    dialog.show()
    assert not dialog.panel.sat_graph.grab().isNull()
    card = dialog.panel.band_cards[0]
    card.drive_pill.valueEdited.emit("6 dB")
    bands = dialog.current_params()["mb_bands"]
    assert bands[0]["drive_db"] == 6.0
    dialog.reject()
