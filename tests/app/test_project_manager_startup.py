"""Startup Project Manager fallback tests."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from PyQt6.QtCore import QCoreApplication, QEvent, QItemSelectionModel, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QWindow
from PyQt6.QtWidgets import QDialog, QLabel, QListView, QMenu

from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin
from src.shared.config.config_manager import ConfigManager


def test_next_untitled_project_name_avoids_project_manager_names(monkeypatch):
    from src.shared.project import library

    monkeypatch.setattr(
        library,
        "scan_video_projects",
        lambda _config: [
            SimpleNamespace(name="Untitled Project"),
            SimpleNamespace(name="Untitled Project 2"),
            SimpleNamespace(name="A saved project"),
        ],
    )

    host = MenuToolbarMixin()
    host.config_manager = object()

    assert host._next_untitled_project_name() == "Untitled Project 3"


@pytest.mark.parametrize(
    ("preferences", "startup", "shown"),
    [
        ({}, True, True),
        ({"show_project_manager_on_startup": False}, True, False),
        ({"show_project_manager_on_startup": False}, False, True),
    ],
)
def test_manager_respects_startup_preference(monkeypatch, preferences, startup, shown):
    import src.app.ui.project_manager_dialog as project_manager_module

    shown_dialogs = []

    class _Signal:
        def connect(self, _slot):
            return None

    class _Dialog:
        project_selected = _Signal()
        project_renamed = _Signal()
        new_project_requested = _Signal()

        def __init__(self, *_args, **_kwargs):
            pass

        def exec(self):
            shown_dialogs.append(self)
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(project_manager_module, "ProjectManagerDialog", _Dialog)
    monkeypatch.setattr(
        MenuToolbarMixin,
        "_next_untitled_project_name",
        lambda _self: "Untitled Project 4",
    )

    class _Host(MenuToolbarMixin):
        def __init__(self):
            self.config_manager = preferences
            self._current_edit_project = None
            self._current_edit_project_filepath = None
            self.created_name = None

        def _auto_create_edit_project(self, name):
            self.created_name = name
            self._current_edit_project = SimpleNamespace(name=name)

        def _open_project_from_manager(self, *_args):
            return False

        def _on_project_renamed_from_manager(self, *_args):
            return None

        def _new_project_from_manager(self, *_args):
            return None

    host = _Host()
    host._show_project_manager(startup=startup)

    assert bool(shown_dialogs) == shown
    if startup:
        assert host.created_name == "Untitled Project 4"
        assert host._current_edit_project.name == "Untitled Project 4"
    else:
        assert host._current_edit_project is None


@pytest.fixture
def project_browser(qtbot, monkeypatch, tmp_path):
    from src.app.ui import project_manager_dialog as manager

    monkeypatch.setenv("TOPOS_PRODUCT_MODE", "studio")
    projects = []
    secondary = tmp_path / "External"
    secondary.mkdir()
    for name, modified in (("Cut", 1), ("Grade", 2), ("Delivery", 3)):
        library = secondary if name == "Delivery" else tmp_path
        path = library / f"{name}.topos"
        with ZipFile(path, "w") as archive:
            archive.writestr("project.json", '{"assets": {}}')
        projects.append(manager.ProjectMeta(str(path), name, "video", 0, modified, modified))
    cut, grade, _delivery = projects
    config = ConfigManager(tmp_path / "test-config.json")
    values = {
        "work_mode": "video",
        "project_library_directory": str(tmp_path),
        "last_project": cut.path,
        "project_tags": {grade.path: ["Night"]},
        "favorite_projects": [grade.path],
    }
    for key, value in values.items():
        config.set(key, value, auto_save=False)
    monkeypatch.setattr(
        manager, "scan_project_libraries",
        lambda _config: (
            projects,
            [manager.ProjectLibraryLocation(str(path), True) for path in (tmp_path, secondary)],
        ),
    )
    dialog = manager.ProjectManagerDialog(config)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.list_widget.count() == 3)
    return dialog, projects


def test_search_filters_in_place_and_enter_opens_the_only_match(qtbot, project_browser):
    dialog, projects = project_browser
    cut, grade, _delivery = projects
    found = dialog.list_widget.findItems(grade.name, Qt.MatchFlag.MatchExactly)[0]
    dialog.list_widget.setCurrentRow(dialog.list_widget.count() - 1)
    assert dialog.open_btn.isEnabled()

    dialog.search_box.setText("night")
    assert not found.isHidden()
    assert not dialog.open_btn.isEnabled()
    assert dialog.list_widget.selectedItems() == []
    assert dialog.list_widget.findItems(cut.name, Qt.MatchFlag.MatchExactly)[0].isHidden()
    with qtbot.waitSignal(dialog.project_selected, timeout=1000) as opened:
        qtbot.keyClick(dialog.search_box, Qt.Key.Key_Return)
    assert opened.args == [grade.path]

    dialog.search_box.setText("no such project")
    assert dialog.empty_label.isVisible()
    dialog.search_box.clear()
    assert not dialog.empty_label.isVisible()
    assert not found.isHidden()


def test_sort_refresh_and_keyboard_open_preserve_selected_project(qtbot, project_browser):
    dialog, projects = project_browser
    cut, _grade, _delivery = projects
    dialog.list_widget.setCurrentRow(dialog.list_widget.count() - 1)
    dialog.sort_box.setCurrentIndex(dialog.sort_box.findData("name"))
    assert dialog.list_widget.currentRow() == 0

    dialog.refresh_btn.click()
    dialog.sort_box.setCurrentIndex(dialog.sort_box.findData("recent"))
    qtbot.waitUntil(lambda: dialog.open_btn.isEnabled())
    assert dialog.list_widget.currentRow() == 2

    # Qt can keep the current index independent from the selected item.
    dialog.list_widget.setCurrentRow(0, QItemSelectionModel.SelectionFlag.NoUpdate)
    dialog.list_widget.setFocus()
    with qtbot.waitSignal(dialog.project_selected, timeout=1000) as opened:
        qtbot.keyClick(dialog.list_widget, Qt.Key.Key_Return)
    assert opened.args == [cut.path]


def test_resume_hides_if_last_project_is_removed(qtbot, project_browser):
    dialog, projects = project_browser
    cut, _grade, _delivery = projects
    assert dialog.resume_btn.isVisible()
    with qtbot.waitSignal(dialog.project_selected, timeout=1000) as opened:
        dialog.resume_btn.click()
    assert opened.args == [cut.path]

    Path(cut.path).unlink()
    with qtbot.assertNotEmitted(dialog.project_selected):
        dialog.resume_btn.click()
    assert not dialog.resume_btn.isVisible()


def test_actions_menu_keeps_batch_selection_and_protects_open_project(monkeypatch, project_browser):
    dialog, projects = project_browser
    cut, grade, _delivery = projects
    for row in (1, 2):
        dialog.list_widget.item(row).setSelected(True)
    assert dialog.actions_btn.isEnabled()
    assert not dialog.open_btn.isEnabled()

    delete_previews = []
    deleted_paths = []

    def select_delete(menu, _anchor):
        action = menu.actions()[-1]
        delete_previews.append(action.isEnabled())
        return action if action.isEnabled() else None

    monkeypatch.setattr(QMenu, "exec", select_delete)
    monkeypatch.setattr(dialog, "_delete_projects", lambda paths: deleted_paths.extend(paths))
    dialog._active_project_path = grade.path
    dialog.actions_btn.click()
    assert delete_previews == [False]
    assert not deleted_paths

    dialog._active_project_path = None
    dialog.actions_btn.click()
    assert delete_previews == [False, True]
    assert set(deleted_paths) == {cut.path, grade.path}


def test_startup_checkbox_saves_and_can_be_reenabled(project_browser):
    dialog, _projects = project_browser
    config_path = dialog._config.config_path
    assert not dialog.skip_startup_check.isChecked()
    dialog.skip_startup_check.click()
    assert ConfigManager(config_path).get("show_project_manager_on_startup") is False
    dialog.skip_startup_check.click()
    assert ConfigManager(config_path).get("show_project_manager_on_startup") is True


def test_dragging_header_moves_window_when_native_move_is_unavailable(
    qtbot, monkeypatch, project_browser
):
    dialog, _projects = project_browser
    monkeypatch.setattr(QWindow, "startSystemMove", lambda _window: False)
    brand = dialog.findChild(QLabel, "pmBrand")
    start = dialog.pos()
    origin = brand.mapToGlobal(brand.rect().center())
    delta = QPoint(42, 24)

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


def test_views_preserve_selection_and_combine_library_favorites_and_search(qtbot, project_browser):
    dialog, projects = project_browser
    cut, grade, delivery = projects
    grade_item = dialog.list_widget.findItems(grade.name, Qt.MatchFlag.MatchExactly)[0]
    dialog.list_widget.setCurrentItem(grade_item)
    dialog.list_view_btn.click()
    assert dialog.list_widget.viewMode() == QListView.ViewMode.ListMode
    assert dialog.list_headers.isVisible()
    assert dialog.list_widget.selectedItems() == [grade_item]
    dialog.list_view_btn.click()
    assert dialog.list_widget.viewMode() == QListView.ViewMode.IconMode
    assert dialog.list_widget.selectedItems() == [grade_item]

    dialog._set_library_filter(str(Path(cut.path).parent))
    assert dialog.list_widget.findItems(delivery.name, Qt.MatchFlag.MatchExactly)[0].isHidden()
    dialog.favorites_only_btn.click()
    assert dialog.list_widget.findItems(cut.name, Qt.MatchFlag.MatchExactly)[0].isHidden()
    assert not grade_item.isHidden()
    dialog.search_box.setText("Cut")
    assert grade_item.isHidden()
    assert dialog.empty_label.isVisible()


def test_selected_project_gets_media_result_and_library(qtbot, project_browser, monkeypatch):
    from src.app.ui import project_manager_dialog as manager

    dialog, projects = project_browser
    _cut, _grade, delivery = projects
    monkeypatch.setattr(manager, "inspect_project_media", lambda _path: (1, ("lost.mov",)))
    dialog.list_widget.setCurrentRow(0)

    qtbot.waitUntil(lambda: "lost.mov" in dialog.selection_label.toolTip())
    assert delivery.name in dialog.selection_label.text()
    assert "External" in dialog.selection_label.text()
    assert Path(delivery.path).parent.as_posix() in dialog.selection_label.toolTip()


def test_backup_history_can_restore_a_copy_of_the_active_project(
    qtbot, project_browser, monkeypatch
):
    from src.app.ui import project_manager_dialog as manager

    parent, projects = project_browser
    cut, _grade, _delivery = projects
    original = Path(cut.path)
    backup = original.parent / "Backup.topos"
    with ZipFile(backup, "w") as archive:
        archive.writestr("manifest.json", '{"project_id": "original"}')
        archive.writestr("project.json", '{"assets": {}}')
    monkeypatch.setattr(
        manager, "list_project_backups",
        lambda *_args: [manager.ProjectBackupMeta(str(backup), cut.path, 1.0)],
    )
    history = manager._BackupHistoryDialog(cut.path, parent._config, True, parent)
    qtbot.addWidget(history)
    history.show()

    qtbot.waitUntil(history._copy.isEnabled)
    assert not history._replace.isEnabled()
    with qtbot.waitSignal(history.accepted, timeout=3000):
        history._copy.click()
    assert Path(history.restored_path).parent == original.parent
    assert Path(history.restored_path).is_file()
    assert original.is_file() and backup.is_file()


def test_media_result_for_previous_selection_is_ignored(qtbot, project_browser, monkeypatch):
    from src.app.ui import project_manager_dialog as manager

    dialog, projects = project_browser
    _cut, grade, delivery = projects
    started = Event()
    release = Event()

    def inspect(path):
        if path == delivery.path:
            started.set()
            release.wait(timeout=3)
            return 1, ("stale.mov",)
        return 1, ("current.mov",)

    monkeypatch.setattr(manager, "inspect_project_media", inspect)
    dialog.list_widget.setCurrentRow(0)
    try:
        qtbot.waitUntil(started.is_set)
        dialog.list_widget.setCurrentRow(1)
        assert grade.name in dialog.selection_label.text()
    finally:
        release.set()

    qtbot.waitUntil(lambda: "current.mov" in dialog.selection_label.toolTip())
    assert "stale.mov" not in dialog.selection_label.toolTip()
