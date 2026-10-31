"""R-07: DevTools DOM edits map to element property commands, never silently lost."""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core.devtools_property_mapping import (
    diff_native_dom,
    edits_to_update_commands,
)
from src.features.teaching_animation.core.model import (
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)


def _overlay(*declarations: str, element_id: str = "t1", tag: str = "div",
             text: str = "Hello") -> str:
    style = ";".join(declarations)
    if tag == "div":
        return (f'<div data-topos-native-element="{element_id}" '
                f'style="{style}">{text}</div>')
    return (f'<{tag} data-topos-native-element="{element_id}" '
            f'style="{style}">{text}</{tag}>')


def _scene_with_element() -> TeachingSceneDocument:
    return TeachingSceneDocument.from_dict({
        "scene_id": "mapping-test", "name": "Mapping", "width": 1920, "height": 1080,
        "fps_num": 24, "fps_den": 1, "duration_frames": 48,
        "background": {"color": "#111827"},
        "elements": [SceneElement(
            "t1", "text",
            transform=SceneTransform(x=100, y=200, width=800, height=120),
            style={"color": "#ffffff", "font_size": 32.0, "opacity": 1.0},
            content={"text": "Hello"},
        ).to_dict()],
        "animations": [],
    })


# -- whitelisted edits become property commands ---------------------------------

def test_position_color_font_and_text_map_to_properties():
    before = _overlay(
        "position:absolute;left:100px;top:200px", "width:800px", "height:120px",
        "opacity:1.0000", "color:#ffffff", "font-size:32.0000px")
    after = _overlay(
        "position:absolute", "left:140px", "top:260px", "width:800px", "height:120px",
        "opacity:0.5000", "color:#ff8800", "font-size:40px", text="Hello DevTools")
    result = diff_native_dom(before, after)
    assert result.unmappable == ()
    mapped = {(edit.field, edit.property): edit.value for edit in result.edits}
    assert mapped[("transform", "x")] == 140.0
    assert mapped[("transform", "y")] == 260.0
    assert mapped[("style", "opacity")] == 0.5
    assert mapped[("style", "color")] == "#ff8800"
    assert mapped[("style", "font_size")] == 40.0
    assert mapped[("content", "text")] == "Hello DevTools"


def test_unchanged_values_do_not_produce_edits():
    # The overlay formats numbers as 32.0000px; Chromium serializes 32px.
    # Equal-by-value formatting differences must not look like edits.
    before = _overlay("left:100px", "color:#ffffff", "font-size:32.0000px")
    after = _overlay("left:100.0px", "color:#FFFFFF", "font-size:32px")
    from src.features.teaching_animation.core.devtools_property_mapping import DomDiffResult
    assert diff_native_dom(before, after) == DomDiffResult()


def test_transform_rotate_scale_maps_to_rotation_and_scale():
    before = _overlay("transform:rotate(0.0000deg) scale(1.0000, 1.0000)")
    after = _overlay("transform:rotate(45deg) scale(2.0, 1.5000)")
    mapped = {(e.field, e.property): e.value for e in diff_native_dom(before, after).edits}
    assert mapped[("transform", "rotation")] == 45.0
    assert mapped[("transform", "scale_x")] == 2.0
    assert mapped[("transform", "scale_y")] == 1.5


def test_edits_fold_into_one_update_command_per_element():
    before = _overlay("left:100px", "top:200px", "color:#ffffff")
    after = _overlay("left:120px", "top:210px", "color:#ff8800")
    result = diff_native_dom(before, after)
    scene = _scene_with_element()
    commands, unmappable = edits_to_update_commands(scene, result.edits)
    assert unmappable == ()
    assert len(commands) == 1
    updated = commands[0].apply(scene)
    element = updated.element_by_id("t1")
    assert (element.transform.x, element.transform.y) == (120.0, 210.0)
    assert element.style["color"] == "#ff8800"
    assert element.style["font_size"] == 32.0, "untouched style keys must survive"
    assert commands[0].undo(scene).element_by_id("t1").transform.x == 100.0


# -- unmappable changes are reported, never dropped silently --------------------

def test_unmapped_css_property_is_reported():
    before = _overlay("left:100px")
    after = _overlay("left:100px", "filter:blur(4px)")
    result = diff_native_dom(before, after)
    assert result.edits == ()
    assert any("filter" in reason for reason in result.unmappable)


def test_structural_dom_edits_are_reported():
    removed = diff_native_dom(_overlay(element_id="a") + _overlay(element_id="b"),
                              _overlay(element_id="a"))
    added = diff_native_dom(_overlay(element_id="a"),
                            _overlay(element_id="a") + _overlay(element_id="b"))
    assert any("removed" in reason for reason in removed.unmappable)
    assert any("added" in reason for reason in added.unmappable)


def test_unknown_element_ids_are_unmappable():
    scene = _scene_with_element()
    result = diff_native_dom(_overlay(element_id="ghost"),
                             _overlay("left:5px", element_id="ghost"))
    commands, unmappable = edits_to_update_commands(scene, result.edits)
    assert commands == ()
    assert any("ghost" in reason for reason in unmappable)


def test_non_numeric_value_is_reported_not_crashed():
    before = _overlay("left:100px")
    after = _overlay("left:auto")
    result = diff_native_dom(before, after)
    assert result.edits == ()
    assert any("not numeric" in reason for reason in result.unmappable)
