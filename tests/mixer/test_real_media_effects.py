"""Decoded audio through the production panels, models and playback mixer.

This is an integration gate, not a substitute for hardware listening or native
mouse/keyboard QA. No decoder or DSP processor is mocked.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
import wave

import numpy as np
import pytest

from src.features.edit.core.audio_playback import AudioDecoder, AudioMixer, AudioTrackState
from src.features.edit.core.timeline_v2 import EffectChainParams, SpatialPannerParams, TrackEffectInstance
from src.features.mixer.effects.bindings_core import set_path
from src.features.mixer.effects.production_dialog import ProductionEffectDialog
from src.shared.ui.audio_mixer.track_mixer_widget import _effect_default_params

MEDIA = Path(__file__).resolve().parents[1] / "test_videos" / "2K_h264.mp4"
RATE, FPS, BLOCK, BLOCKS = 48000, 25, 1920, 30

# One audible, production-backed adjustment per insert. Nested paths are panel
# state keys, so the same mappings used by a person editing a knob are exercised.
CONTROLS = {
    "strip_eq": {"master_gain": -6.0},
    "expander": {"eg.threshold": -5.0},
    "noise_gate": {"eg.threshold": -5.0},
    "compressor": {"comp.threshold": -40.0},
    "limiter": {"lim.threshold": -30.0},
    "dynamics": {"comp.threshold": -40.0},
    "reverb": {"dry_wet": 65.0},
    "delay": {"dry_wet": 75.0},
    "saturation": {"drive": 75.0},
    "imager": {"width": 25.0, "output_gain": -6.0},
    "voice_changer": {"pitch.enabled": True, "pitch.semitones": 5.0, "formant.shift": 2.0},
    "spectral_mangler": {"ring.enabled": True},
    "exciter": {"bands.1.enabled": True, "bands.1.amount": 60.0, "output_gain": -6.0},
    "subharmonic": {"source.oct1_level": 90.0, "output.mix": 80.0},
    "de_esser": {"threshold": -55.0, "output_gain": -6.0},
    "true_peak_limiter": {"ceiling": -20.0},
    "dynamic_eq": {"bands.3.gain": -9.0},
    "bus_comp": {"threshold": -40.0},
    "multiband_comp": {"bands.1.threshold": -40.0},
    "convolution_reverb": {"dry_wet": 70.0},
    "transient_designer": {"output_gain": -6.0},
    "tremolo_auto_pan": {"depth": 95.0},
    "chorus_flanger": {"chorus.mix": 85.0},
    "phaser": {"phaser.mix": 85.0},
}


def render_mix(
    chain: EffectChainParams | None, target: str, qtbot: Any,
    panner: dict[str, Any] | None = None,
    *, media: Path = MEDIA, source_start: int = 125,
) -> np.ndarray:
    decoder = AudioDecoder()
    assert decoder.open(media, timeline_fps=FPS, source_start=source_start, source_fps=FPS)
    mixer = AudioMixer()
    mixer.add_track("real", decoder, AudioTrackState(track_id="real"))
    try:
        destination = "real"
        if target == "bus":
            destination = "bus"
            mixer.set_track_state(destination, AudioTrackState(track_id=destination))
            mixer.set_track_routing(destination, None, [], is_bus=True)
            mixer.set_track_routing("real", destination, [])
        if target == "track":
            mixer.set_track_effect_chain("real", chain)
        elif target == "bus":
            mixer.set_track_effect_chain(destination, chain)
        elif target == "master":
            mixer.set_master_effect_chain(chain)
        else:
            raise ValueError(f"Unknown mixer target: {target}")
        if panner is not None:
            mixer.set_track_state(destination, AudioTrackState(
                track_id=destination, pan=panner["pan"],
            ))
            mixer.set_track_panner(destination, panner["panner"])
        if chain and chain.effects[0].effect_type == "convolution_reverb" and chain.effects[0].enabled:
            qtbot.waitUntil(lambda: all(effect.is_active for fx in mixer._effect_chains.values()
                                       for effect in fx._effects), timeout=5000)
        chunks = [mixer.mix_samples(frame, FPS, BLOCK)[0] for frame in range(BLOCKS)]
        output = np.concatenate(chunks, axis=1)
        assert output.shape == (2, BLOCK * BLOCKS)
        assert np.isfinite(output).all()
        assert np.max(np.abs(output)) > 1e-5
        return output
    finally:
        mixer.set_track_effect_chain("real", None)
        if target == "bus":
            mixer.set_track_effect_chain("bus", None)
        mixer.set_master_effect_chain(None)
        decoder.close()


@pytest.fixture(scope="module")
def ir_path(tmp_path_factory: Any) -> str:
    path = tmp_path_factory.mktemp("real-media-ir") / "echo.wav"
    ir = np.zeros((4800,), dtype="<i2")
    ir[0], ir[2400] = 16000, 8000
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        output.writeframes(ir.tobytes())
    return str(path)


@pytest.fixture(scope="module")
def subharmonic_media(tmp_path_factory: Any) -> Path:
    """Give the detector a controlled bass fundamental in both input channels."""
    path = tmp_path_factory.mktemp("real-media-subharmonic") / "bass.wav"
    left = (7000.0 * np.sin(2 * np.pi * 90.0 * np.arange(RATE * 2) / RATE)).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
        output.writeframes(np.column_stack((left, -left)).tobytes())
    return path


@pytest.mark.real_media
@pytest.mark.parametrize("effect_type", CONTROLS)
@pytest.mark.parametrize("target", ["track", "bus", "master"])
def test_production_panel_real_playback_and_bypass(
    qtbot: Any, effect_type: str, target: str, ir_path: str, caplog: Any,
    request: pytest.FixtureRequest,
) -> None:
    media = request.getfixturevalue("subharmonic_media") if effect_type == "subharmonic" else MEDIA
    source_start = 0 if effect_type == "subharmonic" else 125
    if not media.is_file():
        pytest.skip("Repository 2K_h264.mp4 is unavailable")
    defaults = _effect_default_params(effect_type)
    if effect_type == "convolution_reverb":
        defaults["ir_path"] = ir_path
    defaults["future_metadata"] = {"preserve": True}
    dialog = ProductionEffectDialog(effect_type, "Real AAC", defaults, enabled=True)
    qtbot.addWidget(dialog)
    assert dialog.current_params() == defaults
    state = dialog.panel.get_state()
    for path, value in CONTROLS[effect_type].items():
        set_path(state, path, value)
    dialog.panel.set_state(state)
    dialog.panel.stateChanged.emit(dialog.panel.get_state())
    params = dialog.current_params()
    assert params != defaults, effect_type
    assert params["future_metadata"] == {"preserve": True}
    with qtbot.waitSignal(dialog.committed, timeout=1000) as committed:
        dialog.apply()
    assert committed.args[0] == params
    instance = TrackEffectInstance(effect_type=effect_type, params=params)
    # Reopening a persisted snapshot must keep the exact canonical parameters.
    persisted = TrackEffectInstance.from_dict(instance.to_dict())
    dialog.set_current_params(persisted.params, enabled=True)
    assert dialog.current_params() == params
    active = render_mix(EffectChainParams((persisted,)), target, qtbot, media=media, source_start=source_start)
    bypassed = render_mix(EffectChainParams((replace(persisted, enabled=False),)), target, qtbot,
                          media=media, source_start=source_start)
    dry = render_mix(None, target, qtbot, media=media, source_start=source_start)
    np.testing.assert_array_equal(bypassed, dry)
    delta = float(np.sqrt(np.mean((active.astype(np.float64) - dry) ** 2)))
    assert delta > 1e-6, effect_type
    request.node.user_properties.extend([
        ("effect_type", effect_type), ("target", target), ("delta_rms", delta),
        ("output_peak", float(np.max(np.abs(active)))), ("bypass_max_error", 0.0),
        ("samples_per_channel", int(active.shape[1])),
    ])
    assert not [record for record in caplog.records if record.levelname in {"ERROR", "CRITICAL"}]
    dialog.close()


@pytest.mark.real_media
@pytest.mark.parametrize("target", ["track", "bus"])
def test_spatial_panner_real_media_changes_channel_image(qtbot: Any, target: str) -> None:
    if not MEDIA.is_file():
        pytest.skip("Repository 2K_h264.mp4 is unavailable")
    original = {"pan": 0.0, "panner": SpatialPannerParams.default().to_dict()}
    dialog = ProductionEffectDialog("spatial_panner", "Real AAC", original)
    qtbot.addWidget(dialog)
    state = dialog.panel.get_state()
    state.update(lr=75.0, spread=10.0)
    dialog.panel.set_state(state)
    payload = dialog.current_params()
    assert payload["pan"] == pytest.approx(.75)
    active = render_mix(None, target, qtbot, panner=payload)
    dry = render_mix(None, target, qtbot)
    assert np.max(np.abs(active - dry)) > 1e-4
    energy = np.mean(active.astype(np.float64) ** 2, axis=1)
    assert energy[1] > energy[0] * 2
    dialog.close()


@pytest.mark.real_media
@pytest.mark.parametrize("processor", ["de_click", "de_hum", "pitch_correction", "doubler"])
def test_selected_offline_tools_on_video_audio(processor: str) -> None:
    from src.features.edit.core.audio_edit import make_step, new_audio_edit, render_audio_edit_full

    if not MEDIA.is_file():
        pytest.skip("Repository 2K_h264.mp4 is unavailable")
    decoder = AudioDecoder()
    assert decoder.open(MEDIA, timeline_fps=FPS, source_start=125, source_fps=FPS)
    try:
        pcm = decoder.decode_samples_for_frame(0, FPS, RATE)
    finally:
        decoder.close()
    before = pcm.copy()
    edit = new_audio_edit([make_step(processor, 4800, 43200, [0])])
    output = render_audio_edit_full(pcm, edit, sample_rate=RATE)
    assert output.shape == before.shape and np.isfinite(output).all()
    np.testing.assert_array_equal(pcm, before)
    np.testing.assert_array_equal(output[1], before[1])
    np.testing.assert_array_equal(output[0, :4800], before[0, :4800])
    np.testing.assert_array_equal(output[0, 43200:], before[0, 43200:])
