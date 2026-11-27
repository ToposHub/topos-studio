"""Video project manager package workflow tests."""

from __future__ import annotations

from threading import Event
from types import SimpleNamespace

from PyQt6.QtWidgets import QFileDialog, QMessageBox, QWidget

from src.app.ui.project_package_dialog import ProjectPackageDialog


class _Cancelled(Exception):
    pass


def _dialog(qtbot, monkeypatch, tmp_path, inspect, export):
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: str(tmp_path))
    parent = QWidget()
    qtbot.addWidget(parent)
    api = SimpleNamespace(
        inspect_project_package=inspect,
        export_project_package=export,
        ProjectPackageCancelled=_Cancelled,
    )
    dialog = ProjectPackageDialog(str(tmp_path / "Demo.topos"), api, active=True, parent=parent)
    dialog._test_parent = parent
    qtbot.addWidget(dialog)
    dialog.show()
    dialog._choose.click()
    return dialog


def test_packaging_rechecks_scope_and_keeps_folder_default(qtbot, monkeypatch, tmp_path):
    inspected = []
    exported = []

    def inspect(_project, scope):
        inspected.append(scope)
        return SimpleNamespace(estimated_bytes=16384, missing=(), dependencies=())

    def export(_project, destination, **options):
        exported.append((destination, options["scope"], options["zip_output"]))
        return SimpleNamespace(path=str(tmp_path / "Demo.topos"), missing=())

    dialog = _dialog(qtbot, monkeypatch, tmp_path, inspect, export)
    qtbot.waitUntil(dialog._export.isEnabled)
    assert dialog._scope.currentData() == "all"
    assert dialog._format.currentData() == "folder"
    dialog._format.setCurrentIndex(dialog._format.findData("zip"))
    dialog._scope.setCurrentIndex(dialog._scope.findData("project"))
    qtbot.waitUntil(dialog._export.isEnabled)
    assert not dialog._format.isVisible()
    with qtbot.waitSignal(dialog.accepted):
        dialog._export.click()
    assert inspected == ["all", "project"]
    assert exported == [(str(tmp_path), "project", False)]


def test_missing_files_require_confirmation_before_export(qtbot, monkeypatch, tmp_path):
    exported = []

    def inspect(_project, _scope):
        return SimpleNamespace(estimated_bytes=100, missing=("missing.mov",), dependencies=())

    def export(_project, destination, **options):
        exported.append((
            destination, options["allow_missing"], options["zip_output"], options["expected_missing"],
        ))
        return SimpleNamespace(path=str(tmp_path / "Demo.zip"), missing=("missing.mov",))

    dialog = _dialog(qtbot, monkeypatch, tmp_path, inspect, export)
    qtbot.waitUntil(dialog._export.isEnabled)
    assert "missing.mov" in dialog._details.toPlainText()
    dialog._format.setCurrentIndex(dialog._format.findData("zip"))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    dialog._export.click()
    assert not exported and dialog._export.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    with qtbot.waitSignal(dialog.accepted):
        dialog._export.click()
    assert exported == [(str(tmp_path), True, True, ("missing.mov",))]
    assert dialog.result_package.missing == ("missing.mov",)


def test_cancel_keeps_dialog_alive_until_export_worker_stops(qtbot, monkeypatch, tmp_path):
    started = Event()
    stopped = Event()

    def export(_project, _destination, *, cancelled_fn, progress_callback, **_options):
        progress_callback(8_000_000_000, 10_000_000_000)
        started.set()
        for _step in range(200):
            if cancelled_fn():
                stopped.set()
                raise _Cancelled()
            stopped.wait(0.01)
        raise AssertionError("Export cancellation was not forwarded")

    dialog = _dialog(
        qtbot, monkeypatch, tmp_path,
        lambda *_args: SimpleNamespace(estimated_bytes=100, missing=(), dependencies=()),
        export,
    )
    qtbot.waitUntil(dialog._export.isEnabled)
    dialog._export.click()
    qtbot.waitUntil(started.is_set)
    qtbot.waitUntil(lambda: dialog._progress.value() == 80)
    with qtbot.waitSignal(dialog.rejected):
        dialog.reject()
        assert dialog.isVisible()
    assert stopped.is_set()
