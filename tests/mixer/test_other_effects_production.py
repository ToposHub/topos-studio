"""Production controls and visibility-scoped measurements for the tone generators."""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import Qt
from pytestqt.qtbot import QtBot

from src.features.edit.core.audio_dsp import EffectChain
from src.features.edit.core.timeline_v2 import EffectChainParams, ExciterParams, TrackEffectInstance
from src.features.mixer.effects.production_dialog import ProductionEffectDialog
from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget


def test_exciter_card_graph_and_oversampling_path_preview_and_apply(qtbot: QtBot) -> None:
    saved = {"ex_bands": [{"enabled": True, "amount": .2, "future": "keep"}]}
    strip = TrackMixerWidget("tone", "Track")
    qtbot.addWidget(strip)
    strip._effect_add("exciter", saved)
    strip._open_insert_effect(strip._effect_chain.effects[0])
    _, _, dialog = next(strip._iter_insert_dialogs())
    assert isinstance(dialog, ProductionEffectDialog)
    panel = dialog.panel
    try:
        assert dialog.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        assert not panel._demo_timer.isActive()
        assert not panel.band_cards[1].isHidden() and panel.band_cards[2].isHidden()
        assert panel.exciter_graph._dry_dbs == []
        assert panel.quality_combo.currentData() == "legacy"
        assert dialog.current_params() == saved

        card = panel.band_cards[0]
        with qtbot.waitSignal(dialog.preview, timeout=1000):
            card.mode_combo.setCurrentText("Odd")
            card.c_mix.pill.valueEdited.emit("72%")
            panel.band_cards[1].power_sw.setChecked(True, emit=True)
            panel.exciter_graph.bandAmountDragged.emit(1, 41.0)
            panel.exciter_graph.crossoverDragged.emit(0, 3200.0)
            panel.os_combo.setCurrentText("2x")
            panel.quality_combo.setCurrentIndex(panel.quality_combo.findData("clean"))
            panel.c_global_mix.pill.valueEdited.emit("65%")
            panel.out_fader.setGain(-19.0, emit=True)
        assert not panel.exciter_graph.grab().isNull()
        dialog.apply()
        committed = strip._effect_chain.effects[0].params
        assert committed["ex_crossover_hz"] == 3200.0
        assert committed["ex_oversampling"] == 2
        assert committed["ex_quality"] == "clean"
        assert committed["ex_wet"] == .65
        assert committed["ex_output_gain_db"] == -19.0
        assert committed["ex_bands"][0] == {"enabled": True, "amount": .2, "future": "keep",
                                            "mode": "odd", "mix": .72}
        assert committed["ex_bands"][1]["enabled"]
        assert committed["ex_bands"][1]["amount"] == .41
        with qtbot.waitSignal(dialog.preview, timeout=1000):
            panel.band_cards[1].power_sw.setChecked(False, emit=True)
        dialog.reject()
        assert strip._effect_chain.effects[0].params == committed
    finally:
        strip.close_all_dialogs()


def test_exciter_factory_presets_toggle_only_supported_controls(qtbot: QtBot) -> None:
    params = ExciterParams.default().to_dict()
    dialog = ProductionEffectDialog("exciter", "Track", params, enabled=True)
    qtbot.addWidget(dialog)
    panel = dialog.panel
    assert panel.preset_combo.count() >= 3
    for index in range(2):
        panel.preset_combo.setCurrentIndex(index)
        settings = dialog.current_params()
        assert settings["ex_oversampling"] == 2
        assert settings["ex_quality"] == "clean"
        assert len(settings["ex_bands"]) == 2
        assert any(band["enabled"] and band["amount"] for band in settings["ex_bands"])
        assert not panel.band_cards[2].isVisible()
    panel.reset_exciter()
    assert not any(band["enabled"] for band in dialog.current_params()["ex_bands"])


def test_exciter_spectrum_uses_actual_pcm_only_while_open(qtbot: QtBot) -> None:
    strip = TrackMixerWidget("tone", "Track")
    qtbot.addWidget(strip)
    instance = TrackEffectInstance("exciter", ExciterParams.default().to_dict(), instance_id="harmonics")
    model = EffectChainParams((instance,))
    strip.set_effect_chain(model)
    engine = EffectChain(48000, realtime_resources=True)
    engine.set_params(model.to_dsp_entries())
    t = np.arange(4096) / 48000
    wave = np.tile(.25 * np.sin(2 * np.pi * 2500.0 * t), (2, 1)).astype(np.float32)
    np.testing.assert_array_equal(engine.process(wave), wave)
    assert engine.tap_levels()["harmonics"]["bypassed"]
    strip._open_insert_effect(instance)
    _, _, dialog = next(strip._iter_insert_dialogs())
    try:
        assert strip.spectrum_targets() == frozenset({"harmonics"})
        engine.set_spectrum_instances(strip.spectrum_targets())
        np.testing.assert_array_equal(engine.process(wave), wave)
        qtbot.waitUntil(lambda: bool(engine.tap_levels()["harmonics"]["spectrum"]), timeout=2000)
        payload = engine.tap_levels()
        strip.update_track_telemetry({"taps": payload})
        graph = dialog.panel.exciter_graph
        assert graph._spectrum_freqs == payload["harmonics"]["spectrum"]["freqs"]
        assert graph._dry_dbs == graph._wet_dbs
        strip.update_track_telemetry({"silent": True, "taps": payload})
        assert not graph._dry_dbs and not graph._wet_dbs
        dialog.close()
        assert not strip.spectrum_targets()
        engine.set_spectrum_instances(strip.spectrum_targets())
        engine.process(wave)
        assert "spectrum" not in engine.tap_levels()["harmonics"]
    finally:
        strip.close_all_dialogs()


def test_exciter_loads_sparse_user_presets_without_losing_band_metadata(qtbot: QtBot) -> None:
    params = {"ex_bands": [
        {"enabled": True, "amount": .7, "first_extra": 2},
        {"enabled": True, "amount": .8, "second_extra": "retain"},
    ]}
    dialog = ProductionEffectDialog("exciter", "Track", params, enabled=True)
    qtbot.addWidget(dialog)
    dialog._apply_user_preset({"ex_bands": []})
    assert dialog.current_params()["ex_bands"] == [
        {"enabled": False, "mode": "even", "amount": 0.0, "mix": 1.0, "first_extra": 2},
        {"enabled": False, "mode": "even", "amount": 0.0, "mix": 1.0, "second_extra": "retain"},
    ]


def test_subharmonic_focus_controls_are_saved_and_legacy_fields_remain(qtbot: QtBot) -> None:
    saved = {"sh_source_center_hz": 92.0, "future": 7}
    dialog = ProductionEffectDialog("subharmonic", "Track", saved, enabled=True)
    qtbot.addWidget(dialog)
    panel = dialog.panel
    dialog.show()
    assert panel.detection_mode_combo.currentData() == "legacy"
    assert not panel.src_card.c_bw.isEnabled()
    assert dialog.current_params() == saved
    combo = panel.detection_mode_combo
    combo.setCurrentIndex(combo.findData("focused"))
    assert panel.src_card.c_bw.isEnabled()
    panel.sub_graph.sourceFreqDragged.emit(105)
    panel.src_card.c_bw.pill.valueEdited.emit("40")
    panel.sub_graph.subLpfDragged.emit(365)
    panel.src_card.c_oct1.pill.valueEdited.emit("100")
    panel.src_card.c_oct2.pill.valueEdited.emit("0")
    panel.drv_card.c_attack.pill.valueEdited.emit("37.5")
    panel.drv_card.c_release.pill.valueEdited.emit("260")
    panel.out_card.c_mix.pill.valueEdited.emit("75")
    panel.out_card.c_trim.pill.valueEdited.emit("-4")
    with qtbot.waitSignal(dialog.committed, timeout=1000) as committed:
        dialog.apply()
    assert committed.args[0] == {
        **saved, "sh_detection_mode": "focused", "sh_source_center_hz": 105,
        "sh_source_bandwidth_hz": 40, "sh_sub_lpf_hz": 365,
        "sh_octave1_gain_db": 0, "sh_octave2_gain_db": -120,
        "sh_attack_ms": 37.5, "sh_release_ms": 260, "sh_mix": .75, "sh_output_gain_db": -4,
    }
    assert not panel.sub_graph.grab().isNull()


def test_subharmonic_visible_instance_gets_measured_pitch_without_an_fft_tap(qtbot: QtBot) -> None:
    strip = TrackMixerWidget("tone", "Track")
    qtbot.addWidget(strip)
    strip._effect_add("subharmonic")
    instance = strip._effect_chain.effects[0]
    engine = EffectChain(48000, realtime_resources=True)
    engine.set_params(strip._effect_chain.to_dsp_entries())
    t = np.arange(12000) / 48000
    signal = (.18 * np.sin(2 * np.pi * 92 * t)).astype(np.float32)
    pcm = np.stack((signal, -signal))
    hidden = engine.process(pcm)
    assert "analysis" not in engine.tap_levels()[instance.instance_id]
    strip._open_insert_effect(instance)
    _, _, dialog = next(strip._iter_insert_dialogs())
    try:
        assert strip.spectrum_targets() == frozenset({instance.instance_id})
        engine.set_spectrum_instances(strip.spectrum_targets())
        engine.reset_state()
        np.testing.assert_array_equal(engine.process(pcm), hidden)
        tap = engine.tap_levels()[instance.instance_id]
        assert "spectrum" not in tap
        assert tap["analysis"]["locked"]
        assert abs(tap["analysis"]["f0_hz"] - 92.0) < 1.0
        assert tap["analysis"]["sub_peak_db"] > -55.0
        strip.update_track_telemetry({"taps": {instance.instance_id: tap}})
        displayed = dialog.panel.sub_graph._processor_telemetry
        assert displayed["locked"]
        assert displayed["f0_hz"] == tap["analysis"]["f0_hz"]
        assert displayed["confidence"] == tap["analysis"]["confidence"]
        assert dialog.panel.meter_sub_gen._value_db == tap["analysis"]["sub_peak_db"]
        strip.update_track_telemetry({"silent": True})
        assert dialog.panel.sub_graph._processor_telemetry == {}
        dialog.close()
        engine.set_spectrum_instances(strip.spectrum_targets())
        engine.process(pcm)
        assert "analysis" not in engine.tap_levels()[instance.instance_id]
    finally:
        strip.close_all_dialogs()


def test_reenabling_tone_generators_does_not_replay_a_pre_bypass_signal() -> None:
    t = np.arange(12000) / 48000.0
    tone = (.2 * np.sin(2 * np.pi * 90 * t)).astype(np.float32)
    signal = np.stack((tone, -tone))
    signal[:, -1] = .5
    silence = np.zeros((2, 512), np.float32)
    effects = {
        "exciter": {"ex_quality": "clean", "ex_oversampling": 2,
                    "ex_bands": [{"enabled": True, "amount": .8}]},
        "subharmonic": {"sh_detection_mode": "focused", "sh_mix": 1.0},
    }
    for kind, params in effects.items():
        for inner in (True, False):
            engine = EffectChain(48000, realtime_resources=True)
            engine.set_params([(kind, params, True, "tone")])
            engine.process(signal)
            engine.set_params([(kind, params | {"enabled": not inner}, inner, "tone")])
            np.testing.assert_array_equal(engine.process(silence), silence)
            engine.set_params([(kind, params, True, "tone")])
            np.testing.assert_array_equal(engine.process(silence), silence)
