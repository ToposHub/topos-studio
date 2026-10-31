"""Space/playback transport never waits for Motion Graphic caches.

Uncached Motion Graphic clips play as static frames while the Edit
controller's idle scheduler builds their disk caches in the background;
the transport itself must always start immediately.
"""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.shared.media.metadata import Rational


def _motion_graphic_project() -> tuple[EditProject, TimelineSequence]:
    project = EditProject.create("Auto cache")
    asset = MediaAsset(
        asset_id="teaching_animation:scene-1",
        path="topos://motion_graphic/scene-1",
        media_type="motion_graphic",
        display_name="Motion Graphic",
        metadata={"scene_id": "scene-1", "duration_frames": 48},
    )
    project = replace(project, assets={asset.asset_id: asset})
    clip = ClipInstance(
        clip_id="mg-clip",
        asset_id=asset.asset_id,
        track_id="v1",
        timeline_start=0,
        timeline_duration=48,
        source_start=0,
        source_duration=48,
        composite_id="teaching_scene:scene-1",
    )
    timeline = TimelineSequence(
        timeline_id="timeline-1",
        name="Timeline",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(EditTrack("v1", "Graphics", "video", 0, clips=(clip,)),),
    )
    return project, timeline


def test_space_plays_immediately_with_uncached_motion_graphic(qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.features.edit.ui.timeline_panel import TimelinePanel

    project, timeline = _motion_graphic_project()
    panel = TimelinePanel()
    panel.load_timeline(timeline)
    panel.set_project(project)
    requested: list[tuple[str, str]] = []
    panel.render_cache_clip_requested.connect(lambda clip, track: requested.append((clip, track)))
    panel._playback_controller.play = Mock()
    monkeypatch.setattr(
        "src.features.edit.core.render_cache_status.cache_status_for_clip",
        lambda *_args, **_kwargs: "uncached",
    )

    panel.toggle_playback()

    # 空格立即开播：未缓存 MG 片段显示静帧，缓存由 EditController 的
    # 空闲调度器在后台补齐，面板不再于播放前发起缓存请求。
    assert panel._playback_controller.play.call_count == 1
    assert requested == []
    assert not hasattr(panel, "_auto_cache_playback_pending")
    panel.stop_playback()
    panel.deleteLater()
    qapp.processEvents()


def test_space_plays_empty_timeline_as_black(qapp) -> None:  # type: ignore[no-untyped-def]
    """An empty but created timeline still owns a ten-second black range."""
    from src.features.edit.ui.timeline_panel import TimelinePanel

    panel = TimelinePanel()
    panel.show_provisional_timeline()

    panel.toggle_playback()

    assert panel._total_frames >= 240
    assert panel._playback_controller.is_playing
    panel.stop_playback()
    panel.deleteLater()
    qapp.processEvents()


def test_generator_only_playback_uses_newer_view_project(qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A generator-only timeline must not wait for a second media clip.

    The generator drop publishes the timeline command before its immutable
    project/asset snapshot.  Simulate that signal order and verify playback
    still sees the Motion Graphic asset and starts immediately when cached.
    """
    from src.features.edit.ui.timeline_panel import TimelinePanel

    project, timeline = _motion_graphic_project()
    stale_project = replace(project, assets={})
    panel = TimelinePanel()
    panel.load_timeline(timeline)
    panel.set_project(stale_project)
    panel.timeline_view.set_project(project)
    monkeypatch.setattr(
        "src.features.edit.core.render_cache_status.cache_status_for_clip",
        lambda *_args, **_kwargs: "cached",
    )

    panel.toggle_playback()

    assert panel._playback_controller.is_playing
    assert panel._total_frames == 240
    panel.stop_playback()
    panel.deleteLater()
    qapp.processEvents()
