"""Post-normalization protection must not run the user's Master FX twice."""

import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_export import TimelineExportConfig, TimelineExporter
from src.features.edit.core.timeline_v2 import (
    ClipInstance, DelayParams, EditTrack, EffectChainParams,
    TimelineSequence, TrackEffectInstance,
)
from src.shared.media.metadata import Rational
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState


SR = 48000


def _write_pcm(path: Path, signal: np.ndarray) -> None:
    pcm = np.rint(signal.T * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as file:
        file.setnchannels(signal.shape[0])
        file.setsampwidth(2)
        file.setframerate(SR)
        file.writeframes(pcm.tobytes())


def _master_delay() -> EffectChainParams:
    return EffectChainParams(effects=(TrackEffectInstance(
        effect_type="delay", instance_id="master-delay",
        params=DelayParams(left_delay=200.0, right_delay=200.0,
                           left_feedback=0.0, right_feedback=0.0,
                           stereo_blend=0.0, dry_wet=100.0).to_dict(),
    ),))


def test_limiter_only_pass_preserves_master_delay_time() -> None:
    renderer = AudioGraphRenderer(float(SR))
    renderer.configure_from_sequence(TimelineSequence(master_effect_chain=_master_delay()))
    impulse = np.zeros((2, SR), dtype=np.float32)
    impulse[:, 0] = 0.3
    processed = renderer.apply_master(impulse)
    first = np.flatnonzero(np.abs(processed[0]) > 1e-4)[0]
    assert first == round(0.2 * SR)
    protected = renderer.limit_master_output(processed.copy(), reset_state=True)
    np.testing.assert_array_equal(protected, processed)


def test_new_limiter_pass_does_not_inherit_end_of_previous_pass_gain() -> None:
    renderer = AudioGraphRenderer(float(SR))
    renderer.apply_master(np.full((2, 2000), 2.0, dtype=np.float32))
    quiet = np.full((2, 2000), 0.1, dtype=np.float32)
    np.testing.assert_array_equal(
        renderer.limit_master_output(quiet, reset_state=True), quiet)


def test_normalization_with_zero_gain_preserves_exported_master_fx(tmp_path: Path) -> None:
    signal = np.zeros((SR, 2), dtype=np.int16)
    signal[0] = 8192
    path = tmp_path / "impulse.wav"
    with wave.open(str(path), "wb") as file:
        file.setnchannels(2)
        file.setsampwidth(2)
        file.setframerate(SR)
        file.writeframes(signal.tobytes())
    asset = MediaAsset("a", str(path), "audio", "Impulse", {})
    project = EditProject("p", "Project", assets={"a": asset})
    clip = ClipInstance("c", "a", "t", 0, 25, 0, 25)
    sequence = TimelineSequence(
        fps=Rational(25, 1), audio_tracks=(
            EditTrack("t", "Track", "audio", 0, clips=(clip,)),
        ), master_effect_chain=_master_delay(),
    )
    plain = TimelineExporter(project, sequence, Mock())._render_audio_master(
        0, 25, TimelineExportConfig(output_path=str(tmp_path / "plain.mov")),
        encoder=Mock())
    # Fix the loudness decision to 0 dB so this test isolates the export
    # post-processing path from the analyzer's independent calibration.
    report = SimpleNamespace(normalize_gain_db=0.0, integrated_lufs=-23.0)
    with patch("src.features.edit.core.loudness_analyzer.analyze_loudness",
               return_value=report):
        normalized = TimelineExporter(project, sequence, Mock())._render_audio_master(
            0, 25, TimelineExportConfig(
                output_path=str(tmp_path / "normalized.mov"), loudness_target_lufs=-23.0),
            encoder=Mock())
    np.testing.assert_array_equal(normalized, plain)


def test_export_keeps_sidechain_source_referenced_only_by_master(tmp_path: Path) -> None:
    t = np.arange(SR) / SR
    programme = np.stack([0.05 * np.sin(2.0 * np.pi * 100.0 * t)] * 2)
    key = np.stack([0.9 * np.sin(2.0 * np.pi * 880.0 * t)] * 2)
    assets = {}
    tracks = []
    for name, signal in (("programme", programme), ("key", key)):
        path = tmp_path / f"{name}.wav"
        _write_pcm(path, signal)
        assets[name] = MediaAsset(name, str(path), "audio", name, {})
        tracks.append(EditTrack(
            name, name, "audio", len(tracks),
            clips=(ClipInstance(f"{name}-clip", name, name, 0, 25, 0, 25),),
            output_bus_id="muted-bus" if name == "key" else None,
        ))
    tracks.append(EditTrack("muted-bus", "Muted Bus", "audio", 2,
                            is_bus=True, is_muted=True))
    master_chain = EffectChainParams(effects=(TrackEffectInstance(
        effect_type="compressor", instance_id="master-comp", params={
            "enabled": True, "threshold_db": -30.0, "ratio": 10.0,
            "knee": 0.0, "attack_ms": 0.0, "release_ms": 50.0,
            "mix": 100.0, "makeup_gain": 0.0,
            "sc_on": True, "sc_source": "key",
        },
    ),))
    sequence = TimelineSequence(fps=Rational(25, 1), audio_tracks=tuple(tracks),
                                master_effect_chain=master_chain)
    project = EditProject("p", "Project", assets=assets)
    exported = TimelineExporter(project, sequence, Mock())._render_audio_master(
        0, 25, TimelineExportConfig(output_path=str(tmp_path / "sc.mov")), encoder=Mock())
    renderer = AudioGraphRenderer(float(SR))
    renderer.configure_from_sequence(sequence)
    # Independent full-buffer oracle uses the explicitly supplied source,
    # rather than adding a dummy Bus consumer to make the tap cache work.
    quantized_programme = (np.rint(programme * 32767.0) / 32768.0).astype(np.float32)
    quantized_key = (np.rint(key * 32767.0) / 32768.0).astype(np.float32)
    expected = renderer.apply_master(quantized_programme, fps=25.0,
                                     sidechain_taps={"key": quantized_key})
    np.testing.assert_allclose(exported, expected, atol=1e-6, rtol=1e-5)


def _mute_curve() -> KeyframeTrack:
    curve = KeyframeTrack(param_name="mute", min_val=0.0, max_val=1.0)
    curve.add_keyframe(Keyframe(frame=0, value=0.0, interpolation=InterpolationType.HOLD))
    curve.add_keyframe(Keyframe(frame=1, value=1.0, interpolation=InterpolationType.HOLD))
    return curve


@pytest.mark.parametrize("automated", [False, True])
def test_master_mute_gates_existing_delay_tail_in_playback(automated: bool) -> None:
    block = 960
    impulse = np.zeros((2, block), dtype=np.float32)
    impulse[:, 0] = 0.3
    decoder = Mock(spec=["decode_samples_for_frame", "close"])
    decoder.decode_samples_for_frame.side_effect = [impulse] + [np.zeros_like(impulse)] * 14
    mixer = AudioMixer()
    mixer.add_track("t", decoder, AudioTrackState(track_id="t"))
    mixer.set_master_effect_chain(_master_delay())
    if automated:
        mixer.set_master_automation({"mute": _mute_curve()})
        mixer.set_master_automation_mode("read")
    mixer.mix_samples(0, 50.0, block)
    if not automated:
        mixer.set_master_muted(True)
    muted = np.concatenate([mixer.mix_samples(frame, 50.0, block)[0]
                            for frame in range(1, 15)], axis=-1)
    assert np.max(np.abs(muted)) == 0.0


def test_master_mute_automation_gates_existing_delay_tail_in_renderer() -> None:
    renderer = AudioGraphRenderer(float(SR))
    renderer.configure_from_sequence(TimelineSequence(
        master_effect_chain=_master_delay(), master_automation={"mute": _mute_curve()},
        master_automation_mode="read"))
    impulse = np.zeros((2, 960), dtype=np.float32)
    impulse[:, 0] = 0.3
    renderer.apply_master(impulse, start_frame=0, fps=50.0)
    muted = np.concatenate([
        renderer.apply_master(np.zeros_like(impulse), start_frame=frame, fps=50.0)
        for frame in range(1, 15)], axis=-1)
    assert np.max(np.abs(muted)) == 0.0


def test_master_telemetry_carries_pre_fader_input_peak() -> None:
    decoder = Mock(spec=["decode_samples_for_frame", "close"])
    decoder.decode_samples_for_frame.return_value = np.full((2, 960), 0.2, dtype=np.float32)
    mixer = AudioMixer()
    mixer.add_track("t", decoder, AudioTrackState(track_id="t"))
    mixer.set_master_volume(0.5)
    _output, levels = mixer.mix_samples(0, 50.0, 960)
    assert levels["-1"].pre_peak == pytest.approx(0.2)
    assert levels["-1"].peak_l == pytest.approx(0.1)
