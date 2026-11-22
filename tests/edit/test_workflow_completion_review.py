"""Independent regressions for production-safe dialogue and comp completion."""
from dataclasses import replace
from fractions import Fraction

import numpy as np
import pytest

from src.features.edit.core.audio_comp import (
    AudioComp, CompRegion, FlattenAudioCompCommand, flatten_comp_clips,
    replace_region,
)
from src.features.edit.core.commands import CommandStack
from src.features.edit.core.dialogue_proposal import (
    ApplyDialogueProposalCommand, DialogueProposal, ProposalOperation,
    apply_alignment_to_clip, validate_proposal,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.clip_params import SpeedParams
from src.shared.media.metadata import Rational


def _clip(clip_id: str = "take") -> ClipInstance:
    return ClipInstance(clip_id=clip_id, asset_id="source", track_id="dx",
                        timeline_duration=100, source_duration=100,
                        audio_gain_db=-3.0, fade_curve_in="linear")


def _proposal(*operations: ProposalOperation) -> DialogueProposal:
    return DialogueProposal("p", 0, ("fingerprint",), tuple(operations))


def test_dialogue_command_scopes_operations_to_the_requested_track() -> None:
    proposal = _proposal(
        ProposalOperation("align", "dx", "take", 12, 0.9),
        ProposalOperation("align", "other", "elsewhere", 48, 0.9),
    )
    command, _ = ApplyDialogueProposalCommand.create(proposal, "dx")
    assert command.align_offsets == (("take", 12),)


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_confidence_never_bypasses_auto_apply(confidence: float) -> None:
    proposal = _proposal(ProposalOperation("align", "dx", "take", 12, confidence))
    assert validate_proposal(proposal, current_revision=0,
                             current_fingerprints=("fingerprint",))
    with pytest.raises(ValueError, match="confidence"):
        ApplyDialogueProposalCommand.create(proposal, "dx", include_low_confidence=True)


@pytest.mark.parametrize("direction", [-1, 1])
def test_opposite_alignment_cancels_without_a_silent_prefix(direction: int) -> None:
    original = _clip()
    shifted = apply_alignment_to_clip(original, direction * 37)
    restored = apply_alignment_to_clip(shifted, direction * -37)
    assert restored.audio_sample_offset == 0
    assert restored.source_sample_offset == 0


def test_dialogue_noop_preserves_command_history_and_locked_track_rejected() -> None:
    clip = _clip()
    sequence = TimelineSequence(audio_tracks=(EditTrack("dx", "DX", "audio", 0, clips=(clip,)),))
    command, _ = ApplyDialogueProposalCommand.create(
        _proposal(ProposalOperation("align", "dx", "take", 0, 0.9)), "dx")
    assert command.apply(sequence)[0] is sequence
    locked = sequence.with_track_updated(replace(sequence.audio_tracks[0], is_locked=True))
    with pytest.raises(ValueError, match="locked"):
        command.apply(locked)


@pytest.mark.parametrize("kwargs", [dict(source_start=-1), dict(start=-1),
                                    dict(fade_in=-1), dict(start=True)])
def test_invalid_comp_sample_coordinates_rejected(kwargs: dict) -> None:
    args = dict(start=0, end=2000, take_id="take", source_start=0)
    args.update(kwargs)
    with pytest.raises(ValueError):
        CompRegion(**args)


def test_comp_replace_does_not_move_an_outer_fade_to_a_new_cut() -> None:
    original = CompRegion(0, 6000, "A", 0, fade_in=100, fade_out=200)
    left, middle, right = replace_region((original,), CompRegion(2000, 4000, "B", 0))
    assert (left.fade_in, left.fade_out) == (100, 0)
    assert (right.fade_in, right.fade_out) == (0, 200)


@pytest.mark.parametrize("fps", [Fraction(24), Fraction(30000, 1001)])
def test_flatten_is_one_command_and_preserves_the_exact_sample_positions(fps: Fraction) -> None:
    frame_end = int(Fraction(3 * 48000) / fps)
    comp = AudioComp("comp", "dx", (CompRegion(37, frame_end, "take", 239, fade_in=16),))
    sequence = TimelineSequence(fps=Rational(fps.numerator, fps.denominator),
        audio_tracks=(EditTrack("dx", "DX", "audio", 0, clips=(_clip("old"),), volume=0.7),))
    command = FlattenAudioCompCommand.create(comp, {"take": _clip()}, {"take": 480000},
                                            expected_revision=sequence.revision)
    stack = CommandStack()
    flattened = stack.execute(command, sequence)
    clip = flattened.audio_tracks[0].clips[0]
    assert clip.clip_id == "comp-comp-0"
    assert clip.audio_gain_db == -3.0
    assert clip.fade_curve_in == "linear"
    assert clip.fade_in_samples == 16
    assert clip.audio_sample_offset == 37
    assert clip.source_start == 0 and clip.source_sample_offset == 239
    assert int(Fraction(clip.timeline_end * 48000) / fps) == frame_end
    assert flattened.audio_tracks[0].volume == 0.7
    restored = stack.undo(flattened)
    assert restored.audio_tracks == sequence.audio_tracks
    redone = stack.redo(restored)
    assert redone.audio_tracks == flattened.audio_tracks
    applied = command.apply(sequence)[1]
    reloaded = FlattenAudioCompCommand.from_dict(applied.to_dict())
    assert reloaded.undo(flattened).audio_tracks == sequence.audio_tracks


def test_flatten_never_quantizes_an_unrepresentable_sample_tail() -> None:
    comp = AudioComp("comp", "dx", (CompRegion(0, 2001, "take", 0),))
    with pytest.raises(ValueError, match="not representable"):
        flatten_comp_clips(comp, {"take": _clip()}, {"take": 48000}, Fraction(24))


def test_flatten_rejects_offline_take_and_locked_or_stale_destination() -> None:
    comp = AudioComp("comp", "dx", (CompRegion(0, 2000, "take", 0),))
    sequence = TimelineSequence(audio_tracks=(EditTrack("dx", "DX", "audio", 0, is_locked=True),))
    command = FlattenAudioCompCommand.create(comp, {"take": _clip()}, {"take": 48000})
    with pytest.raises(ValueError, match="locked"):
        command.apply(sequence)
    stale = replace(command, expected_revision=sequence.revision + 1)
    with pytest.raises(ValueError, match="revision"):
        stale.apply(sequence)
    with pytest.raises(ValueError, match="offline"):
        flatten_comp_clips(comp, {}, {}, Fraction(24))


@pytest.mark.parametrize("clip", [replace(_clip(), speed=SpeedParams(reverse=True)),
                                  replace(_clip(), speed=2.0),
                                  replace(_clip(), audio_edit={"version": 1, "steps": []})])
def test_flatten_demands_explicit_bake_for_unsupported_take_processing(clip: ClipInstance) -> None:
    comp = AudioComp("comp", "dx", (CompRegion(0, 2000, "take", 0),))
    with pytest.raises(ValueError, match="baking"):
        flatten_comp_clips(comp, {"take": clip}, {"take": 48000}, Fraction(24))

from src.features.edit.core.capture_clock import CaptureJournal, CaptureStream, ClockAnchor, apply_punch
import json


@pytest.mark.parametrize("starts", [(0, 300), (0, 200), (256, 0)])
def test_punch_refuses_to_pack_dropouts_overlaps_or_reordered_data(starts: tuple) -> None:
    block = np.ones((2, 256), np.float32)
    with pytest.raises(ValueError, match="missing|overlapping|incomplete"):
        apply_punch([(starts[0], block), (starts[1], block)], 0, 512)


def test_punch_refuses_channel_truncation() -> None:
    with pytest.raises(ValueError, match="channel"):
        apply_punch([(0, np.ones((2, 256))), (256, np.ones((3, 256)))], 0, 512)


@pytest.mark.parametrize("bad", [{"sequence_no": 1}, [],
    {"sequence_no": 1, "first_device_sample": 256, "sample_count": -1}])
def test_recovery_stops_at_malformed_complete_json_record(tmp_path, bad: object) -> None:
    path = tmp_path / "journal.jsonl"
    good = {"sequence_no": 0, "first_device_sample": 0, "sample_count": 256, "crc32": 0}
    path.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n")
    journal = CaptureJournal(path)
    assert journal.recoverable_prefix() == (1, 256)


@pytest.mark.parametrize("second", [200, 300])
def test_recovery_verifies_device_sample_continuity_not_only_sequence(tmp_path, second: int) -> None:
    path = tmp_path / "journal.jsonl"
    records = [dict(sequence_no=0, first_device_sample=0, sample_count=256, crc32=0),
               dict(sequence_no=1, first_device_sample=second, sample_count=256, crc32=0)]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    assert CaptureJournal(path).recoverable_prefix() == (1, 256)


def test_fsynced_status_is_true_only_after_os_sync(tmp_path) -> None:
    journal = CaptureJournal(tmp_path / "journal.jsonl", fsync_every=2)
    stream = CaptureStream("device")
    for i in range(2):
        segment = journal.record_segment(stream.accept(np.ones((2, 16), np.float32)))
        assert journal.commit_segment(i)
        if i == 0:
            assert not segment.fsynced
            assert not journal.commit_segment(0)  # no duplicate append before fsync
    assert all(segment.fsynced for segment in journal.segments())
    journal.close()


def test_drift_fit_refuses_a_repeated_or_backward_clock_anchor() -> None:
    clock = ClockAnchor(48000, 48000)
    clock.add_anchor(1000, 2000)
    with pytest.raises(ValueError, match="advance"):
        clock.add_anchor(1000, 2500)


def _aaf_sequence() -> TimelineSequence:
    return TimelineSequence(audio_tracks=(EditTrack("dx", "DX", "audio", 0,
        clips=(replace(_clip(), timeline_start=1, timeline_duration=2,
                       audio_sample_offset=17),)),))


def test_aaf_roundtrip_preserves_a_subframe_head_and_absolute_tail(tmp_path) -> None:
    pytest.importorskip("aaf2")
    from src.features.edit.adapters.aaf_adapter import export_aaf, import_aaf
    path = tmp_path / "precise.aaf"
    original = _aaf_sequence()
    export_aaf(original, path)
    result = import_aaf(path)
    clip = result.sequences[0].audio_tracks[0].clips[0]
    assert clip.timeline_start * 2000 + clip.audio_sample_offset == 2017
    assert clip.timeline_end * 2000 == 6000
    assert clip.timeline_end * 2000 - (clip.timeline_start * 2000 + clip.audio_sample_offset) == 3983
    assert not result.report.has_errors


def test_aaf_export_does_not_overwrite_an_unrelated_temporary_file(tmp_path) -> None:
    pytest.importorskip("aaf2")
    from src.features.edit.adapters.aaf_adapter import export_aaf
    path = tmp_path / "mix.aaf"
    unrelated = tmp_path / "mix.aaf.tmp"
    unrelated.write_bytes(b"another job owns this file")
    export_aaf(_aaf_sequence(), path)
    assert unrelated.read_bytes() == b"another job owns this file"
    assert set(tmp_path.iterdir()) == {path, unrelated}


def test_aaf_import_enforces_size_budget_before_parser(tmp_path, monkeypatch) -> None:
    pytest.importorskip("aaf2")
    from src.features.edit.adapters import aaf_adapter as aaf
    path = tmp_path / "oversized.aaf"
    path.write_bytes(b"not parsed")
    monkeypatch.setattr(aaf, "INSPECT_MAX_FILE_BYTES", 1)
    with pytest.raises(ValueError, match="size budget"):
        aaf.import_aaf(path)


def test_aaf_import_does_not_create_duplicate_timelines_for_referenced_master_mobs(tmp_path) -> None:
    aaf2 = pytest.importorskip("aaf2")
    from src.features.edit.adapters.aaf_adapter import export_aaf, import_aaf
    path = tmp_path / "master.aaf"
    export_aaf(_aaf_sequence(), path)
    with aaf2.open(str(path), "rw") as f:
        master = f.create.from_name("MasterMob")
        master.name = "Referenced source master"
        f.content.mobs.append(master)
        f.save()
    assert len(import_aaf(path).sequences) == 1


def test_journal_does_not_recover_a_readable_but_uncheckpointed_tail(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = CaptureJournal(path, fsync_every=2)
    stream = CaptureStream("device")
    for _ in range(3):
        block = stream.accept(np.ones((2, 16), np.float32))
        journal.record_segment(block)
        journal.commit_segment(block.sequence_no)
    # Simulate OS-visible buffered tail without a durable journal checkpoint.
    journal._fh.flush()
    assert journal.recoverable_prefix() == (2, 32)
    journal._fh.close()
    journal._fh = None  # process crash, no graceful close/checkpoint
    restored = CaptureJournal(path, fsync_every=1)
    assert restored.recoverable_prefix() == (2, 32)
    next_block = stream.accept(np.ones((2, 16), np.float32), first_device_sample=32)
    # Resume from the last confirmed sequence, not from the lost sequence.
    next_block = replace(next_block, sequence_no=2)
    restored.record_segment(next_block)
    restored.commit_segment(2)
    restored.close()
    assert restored.recoverable_prefix() == (3, 48)


def test_reference_fixture_is_exactly_ten_minutes_in_the_model_frame_domain() -> None:
    from tests.support.reference_project import build_reference_sequence
    sequence = build_reference_sequence()
    assert sequence.duration_frames == 24 * 600
    assert all(0 <= clip.timeline_start < clip.timeline_end <= 24 * 600
               for track in sequence.audio_tracks for clip in track.clips)
    dx = sequence.get_track_by_id("dx")
    first = dx.clips[0]
    assert first.timeline_duration == 12  # half a second, not 24000 frames
    assert first.source_start * 2000 + first.source_sample_offset == 240
    assert [key.frame for key in dx.automation["volume"].keyframes] == [0, 24 * 30, 24 * 60]
    assert sequence.get_track_by_id("fx").output_bus_id == "fx_bus"
    assert sequence.vcas[0].member_track_ids == ("dx", "adr")


def test_reference_project_actual_topos_save_reopen_preserves_sound(qapp, tmp_path) -> None:
    from tests.support.reference_project import build_reference_sequence
    from tests.support.sound_fixtures import write_wav
    from src.features.edit.core.audio_graph_snapshot import build_audio_graph_snapshot
    from src.features.edit.core.edit_project_io import EditProjectIO
    from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef

    sequence = build_reference_sequence()
    sequence = replace(sequence, timeline_id="reference-timeline", name="10 minute mixing reference")
    project = EditProject.create("Professional audio reference")
    for role in ("dx", "adr", "foley", "fx", "mx"):
        rate = 48000
        samples = np.arange(rate * 2, dtype=np.float32)
        signal = (0.1 * np.sin(2 * np.pi * (200 + len(role) * 101) * samples / rate))[None, :]
        path = write_wav(tmp_path / f"{role}.wav", signal, rate)
        project = project.with_media_asset_added(MediaAsset(
            asset_id=f"synth-{role}", path=str(path), media_type="audio", display_name=role,
            metadata={"duration_seconds": 2.0, "sample_rate": 48000, "channels": 1}))
    project = project.with_timeline_ref_added(TimelineSequenceRef(
        timeline_id=sequence.timeline_id, name=sequence.name, fps=24.0), set_active=True)
    before = build_audio_graph_snapshot(sequence).stable_hash
    path = tmp_path / "reference.topos"
    io = EditProjectIO()
    try:
        assert io.save_project(path, project, timeline_sequences={sequence.timeline_id: sequence},
                               create_backup=False)
        loaded = io.load_project(path)
        assert loaded is not None
        reopened_project, _, sequences = loaded
        reopened = sequences[sequence.timeline_id]
        assert reopened.duration_frames == 24 * 600
        assert reopened.vcas == sequence.vcas
        assert reopened.audio_tracks[0].automation_trim == sequence.audio_tracks[0].automation_trim
        assert reopened.audio_tracks[0].clips[0].source_sample_offset == 240
        assert build_audio_graph_snapshot(reopened).stable_hash == before
        assert set(reopened_project.assets) == set(project.assets)
    finally:
        io.cleanup()

import threading
from src.features.edit.core import recording_manager as recording


def test_recording_writer_drains_on_stop_and_fsync_never_runs_on_capture_thread(tmp_path, monkeypatch) -> None:
    calls = []
    original_fsync = recording.os.fsync
    monkeypatch.setattr(recording.os, "fsync", lambda fd: (calls.append(threading.get_ident()), original_fsync(fd))[1])
    writer = recording._DurablePcmSpool(str(tmp_path), 2, 48000)
    left = np.arange(1001, dtype=np.float32) / 2000
    block = np.vstack((left, -left))
    writer.append(block)
    block.fill(0)  # callback memory reuse cannot mutate the queued recording
    writer.finish()
    assert writer.confirmed_samples == 1001
    actual = np.concatenate(list(writer.iter_blocks()), axis=1)
    np.testing.assert_array_equal(actual, np.vstack((left, -left)))
    assert calls and threading.get_ident() not in calls
    writer.discard()


def test_capture_writer_overload_is_bounded_and_retains_the_accepted_prefix(tmp_path, monkeypatch) -> None:
    gate = threading.Event()
    run = recording._DurablePcmSpool._run
    monkeypatch.setattr(recording._DurablePcmSpool, "_run", lambda self: (gate.wait(5), run(self)))
    writer = recording._DurablePcmSpool(str(tmp_path), 1, 48000, memory_limit=1024)
    try:
        writer.append(np.full((1, 256), 0.1, np.float32))
        with pytest.raises(recording.DiskSpaceExhausted, match="memory budget"):
            writer.append(np.full((1, 256), 0.2, np.float32))
        assert writer.memory_bytes == 1024
    finally:
        gate.set()
        writer.finish()
    assert writer.confirmed_samples == 256
    writer.discard()


def test_cancel_discards_queued_capture_and_removes_recovery_files(tmp_path) -> None:
    writer = recording._DurablePcmSpool(str(tmp_path), 1, 48000)
    writer.append(np.full((1, 100), 0.1, np.float32))
    writer.discard()
    writer._thread.join(5)
    assert not writer._thread.is_alive()
    assert not list(tmp_path.glob("topos_capture_*"))


def test_recovery_verifies_pcm_crc_and_recovers_only_a_valid_prefix(tmp_path) -> None:
    writer = recording._DurablePcmSpool(str(tmp_path), 1, 48000)
    writer.append(np.full((1, 256), 0.1, np.float32))
    writer.append(np.full((1, 256), 0.2, np.float32))
    writer.finish()
    with open(writer.raw_path, "r+b") as fh:
        fh.seek(256 * 4)
        fh.write(b"\0" * 4)
    take = recording.recover_capture_spool(writer.manifest_path, tmp_path / "recovered.wav")
    assert take.status == "completed_partial"
    assert take.duration_seconds == pytest.approx(256 / 48000)
    assert "alignment unknown" in take.notes
    assert writer.raw_path.exists()  # recovery does not delete original capture data
    writer.discard()


def test_final_encoding_failure_preserves_durable_capture_for_recovery(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(recording.RecordingManager, "_start_capture_device", lambda self: True)
    manager = recording.RecordingManager(recording.RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=0))
    assert manager.start_recording()
    writer = manager._pcm_buffer._durable
    manager.inject_pcm(np.full((1, 256), 0.1, np.float32))
    monkeypatch.setattr(recording, "write_take_blocks", lambda *args, **kwargs: False)
    take = manager.stop_recording()
    assert take.status == "failed"
    assert writer.manifest_path.exists() and writer.raw_path.exists()
    assert "Recovery retained" in take.notes
    writer.discard()


def test_capture_disk_failure_fails_closed_without_claiming_complete_take(tmp_path, monkeypatch) -> None:
    def fail_sync(fd: int) -> None:
        raise OSError("injected disk full")
    monkeypatch.setattr(recording.os, "fsync", fail_sync)
    writer = recording._DurablePcmSpool(str(tmp_path), 1, 48000)
    # Opening the recovery manifest may already have failed on its worker.
    try:
        writer.append(np.full((1, 256), 0.1, np.float32))
    except recording.DiskSpaceExhausted:
        pass
    writer.finish()
    assert writer.error is not None
    assert writer.confirmed_samples == 0
    assert not list(writer.iter_blocks())
    writer.discard()


def test_live_recording_overload_delivers_the_durable_partial_via_existing_callback(tmp_path, monkeypatch) -> None:
    gate = threading.Event()
    run = recording._DurablePcmSpool._run
    monkeypatch.setattr(recording._DurablePcmSpool, "_run", lambda self: (gate.wait(5), run(self)))
    monkeypatch.setattr(recording.RecordingManager, "_start_capture_device", lambda self: True)
    manager = recording.RecordingManager(recording.RecordingConfig(output_dir=str(tmp_path), pre_roll_seconds=0))
    completed = []
    manager.on_take_completed = completed.append
    manager.on_error = lambda message: gate.set()
    assert manager.start_recording()
    manager._pcm_buffer._durable.limit = 1024
    manager._receive_pcm(np.full((1, 256), 0.1, np.float32))
    manager._receive_pcm(np.full((1, 256), 0.2, np.float32))
    assert manager.state == manager.STATE_IDLE
    assert len(completed) == 1
    assert completed[0].status == "completed_partial"
    assert completed[0].duration_seconds == pytest.approx(256 / 48000)
