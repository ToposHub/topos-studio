"""Multicam creation failure atomicity and async synchronization identities."""
from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.app.controllers.edit_controller import EditControllerMixin
from src.features.edit.core.commands import CommandStack, InsertClipCommand
from src.features.edit.core.multicam import (
    MulticamSyncIdentity, build_multicam_group_from_assets, multicam_group_from_asset,
    map_angle_source_frame, plan_multicam_insert, stamp_multicam_group_on_assets,
    UpdateMulticamSyncCommand, group_with_sync_offsets,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence

pytestmark = pytest.mark.edit_core


def _project(tmp_path):
    assets = []
    for aid, fps in (("a", 24), ("b", 48)):
        path = tmp_path / f"{aid}.mov"
        path.write_bytes(aid.encode())
        assets.append(MediaAsset(aid, str(path), "video", aid, metadata={
            "fps": fps, "total_frames": 240}))
    return EditProject("p", "P", assets={a.asset_id: a for a in assets}, revision=7)


def _controller(project, *, locked=False, fail_commit=False):
    ctrl = EditControllerMixin.__new__(EditControllerMixin)
    timeline = TimelineSequence("tl", "T", fps=30.0, video_tracks=(
        EditTrack("v", "V", "video", 0, is_locked=locked),))
    ctrl._current_edit_project = project
    ctrl._current_timeline_sequences = {"tl": timeline}
    ctrl.edit_page = SimpleNamespace(_current_timeline=timeline, _current_frame=0)
    ctrl.media_pool_panel = Mock()
    ctrl.status_bar = Mock()
    ctrl._refresh_multicam_panel = Mock()
    ctrl._on_multicam_playhead_frame = Mock()
    stack = CommandStack()
    executed = []
    def execute(command):
        if fail_commit:
            raise ValueError("commit rejected")
        prepared_project = None
        if hasattr(command, "prepare_project"):
            prepared_project, children = command.prepare_project(
                ctrl._current_edit_project, ctrl._current_timeline_sequences)
        updated = stack.execute(command, ctrl.edit_page._current_timeline)
        ctrl.edit_page._current_timeline = updated
        view._timeline = updated
        ctrl._current_timeline_sequences[updated.timeline_id] = updated
        if prepared_project is not None:
            ctrl._current_edit_project = prepared_project
            ctrl._current_timeline_sequences.update(children)
            ctrl.media_pool_panel.set_project(prepared_project)
        executed.append(command)
    view = SimpleNamespace(execute_command=execute, _timeline=timeline)
    ctrl.timeline_panel = SimpleNamespace(get_timeline_view=lambda: view)
    return ctrl, stack, executed


@pytest.mark.parametrize("failure", ["locked", "missing_view", "commit"])
def test_creation_failure_is_zero_metadata_revision_and_history(tmp_path, failure) -> None:
    project = _project(tmp_path)
    ctrl, stack, _executed = _controller(
        project, locked=failure == "locked", fail_commit=failure == "commit")
    if failure == "missing_view":
        ctrl.timeline_panel.get_timeline_view = lambda: None
    ctrl._on_create_multicam_group(["a", "b"])
    assert ctrl._current_edit_project is project
    assert ctrl._current_edit_project.revision == 7
    assert not stack.can_undo()
    assert all(multicam_group_from_asset(asset) is None for asset in project.assets.values())
    ctrl.media_pool_panel.set_project.assert_not_called()


def test_dry_apply_is_pure_and_commit_keeps_prepared_ids_and_fps(tmp_path) -> None:
    project = _project(tmp_path)
    ctrl, stack, executed = _controller(project)
    original = ctrl.edit_page._current_timeline
    plan = plan_multicam_insert(project, original, ["a", "b"], "v", 0)
    assert original.video_tracks[0].clips == ()
    assert project.revision == 7 and all(multicam_group_from_asset(a) is None for a in project.assets.values())
    preview, prepared = plan.command.apply(original)
    assert preview.get_clip_by_id(plan.command._inserted_clip_id) is not None
    assert prepared._inserted_clip_id == plan.command._inserted_clip_id
    ctrl._on_create_multicam_group(["a", "b"])
    committed = ctrl.edit_page._current_timeline
    command = executed[0]
    inserted = committed.video_tracks[0].clips[0]
    assert inserted is not None
    # 240/48 is the shortest angle, i.e. 5s = 120 initial-source / 150 TL frames.
    assert (inserted.source_duration, inserted.timeline_duration) == (120, 150)
    assert (inserted._source_fps_num, inserted._timeline_fps_num) == (24, 30)
    assert ctrl._current_edit_project.revision == 8
    assert len(stack._undo_stack) == 1
    assert all(multicam_group_from_asset(asset) is not None for asset in ctrl._current_edit_project.assets.values())


def _sync_controller(tmp_path):
    project = _project(tmp_path)
    group = build_multicam_group_from_assets(["a", "b"])
    members = stamp_multicam_group_on_assets(tuple(project.assets.values()), group)
    project = replace(project, assets={asset.asset_id: asset for asset in members})
    identity = MulticamSyncIdentity.capture(project, group)
    ctrl = EditControllerMixin.__new__(EditControllerMixin)
    ctrl._current_edit_project = project
    ctrl._multicam_sync_request = ("new", identity)
    ctrl._multicam_sync_worker = object()
    ctrl._multicam_sync_group_asset_id = "a"
    ctrl.multicam_panel = Mock()
    ctrl.status_bar = Mock()
    ctrl._apply_multicam_sync_offsets = Mock()
    return ctrl, project, identity


def test_old_sync_completion_and_failure_keep_new_request_intact(tmp_path) -> None:
    ctrl, _project, _identity = _sync_controller(tmp_path)
    worker = ctrl._multicam_sync_worker
    ctrl._on_multicam_sync_done({}, "old")
    ctrl._on_multicam_sync_failed("old failure", "old")
    assert ctrl._multicam_sync_worker is worker
    assert ctrl._multicam_sync_request[0] == "new"
    ctrl.multicam_panel.set_sync_running.assert_not_called()
    ctrl._apply_multicam_sync_offsets.assert_not_called()


@pytest.mark.parametrize("change", ["project", "source", "file"])
def test_sync_result_rejects_changed_identity_before_dialog(qapp, tmp_path, monkeypatch, change) -> None:
    ctrl, project, _identity = _sync_controller(tmp_path)
    if change == "project":
        ctrl._current_edit_project = replace(project, project_id="different")
    elif change == "source":
        ctrl._current_edit_project = replace(project, assets={
            **project.assets, "b": replace(project.assets["b"], path="/new/source.mov")})
    else:
        with open(project.assets["b"].path, "wb") as stream:
            stream.write(b"different in-place source")
    from src.features.edit.ui.multicam_sync_result_dialog import MulticamSyncResultDialog
    opened = Mock(side_effect=AssertionError("stale request opened a dialog"))
    monkeypatch.setattr(MulticamSyncResultDialog, "exec", opened)
    ctrl._on_multicam_sync_done({}, "new")
    opened.assert_not_called()
    ctrl._apply_multicam_sync_offsets.assert_not_called()
    assert ctrl._current_edit_project.revision == 7


def test_sync_rechecks_identity_after_modal_dialog(qapp, tmp_path, monkeypatch) -> None:
    ctrl, project, _identity = _sync_controller(tmp_path)
    from src.features.edit.ui.multicam_sync_result_dialog import MulticamSyncResultDialog
    def accept_after_project_change(_dialog):
        ctrl._current_edit_project = replace(project, project_id="new-project")
        return MulticamSyncResultDialog.DialogCode.Accepted
    monkeypatch.setattr(MulticamSyncResultDialog, "exec", accept_after_project_change)
    ctrl._on_multicam_sync_done({}, "new")
    ctrl._apply_multicam_sync_offsets.assert_not_called()
    assert ctrl._current_edit_project.revision == 7


def test_unrelated_project_edits_do_not_invalidate_source_identity(tmp_path) -> None:
    _ctrl, project, identity = _sync_controller(tmp_path)
    assert identity.matches(replace(project, name="Renamed", revision=8))


def test_deleted_multicam_member_has_actionable_preflight(tmp_path) -> None:
    project = _project(tmp_path)
    group = build_multicam_group_from_assets(["a", "b", "missing"])
    with pytest.raises(ValueError, match="已删除"):
        MulticamSyncIdentity.capture(project, group)


def test_angle_mapping_converts_each_cameras_own_sync_clock() -> None:
    # A source frame 30 includes a 6f delay. The shared instant is 24/24s;
    # camera B samples 48fps and adds its own 10f delay.
    assert map_angle_source_frame(30, 6, 10, Fraction(24), Fraction(48)) == 58
    assert map_angle_source_frame(58, 10, 6, Fraction(48), Fraction(24)) == 30


@pytest.mark.parametrize("reverse", [False, True])
def test_grid_maps_sampled_retimed_frame_into_target_camera_clock(tmp_path, reverse) -> None:
    from src.features.edit.core.clip_params import SpeedParams
    from src.features.edit.core.timeline_v2 import ClipInstance
    project = _project(tmp_path)
    group = build_multicam_group_from_assets(["a", "b"])
    stamped = stamp_multicam_group_on_assets(tuple(project.assets.values()), group)
    project = replace(project, assets={asset.asset_id: asset for asset in stamped})
    ctrl, _stack, _executed = _controller(project)
    clip = ClipInstance(
        "c", "a", "v", 0, 50, 0, 100,
        speed=SpeedParams(speed=2.0, reverse=reverse),
        _source_fps_num=24, _source_fps_den=1,
        _timeline_fps_num=30, _timeline_fps_den=1)
    ctrl._multicam_clip_at_playhead = lambda: clip
    ctrl.multicam_panel = Mock()
    ctrl._on_multicam_playhead_frame = EditControllerMixin._on_multicam_playhead_frame.__get__(ctrl)
    ctrl._on_multicam_playhead_frame(10)
    frames, _infos = ctrl.multicam_panel.request_angle_frames.call_args.args
    expected = clip.timeline_to_source_frame(10)
    assert frames == {"a": expected, "b": expected * 2}


def test_sync_command_history_preserves_unrelated_live_asset_changes(tmp_path) -> None:
    import json
    from src.features.edit.core.commands import ProjectCommandStack
    _ctrl, project, _identity = _sync_controller(tmp_path)
    group = multicam_group_from_asset(project.assets["a"])
    updated_group = group_with_sync_offsets(group, {"b": 48})
    command = UpdateMulticamSyncCommand.create(project, updated_group)
    command = UpdateMulticamSyncCommand.from_dict(json.loads(json.dumps(command.to_dict())))
    stack = ProjectCommandStack()
    committed = stack.execute(command, project)
    assert committed.revision == 8
    # Another metadata edit outside this history must survive Sync Undo.
    live = replace(committed, assets={
        **committed.assets,
        "a": replace(committed.assets["a"], metadata={
            **committed.assets["a"].metadata, "user_log": "new log"})})
    restored = stack.undo(live)
    assert restored.revision == 9
    assert multicam_group_from_asset(restored.assets["b"]) == group
    assert restored.assets["a"].metadata["user_log"] == "new log"
    redone = stack.redo(restored)
    assert redone.revision == 10
    assert multicam_group_from_asset(redone.assets["b"]) == updated_group


def test_sync_command_rejects_group_conflict_without_partial_undo(tmp_path) -> None:
    from src.features.edit.core.commands import ProjectCommandStack
    _ctrl, project, _identity = _sync_controller(tmp_path)
    group = multicam_group_from_asset(project.assets["a"])
    command = UpdateMulticamSyncCommand.create(project, group_with_sync_offsets(group, {"b": 48}))
    stack = ProjectCommandStack()
    committed = stack.execute(command, project)
    conflicting = replace(committed, assets={
        **committed.assets,
        "b": replace(committed.assets["b"], metadata={
            **committed.assets["b"].metadata, "multicam_group": {"changed": True}})})
    with pytest.raises(ValueError, match="其他操作"):
        stack.undo(conflicting)
    assert stack.can_undo() and not stack.can_redo()
    assert multicam_group_from_asset(conflicting.assets["a"]).angles[1].sync_offset == 48


def test_sync_controller_publishes_project_history_revision_and_dirty(tmp_path) -> None:
    from src.features.edit.core.commands import ProjectCommandStack
    ctrl, project, _identity = _sync_controller(tmp_path)
    ctrl._apply_multicam_sync_offsets = EditControllerMixin._apply_multicam_sync_offsets.__get__(ctrl)
    stack = ProjectCommandStack()
    ctrl.media_pool_panel = Mock()
    ctrl.media_pool_panel.get_command_stack.return_value = stack
    ctrl._refresh_multicam_panel = Mock()
    ctrl._on_multicam_playhead_frame = Mock()
    ctrl.edit_page = None
    group = multicam_group_from_asset(project.assets["a"])
    ctrl._apply_multicam_sync_offsets(group, {"b": {"offset_frames": 48}})
    assert ctrl._current_edit_project.revision == 8
    assert ctrl.edit_project_dirty and stack.can_undo()
    ctrl.media_pool_panel.project_changed.emit.assert_called_once_with(ctrl._current_edit_project)


def test_refused_multicam_switch_restores_panel_without_escaping_slot(tmp_path) -> None:
    from src.features.edit.core.timeline_v2 import ClipInstance
    project = _project(tmp_path)
    ctrl, _stack, _executed = _controller(project, fail_commit=True)
    ctrl._multicam_clip_at_playhead = lambda *_args: ClipInstance("c", "a", "v", 0, 20, 0, 20)
    ctrl._switch_multicam_angle("group", "b")
    assert "commit rejected" in ctrl.status_bar.showMessage.call_args.args[0]
    ctrl._refresh_multicam_panel.assert_called_once()
    ctrl._on_multicam_playhead_frame.assert_called_once()


@pytest.mark.parametrize("kind", ["multicam", "nested"])
def test_structure_creation_undo_redo_follows_later_clip_history_and_ids(tmp_path, kind) -> None:
    import json
    from src.features.edit.core.commands import UpdateClipParamCommand
    from src.features.edit.core.timeline_v2 import ClipInstance
    from src.features.edit.core.timeline_project_transaction import TimelineProjectTransactionCommand

    project = _project(tmp_path)
    ctrl, stack, executed = _controller(project)
    before_sequence = ctrl.edit_page._current_timeline
    if kind == "nested":
        before_sequence = replace(before_sequence, video_tracks=(replace(
            before_sequence.video_tracks[0], clips=(
                ClipInstance("one", "a", "v", 0, 20, 0, 20),
                ClipInstance("two", "b", "v", 20, 20, 0, 20))),))
        ctrl.edit_page._current_timeline = before_sequence
        ctrl.timeline_panel.get_timeline_view()._timeline = before_sequence
        ctrl._current_timeline_sequences["tl"] = before_sequence
        ctrl._on_create_nested_from_selection(["one", "two"])
    else:
        ctrl._on_create_multicam_group(["a", "b"])
    assert len(executed) == 1
    structure = executed[0]
    # Public JSON roundtrip carries the exact asset/ref/child payloads.
    structure = TimelineProjectTransactionCommand.from_dict(json.loads(json.dumps(structure.to_dict())))
    stack._undo_stack[0] = structure
    created_sequence = ctrl.edit_page._current_timeline
    created_project = ctrl._current_edit_project
    created_ids = {clip.clip_id for track in created_sequence.all_tracks for clip in track.clips}
    clip = created_sequence.video_tracks[0].clips[0]
    edited = stack.execute(UpdateClipParamCommand(
        "opacity", "Opacity", "v", clip.clip_id, "opacity", .5,
        old_value=clip.opacity), created_sequence)
    assert edited.get_clip_by_id(clip.clip_id).opacity == .5
    unedited = stack.undo(edited)
    assert unedited.get_clip_by_id(clip.clip_id).opacity == clip.opacity
    restored_project, child_updates = structure.prepare_project(
        created_project, ctrl._current_timeline_sequences, undo=True)
    restored_sequence = stack.undo(unedited)
    assert restored_sequence.video_tracks == before_sequence.video_tracks
    assert {key: vars(asset) for key, asset in restored_project.assets.items()} == {
        key: vars(asset) for key, asset in project.assets.items()}
    assert restored_project.timelines == project.timelines
    sequences = dict(ctrl._current_timeline_sequences)
    for key, child in child_updates.items():
        if child is None:
            sequences.pop(key, None)
        else:
            sequences[key] = child
    redone_project, redone_children = structure.prepare_project(restored_project, sequences)
    redone_sequence = stack.redo(restored_sequence)
    assert {clip.clip_id for track in redone_sequence.all_tracks for clip in track.clips} == created_ids
    assert {key: vars(asset) for key, asset in redone_project.assets.items()} == {
        key: vars(asset) for key, asset in created_project.assets.items()}
    assert redone_project.timelines == created_project.timelines
    if kind == "nested":
        assert len(redone_children) == 1
        assert all(child is not None for child in redone_children.values())
