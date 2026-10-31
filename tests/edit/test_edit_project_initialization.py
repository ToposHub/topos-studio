"""Regression tests for project references needed by timeline drag-and-drop."""

from __future__ import annotations


class _ProjectTarget:
    def __init__(self) -> None:
        self.project = None

    def set_project(self, project) -> None:
        self.project = project


class _TimelinePanel(_ProjectTarget):
    def __init__(self) -> None:
        super().__init__()
        self.sequence = None

    def set_timeline_sequence(self, sequence) -> None:
        self.sequence = sequence


class _AutoProjectHost:
    """Minimal FileOpsMixin host without GUI or renderer dependencies."""

    def __init__(self) -> None:
        self.media_pool_panel = _ProjectTarget()
        self.timeline_panel = _TimelinePanel()
        self._current_edit_project = None
        self._current_edit_project_filepath = "previous.topos"
        self._current_timeline_sequences = {}
        self.title = ""
        self.renderer_initialized = False

    def setWindowTitle(self, title: str) -> None:
        self.title = title

    def _init_timeline_preview_renderer(self) -> None:
        self.renderer_initialized = True

    def _setup_ai_editing(self) -> None:
        """Stub: AI subsystem re-binding is a MainWindow-only concern."""
        return None


def test_auto_created_project_is_assigned_to_timeline_drag_target() -> None:
    """A freshly auto-created project is bound to the timeline drag target.

    No default timeline is pre-created (it is created lazily on the first
    media drop). The renderer init still runs but skips because there is
    no active timeline yet.
    """
    from src.app.controllers.file_controller import FileOpsMixin

    host = _AutoProjectHost()
    FileOpsMixin._auto_create_edit_project(host, "Imported Media")

    assert host._current_edit_project is not None
    assert host.media_pool_panel.project is host._current_edit_project
    assert host.timeline_panel.project is host._current_edit_project
    # No default timeline is pre-created — it is built lazily on first drop.
    assert host.timeline_panel.sequence is None
    assert host._current_edit_project.active_timeline_id is None
    assert host._current_timeline_sequences == {}
