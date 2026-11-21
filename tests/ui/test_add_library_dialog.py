"""The native form chooses a library path without creating or registering it."""
from collections.abc import Iterator
from pathlib import Path
from threading import Event

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QPoint, QRect, QTimer, Qt
from PyQt6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QLineEdit, QListWidget, QListWidgetItem, QPushButton, QWidget,
)
from pytestqt.qtbot import QtBot

from src.app.ui.add_library_dialog import AddLibraryDialog
from src.app.ui import library_folder_picker
from src.app.ui.library_folder_picker import LibraryFolderPicker
from src.app.ui.native_files_locations import MountedVolume
from src.app.ui.native_files_transport import NativeFilesTransport
from src.features.files.repository import FilesDataLocator
from src.features.files.repository.catalog import FilesCatalog
from src.features.files.services.facade import FilesFacade
from src.shared.ui.style_sheet import DARK_THEME


@pytest.fixture(autouse=True)
def dark_theme(qapp: QApplication) -> Iterator[None]:
    original = qapp.styleSheet()
    qapp.setStyleSheet(DARK_THEME)
    try:
        yield
    finally:
        qapp.setStyleSheet(original)


@pytest.fixture
def folder_transport(tmp_path: Path) -> Iterator[NativeFilesTransport]:
    locator = FilesDataLocator.for_config_root(tmp_path / "config")
    locator.ensure_layout()
    catalog = FilesCatalog(locator)
    facade = FilesFacade(catalog, max_workers=1)
    transport = NativeFilesTransport(lambda: "", None, facade=facade)
    try:
        yield transport
    finally:
        transport.shutdown()
        facade.shutdown(wait=True)
        catalog.close()


def _fits(dialog: QDialog, widget: QWidget) -> bool:
    return dialog.contentsRect().contains(QRect(widget.mapTo(dialog, QPoint()), widget.size()))


def test_reference_starts_at_current_directory_without_a_library_name(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = AddLibraryDialog(None, initial_path=str(tmp_path))
    qtbot.addWidget(dialog)
    dialog.show()

    assert dialog.reference_mode.isChecked()
    assert dialog.name_edit.isHidden()
    assert dialog.destination.isHidden()
    assert _fits(dialog, dialog.create_mode)
    assert _fits(dialog, dialog.directory_edit)
    assert _fits(dialog, dialog.add_button)
    dialog.add_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.directory == str(tmp_path)
    assert dialog.library_name == ""


def test_create_requires_a_leaf_and_previews_destination_before_accepting(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = AddLibraryDialog(None, initial_path=str(tmp_path))
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.create_mode.click()
    assert not dialog.name_edit.isHidden()
    assert not dialog.add_button.isEnabled()

    for name in ("../other", "mixed\\other", "C:other", "..", "NUL"):
        dialog.name_edit.setText(name)
        assert not dialog.add_button.isEnabled()
        dialog.accept()
        assert dialog.result() == QDialog.DialogCode.Rejected

    name = "采访素材"
    target = tmp_path / name
    dialog.name_edit.setText(name)
    qtbot.waitUntil(lambda: _fits(dialog, dialog.destination) and _fits(dialog, dialog.add_button))
    assert dialog.destination.text() == str(target)
    assert dialog.add_button.isEnabled()
    dialog.add_button.click()

    assert dialog.result() == QDialog.DialogCode.Accepted
    assert (dialog.directory, dialog.library_name) == (str(tmp_path), name)
    assert not target.exists()


@pytest.mark.parametrize("creating", (False, True))
def test_folder_picker_can_replace_an_invalid_path(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, creating: bool,
) -> None:
    folder = tmp_path / "素材 01"
    folder.mkdir()
    dialog = AddLibraryDialog(None, initial_path=str(tmp_path))
    monkeypatch.setattr(dialog, "_home", tmp_path)
    qtbot.addWidget(dialog)
    if creating:
        dialog.create_mode.setChecked(True)
        dialog.name_edit.setText("Library")
    dialog.directory_edit.setText("relative")
    assert not dialog.add_button.isEnabled()
    dialog.show()

    def choose_folder() -> None:
        picker = dialog.findChild(LibraryFolderPicker)
        try:
            qtbot.waitUntil(lambda: picker.choose.isEnabled())
            _click(qtbot, picker.folders, _item(picker.folders, folder))
            assert picker.selected.text() == str(folder)
            picker.choose.click()
            qtbot.waitUntil(lambda: not picker.isVisible())
        finally:
            if picker.isVisible():
                picker.reject()

    QTimer.singleShot(0, choose_folder)
    browse = next(button for button in dialog.findChildren(QPushButton) if button not in
                  dialog.findChild(QDialogButtonBox).buttons())
    qtbot.mouseClick(browse, Qt.MouseButton.LeftButton)
    assert dialog.directory_edit.text() == str(folder)
    assert dialog.add_button.isEnabled()
    assert dialog.directory == ""
    assert not (folder / "Library").exists()


def _item(view: QListWidget, path: Path) -> QListWidgetItem:
    return next(view.item(row) for row in range(view.count())
                if view.item(row).data(Qt.ItemDataRole.UserRole) == path)


def _click(qtbot: QtBot, view: QListWidget, item: QListWidgetItem) -> None:
    view.scrollToItem(item)
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualItemRect(item).center())


def _submit_folder_name(picker: LibraryFolderPicker, name: str, *, cancel: bool = False) -> None:
    def enter_name() -> None:
        prompt = QApplication.activeModalWidget()
        assert isinstance(prompt, QDialog) and prompt.parent() is picker
        try:
            edit = prompt.findChild(QLineEdit)
            actions = prompt.findChild(QDialogButtonBox)
            create = actions.button(QDialogButtonBox.StandardButton.Ok)
            assert not create.isEnabled()
            edit.setText("../outside")
            assert not create.isEnabled()
            edit.setText(name)
            assert create.isEnabled()
            button = QDialogButtonBox.StandardButton.Cancel if cancel else QDialogButtonBox.StandardButton.Ok
            actions.button(button).click()
        finally:
            if prompt.isVisible():
                prompt.reject()

    QTimer.singleShot(0, enter_name)
    picker.new_folder.click()


def test_create_folder_from_browse_previews_new_library_path(
    qtbot: QtBot, tmp_path: Path, folder_transport: NativeFilesTransport,
) -> None:
    existing = tmp_path / "Existing"
    existing.mkdir()
    new_parent = tmp_path / "项目素材"
    dialog = AddLibraryDialog(None, initial_path=str(tmp_path), transport=folder_transport)
    qtbot.addWidget(dialog)
    dialog.create_mode.click()
    dialog.name_edit.setText("素材库")
    dialog.show()

    def browse_and_create() -> None:
        picker = dialog.findChild(LibraryFolderPicker)
        try:
            qtbot.waitUntil(lambda: picker.new_folder.isEnabled())
            _click(qtbot, picker.folders, _item(picker.folders, existing))
            _submit_folder_name(picker, new_parent.name)
            qtbot.waitUntil(lambda: picker.location.text() == str(new_parent))
            assert picker.directory is None
            assert new_parent.is_dir()
            assert not (existing / new_parent.name).exists()
            assert picker.selected.text() == str(new_parent)
            picker.choose.click()
            qtbot.waitUntil(lambda: not picker.isVisible())
        finally:
            if picker.isVisible():
                picker.reject()

    QTimer.singleShot(0, browse_and_create)
    browse = next(button for button in dialog.findChildren(QPushButton) if button not in
                  dialog.findChild(QDialogButtonBox).buttons())
    qtbot.mouseClick(browse, Qt.MouseButton.LeftButton)
    assert dialog.directory_edit.text() == str(new_parent)
    assert dialog.destination.text() == str(new_parent / "素材库")
    assert not (new_parent / "素材库").exists()


def test_new_folder_cancel_and_name_conflict_leave_location_in_place(
    qtbot: QtBot, tmp_path: Path, folder_transport: NativeFilesTransport,
) -> None:
    existing = tmp_path / "Existing"
    existing.mkdir()
    parent = QWidget()
    qtbot.addWidget(parent)
    picker = LibraryFolderPicker(parent, title="Choose", initial_path=tmp_path, home=tmp_path,
                                 transport=folder_transport)
    picker.show()
    qtbot.waitUntil(lambda: picker.new_folder.isEnabled())
    _submit_folder_name(picker, "Cancelled", cancel=True)
    assert not (tmp_path / "Cancelled").exists()
    with qtbot.waitSignal(folder_transport.task_finished) as result:
        _submit_folder_name(picker, existing.name)
    assert result.args[0]["failures"][0]["code"] == "DEST_EXISTS"
    qtbot.waitUntil(lambda: picker.new_folder.isEnabled())
    assert picker.location.text() == str(tmp_path)
    assert picker.status.isVisible()
    assert picker.directory is None


def test_new_folder_preserves_the_disk_path_spelling(
    qtbot: QtBot, tmp_path: Path, folder_transport: NativeFilesTransport,
) -> None:
    parent = tmp_path / "Cafe\u0301"
    parent.mkdir()
    destination = parent / "Media"
    with qtbot.waitSignal(folder_transport.folder_created) as result:
        folder_transport.create_folder("new-folder", str(parent), destination.name)
    assert result.args == ["new-folder", str(destination), ""]
    assert destination.is_dir()


def test_pending_folder_creation_is_cancelled_if_the_mount_is_replaced(
    qtbot: QtBot, tmp_path: Path, folder_transport: NativeFilesTransport, monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive, home = tmp_path / "disk", tmp_path / "home"
    drive.mkdir()
    home.mkdir()
    device = b"first"
    monkeypatch.setattr(library_folder_picker, "_drives", lambda: (MountedVolume(str(drive), "Disk", device),))
    parent = QWidget()
    qtbot.addWidget(parent)
    picker = LibraryFolderPicker(parent, title="Choose", initial_path=drive, home=home, transport=folder_transport)
    picker.show()
    qtbot.waitUntil(lambda: picker.new_folder.isEnabled() and drive in picker._places)

    release = Event()
    folder_transport._facade.run("busy", "busy", work=lambda: release.wait(5), listener=lambda _: None)
    try:
        with qtbot.waitSignal(folder_transport.task_finished) as result:
            _submit_folder_name(picker, "New folder")
            device = b"replacement"
            picker._load_drives()
            qtbot.waitUntil(lambda: picker.location.text() == str(home))
            release.set()
        assert result.args[0]["cancelled"]
        assert not (drive / "New folder").exists()
    finally:
        release.set()


def test_picker_browses_drive_directories_and_detects_mount_changes(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive, removable, home = (tmp_path / name for name in ("disk", "usb", "home"))
    for directory in (drive, removable, home):
        directory.mkdir()
    nested = drive / "一级" / "二级"
    nested.mkdir(parents=True)
    (drive / ".hidden").mkdir()
    (drive / "photo.png").touch()
    first = MountedVolume(str(drive), "Media Drive", b"disk")
    second = MountedVolume(str(removable), "Media Drive", b"usb")
    drives = [first, second]
    monkeypatch.setattr(library_folder_picker, "_drives", lambda: tuple(drives))
    parent = QWidget()
    qtbot.addWidget(parent)
    picker = LibraryFolderPicker(parent, title="Choose", initial_path=home, home=home)
    picker.show()
    qtbot.waitUntil(lambda: picker.choose.isEnabled() and removable in picker._places)
    assert _item(picker.places, drive).text() == _item(picker.places, removable).text()

    _click(qtbot, picker.places, _item(picker.places, drive))
    qtbot.waitUntil(lambda: picker.location.text() == str(drive))
    assert picker.folders.count() == 1
    _click(qtbot, picker.folders, _item(picker.folders, nested.parent))
    qtbot.keyClick(picker.folders, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: picker.location.text() == str(nested.parent))
    assert picker.folders.item(0).text() == nested.name
    picker.up.click()
    qtbot.waitUntil(lambda: picker.location.text() == str(drive))

    drives.remove(second)
    picker._load_drives()
    qtbot.waitUntil(lambda: removable not in picker._places)
    assert _item(picker.places, drive).isSelected()

    drives[0] = MountedVolume(str(drive), first.name, b"replacement")
    picker._load_drives()
    qtbot.waitUntil(lambda: picker.location.text() == str(home))
    assert picker.folders.count() == 0
    assert picker.selected.text() == str(home)


def test_missing_start_falls_back_and_vanished_selection_is_rejected(
    qtbot: QtBot, tmp_path: Path,
) -> None:
    available = tmp_path / "Available"
    available.mkdir()
    missing = tmp_path / "Missing"
    parent = QWidget()
    qtbot.addWidget(parent)
    picker = LibraryFolderPicker(parent, title="Choose", initial_path=missing, home=tmp_path)
    picker.show()
    qtbot.waitUntil(lambda: picker.location.text() == str(tmp_path))
    _click(qtbot, picker.folders, _item(picker.folders, available))
    available.rmdir()
    picker.choose.click()
    qtbot.waitUntil(lambda: picker.status.toolTip() != "")
    assert picker.isVisible()
    assert picker.directory is None
    assert picker.selected.text() == str(tmp_path)


def test_slow_folder_read_does_not_override_new_navigation_or_block_close(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slow = tmp_path / "Slow drive"
    slow.mkdir()
    slow_started = Event()
    allow_slow = Event()
    real_read = library_folder_picker.child_directories

    def read(path: Path) -> list[Path]:
        if path == slow:
            slow_started.set()
            allow_slow.wait(5)
        return real_read(path)

    monkeypatch.setattr(library_folder_picker, "child_directories", read)
    monkeypatch.setattr(library_folder_picker, "_drives", lambda: (MountedVolume(str(slow), slow.name, b"slow"),))
    parent = QWidget()
    qtbot.addWidget(parent)
    picker = LibraryFolderPicker(parent, title="Choose", initial_path=slow, home=tmp_path)
    picker.show()
    try:
        qtbot.waitUntil(lambda: slow_started.is_set())
        _click(qtbot, picker.places, _item(picker.places, tmp_path))
        assert not picker.choose.isEnabled()
        allow_slow.set()
        qtbot.waitUntil(lambda: picker.location.text() == str(tmp_path))

        allow_slow.clear()
        slow_started.clear()
        qtbot.waitUntil(lambda: slow in picker._places)
        _click(qtbot, picker.places, _item(picker.places, slow))
        qtbot.waitUntil(lambda: slow_started.is_set())
        picker.reject()
        assert not picker.isVisible()
        picker.deleteLater()
    finally:
        allow_slow.set()


def test_late_notification_is_dropped_after_qt_tears_down_signals() -> None:
    job = library_folder_picker._PickerJob(lambda: ())
    sip.delete(job.signals)
    job.run()
