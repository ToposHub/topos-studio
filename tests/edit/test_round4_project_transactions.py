"""Structural edits share clip history without restoring whole-project snapshots."""
from __future__ import annotations

from dataclasses import replace
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.commands import MacroCommand, UpdateClipParamCommand
from src.features.edit.core.nested_sequence import build_nested_from_selection
from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
from src.features.edit.core.timeline_project_transaction import TimelineProjectTransactionCommand
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_view import TimelineView


class _Host(EditControllerMixin):
    def _schedule_auto_render_cache(self) -> None:
        pass

    def _schedule_audio_mixer_sync(self, delay_ms: int = 100) -> None:
        pass

    def _invalidate_motion_graphic_caches_for_timeline_change(self, before: Any, after: Any) -> None:
        pass


def _fixture(qtbot: Any) -> tuple[Any, Any, Any, Any]:
    clip = ClipInstance(clip_id="c", asset_id="a", track_id="v", timeline_start=0,
                        timeline_duration=100, source_start=0, source_duration=100)
    before = TimelineSequence(timeline_id="parent", name="Parent", revision=3,
        video_tracks=(EditTrack(track_id="v", name="V", track_type="video", index=0,
                                clips=(clip,)),))
    project = EditProject(project_id="p", name="P", active_timeline_id="parent", revision=7,
        assets={"a": MediaAsset(asset_id="a", path="/missing/a.mp4", media_type="video",
                                 display_name="A", metadata={"future": {"keep": True}})},
        timelines={"parent": TimelineSequenceRef(timeline_id="parent", name="Parent")})
    txn = build_nested_from_selection(before, ("c",))
    child = txn.child_timeline
    after_project = project.with_media_asset_added(txn.nested_asset).with_timeline_ref_added(
        TimelineSequenceRef(timeline_id=child.timeline_id, name=child.name), set_active=False)
    command = TimelineProjectTransactionCommand.create(before, txn.new_timeline,
        before_project=project, after_project=after_project,
        after_child_sequences={child.timeline_id: child}, label="Nest")
    view = TimelineView()
    qtbot.addWidget(view)
    view.set_project(project)
    view.load_timeline(before)
    host = _Host()
    host.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
    host.edit_page = SimpleNamespace(_current_timeline=before)
    host.edit_page.set_timeline = lambda value: setattr(host.edit_page, "_current_timeline", value)
    host._current_edit_project = project
    host._current_timeline_sequences = {before.timeline_id: before}
    host.timeline_preview_renderer = None
    host.edit_properties_panel = None
    host.edit_info_panel = None
    host.media_pool_panel = MagicMock()
    view.timeline_changed.connect(host._on_edit_timeline_changed)
    return host, view, command, txn


def test_nested_transaction_edit_undo_redo_publishes_one_revision_and_stable_ids(qtbot: Any) -> None:
    host, view, command, txn = _fixture(qtbot)
    child_id, asset_id = txn.child_timeline.timeline_id, txn.nested_asset.asset_id
    host._execute_timeline_edit_command(command, view._timeline)
    assert host._current_edit_project.revision == 8
    assert view._timeline.revision == 4
    assert child_id in host._current_timeline_sequences
    assert child_id in host._current_edit_project.timelines
    assert asset_id in host._current_edit_project.assets
    nested_clip = view._timeline.video_tracks[0].clips[0]
    view.execute_command(UpdateClipParamCommand(command_id="x", label="X", track_id="v",
        clip_id=nested_clip.clip_id, param_type="transform", old_value=nested_clip.transform,
        new_value=replace(nested_clip.transform, translate_x=55)))
    view.undo()
    assert view._timeline.get_clip_by_id(nested_clip.clip_id).transform.translate_x == 0
    assert child_id in host._current_timeline_sequences
    view.undo()
    assert host._current_edit_project.revision == 11
    assert view._timeline.get_clip_by_id("c") is not None
    assert child_id not in host._current_timeline_sequences
    assert child_id not in host._current_edit_project.timelines
    assert asset_id not in host._current_edit_project.assets
    view.redo()
    assert host._current_edit_project.revision == 12
    assert view._timeline.get_clip_by_id(nested_clip.clip_id) is not None
    assert child_id in host._current_timeline_sequences
    view.redo()
    assert view._timeline.get_clip_by_id(nested_clip.clip_id).transform.translate_x == 55
    assert host._current_edit_project.revision == 13


def test_unrelated_project_changes_survive_nested_undo(qtbot: Any) -> None:
    host, view, command, txn = _fixture(qtbot)
    host._execute_timeline_edit_command(command, view._timeline)
    unrelated = MediaAsset(asset_id="other", path="/missing/other.mov", media_type="video",
                           display_name="Other", metadata={"new": True})
    host._current_edit_project = host._current_edit_project.with_media_asset_added(unrelated)
    view.undo()
    assert host._current_edit_project.assets["other"] is unrelated
    assert txn.nested_asset.asset_id not in host._current_edit_project.assets


@pytest.mark.parametrize("conflict", ["asset", "child", "other_reference"])
def test_conflicting_structural_undo_is_zero_write_and_keeps_history(qtbot: Any, conflict: str) -> None:
    host, view, command, txn = _fixture(qtbot)
    host._execute_timeline_edit_command(command, view._timeline)
    child_id, asset_id = txn.child_timeline.timeline_id, txn.nested_asset.asset_id
    if conflict == "asset":
        asset = host._current_edit_project.assets[asset_id]
        assets = dict(host._current_edit_project.assets)
        assets[asset_id] = replace(asset, display_name="User rename")
        host._current_edit_project = replace(host._current_edit_project, assets=assets)
    elif conflict == "child":
        child = host._current_timeline_sequences[child_id]
        host._current_timeline_sequences[child_id] = replace(child, name="Edited child")
    else:
        clip = replace(view._timeline.video_tracks[0].clips[0], clip_id="other-use", track_id="ov")
        other = TimelineSequence(timeline_id="other", name="Other",
            video_tracks=(EditTrack(track_id="ov", name="V", track_type="video", index=0,
                                    clips=(clip,)),))
        host._current_timeline_sequences["other"] = other
    before_project, before_sequence = host._current_edit_project, view._timeline
    before_cache = dict(host._current_timeline_sequences)
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    view.undo()
    assert host._current_edit_project is before_project
    assert view._timeline is before_sequence
    assert host._current_timeline_sequences == before_cache
    assert len(view._command_stack._undo_stack) == 1
    assert not view.can_redo()
    assert messages


def test_macro_wrapped_project_transaction_is_explicitly_rejected(qtbot: Any) -> None:
    host, view, command, _ = _fixture(qtbot)
    before = view._timeline
    with pytest.raises(ValueError, match="outside a Macro"):
        host._execute_timeline_edit_command(MacroCommand.create("Unsafe wrapper", [command]), before)
    assert view._timeline is before
    assert not view.can_undo()
    assert host._current_edit_project.revision == 7


def test_structural_command_json_roundtrip_and_snapshots_are_detached(qtbot: Any) -> None:
    host, view, command, txn = _fixture(qtbot)
    restored = TimelineProjectTransactionCommand.from_dict(json.loads(json.dumps(command.to_dict())))
    assert restored.to_dict() == command.to_dict()
    original_parent = txn.new_timeline.video_tracks[0].clips[0]
    original_parent._extra_fields["new"] = 1
    assert command.after_sequence.video_tracks[0].clips[0]._extra_fields.get("new") is None
    host._execute_timeline_edit_command(restored, view._timeline)
    view.undo()
    assert view._timeline.get_clip_by_id("c") is not None


def test_noop_transaction_preserves_redo_revision_and_dirty_state(qtbot: Any) -> None:
    host, view, command, _ = _fixture(qtbot)
    host._execute_timeline_edit_command(command, view._timeline)
    view.undo()
    before_project, before_sequence = host._current_edit_project, view._timeline
    noop = TimelineProjectTransactionCommand.create(
        before_sequence, before_sequence, before_project=before_project, after_project=before_project)
    host.edit_project_dirty = False
    host._execute_timeline_edit_command(noop, before_sequence)
    assert view._timeline is before_sequence
    assert host._current_edit_project is before_project
    assert host.edit_project_dirty is False
    assert view.can_redo()
    assert not view.can_undo()


def test_serialized_transaction_cannot_spoof_child_identity(qtbot: Any) -> None:
    _, _, command, _ = _fixture(qtbot)
    data = command.to_dict()
    child_pair = next(iter(data["child_sequence_changes"].values()))
    child_pair[1]["timeline_id"] = "wrong-child"
    with pytest.raises(ValueError, match="object identity"):
        TimelineProjectTransactionCommand.from_dict(data)


def test_panel_refresh_observes_complete_project_and_sequence_publication(qtbot: Any) -> None:
    host, view, command, txn = _fixture(qtbot)
    publications: list[tuple[bool, bool, bool]] = []
    def refreshed(sequence: TimelineSequence) -> None:
        publications.append((
            txn.nested_asset.asset_id in host._current_edit_project.assets,
            txn.child_timeline.timeline_id in host._current_timeline_sequences,
            host._current_timeline_sequences[sequence.timeline_id] is sequence,
        ))
    host.timeline_panel.refresh_committed_timeline = refreshed
    host._execute_timeline_edit_command(command, view._timeline)
    assert publications == [(True, True, True)]
