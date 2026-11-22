"""Reference listening passes through the output clock and stays out of the mix."""
from __future__ import annotations

import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.features.edit.core.commands import SetReferenceMixCommand
from src.features.edit.core.reference_mix import ReferenceMixRef
from src.features.edit.core.reference_mix_session import (
    ReferenceCapture, ReferenceMixMonitor, measure_reference_capture, prepare_reference,
)
from src.features.edit.core.timeline_v2 import TimelineSequence


def _write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setparams((samples.shape[0], 2, rate, samples.shape[1], "NONE", ""))
        writer.writeframes(np.rint(samples.T * 32768.0).astype("<i2").tobytes())


def test_reference_pcm_uses_source_and_timeline_sample_domains(tmp_path: Path) -> None:
    from src.features.edit.core.bwf import BextMetadata, inject_bext_chunk

    path = tmp_path / "reference.wav"
    base = np.zeros((2, 44100), np.float32)
    base[1, 11025] = 0.5
    _write_wav(path, base, 44100)
    assert inject_bext_chunk(str(path), BextMetadata(time_reference_samples=3_000_000_000))
    ref = ReferenceMixRef("version", "asset", 11025, 1024, original_rate=44100, timeline_rate=48000)
    prepared = prepare_reference(str(path), ref, 48000, 2)
    assert prepared is not None
    assert prepared.bwf_time_reference == 3_000_000_000
    assert prepared.pcm.shape == (48000, 2)
    assert not prepared.pcm.flags.writeable
    block = prepared.block(1020, 12)
    np.testing.assert_array_equal(block[0], 0.0)
    np.testing.assert_array_equal(block[:, :4], 0.0)
    assert block[1, 4] == pytest.approx(0.5, abs=1e-5)
    assert prepare_reference(str(path), ref, 48000, 2, cancel_check=lambda: True) is None


def test_reference_uses_actual_output_stage_and_keeps_programme_immutable(tmp_path: Path) -> None:
    from src.features.edit.core.audio_playback import AudioPlaybackThread
    from src.features.edit.core.monitor_service import MonitorSessionState

    path = tmp_path / "reference.wav"
    _write_wav(path, np.full((2, 48000), 0.25, np.float32), 48000)
    ref = ReferenceMixRef("version", "asset", timeline_anchor_sample=4004)
    prepared = prepare_reference(str(path), ref, 48000, 2)
    assert prepared is not None
    monitor = ReferenceMixMonitor()
    monitor.set_source(prepared)
    source = np.full((2, 800), 0.125, np.float32)
    source.setflags(write=False)
    output = AudioPlaybackThread()
    output._fps = 48000 / 800.8
    output._mixer = SimpleNamespace(
        mix_samples=lambda frame, fps, samples, channels, **kwargs: (source, {}),
        programme_channels=lambda value=2: value,
    )
    output.reference_mix = monitor
    output.control_room_state = lambda: MonitorSessionState()
    monitor.select_reference(True)
    before, _ = output._render_output_block(4, 800, 2)
    transition, _ = output._render_output_block(5, 800, 2)
    steady, _ = output._render_output_block(6, 800, 2)
    np.testing.assert_array_equal(before, source)
    assert transition[0, 0] == 0.125
    np.testing.assert_array_equal(steady, 0.25)
    # Taking A again crossfades in the same device ring; the render programme
    # provided by the graph never sees the selector or the reference gain.
    monitor.select_reference(False)
    returned, _ = output._render_output_block(7, 800, 2)
    assert 0.125 < returned[0, 0] < 0.25
    assert returned[0, -1] == 0.125
    np.testing.assert_array_equal(source, 0.125)


def test_71_reference_keeps_all_speakers_after_resampling(tmp_path: Path) -> None:
    from src.features.edit.core.stem_export import write_stem_wav

    path = tmp_path / "reference-71.wav"
    base = np.zeros((8, 44100), np.float32)
    source_positions = np.arange(8) * 4410 + 4410
    base[np.arange(8), source_positions] = 0.375
    assert write_stem_wav(str(path), base, sample_rate=44100)
    reference = ReferenceMixRef("version", "asset", original_rate=44100)
    prepared = prepare_reference(str(path), reference, 48000, 8)
    assert prepared is not None
    assert prepared.pcm.shape == (48000, 8)
    np.testing.assert_array_equal(np.argmax(prepared.pcm, axis=0), np.arange(8) * 4800 + 4800)
    np.testing.assert_allclose(prepared.pcm.max(axis=0), 0.375, atol=1e-4)


def test_live_measurement_uses_equal_windows_and_rejects_seek(tmp_path: Path) -> None:
    from src.features.edit.core.audio_capabilities import is_scipy_available_for_dsp

    if not is_scipy_available_for_dsp():
        pytest.skip("programme loudness requires SciPy")
    rate = 48000
    time = np.arange(4 * rate, dtype=np.float64) / rate
    waveform = (0.08 * np.sin(2 * np.pi * 997 * time)).astype(np.float32)
    pcm = np.broadcast_to(waveform, (2, waveform.size))
    path = tmp_path / "reference.wav"
    _write_wav(path, pcm, rate)
    source = prepare_reference(str(path), ReferenceMixRef("version", "asset"), rate, 2)
    assert source is not None
    monitor = ReferenceMixMonitor()
    monitor.set_source(source)
    capture = monitor.start_measurement()
    offset = 1000
    for position in range(offset, offset + 3 * rate, 2400):
        block = pcm[:, position:position + 2400] * 2.0
        assert monitor.render_block(block, position, sample_rate=rate) is block
    captured = monitor.measurement_result(capture)
    assert isinstance(captured, ReferenceCapture)
    assert captured.start_sample == offset
    measured = measure_reference_capture(captured, lambda: False)
    assert measured is not None
    assert measured.match_gain == pytest.approx(2.0, abs=0.002)
    assert measured.sample_count == 3 * rate
    interrupted = monitor.start_measurement()
    block = pcm[:, :2400]
    monitor.render_block(block, 0, sample_rate=rate)
    monitor.render_block(block, 9600, sample_rate=rate)
    assert monitor.measurement_result(interrupted) == "transport"


def test_reference_command_and_project_round_trip(qapp, tmp_path: Path) -> None:
    from src.features.edit.core.edit_project_io import EditProjectIO
    from src.features.edit.core.project import EditProject, TimelineSequenceRef

    audio = tmp_path / "reference.wav"
    _write_wav(audio, np.zeros((2, 48000), np.float32), 48000)
    project = EditProject.create("Mix").import_asset(
        str(audio), media_type="audio", metadata={"duration_seconds": 1.0, "sample_rate": 48000})
    original = TimelineSequence(timeline_id="edit", name="Edit")
    project = project.with_timeline_ref_added(TimelineSequenceRef(timeline_id="edit", name="Edit"))
    original = replace(original, reference_mixes=({"version": 3, "future": {"x": 123}},))
    reference = ReferenceMixRef("old-mix", next(iter(project.assets)), 113, -235,
                                label="Previous print", provenance="media_pool")
    command = SetReferenceMixCommand.create(original.timeline_id, reference.version_id, reference)
    edited, applied = command.apply(original)
    restored = SetReferenceMixCommand.from_dict(applied.to_dict()).undo(edited)
    assert restored.reference_mixes == original.reference_mixes
    assert edited.with_master_volume(0.5).reference_mixes == edited.reference_mixes
    io = EditProjectIO()
    try:
        path = tmp_path / "reference.topos"
        assert io.save_project(path, project, timeline_sequences={"edit": edited}, create_backup=False)
        result = io.load_project(path)
        assert result is not None
        loaded_project, _, sequences = result
        assert reference.asset_id in loaded_project.assets
        assert sequences is not None and sequences["edit"].reference_mixes == edited.reference_mixes
    finally:
        io.cleanup()


def test_reference_dialog_commits_only_prepared_same_project_audio(qtbot, tmp_path: Path) -> None:
    from src.features.edit.core.project import EditProject
    from src.features.mixer.ui.reference_mix_dialog import ReferenceMixDialog

    audio = tmp_path / "reference.wav"
    _write_wav(audio, np.full((2, 24000), 0.25, np.float32), 48000)
    project = EditProject.create("Mix").import_asset(
        str(audio), media_type="audio", metadata={"duration_seconds": 0.5, "sample_rate": 48000})
    timeline = TimelineSequence(timeline_id="edit", name="Edit")
    monitor = ReferenceMixMonitor()
    operation_count = 0

    def commit(sequence_id: str, ref_id: str, ref: ReferenceMixRef | None) -> bool:
        nonlocal timeline, operation_count
        timeline, _ = SetReferenceMixCommand.create(sequence_id, ref_id, ref).apply(timeline)
        timeline = timeline.with_revision_bumped()
        operation_count += 1
        dialog.sync_context()
        return True

    dialog = ReferenceMixDialog(lambda: (project, timeline, monitor), commit)
    qtbot.addWidget(dialog)
    try:
        dialog.show()
        assert dialog.save_button.isEnabled()
        dialog.label.setText("Reference take")
        dialog.anchor.setValue(-120.0)
        dialog.save_button.click()
        qtbot.waitUntil(lambda: monitor.source is not None, timeout=5000)
        assert operation_count == 1
        assert dialog.reference_list.currentText() == "Reference take"
        assert timeline.reference_mixes[0].timeline_anchor_sample == -120
        assert monitor.source.timeline_anchor == -120
        dialog.b_button.click()
        assert monitor.reference_selected
        assert operation_count == 1

        project = EditProject.create("New project")
        timeline = TimelineSequence(timeline_id="edit", name="Edit")
        dialog.sync_context()
        assert monitor.source is None
        assert not dialog.b_button.isEnabled()
        assert operation_count == 1
    finally:
        dialog.close()
        dialog._worker.finish()
        assert dialog._worker.wait(5000)
