"""Independent counterexamples from the production mixing completion review."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from src.features.edit.core.audio_graph_snapshot import build_audio_graph_snapshot
from src.features.edit.core.automation_recorder import (
    _FaderSample, simplify_samples, simplify_volume_db,
)
from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.delivery_profile import (
    DeliveryProfile, MeasurementResult, measurement_rejection,
    publish_delivery_atomic,
)
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack


def test_volume_simplification_uses_timestamps_and_preserves_exact_mute() -> None:
    samples = [_FaderSample(0, 0.1), _FaderSample(1, 0.55),
               _FaderSample(100, 1.0)]
    result = simplify_volume_db(samples)
    assert [p.frame for p in result] == [0, 1, 100]
    assert simplify_volume_db([_FaderSample(0, 0.0), _FaderSample(1, 0.0),
                               _FaderSample(9, 0.0)])[0].value == 0.0
    deduped = simplify_volume_db([_FaderSample(0, 0.2), _FaderSample(0, 0.6)])
    assert [(p.frame, p.value) for p in deduped] == [(0, 0.6)]


def test_protected_rdp_segments_obey_replay_error_bound() -> None:
    # The outer line is flat. Keeping only the protected spike makes the new
    # piecewise interpolation at 2/8 wrong by 0.024, over twice the tolerance.
    samples = [_FaderSample(0, 0.0), _FaderSample(2, 0.0),
               _FaderSample(5, 0.04), _FaderSample(8, 0.0),
               _FaderSample(10, 0.0)]
    result = simplify_samples(samples, threshold=0.01, protected_frames={5})
    replay = np.interp([s.frame for s in samples], [s.frame for s in result],
                       [s.value for s in result])
    assert np.max(np.abs(replay - [s.value for s in samples])) <= 0.01


def _sequence() -> TimelineSequence:
    clip = ClipInstance(clip_id="c", asset_id="a", track_id="a",
                        timeline_start=0, timeline_duration=24,
                        source_start=0, source_duration=24)
    return TimelineSequence(audio_tracks=(
        EditTrack("a", "A", "audio", 0, clips=(clip,)),
        EditTrack("b", "B", "audio", 1, volume=0.5),
    ))


def test_clip_ownership_and_retime_contract_change_sound_hash() -> None:
    sequence = _sequence()
    clip = sequence.audio_tracks[0].clips[0]
    base = build_audio_graph_snapshot(sequence).stable_hash
    moved = replace(sequence, audio_tracks=(
        replace(sequence.audio_tracks[0], clips=()),
        replace(sequence.audio_tracks[1], clips=(replace(clip, track_id="b"),)),
    ))
    assert build_audio_graph_snapshot(moved).stable_hash != base
    for speed in (SpeedParams(reverse=True), SpeedParams(preserve_pitch=True)):
        changed = sequence.with_track_updated(replace(
            sequence.audio_tracks[0], clips=(replace(clip, speed=speed),)))
        assert build_audio_graph_snapshot(changed).stable_hash != base
    changed_duration = sequence.with_track_updated(replace(
        sequence.audio_tracks[0], clips=(replace(clip, source_duration=48),)))
    assert build_audio_graph_snapshot(changed_duration).stable_hash != base


@pytest.mark.parametrize("master", [False, True])
def test_automation_shape_changes_sound_hash(master: bool) -> None:
    curve = KeyframeTrack("volume", min_val=0.0, max_val=2.0)
    curve.add_keyframe(Keyframe(0, 0.2))
    curve.add_keyframe(Keyframe(24, 0.8))
    sequence = _sequence()
    if master:
        sequence = replace(sequence, master_automation={"volume": curve})
    else:
        sequence = sequence.with_track_updated(replace(
            sequence.audio_tracks[0], automation={"volume": curve}))
    base = build_audio_graph_snapshot(sequence).stable_hash
    changed_curve = KeyframeTrack.from_dict(curve.to_dict())
    changed_curve.add_keyframe(Keyframe(0, 0.2, interpolation=InterpolationType.HOLD))
    if master:
        changed = replace(sequence, master_automation={"volume": changed_curve})
    else:
        changed = sequence.with_track_updated(replace(
            sequence.audio_tracks[0], automation={"volume": changed_curve}))
    assert build_audio_graph_snapshot(changed).stable_hash != base


def test_fx_processing_order_changes_sound_hash() -> None:
    sequence = _sequence()
    clip = sequence.audio_tracks[0].clips[0]
    fx = (EffectInstance(effect_type="audio_gain", params={"gain_db": -3.0}, order=0),
          EffectInstance(effect_type="audio_gain", params={"gain_db": -8.0}, order=1))
    original = sequence.with_track_updated(replace(
        sequence.audio_tracks[0], clips=(replace(clip, effects=fx),)))
    reordered = sequence.with_track_updated(replace(
        sequence.audio_tracks[0], clips=(replace(clip, effects=(
            replace(fx[0], order=2), fx[1])),)))
    assert build_audio_graph_snapshot(original).stable_hash != \
        build_audio_graph_snapshot(reordered).stable_hash


def test_delivery_publish_restores_preexisting_files_on_late_failure(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.core import delivery_profile as module

    sources = tmp_path / "source"
    sources.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    for name in ("a.wav", "b.wav"):
        (sources / name).write_bytes(b"new-" + name.encode())
        (out / name).write_bytes(b"old-" + name.encode())
    original_replace = module.os.replace

    def fail_second(source: Path, target: Path) -> None:
        if Path(source).name.startswith("file-") and Path(target).name == "b.wav":
            raise OSError("injected publication failure")
        original_replace(source, target)

    monkeypatch.setattr(module.os, "replace", fail_second)
    with pytest.raises(OSError, match="injected"):
        publish_delivery_atomic({"A": sources / "a.wav", "B": sources / "b.wav"}, out)
    assert (out / "a.wav").read_bytes() == b"old-a.wav"
    assert (out / "b.wav").read_bytes() == b"old-b.wav"
    assert not list(out.glob(".delivery-tmp-*"))


def test_stem_metadata_cannot_escape_delivery_staging(tmp_path: Path) -> None:
    source = tmp_path / "a.wav"
    source.write_bytes(b"content")
    escaped = tmp_path / "escape"
    publish_delivery_atomic({str(escaped): source}, tmp_path / "out")
    assert not escaped.with_suffix(".wav").exists()
    assert (tmp_path / "out" / "a.wav").read_bytes() == b"content"


@pytest.mark.parametrize("overrides", [
    {"sample_count": 0}, {"start_sample": -1}, {"algorithm_version": ""},
    {"file_sha256": ""}, {"gate_mode": "dialogue_gated"},
])
def test_delivery_evidence_requires_complete_identity(overrides: dict) -> None:
    profile = DeliveryProfile(profile_id="technical", name="Technical",
                              target_lufs=None)
    base = MeasurementResult(stem_id="FullMix", status="complete", file_sha256="a",
                             sample_rate=48000, channel_order=("L", "R"),
                             sample_count=100, algorithm_version="test-v1",
                             measurements={"true_peak_dbtp": -3.0, "peak_dbfs": -4.0})
    assert measurement_rejection(base, profile) is None
    assert measurement_rejection(replace(base, **overrides), profile) is not None


def test_measurement_reload_cannot_launder_boolean_sample_range() -> None:
    profile = DeliveryProfile(profile_id="technical", name="Technical", target_lufs=None)
    data = MeasurementResult(
        stem_id="FullMix", status="complete", file_sha256="a", sample_rate=48000,
        channel_order=("L", "R"), algorithm_version="v1", sample_count=True,
        measurements={"true_peak_dbtp": -3.0, "peak_dbfs": -4.0}).to_dict()
    assert measurement_rejection(MeasurementResult.from_dict(data), profile) is not None


def test_solo_send_four_tuple_and_mute_obey_closure() -> None:
    from src.features.edit.core.audio_playback import compute_audible_solo_closure

    routing = {
        "solo": {"sends": [("wet", 0.5, "pre_fader", False),
                             ("muted", 0.5, "post_fader", True)]},
        "wet": {"is_bus": True}, "muted": {"is_bus": True},
        "safe": {"output_bus_id": "safe_bus"}, "safe_bus": {"is_bus": True},
    }
    assert compute_audible_solo_closure(list(routing), routing, ["solo"], ["safe"]) == {
        "solo", "wet", "safe", "safe_bus"}


@pytest.mark.parametrize("solo_bus", [False, True])
def test_solo_safe_realtime_stem_and_movie_parity(
        solo_bus: bool, qapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import Mock
    from src.features.edit.core.audio_playback import AudioDecoder, AudioMixer, AudioTrackState
    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.core.stem_export import StemSpec, StemType, render_stem_pcm
    from src.features.edit.core.timeline_export import TimelineExportConfig, TimelineExporter

    tracks = []
    for i, tid in enumerate(("source", "safe", "other")):
        tracks.append(EditTrack(tid, tid, "audio", i,
                                clips=(ClipInstance(tid, "asset", tid, 0, 2, 0, 2),),
                                is_solo=(tid == "source" and not solo_bus),
                                solo_safe=(tid == "safe"),
                                output_bus_id="bus" if tid == "source" else None))
    tracks.append(EditTrack("bus", "bus", "audio", 3, is_bus=True, is_solo=solo_bus))
    sequence = TimelineSequence(audio_tracks=tuple(tracks))

    class Decoder:
        def decode_samples_for_frame(self, frame: int, fps: float, num_samples: int) -> np.ndarray:
            return np.full((2, num_samples), 0.1, np.float32)

        def close(self) -> None:
            pass

    mixer = AudioMixer()
    for track in tracks:
        state = AudioTrackState(track.track_id, solo=track.is_solo, solo_safe=track.solo_safe)
        if track.is_bus:
            mixer.register_bus_track(track.track_id, state)
        else:
            mixer.add_track(track.track_id, Decoder(), state)
        mixer.set_track_routing(track.track_id, track.output_bus_id, [], is_bus=track.is_bus)
    # Incremental fader updates must retain the newly synchronized safe flag.
    mixer.update_track_volume("safe", 1.0)
    assert mixer.get_track_state("safe").solo_safe is True
    live, _ = mixer.mix_samples(0, 24.0, 2000, 2)
    np.testing.assert_allclose(live, 0.2, atol=1e-6)
    stem = render_stem_pcm(sequence, StemSpec(StemType.MASTER, "Master"),
                           decode_clip_fn=lambda _t, _c, _f, _fps, n:
                           np.full((2, n), 0.1, np.float32))
    np.testing.assert_allclose(stem, 0.2, atol=1e-6)
    source = tmp_path / "source.wav"
    source.touch()
    project = EditProject("p", "P", assets={
        "asset": MediaAsset("asset", str(source), "audio", "Audio", {})})
    monkeypatch.setattr(AudioDecoder, "open", lambda self, *args, **kwargs: True)
    monkeypatch.setattr(AudioDecoder, "decode_samples_for_frame", lambda self, frame, timeline_fps, num_samples:
                        np.full((2, num_samples), 0.1, np.float32))
    exported = TimelineExporter(project, sequence, Mock())._render_audio_master(
        0, 2, TimelineExportConfig(output_path=str(tmp_path / "movie.mov")), encoder=Mock())
    np.testing.assert_allclose(exported, 0.2, atol=1e-6)
