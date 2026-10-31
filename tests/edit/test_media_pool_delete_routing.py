"""Regression tests for media-pool delete routing and shortcut scope."""

from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QMainWindow, QWidget

from src.app.controllers.menu_toolbar_controller import MenuToolbarMixin
from src.app.controllers.file_controller import FileOpsMixin
from src.features.edit.core.commands import DeleteAssetCommand
from src.features.edit.core.composite_graph_data import CompositeGraphData
from src.features.edit.core.project import EditProject, SmartBin
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.media_pool_panel import MediaPoolPanel
from src.shared.ui.shortcut_manager import ShortcutManager


def test_timeline_delete_actions_are_scoped_to_timeline_widget(qapp: Any) -> None:
    """Timeline Delete actions must not compete with media-pool shortcuts."""

    class _Host(QMainWindow, MenuToolbarMixin):
        pass

    host = _Host()
    timeline_view = QWidget(host)
    host.timeline_panel = SimpleNamespace(timeline_view=timeline_view)
    delete_action = QAction("Delete", host)
    ripple_delete_action = QAction("Ripple Delete", host)
    host._module_actions = {
        "edit.timeline.delete": delete_action,
        "edit.timeline.ripple_delete": ripple_delete_action,
    }

    host._scope_timeline_delete_shortcuts()

    for action in (delete_action, ripple_delete_action):
        assert action.property("force_widget_shortcut") is True
        assert action.shortcutContext() == Qt.ShortcutContext.WidgetShortcut
        assert action in timeline_view.actions()

    host.close()


def test_shortcut_manager_preserves_widget_scope_override(qapp: Any) -> None:
    action = QAction()
    action.setProperty("force_widget_shortcut", True)

    assert (
        ShortcutManager._shortcut_context(action, global_mode=True)
        == Qt.ShortcutContext.WidgetShortcut
    )


def test_single_asset_delete_uses_project_command_stack(qapp: Any) -> None:
    project = EditProject.create("Single delete command")
    project = project.import_asset(
        path="/tmp/single-delete-command.mp4",
        media_type="video",
        metadata={},
        display_name="single-delete-command.mp4",
    )
    asset_id = next(iter(project.assets))

    panel = MediaPoolPanel()
    panel.set_project(project)
    assert panel._do_remove_asset(asset_id) is True
    assert panel._project is not None
    assert asset_id not in panel._project.assets
    assert panel._command_stack.can_undo()

    panel.close()


def test_context_asset_delete_keeps_source_file(qapp: Any, tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"keep this file")
    project = EditProject.create("Safe context delete")
    project = project.import_asset(
        path=str(source),
        media_type="video",
        metadata={},
        display_name=source.name,
    )
    asset_id = next(iter(project.assets))

    panel = MediaPoolPanel()
    panel.set_project(project)
    panel._delete_asset(asset_id)

    assert panel._project is not None
    assert asset_id not in panel._project.assets
    assert source.exists()
    panel.close()


def test_context_bin_delete_keeps_assets_and_uses_one_action(qapp: Any) -> None:
    project = EditProject.create("Safe folder delete")
    project = project.import_asset(
        path="/tmp/folder-delete-source.mp4",
        media_type="video",
        metadata={},
        display_name="folder-delete-source.mp4",
    )
    project = project.create_bin("Folder")
    bin_id = next(bin_id for bin_id in project.bins if bin_id != project.root_bin_id)
    asset_id = next(iter(project.assets))
    project = project.move_asset_to_bin(asset_id, bin_id)

    panel = MediaPoolPanel()
    panel.set_project(project)
    panel._delete_bin(bin_id)

    assert panel._project is not None
    assert bin_id not in panel._project.bins
    assert asset_id in panel._project.assets
    assert panel._command_stack.can_undo()
    panel.close()


def test_context_timeline_delete_uses_media_pool_command(qapp: Any) -> None:
    project = EditProject.create("Safe timeline delete")
    project = project.create_timeline("Timeline")
    timeline_id = next(iter(project.timelines))

    panel = MediaPoolPanel()
    panel.set_project(project)
    assert panel._delete_selected_pool_items(
        selected_items=[("timeline", timeline_id)]
    ) is True

    assert panel._project is not None
    assert timeline_id not in panel._project.timelines
    assert panel._command_stack.can_undo()
    panel.close()


def test_context_smart_bin_delete_is_immediate(qapp: Any) -> None:
    project = EditProject.create("Safe smart bin delete")
    smart_bin = SmartBin(
        bin_id="smart-bin-test",
        name="Smart Bin",
        filter_type="custom",
        filter_params={},
    )
    project = project.add_smart_bin(smart_bin)

    panel = MediaPoolPanel()
    panel.set_project(project)
    panel._on_delete_smart_bin(smart_bin.bin_id)

    assert panel._project is not None
    assert smart_bin.bin_id not in panel._project.smart_bins
    assert panel._command_stack.can_undo()
    panel.close()


def test_deleted_asset_clips_are_removed_from_cached_timeline_and_restored() -> None:
    """Project asset deletion must not leave renderer-visible dangling clips."""
    project = EditProject.create("Asset/timeline sync")
    project = project.import_asset(
        path="/tmp/asset-timeline-sync.mp4",
        media_type="video",
        metadata={},
        display_name="asset-timeline-sync.mp4",
    )
    asset_id = next(iter(project.assets))
    project = project.create_timeline("Timeline")
    timeline_id = next(iter(project.timelines))
    clip = ClipInstance(
        clip_id="clip-using-deleted-asset",
        asset_id=asset_id,
        track_id="video-1",
        timeline_duration=24,
        source_duration=24,
    )
    sequence = TimelineSequence(
        timeline_id=timeline_id,
        video_tracks=(EditTrack(track_id="video-1", clips=(clip,)),),
    )

    host = FileOpsMixin()
    host._current_timeline_sequences = {timeline_id: sequence}
    deleted_project = project.remove_asset(asset_id)

    assert host._sync_asset_references_after_project_change(
        project, deleted_project
    ) is True
    assert (
        host._current_timeline_sequences[timeline_id].get_clip_by_id(clip.clip_id)
        is None
    )

    # The same project-level undo transition restores the full pre-delete
    # sequence, including the clip's original timing and identity.
    assert host._sync_asset_references_after_project_change(
        deleted_project, project
    ) is True
    restored = host._current_timeline_sequences[timeline_id].get_clip_by_id(
        clip.clip_id
    )
    assert restored == clip


def test_batch_asset_delete_restores_one_shared_timeline_snapshot() -> None:
    """One batch delete must restore a shared sequence only once on undo."""
    project = EditProject.create("Batch asset/timeline sync")
    project = project.import_asset(
        path="/tmp/batch-sync-a.mp4",
        media_type="video",
        metadata={},
        display_name="batch-sync-a.mp4",
    )
    project = project.import_asset(
        path="/tmp/batch-sync-b.mp4",
        media_type="video",
        metadata={},
        display_name="batch-sync-b.mp4",
    )
    asset_a, asset_b = tuple(project.assets)
    project = project.create_timeline("Timeline")
    timeline_id = next(iter(project.timelines))
    track_id = "video-1"
    sequence = TimelineSequence(
        timeline_id=timeline_id,
        video_tracks=(
            EditTrack(
                track_id=track_id,
                clips=(
                    ClipInstance(
                        clip_id="batch-clip-a",
                        asset_id=asset_a,
                        track_id=track_id,
                        timeline_duration=24,
                        source_duration=24,
                    ),
                    ClipInstance(
                        clip_id="batch-clip-b",
                        asset_id=asset_b,
                        track_id=track_id,
                        timeline_start=24,
                        timeline_duration=24,
                        source_duration=24,
                    ),
                ),
            ),
        ),
    )
    host = FileOpsMixin()
    host._current_timeline_sequences = {timeline_id: sequence}
    deleted_project = project.remove_asset(asset_a).remove_asset(asset_b)

    assert host._sync_asset_references_after_project_change(
        project, deleted_project
    ) is True
    assert host._sync_asset_references_after_project_change(
        deleted_project, project
    ) is True
    restored_sequence = host._current_timeline_sequences[timeline_id]
    assert {clip.clip_id for clip in restored_sequence.all_tracks[0].clips} == {
        "batch-clip-a",
        "batch-clip-b",
    }


def test_deleted_asset_is_detached_from_composite_graph_and_undo_restores() -> None:
    """Deleting a media-pool asset must not leave Composite asset refs behind."""
    source_path = "/tmp/composite-delete-source.mp4"
    project = EditProject.create("Composite asset sync")
    project = project.import_asset(
        path=source_path,
        media_type="video",
        metadata={},
        display_name="composite-delete-source.mp4",
    )
    asset_id = next(iter(project.assets))
    graph = {
        "composite_id": "graph-asset-delete",
        "source_clip_id": "clip-1",
        "source_asset_id": asset_id,
        "source_asset_path": source_path,
        "source_kind": "timeline_clip_upgrade",
        "source_refs": {
            "original_asset_id": asset_id,
            "original_clip_id": "clip-1",
        },
        "nodes": [
            {
                "id": "image",
                "type": "image_input",
                "parameters": {
                    "file_path": source_path,
                    "asset_id": asset_id,
                },
            },
            {
                "id": "timeline",
                "type": "timeline_clip_input",
                "parameters": {
                    "timeline_id": "timeline-1",
                    "clip_id": "clip-1",
                    "asset_id": asset_id,
                },
            },
        ],
        "connections": [],
    }
    project = replace(project, compositions={"graph-asset-delete": graph})

    command = DeleteAssetCommand.create(asset_id)
    deleted = command.apply(project)
    detached = deleted.compositions["graph-asset-delete"]

    assert asset_id not in deleted.assets
    assert detached["source_kind"] == "detached_asset"
    assert detached["source_asset_id"] == ""
    assert detached["source_asset_path"] == source_path
    assert detached["source_refs"]["original_asset_id"] == ""
    assert detached["nodes"][0]["parameters"]["file_path"] == source_path
    assert detached["nodes"][0]["parameters"]["asset_id"] == ""
    assert detached["nodes"][1]["parameters"]["asset_id"] == ""
    assert detached["nodes"][1]["parameters"]["timeline_id"] == ""
    assert detached["nodes"][1]["parameters"]["clip_id"] == ""

    # The detached graph remains loadable after its source asset is gone.
    loaded = CompositeGraphData.from_dict(detached)
    assert loaded.source_kind == "detached_asset"
    assert loaded.source_asset_id == ""

    restored = command.undo(deleted)
    assert restored.assets == project.assets
    assert restored.compositions == project.compositions


def test_comp_clip_asset_delete_removes_graph_and_undo_restores_all_records() -> None:
    """A Comp Clip delete must remove asset, clip record, and graph together."""
    project = EditProject.create("Comp Clip delete sync")
    project, comp_clip_id, asset_id, graph_id = project.create_empty_comp_clip(
        name="Comp Clip to delete"
    )
    command = DeleteAssetCommand.create(asset_id)

    deleted = command.apply(project)
    assert asset_id not in deleted.assets
    assert comp_clip_id not in deleted.comp_clips
    assert graph_id not in deleted.compositions
    assert deleted.active_comp_clip_id is None

    restored = command.undo(deleted)
    assert restored.assets == project.assets
    assert restored.comp_clips == project.comp_clips
    assert restored.compositions == project.compositions
    assert restored.active_comp_clip_id == project.active_comp_clip_id


def test_active_composite_page_reloads_sanitized_graph_after_asset_delete() -> None:
    """The live Composite page must receive the project-side graph cleanup."""
    project = EditProject.create("Active Composite refresh")
    project = project.import_asset(
        path="/tmp/active-composite-source.png",
        media_type="image",
        metadata={},
        display_name="active-composite-source.png",
    )
    asset_id = next(iter(project.assets))
    graph = {
        "composite_id": "active-graph",
        "source_asset_id": asset_id,
        "source_asset_path": "/tmp/active-composite-source.png",
        "source_clip_id": "clip-1",
        "nodes": [{
            "id": "image",
            "type": "image_input",
            "parameters": {"file_path": "/tmp/active-composite-source.png", "asset_id": asset_id},
        }],
        "connections": [],
    }
    project = replace(project, compositions={"active-graph": graph})
    deleted = DeleteAssetCommand.create(asset_id).apply(project)

    class _Page:
        _current_composite_id = "active-graph"

        def __init__(self) -> None:
            self.project = None
            self.loaded = None

        def set_edit_project(self, value: Any) -> None:
            self.project = value

        def load_composite_data(self, value: dict) -> None:
            self.loaded = value

    host = FileOpsMixin()
    host.composite_page = _Page()
    host._current_composite_id = "active-graph"
    host._sync_active_composite_after_project_change(project, deleted)

    assert host.composite_page.project is deleted
    assert host.composite_page.loaded == deleted.compositions["active-graph"]
