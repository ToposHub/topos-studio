"""Startup Project Manager fallback tests."""

from __future__ import annotations

from types import SimpleNamespace

from PyQt6.QtWidgets import QDialog

from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin


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


def test_closing_startup_manager_creates_unsaved_untitled_project(monkeypatch):
    import src.app.ui.project_manager_dialog as project_manager_module

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
            return QDialog.DialogCode.Rejected

    monkeypatch.setattr(project_manager_module, "ProjectManagerDialog", _Dialog)
    monkeypatch.setattr(
        MenuToolbarMixin,
        "_next_untitled_project_name",
        lambda _self: "Untitled Project 4",
    )

    class _Host(MenuToolbarMixin):
        def __init__(self):
            self.config_manager = object()
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
    host._show_project_manager(startup=True)

    assert host.created_name == "Untitled Project 4"
    assert host._current_edit_project.name == "Untitled Project 4"
