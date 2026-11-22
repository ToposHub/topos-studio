"""Read-only probes for the professional mixer audit; never mutates a project.

Run from the repository root:
    QT_QPA_PLATFORM=offscreen .venv/bin/python \
        docs/validation/2026-11-22-mixer-professional-audit.py

Writes the adjacent JSON evidence file. These are diagnostic probes, not passing
acceptance tests: ``contract_satisfied=False`` records an unresolved backend gap.
The PDC probe injects deterministic delay-only effects at the existing DSP seam;
it requires neither media files nor physical audio devices.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_graph_snapshot import build_audio_graph_snapshot
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.features.edit.core.delivery_preset import evaluate_delivery, preset_by_id
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Send,
    TimelineSequence,
    VcaGroup,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


class DelayOnlyEffect:
    """A deterministic stateful effect declaring its actual sample latency."""

    def __init__(self, latency_samples: int) -> None:
        self._latency = latency_samples
        self._history = np.zeros((2, latency_samples), dtype=np.float32)

    def latency_samples(self) -> int:
        return self._latency

    def tail_estimate_samples(self) -> int:
        return self._latency

    def sidechain_sources(self) -> set[str]:
        return set()

    def process(
        self, samples: np.ndarray, sidechain_by_id: object = None,
    ) -> np.ndarray:
        combined = np.concatenate((self._history, samples), axis=1)
        count = samples.shape[-1]
        self._history = combined[:, count:]
        return combined[:, :count]


def probe_pdc() -> dict[str, Any]:
    """Compare a direct track against the same pulse through two delayed buses."""
    sequence = TimelineSequence(audio_tracks=(
        EditTrack("dry", "Dry", "audio", 0),
        EditTrack("wet", "Wet", "audio", 1, output_bus_id="bus1"),
        EditTrack("bus1", "Bus 1", "audio", 2, is_bus=True,
                  output_bus_id="bus2"),
        EditTrack("bus2", "Bus 2", "audio", 3, is_bus=True),
    ))
    renderer = AudioGraphRenderer(48000.0)
    renderer.configure_from_sequence(sequence)
    bus1 = renderer.get_node("bus1")
    bus2 = renderer.get_node("bus2")
    assert bus1 is not None and bus2 is not None
    bus1.effect_chain = DelayOnlyEffect(100)
    bus2.effect_chain = DelayOnlyEffect(200)
    renderer._recompute_pdc()
    pulse = np.zeros((2, 2000), dtype=np.float32)
    pulse[:, 100] = 1.0
    output = np.zeros_like(pulse)
    bus_inputs: dict[str, np.ndarray] = {}
    for track_id in ("dry", "wet"):
        processed = renderer.render_track_block(track_id, pulse, 0)
        assert processed is not None
        renderer.route_track_output(track_id, processed, output, bus_inputs)
    renderer.render_bus_stage(bus_inputs, output, False)
    hits = np.flatnonzero(np.abs(output[0]) > 1e-7).tolist()
    return {
        "entry": "AudioGraphRenderer._recompute_pdc/render_bus_stage",
        "fixture": "sample 100; dry->Master; wet->bus1(100)->bus2(200)->Master",
        "observed_pulse_positions": hits,
        "observed_declared_total_pdc_samples": renderer.pdc_total_samples(),
        "observed_node_padding_samples": dict(renderer._pdc_delay),
        "expected": "one aligned pulse at sample 400, amplitude 2.0",
        "contract_satisfied": hits == [400] and bool(
            np.isclose(output[0, 400], 2.0)),
    }


def probe_trim() -> dict[str, Any]:
    """Compare model trim evaluation with the production export renderer."""
    track = EditTrack("track", "Track", "audio", 0).with_automation_trim(
        "volume", 0.5)
    renderer = AudioGraphRenderer(48000.0)
    renderer.configure_from_sequence(TimelineSequence(audio_tracks=(track,)))
    source_amplitude = 0.5
    output = renderer.render_track_block(
        "track", np.full((2, 16), source_amplitude, dtype=np.float32), 0)
    assert output is not None
    model_gain = track.get_automation_value("volume", 0, 1.0)
    expected = source_amplitude * model_gain
    actual = float(output[0, 0])
    return {
        "entry": "AudioGraphRenderer._TrackNode/render_track_block",
        "input_amplitude": source_amplitude,
        "model_trimmed_gain": model_gain,
        "expected_output_amplitude": expected,
        "observed_output_amplitude": actual,
        "contract_satisfied": bool(np.isclose(actual, expected)),
    }


def probe_qc() -> dict[str, Any]:
    """A valid LUFS value alone cannot prove peak compliance."""
    preset = preset_by_id("broadcast-ebu-r128-v1")
    assert preset is not None
    preset = preset.with_stems(("FullMix",))
    measurement = {"FullMix": {"integrated_lufs": -23.0}}
    findings, passed = evaluate_delivery(preset, measurement)
    return {
        "entry": "delivery_preset.evaluate_delivery",
        "measurement": measurement,
        "missing_measurements": ["true_peak_dbtp", "peak_dbfs"],
        "observed_findings": findings,
        "observed_passed": passed,
        "expected_passed": False,
        "contract_satisfied": not passed,
    }


def probe_signatures() -> dict[str, Any]:
    """Mutate one audible field per candidate, keeping every identity stable."""
    from src.features.edit.ui.edit_page import EditPage

    clip = ClipInstance("clip", "asset", "track", 0, 100, 0, 100)
    track = EditTrack("track", "Track", "audio", 0, clips=(clip,),
                      sends=(Send(target_bus_id="bus", level=0.5),))
    bus = EditTrack("bus", "Bus", "audio", 1, is_bus=True)
    sequence = TimelineSequence(audio_tracks=(track, bus))
    variants = {
        "send_tap": replace(sequence, audio_tracks=(
            replace(track, sends=(replace(track.sends[0], tap="pre_fader"),)), bus)),
        "send_muted": replace(sequence, audio_tracks=(
            replace(track, sends=(replace(track.sends[0], muted=True),)), bus)),
        "automation_trim": replace(sequence, audio_tracks=(
            track.with_automation_trim("volume", 0.5), bus)),
        "vca_gain": replace(sequence, vcas=(
            VcaGroup(vca_id="vca", member_track_ids=("track",), gain_db=-6.0),)),
        "clip_gain_control": replace(sequence, audio_tracks=(
            replace(track, clips=(replace(clip, audio_gain_db=-6.0),)), bus)),
    }
    project = SimpleNamespace(assets={
        "asset": SimpleNamespace(path="audit-synthetic.wav", fps=24.0),
    })
    # Only replace asynchronous cache readiness: this fixture has no pre-render
    # steps. Invoke the actual production signature function unchanged.
    host = SimpleNamespace(_pre_render_signature_state=lambda *_args: None)

    def signature(candidate: TimelineSequence) -> object:
        return EditPage._build_audio_timeline_signature(
            host, candidate.audio_tracks, project, 24.0, timeline=candidate)

    initial_live = signature(sequence)
    initial_snapshot = build_audio_graph_snapshot(sequence).stable_hash
    changed_live = {
        key: signature(candidate) != initial_live
        for key, candidate in variants.items()
    }
    changed_snapshot = {
        key: build_audio_graph_snapshot(candidate).stable_hash != initial_snapshot
        for key, candidate in variants.items()
    }
    return {
        "entries": ["EditPage._build_audio_timeline_signature",
                    "AudioGraphSnapshot.stable_hash"],
        "live_signature_changed": changed_live,
        "snapshot_hash_changed": changed_snapshot,
        "expected": "every independently audible field changes both signatures",
        "contract_satisfied": all(changed_live.values()) and all(
            changed_snapshot.values()),
        "scope_note": "snapshot currently acts mainly as a validation gate; "
                      "do not replace the live signature with its hash yet",
    }


def probe_vca_automation() -> dict[str, Any]:
    """A volume automation ramp must preserve the independent VCA factor."""

    class ConstantDecoder:
        _timeline_start = 0
        _timeline_duration = 48
        _channels = 2

        def decode_samples_for_frame(
            self, frame: int, fps: float, num_samples: int,
        ) -> np.ndarray:
            return np.full((2, num_samples), 0.5, dtype=np.float32)

        def close(self) -> None:
            pass

    mixer = AudioMixer()
    mixer.add_track("track", ConstantDecoder(), AudioTrackState(
        track_id="track", volume=1.0))
    try:
        mixer.set_vca_overrides({"track": 0.5})
        static, _levels = mixer.mix_samples(0, 24.0, 480, 2)
        curve = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        curve.add_keyframe(Keyframe(frame=0.0, value=1.0))
        curve.add_keyframe(Keyframe(frame=24.0, value=1.0))
        mixer.set_track_automation("track", {"volume": curve})
        mixer.set_track_automation_mode("track", "read")
        automated, _levels = mixer.mix_samples(0, 24.0, 480, 2)
        static_peak = float(np.max(np.abs(static)))
        automated_peak = float(np.max(np.abs(automated)))
        return {
            "entry": "AudioMixer.mix_samples volume_ramp versus VCA factor",
            "input_amplitude": 0.5,
            "vca_linear_factor": 0.5,
            "volume_automation_value": 1.0,
            "observed_without_volume_automation": static_peak,
            "observed_with_read_volume_automation": automated_peak,
            "expected_output_amplitude_in_both_cases": 0.25,
            "contract_satisfied": bool(
                np.isclose(static_peak, 0.25) and np.isclose(automated_peak, 0.25)),
        }
    finally:
        mixer.remove_track("track")


def main() -> None:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT,
        check=True, capture_output=True, text=True).stdout.strip()
    report = {
        "audit_date": "2026-11-22",
        "scope": "read-only synthetic probes; no media/device/project mutations",
        "source_revision": revision,
        "source_state": "working tree including mixer plan implementation and completion review",
        "python": platform.python_version(),
        "numpy": np.__version__,
        "sample_rate": 48000,
        "probes": {
            "dag_pdc": probe_pdc(),
            "automation_trim_export": probe_trim(),
            "delivery_qc_missing_measurements": probe_qc(),
            "audible_field_signatures": probe_signatures(),
            "vca_with_read_volume_automation": probe_vca_automation(),
        },
        "interpretation": "Unmet contracts are backend roadmap tasks. "
                          "This probe deliberately does not repair production code.",
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    Path(__file__).with_suffix(".json").write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    main()
