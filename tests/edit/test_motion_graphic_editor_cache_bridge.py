"""Motion Graphic editor → Edit render-cache host bridge."""

from __future__ import annotations

from types import SimpleNamespace

from src.features.teaching_animation.core.model import TeachingSceneDocument
from src.features.teaching_animation.module import TeachingAnimationModule


def test_editor_cache_bridge_prefers_selected_instance() -> None:
    scene = TeachingSceneDocument.create_default("cache bridge")
    selected = SimpleNamespace(
        clip_id="selected", asset_id=f"teaching_animation:{scene.scene_id}",
    )
    other = SimpleNamespace(
        clip_id="other", asset_id=f"teaching_animation:{scene.scene_id}",
    )
    track = SimpleNamespace(track_id="graphics", clips=(selected, other))
    view = SimpleNamespace(
        _timeline=SimpleNamespace(video_tracks=(track,)),
        _selection=SimpleNamespace(selected_clip_ids=("selected",)),
    )
    panel = SimpleNamespace(get_timeline_view=lambda: view)
    calls: list[tuple[str, str]] = []
    main_window = SimpleNamespace(
        timeline_panel=panel,
        request_render_cache_for_clip=lambda clip_id, track_id: (
            calls.append((clip_id, track_id)) or True
        ),
    )

    message = TeachingAnimationModule()._render_motion_graphic_video_cache(
        main_window, scene,
    )

    assert calls == [("selected", "graphics")]
    assert "1 个" in message


def test_editor_cache_bridge_reports_scene_not_on_timeline() -> None:
    scene = TeachingSceneDocument.create_default("not inserted")
    view = SimpleNamespace(
        _timeline=SimpleNamespace(video_tracks=()),
        _selection=SimpleNamespace(selected_clip_ids=()),
    )
    main_window = SimpleNamespace(
        timeline_panel=SimpleNamespace(get_timeline_view=lambda: view),
        request_render_cache_for_clip=lambda _clip_id, _track_id: True,
    )

    message = TeachingAnimationModule()._render_motion_graphic_video_cache(
        main_window, scene,
    )

    assert "尚未在时间线中" in message
