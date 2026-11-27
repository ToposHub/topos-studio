"""The Dynamics rack and standalone stages agree with the running processors."""
from __future__ import annotations

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

from src.features.edit.core.audio_dsp import DynamicsEffect, EffectChain
from src.features.mixer.effects.production_dialog import ProductionEffectDialog
from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget


@pytest.mark.parametrize(("kind", "stage"), [
    ("compressor", "comp"), ("expander", "eg"), ("limiter", "lim"),
])
def test_single_stage_presets_and_ab_never_enable_hidden_processors(
    qtbot: QtBot, kind: str, stage: str,
) -> None:
    dialog = ProductionEffectDialog(kind, "Track", {})
    qtbot.addWidget(dialog)
    panel = dialog.panel
    for index in range(panel.preset_combo.count()):
        panel.preset_combo.setCurrentIndex(index)
        state = panel.get_state()
        assert {key for key in ("comp", "eg", "lim") if state[key]["enabled"]} == {stage}
        if kind != "compressor":
            assert state["makeup"] == 0.0
    panel.btn_b.click()
    state = panel.get_state()
    assert {key for key in ("comp", "eg", "lim") if state[key]["enabled"]} == {stage}


@pytest.mark.parametrize(("kind", "params", "input_db"), [
    ("expander", {"threshold_db": -35.0, "ratio": 2.0}, -41.0),
    ("expander", {"threshold_db": -35.0, "range_db": 18.0, "mode": "Gate"}, -41.0),
    ("compressor", {"threshold_db": -30.0, "ratio": 4.0, "mix": 50.0, "knee": 0.0}, -3.0),
    ("limiter", {"threshold_db": -18.0}, -6.0),
])
def test_standalone_transfer_matches_dsp_at_steady_state(
    qtbot: QtBot, kind: str, params: dict[str, float | str], input_db: float,
) -> None:
    dialog = ProductionEffectDialog(kind, "Track", params)
    qtbot.addWidget(dialog)
    chain = EffectChain()
    chain.set_params([(kind, params, True, "instance")])
    source = np.full((2, 96000), 10.0 ** (input_db / 20.0), dtype=np.float32)
    actual = float(20.0 * np.log10(chain.process(source)[0, -1]))
    predicted = dialog.panel.dyn_graph.evaluate_transfer(input_db)
    assert predicted == pytest.approx(actual, abs=0.02)


def test_new_dynamics_applies_makeup_with_disabled_stages_and_meters_actual_output(
    qtbot: QtBot,
) -> None:
    strip = TrackMixerWidget("audio", "Track", "A1")
    qtbot.addWidget(strip)
    strip._add_dynamics_unit()
    try:
        dialog = strip._dynamics_dialog
        assert dialog is not None
        assert strip._effect_chain.effects[1].params["makeup_post"] is True
        state = dialog.panel.get_state()
        state["makeup"] = 6.0
        for stage in ("eg", "comp", "lim"):
            state[stage]["enabled"] = False
        dialog.panel.set_state(state)
        dialog.apply()

        chain = EffectChain()
        chain.set_params(strip._effect_chain.to_dsp_entries())
        assert chain.is_active
        source = np.full((2, 512), 0.08, dtype=np.float32)
        output = chain.process(source)
        np.testing.assert_allclose(output, source * 10.0 ** (6.0 / 20.0))
        input_db = float(20.0 * np.log10(source.max()))
        output_db = float(20.0 * np.log10(output.max()))
        strip.update_track_telemetry({
            "taps": chain.tap_levels(), "gr": chain.collect_gain_reduction(),
            "pre": -50.0, "peak": [-50.0, -50.0],
        })
        panel = dialog.panel
        assert panel.meter_input._value_db == pytest.approx(input_db)
        assert panel.meter_output._value_db == pytest.approx(output_db)
        assert panel.dyn_graph._trail_points[-1] == pytest.approx((input_db, output_db))

        strip._effect_bypass_dynamics()
        chain.set_params(strip._effect_chain.to_dsp_entries())
        assert not chain.is_active
        np.testing.assert_array_equal(chain.process(source), source)
        strip.update_track_telemetry({"taps": chain.tap_levels()})
        assert panel.meter_output._value_db <= -55.0
        assert not panel.dyn_graph._trail_points
    finally:
        strip.close_all_dialogs()


def test_dynamics_detector_reports_zero_dbfs_and_clears_when_compressor_is_disabled() -> None:
    effect = DynamicsEffect()
    params = {"eg": {"enabled": False}, "comp": {"enabled": True}, "lim": {"enabled": False}}
    source = np.ones((2, 512), dtype=np.float32)
    effect.set_params(params)
    effect.process(source)
    assert effect.collect_telemetry()["detector_dbfs"] == 0.0
    effect.set_params(params | {"comp": {"enabled": False}})
    effect.process(source)
    assert effect.collect_telemetry()["detector_dbfs"] <= -100.0
