"""UX-10 layer-tree view-model + hierarchy command (Qt-free, fast).

Covers the pure logic the LayerTreePanel renders: ``build_layer_rows`` ordering
+ flag mapping, and the ``SetHierarchyCommand`` reversible write that the eye /
lock / solo icons emit.  No Qt is imported, so these run anywhere.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.features.teaching_animation.core.commands import SetHierarchyCommand, SetParentCommand
from src.features.teaching_animation.core.hierarchy import (
    Hierarchy,
    read_hierarchy,
    write_hierarchy_into,
)
from src.features.teaching_animation.core.model import SceneElement, SceneTransform, TeachingSceneDocument
from src.features.teaching_animation.ui.layer_tree_model import LayerRow, build_layer_rows, page_row_id


def _scene_with(elements: tuple[SceneElement, ...], *, hierarchy: Hierarchy | None = None) -> TeachingSceneDocument:
    base = TeachingSceneDocument.create_default("tree", width=320, height=200, duration_frames=6)
    base = replace(base, elements=elements)
    if hierarchy is not None:
        base = replace(base, metadata=write_hierarchy_into(base.metadata, hierarchy))
    return base


def _el(eid: str, *, z: int = 0, content: str = "") -> SceneElement:
    return SceneElement(
        element_id=eid, type="text",
        transform=SceneTransform(x=0, y=0, width=100, height=40),
        style={}, content=content or eid)


# --------------------------------------------------------------------------- #
# build_layer_rows
# --------------------------------------------------------------------------- #


def test_layer_rows_ordered_top_most_first_by_z_index() -> None:
    scene = _scene_with((_el("back", z=0), _el("mid", z=5), _el("front", z=10)))
    rows = build_layer_rows(scene)
    assert [r.id for r in rows] == [
        page_row_id("page_1"), "front", "mid", "back",
        "__background__:page_1", "__global_background__",
    ]


def test_layer_rows_equal_z_index_breaks_by_document_order() -> None:
    """Equal z_index: later in the document tuple renders on top (appears first)."""
    scene = _scene_with((_el("a", z=1), _el("b", z=1), _el("c", z=1)))
    rows = build_layer_rows(scene)
    assert [r.id for r in rows] == [
        page_row_id("page_1"), "c", "b", "a",
        "__background__:page_1", "__global_background__",
    ]


def test_layer_rows_read_lock_hide_solo_from_hierarchy() -> None:
    scene = _scene_with(
        (_el("a"), _el("b"), _el("c")),
        hierarchy=Hierarchy(locked={"a"}, solo={"b"}, hidden={"c"}))
    rows = {r.id: r for r in build_layer_rows(scene)}
    assert rows["a"].locked is True and rows["a"].visible is True and rows["a"].solo is False
    assert rows["b"].solo is True and rows["b"].locked is False
    assert rows["c"].visible is False  # in hierarchy.hidden


def test_layer_rows_no_hierarchy_means_all_visible_unlocked() -> None:
    scene = _scene_with((_el("a"), _el("b")))
    rows = build_layer_rows(scene)
    assert all(r.visible for r in rows)
    assert all(not r.locked for r in rows)
    assert all(not r.solo for r in rows)


def test_layer_rows_keep_html_source_out_of_user_facing_tree() -> None:
    from src.features.teaching_animation.core.html_layer import build_html_layer
    base = _scene_with((_el("a"),))
    layer = build_html_layer(
        layer_id="L", sanitized_html="<p>x</p>", authored_source="<p>x</p>",
        viewport_width=320, viewport_height=200, fps_num=24, fps_den=1, duration_frames=6)
    scene = replace(base, html_layer=layer)
    rows = build_layer_rows(scene)
    assert not any(r.is_html for r in rows)
    assert rows[-1].kind == "background"
    assert rows[-1].label == "Global Background"


def test_layer_row_label_prefers_content_text() -> None:
    scene = _scene_with((_el("t1", content="Hello"), _el("t2")))
    rows = {r.id: r for r in build_layer_rows(scene)}
    assert rows["t1"].label == "Hello"
    assert "t2" in rows["t2"].label  # falls back to type · id


# --------------------------------------------------------------------------- #
# SetHierarchyCommand
# --------------------------------------------------------------------------- #


def test_set_hierarchy_command_toggles_hidden_and_writes_metadata() -> None:
    scene = _scene_with((_el("a"),))
    cmd = SetHierarchyCommand(element_id="a", flag="hidden", value=True)
    applied = cmd.apply(scene)
    assert "a" in read_hierarchy(applied.metadata).hidden
    # The scene object changed; the row now reports not visible.
    assert next(r for r in build_layer_rows(applied) if r.id == "a").visible is False


def test_set_hierarchy_command_undo_restores_previous_hierarchy() -> None:
    scene = _scene_with((_el("a"), _el("b")), hierarchy=Hierarchy(locked={"b"}))
    cmd = SetHierarchyCommand(element_id="a", flag="locked", value=True)
    applied = cmd.apply(scene)
    assert read_hierarchy(applied.metadata).locked == frozenset({"a", "b"})
    restored = cmd.undo(applied)
    assert read_hierarchy(restored.metadata).locked == frozenset({"b"})


def test_set_hierarchy_command_is_idempotent_on_repeat() -> None:
    """Setting the same flag/value twice leaves membership unchanged."""
    scene = _scene_with((_el("a"),))
    once = SetHierarchyCommand("a", "solo", True).apply(scene)
    twice = SetHierarchyCommand("a", "solo", True).apply(once)
    assert read_hierarchy(twice.metadata).solo == frozenset({"a"})


def test_set_hierarchy_command_rejects_unknown_flag() -> None:
    _scene_with((_el("a"),))  # scene unaffected; validation is at construction
    with pytest.raises(ValueError, match="locked/solo/hidden"):
        SetHierarchyCommand(element_id="a", flag="frobnicate", value=True)


def test_set_hierarchy_command_roundtrips_through_stack() -> None:
    """The command works through SceneCommandStack like any other (undo/redo)."""
    from src.features.teaching_animation.core.commands import SceneCommandStack
    scene = _scene_with((_el("a"),))
    stack = SceneCommandStack()
    scene = stack.execute(SetHierarchyCommand("a", "hidden", True), scene)
    assert "a" in read_hierarchy(scene.metadata).hidden
    scene = stack.undo(scene)
    assert "a" not in read_hierarchy(scene.metadata).hidden
    scene = stack.redo(scene)
    assert "a" in read_hierarchy(scene.metadata).hidden


def test_set_parent_command_round_trips_and_undoes() -> None:
    scene = _scene_with((_el("parent"), _el("child")))
    command = SetParentCommand("child", "parent")
    nested = command.apply(scene)
    assert read_hierarchy(nested.metadata).parents == {"child": "parent"}
    assert command.undo(nested) == scene


def test_set_parent_command_rejects_cycles() -> None:
    scene = _scene_with((_el("parent"), _el("child")),
                        hierarchy=Hierarchy(parents={"child": "parent"}))
    with pytest.raises(ValueError, match="cycle"):
        SetParentCommand("parent", "child").apply(scene)


# --------------------------------------------------------------------------- #
# LayerTreePanel widget (Qt) — thin renderer smoke tests
# --------------------------------------------------------------------------- #


def _qt_scene() -> TeachingSceneDocument:
    return _scene_with((_el("back", z=0, content="Back"), _el("front", z=10, content="Front")))


def test_layer_tree_panel_populates_rows_top_most_first(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.ui.layer_tree_panel import LayerTreePanel
    panel = LayerTreePanel()
    panel.set_scene(_qt_scene())
    ids = [panel.list_widget.item(i).data(256) for i in range(panel.list_widget.count())]
    assert ids == [page_row_id("page_1"), "front", "back", "__background__:page_1", "__global_background__"]


def test_layer_tree_panel_selection_emits_and_syncs(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.ui.layer_tree_panel import LayerTreePanel
    panel = LayerTreePanel()
    panel.set_scene(_qt_scene())
    received: list[str] = []
    panel.elementSelected.connect(lambda eid: received.append(eid))
    # Clicking row 1 ("front", under the page parent) selects it.
    panel.list_widget.setCurrentRow(1)
    assert received == ["front"]
    # External selection sync highlights without re-emitting (set_selection is silent).
    panel.set_selection("back")
    assert panel.current_row_id == "back"
    assert received == ["front"]  # set_selection did not emit


def test_layer_tree_panel_multi_select_emits_set_and_accepts_collection(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.ui.layer_tree_panel import LayerTreePanel
    panel = LayerTreePanel()
    panel.set_scene(_qt_scene())
    sets: list[tuple[str, ...]] = []
    panel.selectionChanged.connect(lambda ids: sets.append(tuple(ids)))
    # A selection change emits the full set as a tuple (here a single-row set).
    panel.list_widget.setCurrentRow(1)
    assert sets and sets[-1] == ("front",)
    # set_selection accepts a collection and highlights multiple rows silently.
    sets.clear()
    panel.set_selection(["front", "back"])
    assert set(panel.selected_row_ids) == {"front", "back"}
    assert sets == []  # external sync does not re-emit


def test_layer_tree_panel_visibility_toggle_emits(qapp) -> None:  # type: ignore[no-untyped-def]
    from src.features.teaching_animation.ui.layer_tree_panel import LayerTreePanel
    panel = LayerTreePanel()
    panel.set_scene(_qt_scene())
    received: list[str] = []
    panel.visibilityToggled.connect(lambda eid: received.append(eid))
    # Grab the item widget for row 1 and toggle its visibility button.
    item = panel.list_widget.item(1)
    widget = panel.list_widget.itemWidget(item)
    assert widget is not None
    widget.vis_button.setChecked(False)  # was visible → toggle off
    assert received == ["front"]


def test_layer_tree_footer_uses_real_create_duplicate_and_move_actions(qapp) -> None:  # type: ignore[no-untyped-def]
    """The colour-page-style footer must expose working MG layer commands."""
    from src.features.teaching_animation.ui.layer_tree_panel import LayerTreePanel

    panel = LayerTreePanel()
    panel.set_scene(_qt_scene())
    created: list[str] = []
    duplicated: list[str] = []
    reordered: list[tuple[str, int]] = []
    pages_created: list[bool] = []
    panel.createLayerRequested.connect(created.append)
    panel.pageCreateRequested.connect(lambda: pages_created.append(True))
    panel.duplicateRequested.connect(duplicated.append)
    panel.reorderRequested.connect(lambda layer_id, index: reordered.append((layer_id, index)))

    panel.list_widget.setCurrentRow(1)  # front/top-most layer
    panel.duplicate_button.click()
    panel.down_button.click()
    text_action = next(
        action for action in panel.add_button.menu().actions()
        if action.text() == "Text Layer"
    )
    text_action.trigger()
    page_action = next(
        action for action in panel.add_button.menu().actions()
        if action.text() == "New Page"
    )
    page_action.trigger()

    assert duplicated == ["front"]
    assert reordered == [("front", 0)]
    assert created == ["text"]
    assert pages_created == [True]
    assert panel.add_button.autoRaise() is False  # explicit transparent QSS, no native frame
