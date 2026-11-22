"""Independent race/budget/Qt shutdown checks for the capture spool."""
from __future__ import annotations

import builtins
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.features.edit.core import recording_manager as recording


def test_capture_writer_thread_start_failure_does_not_leave_a_live_take(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = threading.Thread.start
    def start(thread: threading.Thread) -> None:
        if thread.name == "CaptureSpool":
            raise RuntimeError("no thread resources")
        original(thread)
    monkeypatch.setattr(threading.Thread, "start", start)
    manager = recording.RecordingManager(recording.RecordingConfig(output_dir=str(tmp_path)))
    errors = []
    manager.on_error = errors.append
    assert manager.start_recording() is False
    assert manager.state == manager.STATE_FAILED
    assert manager._current_take is None and manager._pcm_buffer is None
    assert errors and "capture writer" in errors[0]
    assert not list(tmp_path.iterdir())


def test_spool_counts_owned_dtype_and_returns_memory_to_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    run = recording._DurablePcmSpool._run
    monkeypatch.setattr(recording._DurablePcmSpool, "_run", lambda self: (gate.wait(3), run(self)))
    writer = recording._DurablePcmSpool(str(tmp_path), 1, 48000, memory_limit=1024)
    try:
        writer.append(np.zeros((1, 128), dtype=np.float64))
        assert writer.memory_bytes == 512
    finally:
        gate.set()
        writer.finish()
    assert writer.memory_bytes == 0
    assert writer.confirmed_samples == 128
    writer.discard()


def test_append_admission_is_atomic_with_finish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    finishing = threading.Event()
    original_array = recording.np.array

    def slow_owned_copy(*args: object, **kwargs: object) -> np.ndarray:
        entered.set()
        assert release.wait(3)
        return original_array(*args, **kwargs)

    monkeypatch.setattr(recording.np, "array", slow_owned_copy)
    writer = recording._DurablePcmSpool(str(tmp_path), 1, 48000)
    errors: list[Exception] = []

    def append() -> None:
        try:
            writer.append(np.zeros((1, 10), np.float32))
        except Exception as error:
            errors.append(error)

    def finish() -> None:
        writer.finish()
        finishing.set()

    producer = threading.Thread(target=append)
    producer.start()
    assert entered.wait(3)
    finisher = threading.Thread(target=finish)
    finisher.start()
    try:
        # A shutdown cannot silently close the writer while an admitted copy
        # is still about to enqueue its bytes.
        assert not finishing.wait(0.05)
    finally:
        release.set()
        producer.join(3)
        finisher.join(3)
        writer.discard()
    assert not errors
    assert writer.confirmed_samples == 10
    assert writer.memory_bytes == 0


def test_recovery_reads_big_segments_with_a_fixed_pcm_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = recording._DurablePcmSpool(str(tmp_path), 2, 48000)
    writer.append(np.arange(2000, dtype=np.float32).reshape(2, 1000) / 2000)
    writer.finish()
    reads: list[int] = []

    class Reader:
        def __init__(self, file: object) -> None:
            self.file = file

        def __enter__(self) -> Reader:
            self.file.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self.file.__exit__(*args)

        def seek(self, *args: object) -> object:
            return self.file.seek(*args)

        def read(self, size: int = -1) -> bytes:
            reads.append(size)
            assert 0 <= size <= 32  # 96-byte budget / raw+planar+channel copies
            return self.file.read(size)

    def checked_open(path: object, mode: str = "r", *args: object, **kwargs: object) -> object:
        file = builtins.open(path, mode, *args, **kwargs)
        return Reader(file) if Path(path) == writer.raw_path and mode == "rb" else file

    monkeypatch.setattr(recording, "open", checked_open, raising=False)
    take = recording.recover_capture_spool(writer.manifest_path, tmp_path / "recovered.wav",
                                           memory_limit_bytes=96)
    assert reads
    assert take.status == "completed_partial"
    assert take.duration_seconds == pytest.approx(1000 / 48000)
    writer.discard()


def test_no_durable_prefix_cannot_publish_completed_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = recording.RecordingManager(recording.RecordingConfig(output_dir=str(tmp_path)))
    take = recording.TakeInfo(file_path=str(tmp_path / "empty.wav"), status="recording")
    pcm = SimpleNamespace(total_samples=0, finish=lambda: None,
                          _durable=SimpleNamespace(error=OSError("disk full"),
                                                   manifest_path=tmp_path / "recovery.json"))
    writes = []
    monkeypatch.setattr(recording, "write_take_blocks", lambda *args, **kwargs: writes.append(args) or True)
    manager._write_take_payload(take, pcm, False)
    assert take.status == "failed"
    assert take.duration_seconds == 0
    assert not writes


def test_recovery_cancellation_preserves_previous_output_and_sources(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = recording._DurablePcmSpool(str(tmp_path), 1, 48000)
    writer.append(np.ones((1, 100), np.float32) * 0.1)
    writer.finish()
    destination = tmp_path / "approved.wav"
    destination.write_bytes(b"approved previous delivery")
    cancelled = threading.Event()
    write = recording.write_take_blocks

    def cancel_after_encoding(*args: object, **kwargs: object) -> bool:
        result = write(*args, **kwargs)
        cancelled.set()  # Encoding finished, but the final publication has not.
        return result

    monkeypatch.setattr(recording, "write_take_blocks", cancel_after_encoding)
    try:
        with pytest.raises(InterruptedError, match="cancelled"):
            recording.recover_capture_spool(writer.manifest_path, destination,
                                            cancel_check=cancelled.is_set)
        assert destination.read_bytes() == b"approved previous delivery"
        assert writer.raw_path.is_file() and writer.journal_path.is_file()
        assert not list(tmp_path.glob("*.recovery-*"))
    finally:
        writer.discard()


def test_cancel_during_background_flush_returns_without_joining_disk(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qtbot) -> None:
    gate = threading.Event()
    original = recording.RecordingManager._write_take_payload

    def slow_write(self, take, pcm, partial) -> None:
        assert gate.wait(3)
        original(self, take, pcm, partial)

    monkeypatch.setattr(recording.RecordingManager, "_start_capture_device", lambda self: True)
    monkeypatch.setattr(recording.RecordingManager, "_write_take_payload", slow_write)
    manager = recording.RecordingManager(recording.RecordingConfig(
        output_dir=str(tmp_path), pre_roll_seconds=0, post_roll_seconds=0))
    manager.enable_background_flush = True
    completed = []
    manager.on_take_completed = completed.append
    assert manager.start_recording()
    manager.inject_pcm(np.zeros((1, 100), np.float32))
    assert manager.stop_recording() is None
    assert manager.state == manager.STATE_FLUSHING
    unblock = threading.Timer(0.5, gate.set)
    unblock.start()
    try:
        started = time.monotonic()
        manager.cancel_recording()
        elapsed = time.monotonic() - started
        assert elapsed < 0.2
        assert manager.state == manager.STATE_FLUSHING
    finally:
        gate.set()
        unblock.cancel()
        qtbot.waitUntil(lambda: manager.state == manager.STATE_IDLE, timeout=10000)
    assert len(completed) == 1


@pytest.mark.parametrize("failure", ["timer_start", "timer_inactive", "thread_start"])
def test_gui_flush_start_failure_preserves_recovery_without_ui_disk_work(
        failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp) -> None:
    from PyQt6.QtCore import QTimer

    gate = threading.Event()
    run = recording._DurablePcmSpool._run
    monkeypatch.setattr(recording._DurablePcmSpool, "_run", lambda self: (gate.wait(3), run(self)))
    monkeypatch.setattr(recording.RecordingManager, "_start_capture_device", lambda self: True)
    manager = recording.RecordingManager(recording.RecordingConfig(
        output_dir=str(tmp_path), pre_roll_seconds=0, post_roll_seconds=0))
    manager.enable_background_flush = True
    completed: list = []
    errors: list[str] = []
    manager.on_take_completed = completed.append
    manager.on_error = errors.append
    assert manager.start_recording()
    writer = manager._pcm_buffer._durable
    manager.inject_pcm(np.full((1, 256), 0.1, np.float32))
    writes: list[int] = []
    original_write = manager._write_take_payload

    def write(*args: object, **kwargs: object) -> None:
        writes.append(threading.get_ident())
        original_write(*args, **kwargs)

    monkeypatch.setattr(manager, "_write_take_payload", write)
    original_start = threading.Thread.start

    def fail_thread_start(thread: threading.Thread) -> None:
        if thread.name == "TakeFlush":
            raise RuntimeError("injected encoder startup failure")
        original_start(thread)

    def fail_timer_start(timer: QTimer, *args: object) -> None:
        raise RuntimeError("injected polling startup failure")

    try:
        with monkeypatch.context() as patch:
            if failure == "thread_start":
                patch.setattr(threading.Thread, "start", fail_thread_start)
            elif failure == "timer_start":
                patch.setattr(QTimer, "start", fail_timer_start)
            else:
                patch.setattr(QTimer, "start", lambda timer, *args: None)
            started = time.monotonic()
            assert manager.stop_recording() is None
            assert time.monotonic() - started < 0.2
        assert manager.state == manager.STATE_IDLE
        assert manager._flush_thread is None and manager._flush_timer is None
        assert not writes and not completed
        assert errors
        assert len(manager.takes) == 1 and manager.takes[0].status == "failed"
        assert "recovery retained" in manager.takes[0].notes
        assert writer._stop.is_set()
    finally:
        gate.set()
        writer.finish()
    assert writer.confirmed_samples == 256
    recovered = recording.recover_capture_spool(writer.manifest_path, tmp_path / "recovered.wav")
    assert recovered.status == "completed_partial"
    assert recovered.duration_seconds == pytest.approx(256 / 48000)
    assert not Path(manager.takes[0].file_path).exists()
    writer.discard()
