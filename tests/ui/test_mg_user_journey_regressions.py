"""Focused regressions for bugs found by the real-user E2E journey script.

Each test pins one defect discovered while driving the real editor window in
``scripts/e2e_mg_user_journey.py``; see the journey script for the end-to-end
version of every scenario below.
"""

from __future__ import annotations

from dataclasses import replace

from PyQt6.QtGui import QColor

from src.features.teaching_animation.core.commands import AddElementCommand
from src.features.teaching_animation.core.hierarchy import sort_for_paint
from src.features.teaching_animation.core.html_page_deck import (
    PageDeckTiming, page_deck_editor_html,
)
from src.features.teaching_animation.core.model import SceneElement, SceneTransform, TeachingSceneDocument
from src.features.teaching_animation.ui.editor_controller import SceneEditorController
from src.features.teaching_animation.ui.motion_graphic_editor_window import MotionGraphicEditorWindow
from src.features.teaching_animation.ui.scene_inspector import SceneInspector
from src.shared.i18n.i18n import I18N


def _scene(*elements: SceneElement) -> TeachingSceneDocument:
    return replace(
        TeachingSceneDocument.create_default("regressions", duration_frames=120),
        elements=tuple(elements),
    )


def _text(element_id: str = "t1") -> SceneElement:
    return SceneElement(
        element_id=element_id, type="text", z_index=0,
        transform=SceneTransform(x=80.0, y=60.0, width=1000.0, height=100.0),
        style={"color": "#ffffff", "font_size": 48.0}, content="hello",
    )


def _shape(element_id: str = "s1", z_index: int = 1) -> SceneElement:
    return SceneElement(
        element_id=element_id, type="shape", z_index=z_index,
        transform=SceneTransform(x=80.0, y=200.0, width=300.0, height=60.0),
        style={"fill": "#ff8800", "stroke": "#000000", "stroke_width": 0.0},
        content={"kind": "rect"},
    )


# --------------------------------------------------------------------------- #
# Colour commits must round-trip through the picker's linear RGBA storage
# --------------------------------------------------------------------------- #

def test_color_commit_round_trips_exact_hex(qapp) -> None:  # type: ignore[no-untyped-def]
    inspector = SceneInspector()
    inspector.set_selection((_text(),))
    inspector.color_edit._commit_color(QColor("#ffcc00"))
    from src.shared.ui.widgets.color_picker_control import linear_rgba_to_qcolor
    assert linear_rgba_to_qcolor(inspector.color_edit.rgba()).name() == "#ffcc00"


def test_color_patch_writes_exact_hex_into_style(qapp) -> None:  # type: ignore[no-untyped-def]
    inspector = SceneInspector()
    emitted: list[dict] = []
    inspector.elementUpdateRequested.connect(lambda _eid, updates: emitted.append(dict(updates)))
    inspector.set_selection((_text(),))
    inspector.color_edit._commit_color(QColor("#ffcc00"))
    assert emitted, "colour commit produced no update"
    style = emitted[-1]["style"]
    assert style["color"] == "#ffcc00", f"colour came back wrong: {style['color']}"
    assert style["fill"] == "#ffcc00"


# --------------------------------------------------------------------------- #
# The opacity-keyframe button keyframes the slider, not a hardcoded 1.0
# --------------------------------------------------------------------------- #

def test_keyframe_button_records_slider_value(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene(_text()))
    try:
        window._set_selection({"t1"})
        window.timeline.slider.setValue(0)
        inspector = window.inspector
        inspector.opacity.slider.setValue(30)          # 0.3 in percent
        inspector.opacity.interactionEnded.emit()
        inspector.keyframe_button.click()
        track = next(a for a in window.editor_controller.scene.animations if a.property == "opacity")
        assert [(k.frame, k.value) for k in track.track.keyframes] == [(0, 0.3)]
    finally:
        window.editor_controller.mark_clean()
        window.close()


# --------------------------------------------------------------------------- #
# Copy/Paste Style must become usable the moment the selection changes
# --------------------------------------------------------------------------- #

def test_style_actions_enable_on_plain_reselection(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene(_text(), _shape()))
    try:
        window._set_selection({"s1"})
        assert window._copy_style_action.isEnabled(), "Copy Style dead after re-select"
        window._copy_style_action.trigger()
        window._set_selection({"t1"})
        assert window._paste_style_action.isEnabled(), "Paste Style dead right after Copy"
        window._paste_style_action.trigger()
        text = window.editor_controller.scene.element_by_id("t1")
        shape = window.editor_controller.scene.element_by_id("s1")
        assert dict(text.style) == dict(shape.style)  # type: ignore[union-attr]
    finally:
        window.editor_controller.mark_clean()
        window.close()


# --------------------------------------------------------------------------- #
# The colour-token combo must list palette entries from the real editor path
# --------------------------------------------------------------------------- #

def test_window_selection_populates_token_combo(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene(_text()))
    try:
        window._set_selection({"t1"})
        tokens = [window.inspector.color_token.itemText(i)
                  for i in range(window.inspector.color_token.count())]
        assert "accent" in tokens, f"palette tokens missing from combo: {tokens}"
    finally:
        window.editor_controller.mark_clean()
        window.close()


# --------------------------------------------------------------------------- #
# Keyframe edits on existing tracks must be undoable
# --------------------------------------------------------------------------- #

def test_existing_track_keyframes_survive_undo_redo(qapp) -> None:  # type: ignore[no-untyped-def]
    controller = SceneEditorController(_scene(_text()))
    controller.add_keyframe("t1", "opacity", 0, 0.2)
    controller.add_keyframe("t1", "opacity", 48, 1.0)
    depth = len(controller._stack._undo_stack)
    assert depth == 2, "second keyframe did not enter the undo stack"

    controller.undo()
    controller.undo()
    assert controller.scene.animations == (), "undo did not remove the track"

    controller.redo()
    controller.redo()
    track = next(a for a in controller.scene.animations if a.property == "opacity")
    values = sorted(k.value for k in track.track.keyframes)
    assert values == [0.2, 1.0], f"undo/redo lost keyframes: {values}"


# --------------------------------------------------------------------------- #
# The page runtime must expose window.toposPageBridge for native overlay calls
# --------------------------------------------------------------------------- #

def test_editor_html_exposes_page_bridge() -> None:
    document = '<section data-topos-page="1"><h1>One</h1></section>'
    prepared = page_deck_editor_html(document, PageDeckTiming(page_count=1))
    assert "window.toposPageBridge = bridge" in prepared, (
        "native overlay select/edit/draw gestures cannot reach Python"
    )


# --------------------------------------------------------------------------- #
# Paint order: a frame authored later must not cover its own children
# --------------------------------------------------------------------------- #

def test_sort_for_paint_keeps_children_above_parent_plate() -> None:
    text, shape = _text(), _shape()
    frame = SceneElement(
        element_id="f1", type="frame", z_index=2,
        transform=SceneTransform(x=40.0, y=20.0, width=1800.0, height=1000.0),
        style={"fill": "#1e293b"}, layout={"mode": "flex-row"}, content="frame",
    )
    parents = {"t1": "f1", "s1": "f1"}
    order = [element.element_id for element in sort_for_paint((text, shape, frame), parents)]
    assert order.index("f1") < order.index("t1"), f"frame plate covers its children: {order}"
    assert order.index("f1") < order.index("s1"), f"frame plate covers its children: {order}"


def test_sort_for_paint_preserves_flat_order_without_hierarchy() -> None:
    text, shape, frame = _text(), _shape(), SceneElement(
        element_id="f1", type="frame", z_index=2,
        transform=SceneTransform(x=40.0, y=20.0, width=100.0, height=100.0),
        style={}, content="frame",
    )
    order = [element.element_id for element in sort_for_paint((frame, text, shape), {})]
    assert order == ["t1", "s1", "f1"], "flat z order regressed"


# --------------------------------------------------------------------------- #
# I18N defaults interpolate placeholders even before translations load
# --------------------------------------------------------------------------- #

def test_i18n_default_interpolates_kwargs_without_translations() -> None:
    I18N._translations = {}  # simulate contexts that never loaded a locale
    try:
        text = I18N.tr("motion_graphic.browser.unavailable",
                       "HTML browser unavailable: {error}", error="no webengine")
        assert text == "HTML browser unavailable: no webengine"
        assert "{error}" not in text
    finally:
        I18N._translations = {}


# --------------------------------------------------------------------------- #
# AddElement path sanity used by the journey (auto-select + undo)
# --------------------------------------------------------------------------- #

def test_create_layer_is_one_undoable_auto_selected_step(qapp) -> None:  # type: ignore[no-untyped-def]
    window = MotionGraphicEditorWindow(_scene())
    try:
        window._create_layer("frame")
        scene = window.editor_controller.scene
        assert len(scene.elements) == 1
        element = scene.elements[0]
        assert window._selected_elements == {element.element_id}
        assert len(window.editor_controller._stack._undo_stack) == 1
    finally:
        window.editor_controller.mark_clean()
        window.close()
