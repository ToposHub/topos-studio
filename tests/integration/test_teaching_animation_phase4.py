from src.features.composite.facade import get_teaching_animation_node_provider
from src.features.edit.commands_facade import create_teaching_animation_clip_command
from src.features.edit.core.commands import CommandStack
from src.features.edit.core.timeline_v2 import TimelineSequence
from src.features.teaching_animation.core.model import SceneElement, SceneTransform, TeachingSceneDocument
from src.features.teaching_animation.host_adapters import (
    MotionGraphicNodeProvider,
    SceneChangeBus,
    SceneReferenceManifest,
    TimelineSceneFrameProvider,
    get_scene_reference_manifest,
    insert_motion_graphic_clip,
)


def _scene() -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id="lesson-scene",
        schema_version=1,
        name="Lesson",
        width=64,
        height=48,
        fps_num=24,
        fps_den=1,
        duration_frames=12,
        background={"color": "#111827"},
        elements=(SceneElement("title", "text", transform=SceneTransform(width=40, height=20), content="Hello"),),
        animations=(),
    )


def test_edit_facade_inserts_graphics_clip_with_manifest_and_undo() -> None:
    scene = _scene()
    timeline = TimelineSequence(timeline_id="timeline")
    stack = CommandStack()
    command = create_teaching_animation_clip_command(scene, timeline_start=3, mode="append")
    timeline = stack.execute(command, timeline)
    clip = timeline.get_clip_by_id(command._clip_id)
    assert clip is not None
    assert timeline.get_track_by_id(clip.track_id).track_role == "graphics"
    manifest = get_scene_reference_manifest(timeline, clip.clip_id)
    assert manifest == SceneReferenceManifest.from_scene(scene)
    timeline = stack.undo(timeline)
    assert timeline.get_clip_by_id(clip.clip_id) is None
    assert timeline.get_track_by_id(clip.track_id) is None


def test_append_and_overwrite_modes_and_same_scene_node_reference() -> None:
    scene = _scene()
    timeline = TimelineSequence(timeline_id="timeline")
    first = insert_motion_graphic_clip(timeline, scene, timeline_start=0, mode="append")
    second = insert_motion_graphic_clip(first.timeline, scene, timeline_start=0, mode="append", track_id=first.track_id)
    assert second.clip.timeline_start == first.clip.timeline_end
    overwritten = insert_motion_graphic_clip(second.timeline, scene, timeline_start=2, timeline_duration=3, track_id=first.track_id, mode="overwrite")
    assert overwritten.clip.timeline_start == 2
    assert overwritten.clip.timeline_duration == 3
    assert get_scene_reference_manifest(overwritten.timeline, overwritten.clip.clip_id).scene_id == scene.scene_id
    node = MotionGraphicNodeProvider().create_node(scene, "node-1")
    assert node.scene_id == scene.scene_id


def test_timeline_preview_and_composite_provider_return_rgba() -> None:
    scene = _scene()
    result = insert_motion_graphic_clip(TimelineSequence(timeline_id="timeline"), scene)
    frame = TimelineSceneFrameProvider({scene.scene_id: scene}).render_frame(result.clip, result.clip.timeline_start)
    assert frame.pixel_format == "rgba8"
    assert len(frame.data) == scene.width * scene.height * 4
    provider = get_teaching_animation_node_provider()
    assert provider is not None
    assert provider.descriptor.node_type == "motion_graphic"
    node_frame = provider.render_node(scene, 0)
    assert node_frame.data == frame.data


def test_scene_change_bus_notifies_clip_and_node_consumers() -> None:
    bus = SceneChangeBus()
    received = []
    bus.subscribe(received.append)
    scene = _scene()
    bus.publish(scene)
    assert received == [scene]

