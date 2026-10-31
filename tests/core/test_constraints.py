"""R1: canvas constraints and named breakpoints (pure logic + evaluator)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.features.teaching_animation.core.constraints import (
    BASE_CANVAS_KEY,
    BREAKPOINTS_KEY,
    Breakpoint,
    CanvasConstraints,
    DEFAULT_BREAKPOINTS,
    QUICK_PREVIEW_RATIOS,
    read_base_canvas,
    read_breakpoints,
    read_constraints,
    resolve_constraint_transform,
    write_base_canvas_into,
    write_breakpoints_into,
)
from src.features.teaching_animation.core.contracts import AnimationFrame
from src.features.teaching_animation.core.model import (
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.features.teaching_animation.render.evaluator import evaluate


def _doc(width: int = 1920, height: int = 1080, **metadata) -> TeachingSceneDocument:
    return TeachingSceneDocument(
        scene_id="s", schema_version=3, name="s", width=width, height=height,
        fps_num=30, fps_den=1, duration_frames=30,
        background={"color": "#000000"},
        elements=(), animations=(), metadata=dict(metadata),
    )


def _elem(element_id: str, layout: dict | None = None) -> SceneElement:
    return SceneElement(
        element_id=element_id, type="shape", z_index=0, visible=True,
        transform=SceneTransform(x=100.0, y=50.0, width=200.0, height=100.0),
        style={}, layout=layout or {}, content={},
    )


# ── CanvasConstraints model ──────────────────────────────────────────────────

def test_constraints_roundtrip_and_defaults():
    c = CanvasConstraints()
    assert (c.horizontal, c.vertical, c.keep_aspect) == ("left", "top", False)
    data = CanvasConstraints.from_dict(c.to_dict())
    assert data == c
    styled = CanvasConstraints(horizontal="right", vertical="scale", keep_aspect=True)
    assert CanvasConstraints.from_dict(styled.to_dict()) == styled


def test_constraints_reject_unknown_axis_values():
    with pytest.raises(ValueError):
        CanvasConstraints(horizontal="diagonal")
    with pytest.raises(ValueError):
        CanvasConstraints(vertical="middle")


def test_read_constraints_missing_returns_none():
    assert read_constraints(None) is None
    assert read_constraints({}) is None
    assert read_constraints({"mode": "absolute"}) is None
    got = read_constraints({"constraints": {"horizontal": "center"}})
    assert got == CanvasConstraints(horizontal="center", vertical="top")


# ── resolver semantics (1920×1080 → 1080×1080) ──────────────────────────────
# sx = 1080/1920 = 0.5625, sy = 1.0; dx = -840, dy = 0

_BASE = (1920.0, 1080.0)
_TARGET = (1080.0, 1080.0)
_T = SceneTransform(x=100.0, y=50.0, width=200.0, height=100.0)


def test_left_top_is_identity_against_origin():
    out = resolve_constraint_transform(*_BASE, *_TARGET, _T,
                                       CanvasConstraints("left", "top"))
    assert (out.x, out.y, out.width, out.height) == (100.0, 50.0, 200.0, 100.0)


def test_center_follows_canvas_centre_delta():
    out = resolve_constraint_transform(*_BASE, *_TARGET, _T,
                                       CanvasConstraints("center", "center"))
    # x + dx/2 = 100 - 420 = -320; y unchanged (dy = 0)
    assert out.x == pytest.approx(-320.0)
    assert out.y == pytest.approx(50.0)
    assert (out.width, out.height) == (200.0, 100.0)


def test_right_bottom_keep_far_edge_distance():
    out = resolve_constraint_transform(*_BASE, *_TARGET, _T,
                                       CanvasConstraints("right", "bottom"))
    assert out.x == pytest.approx(100.0 - 840.0)
    assert out.y == pytest.approx(50.0)  # dy = 0


def test_scale_repositions_and_resizes():
    out = resolve_constraint_transform(*_BASE, *_TARGET, _T,
                                       CanvasConstraints("scale", "scale"))
    assert out.x == pytest.approx(100.0 * 0.5625)
    assert out.width == pytest.approx(200.0 * 0.5625)
    assert out.y == pytest.approx(50.0)
    assert out.height == pytest.approx(100.0)


def test_keep_aspect_uses_smaller_ratio():
    out = resolve_constraint_transform(*_BASE, *_TARGET, _T,
                                       CanvasConstraints("scale", "scale", keep_aspect=True))
    uniform = 0.5625  # min(sx, sy)
    assert out.x == pytest.approx(100.0 * uniform)
    assert out.height == pytest.approx(100.0 * uniform)


def test_scale_rotation_pass_through():
    t = replace(_T, scale_x=2.0, scale_y=3.0, rotation=45.0)
    out = resolve_constraint_transform(*_BASE, *_TARGET, t,
                                       CanvasConstraints("scale", "scale"))
    assert (out.scale_x, out.scale_y, out.rotation) == (2.0, 3.0, 45.0)


def test_zero_base_canvas_is_identity():
    out = resolve_constraint_transform(0.0, 0.0, *_TARGET, _T, CanvasConstraints("scale", "scale"))
    assert out == _T


# ── breakpoints + base canvas storage ────────────────────────────────────────

def test_default_breakpoints_when_missing():
    assert read_breakpoints({}) == DEFAULT_BREAKPOINTS
    assert read_breakpoints(None) == DEFAULT_BREAKPOINTS
    assert read_breakpoints({BREAKPOINTS_KEY: []}) == DEFAULT_BREAKPOINTS


def test_breakpoint_roundtrip_filters_invalid():
    meta = write_breakpoints_into({}, (Breakpoint("Cinema", 3840, 2160),))
    assert read_breakpoints(meta) == (Breakpoint("Cinema", 3840, 2160),)
    corrupt = {BREAKPOINTS_KEY: [
        {"name": "ok", "width": 800, "height": 600},
        {"name": "", "width": 10, "height": 10},      # invalid: empty name
        "not-a-mapping",
    ]}
    assert read_breakpoints(corrupt) == (Breakpoint("ok", 800, 600),)


def test_base_canvas_roundtrip_and_invalid():
    meta = write_base_canvas_into({}, 1920.0, 1080.0)
    assert read_base_canvas(meta) == (1920.0, 1080.0)
    assert read_base_canvas({}) is None
    assert read_base_canvas({BASE_CANVAS_KEY: {"width": 0, "height": 100}}) is None
    assert read_base_canvas({BASE_CANVAS_KEY: "corrupt"}) is None


def test_quick_preview_ratios_cover_landscape_portrait_square():
    names = {name for name, _w, _h in QUICK_PREVIEW_RATIOS}
    assert {"16:9", "9:16", "1:1"} <= names


# ── evaluator integration ────────────────────────────────────────────────────

def _scene_with_constraints(constraints: dict | None) -> TeachingSceneDocument:
    scene = _doc(width=1080, height=1080, **{BASE_CANVAS_KEY: {"width": 1920, "height": 1080}})
    layout = {"constraints": constraints} if constraints else {}
    return replace(scene, elements=(_elem("card", layout),))


def test_evaluator_applies_constraints_when_canvas_differs():
    scene = _scene_with_constraints({"horizontal": "scale", "vertical": "center"})
    result = evaluate(scene, AnimationFrame(0, 30))
    card = result.elements[0]
    assert card.transform.x == pytest.approx(100.0 * (1080.0 / 1920.0))
    assert card.transform.width == pytest.approx(200.0 * (1080.0 / 1920.0))
    assert card.transform.y == pytest.approx(50.0 + (1080.0 - 1080.0) / 2.0)


def test_evaluator_legacy_scene_unchanged_without_constraints():
    scene = _scene_with_constraints(None)
    result = evaluate(scene, AnimationFrame(0, 30))
    card = result.elements[0]
    assert (card.transform.x, card.transform.y) == (100.0, 50.0)
    assert (card.transform.width, card.transform.height) == (200.0, 100.0)


def test_evaluator_no_base_canvas_disables_resolution():
    scene = _doc(width=390, height=844)
    layout = {"constraints": {"horizontal": "scale", "vertical": "scale"}}
    scene = replace(scene, elements=(_elem("card", layout),))
    result = evaluate(scene, AnimationFrame(0, 30))
    assert result.elements[0].transform.x == 100.0  # untouched


def test_evaluator_same_canvas_is_identity():
    scene = _doc(width=1920, height=1080, **{BASE_CANVAS_KEY: {"width": 1920, "height": 1080}})
    layout = {"constraints": {"horizontal": "scale", "vertical": "scale"}}
    scene = replace(scene, elements=(_elem("card", layout),))
    result = evaluate(scene, AnimationFrame(0, 30))
    assert result.elements[0].transform.x == 100.0


def test_validator_accepts_constraints():
    from src.features.teaching_animation.core.validator import validate_scene
    scene = _scene_with_constraints({"horizontal": "right", "vertical": "bottom",
                                     "keep_aspect": True})
    result = validate_scene(scene)
    assert not [i for i in result.issues if "constraints" in i.path]


def test_validator_rejects_bad_constraint_axis():
    from src.features.teaching_animation.core.validator import validate_scene
    scene = _scene_with_constraints({"horizontal": "diagonal"})
    result = validate_scene(scene)
    assert any("constraints.horizontal" in i.path for i in result.issues)
