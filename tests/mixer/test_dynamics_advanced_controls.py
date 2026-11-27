"""Exercise Mixer dynamics controls against the production DSP chain."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PyQt6.QtWidgets import QInputDialog, QPushButton
from pytestqt.qtbot import QtBot

from src.features.edit.core import effect_preset
from src.features.edit.core.audio_dsp import ALGORITHM_VERSION, EffectChain
from src.features.edit.core.audio_effects.modulation_tools import TransientDesignerEffect
from src.features.edit.core.audio_effects.true_peak_limiter import TruePeakLimiterEffect
from src.features.edit.core.timeline_v2 import TransientDesignerParams
from src.features.edit.ui.clip_audio_effect_dialog_adapter import ClipAudioEffectDialogManager
from src.features.mixer.effects.production_dialog import ProductionEffectDialog


@pytest.mark.parametrize("kind,saved,current,expected", [
    ("compressor", {"threshold_db": -40.0}, {"threshold_db": -26.0}, {"threshold_db": -40.0}),
    ("bus_comp", {"template": "mx_glue"}, {"template": "dx_glue", "threshold_db": -50.0},
     {"template": "mx_glue", "threshold_db": None}),
    ("multiband_comp", {"crossovers": [390.0], "bands": [{"ratio": 2.5}, {"ratio": 3.0}]},
     {"crossovers": [180.0, 4200.0], "bands": [{"future_band": "keep"}, {}, {}]},
     {"crossovers": [390.0], "bands": [{"ratio": 2.5, "future_band": "keep"}, {"ratio": 3.0}]}),
    ("transient_designer", {"attack_db": 7.2}, {"attack_pct": 25.0},
     {"attack_pct": None, "attack_db": 7.2}),
    ("true_peak_limiter", {"ceiling_dbtp": -8.0}, {"ceiling_dbtp": -2.0},
     {"ceiling_dbtp": -8.0}),
])
def test_dynamics_user_preset_persists_and_recalls_the_processing_model(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    kind: str, saved: dict, current: dict, expected: dict,
) -> None:
    monkeypatch.setattr(effect_preset, "_store_path", lambda: str(tmp_path / "dynamics.json"))
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("My processing", True))
    dialog = ProductionEffectDialog(kind, "Original", saved)
    qtbot.addWidget(dialog)
    save = next(button for button in dialog.panel.findChildren(QPushButton, "ToolBtn") if button.text() == "+")
    save.click()
    reopened = ProductionEffectDialog(kind, "New track", current)
    qtbot.addWidget(reopened)
    combo = reopened.panel.preset_combo
    index = combo.findText("My processing")
    assert index >= 0
    combo.setCurrentIndex(index)
    payload = reopened.current_params()
    assert {key: payload[key] for key in expected} == expected


def test_bus_template_updates_inherited_controls_and_restores_cancelled_override(qtbot: QtBot) -> None:
    initial = {"template": "dx_glue", "ratio": 3.0, "sc_hpf_hz": 0.0}
    dialog = ProductionEffectDialog("bus_comp", "Bus", initial)
    qtbot.addWidget(dialog)
    dialog.panel.btn_api.click()
    assert dialog.current_params() == initial | {"template": "mx_glue"}
    assert dialog.panel.get_state()["threshold"] == -18.0
    assert dialog.panel.get_state()["ratio"] == 3.0
    dialog.panel.c_knee.valueChanged.emit(0.0)
    assert dialog.current_params()["knee"] == 0.0
    assert np.isfinite(dialog.panel.bus_graph.evaluate_transfer(-18.0))

    def run(chain: EffectChain, params: dict) -> np.ndarray:
        chain.set_params([("bus_comp", params, True, "bus", ALGORITHM_VERSION)])
        return chain.process(np.full((2, 2048), 0.8, dtype=np.float32))

    chain = EffectChain()
    run(chain, initial | {"threshold_db": -50.0})
    chain.reset_state()
    np.testing.assert_array_equal(run(chain, initial), run(EffectChain(), initial))
    assert chain.tap_levels()["bus"]["analysis"]["gain_reduction_db"] > 0.0


def test_bus_curve_matches_dsp_with_parallel_mix_and_makeup(qtbot: QtBot) -> None:
    params = {"threshold_db": -22.0, "attack_ms": 0.7, "sc_hpf_hz": 0.0,
              "ratio": 3.0, "knee": 0.0, "mix": 35.0, "makeup_gain": 8.0}
    dialog = ProductionEffectDialog("bus_comp", "Bus", params)
    qtbot.addWidget(dialog)
    assert dialog.panel.c_thresh._val == -22.0
    assert dialog.panel.c_attack._val == 0.7
    chain = EffectChain()
    chain.set_params([("bus_comp", params, True, "bus", ALGORITHM_VERSION)])
    pcm = chain.process(np.full((2, 4096), 0.5, dtype=np.float32))
    input_db = 20.0 * np.log10(0.5)
    output_db = 20.0 * np.log10(pcm[0, -1])
    assert dialog.panel.bus_graph.evaluate_transfer(input_db) == pytest.approx(output_db, abs=0.01)


@pytest.mark.parametrize("rate,frequency", [(48000.0, 15000.0), (44100.0, 19990.0)])
def test_multiband_switches_band_count_with_valid_crossovers_and_clears_muted_gr(
    qtbot: QtBot, rate: float, frequency: float,
) -> None:
    bands = [{"threshold_db": -48.0, "attack_ms": 1.0, "ratio": 4.0}] * 2
    dialog = ProductionEffectDialog("multiband_comp", "Track", {"crossovers": [frequency], "bands": bands})
    qtbot.addWidget(dialog)
    dialog.panel.set_sample_rate(rate)
    dialog.panel._band_count_btns[4].click()
    params = dialog.current_params()
    low, mid, high = params["crossovers"]
    assert low == min(frequency, 0.45 * rate - 2.0)
    assert low < mid < high <= min(20000.0, 0.45 * rate)
    assert len(params["bands"]) == 4
    chain = EffectChain(rate)
    samples = np.full((2, 4096), 0.7, dtype=np.float32)
    chain.set_params([("multiband_comp", params, True, "multi", ALGORITHM_VERSION)])
    chain.process(samples)
    assert chain.tap_levels()["multi"]["analysis"]["band_0_gr_db"] > 0.0
    assert chain.tap_levels()["multi"]["analysis"]["band_count"] == 4
    dialog.panel.band_cards[0].btn_mute.click()
    muted = dialog.current_params()
    assert muted["bands"][0]["mute"] is True
    chain.set_params([("multiband_comp", muted, True, "multi", ALGORITHM_VERSION)])
    chain.process(samples)
    assert chain.tap_levels()["multi"]["analysis"]["band_0_gr_db"] == 0.0


def test_multiband_sparse_snapshot_restores_band_count_after_cancel() -> None:
    chain = EffectChain(44100.0)
    initial = {"attack_ms": 8.0}
    samples = np.zeros((2, 32), dtype=np.float32)
    for params, count in (
        (initial, 3), (initial | {"crossovers": [300.0]}, 2),
        (initial | {"crossovers": [19970.0, 19980.0, 19990.0]}, 4), (initial, 3),
    ):
        chain.set_params([("multiband_comp", params, True, "multi", ALGORITHM_VERSION)])
        chain.process(samples)
        assert chain.tap_levels()["multi"]["analysis"]["band_count"] == count


def test_true_peak_linked_release_matches_mono_and_measures_output() -> None:
    mono = np.concatenate((np.full((1, 512), 0.95), np.full((1, 2048), 0.1)), axis=1).astype(np.float32)
    params = {"ceiling_dbtp": -6.0, "lookahead_ms": 2.0, "release_ms": 100.0, "linked": True}
    mono_limiter, stereo_limiter = TruePeakLimiterEffect(), TruePeakLimiterEffect()
    for limiter in (mono_limiter, stereo_limiter):
        limiter.set_params(params)
    expected = mono_limiter.process(mono)
    output = stereo_limiter.process(np.repeat(mono, 2, axis=0))
    np.testing.assert_array_equal(output, np.repeat(expected, 2, axis=0))
    measured = stereo_limiter.collect_telemetry()
    assert measured["true_peak_dbtp"] > measured["output_true_peak_dbtp"]
    assert measured["output_true_peak_dbtp"] == pytest.approx(-6.0, abs=0.5)
    stereo_limiter.reset_state()
    assert stereo_limiter.collect_telemetry()["output_true_peak_dbtp"] == -120.0


@pytest.mark.parametrize("version", [4, ALGORITHM_VERSION])
def test_true_peak_cancel_restores_default_latency_from_sparse_snapshot(version: int) -> None:
    current = {"ceiling_dbtp": -8.0}
    chain = EffectChain()
    chain.set_params([("true_peak_limiter", current, True, "limit", version)])
    original_latency = chain.latency_samples()
    chain.set_params([("true_peak_limiter", current | {"lookahead_ms": 12.0}, True, "limit", version)])
    assert chain.latency_samples() > original_latency
    chain.set_params([("true_peak_limiter", current, True, "limit", version)])
    assert chain.latency_samples() == original_latency


def test_transient_filter_can_be_bypassed_and_reset_without_losing_stereo() -> None:
    time = np.arange(2048) / 48000.0
    low = (0.4 * np.sin(2 * np.pi * 50.0 * time)).astype(np.float32)
    opposite = np.stack((low, -low))
    effect = TransientDesignerEffect()
    effect.set_params({"sc_listen": True, "sc_hpf_hz": 500.0, "sc_filter_enabled": True})
    filtered = effect.process(opposite)
    assert np.linalg.norm(filtered) < np.linalg.norm(opposite) * 0.1
    effect.reset_state()
    np.testing.assert_array_equal(effect.process(opposite), filtered)
    snapshot = TransientDesignerParams.from_dict({"sc_filter_enabled": False})
    assert snapshot.to_dict()["sc_filter_enabled"] is False
    effect.set_params(snapshot.to_dict())
    np.testing.assert_array_equal(effect.process(opposite), opposite)


def test_transient_sidechain_selection_and_clip_listen_preview(qtbot: QtBot) -> None:
    sources = (("first", "Dialogue"), ("second", "Dialogue"))
    dialog = ProductionEffectDialog("transient_designer", "Track", {}, sidechain_sources=sources)
    qtbot.addWidget(dialog)
    dialog._transient_source_combo.setCurrentIndex(dialog._transient_source_combo.findData("second"))
    assert dialog.current_params()["sidechain_source_id"] == "second"
    dialog.set_sidechain_sources((("second", "Renamed"),))
    assert dialog._transient_source_combo.currentText() == "Renamed"

    manager = ClipAudioEffectDialogManager()
    clip_dialog = manager.open_dialog("transient_designer", "clip", "fx", {})
    assert clip_dialog is not None
    try:
        with qtbot.waitSignal(manager.clip_effect_preview, timeout=500) as preview:
            clip_dialog.panel.sidechain_card.listen_btn.click()
        assert preview.args[2]["sc_listen"] is True
        with qtbot.waitSignal(manager.clip_effect_preview_finished, timeout=500):
            clip_dialog.apply()
        assert manager.previews_for("clip") == {}
        assert "sc_listen" not in clip_dialog.current_params()
    finally:
        manager.close_all()
