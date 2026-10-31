"""R1 UI coverage: inspector constraints/canvas controls + SetCanvasSizeCommand."""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace

import pytest

from src.features.teaching_animation.core.commands import SetCanvasSizeCommand
from src.features.teaching_animation.core.constraints import BASE_CANVAS_KEY
from src.features.teaching_animation.core.model import SceneElement, SceneTransform
from src.features.teaching_animation.ui.scene_inspector import SceneInspector


def _element(element_id: str = "card") -> SceneElement:
    return SceneElement(
        element_id=element_id, type="shape", z_index=0, visible=True,
        transform=SceneTransform(x=100.0, y=50.0, width=200.0, height=100.0),
        style={}, layout={}, content={},
    )


@pytest.fixture
def inspector(qapp):  # type: ignore[no-untyped-def]
    widget = SceneInspector()
    widget.set_element(_element())
    return widget


# ── Constraints section ──────────────────────────────────────────────────────

def test_constraints_section_visible_for_element(inspector) -> None:  # type: ignore[no-untyped-def]
    assert inspector._constraints_section.isVisibleTo(inspector.parentWidget()) or \
        not inspector._constraints_section.isHidden()


def test_constraints_defaults_left_top(inspector) -> None:  # type: ignore[no-untyped-def]
    assert inspector.constraint_horizontal.currentData() == "left"
    assert inspector.constraint_vertical.currentData() == "top"
    assert inspector.constraint_keep_aspect.currentData() is False


def test_constraints_combo_emits_layout_patch(inspector) -> None:  # type: ignore[no-untyped-def]
    received: list[tuple[str, dict]] = []
    inspector.elementUpdateRequested.connect(
        lambda eid, patch: received.append((eid, patch)))

    idx = inspector.constraint_horizontal.findData("scale")
    inspector.constraint_horizontal.setCurrentIndex(idx)

    assert len(received) == 1
    element_id, patch = received[0]
    assert element_id == "card"
    constraints = patch["layout"]["constraints"]
    assert constraints["horizontal"] == "scale"
    assert constraints["vertical"] == "top"       # untouched axis passes through
    assert constraints["keep_aspect"] is False


def test_constraints_reflect_element_state(qapp) -> None:  # type: ignore[no-untyped-def]
    widget = SceneInspector()
    element = replace(_element(), layout={
        "constraints": {"horizontal": "right", "vertical": "bottom", "keep_aspect": True},
        "width_mode": "fill", "height_mode": "hug",
    })
    widget.set_element(element)
    assert widget.constraint_horizontal.currentData() == "right"
    assert widget.constraint_vertical.currentData() == "bottom"
    assert widget.constraint_keep_aspect.currentData() is True
    assert widget.width_mode.currentData() == "fill"
    assert widget.height_mode.currentData() == "hug"


def test_size_policy_combo_emits_layout_patch(inspector) -> None:  # type: ignore[no-untyped-def]
    received: list[dict] = []
    inspector.elementUpdateRequested.connect(
        lambda _eid, patch: received.append(patch))
    inspector.width_mode.setCurrentIndex(inspector.width_mode.findData("fill"))
    # Modes commit as a pair; the untouched axis carries its current value.
    assert received[-1]["layout"]["width_mode"] == "fill"
    assert received[-1]["layout"]["height_mode"] == "fixed"


def test_constraints_hidden_without_selection(inspector) -> None:  # type: ignore[no-untyped-def]
    inspector.set_element(None)
    assert inspector._constraints_section.isHidden()


# ── Canvas section ───────────────────────────────────────────────────────────

def test_canvas_breakpoint_lists_defaults_plus_current(qapp) -> None:  # type: ignore[no-untyped-def]
    widget = SceneInspector()
    widget.set_scene_canvas(1920, 1080, {})
    assert widget.canvas_breakpoint.count() == 4  # current + Desktop/Tablet/Mobile


def test_canvas_breakpoint_emits_size(qapp) -> None:  # type: ignore[no-untyped-def]
    widget = SceneInspector()
    widget.set_scene_canvas(1920, 1080, {})
    received: list[tuple[int, int]] = []
    widget.canvasSizeRequested.connect(lambda w, h: received.append((w, h)))
    widget.canvas_breakpoint.setCurrentIndex(3)  # Mobile 390×844
    assert received == [(390, 844)]


def test_quick_preview_ratio_buttons_exist(qapp) -> None:  # type: ignore[no-untyped-def]
    widget = SceneInspector()
    buttons = widget.findChildren(type(widget.canvas_breakpoint)) # noqa: F841 — smoke
    from src.features.teaching_animation.ui.scene_inspector import _RatioButton
    ratio_buttons = widget.findChildren(_RatioButton)
    labels = {b.text() for b in ratio_buttons}
    assert {"16:9", "9:16", "1:1"} <= labels


# ── SetCanvasSizeCommand ─────────────────────────────────────────────────────

def _doc():  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.core.model import TeachingSceneDocument
    return TeachingSceneDocument.create_default("canvas_cmd", duration_frames=60)


def test_canvas_command_changes_size_and_records_base() -> None:  # type: ignore[no-untyped-def]
    scene = _doc()
    command = SetCanvasSizeCommand(1080, 1920)
    out = command.apply(scene)

    assert (out.width, out.height) == (1080, 1920)
    base = out.metadata.get(BASE_CANVAS_KEY)
    assert base == {"width": scene.width, "height": scene.height}


def test_canvas_command_undo_restores_metadata() -> None:  # type: ignore[no-untyped-def]
    scene = _doc()
    command = SetCanvasSizeCommand(390, 844)
    out = command.apply(scene)
    restored = command.undo(out)

    assert (restored.width, restored.height) == (scene.width, scene.height)
    assert BASE_CANVAS_KEY not in restored.metadata  # metadata rewound too


def test_canvas_command_existing_base_not_overwritten() -> None:  # type: ignore[no-untyped-def]
    scene = replace(_doc(), metadata={BASE_CANVAS_KEY: {"width": 1920, "height": 1080}})
    command = SetCanvasSizeCommand(1080, 1080)
    out = command.apply(scene)
    assert out.metadata[BASE_CANVAS_KEY] == {"width": 1920, "height": 1080}
