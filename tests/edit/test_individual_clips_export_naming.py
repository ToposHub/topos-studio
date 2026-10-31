"""Unit tests for Resolve-style Individual Clips delivery planning.

These intentionally stop before opening an encoder.  They verify the file
plan that prevents same-named source clips from overwriting one another.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from src.features.edit.core.timeline_export import (
    ExportCodec,
    ExportRenderMode,
    ExportRangeType,
    TimelineExportConfig,
    TimelineExporter,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence


def _clip(asset_id: str, start: int, duration: int, clip_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        asset_id=asset_id,
        clip_id=clip_id,
        timeline_start=start,
        timeline_end=start + duration,
        source_duration=duration,
        is_enabled=True,
    )


def _exporter(tmp_path) -> TimelineExporter:
    v1 = _clip("a", 0, 24, "c1")
    v2 = _clip("a", 24, 12, "c2")
    v3 = _clip("b", 0, 36, "c3")
    sequence = SimpleNamespace(
        name="Timeline A",
        duration_frames=36,
        video_tracks=(
            SimpleNamespace(clips=(v1, v2)),
            SimpleNamespace(clips=(v3,)),
        ),
    )
    project = SimpleNamespace(
        get_asset_by_id=lambda asset_id: SimpleNamespace(
            path=f"/media/{'Camera A.mov' if asset_id == 'a' else 'Camera B.mov'}",
            display_name="",
        )
    )
    return TimelineExporter(project, sequence, frame_provider=None)


def test_individual_source_names_get_track_and_clip_identifiers(tmp_path):
    exporter = _exporter(tmp_path)
    config = TimelineExportConfig(
        codec=ExportCodec.H264,
        output_path=str(tmp_path / "deliver.mp4"),
        render_mode=ExportRenderMode.INDIVIDUAL_CLIPS.value,
        filename_use="source",
        unique_filenames=True,
    )

    exports = exporter._build_individual_clip_exports(config)

    assert [item.output_path.rsplit(os.sep, 1)[-1] for item in exports] == [
        "Camera A_V1-0001.mp4",
        "Camera A_V1-0002.mp4",
        "Camera B_V2-0001.mp4",
    ]
    assert [(item.start_frame, item.end_frame) for item in exports] == [(0, 24), (24, 36), (0, 36)]


def test_individual_exports_still_deduplicate_when_unique_toggle_is_off(tmp_path):
    exporter = _exporter(tmp_path)
    config = TimelineExportConfig(
        codec=ExportCodec.H264,
        output_path=str(tmp_path / "deliver.mp4"),
        render_mode=ExportRenderMode.INDIVIDUAL_CLIPS.value,
        filename_use="source",
        unique_filenames=False,
        file_suffix="graded",
        add_source_frame_count=True,
    )

    exports = exporter._build_individual_clip_exports(config)

    assert [item.output_path.rsplit(os.sep, 1)[-1] for item in exports] == [
        "Camera A_24fr_graded.mp4",
        "Camera A_12fr_graded.mp4",
        "Camera B_36fr_graded.mp4",
    ]


def test_individual_custom_name_supports_source_metadata_and_range(tmp_path):
    exporter = _exporter(tmp_path)
    config = TimelineExportConfig(
        codec=ExportCodec.H264,
        output_path=str(tmp_path / "deliver.mp4"),
        range_type=ExportRangeType.CUSTOM,
        start_frame=10,
        end_frame=30,
        render_mode=ExportRenderMode.INDIVIDUAL_CLIPS.value,
        filename_use="custom",
        custom_name="review_{source}",
        unique_filenames=False,
    )

    exports = exporter._build_individual_clip_exports(config)

    assert [item.output_path.rsplit(os.sep, 1)[-1] for item in exports] == [
        "review_Camera A.mp4",
        "review_Camera A_2.mp4",
        "review_Camera B.mp4",
    ]
    assert [(item.start_frame, item.end_frame) for item in exports] == [(10, 24), (24, 30), (10, 30)]


def test_individual_unique_id_digits_are_configurable(tmp_path):
    exporter = _exporter(tmp_path)
    config = TimelineExportConfig(
        codec=ExportCodec.H264,
        output_path=str(tmp_path / "deliver.mp4"),
        render_mode=ExportRenderMode.INDIVIDUAL_CLIPS.value,
        filename_use="source",
        unique_filenames=True,
        unique_filename_digits=10,
    )

    exports = exporter._build_individual_clip_exports(config)

    assert exports[0].output_path.endswith("Camera A_V1-0000000001.mp4")
    assert exports[1].output_path.endswith("Camera A_V1-0000000002.mp4")


def test_individual_clip_handles_extend_source_and_isolate_linked_audio(tmp_path):
    """Handles are real source frames; unrelated timeline media is excluded."""
    video = ClipInstance(
        clip_id="video-1", asset_id="video", track_id="V1",
        timeline_start=50, timeline_duration=20,
        source_start=100, source_duration=20, linked_clip_id="audio-1",
    )
    linked_audio = ClipInstance(
        clip_id="audio-1", asset_id="video", track_id="A1",
        timeline_start=50, timeline_duration=20,
        source_start=100, source_duration=20, linked_clip_id="video-1",
    )
    unrelated_music = ClipInstance(
        clip_id="music-1", asset_id="music", track_id="A2",
        timeline_start=0, timeline_duration=120,
        source_start=0, source_duration=120,
    )
    sequence = TimelineSequence(
        name="Handles", video_tracks=(EditTrack(track_id="V1", clips=(video,)),),
        audio_tracks=(
            EditTrack(track_id="A1", track_type="audio", clips=(linked_audio,)),
            EditTrack(track_id="A2", track_type="audio", clips=(unrelated_music,)),
        ),
    )
    project = SimpleNamespace(
        assets={
            "video": SimpleNamespace(metadata={"duration_frames": 200}),
            "music": SimpleNamespace(metadata={"duration_frames": 120}),
        }
    )
    exporter = TimelineExporter(project, sequence, frame_provider=None)
    config = TimelineExportConfig(
        codec=ExportCodec.H264,
        output_path=str(tmp_path / "deliver.mp4"),
        render_mode=ExportRenderMode.INDIVIDUAL_CLIPS.value,
        frame_handles=25,
    )

    exports = exporter._build_individual_clip_exports(config)

    assert len(exports) == 1
    item = exports[0]
    assert (item.start_frame, item.end_frame) == (25, 95)
    isolated_video = item.sequence.video_tracks[0].clips[0]
    assert (isolated_video.timeline_start, isolated_video.timeline_duration) == (25, 70)
    assert (isolated_video.source_start, isolated_video.source_duration) == (75, 70)
    assert len(item.sequence.audio_tracks) == 1
    isolated_audio = item.sequence.audio_tracks[0].clips[0]
    assert isolated_audio.clip_id == "audio-1"
    assert (isolated_audio.timeline_start, isolated_audio.timeline_duration) == (25, 70)


def test_individual_handles_are_clamped_by_source_and_timeline_start(tmp_path):
    clip = ClipInstance(
        clip_id="video-1", asset_id="video", track_id="V1",
        timeline_start=10, timeline_duration=20,
        source_start=5, source_duration=20,
    )
    sequence = TimelineSequence(
        video_tracks=(EditTrack(track_id="V1", clips=(clip,)),),
    )
    project = SimpleNamespace(
        assets={"video": SimpleNamespace(metadata={"duration_frames": 30})}
    )
    exporter = TimelineExporter(project, sequence, frame_provider=None)
    config = TimelineExportConfig(
        codec=ExportCodec.H264,
        output_path=str(tmp_path / "deliver.mp4"),
        render_mode=ExportRenderMode.INDIVIDUAL_CLIPS.value,
        frame_handles=25,
    )

    item = exporter._build_individual_clip_exports(config)[0]

    # Only five real frames are available before/after the edit.
    assert (item.start_frame, item.end_frame) == (5, 35)
    extended = item.sequence.video_tracks[0].clips[0]
    assert (extended.source_start, extended.source_duration) == (0, 30)
