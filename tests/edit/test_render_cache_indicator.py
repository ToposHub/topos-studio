from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.serialization import EditProjectSerializer
from src.features.edit.core.render_cache import (
    RenderCacheManifest,
    RenderCacheSettings,
    RenderCacheStatus,
)
from src.features.edit.core.render_cache_status import (
    STATUS_CACHED,
    STATUS_UNCACHED,
    build_cache_indicator_segments,
    cache_status_for_clip,
)
from src.features.edit.core.timeline_v2 import ClipInstance


def _clip(clip_id: str, start: int, duration: int, *, composite_id: str | None = None) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id="asset-1",
        track_id="v1",
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=duration,
        composite_id=composite_id,
    )


def _project_with_video_asset() -> EditProject:
    project = EditProject.create("Cache indicator")
    asset = MediaAsset(
        asset_id="asset-1",
        path="/tmp/source.mp4",
        media_type="video",
        display_name="Source",
        metadata={},
    )
    bins = dict(project.bins)
    bins[project.root_bin_id] = bins[project.root_bin_id].with_asset_added(asset.asset_id)
    return replace(project, assets={asset.asset_id: asset}, bins=bins)


def test_direct_source_media_has_no_indicator_segment() -> None:
    project = _project_with_video_asset()
    timeline = SimpleNamespace(
        timeline_id="tl-1",
        all_tracks=[SimpleNamespace(clips=[_clip("source", 0, 25)])],
    )
    assert build_cache_indicator_segments(timeline, project) == ()


def test_uncached_and_cached_composite_ranges_are_colored() -> None:
    project = _project_with_video_asset()
    uncached = _clip("uncached", 0, 25, composite_id="graph-1")
    cached = _clip("cached", 25, 25, composite_id="graph-2")
    manifest = RenderCacheManifest(
        cache_id="cache-2",
        timeline_id="tl-1",
        clip_id="cached",
        composite_id="graph-2",
        path="",
        status=RenderCacheStatus.VALID,
    )
    project = project.set_render_cache_manifest(manifest)
    timeline = SimpleNamespace(
        timeline_id="tl-1",
        all_tracks=[SimpleNamespace(clips=[uncached, cached])],
    )

    segments = build_cache_indicator_segments(timeline, project)
    assert [(s.start_frame, s.end_frame, s.status) for s in segments] == [
        (0, 25, STATUS_UNCACHED),
        (25, 50, STATUS_CACHED),
    ]


def test_overlapping_uncached_range_wins_over_cached_range() -> None:
    project = _project_with_video_asset()
    cached = _clip("cached", 0, 50, composite_id="graph-1")
    uncached = _clip("uncached", 25, 25, composite_id="graph-2")
    project = project.set_render_cache_manifest(
        RenderCacheManifest(
            cache_id="cache-1",
            timeline_id="tl-1",
            clip_id="cached",
            composite_id="graph-1",
            status=RenderCacheStatus.VALID,
        )
    )
    timeline = SimpleNamespace(
        timeline_id="tl-1",
        all_tracks=[SimpleNamespace(clips=[cached, uncached])],
    )
    segments = build_cache_indicator_segments(timeline, project)
    assert [(s.start_frame, s.end_frame, s.status) for s in segments] == [
        (0, 25, STATUS_CACHED),
        (25, 50, STATUS_UNCACHED),
    ]


def test_motion_graphic_indicator_uses_authored_scene_duration(monkeypatch) -> None:
    """A stale overlong clip must not wait for impossible scene frames."""
    project = _project_with_video_asset()
    asset = replace(
        project.assets["asset-1"],
        media_type="motion_graphic",
        path="topos://motion_graphic/scene-1",
        metadata={"scene_id": "scene-1", "duration_frames": 3},
    )
    project = replace(project, assets={"asset-1": asset})
    clip = _clip("mg", 0, 5, composite_id="teaching_scene:scene-1")
    timeline = SimpleNamespace(
        timeline_id="tl-1",
        all_tracks=[SimpleNamespace(clips=[clip])],
    )
    calls: list[tuple[str, int, int]] = []

    class _Cache:
        def is_range_cached(self, scene_id: str, start: int, end: int) -> bool:
            calls.append((scene_id, start, end))
            return True

    monkeypatch.setattr(
        "src.features.teaching_animation.scene_cache.get_existing_scene_cache_coordinator",
        lambda: SimpleNamespace(cache=_Cache()),
    )
    assert build_cache_indicator_segments(timeline, project)[0].status == STATUS_CACHED
    assert calls == [("scene-1", 0, 3)]


def test_motion_graphic_valid_manifest_refresh_does_not_raise() -> None:
    """Refreshing the ruler after cache submission validates scene identity."""
    project = _project_with_video_asset()
    asset = replace(
        project.assets["asset-1"],
        media_type="motion_graphic",
        path="topos://motion_graphic/scene-1",
        metadata={"scene_id": "scene-1", "duration_frames": 3},
    )
    project = replace(project, assets={"asset-1": asset})
    clip = _clip("mg", 0, 3, composite_id="teaching_scene:scene-1")
    project = project.set_render_cache_manifest(
        RenderCacheManifest(
            cache_id="cache-mg",
            timeline_id="tl-1",
            clip_id="mg",
            path="",
            codec="vp8_high_quality",
            container="webm",
            start_frame=0,
            duration_frames=3,
            scene_id="scene-1",
            render_kind="motion_graphic",
            status=RenderCacheStatus.VALID,
        )
    )
    timeline = SimpleNamespace(
        timeline_id="tl-1",
        all_tracks=[SimpleNamespace(clips=[clip])],
    )

    segments = build_cache_indicator_segments(timeline, project)
    assert [(item.start_frame, item.end_frame, item.status) for item in segments] == [
        (0, 3, STATUS_CACHED),
    ]


def test_legacy_motion_graphic_prores_cache_is_not_reported_green() -> None:
    """Old ProRes/MOV caches are invalid after the VP8/40 Mbps contract."""
    project = _project_with_video_asset()
    asset = replace(
        project.assets["asset-1"],
        media_type="motion_graphic",
        path="topos://motion_graphic/scene-1",
        metadata={"scene_id": "scene-1", "duration_frames": 3},
    )
    project = replace(project, assets={"asset-1": asset})
    clip = _clip("mg", 0, 3, composite_id="teaching_scene:scene-1")
    project = project.set_render_cache_manifest(
        RenderCacheManifest(
            cache_id="legacy-mg",
            timeline_id="tl-1",
            clip_id="mg",
            path="/tmp/clip_mg.mov",
            codec="prores_422_proxy",
            container="mov",
            start_frame=0,
            duration_frames=3,
            scene_id="scene-1",
            render_kind="motion_graphic",
            status=RenderCacheStatus.VALID,
        )
    )
    timeline = SimpleNamespace(
        timeline_id="tl-1",
        all_tracks=[SimpleNamespace(clips=[clip])],
    )

    segments = build_cache_indicator_segments(timeline, project)
    assert segments[0].status == STATUS_UNCACHED


def test_motion_graphic_cache_fps_must_match_current_timeline() -> None:
    """A cache from another sequence FPS is stale even when dimensions match."""
    project = _project_with_video_asset()
    asset = replace(
        project.assets["asset-1"],
        media_type="motion_graphic",
        path="topos://motion_graphic/scene-1",
        metadata={"scene_id": "scene-1", "duration_frames": 3},
    )
    project = replace(
        project,
        assets={"asset-1": asset},
        timelines={
            "tl-1": SimpleNamespace(width=1920, height=1080, fps=30.0),
        },
    )
    clip = _clip("mg", 0, 3, composite_id="teaching_scene:scene-1")
    project = project.set_render_cache_manifest(
        RenderCacheManifest(
            cache_id="cache-mg",
            timeline_id="tl-1",
            clip_id="mg",
            path="",
            codec="vp8_high_quality",
            container="webm",
            width=1920,
            height=1080,
            timeline_fps="25/1",
            start_frame=0,
            duration_frames=3,
            scene_id="scene-1",
            render_kind="motion_graphic",
            status=RenderCacheStatus.VALID,
        )
    )
    assert cache_status_for_clip(project, "tl-1", clip) == STATUS_UNCACHED

    matching_project = replace(
        project,
        render_cache_manifests={
            key: RenderCacheManifest.from_dict(value).to_dict()
            | {"timeline_fps": "30/1"}
            for key, value in project.render_cache_manifests.items()
        },
    )
    assert cache_status_for_clip(matching_project, "tl-1", clip) == STATUS_CACHED


def test_timeline_duplicate_reuses_scene_cache_without_new_manifest() -> None:
    """Two timeline instances of one scene share the same baked recording."""
    project = _project_with_video_asset()
    asset = replace(
        project.assets["asset-1"],
        media_type="motion_graphic",
        path="topos://motion_graphic/scene-1",
        metadata={"scene_id": "scene-1", "duration_frames": 10},
    )
    project = replace(
        project,
        assets={"asset-1": asset},
        timelines={"tl-1": SimpleNamespace(width=1920, height=1080, fps=25.0)},
    )
    first = _clip("first", 0, 10, composite_id="teaching_scene:scene-1")
    duplicate = _clip("duplicate", 40, 6, composite_id="teaching_scene:scene-1")
    project = project.set_render_cache_manifest(
        RenderCacheManifest(
            cache_id="cache-mg",
            timeline_id="tl-1",
            clip_id="first",
            path="",
            codec="vp8_high_quality",
            container="webm",
            width=1920,
            height=1080,
            timeline_fps="25/1",
            duration_frames=10,
            scene_id="scene-1",
            render_kind="motion_graphic",
            status=RenderCacheStatus.VALID,
        )
    )
    assert cache_status_for_clip(project, "tl-1", duplicate) == STATUS_CACHED


def test_render_cache_settings_roundtrip_container_and_extension() -> None:
    settings = RenderCacheSettings(codec="h264_proxy", container="mp4")
    restored = RenderCacheSettings.from_dict(settings.to_dict())
    assert restored.codec == "h264_proxy"
    assert restored.file_extension == "mp4"


def test_project_serialization_preserves_cache_manifest_and_settings() -> None:
    project = _project_with_video_asset()
    project = replace(
        project,
        render_cache_settings=RenderCacheSettings(codec="prores_422", container="mov"),
        render_cache_manifests={
            "cache-1": RenderCacheManifest(
                cache_id="cache-1",
                timeline_id="tl-1",
                clip_id="clip-1",
                path="/tmp/cache.mov",
                codec="prores_422",
                container="mov",
                status=RenderCacheStatus.VALID,
            ).to_dict()
        },
    )
    restored = EditProjectSerializer.from_json(EditProjectSerializer.to_json(project))
    assert restored.render_cache_settings["codec"] == "prores_422"
    assert restored.render_cache_manifests["cache-1"]["container"] == "mov"
