"""Regression tests for project-page routing and timeline save snapshots."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock


def test_opening_edit_project_switches_from_color_to_edit(tmp_path) -> None:
    """An edit project must not inherit the previously active Color page."""
    from src.app.controllers.file_controller import FileOpsMixin

    project_path = tmp_path / "timeline.topos"
    project_path.write_bytes(b"placeholder")

    class Host:
        current_page = "Color"
        _current_edit_project = object()
        switched_to = None

        def _check_project_matches_work_mode(self, _filepath: str) -> bool:
            return True

        def close_project(self) -> bool:
            self._current_edit_project = None
            return True

        def _read_project_mode_from_manifest(self, _filepath: str) -> str:
            return "edit_project"

        def _load_edit_project(self, _filepath: str) -> None:
            self._current_edit_project = object()

        def switch_to_page(self, page_name: str) -> None:
            self.switched_to = page_name
            self.current_page = page_name

        def _sync_mg_preset_project_root(self, _filepath) -> None:
            pass  # stubbed: the preset library's project root is out of scope here

    host = Host()

    assert FileOpsMixin._open_project_path(host, str(project_path))
    assert host.switched_to == "Edit"
    assert host.current_page == "Edit"


def test_decoded_photo_project_handoff_preserves_the_image_request(tmp_path) -> None:
    from src.app.controllers.file_controller import FileOpsMixin

    project_path = tmp_path / "photo.topos"
    project_path.touch()
    photo = object()
    loaded_project = object()
    host = SimpleNamespace(
        _current_edit_project=object(),
        _check_project_matches_work_mode=MagicMock(return_value=True),
        _read_project_mode_from_manifest=MagicMock(return_value="standalone_image"),
        _sync_mg_preset_project_root=MagicMock(),
    )

    def close_project(**_options: object) -> bool:
        host._current_edit_project = None
        return True

    def load_project(_path: str, **_options: object) -> None:
        host._current_edit_project = loaded_project

    host.close_project = MagicMock(side_effect=close_project)
    host._load_standalone_project = MagicMock(side_effect=load_project)
    assert FileOpsMixin._open_project_path(host, str(project_path), decoded_photo=photo)
    host.close_project.assert_called_once_with(cancel_grade_set_request=False)
    host._load_standalone_project.assert_called_once_with(str(project_path), decoded_photo=photo)


def test_timeline_save_snapshot_includes_active_editor_timeline() -> None:
    """Ctrl+S must see a just-committed split even if cache sync is pending."""
    from src.app.controllers.file_controller import FileOpsMixin

    cached_timeline = SimpleNamespace(timeline_id="timeline-1", revision="old")
    active_timeline = SimpleNamespace(timeline_id="timeline-1", revision="split")

    class Host:
        _current_timeline_sequences = {
            "timeline-1": cached_timeline,
            "timeline-2": SimpleNamespace(timeline_id="timeline-2"),
        }
        edit_page = SimpleNamespace(_current_timeline=active_timeline)

    host = Host()
    snapshots = FileOpsMixin._get_timeline_sequences(host)

    assert snapshots["timeline-1"] is active_timeline
    assert "timeline-2" in snapshots
    assert host._current_timeline_sequences["timeline-1"] is cached_timeline


def test_close_persists_project_even_without_batch_queue_failure() -> None:
    """Close-time saving must not be nested under an unrelated except block."""
    from src.app.controllers.file_controller import FileOpsMixin

    host = SimpleNamespace(
        _flush_composite_to_edit_before_leave=MagicMock(),
        save_router=MagicMock(),
    )

    FileOpsMixin._persist_active_project_before_close(host)

    host._flush_composite_to_edit_before_leave.assert_called_once_with()
    host.save_router.save.assert_called_once_with()


def test_mixer_saves_through_the_edit_project_route() -> None:
    from src.app.controllers.file_controller import FileOpsMixin

    host = MagicMock(current_page="Mixer")
    FileOpsMixin._setup_save_router_callbacks(host)
    callbacks = host.save_router.set_callbacks.call_args.args[0]
    assert callbacks["get_current_page"]() == "Edit"
    host.current_page = "Color"
    assert callbacks["get_current_page"]() == "Color"


def test_main_window_close_event_runs_mixin_shutdown(monkeypatch) -> None:
    """MainWindow.closeEvent must not shadow FileOpsMixin.closeEvent.

    QMainWindow precedes the controller mixins in MainWindow's MRO, so a
    plain ``super().closeEvent()`` jumps straight to QWidget and silently
    skips the mixin's shutdown — losing the save prompt, config
    persistence, and ``edit_project_io.cleanup()`` (which removes the
    recovery ``.autosave`` file; skipping it re-prompts recovery on every
    launch). This pins the explicit delegation.
    """
    from src.app.main_window import MainWindow
    from src.app.controllers.file_controller import FileOpsMixin

    invoked: list[str] = []
    monkeypatch.setattr(
        FileOpsMixin, "closeEvent", lambda self, event: invoked.append("mixin")
    )

    host = SimpleNamespace(
        _shutdown_playback_pipeline=lambda: invoked.append("pipeline")
    )

    MainWindow.closeEvent(host, SimpleNamespace())

    assert invoked == ["pipeline", "mixin"]


def test_canceling_window_close_keeps_the_mixer_recording_active() -> None:
    from src.app.main_window import MainWindow

    host = SimpleNamespace(
        _mixer_timeline_recorders={"audio-track": object()},
        _confirm_save_before_close=lambda: False,
        _finish_mixer_timeline_recordings=MagicMock(),
    )
    event = SimpleNamespace(ignore=MagicMock())
    MainWindow.closeEvent(host, event)
    event.ignore.assert_called_once_with()
    host._finish_mixer_timeline_recordings.assert_not_called()


def test_closing_project_discards_its_recovery_file_and_detaches_autosave() -> None:
    """Discarding a project must not leave a recovery prompt for it behind."""
    from src.app.controllers.edit_controller import EditControllerMixin

    project_path = "/tmp/closing-project.topos"
    project_io = MagicMock()
    host = SimpleNamespace(
        _current_edit_project=object(),
        _current_edit_project_filepath=project_path,
        _current_timeline_sequences={"timeline": object()},
        edit_project_io=project_io,
        _has_unsaved_changes=lambda: False,
        unload_video=MagicMock(),
        _teardown_ai_editing=MagicMock(),
        _sync_mg_preset_project_root=MagicMock(),
        save_router=MagicMock(),
        media_pool_panel=SimpleNamespace(clear_project=MagicMock()),
        timeline_panel=SimpleNamespace(clear=MagicMock()),
        edit_page=SimpleNamespace(set_timeline=MagicMock()),
        _clear_project_audio_clip_editor=MagicMock(),
        _sync_audio_mixer_from_current_timeline=MagicMock(),
        setWindowTitle=MagicMock(),
        status_bar=SimpleNamespace(showMessage=MagicMock()),
    )

    assert EditControllerMixin._close_current_project(host)

    project_io.discard_autosave.assert_called_once_with(project_path)
    project_io.set_current_filepath.assert_called_once_with(None)
    assert host._current_edit_project is None
    assert host._current_edit_project_filepath is None
    host.edit_page.set_timeline.assert_called_once_with(None)
    host._clear_project_audio_clip_editor.assert_called_once_with()
    host._sync_audio_mixer_from_current_timeline.assert_called_once_with()
