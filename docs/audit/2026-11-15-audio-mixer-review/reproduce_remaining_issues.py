"""Read-only, synthetic probes for the deferred audio review findings.

Run from any directory with the repository's Python environment. This script
prints actual measurements, not a pass verdict, and writes WAVs only inside a
temporary directory. It deliberately remains outside the passing pytest suite.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from types import FrameType
from unittest.mock import Mock, patch
import wave

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.features.edit.core.commands.audio_commands import (
    ApplyChannelStripPresetCommand, ApplyMixSnapshotCommand,
)
from src.features.edit.core.mixer_preset import ChannelStripPreset, MixSnapshot
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.stem_export import StemSpec, StemType, render_stem_pcm
from src.features.edit.core.timeline_export import TimelineExportConfig, TimelineExporter
from src.features.edit.core.timeline_v2 import (
    ClipInstance, EditTrack, EffectChainParams, Send, StripEQParams,
    TimelineSequence, TrackEffectInstance, build_track_fx_sampler,
)
from src.shared.keyframe import Keyframe, KeyframeTrack
from src.shared.media.metadata import Rational

SR = 48000


def delay_chain(*, wet: float = 100.0) -> EffectChainParams:
    return EffectChainParams(effects=(TrackEffectInstance(
        effect_type="delay", instance_id="D", params={
            "enabled": True, "left_delay": 200.0, "right_delay": 200.0,
            "left_feedback": 0.0, "right_feedback": 0.0,
            "dry_wet": wet, "level_db": 0.0, "low_cut": 20.0,
            "high_cut": 20000.0, "high_ratio": 0.0, "stereo_blend": 0.0,
        },
    ),))


def write_pcm(path: Path, signal: np.ndarray) -> None:
    with wave.open(str(path), "wb") as file:
        file.setnchannels(signal.shape[0])
        file.setsampwidth(2)
        file.setframerate(SR)
        file.writeframes(np.rint(signal.T * 32767.0).astype(np.int16).tobytes())


class ConstantDecoder:
    _channels = 2

    def decode_samples_for_frame(self, frame: int, fps: float,
                                num_samples: int) -> np.ndarray:
        return np.full((2, num_samples), 0.2, np.float32)

    def close(self) -> None:
        pass


def probe_export_cursor(root: Path) -> dict:
    signal = np.zeros((2, SR), np.float32)
    signal[:, 0] = 0.5
    path = root / "impulse.wav"
    write_pcm(path, signal)
    project = EditProject("p", "Probe", assets={
        "a": MediaAsset("a", str(path), "audio", "Impulse", {}),
    })
    # The later clip reads a silent section. It is not a second impulse.
    clips = (ClipInstance("c1", "a", "T", 0, 2, 0, 2),
             ClipInstance("c2", "a", "T", 20, 2, 10, 2))
    sequence = TimelineSequence(fps=Rational(25, 1), audio_tracks=(
        EditTrack("T", "Track", "audio", 0, clips=clips, effect_chain=delay_chain()),
    ))
    calls = []
    original = AudioGraphRenderer.render_track_block

    def observe(renderer: AudioGraphRenderer, tid: str, samples: np.ndarray,
                current_frame: int, *args: object, **kwargs: object) -> np.ndarray:
        calls.append(int(current_frame))
        return original(renderer, tid, samples, current_frame, *args, **kwargs)

    with patch.object(AudioGraphRenderer, "render_track_block", observe):
        output = TimelineExporter(project, sequence, Mock())._render_audio_master(
            0, 30, TimelineExportConfig(output_path=str(root / "out.mov")),
            encoder=Mock())
    indices = np.flatnonzero(np.abs(output[0]) > 1e-4)
    return {"render_frames": calls, "expected_first_echo_ms": 200.0,
            "actual_first_echo_ms": float(indices[0] * 1000.0 / SR) if indices.size else None}


def probe_bus_automation() -> dict:
    curve = KeyframeTrack(param_name="fx/D/level_db", min_val=-24.0, max_val=6.0)
    curve.add_keyframe(Keyframe(frame=0, value=-12.0))
    bus = EditTrack("B", "Bus", "audio", 1, is_bus=True,
                    effect_chain=delay_chain(wet=0.0), automation_mode="read",
                    automation={"fx/D/level_db": curve})
    mixer = AudioMixer()
    mixer.add_track("T", ConstantDecoder(), AudioTrackState(track_id="T"))
    mixer.register_bus_track("B")
    mixer.set_track_routing("T", "B", [])
    mixer.set_track_routing("B", None, [], is_bus=True)
    mixer.set_track_effect_chain("B", bus.effect_chain,
                                 animated_sampler=build_track_fx_sampler(bus))
    realtime = mixer.mix_samples(0, 50.0, 960)[0]
    renderer = AudioGraphRenderer()
    renderer.configure_from_sequence(TimelineSequence(audio_tracks=(bus,)))
    offline = np.zeros((2, 960), np.float32)
    renderer.render_bus_stage({"B": np.full((2, 960), 0.2, np.float32)},
                              offline, has_solo=False)
    return {"expected_peak": 0.2 * 10.0 ** (-12.0 / 20.0),
            "realtime_peak": float(np.max(np.abs(realtime))),
            "offline_peak": float(np.max(np.abs(offline)))}


def probe_stem_pre_send() -> dict:
    source = EditTrack("T", "Source", "audio", 0, volume=0.0,
        clips=(ClipInstance("c", "a", "T", 0, 2, 0, 2),),
        sends=(Send(target_bus_id="B", level=1.0, tap="pre_fader"),))
    bus = EditTrack("B", "Bus", "audio", 1, is_bus=True)
    sequence = TimelineSequence(fps=Rational(25, 1), audio_tracks=(source, bus))

    def decode(track: EditTrack, clip: ClipInstance, frame: int,
               fps: float, count: int) -> np.ndarray:
        return np.full((2, count), 0.2, np.float32)

    output = render_stem_pcm(sequence, StemSpec(StemType.MASTER, "Master", ()),
                             decode_clip_fn=decode)
    return {"expected_peak": 0.2, "actual_peak": float(np.max(np.abs(output)))}


def probe_sidechain_cache(root: Path) -> dict:
    assets = {}
    tracks = []
    for name in ("programme", "key"):
        path = root / f"{name}.wav"
        signal = np.stack([0.1 * np.sin(2.0 * np.pi * 440.0 * np.arange(SR) / SR)] * 2)
        write_pcm(path, signal)
        assets[name] = MediaAsset(name, str(path), "audio", name, {})
        tracks.append(EditTrack(name, name, "audio", len(tracks), clips=(
            ClipInstance(f"{name}-clip", name, name, 0, 25, 0, 25),)))
    chain = EffectChainParams(effects=(TrackEffectInstance(
        effect_type="compressor", params={"enabled": True, "sc_on": True,
                                          "sc_source": "key"}, instance_id="C"),))
    sequence = TimelineSequence(fps=Rational(25, 1), audio_tracks=tuple(tracks),
                                master_effect_chain=chain)
    result = {}

    def capture(frame: FrameType, event: str, arg: object) -> None:
        if event == "return" and frame.f_code.co_name == "_render_audio_master":
            cache = frame.f_locals.get("sc_frame_outputs", {})
            concatenated = frame.f_locals.get("_bus_sc_taps", {})
            result.update({
                "duration_seconds": 1.0,
                "retained_frame_cache_bytes": sum(
                    buf.nbytes for frames in cache.values() for buf in frames.values()),
                "concatenated_tap_bytes": sum(buf.nbytes for buf in concatenated.values()),
                "method": "retained NumPy array bytes at renderer return, not peak RSS",
            })

    previous = sys.getprofile()
    sys.setprofile(capture)
    try:
        TimelineExporter(EditProject("sc", "SC", assets=assets), sequence, Mock())._render_audio_master(
            0, 25, TimelineExportConfig(output_path=str(root / "sc.mov")), encoder=Mock())
    finally:
        sys.setprofile(previous)
    return result


def probe_state() -> dict:
    clean = ChannelStripPreset.from_dict(ChannelStripPreset.from_track(
        EditTrack("T", "Clean", "audio", 0)).to_dict())
    rich = EditTrack("T", "Rich", "audio", 0, effect_chain=delay_chain(),
                     panner={"enabled": True, "spread": 80.0}, output_bus_id="B",
                     sends=(Send(target_bus_id="B", level=0.5),))
    changed, _ = ApplyChannelStripPresetCommand.create_from_preset("T", clean).apply(
        TimelineSequence(audio_tracks=(rich,)))
    actual = changed.audio_tracks[0]
    snapshot = MixSnapshot.from_timeline(TimelineSequence())
    changed_master = TimelineSequence(master_strip_eq=StripEQParams(master_gain=9.0))
    restored, _ = ApplyMixSnapshotCommand(
        command_id="probe", label="Probe", snapshot_name="A",
        track_states=snapshot.track_states, master_volume=snapshot.master_volume,
        master_muted=snapshot.master_muted).apply(changed_master)
    future = clean.to_dict()
    future["algorithm_version"] = 777
    saved = ChannelStripPreset.from_dict(future).to_dict()
    unknown = {"effects": [{"effect_type": "future_fx", "instance_id": "future",
                              "params": {"future_parameter": 123}}]}
    track_error = None
    data = EditTrack("U", "Unknown", "audio", 0).to_dict()
    data["effect_chain"] = unknown
    try:
        EditTrack.from_dict(data)
    except ValueError as error:
        track_error = str(error)
    master_data = TimelineSequence().to_dict()
    master_data["master_effect_chain"] = unknown
    master = TimelineSequence.from_dict(master_data)
    return {
        "empty_preset_keeps_fx": actual.effect_chain is not None,
        "empty_preset_keeps_panner": bool(actual.panner),
        "empty_preset_output_bus": actual.output_bus_id,
        "empty_preset_send_count": len(actual.sends),
        "snapshot_master_eq_gain_after_restore": restored.master_strip_eq.master_gain,
        "future_algorithm_version_input": 777,
        "future_algorithm_version_after_save": saved["algorithm_version"],
        "unknown_track_effect_error": track_error,
        "unknown_master_chain_discarded": master.master_effect_chain is None,
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="topos-audio-audit-") as folder:
        result = {"export_cursor": probe_export_cursor(Path(folder)),
                  "bus_automation": probe_bus_automation(),
                  "stem_pre_send": probe_stem_pre_send(), "state": probe_state(),
                  "sidechain_cache": probe_sidechain_cache(Path(folder))}
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
