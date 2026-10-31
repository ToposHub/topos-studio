"""Nested audio windows share timeline sampling and per-instance identities."""
from __future__ import annotations

from dataclasses import replace
import pytest

from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.nested_sequence import (
    _map_clip_into_window, build_nested_from_selection,
    create_nested_sequence_asset, flatten_nested_audio_tracks,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.core.effect_instance import AnimatableParam
from src.shared.keyframe import Keyframe, KeyframeTrack

pytestmark = pytest.mark.edit_core


@pytest.mark.parametrize("speed,source_fps", [
    (SpeedParams(speed=2.0), 24),
    (SpeedParams(speed=2.0, reverse=True), 24),
    (SpeedParams(speed=1.0), 30),
    (SpeedParams(speed=1.0, speed_points=(SpeedPoint(20, 2.0),)), 24),
])
def test_nested_window_retains_every_child_source_sample(speed, source_fps) -> None:
    clip = ClipInstance(
        "child", "source", "a", 0, 50, 100, 160, speed=speed,
        _source_fps_num=source_fps, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1,
        animated_params={"opacity": AnimatableParam(1.0, KeyframeTrack("opacity", keyframes=(
            Keyframe(0, 0.0), Keyframe(49, 1.0))))})
    # Parent frame100 reads child frame20; the retained window is child[20,40).
    mapped = _map_clip_into_window(clip, offset=80, window=(20, 40))
    assert mapped.timeline_start == 100
    assert mapped.timeline_duration == 20
    for frame in range(20):
        assert mapped.timeline_to_source_frame(100 + frame) == clip.timeline_to_source_frame(20 + frame)
        assert mapped.get_param_at_frame("opacity", 100 + frame) == pytest.approx(
            clip.get_param_at_frame("opacity", 20 + frame))


def test_nested_instances_namespace_clip_and_track_identities() -> None:
    child_clip = ClipInstance("child-clip", "source", "a", 0, 20, 0, 20)
    child = TimelineSequence("child", "Child", audio_tracks=(
        EditTrack("a", "A", "audio", 0, clips=(child_clip,)),))
    asset = create_nested_sequence_asset(child)
    first = ClipInstance("first", asset.asset_id, "v", 0, 20, 0, 20)
    second = replace(first, clip_id="second", timeline_start=100)
    parent = TimelineSequence("parent", "Parent", video_tracks=(
        EditTrack("v", "V", "video", 0, clips=(first, second)),))
    tracks = flatten_nested_audio_tracks(parent, {"child": child}.get,
                                         {asset.asset_id: asset}.get)
    assert len(tracks) == 2
    assert len({clip.clip_id for track in tracks for clip in track.clips}) == 2
    assert all(clip.track_id == track.track_id for track in tracks for clip in track.clips)
    assert [track.clips[0].timeline_start for track in tracks] == [0, 100]


@pytest.mark.parametrize("speed", [
    SpeedParams(speed=1.0, reverse=True),
    SpeedParams(speed=1.0, speed_points=(SpeedPoint(5, 2.0),)),
])
def test_parent_reverse_or_curve_does_not_leak_unretimed_audio(speed, caplog) -> None:
    child_clip = ClipInstance("child-clip", "source", "a", 0, 20, 0, 20)
    child = TimelineSequence("child", "Child", audio_tracks=(
        EditTrack("a", "A", "audio", 0, clips=(child_clip,)),))
    asset = create_nested_sequence_asset(child)
    nested = ClipInstance("nested", asset.asset_id, "v", 0, 20, 0, 20, speed=speed)
    parent = TimelineSequence("parent", "Parent", video_tracks=(
        EditTrack("v", "V", "video", 0, clips=(nested,)),))
    assert flatten_nested_audio_tracks(parent, {"child": child}.get,
                                       {asset.asset_id: asset}.get) == ()
    assert "requires parent retime/reverse resampling" in caplog.text


def test_nested_trimmed_source_in_retains_incoming_track_automation_curve() -> None:
    from src.shared.keyframe import InterpolationType
    child_clip = ClipInstance("child-clip", "source", "a", 0, 100, 0, 100)
    automation = KeyframeTrack("volume", keyframes=[
        Keyframe(0, 0.0, InterpolationType.BEZIER, handle_right=(20.0, .05)),
        Keyframe(100, 1.0, handle_left=(-30.0, -.3))])
    child = TimelineSequence("child", "Child", audio_tracks=(
        EditTrack("a", "A", "audio", 0, clips=(child_clip,),
                  automation={"volume": automation}),))
    asset = create_nested_sequence_asset(child)
    nested = ClipInstance("nested", asset.asset_id, "v", 0, 40, 50, 40)
    parent = TimelineSequence("parent", "Parent", video_tracks=(
        EditTrack("v", "V", "video", 0, clips=(nested,)),))
    flattened = flatten_nested_audio_tracks(parent, {"child": child}.get,
                                           {asset.asset_id: asset}.get)
    mapped_automation = flattened[0].automation["volume"]
    assert [key.frame for key in mapped_automation.keyframes] == [-50, 50]
    for frame in range(40):
        assert mapped_automation.get_value(frame) == pytest.approx(automation.get_value(frame + 50))


def test_nesting_selected_clips_cannot_bypass_track_lock() -> None:
    clip = ClipInstance("locked-clip", "source", "v", 0, 20, 0, 20)
    sequence = TimelineSequence("parent", "Parent", video_tracks=(
        EditTrack("v", "V", "video", 0, clips=(clip,), is_locked=True),))
    with pytest.raises(ValueError, match="Track is locked"):
        build_nested_from_selection(sequence, [clip.clip_id])
    assert sequence.video_tracks[0].clips == (clip,)
