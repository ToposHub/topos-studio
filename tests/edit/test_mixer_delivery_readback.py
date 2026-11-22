"""Encoded-file evidence and production Stem worker transaction regressions."""
from __future__ import annotations

import hashlib
import json
import wave
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from src.features.edit.core.delivery_profile import DeliveryProfile, StemRule
from src.features.edit.core.delivery_readback import (
    delivery_profile_rejection, measure_delivery_wav,
)
from src.features.edit.core.stem_export import (
    StemSpec, StemType, export_all_stems, write_stem_wav,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence


def _technical() -> DeliveryProfile:
    return DeliveryProfile(profile_id="technical", name="Technical", file_format="wav",
                           max_true_peak_dbtp=None, max_sample_peak_dbfs=None,
                           stems=(StemRule("FullMix", allow_silence=True),))


def _sequence() -> TimelineSequence:
    clip = ClipInstance("clip", "asset", "t", 0, 24, 0, 24)
    return TimelineSequence(audio_tracks=(EditTrack("t", "对白 / Take", "audio", 0,
                                                    clips=(clip,)),))


@pytest.mark.parametrize("depth", [16, 24])
def test_real_encoded_wav_readback_binds_pcm_specs_and_checksum(
        depth: int, tmp_path: Path) -> None:
    path = tmp_path / "source.wav"
    samples = (0.1 * np.sin(2.0 * np.pi * 997.0 * np.arange(48000) / 48000))[None].repeat(2, 0)
    assert write_stem_wav(str(path), samples, bit_depth=depth)
    measured = measure_delivery_wav(path)
    assert measured.status == "complete"
    assert measured.sample_count == 48000
    assert measured.channel_order == ("L", "R")
    assert measured.file_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert measured.measurements["peak_dbfs"] == pytest.approx(-20.0, abs=0.01)
    assert measured.measurements["true_peak_dbtp"] >= measured.measurements["peak_dbfs"]
    assert "integrated_lufs" in measured.measurements
    assert delivery_profile_rejection(measured, replace(_technical(), bit_depth=depth)) is None
    strict = replace(_technical(), bit_depth=depth, max_true_peak_dbtp=-30.0)
    assert "true peak" in delivery_profile_rejection(measured, strict)


def test_silence_empty_truncated_and_cancelled_are_distinct(tmp_path: Path) -> None:
    silent = tmp_path / "silent.wav"
    assert write_stem_wav(str(silent), np.zeros((2, 48000), dtype=np.float32))
    result = measure_delivery_wav(silent)
    assert result.status == "silence"
    assert delivery_profile_rejection(result, _technical()) is None
    assert delivery_profile_rejection(result, replace(_technical(), stems=())) is not None
    truncated = tmp_path / "truncated.wav"
    truncated.write_bytes(silent.read_bytes()[:-23])
    assert measure_delivery_wav(truncated).status == "failed"
    empty = tmp_path / "empty.wav"
    with wave.open(str(empty), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(48000)
    assert measure_delivery_wav(empty).status == "failed"
    assert measure_delivery_wav(silent, cancel_check=lambda: True).status == "cancelled"


def test_profile_gate_mode_cannot_be_faked_by_requested_metadata(tmp_path: Path) -> None:
    path = tmp_path / "tone.wav"
    assert write_stem_wav(str(path), np.ones((2, 48000), dtype=np.float32) * 0.1)
    result = measure_delivery_wav(path)
    assert "gate mode" in delivery_profile_rejection(
        result, replace(_technical(), measurement_mode="dialogue_gated"))


def test_production_export_reports_encoded_file_qc(tmp_path: Path) -> None:
    sequence = _sequence()
    result = export_all_stems(
        sequence, str(tmp_path), specs=[StemSpec(StemType.MASTER, "Master")],
        decode_clip_fn=lambda _t, _c, _f, _fps, n: np.full((2, n), 0.125, np.float32),
        qc_profile=_technical(),
    )
    assert result.all_succeeded
    report = json.loads(Path(result.delivery_manifest_path).read_text())
    assert report["qc_passed"] is True
    evidence = report["files"][0]
    assert evidence["path"] == "Master.wav"
    assert evidence["sha256"] == hashlib.sha256((tmp_path / "Master.wav").read_bytes()).hexdigest()
    assert evidence["measurement"]["sample_count"] == 48000


def test_worker_prefix_uses_actual_sanitized_deduplicated_names(qtbot, tmp_path: Path) -> None:
    from src.features.edit.ui.stem_export_dialog import StemExportWorker

    sequence = _sequence()
    specs = (StemSpec(StemType.TRACK, "对白 / Take", ("t",)),
             StemSpec(StemType.TRACK, "对白 / Take", ("t",)))
    worker = StemExportWorker(sequence, None, str(tmp_path), specs, prefix="Film_")
    with qtbot.waitSignal(worker.finished_ok, timeout=30000) as signal:
        worker.start()
    worker.wait(30000)
    result = signal.args[0]
    assert result.all_succeeded
    assert len(result.output_files) == 2
    assert len({Path(path).name for path in result.output_files}) == 2
    assert all(Path(path).is_file() for path in result.output_files)
    assert all(Path(path).name.startswith("Film_") for path in result.output_files)
    report = json.loads((tmp_path / "delivery.json").read_text())
    assert {entry["path"] for entry in report["files"]} == {
        Path(path).name for path in result.output_files}
    assert not list(tmp_path.glob(".stem-job-*"))


def test_worker_qc_failure_preserves_existing_delivery(qtbot, tmp_path: Path) -> None:
    from src.features.edit.ui.stem_export_dialog import StemExportWorker

    old_wav = tmp_path / "Master.wav"
    old_wav.write_bytes(b"old approved delivery")
    old_manifest = tmp_path / "delivery.json"
    old_manifest.write_text("old approved manifest")
    # A silent rendered clip cannot pass a profile that forbids silence.
    worker = StemExportWorker(_sequence(), None, str(tmp_path),
                              (StemSpec(StemType.MASTER, "Master"),),
                              delivery_profile=replace(_technical(), stems=()))
    with qtbot.waitSignal(worker.failed, timeout=30000):
        worker.start()
    worker.wait(30000)
    assert old_wav.read_bytes() == b"old approved delivery"
    assert old_manifest.read_text() == "old approved manifest"
    assert not list(tmp_path.glob(".stem-job-*"))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True])
def test_nonfinite_profile_cannot_bypass_limit_comparisons(value: float) -> None:
    with pytest.raises(ValueError):
        replace(_technical(), target_lufs=value)
