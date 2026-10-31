"""Motion Graphic duration retiming tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.features.edit.core.commands import RetimeMotionGraphicClipCommand
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.host_adapters import TimelineSceneFrameProvider
from src.shared.media.metadata import Rational


def _timeline(duration: int = 375) -> TimelineSequence:
    clip = ClipInstance(
        clip_id="mg-clip",
        asset_id="teaching_animation:scene-1",
        track_id="video-1",
        timeline_start=0,
        timeline_duration=duration,
        source_start=0,
        source_duration=375,
    )
    track = EditTrack(
        track_id="video-1", name="Graphics", track_type="video", index=0,
        clips=(clip,),
    )
    return TimelineSequence(
        timeline_id="timeline-1", name="Test", fps=Rational(25, 1),
        width=1920, height=1080, sample_rate=48000, video_tracks=(track,),
    )


def test_retime_command_changes_timeline_only_and_undoes() -> None:
    timeline = _timeline()
    command = RetimeMotionGraphicClipCommand.create(
        "video-1", "mg-clip", new_duration=125, old_duration=375,
    )

    retimed = command.apply(timeline)
    clip = retimed.get_clip_by_id("mg-clip")
    assert clip is not None
    assert clip.timeline_duration == 125
    assert clip.source_duration == 375
    assert clip.timeline_to_source_frame(0) == 0
    assert clip.timeline_to_source_frame(124) == 374

    restored = command.undo(retimed).get_clip_by_id("mg-clip")
    assert restored is not None
    assert restored.timeline_duration == 375
    assert restored.source_duration == 375


def test_motion_graphic_provider_uses_same_normalized_mapping_as_clip() -> None:
    scene = TeachingSceneDocument.create_default(
        scene_id="scene-1", duration_frames=375,
    )
    provider = TimelineSceneFrameProvider({scene.scene_id: scene})
    lightweight_clip = SimpleNamespace(
        asset_id="teaching_animation:scene-1",
        timeline_start=0,
        timeline_end=125,
        source_start=0,
        source_duration=375,
    )
    try:
        assert provider.local_frame(lightweight_clip, 0) == 0
        assert provider.local_frame(lightweight_clip, 62) == 187
        assert provider.local_frame(lightweight_clip, 124) == 374
    finally:
        provider.close()


def test_retime_command_rejects_non_motion_graphic_clip() -> None:
    clip = ClipInstance(
        clip_id="video-clip", asset_id="video.mov", track_id="video-1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
    )
    timeline = TimelineSequence(
        timeline_id="timeline-1", name="Test", fps=Rational(25, 1),
        width=1920, height=1080, sample_rate=48000,
        video_tracks=(EditTrack(
            track_id="video-1", name="Video", track_type="video", index=0,
            clips=(clip,),
        ),),
    )
    command = RetimeMotionGraphicClipCommand.create(
        "video-1", "video-clip", new_duration=50, old_duration=100,
    )
    with pytest.raises(ValueError, match="Motion Graphic"):
        command.apply(timeline)
