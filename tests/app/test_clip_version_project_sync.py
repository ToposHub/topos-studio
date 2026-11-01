"""Real controller publication of version transactions and Color save routing."""
from dataclasses import dataclass, replace
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.app.controllers.file_controller import FileOpsMixin
from src.app.controllers.page_controller import PageManagementMixin
from src.features.color.core.color_session import ColorSession, ColorSessionManager
from src.features.edit.core.commands import ProjectCommandStack
from src.features.edit.core.project import EditProject, TimelineSequenceRef
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.project.color_stack import ColorStackData


def sequence(stack_id: str, revision: int = 3) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="tl", name="TL", fps=24.0, revision=revision,
        video_tracks=(EditTrack("v", "V1", "video", 0, clips=(
            ClipInstance("c", "asset", "v", 0, 24, 0, 24, color_stack_id=stack_id),)),))


def project() -> EditProject:
    return EditProject(project_id="p", name="P", active_timeline_id="tl",
                       timelines={"tl": TimelineSequenceRef(timeline_id="tl", name="TL")},
                       color_stacks={sid: ColorStackData(stack_id=sid) for sid in ("old", "new")})


@dataclass
class _Context:
    timeline_id: str = "tl"
    clip_id: str = "c"
    color_stack_id: str = "old"
    timeline_frame: int = 11


class _ColorHost(PageManagementMixin):
    def __init__(self) -> None:
        self._current_edit_project = project()
        self._current_timeline_sequences = {"tl": sequence("new")}
        self._color_session_manager = ColorSessionManager()
        self._color_session_manager.set_current_session(ColorSession.create_timeline_clip(
            "p", "asset", "c", "tl", "old", "/media.mov", timeline_frame=11, source_frame=11))
        self.context = _Context()
        self._page_context_manager = SimpleNamespace(
            get_current_context=lambda: self.context,
            set_current_context=lambda context: setattr(self, "context", context))
        self._color_timeline_clip_playback_context = {
            "timeline_id": "tl", "clip": sequence("old").get_clip_by_id("c"),
            "effective_source_start": 7}
        self.calls = []

    def _setup_grading_save_callback(self, stack_id: str) -> None:
        assert self._color_stack_hydration_in_progress
        self._grading_save_stack_id = stack_id
        self.calls.append(("bind", stack_id))

    def _load_color_stack_to_grading_panel(self, stack_id: str) -> None:
        assert self._color_stack_hydration_in_progress
        assert self._grading_save_stack_id == stack_id
        assert self._color_session_manager.get_current_session().color_stack_id == stack_id
        self.calls.append(("hydrate", stack_id))


def test_version_load_rebinds_save_session_context_and_playback_without_seeking() -> None:
    host = _ColorHost()
    original = host._color_session_manager.get_current_session()
    host._sync_active_color_version_stack("new")
    current = host._color_session_manager.get_current_session()
    assert current.color_stack_id == "new"
    assert current.session_id == original.session_id
    assert current.source_frame == original.source_frame
    assert host.context.color_stack_id == "new" and host.context.timeline_frame == 11
    assert host._color_timeline_clip_playback_context["clip"].color_stack_id == "new"
    assert host._color_timeline_clip_playback_context["effective_source_start"] == 7
    assert host.calls == [("bind", "new"), ("hydrate", "new")]
    assert not host._color_stack_hydration_in_progress


def test_other_project_or_noncurrent_binding_cannot_rebind_color_session() -> None:
    host = _ColorHost()
    host._sync_active_color_version_stack("old")
    assert not host.calls
    host._current_edit_project = replace(host._current_edit_project, project_id="another")
    host._sync_active_color_version_stack("new")
    assert not host.calls


def test_hydrate_error_restores_existing_guard() -> None:
    import pytest
    host = _ColorHost()
    host._load_color_stack_to_grading_panel = MagicMock(side_effect=RuntimeError("hydrate failed"))
    with pytest.raises(RuntimeError, match="hydrate failed"):
        host._sync_active_color_version_stack("new")
    assert not host._color_stack_hydration_in_progress


class _FileHost(FileOpsMixin):
    def __init__(self) -> None:
        self._current_edit_project = project()
        self._current_timeline_sequences = {"tl": sequence("old", revision=9)}
        self.stack = ProjectCommandStack()
        self.media_pool_panel = SimpleNamespace(get_command_stack=lambda: self.stack)
        self.timeline_panel = SimpleNamespace(set_project=MagicMock(), load_timeline=MagicMock())
        self.edit_page = SimpleNamespace(set_timeline=MagicMock())
        self.timeline_preview_renderer = SimpleNamespace(set_project=MagicMock(), set_sequence=MagicMock())
        self._current_edit_project_filepath = None
        self._clip_version_request_host = SimpleNamespace(on_project_changed=MagicMock())
        self.update_undo_redo_actions = MagicMock()
        self.setWindowTitle = MagicMock()


def test_project_publication_consumes_transaction_sequence_before_version_ui_refresh() -> None:
    host = _FileHost()
    old = host._current_edit_project
    new = replace(old, revision=4)
    host.stack.last_sequence_updates = {"tl": sequence("new", revision=3)}

    def assert_published(previous: EditProject, current: EditProject) -> None:
        assert previous is old and current is new
        assert host._current_timeline_sequences["tl"].get_clip_by_id("c").color_stack_id == "new"
        assert host._current_timeline_sequences["tl"].revision == 10
        assert not host.stack.last_sequence_updates
        host.edit_page.set_timeline.assert_called_once()
    host._clip_version_request_host.on_project_changed.side_effect = assert_published
    host._on_media_pool_project_changed(new)
    host._clip_version_request_host.on_project_changed.assert_called_once_with(old, new)


def test_sequence_publication_monotonic_on_undo_and_redo_and_consumed_only_once() -> None:
    host = _FileHost()
    for expected_revision, sid in [(10, "new"), (11, "old"), (12, "new")]:
        host.stack.last_sequence_updates = {"tl": sequence(sid, revision=3)}
        assert host._consume_project_sequence_updates(host._current_edit_project)
        assert host._current_timeline_sequences["tl"].revision == expected_revision
        assert not host._consume_project_sequence_updates(host._current_edit_project)


def test_binding_patch_publishes_without_overwriting_later_trim() -> None:
    from src.features.edit.core.clip_version_transaction import SequenceColorBindingUpdate
    host = _FileHost()
    seq = host._current_timeline_sequences["tl"]
    clip = replace(seq.get_clip_by_id("c"), timeline_duration=12, source_duration=12)
    seq = seq.with_track_updated(seq.video_tracks[0].with_clip_updated(clip))
    host._current_timeline_sequences["tl"] = seq
    host.stack.last_sequence_updates = {"tl": SequenceColorBindingUpdate(
        "tl", "c", "asset", "old", False, "new", True)}
    assert host._consume_project_sequence_updates(host._current_edit_project)
    published = host._current_timeline_sequences["tl"].get_clip_by_id("c")
    assert published.timeline_duration == 12 and published.source_duration == 12
    assert published.color_stack_id == "new" and published.color_shared


def test_transaction_preflight_rejects_binding_conflict_without_popping_history() -> None:
    import pytest
    from src.features.edit.core.clip_version_transaction import LoadClipVersionCommand
    from src.features.edit.core.color_versions import SaveColorVersionCommand
    host = _FileHost()
    p = SaveColorVersionCommand.create(host._current_edit_project, "new", "A").apply(host._current_edit_project)
    before = host._current_timeline_sequences["tl"]
    clip = before.get_clip_by_id("c").with_color_stack(color_stack_id="new", color_shared=True)
    after = before.with_track_updated(before.video_tracks[0].with_clip_updated(clip))
    cmd = LoadClipVersionCommand.create(before, after, "new", "A", project_id="p")
    host.stack.sequence_provider = lambda: host._current_timeline_sequences
    updated = host.stack.execute(cmd, p)
    host._consume_project_sequence_updates(updated)
    current = host._current_timeline_sequences["tl"]
    foreign = current.get_clip_by_id("c").with_color_stack(color_stack_id="other", color_shared=False)
    host._current_timeline_sequences["tl"] = current.with_track_updated(current.video_tracks[0].with_clip_updated(foreign))
    size = host.stack.get_history_size()
    with pytest.raises(ValueError, match="绑定"):
        host.stack.undo(updated)
    assert host.stack.get_history_size() == size
    assert not host.stack.can_redo()


def test_color_global_undo_and_menu_use_version_project_history(qtbot) -> None:
    from src.app.controllers.clip_version_host import ClipVersionRequestHost
    from src.app.controllers.layer_controller import LayerOpsMixin
    from src.features.edit.core.color_versions import SaveColorVersionCommand, versions_state
    host = _FileHost()
    host.current_page = "Color"
    host._color_session_manager = ColorSessionManager()
    host._color_session_manager.set_current_session(ColorSession.create_timeline_clip(
        "p", "asset", "c", "tl", "old", "/media.mov"))
    host.color_timeline_clip_strip = None
    host.engine = SimpleNamespace(can_undo=lambda: False, can_redo=lambda: False,
                                  undo=MagicMock(), redo=MagicMock())
    host.canvas = SimpleNamespace(sam_mode_active=False)
    host.undo_action = MagicMock()
    host.redo_action = MagicMock()
    host._clip_version_request_host = ClipVersionRequestHost(host)
    host._clip_version_request_host._remember_version_history()
    updated = host.stack.execute(SaveColorVersionCommand.create(
        host._current_edit_project, "old", "A"), host._current_edit_project)
    host._on_media_pool_project_changed(updated)
    FileOpsMixin.update_undo_redo_actions(host)
    host.undo_action.setEnabled.assert_called_with(True)
    LayerOpsMixin.undo(host)
    assert versions_state(host._current_edit_project, "old")["versions"] == {}
    host.engine.undo.assert_not_called()
    FileOpsMixin.update_undo_redo_actions(host)
    host.redo_action.setEnabled.assert_called_with(True)
    LayerOpsMixin.redo(host)
    assert "A" in versions_state(host._current_edit_project, "old")["versions"]
    host.engine.redo.assert_not_called()


def test_new_engine_history_keeps_priority_over_older_version_command() -> None:
    from src.app.controllers.clip_version_host import ClipVersionRequestHost
    from src.features.color.core.history import HistoryManager
    from src.features.edit.core.color_versions import SaveColorVersionCommand
    host = _FileHost()
    host.current_page = "Color"
    host.engine = SimpleNamespace(history=HistoryManager())
    host._color_session_manager = ColorSessionManager()
    host._color_session_manager.set_current_session(ColorSession.create_timeline_clip(
        "p", "asset", "c", "tl", "old", "/media.mov"))
    versions = ClipVersionRequestHost(host)
    host._current_edit_project = host.stack.execute(SaveColorVersionCommand.create(
        host._current_edit_project, "old", "A"), host._current_edit_project)
    assert versions.history_target() is None  # An unrelated project history is not enough.
    versions._remember_version_history()
    assert versions.history_target() is versions
    original = {"layers": []}
    host.engine.history.undo_stack.append(original)  # New layer, not yet autosaved.
    assert versions.history_target() is None
    host.engine.history.undo_stack.pop()
    host.engine.history.redo_stack.append({"layers": ["new"]})
    assert versions.history_target() is versions  # The newer engine edit has been undone.
    host.engine.history.undo_stack.append({"layers": []})
    assert versions.history_target() is None  # Same depth can contain a different snapshot.


def test_new_engine_edit_after_version_undo_cannot_redo_old_version_over_it() -> None:
    from src.app.controllers.clip_version_host import ClipVersionRequestHost
    from src.features.color.core.history import HistoryManager
    from src.features.edit.core.color_versions import SaveColorVersionCommand
    host = _FileHost()
    host.current_page = "Color"
    host.engine = SimpleNamespace(history=HistoryManager())
    host._color_session_manager = ColorSessionManager()
    host._color_session_manager.set_current_session(ColorSession.create_timeline_clip(
        "p", "asset", "c", "tl", "old", "/media.mov"))
    versions = ClipVersionRequestHost(host)
    saved = host.stack.execute(SaveColorVersionCommand.create(
        host._current_edit_project, "old", "A"), host._current_edit_project)
    host._current_edit_project = host.stack.undo(saved)
    versions._remember_version_history()
    assert versions.history_target(redo=True) is versions
    host.engine.history.undo_stack.append({"layers": ["new layer"]})
    assert not host.engine.history.redo_stack
    assert versions.history_target(redo=True) is None


def test_redo_newer_engine_edit_uses_engine_instead_of_version_history() -> None:
    from src.app.controllers.layer_controller import LayerOpsMixin
    owner = SimpleNamespace(
        _clip_version_request_host=SimpleNamespace(history_target=lambda **kwargs: None),
        canvas=SimpleNamespace(sam_mode_active=False),
        _edit_undo_redo_target=lambda **kwargs: None,
        _composite_available=False,
        engine=SimpleNamespace(redo=MagicMock(return_value=True)),
        _sync_ui_after_undo_redo=MagicMock())
    LayerOpsMixin.redo(owner)
    owner.engine.redo.assert_called_once()
    owner._sync_ui_after_undo_redo.assert_called_once()
