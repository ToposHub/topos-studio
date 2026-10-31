"""
Tests for clip speed/retime functionality.

Verifies:
- ChangeClipSpeedCommand apply/undo
- Speed affects timeline_duration
- Speed frame mapping consistency
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.commands import ChangeClipSpeedCommand
from src.features.edit.core.clip_params import SpeedParams
from src.shared.media.metadata import Rational


def _make_clip(clip_id: str = "c1", track_id: str = "v1",
               start: int = 0, duration: int = 100, source_duration: int = 100) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=f"a_{clip_id}", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=source_duration,
        speed=SpeedParams(speed=1.0),
    )


def _make_timeline(clips=None) -> TimelineSequence:
    clips = clips or [_make_clip()]
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=tuple(clips))
    return TimelineSequence(
        timeline_id="tl1", name="Test", fps=Rational(24, 1),
        video_tracks=(track,),
    )


class TestChangeClipSpeedCommand:
    """Verify ChangeClipSpeedCommand apply and undo."""

    def test_apply_speed_2x(self):
        tl = _make_timeline([_make_clip(source_duration=100)])
        cmd = ChangeClipSpeedCommand.create("c1", "v1", 2.0)
        new_tl, updated_cmd = cmd.apply(tl)
        clip = new_tl.get_clip_by_id("c1")
        assert isinstance(clip.speed, SpeedParams)
        assert clip.speed.speed == 2.0
        assert clip.timeline_duration == 50  # 100 / 2

    def test_apply_speed_0_5x(self):
        tl = _make_timeline([_make_clip(source_duration=100)])
        cmd = ChangeClipSpeedCommand.create("c1", "v1", 0.5)
        new_tl, updated_cmd = cmd.apply(tl)
        clip = new_tl.get_clip_by_id("c1")
        assert clip.speed.speed == 0.5
        assert clip.timeline_duration == 200  # 100 / 0.5

    def test_undo_restores_original(self):
        tl = _make_timeline([_make_clip(source_duration=100)])
        cmd = ChangeClipSpeedCommand.create("c1", "v1", 2.0)
        new_tl, updated_cmd = cmd.apply(tl)
        restored = updated_cmd.undo(new_tl)
        clip = restored.get_clip_by_id("c1")
        assert clip.speed.speed == 1.0
        assert clip.timeline_duration == 100

    def test_speed_1x_no_change(self):
        tl = _make_timeline([_make_clip(source_duration=100)])
        cmd = ChangeClipSpeedCommand.create("c1", "v1", 1.0)
        new_tl, _ = cmd.apply(tl)
        clip = new_tl.get_clip_by_id("c1")
        assert clip.speed.speed == 1.0
        assert clip.timeline_duration == 100


class TestSpeedDurationRelationship:
    """Verify speed/duration mathematical relationship."""

    def test_speed_4x_quarters_duration(self):
        tl = _make_timeline([_make_clip(source_duration=200)])
        cmd = ChangeClipSpeedCommand.create("c1", "v1", 4.0)
        new_tl, _ = cmd.apply(tl)
        clip = new_tl.get_clip_by_id("c1")
        assert clip.timeline_duration == 50

    def test_speed_0_25x_quadruples_duration(self):
        tl = _make_timeline([_make_clip(source_duration=100)])
        cmd = ChangeClipSpeedCommand.create("c1", "v1", 0.25)
        new_tl, _ = cmd.apply(tl)
        clip = new_tl.get_clip_by_id("c1")
        assert clip.timeline_duration == 400

    def test_source_duration_preserved(self):
        tl = _make_timeline([_make_clip(source_duration=100)])
        cmd = ChangeClipSpeedCommand.create("c1", "v1", 2.0)
        new_tl, _ = cmd.apply(tl)
        clip = new_tl.get_clip_by_id("c1")
        assert clip.source_duration == 100


class TestSpeedCommandEdgeCases:
    """Verify edge cases for speed commands."""

    def test_invalid_track_raises(self):
        tl = _make_timeline()
        cmd = ChangeClipSpeedCommand.create("c1", "nonexistent", 2.0)
        with pytest.raises(ValueError):
            cmd.apply(tl)

    def test_invalid_clip_raises(self):
        tl = _make_timeline()
        cmd = ChangeClipSpeedCommand.create("nonexistent", "v1", 2.0)
        with pytest.raises(ValueError):
            cmd.apply(tl)


class TestResolveStyleSpeedPoints:
    """Speed points remain stable in source space as clip duration changes."""

    def test_speed_point_maps_piecewise_timeline_frames(self):
        params = SpeedParams(speed=1.0).with_speed_point(50, 100)
        params = params.with_speed_point_speed(50, 0.5)
        clip = _make_clip(source_duration=100).with_speed_params(params)

        # First 50 source frames play at 100%, second half at 50%.
        assert clip.timeline_duration == 150
        assert clip.timeline_to_source_frame(49) == 49
        assert clip.timeline_to_source_frame(50) == 50
        assert clip.timeline_to_source_frame(149) == 99

    def test_speed_point_change_updates_duration_and_undo(self):
        params = SpeedParams(speed=1.0).with_speed_point(50, 100)
        params = params.with_speed_point_speed(50, 2.0)
        tl = _make_timeline([_make_clip(source_duration=100)])
        cmd = ChangeClipSpeedCommand(
            command_id="retime-point",
            label="Change retime segment",
            clip_id="c1",
            track_id="v1",
            new_speed=params,
        )

        retimed, applied = cmd.apply(tl)
        assert retimed.get_clip_by_id("c1").timeline_duration == 75
        restored = applied.undo(retimed)
        assert restored.get_clip_by_id("c1").timeline_duration == 100

    def test_speed_points_round_trip_through_clip_serialization(self):
        params = SpeedParams(speed=1.0).with_speed_point(40, 100)
        params = params.with_speed_point_speed(40, 0.5)
        clip = _make_clip(source_duration=100).with_speed_params(params)

        restored = type(clip).from_dict(clip.to_dict())
        assert isinstance(restored.speed, SpeedParams)
        assert restored.speed.speed_points == params.speed_points
        assert restored.timeline_to_source_frame(40) == 40


class TestSpeedAnimationRemap:
    """F2.4：变速重锚时 clip 动画关键帧按比例重映射（不再留原绝对帧位）。"""

    def _animated_clip(self) -> ClipInstance:
        from src.features.edit.core.commands import AddKeyframeCommand

        clip = _make_clip(start=100, duration=48, source_duration=48)
        timeline = _make_timeline([clip])
        timeline = AddKeyframeCommand.create(
            "v1", "c1", "opacity", 100, 1.0).apply(timeline)
        timeline = AddKeyframeCommand.create(
            "v1", "c1", "opacity", 147, 0.0).apply(timeline)
        return timeline.video_tracks[0].get_clip_by_id("c1"), timeline

    def test_2x_remaps_keyframes_into_new_span(self):
        clip, timeline = self._animated_clip()
        cmd = ChangeClipSpeedCommand.create("c1", "v1", 2.0)
        new_timeline, _updated = cmd.apply(timeline)

        new_clip = new_timeline.video_tracks[0].get_clip_by_id("c1")
        assert new_clip.timeline_duration == 24
        kfs = new_clip.animated_params["opacity"].keyframes.keyframes
        frames = [kf.frame for kf in kfs]
        # 首帧锚定 clip 起点；尾帧按比例 47*24/48 落入新 span 内
        assert frames[0] == 100
        assert frames[-1] in (123, 124)
        assert all(100 <= f <= 124 for f in frames)

    def test_speed_1x_keeps_keyframes(self):
        clip, timeline = self._animated_clip()
        new_timeline, _updated = ChangeClipSpeedCommand.create(
            "c1", "v1", 1.0).apply(timeline)
        new_clip = new_timeline.video_tracks[0].get_clip_by_id("c1")
        frames = [kf.frame for kf in
                  new_clip.animated_params["opacity"].keyframes.keyframes]
        assert frames == [100, 147]

    def test_undo_restores_keyframes(self):
        clip, timeline = self._animated_clip()
        cmd = ChangeClipSpeedCommand.create("c1", "v1", 0.5)
        new_timeline, updated_cmd = cmd.apply(timeline)
        restored = updated_cmd.undo(new_timeline)
        clip2 = restored.video_tracks[0].get_clip_by_id("c1")
        frames = [kf.frame for kf in
                  clip2.animated_params["opacity"].keyframes.keyframes]
        assert clip2.timeline_duration == 48
        assert frames == [100, 147]
