"""R9: the full component lifecycle as one user journey.

create master → instance → instance override → master update (propagation)
→ variant switch → save/reopen → detach (bake) → undo.  The individual
commands had isolated tests; this pins the complete path the audit found
untested, plus core-level coverage for DetachComponentInstanceCommand.
"""

from __future__ import annotations

import json

from src.features.teaching_animation.core.commands import (
    AddComponentInstanceCommand,
    DetachComponentInstanceCommand,
    RegisterComponentMasterCommand,
    SetComponentOverrideCommand,
    SetComponentVariantCommand,
    UpdateComponentMasterCommand,
)
from src.features.teaching_animation.core.components import (
    ComponentInstance,
    ComponentMaster,
    read_component_instances,
    read_component_masters,
)
from src.features.teaching_animation.core.contracts import AnimationFrame
from src.features.teaching_animation.core.model import SceneElement, SceneTransform, TeachingSceneDocument
from src.features.teaching_animation.render.evaluator import evaluate
from src.shared.keyframe import Keyframe, KeyframeTrack


def _card_element(element_id: str, x: float, *, color: str = "#4f8ef7") -> SceneElement:
    return SceneElement(
        element_id=element_id, type="shape",
        transform=SceneTransform(x=x, y=40.0, width=140.0, height=90.0),
        style={"color": color, "opacity": 1.0},
        content={"kind": "rounded_rect"},
        local_animation_tracks=(
            KeyframeTrack("opacity", (Keyframe(0, 0.0), Keyframe(11, 1.0))),
        ),
    )


def _scene() -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id="lifecycle", schema_version=3, name="Lifecycle",
        width=640, height=360, fps_num=24, fps_den=1, duration_frames=12,
        background={"color": "#101010"}, animations=(),
        elements=(
            _card_element("card-a", 40.0),
            _card_element("card-b", 240.0, color="#22c55e"),
        ),
    )


MASTER = ComponentMaster(
    component_id="stat-card",
    display_name="Stat Card",
    element_ids=("card-a",),
    variants={"compact": {"card-a": {"transform": {"x": 8.0, "y": 8.0}}}},
)


def test_full_component_lifecycle_journey() -> None:
    scene = _scene()

    # 1. Create a component master over the selected frame group.
    scene = RegisterComponentMasterCommand(MASTER).apply(scene)
    masters = read_component_masters(scene.metadata)
    assert [m.component_id for m in masters] == ["stat-card"]

    # 2. Insert an instance (reference-based, element count must match).
    scene = AddComponentInstanceCommand(
        ComponentInstance("card-b-instance", "stat-card", ("card-b",))
    ).apply(scene)
    instances = read_component_instances(scene.metadata)
    assert [i.instance_id for i in instances] == ["card-b-instance"]

    # 3. Modify the instance: its override wins over any later master change.
    scene = SetComponentOverrideCommand(
        "card-b-instance", "card-b", {"style": {"color": "#ef4444"}}
    ).apply(scene)
    assert scene.element_by_id("card-b").style["color"] == "#22c55e"  # not baked yet

    # 4. Modify the master: propagation reaches the instance's free fields,
    #    but never clobbers the override.
    scene = UpdateComponentMasterCommand(
        "stat-card", {"card-a": {"transform": {"x": 120.0, "y": 160.0}}}
    ).apply(scene)
    assert scene.element_by_id("card-a").transform.x == 120.0
    assert scene.element_by_id("card-b").transform.x == 120.0  # propagated
    assert scene.element_by_id("card-b").style["color"] == "#22c55e"  # own value untouched

    # 5. Switch variant: evaluate() resolves overrides > variant > master.
    scene = SetComponentVariantCommand("stat-card", "compact").apply(scene)
    evaluated = evaluate(scene, AnimationFrame(6, scene.fps))
    by_id = {item.source.element_id: item for item in evaluated.elements}
    assert by_id["card-a"].transform.x == 8.0
    # The instance follows the variant; its override still wins on style.
    assert by_id["card-b"].transform.x == 8.0
    assert by_id["card-b"].style["color"] == "#ef4444"
    # Local animation survives the whole chain.
    assert by_id["card-b"].opacity == 6.0 / 11.0

    # 6. Save and reopen: component state, overrides and variant survive JSON.
    reopened = TeachingSceneDocument.from_json(scene.to_json())
    assert json.loads(reopened.to_json()) == json.loads(scene.to_json())
    assert read_component_masters(reopened.metadata)[0].variants
    assert read_component_instances(reopened.metadata)[0].overrides["card-b"]["style"]["color"] == "#ef4444"

    # 7. Detach the instance: overrides + resolved variant bake in, the link
    #    disappears, and the master keeps working for future instances.
    detach = DetachComponentInstanceCommand("card-b-instance")
    detached = detach.apply(reopened)
    assert read_component_instances(detached.metadata) == ()
    baked = detached.element_by_id("card-b")
    assert baked.transform.x == 8.0  # resolved variant position baked
    assert baked.style["color"] == "#ef4444"  # override baked
    assert baked.local_animation_tracks  # animation preserved
    assert read_component_masters(detached.metadata)  # master untouched

    # 8. One undo restores the instance link and the pre-detach elements.
    restored = detach.undo(detached)
    assert restored == reopened


def test_detach_bakes_master_style_and_keeps_animations() -> None:
    scene = RegisterComponentMasterCommand(MASTER).apply(_scene())
    scene = AddComponentInstanceCommand(
        ComponentInstance("card-b-instance", "stat-card", ("card-b",))
    ).apply(scene)
    scene = UpdateComponentMasterCommand(
        "stat-card", {"card-a": {"style": {"color": "#111111"}}}
    ).apply(scene)

    detached = DetachComponentInstanceCommand("card-b-instance").apply(scene)
    baked = detached.element_by_id("card-b")
    assert baked.style["color"] == "#111111"
    assert [track.param_name for track in baked.local_animation_tracks] == ["opacity"]
    evaluated = evaluate(detached, AnimationFrame(11, detached.fps))
    baked_evaluated = next(e for e in evaluated.elements if e.source.element_id == "card-b")
    assert baked_evaluated.opacity == 1.0


def test_detach_rejects_unknown_instance() -> None:
    import pytest
    with pytest.raises(ValueError):
        DetachComponentInstanceCommand("ghost").apply(_scene())
