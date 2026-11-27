"""Production-chain and parameter round trips missed by direct DSP fixtures."""
from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import ALGORITHM_VERSION, EffectChain
from src.features.edit.core.timeline_v2 import DeEsserParams, DynamicEqParams


@pytest.mark.parametrize("effect_type", [
    "convolution_reverb", "bus_comp", "chorus_flanger", "phaser",
    "tremolo_auto_pan", "transient_designer", "exciter",
])
def test_adapter_without_analysis_runs_with_instance_taps(effect_type: str) -> None:
    chain = EffectChain(48000)
    # Exciter's neutral defaults now skip DSP until a band adds harmonics.
    params = {"bands": [{"enabled": True, "amount": .5}]} if effect_type == "exciter" else {}
    chain.set_params([(effect_type, params, True, "fixture-instance", ALGORITHM_VERSION)])
    if effect_type == "convolution_reverb":
        chain._effects[0]._impl.set_ir(np.ones((1, 1), dtype=np.float32))
    pcm = np.random.default_rng(87).normal(0, 0.1, (2, 513)).astype(np.float32)
    original = pcm.copy()
    output = chain.process(pcm)
    assert output.shape == pcm.shape
    assert np.isfinite(output).all()
    np.testing.assert_array_equal(pcm, original)
    assert chain.tap_levels()["fixture-instance"]["bypassed"] is False
    assert isinstance(chain.tap_levels()["fixture-instance"].get("analysis", {}), dict)


def test_analysis_implementation_errors_are_not_silenced() -> None:
    chain = EffectChain(48000)
    chain.set_params([("bus_comp", {}, True, "bus", ALGORITHM_VERSION)])

    def broken_analysis() -> dict:
        raise ValueError("invalid analysis snapshot")

    chain._effects[0]._impl.collect_telemetry = broken_analysis
    with pytest.raises(ValueError, match="invalid analysis snapshot"):
        chain.process(np.zeros((2, 32), dtype=np.float32))


def test_deesser_new_controls_survive_model_and_reach_chain_latency() -> None:
    raw = {
        "detector_shape": "high_shelf", "lookahead_ms": 5.0,
        "output_gain_db": -3.0, "future": {"revision": 2},
    }
    params = DeEsserParams.from_dict(raw)
    restored = DeEsserParams.from_dict(params.to_dict()).to_dict()
    for key, value in raw.items():
        assert restored[key] == value
    raw["future"]["revision"] = 3
    assert params.to_dict()["future"] == {"revision": 2}
    chain = EffectChain(48000)
    chain.set_params([("de_esser", restored, True, "deesser", ALGORITHM_VERSION)])
    assert chain.latency_samples() == 240


def test_deesser_legacy_defaults_do_not_select_new_branch() -> None:
    params = DeEsserParams.from_dict({"frequency": 5400.0}).to_dict()
    assert not {"detector_shape", "lookahead_ms", "output_gain_db"} & params.keys()
    chain = EffectChain(48000)
    chain.set_params([("de_esser", params, True, "legacy", 0)])
    assert not chain._effects[0]._impl._uses_v2()


def test_deesser_new_controls_reject_nonfinite_values() -> None:
    params = DeEsserParams.from_dict({
        "detector_shape": "invalid", "lookahead_ms": float("nan"),
        "output_gain_db": float("inf"),
    }).to_dict()
    assert params["detector_shape"] == "bandpass"
    assert params["lookahead_ms"] == 0.0
    assert params["output_gain_db"] == 0.0


def test_deesser_preview_removal_restores_legacy_latency_on_the_same_instance() -> None:
    chain = EffectChain(48000)
    legacy = DeEsserParams.default().to_dict()
    chain.set_params([("de_esser", legacy | {"lookahead_ms": 5.0}, True, "ess", ALGORITHM_VERSION)])
    assert chain.latency_samples() == 240
    chain.set_params([("de_esser", legacy, True, "ess", ALGORITHM_VERSION)])
    assert chain.latency_samples() == 0


def test_dynamic_eq_preview_removal_restores_internal_detector_and_single_band() -> None:
    chain = EffectChain(48000)
    legacy = DynamicEqParams.default().to_dict()
    params = legacy | {"sidechain_source_id": "dialogue", "bands": [{"frequency_hz": 1000.0}]}
    chain.set_params([("dynamic_eq", params, True, "eq", ALGORITHM_VERSION)])
    assert chain.sidechain_map({"dialogue": np.zeros((2, 512), dtype=np.float32)})
    chain.set_params([("dynamic_eq", legacy, True, "eq", ALGORITHM_VERSION)])
    assert not chain.sidechain_map({"dialogue": np.zeros((2, 512), dtype=np.float32)})
    chain.process(np.ones((2, 512), dtype=np.float32))
    assert chain.tap_levels()["eq"]["analysis"]["bands"] == []


def test_dynamic_eq_band_identity_static_mode_and_future_fields_survive() -> None:
    raw = {
        "future_top": {"value": 1},
        "bands": [{
            "id": "presence", "dynamic_enabled": False,
            "static_gain_db": 6.0, "future_band": {"value": 2},
        }],
    }
    params = DynamicEqParams.from_dict(raw)
    raw["bands"][0]["future_band"]["value"] = 99
    restored = DynamicEqParams.from_dict(params.to_dict()).to_dict()
    assert restored["future_top"] == {"value": 1}
    band = restored["bands"][0]
    assert band["id"] == "presence"
    assert band["dynamic_enabled"] is False
    assert band["future_band"] == {"value": 2}
    # A disabled detector must remain static even for a signal above threshold.
    chain = EffectChain(48000)
    chain.set_params([("dynamic_eq", restored, True, "eq", ALGORITHM_VERSION)])
    time = np.arange(4800) / 48000
    output = chain.process((0.5 * np.sin(2 * np.pi * 3000 * time))[None].astype(np.float32))
    assert np.isfinite(output).all()
    assert chain._effects[0]._impl._mb_states[0].gain_state == 0.0


def test_dynamic_eq_band_nonfinite_number_uses_finite_default() -> None:
    band = DynamicEqParams.from_dict({
        "bands": [{"frequency_hz": float("nan"), "q": float("inf")}],
    }).to_dict()["bands"][0]
    assert band["frequency_hz"] == 3000.0
    assert band["q"] == 1.4


@pytest.mark.parametrize("effect_type,params", [
    ("de_esser", {"lookahead_ms": 5.0, "mode": "wide"}),
    ("bus_comp", {"sc_hpf_hz": 120.0}),
])
def test_repeated_chain_configuration_retains_filter_and_delay_state(
    effect_type: str, params: dict,
) -> None:
    pcm = np.random.default_rng(18).normal(0, 0.2, (2, 4096)).astype(np.float32)

    def render(reconfigure: bool) -> np.ndarray:
        chain = EffectChain(48000)
        entries = [(effect_type, params, True, "instance", ALGORITHM_VERSION)]
        chain.set_params(entries)
        blocks = []
        for start in range(0, pcm.shape[-1], 127):
            if reconfigure:
                chain.set_params(entries)
            blocks.append(chain.process(pcm[:, start:start + 127]))
        return np.concatenate(blocks, axis=-1)

    np.testing.assert_array_equal(render(True), render(False))


def test_deesser_gain_change_preserves_audio_lookahead_history() -> None:
    from src.features.edit.core.audio_effects.de_esser import DeEsserEffect

    effect = DeEsserEffect(48000)
    effect.set_params({"lookahead_ms": 5.0, "mode": "wide", "ratio": 1.0})
    effect.process(np.ones((1, 240), dtype=np.float32))
    effect.set_params({"output_gain_db": -6.0})
    output = effect.process(np.zeros((1, 240), dtype=np.float32))
    np.testing.assert_allclose(output, 10 ** (-6 / 20), atol=1e-7)


def test_dynamic_eq_detectors_read_original_input_for_every_band() -> None:
    from src.features.edit.core.audio_effects.dynamic_eq import DynamicEqEffect

    time = np.arange(6000) / 48000
    pcm = (0.3 * np.sin(2 * np.pi * 1000 * time))[None].astype(np.float32)
    detector_band = {
        "frequency_hz": 1000.0, "threshold_dbfs": -22.0,
        "max_cut_db": 9.0,
    }
    single = DynamicEqEffect(48000)
    single.set_params({"bands": [detector_band]})
    single.process(pcm)
    cascade = DynamicEqEffect(48000)
    cascade.set_params({"bands": [
        {"frequency_hz": 1000.0, "static_gain_db": -18.0,
         "dynamic_enabled": False},
        detector_band,
    ]})
    cascade.process(pcm)
    assert cascade._mb_states[1].gain_state == single._mb_states[0].gain_state
