from src.features.edit.core.project import EditProject
from src.features.edit.core.timeline_v2 import TimelineSequence
from src.features.edit.core.edit_project_io import (
    convert_edit_project_to_topos_project,
    convert_topos_project_to_edit_project,
)
from src.features.teaching_animation.core.model import (
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.host_adapters import (
    MotionGraphicClipCommand,
    TimelineSceneFrameProvider,
)
from src.features.teaching_animation.scene_cache import (
    get_shared_scene_cache_coordinator,
)


def _scene(scene_id: str = "virtual-motion-graphic") -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id=scene_id,
        schema_version=1,
        name="HTML 学习演示",
        width=64,
        height=48,
        fps_num=25,
        fps_den=1,
        duration_frames=10,
        background={"color": "#111827"},
        elements=(
            SceneElement(
                "title",
                "text",
                transform=SceneTransform(width=40, height=20),
                content="HTML",
            ),
        ),
        animations=(),
    )


def test_motion_graphic_scene_is_registered_in_media_pool_root_bin() -> None:
    scene = _scene()
    project, asset_id = EditProject.create("Motion Graphic Test").create_motion_graphic_asset(scene)

    asset = project.get_asset_by_id(asset_id)
    assert asset is not None
    assert asset.media_type == "motion_graphic"
    assert asset.is_motion_graphic
    assert asset.path == f"topos://motion_graphic/{scene.scene_id}"
    assert asset.metadata["render_mode"] == "on_demand_cache"
    assert asset.metadata["fps"] == 25.0
    assert asset.metadata["duration_frames"] == scene.duration_frames
    assert asset_id in project.bins[project.root_bin_id].asset_ids

    # Regenerating/editing the same scene updates metadata without duplicating
    # the Media Pool reference.
    updated, updated_id = project.create_motion_graphic_asset(scene, name="Updated")
    assert updated_id == asset_id
    assert updated.bins[updated.root_bin_id].asset_ids.count(asset_id) == 1
    assert updated.get_asset_by_id(asset_id).display_name == "Updated"

    serialized = convert_edit_project_to_topos_project(updated, force_project_mode="edit_project")
    restored = convert_topos_project_to_edit_project(
        serialized,
        full_data={
            "assets": {
                asset_id: {
                    "asset_id": asset_id,
                    "path": asset.path,
                    "media_type": asset.media_type,
                    "display_name": asset.display_name,
                    "metadata": asset.metadata,
                }
            },
            "bins": {
                updated.root_bin_id: {
                    "bin_id": updated.root_bin_id,
                    "name": "Root",
                    "parent_id": None,
                    "asset_ids": [asset_id],
                }
            },
        },
    )
    assert restored.get_asset_by_id(asset_id).is_motion_graphic
    assert restored.get_asset_by_id(asset_id).path == asset.path


def test_motion_graphic_timeline_clip_can_use_virtual_asset_and_cache_frames() -> None:
    scene = _scene("cached-motion-graphic")
    timeline = TimelineSequence(timeline_id="timeline")
    command = MotionGraphicClipCommand(
        scene,
        timeline_start=2,
        timeline_duration=scene.duration_frames,
        mode="overwrite",
        asset_id=f"motion_graphic:{scene.scene_id}",
    )
    timeline = command.apply(timeline)
    clip = timeline.get_clip_by_id(command._clip_id)
    assert clip is not None
    assert clip.asset_id == f"motion_graphic:{scene.scene_id}"

    coordinator = get_shared_scene_cache_coordinator()
    coordinator.cache.clear()
    provider = TimelineSceneFrameProvider({scene.scene_id: scene})
    try:
        first = provider.render_frame(clip, clip.timeline_start)
        stats_after_first = coordinator.cache.stats()
        second = provider.render_frame(clip, clip.timeline_start)
        stats_after_second = coordinator.cache.stats()
    finally:
        provider.close()

    assert first.data == second.data
    assert stats_after_first["entries"] == 1
    assert stats_after_second["hits"] == stats_after_first["hits"] + 1
