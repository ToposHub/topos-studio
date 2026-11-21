"""Independent regressions for the post-integration audio dialog review."""

from collections.abc import Iterator

import pytest
from pytestqt.qtbot import QtBot

from src.features.edit.core.timeline_v2 import SpectralManglerParams, VoiceChangerParams
from src.shared.ui.audio_mixer.spectral_mangler_dialog import SpectralManglerDialog
from src.shared.ui.audio_mixer.voice_changer_dialog import VoiceChangerDialog


@pytest.fixture(params=["voice", "mangler"])
def effect_dialog(request: pytest.FixtureRequest, qtbot: QtBot) -> Iterator:
    if request.param == "voice":
        params = VoiceChangerParams(random_seed=317).to_dict()
        cls = VoiceChangerDialog
    else:
        params = SpectralManglerParams(random_seed=317).to_dict()
        cls = SpectralManglerDialog
    params["future_metadata"] = {"keep": [3, 7]}
    dialog = cls("T", "Track", "FX", initial_params=params)
    qtbot.addWidget(dialog)
    yield dialog


def _signals(dialog) -> tuple:
    prefix = "voice_changer" if isinstance(dialog, VoiceChangerDialog) else "spectral_mangler"
    return getattr(dialog, prefix + "_preview"), getattr(dialog, prefix + "_committed")


def _edit_gain(dialog, gain: float) -> None:
    widget = dialog._widget
    state = widget.get_state()
    state["output_gain"] = gain
    widget.set_state(state)
    widget.stateChanged.emit(widget.get_state())


def test_edit_preserves_seed_and_unexposed_metadata(effect_dialog) -> None:
    dialog = effect_dialog
    _edit_gain(dialog, 4.0)
    params = dialog._current_params()
    assert params["random_seed"] == 317
    assert params["future_metadata"] == {"keep": [3, 7]}
    assert params["output_gain_db"] == 4.0


def test_external_backfill_cancels_pending_user_notifications(effect_dialog, qtbot: QtBot) -> None:
    dialog = effect_dialog
    events = []
    for signal in _signals(dialog):
        signal.connect(lambda *_args: events.append(_args))
    _edit_gain(dialog, 3.0)
    params = dialog._current_params()
    params["output_gain_db"] = -5.0
    params["random_seed"] = 991
    dialog.set_current_params(params)
    assert not dialog._preview_timer.isActive()
    assert not dialog._commit_timer.isActive()
    qtbot.wait(450)
    assert events == []
    _edit_gain(dialog, 2.0)
    assert dialog._current_params()["random_seed"] == 991


def test_continuous_edits_preview_before_the_drag_stops(effect_dialog, qtbot: QtBot) -> None:
    dialog = effect_dialog
    previews = []
    _signals(dialog)[0].connect(lambda *_args: previews.append(_args))
    for gain in range(6):
        _edit_gain(dialog, float(gain))
        qtbot.wait(25)
    assert previews, "preview must be throttled, rather than debounced until edits stop"


def test_closing_modeless_editor_flushes_last_pending_edit_once(effect_dialog) -> None:
    dialog = effect_dialog
    commits = []
    _signals(dialog)[1].connect(lambda _tid, params: commits.append(params))
    _edit_gain(dialog, 7.0)
    dialog.done(0)
    assert len(commits) == 1
    assert commits[0]["output_gain_db"] == 7.0
    assert not dialog._preview_timer.isActive()
    assert not dialog._commit_timer.isActive()


def test_voice_starts_with_no_claimed_f0(qtbot: QtBot) -> None:
    dialog = VoiceChangerDialog("T", "Track")
    qtbot.addWidget(dialog)
    graph = dialog._widget.voice_visualizer
    assert graph._f0_in_hz == 0.0
    assert graph._f0_out_hz == 0.0
    assert graph._clarity_pct == 0.0


def test_voice_f0_updates_even_without_a_spectrum(qtbot: QtBot) -> None:
    dialog = VoiceChangerDialog("T", "Track")
    qtbot.addWidget(dialog)
    dialog.update_telemetry({"in_rms": 0.1, "out_rms": 0.2,
                             "f0_in_hz": 220.0, "f0_out_hz": 440.0,
                             "voiced": True, "confidence": 0.8})
    graph = dialog._widget.voice_visualizer
    assert graph._f0_in_hz == 220.0
    assert graph._f0_out_hz == 440.0
    assert graph._clarity_pct == 80.0


def test_voice_silence_clears_meters_spectrum_and_peak_hold(qtbot: QtBot) -> None:
    dialog = VoiceChangerDialog("T", "Track")
    qtbot.addWidget(dialog)
    dialog.update_telemetry({"in_rms": 1.1, "out_rms": 1.1,
        "spectrum_hz": [100.0, 1000.0], "input_spectrum_db": [-3.0, -6.0],
        "output_spectrum_db": [-4.0, -7.0], "f0_in_hz": 220.0, "f0_out_hz": 440.0,
        "formant_active": True, "pitch_active": True})
    dialog.update_telemetry(None)
    widget = dialog._widget
    assert widget.meter_input._value == -55.0
    assert widget.meter_input._peak == -55.0
    assert not widget.meter_input._clipped
    assert widget.meter_fmt._value == 0.0
    assert widget.voice_visualizer._f0_in_hz == 0.0
    assert max(widget.voice_visualizer._in_spec) <= -65.0


def test_mangler_telemetry_uses_the_widget_public_entry_and_clears_zero(qtbot: QtBot) -> None:
    dialog = SpectralManglerDialog("T", "Track")
    qtbot.addWidget(dialog)
    assert dialog._widget.master_strip._in_db == -60.0
    wave = [0.1, -0.3, 0.2]
    grains = [{"id": 3, "position": 0.3}]
    dialog.update_telemetry({"in_rms": 0.1, "out_rms": 0.2,
                             "waveform": wave, "grains": grains})
    assert dialog._widget.visualizer._external_wave == wave
    assert dialog._widget.visualizer._external_grains == grains
    assert dialog._widget.master_strip._out_db > -20.0
    dialog.update_telemetry({"in_rms": 0.0, "out_rms": 0.0})
    assert dialog._widget.master_strip._in_db == -60.0
    assert dialog._widget.master_strip._out_db == -60.0
    dialog.update_telemetry(None)
    assert dialog._widget.visualizer._external_wave == []
    assert dialog._widget.visualizer._external_grains == []
    assert not dialog._widget.master_strip._clipped


def test_real_processor_analysis_reaches_its_own_open_editor(qtbot: QtBot) -> None:
    import numpy as np
    from src.features.edit.core.audio_dsp import EffectChain
    from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance
    from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget

    params = VoiceChangerParams().to_dict()
    chain_params = EffectChainParams(effects=(TrackEffectInstance(
        "voice_changer", params, instance_id="voice"),))
    processor = EffectChain()
    processor.set_params(chain_params.to_dsp_entries())
    t = np.arange(1920) / 48000.0
    processor.process(np.stack([0.2 * np.sin(2.0 * np.pi * 220.0 * t)] * 2))
    strip = TrackMixerWidget("T", "Track")
    qtbot.addWidget(strip)
    strip.set_effect_chain(chain_params)
    strip._open_voice_changer_editor(chain_params.effects[0])
    dialog = strip._voice_changer_dialogs["voice"]
    analysis = processor.tap_levels()["voice"]["analysis"]
    assert analysis["f0_in_hz"] is not None
    strip.update_track_telemetry({"taps": processor.tap_levels()})
    graph = dialog.panel.voice_visualizer
    assert graph._f0_in_hz == analysis["f0_in_hz"]
    assert dialog.panel.meter_fmt._value == 100.0
    assert dialog.panel.meter_pch._value == 0.0
    assert dialog.panel.meter_voc._value == 0.0
    strip.update_track_telemetry({"silent": True})
    assert graph._f0_in_hz == 0.0
    assert dialog.panel.meter_fmt._value == 0.0
    strip.close_all_dialogs()
