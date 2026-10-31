"""Phase 1 data-layer tests: model, schema, payload and undo/redo."""

from __future__ import annotations

import json

import pytest

from src.features.teaching_animation.core.commands import (
    AddElementCommand,
    ReorderElementCommand,
    SceneCommandStack,
    SetDurationCommand,
    UpdateElementCommand,
)
from src.features.teaching_animation.core.migration import (
    SceneMigrationError,
    migrate_scene_json,
)
from src.features.teaching_animation.core.model import (
    MotionGraphicClip,
    MotionGraphicNode,
    SCENE_SCHEMA_VERSION,
    SceneAnimation,
    SceneElement,
    SceneTransform,
    TeachingAnimationProjectPayload,
    TeachingSceneDocument,
)
from src.features.teaching_animation.core.template_registry import (
    SceneTemplate,
    create_default_template_registry,
)
from src.features.teaching_animation.core.validator import (
    validate_scene_json,
)
from src.features.teaching_animation.facade import (
    deserialize_project_payload,
    deserialize_scene,
    serialize_project_payload,
    serialize_scene,
)
from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType
from src.shared.project.topos_project import ToposProject
from src.shared.project.topos_project_io import ToposProjectIO


def _scene() -> TeachingSceneDocument:
    return TeachingSceneDocument.create_default(
        name="速度演示",
        scene_id="scene-001",
        width=1280,
        height=720,
        duration_frames=120,
    )


def _text(element_id: str, content: str) -> SceneElement:
    return SceneElement(
        element_id=element_id,
        type="text",
        transform=SceneTransform(x=20.0, y=20.0, width=400.0, height=60.0),
        style={"color": "#ffffff"},
        content=content,
    )


def test_scene_document_roundtrip_preserves_structured_data():
    track = KeyframeTrack(param_name="opacity", min_val=0.0, max_val=1.0)
    track.add_keyframe(Keyframe(0, 0.0, InterpolationType.EASE_OUT))
    track.add_keyframe(Keyframe(30, 1.0, InterpolationType.LINEAR))
    scene = _scene()
    element = _text("definition", "v = s / t")
    animation = SceneAnimation("animation-1", element.element_id, "opacity", track)
    scene = scene.with_element_added(element).with_animation_added(animation)

    encoded = serialize_scene(scene)
    decoded = deserialize_scene(encoded)
    assert decoded == scene
    assert json.loads(decoded.to_json())["animations"][0]["property"] == "opacity"


def test_clip_node_reference_contract_roundtrip():
    payload = TeachingAnimationProjectPayload(
        scenes=(_scene(),),
        clips=(MotionGraphicClip("clip-1", "scene-001", timeline_start=10, timeline_duration=60),),
        nodes=(MotionGraphicNode("node-1", "scene-001", composite_id="comp-1"),),
    )
    decoded = deserialize_project_payload(serialize_project_payload(payload))
    assert decoded == payload
    assert decoded.scene_by_id("scene-001") is not None


def test_template_registry_has_versioned_defaults_and_duplicate_guard():
    registry = create_default_template_registry()
    assert registry.get("title") is not None
    assert registry.get("title").version == 1
    assert registry.register(SceneTemplate("title", 2, "Replacement")) is False
    assert registry.register(SceneTemplate("title", 2, "Replacement"), replace=True) is True
    assert registry.get("title").version == 2


def test_validator_reports_ranges_references_and_keyframe_order():
    invalid = {
        "scene_id": "scene-001",
        "schema_version": 1,
        "name": "Invalid",
        "width": 0,
        "height": 720,
        "fps_num": 24,
        "fps_den": 1,
        "duration_frames": 30,
        "background": {},
        "elements": [{
            "element_id": "element-1",
            "type": "text",
            "transform": {"scale_x": 0.0},
        }],
        "animations": [{
            "animation_id": "animation-1",
            "element_id": "missing",
            "property": "opacity",
            "keyframes": [
                {"frame": 12, "value": 0.0, "interpolation": "linear"},
                {"frame": 4, "value": 1.0, "interpolation": "unknown"},
            ],
        }],
        "asset_refs": [],
    }
    result = validate_scene_json(invalid)
    assert not result.is_valid
    codes = {issue.code for issue in result.errors}
    assert {"range", "reference", "order", "enum"}.issubset(codes)


def test_migration_converts_legacy_seconds_and_preserves_unknown_fields():
    legacy = {
        "scene_id": "legacy",
        "name": "Legacy Scene",
        "fps": 30.0,
        "duration_seconds": 2.0,
        "layers": [{"type": "text", "content": "hello", "layout": {"x": 4}}],
        "future_metadata": {"keep": True},
    }
    migrated = migrate_scene_json(legacy)
    assert migrated["schema_version"] == SCENE_SCHEMA_VERSION
    assert migrated["fps_num"] == 30
    assert migrated["duration_frames"] == 60
    assert migrated["elements"][0]["transform"]["x"] == 4
    assert migrated["future_metadata"] == {"keep": True}
    # v1→v2 additive migration preserves unknown fields and adds the canvas hook.
    assert migrated["canvas"] == {}
    assert migrated["cues"] == []

    with pytest.raises(SceneMigrationError):
        migrate_scene_json({"schema_version": 99})


def test_scene_commands_support_add_update_reorder_duration_undo_redo():
    scene = _scene()
    first = _text("first", "first")
    second = _text("second", "second")
    stack = SceneCommandStack()

    added = stack.execute(AddElementCommand(first), scene)
    added = stack.execute(AddElementCommand(second), added)
    updated = stack.execute(
        UpdateElementCommand("first", {"content": "updated", "z_index": 5}),
        added,
    )
    reordered = stack.execute(ReorderElementCommand("first", 1), updated)
    resized = stack.execute(SetDurationCommand(240), reordered)

    assert resized.duration_frames == 240
    assert resized.elements[1].content == "updated"
    assert stack.can_undo()
    restored = stack.undo(resized)
    assert restored is not None and restored.duration_frames == 120
    restored = stack.undo(restored)
    assert restored is not None and restored.element_by_id("first") is not None
    redone = stack.redo(restored)
    assert redone is not None and redone.elements[1].element_id == "first"


def test_typed_payload_can_be_saved_without_touching_timeline_schema(tmp_path):
    path = str(tmp_path / "phase1.topos")
    project = ToposProject.create_single_asset("Phase 1", "/tmp/phase1.jpg")
    payload = TeachingAnimationProjectPayload(scenes=(_scene(),))
    assert ToposProjectIO.save(project, path, teaching_animation_data=payload)
    loaded, full_data = ToposProjectIO.load(path)
    assert loaded is not None and full_data is not None
    decoded = deserialize_project_payload(full_data["teaching_animation"])
    assert decoded.scenes[0].scene_id == "scene-001"
    assert "teaching_animation" not in loaded.to_dict().get("timelines", {})
