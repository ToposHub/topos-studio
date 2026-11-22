"""Independent failure/cancellation oracles for final delivery publication."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from src.features.edit.core.delivery_profile import DeliveryPublicationCancelled, publish_delivery_atomic
from src.features.edit.core.stem_export import StemSpec, StemType, dedupe_stem_filenames
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence


def _sequence() -> TimelineSequence:
    clip = ClipInstance("c", "a", "t", 0, 24, 0, 24)
    return TimelineSequence(audio_tracks=(EditTrack("t", "T", "audio", 0, clips=(clip,)),))


def _sources(tmp_path: Path) -> tuple[Path, Path]:
    source, out = tmp_path / "source", tmp_path / "out"
    source.mkdir()
    out.mkdir()
    for name in ("a.wav", "b.wav"):
        (source / name).write_bytes(b"new-" + name.encode())
        (out / name).write_bytes(b"old-" + name.encode())
    return source, out


def test_publication_checks_the_staged_bytes_against_measured_checksum(tmp_path: Path) -> None:
    source, out = _sources(tmp_path)
    with pytest.raises(ValueError, match="checksum"):
        publish_delivery_atomic({"a": source / "a.wav"}, out,
                                expected_checksums={"a": hashlib.sha256(b"approved").hexdigest()})
    assert (out / "a.wav").read_bytes() == b"old-a.wav"


def test_cancel_during_publication_rolls_back_all_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.core import delivery_profile as module
    source, out = _sources(tmp_path)
    cancelled = False
    original = module.os.replace

    def publish_first_then_cancel(src: Path, dst: Path) -> None:
        nonlocal cancelled
        original(src, dst)
        if Path(src).name.startswith("file-"):
            cancelled = True

    monkeypatch.setattr(module.os, "replace", publish_first_then_cancel)
    with pytest.raises(DeliveryPublicationCancelled):
        publish_delivery_atomic({"a": source / "a.wav", "b": source / "b.wav"}, out,
                                cancel_check=lambda: cancelled)
    assert (out / "a.wav").read_bytes() == b"old-a.wav"
    assert (out / "b.wav").read_bytes() == b"old-b.wav"
    assert not list(out.glob(".delivery-tmp-*"))


def test_failed_rollback_retains_the_only_original_for_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.core import delivery_profile as module
    source, out = _sources(tmp_path)
    original = module.os.replace

    def fail_late_and_rollback(src: Path, dst: Path) -> None:
        if Path(src).name.startswith("file-") and Path(dst).name == "b.wav":
            raise OSError("publish failure")
        if Path(src).name == "backup-0":
            raise OSError("rollback failure")
        original(src, dst)

    monkeypatch.setattr(module.os, "replace", fail_late_and_rollback)
    with pytest.raises(OSError, match="recovery files retained"):
        publish_delivery_atomic({"a": source / "a.wav", "b": source / "b.wav"}, out)
    recovery = list(out.glob(".delivery-tmp-*/backup-0"))
    assert len(recovery) == 1
    assert recovery[0].read_bytes() == b"old-a.wav"


def test_case_insensitive_delivery_names_are_unique() -> None:
    specs = dedupe_stem_filenames([StemSpec(StemType.TRACK, "Dialogue"),
                                  StemSpec(StemType.TRACK, "dialogue")])
    assert len({spec.filename.casefold() for spec in specs}) == 2


@pytest.mark.parametrize("name", ["../outside.wav", "sub/file.wav", "sub\\file.wav", ".."])
def test_override_cannot_escape_staging(name: str) -> None:
    with pytest.raises(ValueError, match="basename"):
        _ = StemSpec(StemType.MASTER, "Master", filename_override=name).filename


def test_worker_cancel_during_decoder_cleanup_prevents_publication(qapp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.features.edit.core import stem_export
    from src.features.edit.ui.stem_export_dialog import StemExportWorker
    old = tmp_path / "Master.wav"
    old.write_bytes(b"approved")
    worker = StemExportWorker(_sequence(), None, str(tmp_path), (StemSpec(StemType.MASTER, "Master"),))
    monkeypatch.setattr(stem_export, "create_clip_decode_fn", lambda *args: (None, worker.cancel))
    done: list = []
    errors: list = []
    worker.finished_ok.connect(done.append)
    worker.failed.connect(errors.append)
    worker.run()
    assert not errors
    assert len(done) == 1 and done[0].cancelled
    assert done[0].output_files == []
    assert old.read_bytes() == b"approved"
    assert not (tmp_path / "delivery.json").exists()


def test_worker_rejects_file_changed_between_qc_and_publish(qapp, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from src.features.edit.core import stem_export
    from src.features.edit.ui.stem_export_dialog import StemExportWorker
    old = tmp_path / "Master.wav"
    old.write_bytes(b"approved")

    def mutate_after_qc() -> None:
        rendered = next(tmp_path.glob(".stem-job-*/Master.wav"))
        with rendered.open("ab") as handle:
            handle.write(b"changed after QC")

    monkeypatch.setattr(stem_export, "create_clip_decode_fn", lambda *args: (None, mutate_after_qc))
    worker = StemExportWorker(_sequence(), None, str(tmp_path), (StemSpec(StemType.MASTER, "Master"),))
    errors: list = []
    done: list = []
    worker.failed.connect(errors.append)
    worker.finished_ok.connect(done.append)
    worker.run()
    assert not done
    assert len(errors) == 1 and "checksum" in errors[0]
    assert old.read_bytes() == b"approved"
    assert not list(tmp_path.glob(".stem-job-*"))


@pytest.mark.parametrize("count", [None, "100", True])
def test_invalid_silence_counts_are_rejected_without_crashing(count: object) -> None:
    from src.features.edit.core.delivery_profile import DeliveryProfile, MeasurementResult, StemRule, measurement_rejection
    profile = DeliveryProfile("technical", "Technical", stems=(StemRule("FX", allow_silence=True),))
    evidence = MeasurementResult("FX", "silence", file_sha256="sha", sample_rate=48000,
                                 channel_order=("L", "R"), sample_count=count,
                                 algorithm_version="known")
    assert measurement_rejection(evidence, profile) is not None


def test_readback_rejects_inplace_change_even_when_mtime_is_restored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import numpy as np
    from src.features.edit.core import delivery_readback
    from src.features.edit.core.stem_export import write_stem_wav
    path = tmp_path / "measured.wav"
    assert write_stem_wav(str(path), np.ones((2, 48000), np.float32) * 0.1)
    before = path.stat()
    original = delivery_readback._pcm_float
    changed = False

    def mutate_once(raw: bytes, width: int, channels: int) -> np.ndarray:
        nonlocal changed
        if not changed:
            changed = True
            with path.open("r+b") as handle:
                handle.seek(-2, 2)
                handle.write(b"\x00\x00")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return original(raw, width, channels)

    monkeypatch.setattr(delivery_readback, "_pcm_float", mutate_once)
    measured = delivery_readback.measure_delivery_wav(path)
    assert measured.status == "failed"
    assert "changed" in measured.error


@pytest.mark.parametrize("rule_changes", [{"pre_master": True}, {"tail_seconds": 1.0}])
def test_profile_tap_and_tail_requirements_cannot_be_silently_ignored(tmp_path: Path, rule_changes: dict) -> None:
    from src.features.edit.core.delivery_profile import DeliveryProfile, StemRule
    from src.features.edit.core.stem_export import export_all_stems
    profile = DeliveryProfile("custom", "Custom", file_format="wav",
                              stems=(StemRule("FullMix", **rule_changes),))
    with pytest.raises(ValueError):
        export_all_stems(_sequence(), str(tmp_path), specs=[StemSpec(StemType.MASTER, "Master")],
                         qc_profile=profile)
    assert not list(tmp_path.glob("*.wav"))


def test_interrupt_during_publication_also_restores_old_delivery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.core import delivery_profile as module
    source, out = _sources(tmp_path)
    original = module.os.replace

    def interrupt_second(src: Path, dst: Path) -> None:
        if Path(src).name.startswith("file-") and Path(dst).name == "b.wav":
            raise KeyboardInterrupt()
        original(src, dst)

    monkeypatch.setattr(module.os, "replace", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        publish_delivery_atomic({"a": source / "a.wav", "b": source / "b.wav"}, out)
    assert (out / "a.wav").read_bytes() == b"old-a.wav"
    assert (out / "b.wav").read_bytes() == b"old-b.wav"


@pytest.mark.parametrize("field,value", [("target_lufs", True), ("tolerance_lu", "1.0"), ("sample_rate", 48000.5), ("bit_depth", True)])
def test_profile_reload_cannot_launder_malformed_limits(field: str, value: object) -> None:
    from src.features.edit.core.delivery_profile import DeliveryProfile
    payload = DeliveryProfile("custom", "Custom").to_dict()
    payload[field] = value
    with pytest.raises(ValueError):
        DeliveryProfile.from_dict(payload)


@pytest.mark.parametrize("field,value", [("allow_silence", "false"), ("pre_master", "false"), ("target_lufs", True)])
def test_stem_rule_reload_cannot_launder_permissions(field: str, value: object) -> None:
    from src.features.edit.core.delivery_profile import DeliveryProfile
    payload = DeliveryProfile("custom", "Custom").to_dict()
    payload["stems"] = [{"stem_id": "FullMix", field: value}]
    with pytest.raises(ValueError):
        DeliveryProfile.from_dict(payload)
