"""UX-13/15 component and responsive-variant semantics."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.features.teaching_animation.core.commands import (
    AddComponentInstanceCommand,
    RegisterComponentMasterCommand,
    SetComponentOverrideCommand,
    SetComponentVariantCommand,
    SetResponsiveVariantsCommand,
    UpdateComponentMasterCommand,
)
from src.features.teaching_animation.core.components import (
    ComponentInstance,
    ComponentMaster,
    ResponsiveVariant,
    read_active_variants,
    read_component_instances,
    read_component_masters,
)
from src.features.teaching_animation.core.contracts import AnimationFrame
from src.features.teaching_animation.core.model import SceneElement, SceneTransform, TeachingSceneDocument
from src.features.teaching_animation.render.evaluator import evaluate


def _element(element_id: str, x: float, *, color: str = "#fff") -> SceneElement:
    return SceneElement(
        element_id=element_id,
        type="shape",
        transform=SceneTransform(x=x, y=10.0, width=80.0, height=40.0),
        style={"color": color},
        content={"kind": "rect"},
    )


def _scene() -> TeachingSceneDocument:
    base = TeachingSceneDocument.create_default("components", width=320, height=200, duration_frames=12)
    return replace(base, elements=(
        _element("master", 10.0), _element("instance", 10.0, color="#0f0"),
    ))


def _master() -> ComponentMaster:
    return ComponentMaster(
        component_id="card",
        display_name="Card",
        element_ids=("master",),
        variants={"compact": {"master": {"transform": {"x": 2.0, "y": 4.0}}}},
    )


def _with_component() -> TeachingSceneDocument:
    scene = RegisterComponentMasterCommand(_master()).apply(_scene())
    return AddComponentInstanceCommand(
        ComponentInstance("card-instance", "card", ("instance",))).apply(scene)


def test_component_state_is_reference_based_and_round_trips() -> None:
    scene = _with_component()
    assert read_component_masters(scene.metadata)[0].element_ids == ("master",)
    assert read_component_instances(scene.metadata)[0].element_ids == ("instance",)
    assert "elements" not in scene.metadata["components"]


def test_master_update_propagates_but_instance_override_wins() -> None:
    scene = _with_component()
    scene = SetComponentOverrideCommand(
        "card-instance", "instance", {"style": {"color": "#f00"}}).apply(scene)
    scene = UpdateComponentMasterCommand(
        "card", {"master": {"style": {"color": "#00f"}, "transform": {"x": 72.0}}}
    ).apply(scene)
    assert scene.element_by_id("master").transform.x == 72.0
    assert scene.element_by_id("instance").transform.x == 72.0
    assert scene.element_by_id("instance").style["color"] == "#0f0"
    scene = SetComponentVariantCommand("card", "compact").apply(scene)
    evaluated = evaluate(scene, AnimationFrame(0, scene.fps))
    instance = next(item for item in evaluated.elements if item.source.element_id == "instance")
    assert instance.transform.x == 2.0
    assert instance.style["color"] == "#f00"
    assert read_active_variants(scene.metadata) == {"card": "compact"}


def test_component_commands_undo_exact_scene() -> None:
    scene = _scene()
    command = RegisterComponentMasterCommand(_master())
    applied = command.apply(scene)
    assert command.undo(applied) == scene


def test_responsive_variant_is_selected_by_canvas_and_serialized() -> None:
    scene = _scene()
    variant = ResponsiveVariant(
        "portrait", max_width=240,
        overrides={"master": {"transform": {"x": 18.0, "y": 90.0}}},
    )
    scene = SetResponsiveVariantsCommand((variant,)).apply(scene)
    evaluated = evaluate(scene, AnimationFrame(0, scene.fps))
    portrait = next(item for item in evaluated.elements if item.source.element_id == "master")
    assert portrait.transform.x == 10.0  # current canvas is landscape
    portrait_scene = replace(scene, width=200, height=320)
    evaluated = evaluate(portrait_scene, AnimationFrame(0, portrait_scene.fps))
    portrait = next(item for item in evaluated.elements if item.source.element_id == "master")
    assert (portrait.transform.x, portrait.transform.y) == (18.0, 90.0)


def test_invalid_component_instance_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown component"):
        AddComponentInstanceCommand(
            ComponentInstance("bad", "missing", ("instance",))).apply(_scene())
