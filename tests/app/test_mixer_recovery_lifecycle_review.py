"""Review regressions for the asynchronous recording recovery UI lifecycle."""
from pathlib import Path
from types import SimpleNamespace
import threading

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFileDialog, QMainWindow

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.recording_manager import TakeInfo


class _RecoveryHost(QMainWindow, EditControllerMixin):
    def __init__(self) -> None:
        super().__init__()
        self.status_bar = self.statusBar()
        self._current_edit_project = SimpleNamespace(project_id="origin-project")
        self.media_pool_panel = SimpleNamespace()


def _dialogs(monkeypatch, tmp_path: Path) -> list[str]:
    calls = []

    def select_source(*args, **kwargs):
        calls.append("source")
        return str(tmp_path / "capture.json"), ""

    def select_output(*args, **kwargs):
        calls.append("output")
        return str(tmp_path / "recovered.wav"), ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", select_source)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", select_output)
    return calls


def test_recovery_cannot_import_into_a_different_project(qtbot, tmp_path, monkeypatch) -> None:
    from src.features.edit.core import recording_manager
    entered, release = threading.Event(), threading.Event()

    def recover(_source, output, *, cancel_check):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test job was not released")
        return TakeInfo(status="completed_partial", file_path=output)

    monkeypatch.setattr(recording_manager, "recover_capture_spool", recover)
    _dialogs(monkeypatch, tmp_path)
    host = _RecoveryHost()
    qtbot.addWidget(host)
    imported = []
    host._on_mixer_recover_recording()
    worker = host._mixer_recovery_worker
    qtbot.waitUntil(entered.is_set)
    host._current_edit_project = SimpleNamespace(
        project_id="different-project", import_asset=lambda **kwargs: imported.append(kwargs))
    release.set()
    qtbot.waitUntil(lambda: host._mixer_recovery_key is None)
    qtbot.waitUntil(lambda: not worker.isRunning())
    assert imported == []


def test_duplicate_start_does_not_open_more_dialogs_or_replace_the_running_job(qtbot, tmp_path, monkeypatch) -> None:
    from src.features.edit.core import recording_manager
    entered = threading.Event()

    def recover(_source, _output, *, cancel_check):
        entered.set()
        while not cancel_check():
            threading.Event().wait(0.005)
        raise InterruptedError("cancelled")

    monkeypatch.setattr(recording_manager, "recover_capture_spool", recover)
    calls = _dialogs(monkeypatch, tmp_path)
    host = _RecoveryHost()
    qtbot.addWidget(host)
    host._on_mixer_recover_recording()
    worker = host._mixer_recovery_worker
    qtbot.waitUntil(entered.is_set)
    identity = host._mixer_recovery_key
    host._on_mixer_recover_recording()
    assert calls == ["source", "output"]
    assert host._mixer_recovery_key == identity
    assert host._mixer_recovery_worker is worker
    host._cancel_mixer_recording_recovery()
    qtbot.waitUntil(lambda: not worker.isRunning())


def test_host_destruction_cancels_the_unparented_worker_without_late_import(qtbot, tmp_path, monkeypatch) -> None:
    from src.features.edit.core import recording_manager
    entered, cancelled = threading.Event(), threading.Event()

    def recover(_source, _output, *, cancel_check):
        entered.set()
        while not cancel_check():
            threading.Event().wait(0.005)
        cancelled.set()
        raise InterruptedError("cancelled")

    monkeypatch.setattr(recording_manager, "recover_capture_spool", recover)
    _dialogs(monkeypatch, tmp_path)
    host = _RecoveryHost()
    host.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    imported = []
    host._on_take_recorded = lambda *args, **kwargs: imported.append(args)
    host.show()
    host._on_mixer_recover_recording()
    worker = host._mixer_recovery_worker
    qtbot.waitUntil(entered.is_set)
    host.close()
    qtbot.waitUntil(cancelled.is_set)
    qtbot.waitUntil(lambda: not worker.isRunning())
    assert imported == []
