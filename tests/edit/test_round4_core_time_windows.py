"""Retained media must keep its exact sampling clock through edit transactions."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.commands import (
    InsertClipCommand, OverwriteClipCommand, RippleTrimCommand,
    SplitClipCommand, TrimClipCommand,
)
from src.features.edit.core.timeline_ops import timeline_duration_for_source
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence


def _fixture(speed: SpeedParams, fps: tuple[int | None, int | None]) -> ClipInstance:
    return ClipInstance(
        clip_id="c", asset_id="asset", track_id="v", timeline_start=100,
        source_start=123, source_duration=120,
        timeline_duration=timeline_duration_for_source(120, speed, fps[0], 1, fps[1], 1),
        speed=speed, _source_fps_num=fps[0], _source_fps_den=1,
        _timeline_fps_num=fps[1], _timeline_fps_den=1,
    )


def _sequence(clip: ClipInstance) -> TimelineSequence:
    return TimelineSequence(timeline_id="t", video_tracks=(EditTrack(
        track_id="v", clips=(clip,)),))


def _samples(clip: ClipInstance) -> list[int]:
    return [clip.timeline_to_source_frame(f) for f in range(clip.timeline_start, clip.timeline_end)]


@pytest.mark.parametrize("fps", [(None, None), (24, 24), (30, 24), (25, 24)])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("kind", ["slow", "fractional", "step", "smooth"])
def test_split_retains_every_frame_and_repeated_split_phase(fps, reverse, kind) -> None:
    speed = {
        "slow": SpeedParams(speed=.5, reverse=reverse),
        "fractional": SpeedParams(speed=1.25, reverse=reverse),
        "step": SpeedParams(speed=.75, reverse=reverse, speed_points=(SpeedPoint(40, 1.25),)),
        "smooth": SpeedParams(reverse=reverse, speed_points=(SpeedPoint(40, 2, "smooth"),)),
    }[kind]
    clip = _fixture(speed, fps)
    for cut_local in (1, clip.timeline_duration // 3 + 1, clip.timeline_duration - 1):
        command = SplitClipCommand.create("v", "c", 100 + cut_local)
        cut = command.apply(_sequence(clip))
        children = sorted(cut.video_tracks[0].clips, key=lambda c: c.timeline_start)
        assert sum((_samples(c) for c in children), []) == _samples(clip)
        loaded = TimelineSequence.from_dict(json.loads(json.dumps(cut.to_dict())))
        assert sum((_samples(c) for c in loaded.video_tracks[0].clips), []) == _samples(clip)
        assert command.undo(cut).to_dict() == _sequence(clip).to_dict()
        right = children[1]
        if right.timeline_duration > 1:
            again = SplitClipCommand.create("v", right.clip_id, right.timeline_start + 1).apply(cut)
            assert sum((_samples(c) for c in again.video_tracks[0].clips), []) == _samples(clip)


@pytest.mark.parametrize("fps", [(None, None), (30, 24)])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("smooth", [False, True])
@pytest.mark.parametrize("side", ["left", "right"])
def test_trim_commands_keep_curve_clock_and_full_json_undo(fps, reverse, smooth, side) -> None:
    speed = SpeedParams(reverse=reverse, speed_points=(SpeedPoint(40, 2, "smooth" if smooth else "linear"),))
    clip = _fixture(speed, fps)
    delta = 21
    if side == "left":
        source, duration = clip.with_left_edge_source_values(clip.timeline_start + delta)
        cmd = TrimClipCommand.create_left_trim("v", "c", clip.timeline_start + delta, source,
                                               new_source_duration=duration)
        expected = _samples(clip)[delta:]
    else:
        source, duration = clip.with_right_edge_source_values(clip.timeline_duration - delta)
        cmd = TrimClipCommand.create_right_trim("v", "c", clip.timeline_duration - delta, duration,
                                                new_source_start=source)
        expected = _samples(clip)[:-delta]
    changed = cmd.apply(_sequence(clip))
    assert _samples(changed.get_clip_by_id("c")) == expected
    loaded = TrimClipCommand.from_dict(json.loads(json.dumps(cmd.to_dict())))
    assert loaded.undo(changed).to_dict() == _sequence(clip).to_dict()
    assert _samples(loaded.apply(loaded.undo(changed)).get_clip_by_id("c")) == expected


@pytest.mark.parametrize("operation", ["insert", "overwrite", "ripple"])
@pytest.mark.parametrize("reverse", [False, True])
def test_other_window_edit_entries_keep_sampling(operation, reverse) -> None:
    clip = _fixture(SpeedParams(reverse=reverse, speed_points=(SpeedPoint(40, 2, "smooth"),)), (30, 24))
    sequence = _sequence(clip)
    if operation == "ripple":
        changed, updated = RippleTrimCommand.create_left_trim("v", "c", 121).apply(sequence)
        assert _samples(changed.get_clip_by_id("c")) == _samples(clip)[21:]
    else:
        cls = InsertClipCommand if operation == "insert" else OverwriteClipCommand
        changed, updated = cls.create("new", 0, 9, 149, "v").apply(sequence)
        for c in changed.video_tracks[0].clips:
            if c.asset_id != clip.asset_id:
                continue
            original_start = c.timeline_start - (10 if operation == "insert" and c.timeline_start > 149 else 0)
            offset = original_start - clip.timeline_start
            assert _samples(c) == _samples(clip)[offset:offset+c.timeline_duration]
    assert updated.undo(changed).to_dict() == sequence.to_dict()


@pytest.mark.parametrize("side", ["left", "right", "ripple_left", "ripple_right", "roll"])
def test_marker_ownership_follows_removed_content_and_json_undo(side) -> None:
    from src.features.edit.core.timeline_v2 import TimelineMarker, TransitionInstance
    from src.features.edit.core.timeline_ops import validate_timeline
    from src.features.edit.core.commands import RollTrimCommand
    clip = replace(_fixture(SpeedParams(), (24, 24)), timeline_duration=100, source_duration=100)
    seq = _sequence(clip)._rebuild(markers=(
        TimelineMarker(marker_id="head", clip_id="c", track_id="v", frame=105),
        TimelineMarker(marker_id="middle", clip_id="c", track_id="v", frame=150),
        TimelineMarker(marker_id="tail", clip_id="c", track_id="v", frame=195),
    ))
    if side == "left":
        cmd = TrimClipCommand.create_left_trim("v", "c", 120, 143, new_source_duration=80)
    elif side == "right":
        cmd = TrimClipCommand.create_right_trim("v", "c", 80, 80)
    elif side == "ripple_left":
        cmd = RippleTrimCommand.create_left_trim("v", "c", 120)
    elif side == "ripple_right":
        cmd = RippleTrimCommand.create_right_trim("v", "c", 180)
    else:
        other = replace(clip, clip_id="other", timeline_start=200)
        seq = seq.with_clip_added("v", other)
        seq = seq.with_track_updated(seq.video_tracks[0]._rebuild(transitions=(TransitionInstance(
            transition_id="seam", track_id="v", left_clip_id="c", right_clip_id="other",
            center_frame=200, duration=20),)))
        cmd = RollTrimCommand.create("v", "c", "other", -20)
    result = cmd.apply(seq)
    changed, updated = result if isinstance(result, tuple) else (result, cmd)
    assert validate_timeline(changed) == []
    assert len(changed.markers) == 2
    if side == "ripple_left":
        assert {m.marker_id: m.frame for m in changed.markers} == {"middle": 130, "tail": 175}
    loaded = type(cmd).from_dict(json.loads(json.dumps(updated.to_dict())))
    assert loaded.undo(changed).to_dict() == seq.to_dict()


@pytest.mark.parametrize("reverse", [False, True])
def test_shared_roll_bounds_cover_source_head_tail_and_command_payload(reverse) -> None:
    from src.features.edit.core.commands import RollTrimCommand
    from src.features.edit.core.timeline_ops import get_roll_trim_constraints, roll_trim_delta_bounds
    left = replace(_fixture(SpeedParams(speed=2, reverse=reverse), (30, 24)),
                   timeline_start=0, timeline_duration=20, source_start=100, source_duration=50)
    right = replace(left, clip_id="b", timeline_start=20, source_start=2)
    bounds = roll_trim_delta_bounds(left, right, 160, 60)
    assert get_roll_trim_constraints(_sequence(left).with_clip_added("v", right),
                                     "c", "b", 160, 60) == bounds
    for delta in range(-19, 20):
        cmd = RollTrimCommand.create("v", "c", "b", delta,
                                    left_max_source_frames=160, right_max_source_frames=60)
        seq = _sequence(left).with_clip_added("v", right)
        if bounds[0] <= delta <= bounds[1]:
            changed, updated = cmd.apply(seq)
            loaded = RollTrimCommand.from_dict(json.loads(json.dumps(updated.to_dict())))
            assert loaded.undo(changed).to_dict() == seq.to_dict()
        else:
            with pytest.raises(ValueError):
                cmd.apply(seq)


def test_phase_schema_migration_is_idempotent_and_preserves_unknown_fields() -> None:
    from src.features.edit.core.project_migration import migrate_project_payload
    clip = _fixture(SpeedParams(), (30, 24)).to_dict()
    clip.pop("source_frame_phase_num")
    clip.pop("source_frame_phase_den")
    clip["speed"].pop("curve_time_offset")
    clip["future_field"] = {"keep": [1, 2]}
    data = {"edit_content_schema_version": 3, "timeline_sequences": {"t": {"video_tracks": [{"clips": [clip]}]}}}
    migrated, log = migrate_project_payload(data)
    assert migrated["edit_content_schema_version"] == 4
    assert len(log) == 1 and "clip_sampling_phase" in log[0]
    assert clip["source_frame_phase_num"] == 0 and clip["source_frame_phase_den"] == 1
    assert clip["speed"]["curve_time_offset"] is None
    assert ClipInstance.from_dict(clip).to_dict()["future_field"] == {"keep": [1, 2]}
    assert migrate_project_payload(migrated)[1] == []


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("cut", [1, 2, 3, 10, 65, 191])
def test_clip_blend_pair_keeps_fps_and_sampling_phase_after_cut(reverse, cut) -> None:
    clip = _fixture(SpeedParams(speed=.5, reverse=reverse, interpolation="frame_blend"), (30, 24))
    sequence = SplitClipCommand.create("v", "c", 100 + cut).apply(_sequence(clip))
    for child in sequence.video_tracks[0].clips:
        for frame in range(child.timeline_start, child.timeline_end):
            a, b, weight = child.timeline_to_source_frame_pair(frame)
            x, y, original_weight = clip.timeline_to_source_frame_pair(frame)
            assert a + child.source_start == x + clip.source_start
            if weight > 0:
                assert b + child.source_start == y + clip.source_start
            assert weight == pytest.approx(original_weight)


def test_source_overwrite_keeps_both_old_residual_av_pairs_and_history() -> None:
    from src.features.edit.core.commands import MacroCommand, InsertLinkedAssetRippleCommand
    from src.features.edit.core.timeline_ops import validate_timeline
    from src.features.edit.ui.timeline_view import _LinkClipsAfterApplyCommand
    v = ClipInstance(clip_id="v", asset_id="old", track_id="v1", timeline_start=0,
                     timeline_duration=1572, source_start=0, source_duration=1572, linked_clip_id="a")
    a = replace(v, clip_id="a", track_id="a1", linked_clip_id="v")
    before = TimelineSequence(timeline_id="t", video_tracks=(EditTrack(track_id="v1", clips=(v,)),),
                              audio_tracks=(EditTrack(track_id="a1", track_type="audio", clips=(a,)),))
    command = MacroCommand.create("Source Overwrite", [
        OverwriteClipCommand.create("new", 0, 9, 19, "v1"),
        OverwriteClipCommand.create("new", 0, 9, 19, "a1"),
        _LinkClipsAfterApplyCommand(asset_id="new", video_track_id="v1", video_start=19,
                                   audio_clip_id="generated", audio_track_id="a1"),
    ])
    changed, updated = command.apply(before)
    assert validate_timeline(changed) == []
    for track in changed.all_tracks:
        for clip in track.clips:
            partner = changed.get_reciprocal_linked_clip(clip)
            assert partner is not None
            assert (partner.timeline_start, partner.timeline_end) == (clip.timeline_start, clip.timeline_end)
    restored = updated.undo(changed)
    assert restored.to_dict() == before.to_dict()
    redone, redo = updated.apply(restored)
    assert all(redone.get_reciprocal_linked_clip(c) is not None for t in redone.all_tracks for c in t.clips)
    inserted, _ = InsertLinkedAssetRippleCommand.create("next", 0, 4, 40, "v1", "a1").apply(redone)
    assert all(inserted.get_reciprocal_linked_clip(c) is not None for t in inserted.all_tracks for c in t.clips)
    assert validate_timeline(inserted) == []


def test_roll_uses_current_curve_window_instead_of_base_speed_for_head_bounds() -> None:
    from src.features.edit.core.commands import RollTrimCommand
    from src.features.edit.core.timeline_ops import roll_trim_delta_bounds

    left = replace(_fixture(SpeedParams(), (24, 24)), timeline_start=0,
                   timeline_duration=20, source_duration=20)
    right = replace(left, clip_id="right", timeline_start=20, source_start=2,
                    source_duration=10, speed=SpeedParams(
                        speed=8, speed_points=(SpeedPoint(1, .5),),
                        curve_offset=21, curve_length=100, curve_time_offset=40.125))
    before = _sequence(left).with_clip_added("v", right)
    assert roll_trim_delta_bounds(left, right)[0] <= -1
    changed, command = RollTrimCommand.create("v", "c", "right", -1).apply(before)
    assert _samples(changed.get_clip_by_id("right"))[1:] == _samples(right)
    assert command.undo(changed).to_dict() == before.to_dict()


@pytest.mark.parametrize("switch_frame", [0, 24])
def test_multicam_switch_scales_fps_and_roundtrips_track_marker_history(switch_frame) -> None:
    from src.features.edit.core.commands import SwitchMulticamAngleCommand
    from src.features.edit.core.timeline_ops import validate_timeline
    from src.features.edit.core.timeline_v2 import TimelineMarker, TransitionInstance

    clip = replace(_fixture(SpeedParams(), (24, 24)), timeline_start=0,
                   source_start=24, source_duration=72, timeline_duration=72,
                   _source_frame_phase_num=1, _source_frame_phase_den=2)
    following = replace(clip, clip_id="next", timeline_start=72)
    track = EditTrack(track_id="v", clips=(clip, following), transitions=(TransitionInstance(
        transition_id="seam", track_id="v", left_clip_id="c", right_clip_id="next",
        center_frame=72, duration=10),))
    before = TimelineSequence(timeline_id="t", video_tracks=(track,), markers=(TimelineMarker(
        marker_id="owned", clip_id="c", track_id="v", frame=50),))
    command = SwitchMulticamAngleCommand.create(
        "v", "c", switch_frame, "camera48", active_sync_offset=4,
        angle_sync_offset=12, angle_fps_num=48, angle_fps_den=1)
    switch_source = clip.timeline_to_source_frame(switch_frame)
    changed, command = command.apply(before)
    target = changed.video_tracks[0].get_clip_at(switch_frame)
    assert target.source_start == (switch_source - 4) * 2 + 12
    assert target._source_fps_num == 48
    assert target._source_pts_index is None
    assert target._source_frame_phase_num == 0
    assert validate_timeline(changed) == []
    loaded = SwitchMulticamAngleCommand.from_dict(json.loads(json.dumps(command.to_dict())))
    restored = loaded.undo(changed)
    assert restored.to_dict() == before.to_dict()
    redone, loaded = loaded.apply(restored)
    assert redone.to_dict() == changed.to_dict()


def test_source_swap_without_new_fps_keeps_existing_mixed_fps_window() -> None:
    from src.features.edit.core.commands import UpdateClipSourceCommand

    clip = _fixture(SpeedParams(), (30, 24))
    before = _sequence(clip)
    changed, command = UpdateClipSourceCommand.create("v", "c", "replacement").apply(before)
    target = changed.get_clip_by_id("c")
    assert target.source_duration == clip.source_duration
    assert _samples(target) == _samples(clip)
    assert command.undo(changed).to_dict() == before.to_dict()


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("easing", ["linear", "smooth"])
def test_source_swap_keeps_equal_fps_speed_curve_domain(reverse, easing) -> None:
    from src.features.edit.core.commands import UpdateClipSourceCommand

    clip = _fixture(SpeedParams(reverse=reverse, speed_points=(SpeedPoint(40, 2, easing),)), (24, 24))
    before = _sequence(clip)
    changed, command = UpdateClipSourceCommand.create("v", "c", "replacement").apply(before)
    assert _samples(changed.get_clip_by_id("c")) == _samples(clip)
    loaded = UpdateClipSourceCommand.from_dict(json.loads(json.dumps(command.to_dict())))
    assert loaded.undo(changed).to_dict() == before.to_dict()


@pytest.mark.parametrize("operation", ["source", "boundary_angle", "split_angle"])
def test_cross_fps_curve_source_switch_rejects_before_any_partial_edit(operation) -> None:
    from src.features.edit.core.commands import SwitchMulticamAngleCommand, UpdateClipSourceCommand

    clip = _fixture(SpeedParams(speed_points=(SpeedPoint(40, 2, "smooth"),)), (24, 24))
    before = _sequence(clip)
    if operation == "source":
        command = UpdateClipSourceCommand("id", "swap", "v", "c", "other",
                                           _new_source_fps_num=48, _new_source_fps_den=1)
    else:
        command = SwitchMulticamAngleCommand.create(
            "v", "c", 100 if operation == "boundary_angle" else 124, "other",
            angle_fps_num=48, angle_fps_den=1)
    payload = before.to_dict()
    with pytest.raises(ValueError, match="cannot be converted losslessly"):
        command.apply(before)
    assert before.to_dict() == payload
    assert command._original_clip is None


def test_project_transaction_public_json_and_macro_registry_keep_payload() -> None:
    from src.features.edit.core.commands import MacroCommand
    from src.features.edit.core.project import EditProject, MediaAsset, TimelineSequenceRef
    from src.features.edit.core.timeline_project_transaction import TimelineProjectTransactionCommand

    before_sequence = _sequence(_fixture(SpeedParams(), (24, 24)))
    after_sequence = replace(before_sequence, name="Changed")
    before_project = EditProject(project_id="p", name="P", revision=7)
    new_asset = MediaAsset(asset_id="new", display_name="New", media_type="video",
                           path="fixture.mp4", metadata={})
    child = TimelineSequence(timeline_id="child", name="Child")
    after_project = replace(before_project, assets={"new": new_asset}, timelines={
        "child": TimelineSequenceRef(timeline_id="child", name="Child")})
    command = TimelineProjectTransactionCommand.create(
        before_sequence, after_sequence, before_project=before_project, after_project=after_project,
        after_child_sequences={"child": child})
    loaded = TimelineProjectTransactionCommand.from_dict(json.loads(json.dumps(command.to_dict())))
    assert loaded.to_dict() == command.to_dict()
    project, children = loaded.prepare_project(before_project, {"t": before_sequence})
    assert project.assets == after_project.assets
    assert project.timelines == after_project.timelines
    assert project.revision == 8
    assert children == {"child": child}
    reverted, removed = loaded.prepare_project(project, {"t": after_sequence, "child": child}, undo=True)
    assert reverted.assets == before_project.assets
    assert reverted.timelines == before_project.timelines
    assert removed == {"child": None}
    macro = MacroCommand.create("archive", [command])
    restored_macro = MacroCommand.from_dict(json.loads(json.dumps(macro.to_dict())))
    assert isinstance(restored_macro.sub_commands[0], TimelineProjectTransactionCommand)
    assert restored_macro.sub_commands[0].to_dict() == command.to_dict()


@pytest.mark.parametrize("reverse", [False, True])
def test_public_roll_constraints_match_curve_windows_and_reject_noncut(reverse) -> None:
    from src.features.edit.core.timeline_ops import get_roll_trim_constraints, roll_trim_delta_bounds

    left = replace(_fixture(SpeedParams(reverse=reverse, speed_points=(SpeedPoint(40, 2, "smooth"),)),
                             (30, 24)), timeline_start=0)
    right = replace(left, clip_id="right", timeline_start=left.timeline_end, source_start=20)
    sequence = _sequence(left).with_clip_added("v", right)
    assert get_roll_trim_constraints(sequence, "c", "right", 300, 160) == roll_trim_delta_bounds(
        left, right, 300, 160)
    gap = sequence.with_clip_updated(replace(right, timeline_start=right.timeline_start + 1))
    assert get_roll_trim_constraints(gap, "c", "right", 300, 160) == (0, 0)


def test_frozen_trim_retains_potential_source_window_not_frozen_pixel_extent() -> None:
    clip = replace(_fixture(SpeedParams(), (24000, 25)), timeline_start=0,
                   timeline_duration=100, source_start=0, source_duration=100,
                   freeze_source_frame=42, _source_fps_den=1001)
    assert clip.with_right_edge_source_values(80) == (0, 81)
    assert clip.with_left_edge_source_values(20) == (19, 81)
    assert _samples(clip) == [42] * 100
