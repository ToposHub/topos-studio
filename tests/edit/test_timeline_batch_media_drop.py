"""Regression tests for multi-select media-pool drops."""

from __future__ import annotations

from src.features.edit.core.commands import MacroCommand
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.scene_cut.models import SceneCutKind, SceneSubclipSpec
from src.features.edit.core.scene_cut.subclips import create_subclip_asset
from src.features.edit.ui.timeline_items import ClipItem, DragPreviewItem
from src.features.edit.ui.timeline_view import TimelineView


def test_drag_preview_uses_each_asset_duration(qapp) -> None:
    """A multi-asset drag preview must not paint full-width stacked ghosts."""
    preview = DragPreviewItem(pixels_per_frame=1.0)
    preview.set_assets(
        ["Scene 1", "Scene 2", "Scene 3"],
        ["video", "video", "video"],
        total_duration=90,
        durations=[20, 35, 35],
    )

    assert preview._asset_durations == [20, 35, 35]
    assert preview.boundingRect().width() == 90
    assert preview.shape().boundingRect().width() == 90
    preview.deleteLater()


def test_clip_drag_temporarily_disables_device_cache(qapp) -> None:
    """Moving clips must not reuse stale device-pixel cache edges."""
    item = ClipItem("clip", "track", "asset", 0, 30, 1.0, 0)
    assert item.cacheMode().name == "DeviceCoordinateCache"

    item.set_drag_cache_mode(True)
    assert item.cacheMode().name == "NoCache"

    item.set_drag_cache_mode(False)
    assert item.cacheMode().name == "DeviceCoordinateCache"
    item.deleteLater()


def test_left_drag_uses_full_viewport_repaint_temporarily(qapp) -> None:
    """Live moves must repaint old positions instead of leaving trails.

    规格裁决（P1-EDIT-01）：拖拽期采用 BoundingRectViewportUpdate +
    DontAdjustForAntialiasing——重绘区域为「旧+新」包围盒并集，不留
    残影，且避免 FullViewportUpdate 全量重绘造成的起步顿挫与播放期
    paint 开销（timeline_view.py 实测记录）。结束后恢复 Minimal。
    """
    view = TimelineView()
    assert view.viewportUpdateMode().name == "MinimalViewportUpdate"

    view._set_drag_render_mode(True)
    assert (
        view.viewportUpdateMode().name == "BoundingRectViewportUpdate"
    )
    assert (
        view.optimizationFlags()
        & view.OptimizationFlag.DontAdjustForAntialiasing
    )

    view._set_drag_render_mode(False)
    assert view.viewportUpdateMode().name == "MinimalViewportUpdate"
    assert view.optimizationFlags() & view.OptimizationFlag.DontAdjustForAntialiasing
    view.deleteLater()


def test_scene_cut_batch_drop_is_one_timeline_transaction(qapp) -> None:
    """Batch inserting virtual subclips refreshes the view only once."""
    parent = MediaAsset(
        asset_id="batch-parent",
        path="/tmp/batch-parent.mov",
        media_type="video",
        display_name="batch-parent.mov",
        metadata={
            "fps": 25.0,
            "total_frames": 600,
            "duration_seconds": 24.0,
            "audio_info": {"channels": 2},
        },
    )
    project = EditProject.create("batch-drop-test")
    project.assets[parent.asset_id] = parent

    assets = []
    for index, start in enumerate((0, 80, 180), start=1):
        spec = SceneSubclipSpec(
            parent_asset_id=parent.asset_id,
            source_path=parent.path,
            scene_index=index,
            source_start=start,
            source_duration=40,
            start_seconds=start / 25.0,
            duration_seconds=40 / 25.0,
            display_name=f"Scene {index}",
            start_timecode="",
            end_timecode="",
            thumbnail_source_frame=start,
            detection_confidence=0.9,
            detection_kind=SceneCutKind.HARD_CUT,
        )
        asset = create_subclip_asset(spec, "batch-analysis", parent)
        project.assets[asset.asset_id] = asset
        assets.append(asset)

    view = TimelineView()
    view.set_project(project)
    view._timeline = view._create_default_timeline(parent)
    view.load_timeline(view._timeline)
    assert view._get_waveform_asset_id(assets[0].asset_id) == parent.asset_id
    assert view._get_waveform_asset_id(parent.asset_id) == parent.asset_id
    target_track = view._timeline.video_tracks[0]
    timeline_changes = []
    view.timeline_changed.connect(timeline_changes.append)

    commands = []
    view._batch_drop_commands = commands
    current_frame = 0
    try:
        for asset in assets:
            assert view._add_asset_as_clip(asset, target_track, current_frame)
            current_frame += view._get_asset_duration_frames(asset)
    finally:
        view._batch_drop_commands = None

    assert len(commands) == len(assets)
    assert timeline_changes == []

    view._execute_command(MacroCommand.create("Add scene-cut batch", commands))

    assert len(timeline_changes) == 1
    video_clips = list(view._timeline.video_tracks[0].clips)
    audio_clips = list(view._timeline.audio_tracks[0].clips)
    assert len(video_clips) == len(assets)
    assert len(audio_clips) == len(assets)
    assert all(clip.linked_clip_id for clip in video_clips)
    assert [clip.source_start for clip in video_clips] == [0, 80, 180]

    view.undo()
    assert not view._timeline.video_tracks[0].clips
    assert not view._timeline.audio_tracks[0].clips
    view.deleteLater()
