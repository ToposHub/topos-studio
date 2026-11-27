"""Spatial panning and the Imager's production monitoring controls."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from pytestqt.qtbot import QtBot

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.audio_dsp import EffectChain, SpatialPanner
from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.features.edit.core.audio_retime import clip_fx_entries
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, EffectChainParams, TimelineSequence, TrackEffectInstance
from src.features.edit.ui.clip_audio_effect_dialog_adapter import ClipAudioEffectDialogManager
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack
from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel


def _imager_entries(params: dict, *, enabled: bool = True) -> list[tuple]:
    return [("imager", params, enabled, "image")]


def test_imager_analysis_runs_only_for_the_selected_instance() -> None:
    chain = EffectChain(48000, realtime_resources=True)
    chain.set_params(_imager_entries({}) + [("imager", {}, True, "second")])
    noise = np.random.default_rng(4).normal(0.0, 0.1, 480).astype(np.float32)
    signal = np.stack((noise, -noise))
    np.testing.assert_array_equal(chain.process(signal), signal)
    assert not any("analysis" in tap for tap in chain.tap_levels().values())

    chain.set_spectrum_instances(frozenset({"image"}))
    np.testing.assert_array_equal(chain.process(signal), signal)
    taps = chain.tap_levels()
    assert "spectrum" not in taps["image"]
    assert "analysis" not in taps["second"]
    measured = taps["image"]["analysis"]
    assert measured["correlation"] == pytest.approx(-1.0)
    assert measured["mid_db"] == -100.0
    assert measured["side_db"] == pytest.approx(10.0 * np.log10(np.mean(noise**2)))
    assert 0 < len(measured["samples_l"]) == len(measured["samples_r"]) <= 64

    chain.set_params(_imager_entries({}, enabled=False))
    chain.process(signal)
    assert "analysis" not in chain.tap_levels()["image"]


@pytest.mark.parametrize("malformed", [[], {}, "invalid"])
def test_imager_rejects_malformed_audition(malformed: object) -> None:
    chain = EffectChain(48000, realtime_resources=True)
    chain.set_params(_imager_entries({"_imager_audition": malformed}))
    assert not chain.is_active


def test_imager_preview_changes_sound_and_never_survives_apply(qtbot: QtBot) -> None:
    panel = AudioMixerPanel()
    qtbot.addWidget(panel)
    panel.register_track("A", "Track A", "A1")
    strip = panel._track_mixers["A"]
    instance = TrackEffectInstance("imager", {"width": 120.0}, instance_id="image")
    strip.set_effect_chain(EffectChainParams((instance,)))
    live = EffectChain(48000, realtime_resources=True)
    live.set_params(_imager_entries(instance.params))
    strip.track_effects_preview.connect(
        lambda _track, chain: live.set_params(_imager_entries(chain.effects[0].params)),
    )
    strip._open_insert_effect(instance)
    dialog = strip._imager_dialogs["image"]
    imager = dialog.panel
    signal = np.array([[0.2, 0.3], [-0.1, 0.4]], dtype=np.float32)
    normal = live.process(signal)
    try:
        assert panel.spectrum_targets() == {"A": frozenset({"image"})}
        assert imager.vectorscope._correlation is None
        assert not imager._demo_timer.isActive()
        live.set_spectrum_instances(strip.spectrum_targets())
        live.process(signal)
        strip.update_track_telemetry({"taps": live.tap_levels()})
        assert imager.vectorscope._correlation is not None
        assert imager.vectorscope._processed_samples
        assert imager.vectorscope._trail_history

        for button, mode in ((imager.ms_monitor.btn_solo_m, "Mid"),
                             (imager.ms_monitor.btn_solo_s, "Side"),
                             (imager.ms_monitor.btn_mono_chk, "Mono")):
            with qtbot.waitSignal(dialog.preview):
                button.click()
            if mode == "Side":
                expected = (normal[0] - normal[1]) * 0.5
                np.testing.assert_allclose(live.process(signal), [expected, -expected], atol=1e-8)
            else:
                expected = (normal[0] + normal[1]) * 0.5
                np.testing.assert_allclose(live.process(signal), [expected, expected], atol=1e-8)
            offline = EffectChain(48000)
            offline.set_params(_imager_entries(dialog.current_params()))
            np.testing.assert_allclose(offline.process(signal), normal)
            assert "_imager_audition" not in dialog._preset_params()

        dialog.apply()
        assert dialog.current_params() == instance.params
        assert imager.get_state()["solo_mode"] == "None"
        np.testing.assert_array_equal(live.process(signal), normal)

        with qtbot.waitSignal(dialog.preview):
            imager.ms_monitor.btn_solo_s.click()
        imager.ms_monitor.btn_solo_s.click()
        with qtbot.waitSignal(dialog.preview):
            dialog.apply()
        np.testing.assert_array_equal(live.process(signal), normal)

        strip.update_track_telemetry({"silent": True})
        assert imager.vectorscope._correlation is None
        assert not imager.vectorscope._trail_history
        assert imager.ms_monitor.meter_canvas._mid_peak == -60.0
    finally:
        dialog.close()
    assert panel.spectrum_targets() == {}


def test_clip_imager_audition_modifies_only_live_playback(qtbot: QtBot) -> None:
    signal = np.repeat(np.array([[0.2], [-0.2]], dtype=np.float32), 96, axis=1)

    class Decoder:
        _clip_id = "clip"

        def decode_samples_for_frame(self, _frame: float, _fps: float, _count: int) -> np.ndarray:
            return signal

    audio = AudioMixer()
    audio.add_track("track", Decoder(), AudioTrackState(track_id="track"))
    effect = EffectInstance(effect_type="imager", effect_id="image", params={"width": 100.0})
    clip = ClipInstance(clip_id="clip", track_id="track", asset_id="asset", effects=(effect,),
                        source_start=0, source_duration=50, timeline_start=0, timeline_duration=50)
    track = EditTrack(track_id="track", track_type="audio", clips=(clip,))
    timeline = TimelineSequence(audio_tracks=(track,))
    playback = EditControllerMixin()
    playback.edit_page = SimpleNamespace(_current_timeline=timeline)
    playback._get_mixer_audio_manager = lambda: audio
    manager = ClipAudioEffectDialogManager()
    playback._clip_audio_fx_dialog_mgr = manager
    manager.clip_effect_preview.connect(playback._on_edit_clip_effect_preview)
    manager.clip_effect_preview_finished.connect(playback._on_edit_clip_effect_preview_finished)
    dialog = manager.open_dialog("imager", "clip", "image", dict(effect.params), enabled=True)
    assert dialog is not None
    audio.set_clip_effects("clip", clip_fx_entries(clip.effects), track_id="track")
    normal, _ = audio.mix_samples(0, 25.0, signal.shape[1])
    try:
        with qtbot.waitSignal(manager.clip_effect_preview):
            dialog.panel.ms_monitor.btn_mono_chk.click()
        audition, _ = audio.mix_samples(0, 25.0, signal.shape[1])
        np.testing.assert_array_equal(audition, np.zeros_like(signal))
        assert timeline.get_clip_by_id("clip").effects[0].params == {"width": 100.0}

        offline = EffectChain(48000)
        offline.set_params(_imager_entries(dialog.current_params()))
        np.testing.assert_array_equal(offline.process(signal), signal)
        with qtbot.waitSignal(manager.clip_effect_preview_finished):
            dialog.apply()
        np.testing.assert_array_equal(audio.mix_samples(0, 25.0, signal.shape[1])[0], normal)
    finally:
        dialog.close()


def test_spatial_panner_refreshes_its_matrix_and_boom_state() -> None:
    panner = SpatialPanner(48000)
    params = {"enabled": True, "spread_auto": True, "fb": 20.0, "rotate": 25.0}
    panner.set_params(params)
    wave = np.ones((6, 64), dtype=np.float32) * 0.1
    pan_ramp = np.linspace(-1.0, 1.0, wave.shape[1], dtype=np.float32)
    expected = panner.process(wave, float(pan_ramp[wave.shape[1] // 2]))
    np.testing.assert_array_equal(panner.process(wave, -1.0, pan_ramp=pan_ramp), expected)
    np.testing.assert_array_equal(expected[2:], wave[2:])
    panner.set_params(params | {"fb": -80.0, "rotate": -45.0})
    assert not np.allclose(panner.process(wave, 0.0)[:2], expected[:2])

    boom = params | {"boom_on": True, "boom_gain": -3.0, "boom_route": "Only"}
    panner.set_params(boom)
    expected_boom = panner.process(wave, 0.0)
    panner.process(wave, 0.0)
    panner.set_params(boom | {"boom_on": False})
    panner.set_params(boom)
    np.testing.assert_array_equal(panner.process(wave, 0.0), expected_boom)


def test_automated_bus_spatial_panner_matches_live_and_export() -> None:
    wave = np.random.default_rng(3).normal(0.0, 0.035, (2, 1920)).astype(np.float32)

    class Decoder:
        def decode_samples_for_frame(self, _frame: float, _fps: float, _count: int) -> np.ndarray:
            return wave

    params = {"enabled": True, "spread": 61.0, "spread_auto": False, "rotate": 30.0}
    track_curve = KeyframeTrack(param_name="pan", min_val=-1.0, max_val=1.0)
    track_curve.add_keyframe(Keyframe(frame=0, value=-0.8, interpolation=InterpolationType.LINEAR))
    track_curve.add_keyframe(Keyframe(frame=1, value=0.9, interpolation=InterpolationType.LINEAR))
    audio = AudioMixer()
    audio.add_track("track", Decoder(), AudioTrackState(track_id="track"))
    audio.register_bus_track("bus", AudioTrackState(track_id="bus", pan=0.4))
    audio.set_track_routing("bus", None, [], is_bus=True)
    audio.set_track_routing("track", "bus", [])
    audio.set_track_panner("bus", params)
    audio.set_track_automation("bus", {"pan": track_curve})
    audio.set_track_automation_mode("bus", "read")
    live, _ = audio.mix_samples(0, 25.0, wave.shape[1], 2)

    track = EditTrack(track_id="track", track_type="audio", output_bus_id="bus")
    bus = EditTrack(track_id="bus", track_type="audio", is_bus=True, pan=0.4, panner=params,
                    automation={"pan": track_curve}, automation_mode="read", index=1)
    renderer = AudioGraphRenderer(48000)
    renderer.configure_from_sequence(TimelineSequence(audio_tracks=(track, bus)))
    processed = renderer.render_track_block("track", wave, 0, fps=25.0)
    assert processed is not None
    rendered = np.zeros_like(wave)
    renderer.render_bus_stage({"bus": processed}, rendered, has_solo=False, fps=25.0)
    np.testing.assert_allclose(live, renderer.apply_master(rendered), atol=1e-7)
