"""Ordinary Mixer edits must not upgrade unrelated FX or discard opaque data."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pytestqt.qtbot import QtBot

from src.features.edit.core.audio_dsp import ALGORITHM_VERSION
from src.features.edit.core.timeline_v2 import (
    CompressorParams, DelayParams, EffectChainParams, ExpanderParams,
    ImagerParams, LimiterParams, ReverbParams, SaturationParams,
    SpectralManglerParams, StripEQParams, TrackEffectInstance, VoiceChangerParams,
)
from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget
from src.shared.ui.audio_mixer.dynamics_dialog import DynamicsDialog


@pytest.fixture
def strip(qtbot: QtBot) -> Iterator[TrackMixerWidget]:
    widget = TrackMixerWidget("track", "Track")
    qtbot.addWidget(widget)
    yield widget
    widget.close_all_dialogs()


def _instance(effect_type: str, params: dict, iid: str, *,
              order: int = 0, panel: str = "fx") -> TrackEffectInstance:
    return TrackEffectInstance(
        effect_type, params, instance_id=iid, order=order, panel=panel,
        algorithm_version=2,
        _extra_fields={"future_metadata": {"vendor": iid, "payload": [1, 2]}},
    )


def _install(strip: TrackMixerWidget, target: TrackEffectInstance) -> dict:
    legacy = _instance("strip_eq", StripEQParams().to_dict(), "legacy", panel="eq")
    future = TrackEffectInstance(
        "future_fx", {"opaque_parameter": {"preserve": [3, 4]}},
        instance_id="future", order=1, algorithm_version=777,
        _extra_fields={"vendor": {"keep": True}},
    )
    strip.set_effect_chain(EffectChainParams(effects=(legacy, future, target)))
    return {instance.instance_id: instance.to_dict()
            for instance in strip._effect_chain.effects}


def _assert_identity_metadata(strip: TrackMixerWidget, expected: dict) -> None:
    for instance in strip._effect_chain.effects:
        original = expected[instance.instance_id]
        assert instance.algorithm_version == original["algorithm_version"]
        assert instance._extra_fields == {
            key: value for key, value in original.items()
            if key == "future_metadata" or key == "vendor"
        }


@pytest.mark.parametrize("effect_type,method,params_class", [
    ("limiter", "_on_limiter_edited", LimiterParams),
    ("compressor", "_on_compressor_edited", CompressorParams),
    ("reverb", "_on_reverb_edited", ReverbParams),
    ("delay", "_on_delay_edited", DelayParams),
    ("saturation", "_on_saturation_edited", SaturationParams),
    ("imager", "_on_imager_edited", ImagerParams),
    ("expander", "_on_expander_edited", ExpanderParams),
    ("strip_eq", "_on_strip_eq_edited", StripEQParams),
])
@pytest.mark.parametrize("committed", [False, True])
def test_editor_changes_only_target_sound_parameters(
    strip: TrackMixerWidget, effect_type: str, method: str,
    params_class: type, committed: bool,
) -> None:
    target = _instance(effect_type, params_class().to_dict(), "target", order=2)
    expected = _install(strip, target)
    updated = params_class(enabled=False)
    getattr(strip, method)(target, updated, commit=committed)
    _assert_identity_metadata(strip, expected)
    by_id = {instance.instance_id: instance for instance in strip._effect_chain.effects}
    assert by_id["target"].params == updated.to_dict()
    assert by_id["legacy"].to_dict() == expected["legacy"]
    assert by_id["future"].to_dict() == expected["future"]


@pytest.mark.parametrize("effect_type,params_class", [
    ("voice_changer", VoiceChangerParams), ("spectral_mangler", SpectralManglerParams),
])
@pytest.mark.parametrize("committed", [False, True])
def test_new_composite_editor_retains_version_and_opaque_metadata(
    strip: TrackMixerWidget, effect_type: str, params_class: type, committed: bool,
) -> None:
    target = _instance(effect_type, params_class().to_dict(), "target", order=2)
    expected = _install(strip, target)
    updated = params_class(enabled=False).to_dict()
    strip._on_generic_fx_edited(target, effect_type, updated, commit=committed)
    _assert_identity_metadata(strip, expected)
    assert strip._effect_chain.effects[-1].params == updated
    assert strip._effect_chain.effects[0].to_dict() == expected["legacy"]
    assert strip._effect_chain.effects[1].to_dict() == expected["future"]


@pytest.mark.parametrize("committed", [False, True])
def test_eq_badge_edit_keeps_legacy_algorithm_version(
    strip: TrackMixerWidget, committed: bool,
) -> None:
    target = _instance("delay", DelayParams().to_dict(), "target", order=2)
    expected = _install(strip, target)
    params = StripEQParams(master_gain=3.0)
    if committed:
        strip._on_eq_committed_from_badge("track", params)
    else:
        strip._on_eq_preview_from_badge(params)
    _assert_identity_metadata(strip, expected)
    assert strip._effect_chain.effects[0].params == params.to_dict()
    assert strip._effect_chain.effects[1].to_dict() == expected["future"]


@pytest.mark.parametrize("action", ["move", "remove", "bypass", "fx_bypass", "dynamics_bypass"])
def test_rack_actions_preserve_remaining_instance_metadata(
    strip: TrackMixerWidget, action: str,
) -> None:
    target = _instance("compressor", CompressorParams().to_dict(), "target", order=2,
                       panel="dynamics" if action == "dynamics_bypass" else "fx")
    expected = _install(strip, target)
    if action == "move":
        strip._effect_move(target, -1)
    elif action == "remove":
        strip._effect_remove(target)
    elif action == "bypass":
        strip._effect_bypass(target)
    elif action == "fx_bypass":
        strip._on_fx_clicked()
    else:
        strip._effect_bypass_dynamics()
    _assert_identity_metadata(strip, expected)


def test_genuinely_new_effect_still_uses_current_algorithm(strip: TrackMixerWidget) -> None:
    strip._effect_add("delay")
    assert strip._effect_chain.effects[0].algorithm_version == ALGORITHM_VERSION


@pytest.mark.parametrize("instance_enabled", [None, False, True])
def test_dynamics_submodule_update_keeps_version_and_metadata(
    qtbot: QtBot, instance_enabled: bool | None,
) -> None:
    target = _instance("compressor", CompressorParams(makeup_post=True).to_dict(),
                       "dyn-comp", panel="dynamics")
    dialog = DynamicsDialog("track", "Track", chain=EffectChainParams(effects=(target,)))
    qtbot.addWidget(dialog)
    changed = CompressorParams(threshold_db=-24.0).to_dict()
    changed.pop("makeup_post", None)
    dialog._write_instance("compressor", changed, instance_enabled=instance_enabled)
    actual = next(effect for effect in dialog.get_chain().effects
                  if effect.instance_id == target.instance_id)
    assert actual.algorithm_version == target.algorithm_version
    assert actual._extra_fields == target._extra_fields
    assert actual.params["threshold_db"] == -24.0
    assert actual.params["makeup_post"] is True
    assert actual.enabled is (target.enabled if instance_enabled is None else instance_enabled)
