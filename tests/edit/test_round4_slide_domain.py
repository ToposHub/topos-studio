"""Slide preserves gaps and the exact retained sampling clock of both neighbors."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.commands import SlideClipCommand
from src.features.edit.core.timeline_ops import (
    TimelineOperationError, get_slide_bounds, slide_clip,
    timeline_duration_for_source, validate_timeline,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance, EditTrack, TimelineMarker, TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _sequence(speed: SpeedParams, source_fps: int = 30) -> TimelineSequence:
    # Leave a real source handle at the right edge: the nonzero CFR phase
    # must not be artificially clamped by an already exhausted source window.
    duration = timeline_duration_for_source(116, speed, source_fps, 1, 24, 1)
    previous = ClipInstance(
        clip_id="previous", asset_id="asset-p", track_id="v", timeline_start=0,
        timeline_duration=duration, source_start=123, source_duration=120,
        speed=speed, _source_fps_num=source_fps, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1,
        _source_frame_phase_num=1, _source_frame_phase_den=3,
        color_stack_id="keep-color", color_shared=True,
    )
    moving = replace(previous, clip_id="moving", asset_id="asset-m",
                     timeline_start=duration + 7, timeline_duration=40)
    following = replace(previous, clip_id="following", asset_id="asset-f",
                        timeline_start=moving.timeline_end + 11)
    return TimelineSequence(timeline_id="slide", video_tracks=(
        EditTrack(track_id="v", clips=(previous, moving, following)),))


def _samples(clip: ClipInstance) -> list[int]:
    return [clip.timeline_to_source_frame(frame)
            for frame in range(clip.timeline_start, clip.timeline_end)]


@pytest.mark.parametrize("source_fps", [24, 30])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("kind", ["slow", "fractional", "step", "smooth"])
@pytest.mark.parametrize("delta", [-9, 9])
def test_slide_retains_exact_neighbor_sampling_and_individual_gaps(
    source_fps: int, reverse: bool, kind: str, delta: int,
) -> None:
    speed = {
        "slow": SpeedParams(speed=.5, reverse=reverse),
        "fractional": SpeedParams(speed=1.25, reverse=reverse),
        "step": SpeedParams(speed=.75, reverse=reverse,
                            speed_points=(SpeedPoint(40, 1.25),)),
        "smooth": SpeedParams(reverse=reverse,
                              speed_points=(SpeedPoint(40, 2, "smooth"),)),
    }[kind]
    before = _sequence(speed, source_fps)
    payload = before.to_dict()
    after = slide_clip(before, "moving", delta, 1000, 1000)
    p, m, n = before.video_tracks[0].clips
    ap, am, an = after.video_tracks[0].clips
    assert am.timeline_start == m.timeline_start + delta
    assert _samples(am) == _samples(m)
    assert am.source_start == m.source_start
    assert am.source_duration == m.source_duration
    assert am.speed == m.speed
    assert (am._source_frame_phase_num, am._source_frame_phase_den) == (1, 3)
    assert ap.timeline_end == p.timeline_end + delta
    assert an.timeline_start == n.timeline_start + delta
    assert ap.timeline_start == p.timeline_start
    assert an.timeline_end == n.timeline_end
    assert am.timeline_start - ap.timeline_end == 7
    assert an.timeline_start - am.timeline_end == 11
    for old, new in ((p, ap), (n, an)):
        start, end = max(old.timeline_start, new.timeline_start), min(old.timeline_end, new.timeline_end)
        assert [new.timeline_to_source_frame(f) for f in range(start, end)] == [
            old.timeline_to_source_frame(f) for f in range(start, end)]
        assert new.color_stack_id == old.color_stack_id
        assert new.color_shared == old.color_shared
    assert before.to_dict() == payload
    assert validate_timeline(after) == []


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("curve", [False, True])
@pytest.mark.parametrize("known_limits", [False, True])
def test_slide_bounds_exactly_match_all_source_window_candidates(
    reverse: bool, curve: bool, known_limits: bool,
) -> None:
    speed = SpeedParams(speed=1.25, reverse=reverse,
                        speed_points=(SpeedPoint(40, 2, "smooth"),) if curve else ())
    seq = _sequence(speed)
    p, m, n = seq.video_tracks[0].clips
    limits = (p.source_end + 5, n.source_end + 7) if known_limits else (None, None)
    # Exhaustive independent enumeration checks the complete integer interval,
    # including the first rejected frame on either side. No timing tolerance.
    allowed: list[int] = []
    for delta in range(1 - p.timeline_duration, n.timeline_duration):
        windows = (p.with_right_edge_source_values(p.timeline_duration + delta),
                   n.with_left_edge_source_values(n.timeline_start + delta))
        if all(start >= 0 and duration > 0
               and (limit is None or start + duration <= limit)
               for (start, duration), limit in zip(windows, limits)):
            allowed.append(delta)
    bounds = get_slide_bounds(seq, m.clip_id, *limits)
    assert bounds == (min(allowed), max(allowed))
    assert allowed == list(range(bounds[0], bounds[1] + 1))
    for delta in (bounds[0] - 1, bounds[0], bounds[1], bounds[1] + 1):
        if delta in allowed:
            assert validate_timeline(slide_clip(seq, m.clip_id, delta, *limits)) == []
        else:
            with pytest.raises(TimelineOperationError):
                slide_clip(seq, m.clip_id, delta, *limits)


def test_slide_zero_is_identity_and_locked_track_rejects_without_mutation() -> None:
    seq = _sequence(SpeedParams())
    assert slide_clip(seq, "moving", 0) is seq
    track = seq.video_tracks[0]
    locked = seq.with_track_updated(track._rebuild(is_locked=True))
    assert get_slide_bounds(locked, "moving", 1000, 1000) == (0, 0)
    payload = locked.to_dict()
    for delta in (-1, 0, 1):
        with pytest.raises(TimelineOperationError, match="locked"):
            slide_clip(locked, "moving", delta, 1000, 1000)
    assert locked.to_dict() == payload


@pytest.mark.parametrize("side", ["previous", "following", "isolated"])
def test_slide_single_neighbor_respects_source_and_timeline_bounds(side: str) -> None:
    seq = _sequence(SpeedParams(speed=1.25, reverse=True))
    selected = {"moving", side}
    seq = seq.with_track_updated(seq.video_tracks[0]._rebuild(
        clips=tuple(c for c in seq.video_tracks[0].clips if c.clip_id in selected)))
    bounds = get_slide_bounds(seq, "moving", 250, 250)
    if side == "isolated":
        assert bounds == (0, 0)
    else:
        assert bounds[0] < 0 < bounds[1]
    for delta in (bounds[0] - 1, bounds[0], 0, bounds[1], bounds[1] + 1):
        if bounds[0] <= delta <= bounds[1]:
            assert validate_timeline(slide_clip(seq, "moving", delta, 250, 250)) == []
        else:
            with pytest.raises(TimelineOperationError):
                slide_clip(seq, "moving", delta, 250, 250)


@pytest.mark.parametrize("reverse", [False, True])
def test_slide_command_serializes_neighbor_source_limits(reverse: bool) -> None:
    seq = _sequence(SpeedParams(speed=1.25, reverse=reverse))
    p, m, n = seq.video_tracks[0].clips
    limits = (p.source_end + 3, n.source_end + 5)
    low, high = get_slide_bounds(seq, m.clip_id, *limits)
    for delta in (low, high, low - 1, high + 1):
        command = SlideClipCommand.create(seq, "v", m.clip_id, delta, *limits)
        loaded = SlideClipCommand.from_dict(json.loads(json.dumps(command.to_dict())))
        assert loaded.prev_max_source_frames == limits[0]
        assert loaded.next_max_source_frames == limits[1]
        if low <= delta <= high:
            after, updated = loaded.apply(seq)
            assert updated.undo(after).to_dict() == seq.to_dict()
        else:
            with pytest.raises(TimelineOperationError):
                loaded.apply(seq)


@pytest.mark.parametrize("delta", [-9, 9])
def test_slide_marker_scope_json_undo_and_redo(delta: int) -> None:
    seq = _sequence(SpeedParams(reverse=True,
                               speed_points=(SpeedPoint(40, 2, "smooth"),)))
    p, m, n = seq.video_tracks[0].clips
    seq = seq._rebuild(markers=(
        TimelineMarker(marker_id="ruler", frame=m.timeline_start),
        TimelineMarker(marker_id="moving", frame=m.timeline_start + 1, clip_id=m.clip_id, track_id="v"),
        TimelineMarker(marker_id="previous", frame=p.timeline_end - 1, clip_id=p.clip_id, track_id="v"),
        TimelineMarker(marker_id="following", frame=n.timeline_start + 1, clip_id=n.clip_id, track_id="v"),
    ))
    command = SlideClipCommand.create(seq, "v", "moving", delta)
    changed, updated = command.apply(seq)
    expected = {"ruler": m.timeline_start, "moving": m.timeline_start + 1 + delta}
    if delta > 0:
        expected["previous"] = p.timeline_end - 1
    else:
        expected["following"] = n.timeline_start + 1
    assert {marker.marker_id: marker.frame for marker in changed.markers} == expected
    assert validate_timeline(changed) == []
    loaded = SlideClipCommand.from_dict(json.loads(json.dumps(updated.to_dict())))
    undone = loaded.undo(changed)
    assert undone.to_dict() == seq.to_dict()
    redone, _ = loaded.apply(undone)
    assert redone.to_dict() == changed.to_dict()


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("delta", [-9, 9])
def test_slide_curve_frame_blend_preserves_exact_pairs_and_json_clock(
    reverse: bool, delta: int,
) -> None:
    seq = _sequence(SpeedParams(reverse=reverse, interpolation="frame_blend",
                               speed_points=(SpeedPoint(40, 2, "smooth"),)))
    changed = slide_clip(seq, "moving", delta, 1000, 1000)
    loaded = TimelineSequence.from_dict(json.loads(json.dumps(changed.to_dict())))
    for before, after in zip(seq.video_tracks[0].clips, loaded.video_tracks[0].clips):
        if before.clip_id == "moving":
            continue
        for frame in range(max(before.timeline_start, after.timeline_start),
                           min(before.timeline_end, after.timeline_end)):
            a, b, weight = before.timeline_to_source_frame_pair(frame)
            aa, bb, after_weight = after.timeline_to_source_frame_pair(frame)
            assert (before.source_start + a, before.source_start + b, weight) == (
                after.source_start + aa, after.source_start + bb, after_weight)
        assert after.speed.speed_points == before.speed.speed_points
        assert after.speed.curve_length == before.source_duration


def test_curve_handles_preserve_authored_domain_and_extend_at_endpoint_speeds() -> None:
    speed = SpeedParams(speed_points=(SpeedPoint(40, 2, "smooth"),))
    extended = speed.sliced_by_window(-10, 150, 120, timeline_offset=-10)
    assert extended.curve_offset == -10
    assert extended.curve_length == 120
    assert extended.speed_points == speed.speed_points
    assert extended.source_position_at_time(0, 150) == 0
    assert extended.source_position_at_time(5, 150) == 5
    assert extended.source_position_at_time(10, 150) == 10
    total = speed._curve_position_time(120, 120)
    assert extended.source_position_at_time(total + 13, 150) == 136
    for frame in range(75):
        assert extended.timeline_to_source_frames(frame + 10, 150)[0] - 10 == (
            speed.timeline_to_source_frames(frame, 120)[0])
    restored = SpeedParams.from_dict(json.loads(json.dumps(extended.to_dict())))
    assert restored == extended
    assert restored.timeline_duration_for_source_duration(150) == round(total + 20)
