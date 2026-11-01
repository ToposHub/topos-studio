"""Real File publication and preview methods, with GPU work replaced by counters."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

from PyQt6.QtCore import QMutex

from src.app.controllers.clip_version_host import ClipVersionRequestHost
from src.app.controllers.file_controller import FileOpsMixin
from src.features.edit.core.color_versions import (
    ActivateColorVersionCommand, DuplicateColorVersionCommand,
    RenameColorVersionCommand, SaveColorVersionCommand,
)
from src.features.edit.core.commands import ProjectCommandStack
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_preview_renderer import TimelinePreviewRenderer
from src.shared.project.color_stack import ColorStackData


class _Preview:
    """Exercise the real publication APIs without constructing a GPU renderer."""

    set_project = TimelinePreviewRenderer.set_project
    update_project_metadata = TimelinePreviewRenderer.update_project_metadata
    update_color_stacks = TimelinePreviewRenderer.update_color_stacks

    def __init__(self, project: EditProject) -> None:
        self._project = project
        self._worker = SimpleNamespace(
            invalidate_composite_cache=MagicMock(), set_generation_id=MagicMock())
        self._sequence = None
        self._pending_mutex = QMutex()
        self._pending_frame = "pending"
        self._queued_frame = "queued"
        self._pending_layer_count = 2
        self._generation_id = 9
        self._dispatched_generation_id = 9
        self._render_in_flight = True
        self._render_plan = "warm-plan"
        self._clear_offline_media_diagnostics = MagicMock()
        self._sync_gpu_pipeline_generation = MagicMock()
        self._invalidate_preview_frame_caches = MagicMock()
        self._clear_placeholder_requests_for_frame = MagicMock()
        self._clear_prebuffer_state = MagicMock()
        self._clear_stale_pending_present_frames = MagicMock()
        self._reset_emitted_frame_guard = MagicMock()
        self._prewarm_zero_copy_capability = MagicMock()
        self._prewarm_sequence_decode_sources = MagicMock()
        self._schedule_current_frame_warmup = MagicMock()


class _FileHost(FileOpsMixin):
    def __init__(self, project: EditProject) -> None:
        self._current_edit_project = project
        self._current_timeline_sequences = {}
        self.stack = ProjectCommandStack()
        self.media_pool_panel = SimpleNamespace(get_command_stack=lambda: self.stack)
        self.timeline_panel = SimpleNamespace(set_project=MagicMock())
        self.timeline_preview_renderer = _Preview(project)
        self._current_edit_project_filepath = None
        self._clip_version_request_host = ClipVersionRequestHost(self)
        self.color_timeline_clip_strip = None
        self.update_undo_redo_actions = MagicMock()
        self.setWindowTitle = MagicMock()


def _project() -> EditProject:
    project = EditProject(project_id="p", name="P", color_stacks={
        "s": ColorStackData(stack_id="s", grading_params={"exposure": 1.0})})
    project = SaveColorVersionCommand.create(project, "s", "A").apply(project)
    project = project.with_color_stacks({"s": replace(
        project.color_stacks["s"], grading_params={"exposure": 3.0})})
    return SaveColorVersionCommand.create(project, "s", "B").apply(project)


def test_rename_duplicate_undo_redo_keep_preview_warm_and_publish_authoritative_project() -> None:
    host = _FileHost(_project())
    renderer = host.timeline_preview_renderer
    initial_generation = renderer._generation_id
    before = host._current_edit_project
    renamed = host.stack.execute(RenameColorVersionCommand.create(
        before, "s", "A", "Warm"), before)
    host._on_media_pool_project_changed(renamed)
    duplicated = host.stack.execute(DuplicateColorVersionCommand.create(
        renamed, "s", "Warm", "Copy"), renamed)
    host._on_media_pool_project_changed(duplicated)
    undone = host.stack.undo(duplicated)
    host._on_media_pool_project_changed(undone)
    redone = host.stack.redo(undone)
    host._on_media_pool_project_changed(redone)

    assert renderer._project is redone
    assert renderer._worker._asset_resolver is renderer._asset_resolver
    assert renderer._generation_id == initial_generation
    assert renderer._pending_frame == "pending" and renderer._queued_frame == "queued"
    assert renderer._render_plan == "warm-plan" and renderer._render_in_flight
    renderer._invalidate_preview_frame_caches.assert_not_called()
    renderer._clear_prebuffer_state.assert_not_called()
    renderer._prewarm_sequence_decode_sources.assert_not_called()
    renderer._schedule_current_frame_warmup.assert_not_called()


def test_version_load_invalidates_frames_once_without_cold_decode_restart() -> None:
    host = _FileHost(_project())
    renderer = host.timeline_preview_renderer
    loaded = host.stack.execute(ActivateColorVersionCommand.create("s", "A"),
                                host._current_edit_project)
    host._on_media_pool_project_changed(loaded)
    # File publication and the real version host both publish the same snapshot.
    assert renderer._project is loaded
    assert renderer._generation_id == 10
    renderer._invalidate_preview_frame_caches.assert_called_once()
    renderer._worker.set_generation_id.assert_called_once_with(10)
    renderer._clear_prebuffer_state.assert_not_called()
    renderer._prewarm_sequence_decode_sources.assert_not_called()
    renderer._schedule_current_frame_warmup.assert_not_called()


def test_media_source_change_still_runs_full_preview_rebind() -> None:
    old = _project()
    host = _FileHost(old)
    new = replace(old, assets={"media": MediaAsset("media", "/media/new.mov", "video", "New", {})})
    host._on_media_pool_project_changed(new)
    renderer = host.timeline_preview_renderer
    assert renderer._project is new
    renderer._clear_prebuffer_state.assert_called_once()
    renderer._prewarm_sequence_decode_sources.assert_called_once()
    renderer._schedule_current_frame_warmup.assert_called_once()


def test_replaced_asset_with_same_id_is_not_misclassified_as_version_metadata() -> None:
    asset = MediaAsset("media", "/media/old.mov", "video", "Old", {})
    old = replace(_project(), assets={"media": asset})
    host = _FileHost(old)
    changed = replace(asset, path="/media/relinked.mov")
    assert changed == asset  # Domain equality is identity-based.
    new = replace(old, assets={"media": changed})
    host._on_media_pool_project_changed(new)
    host.timeline_preview_renderer._clear_prebuffer_state.assert_called_once()
