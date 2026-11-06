"""Regression tests for multi-select media-pool drops."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PyQt6.QtCore import QMimeData, QPointF, QUrl, Qt
from PyQt6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent
from PyQt6.QtWidgets import QApplication

from src.features.edit.core.commands import MacroCommand
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.scene_cut.models import SceneCutKind, SceneSubclipSpec
from src.features.edit.core.scene_cut.subclips import create_subclip_asset
from src.features.edit.ui.timeline_items import ClipItem, DragPreviewItem
from src.features.edit.ui.timeline_view import MIME_TYPE_MEDIA_POOL_ASSETS, TimelineView


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


@pytest.mark.parametrize("commit", [False, True])
def test_media_pool_drag_previews_on_new_video_lane_and_clears(
    qapp, qtbot, commit: bool
) -> None:
    asset = MediaAsset("drag-video", "/tmp/drag-video.mov", "video", "Video", {
        "fps": 25.0, "duration_seconds": 0, "total_frames": 50,
    })
    project = EditProject.create("drag-preview")
    project.assets[asset.asset_id] = asset
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1200, 650)
    view.set_project(project)
    timeline = view._create_default_timeline(asset)
    view.load_timeline(timeline)
    view.show()
    qapp.processEvents()
    view.set_zoom(2.0)

    mime = QMimeData()
    mime.setData(MIME_TYPE_MEDIA_POOL_ASSETS, json.dumps([asset.asset_id]).encode())
    action = Qt.DropAction.CopyAction
    button = Qt.MouseButton.LeftButton
    modifiers = Qt.KeyboardModifier.NoModifier
    v1 = view._layout_engine.track_rect(timeline.video_tracks[0].track_id)
    x = view._layout_engine.frame_to_x(40)
    point = view.mapFromScene(QPointF(x, v1.top() - 35))
    enter = QDragEnterEvent(point, action, mime, button, modifiers)
    QApplication.sendEvent(view.viewport(), enter)

    preview = view._drag_preview_item
    assert enter.isAccepted()
    assert preview.isVisible()
    assert preview.y() + preview.boundingRect().height() < v1.top()
    assert preview.boundingRect().width() == 100.0
    assert view.get_timeline() is timeline

    view.set_playhead(80)
    destination = view.mapFromScene(QPointF(x + 82, v1.center().y()))
    move = QDragMoveEvent(destination, action, mime, button, modifiers)
    QApplication.sendEvent(view.viewport(), move)
    assert move.isAccepted()
    assert preview.x() == x + 80
    assert preview.y() == v1.top()
    assert preview.boundingRect().height() == v1.height() - 2

    if commit:
        drop = QDropEvent(QPointF(destination), action, mime, button, modifiers)
        QApplication.sendEvent(view.viewport(), drop)
        assert drop.isAccepted()
        clip, = view.get_timeline().video_tracks[0].clips
        assert clip.timeline_start == 80
        assert clip.timeline_duration == 50
    else:
        QApplication.sendEvent(view.viewport(), QDragLeaveEvent())
        assert view.get_timeline() is timeline
    assert not preview.isVisible()
    assert not view._snap_indicator.is_visible()


def test_short_media_preview_stays_visible_at_minimum_zoom(qapp) -> None:
    preview = DragPreviewItem(pixels_per_frame=0.01)
    preview.show_preview(0, 100.0, 54.0, ["One frame"], ["video"], 1)
    assert preview.boundingRect().width() == preview.MIN_WIDTH
    assert preview.boundingRect().height() == 52.0


def test_external_file_drag_shows_placeholder_before_import(qapp, qtbot, tmp_path: Path) -> None:
    file = tmp_path / "unimported.mov"
    file.touch()
    project = EditProject.create("external-preview")
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1000, 600)
    view.set_project(project)
    timeline = view.show_provisional_timeline()
    view.show()
    qapp.processEvents()

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(file))])
    track = view._layout_engine.track_rect(timeline.video_tracks[0].track_id)
    x = view._layout_engine.frame_to_x(30)
    point = view.mapFromScene(QPointF(x, track.center().y()))
    event = QDragEnterEvent(
        point, Qt.DropAction.CopyAction, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(view.viewport(), event)
    assert event.isAccepted()
    assert view._drag_preview_item.isVisible()
    assert view._drag_preview_item.y() == track.top()
    assert view._drag_preview_item.boundingRect().width() >= 64.0
    assert not project.assets
    assert view.has_provisional_timeline()
    QApplication.sendEvent(view.viewport(), QDragLeaveEvent())
    assert not view._drag_preview_item.isVisible()
