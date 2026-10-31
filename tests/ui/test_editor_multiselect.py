"""Multi-select phase-1: frozenset selection model + canvas shift-click +
tree ExtendedSelection + batch delete.

The window is the sole selection owner; these tests exercise the chokepoint
(``_set_selection``) via the three entry points (canvas plain-click /
canvas shift-click toggle / tree multi-row), the three-way sync to the
layer tree, and batch delete.  The inspector's single-element form is
reused (it shows the primary selected element).
"""

from __future__ import annotations

import pytest

from src.features.teaching_animation.core.model import (
    SceneElement, SceneTransform, TeachingSceneDocument,
)
from src.features.teaching_animation.ui.motion_graphic_editor_window import (
    MotionGraphicEditorWindow,
)


def _element(eid: str, *, x: float = 10.0) -> SceneElement:
    return SceneElement(
        element_id=eid, type="shape",
        transform=SceneTransform(x=x, y=10.0, width=80, height=40),
        style={}, content={"kind": "rect"})


def _scene() -> TeachingSceneDocument:
    base = TeachingSceneDocument.create_default("multi", width=320, height=200, duration_frames=24)
    for eid in ("a", "b", "c"):
        base = base.with_element_added(_element(eid, x=10.0 if eid == "a" else (100.0 if eid == "b" else 190.0)))
    return base


@pytest.fixture
def window(qapp):
    win = MotionGraphicEditorWindow(_scene())
    yield win
    win.deleteLater()
    qapp.processEvents()


class TestCanvasSelection:
    def test_plain_click_replaces_selection(self, window) -> None:
        window._on_canvas_select("a")
        assert window._selected_elements == frozenset({"a"})
        assert set(window.layer_tree.selected_row_ids) == {"a"}

    def test_shift_click_toggles_into_set(self, window) -> None:
        window._on_canvas_select("a")
        window._on_canvas_toggle("b")
        assert window._selected_elements == frozenset({"a", "b"})
        assert set(window.layer_tree.selected_row_ids) == {"a", "b"}

    def test_shift_click_toggles_out_of_set(self, window) -> None:
        window._on_canvas_select("a")
        window._on_canvas_toggle("b")
        window._on_canvas_toggle("a")
        assert window._selected_elements == frozenset({"b"})


class TestTreeSelection:
    def test_tree_multi_select_mirrors_set(self, window) -> None:
        # Drive the tree's list selection, then inform the window as the
        # selectionChanged signal would (source=tree → no echo back to tree).
        window.layer_tree.set_selection(["b", "c"])
        window._on_tree_selection(("b", "c"))
        assert window._selected_elements == frozenset({"b", "c"})
        assert set(window.layer_tree.selected_row_ids) == {"b", "c"}

    def test_tree_selection_does_not_echo(self, window) -> None:
        # set_selection is the external-sync path; _on_tree_selection (source=tree)
        # must not call it again (would be redundant / could re-emit).
        window.layer_tree.set_selection(["a"])
        window._on_tree_selection(("a",))
        # Selection still holds exactly {a}; no recursion / duplication.
        assert window._selected_elements == frozenset({"a"})


class TestInspectorPrimary:
    def test_inspector_shows_a_selected_element(self, window) -> None:
        window._on_tree_selection(("b", "c"))
        # The inspector is showing some element from the selection (primary).
        # SceneInspector holds the current element; assert it is one of them.
        current = window.inspector._element
        assert current is not None and current.element_id in {"b", "c"}


class TestBatchDelete:
    def test_delete_button_removes_all_selected(self, window) -> None:
        # Two rows selected in the tree, then the delete button fires
        # removeRequested per selected row → the window removes each.
        window.layer_tree.set_selection(["a", "b"])
        window._on_tree_selection(("a", "b"))
        window.layer_tree.delete_button.click()
        scene = window.editor_controller.scene
        assert scene.element_by_id("a") is None
        assert scene.element_by_id("b") is None
        assert scene.element_by_id("c") is not None   # untouched
        # Selection no longer references the removed ids.
        assert not (window._selected_elements & {"a", "b"})


class TestDragMovesAll:
    """Phase-2: dragging one selected element shifts every selected element by
    the same delta (committed as one UpdateElementCommand per element)."""

    def test_single_drag_moves_one_element(self, window) -> None:
        window._on_canvas_select("a")             # selection = {a}
        window._on_drag_started("a")
        window._element_dragged("a", 60.0, 30.0)  # a was at (10, 10)
        window._on_drag_finished("a", 60.0, 30.0)
        a = window.editor_controller.scene.element_by_id("a")
        assert a is not None and (a.transform.x, a.transform.y) == (60.0, 30.0)
        # b untouched.
        b = window.editor_controller.scene.element_by_id("b")
        assert (b.transform.x, b.transform.y) == (100.0, 10.0)

    def test_multi_drag_moves_all_selected_by_same_delta(self, window) -> None:
        # a=(10,10), b=(100,10). Select both, drag a to (60,40): delta (+50,+30).
        window._on_canvas_select("a")
        window._on_canvas_toggle("b")
        assert window._selected_elements == frozenset({"a", "b"})
        window._on_drag_started("a")
        window._element_dragged("a", 60.0, 40.0)
        window._on_drag_finished("a", 60.0, 40.0)
        a = window.editor_controller.scene.element_by_id("a")
        b = window.editor_controller.scene.element_by_id("b")
        assert (a.transform.x, a.transform.y) == (60.0, 40.0)
        # b shifted by the same delta: (100+50, 10+30) = (150, 40).
        assert (b.transform.x, b.transform.y) == (150.0, 40.0)
        # c untouched.
        c = window.editor_controller.scene.element_by_id("c")
        assert (c.transform.x, c.transform.y) == (190.0, 10.0)

    def test_click_on_selected_in_multiset_keeps_set_for_drag(self, window) -> None:
        window._on_canvas_select("a")
        window._on_canvas_toggle("b")
        # Plain-clicking "a" (already in the multi-set) must NOT narrow the
        # selection — otherwise the following drag could not move both.
        window._on_canvas_select("a")
        assert window._selected_elements == frozenset({"a", "b"})


class TestAlignDistribute:
    def test_align_left_moves_both_and_single_undo_restores(self, window) -> None:
        # a=(10,10), b=(100,10). align_left → both x = 10.
        window._on_canvas_select("a")
        window._on_canvas_toggle("b")
        assert window._align_button.isEnabled() is True
        before_a = window.editor_controller.scene.element_by_id("a").transform.x
        before_b = window.editor_controller.scene.element_by_id("b").transform.x
        window._align_selection("align_left")
        scene = window.editor_controller.scene
        assert scene.element_by_id("a").transform.x == 10.0
        assert scene.element_by_id("b").transform.x == 10.0  # was 100
        # Single undo restores BOTH (CompositeCommand = one undo step).
        window._undo()
        scene = window.editor_controller.scene
        assert scene.element_by_id("a").transform.x == before_a
        assert scene.element_by_id("b").transform.x == before_b

    def test_align_with_one_selected_is_noop(self, window) -> None:
        window._on_canvas_select("a")
        assert not window._align_button.isEnabled()
        before = window.editor_controller.scene.element_by_id("a").transform.x
        window._align_selection("align_left")  # guarded: <2 elements
        assert window.editor_controller.scene.element_by_id("a").transform.x == before


class TestMarqueeAndBulkInspector:
    def test_marquee_replaces_or_adds_to_selection(self, window) -> None:
        window._on_canvas_marquee(("a", "b"), False)
        assert window._selected_elements == frozenset({"a", "b"})
        window._on_canvas_marquee(("c",), True)
        assert window._selected_elements == frozenset({"a", "b", "c"})

    def test_bulk_style_edit_merges_only_inspector_fields_and_undoes_once(self, window) -> None:
        window._on_canvas_select("a")
        window._on_canvas_toggle("b")
        window._selection_updated(("a", "b"), {
            "style_patch": {"color": "#00ff00", "font_family": "Inter"},
        })
        for element_id in ("a", "b"):
            element = window.editor_controller.scene.element_by_id(element_id)
            assert element.style["color"] == "#00ff00"
            assert element.style["font_family"] == "Inter"
        window._undo()
        assert window.editor_controller.scene.element_by_id("a").style == {}
        assert window.editor_controller.scene.element_by_id("b").style == {}

    def test_bulk_transform_preserves_each_element_size(self, window) -> None:
        window._on_canvas_select("a")
        window._on_canvas_toggle("b")
        window._selection_updated(("a", "b"), {
            "transform_patch": {"x": 40.0, "y": 50.0, "scale_x": 2.0,
                                 "scale_y": 2.0, "rotation": 15.0},
        })
        a = window.editor_controller.scene.element_by_id("a")
        b = window.editor_controller.scene.element_by_id("b")
        assert (a.transform.x, a.transform.width) == (40.0, 80.0)
        assert (b.transform.x, b.transform.width) == (40.0, 80.0)

    def test_create_component_from_selection_is_undoable(self, window) -> None:
        window._on_canvas_select("a")
        window._on_canvas_toggle("b")
        assert window._create_component("cards", "Cards")
        from src.features.teaching_animation.core.components import read_component_masters
        assert read_component_masters(window.editor_controller.scene.metadata)[0].element_ids == ("a", "b")
        window._undo()
        assert read_component_masters(window.editor_controller.scene.metadata) == ()
