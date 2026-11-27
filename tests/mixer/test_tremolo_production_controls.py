"""Production tremolo monitoring and snapshot/phase behavior."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QInputDialog, QPushButton
from pytestqt.qtbot import QtBot

from src.features.edit.core import effect_preset
from src.features.edit.core.audio_dsp import EffectChain
from src.features.edit.core.audio_effects.modulation_tools import TremoloAutoPanEffect
from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.features.edit.core.timeline_v2 import (
    EditTrack, EffectChainParams, TimelineSequence, TrackEffectInstance, TremoloAutoPanParams,
)
from src.features.mixer.effects.production_dialog import ProductionEffectDialog
from src.shared.ui.audio_mixer.track_mixer_widget import TrackMixerWidget


def _entry(params: dict, version: int = 5) -> list[tuple]:
    return [("tremolo_auto_pan", params, True, "motion", version)]


@pytest.mark.parametrize("version", [0, 4, 5])
def test_preview_snapshot_drops_removed_tempo_and_waveform(version: int) -> None:
    chain = EffectChain(48000)
    signal = np.ones((2, 480), dtype=np.float32)
    original = {"mode": "tremolo", "rate_hz": 5.0, "depth": 0.8}
    chain.set_params(_entry(original, version))
    chain.process(signal)

    preview = original | {"waveform": "square", "sync": True, "bpm": 90.0, "sync_div": "1/8T"}
    chain.set_params(_entry(preview, version))
    chain.process(signal)
    analysis = chain.tap_levels()["motion"]["analysis"]
    assert analysis["effective_rate_hz"] == pytest.approx(4.5)
    boundary_phase = analysis["phase_cycles"]

    chain.set_params(_entry(original, version))
    restored = chain.process(signal)
    phase = boundary_phase + np.arange(signal.shape[-1]) * 5.0 / 48000.0
    expected = 1.0 - 0.8 * (0.5 - 0.5 * np.sin(2.0 * np.pi * phase))
    np.testing.assert_allclose(restored[0], expected, atol=1e-7)
    np.testing.assert_array_equal(restored[0], restored[1])
    assert chain.tap_levels()["motion"]["analysis"]["bpm_source"] == "free"


@pytest.mark.parametrize("mode,default_phase", [("tremolo", 0.0), ("auto_pan", 180.0), ("dual", 180.0)])
def test_stereo_phase_changes_right_channel_and_keeps_default_pcm(mode: str, default_phase: float) -> None:
    signal = np.ones((2, 14400), dtype=np.float32)
    params = {"mode": mode, "depth": 0.9, "waveform": "square", "smooth_ms": 8.0}
    baseline = TremoloAutoPanEffect()
    baseline.set_params(params)
    expected = baseline.process(signal)
    shifted = TremoloAutoPanEffect()
    shifted.set_params(params | {"phase_offset_deg": default_phase})
    np.testing.assert_array_equal(shifted.process(signal), expected)
    changed = params | {"phase_offset_deg": 90.0}
    assert TremoloAutoPanParams.from_dict(changed).to_dict()["phase_offset_deg"] == 90.0
    shifted.set_params(changed)
    shifted.reset_state()
    output = shifted.process(signal)
    np.testing.assert_array_equal(output[0], expected[0])
    assert np.max(np.abs(output[1] - expected[1])) > 0.15
    shifted.reset_state()
    pieces = [shifted.process(signal[:, i:i + 321]) for i in range(0, signal.shape[-1], 321)]
    np.testing.assert_array_equal(np.concatenate(pieces, axis=-1), output)
    assert shifted.collect_telemetry()["mod_gain_lr"] == pytest.approx(output[:, -1])


def test_tremolo_window_auditions_phase_and_clears_monitoring(qtbot: QtBot) -> None:
    strip = TrackMixerWidget("track", "Track")
    qtbot.addWidget(strip)
    instance = TrackEffectInstance("tremolo_auto_pan", {"mode": "tremolo", "waveform": "", "depth": 1.0},
                                   instance_id="motion")
    strip.set_effect_chain(EffectChainParams((instance,)))
    live = EffectChain(48000, realtime_resources=True)
    live.set_params(_entry(instance.params))
    strip.track_effects_preview.connect(
        lambda _track, chain: live.set_params(_entry(chain.effects[0].params)),
    )
    strip._open_insert_effect(instance)
    dialog = strip._fx_param_dialogs["motion"]
    panel = dialog.panel
    signal = np.ones((2, 480), dtype=np.float32)
    normal = live.process(signal)
    try:
        assert panel.rate_card.btn_retrig.isHidden()
        assert not panel.wave_card.c_shape.isEnabled()
        assert not panel.depth_card.c_center.isEnabled()
        assert not panel._anim_timer.isActive()
        assert not panel.mod_meter._active
        assert panel.get_state()["phase_offset"] == 0.0
        assert panel.get_state()["sync_div"] == "1/4"
        assert panel.get_state()["smooth"] == 0.0

        with qtbot.waitSignal(dialog.preview) as preview:
            panel.depth_card._set_phase_preset(180.0)
        assert preview.args[0]["phase_offset_deg"] == 180.0
        assert preview.args[0]["waveform"] == "sine"
        live.reset_state()
        changed = live.process(signal)
        np.testing.assert_allclose(changed[0], normal[0], atol=1e-7)
        assert np.max(np.abs(changed[1] - changed[0])) > 0.1
        strip.update_track_telemetry({"taps": live.tap_levels()})
        assert panel.mod_meter._active
        assert (panel.mod_meter._l_val, panel.mod_meter._r_val) == pytest.approx(changed[:, -1])
        assert panel.visualizer._phase == pytest.approx((signal.shape[-1] - 1) * 4.0 / 48000.0)
        strip.update_track_telemetry({"silent": True})
        assert not panel.mod_meter._active
        assert panel.visualizer._live_gains is None

        dialog.reject()
        live.reset_state()
        np.testing.assert_array_equal(live.process(signal), normal)
    finally:
        strip.close_all_dialogs()


def test_ordinary_tremolo_changes_do_not_materialize_a_waveform(qtbot: QtBot) -> None:
    params = {"mode": "dual", "rate_hz": 4.0, "depth": 0.7}
    dialog = ProductionEffectDialog("tremolo_auto_pan", "Legacy", params, enabled=True)
    qtbot.addWidget(dialog)
    dialog.panel.depth_card.c_depth._on_pill_edited("invalid")
    assert dialog.current_params() == params
    dialog.panel.depth_card.c_depth._on_pill_edited("80")
    assert dialog.current_params() == params | {"depth": 0.8}
    qtbot.mouseClick(dialog.panel.power_switch, Qt.MouseButton.LeftButton)
    assert dialog.current_params() == params | {"depth": 0.8, "enabled": False}


def test_tremolo_preset_can_restore_the_legacy_kernel(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(effect_preset, "_store_path", lambda: str(tmp_path / "presets.json"))
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: ("Legacy movement", True))
    params = {"mode": "auto_pan", "depth": 0.85}
    legacy = ProductionEffectDialog("tremolo_auto_pan", "Legacy", params)
    qtbot.addWidget(legacy)
    save = next(button for button in legacy.panel.findChildren(QPushButton, "ToolBtn") if button.text() == "+")
    save.click()

    newer = ProductionEffectDialog("tremolo_auto_pan", "New", {
        "mode": "dual", "waveform": "square", "phase_offset_deg": 90.0,
        "smooth_ms": 12.0, "sync": True, "pan_width_pct": 12.0,
    })
    qtbot.addWidget(newer)
    combo = newer.panel.preset_combo
    combo.setCurrentIndex(combo.findText("Legacy movement"))
    assert newer.current_params()["waveform"] == ""
    assert newer.panel.get_state()["phase_offset"] == 180.0
    signal = np.ones((2, 480), dtype=np.float32)
    original, restored = TremoloAutoPanEffect(), TremoloAutoPanEffect()
    original.set_params(params)
    restored.set_params(newer.current_params())
    np.testing.assert_array_equal(restored.process(signal), original.process(signal))


def test_tremolo_track_uses_the_same_phase_and_timing_during_export() -> None:
    signal = np.random.default_rng(5).normal(0.0, 0.1, (2, 1920)).astype(np.float32)

    class Decoder:
        def decode_samples_for_frame(self, _frame: float, _fps: float, _count: int) -> np.ndarray:
            return signal

    chain = EffectChainParams((TrackEffectInstance("tremolo_auto_pan", {
        "mode": "dual", "waveform": "triangle", "phase_offset_deg": 90.0,
        "sync": True, "sync_div": "1/4D", "bpm": 145.0, "smooth_ms": 8.0,
    }, instance_id="motion"),))
    mixer = AudioMixer()
    mixer.add_track("track", Decoder(), AudioTrackState(track_id="track"))
    mixer.set_track_effect_chain("track", chain)
    renderer = AudioGraphRenderer(48000)
    renderer.configure_from_sequence(TimelineSequence(
        audio_tracks=(EditTrack(track_id="track", track_type="audio", effect_chain=chain),),
    ))
    for frame in range(3):
        live, _ = mixer.mix_samples(frame, 25.0, 1920)
        rendered = renderer.render_track_block("track", signal, frame, fps=25.0)
        assert rendered is not None
        np.testing.assert_array_equal(live, renderer.apply_master(rendered))
