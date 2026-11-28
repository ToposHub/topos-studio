"""Interaction tests for the image grading startup dialog."""

from collections.abc import Callable
from pathlib import Path
from zipfile import ZipFile

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QWindow
from PyQt6.QtWidgets import QFileDialog, QLabel, QMessageBox

from src.app.controllers.file_controller import FileOpsMixin
from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin
from src.app.ui.welcome_dialog import WelcomeDialog
from src.shared.config.config_manager import ConfigManager
from src.shared.i18n.i18n import I18N


@pytest.fixture
def welcome_dialog(qtbot, monkeypatch, tmp_path: Path) -> tuple[WelcomeDialog, Path, Path, Path]:
    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
    image = tmp_path / "Image.png"
    image.touch()
    missing = tmp_path / "Missing.png"
    project = tmp_path / "Grade.topos"
    with ZipFile(project, "w") as archive:
        archive.writestr("manifest.json", '{"project_mode": "standalone_image"}')

    config = ConfigManager(tmp_path / "test-config.json")
    config.set("work_mode", "image", auto_save=False)
    config.set("recent_files", [str(image), str(missing)], auto_save=False)
    config.set("recent_projects", [str(project)], auto_save=False)
    dialog = WelcomeDialog(config)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(
        lambda: dialog.projects_list.item(0).data(Qt.ItemDataRole.UserRole) == str(project)
    )
    return dialog, image, missing, project


def test_dragging_brand_moves_dialog_without_native_move(qtbot, monkeypatch, welcome_dialog) -> None:
    dialog, *_ = welcome_dialog
    monkeypatch.setattr(QWindow, "startSystemMove", lambda _window: False)
    brand = dialog.findChild(QLabel, "welcomeTitle")
    start = dialog.pos()
    origin = brand.mapToGlobal(brand.rect().center())
    delta = QPoint(40, 20)
    qtbot.mousePress(brand, Qt.MouseButton.LeftButton, pos=brand.rect().center())

    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(dialog.mapFromGlobal(origin + delta)),
        QPointF(origin + delta),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QCoreApplication.sendEvent(dialog, event)
    qtbot.mouseRelease(dialog, Qt.MouseButton.LeftButton)

    assert dialog.pos() == start + delta
    assert dialog._drag_offset is None


def test_declining_restart_keeps_image_mode_selected(qtbot, monkeypatch, welcome_dialog) -> None:
    dialog, *_ = welcome_dialog
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    assert dialog._mode_btns["image"].isChecked()

    qtbot.mouseClick(dialog._mode_btns["video"], Qt.MouseButton.LeftButton)

    assert dialog._mode_btns["image"].isChecked()
    assert dialog._config.get("work_mode") == "image"


@pytest.mark.parametrize("kind", ["image", "project"])
def test_return_opens_selected_recent_item(qtbot, welcome_dialog, kind: str) -> None:
    dialog, image, _missing, project = welcome_dialog
    if kind == "image":
        recent, signal, path = dialog.recent_list, dialog.recent_file_selected, image
    else:
        recent, signal, path = dialog.projects_list, dialog.recent_project_selected, project
    recent.setCurrentRow(0)
    recent.setFocus()
    with qtbot.waitSignal(signal, timeout=1000) as opened:
        qtbot.keyClick(recent, Qt.Key.Key_Return)
    assert opened.args == [str(path)]


def test_missing_recent_image_shows_path_and_keeps_panel_open(
    qtbot, monkeypatch, welcome_dialog,
) -> None:
    dialog, _image, missing, _project = welcome_dialog
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _dialog, _title, message: messages.append(message))
    dialog.recent_list.setCurrentRow(1)
    dialog.recent_list.setFocus()

    with qtbot.assertNotEmitted(dialog.recent_file_selected):
        qtbot.keyClick(dialog.recent_list, Qt.Key.Key_Return)
        qtbot.waitUntil(lambda: bool(messages))

    assert str(missing) in messages[0]
    assert dialog.isVisible()


def test_open_project_picker_can_open_an_archive_outside_history(
    qtbot, monkeypatch, welcome_dialog, tmp_path: Path,
) -> None:
    dialog, *_ = welcome_dialog
    outside_history = tmp_path / "Other.topos"
    outside_history.touch()
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: ("", ""))
    with qtbot.assertNotEmitted(dialog.recent_project_selected):
        qtbot.mouseClick(dialog.open_project_btn, Qt.MouseButton.LeftButton)
    assert dialog.isVisible()

    def choose_project(_parent: WelcomeDialog, _title: str, _folder: str, files: str) -> tuple[str, str]:
        assert "*.topos" in files
        return str(outside_history), files

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose_project)
    with qtbot.waitSignal(dialog.recent_project_selected, timeout=1000) as opened:
        qtbot.mouseClick(dialog.open_project_btn, Qt.MouseButton.LeftButton)
    assert opened.args == [str(outside_history)]


def test_project_folder_lists_only_image_projects_and_preserves_recent_projects(
    qtbot, monkeypatch, welcome_dialog, tmp_path: Path,
) -> None:
    dialog, _image, _missing, recent = welcome_dialog
    folder = tmp_path / "Projects"
    folder.mkdir()
    image_project = folder / "Legacy.topos"
    for filename, manifest in (
        (image_project, '{"project_mode": "single_asset"}'),
        (folder / "Video.topos", '{"project_mode": "edit_project"}'),
        (folder / "Broken.topos", '{bad json}'),
    ):
        with ZipFile(filename, "w") as archive:
            archive.writestr("manifest.json", manifest)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: str(folder))
    menu = dialog.project_folder_menu
    dialog._build_project_folder_menu()
    menu.actions()[-1].trigger()

    def displayed_paths() -> list[str | None]:
        return [
            dialog.projects_list.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(dialog.projects_list.count())
        ]

    qtbot.waitUntil(lambda: displayed_paths() == [str(image_project)])
    assert ConfigManager(dialog._config.config_path).get("image_project_folders") == [str(folder)]
    assert dialog.projects_label.toolTip() == str(folder)

    dialog._build_project_folder_menu()
    menu.actions()[0].trigger()
    assert displayed_paths() == [str(recent)]

    folder_action = next(action for action in menu.actions() if action.toolTip() == str(folder))
    folder_action.trigger()
    qtbot.waitUntil(lambda: displayed_paths() == [str(image_project)])
    dialog.projects_list.setCurrentRow(0)
    dialog.projects_list.setFocus()
    with qtbot.waitSignal(dialog.recent_project_selected, timeout=1000) as opened:
        qtbot.keyClick(dialog.projects_list, Qt.Key.Key_Return)
    assert opened.args == [str(image_project)]


def test_unavailable_project_folder_shows_error_and_can_be_removed(
    qtbot, welcome_dialog, tmp_path: Path,
) -> None:
    dialog, _image, _missing, recent = welcome_dialog
    missing_folder = str(tmp_path / "Offline")
    dialog._config.set("image_project_folders", ["~unknown_topos_user/Projects", missing_folder], auto_save=False)
    dialog._build_project_folder_menu()
    menu = dialog.project_folder_menu
    next(action for action in menu.actions() if action.toolTip() == missing_folder).trigger()
    expected = I18N.tr("welcome.project_folder_unavailable", "Could not read this project folder.")
    qtbot.waitUntil(lambda: dialog.projects_list.item(0).text() == expected)

    dialog._build_project_folder_menu()
    menu.actions()[-1].trigger()
    assert dialog._config.get("image_project_folders") == []
    assert dialog.projects_list.item(0).data(Qt.ItemDataRole.UserRole) == str(recent)


def test_panel_closes_only_after_a_project_can_finish_opening(
    qtbot, monkeypatch, welcome_dialog,
) -> None:
    dialog, _image, _missing, project = welcome_dialog
    pending: list[Callable[[], object]] = []

    class Host:
        _open_project_path = FileOpsMixin._open_project_path
        _open_project_from_welcome = MenuToolbarMixin._open_project_from_welcome
        allowed = False

        def _check_project_matches_work_mode(self, _path: str) -> bool:
            return self.allowed

        def _read_project_mode_from_manifest(self, _path: str) -> str:
            return "standalone_image"

        def _load_standalone_project(self, _path: str) -> None:
            self._current_edit_project = object()

        def _sync_mg_preset_project_root(self, _path: str) -> None:
            pass

    def prepare_to_leave(_host: Host, resume: Callable[[], object]) -> bool:
        if pending:
            return True
        pending.append(resume)
        return False

    host = Host()
    monkeypatch.setattr(FileOpsMixin, "_prepare_to_leave_photo", prepare_to_leave)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *_args: (str(project), ""))
    dialog.recent_project_selected.connect(lambda path: host._open_project_from_welcome(path, dialog))

    qtbot.mouseClick(dialog.open_project_btn, Qt.MouseButton.LeftButton)
    assert dialog.isVisible()
    assert not pending

    host.allowed = True
    qtbot.mouseClick(dialog.open_project_btn, Qt.MouseButton.LeftButton)
    assert dialog.isVisible()
    assert len(pending) == 1

    pending[0]()
    assert not dialog.isVisible()
