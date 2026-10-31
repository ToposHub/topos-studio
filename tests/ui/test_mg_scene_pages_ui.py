"""Page-tree UI coverage: layer-tree page rows and window flows.

Pages are first-class scene entities; these tests pin the UI contracts:
the Layers panel groups elements under page headers (>=2 pages), drops into
another page section re-page the element, the timeline owns transport/page
stepping, and the editor canvas is page-scoped.
"""

from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace

import pytest

from src.features.teaching_animation.core.model import (
    SceneElement, ScenePage, SceneTransform, TeachingSceneDocument,
)
from src.features.teaching_animation.ui.layer_tree_model import (
    build_layer_rows, page_row_id,
)
from src.features.teaching_animation.ui.layer_tree_panel import LayerTreePanel
from src.features.teaching_animation.ui.motion_graphic_editor_window import MotionGraphicEditorWindow
from src.features.teaching_animation.ui.page_navigation import PageNavigationBar


def _element(eid: str, z: int = 0, page_id: str = "") -> SceneElement:
    return SceneElement(
        element_id=eid, type="text", z_index=z, visible=True,
        transform=SceneTransform(x=10.0, y=10.0, width=200.0, height=60.0),
        style={}, content=eid, page_id=page_id,
    )


def _native(*, duration: int = 48) -> TeachingSceneDocument:
    scene = replace(
        TeachingSceneDocument.create_default("pages-ui", duration_frames=duration),
        elements=())
    return scene


def _two_page_scene() -> TeachingSceneDocument:
    scene = replace(
        _native(),
        pages=(ScenePage(page_id="p1", duration_frames=24),
               ScenePage(page_id="p2", duration_frames=24)),
    )
    scene = scene.with_element_added(_element("a", z=2, page_id="p1"))
    scene = scene.with_element_added(_element("b", z=1, page_id="p1"))
    scene = scene.with_element_added(_element("c", z=0, page_id="p2"))
    return scene


# ---------------------------------------------------------------------------
# layer-tree model
# ---------------------------------------------------------------------------

class TestLayerTreePageRows:
    def test_single_page_scene_still_has_a_page_parent_row(self) -> None:
        rows = build_layer_rows(_native().with_element_added(_element("a")))
        assert [row.kind for row in rows] == ["page", "element", "background", "background"]
        assert rows[0].element_count == 1

    def test_multi_page_groups_elements_under_headers(self) -> None:
        rows = build_layer_rows(_two_page_scene(), active_page_id="p2")
        assert [(row.kind, row.id) for row in rows] == [
            ("page", page_row_id("p1")),
            ("element", "a"),
            ("element", "b"),
            ("background", "__background__:p1"),
            ("page", page_row_id("p2")),
            ("element", "c"),
            ("background", "__background__:p2"),
            ("background", "__global_background__"),
        ]
        page_rows = [row for row in rows if row.kind == "page"]
        assert page_rows[0].active is False
        assert page_rows[1].active is True
        assert page_rows[0].element_count == 2
        assert page_rows[1].element_count == 1
        # Elements indent one extra level under their page header.
        assert rows[1].depth == 1
        assert rows[1].page_id == "p1"
        assert all(row.readonly for row in page_rows)

    def test_page_rows_can_collapse_children(self) -> None:
        rows = build_layer_rows(_two_page_scene(), expanded_page_ids={"p2"})
        assert [(row.kind, row.page_id) for row in rows] == [
            ("page", "p1"), ("page", "p2"), ("element", "p2"),
            ("background", "p2"), ("background", ""),
        ]


# ---------------------------------------------------------------------------
# layer-tree panel
# ---------------------------------------------------------------------------

class TestLayerTreePanelPages:
    def test_page_rows_are_not_selectable(self, qapp) -> None:  # type: ignore[no-untyped-def]
        from PyQt6.QtCore import Qt
        panel = LayerTreePanel()
        panel.set_scene(_two_page_scene(), active_page_id="p2")
        header_item = panel.list_widget.item(0)
        assert not (header_item.flags() & Qt.ItemFlag.ItemIsSelectable)
        assert not (header_item.flags() & Qt.ItemFlag.ItemIsDragEnabled)
        element_item = panel.list_widget.item(1)
        assert element_item.flags() & Qt.ItemFlag.ItemIsSelectable
        panel.deleteLater()

    def test_page_arrow_toggles_children(self, qapp) -> None:  # type: ignore[no-untyped-def]
        panel = LayerTreePanel()
        panel.set_scene(_two_page_scene())
        panel._toggle_page_expanded("p1")
        ids = [panel.list_widget.item(i).data(256) for i in range(panel.list_widget.count())]
        assert ids == [
            page_row_id("p1"), page_row_id("p2"), "c",
            "__background__:p2", "__global_background__",
        ]
        assert panel.expanded_page_ids == ("p2",)
        panel.deleteLater()

    def test_empty_page_arrow_remains_enabled_and_collapsible(self, qapp) -> None:  # type: ignore[no-untyped-def]
        panel = LayerTreePanel()
        scene = replace(
            _native(),
            pages=(ScenePage(page_id="p1", duration_frames=48),),
        )
        panel.set_scene(scene)
        page_item = panel.list_widget.item(0)
        page_widget = panel.list_widget.itemWidget(page_item)
        assert page_widget.expand_button.isEnabled()
        panel._toggle_page_expanded("p1")
        ids = [panel.list_widget.item(i).data(256) for i in range(panel.list_widget.count())]
        assert ids == [page_row_id("p1"), "__global_background__"]
        panel.deleteLater()

    def test_page_header_click_emits_page_activated(self, qapp) -> None:  # type: ignore[no-untyped-def]
        panel = LayerTreePanel()
        panel.set_scene(_two_page_scene())
        activated: list[str] = []
        panel.pageActivated.connect(activated.append)
        panel._item_clicked(panel.list_widget.item(4))  # page 2 header
        assert activated == ["p2"]
        panel.deleteLater()

    def test_move_buttons_operate_in_element_space(self, qapp) -> None:  # type: ignore[no-untyped-def]
        panel = LayerTreePanel()
        scene = _two_page_scene()
        panel.set_scene(scene)
        moves: list[tuple[str, int]] = []
        panel.reorderRequested.connect(lambda eid, index: moves.append((eid, index)))
        # Row order: [page1, a, b, page2, c]. Current on b (list row 2).
        panel.list_widget.setCurrentRow(2)
        panel._move_current(-1)  # above a → top element position
        assert moves == [("b", 2)]
        # Display order is z-driven, so the list stays [page1, a, b, page2, c]
        # after the host applies the tuple reorder. Moving row 1 (a) down:
        panel.set_scene(scene.with_elements_reordered("b", 2))
        panel.list_widget.setCurrentRow(1)
        panel._move_current(1)   # a below b
        assert moves == [("b", 2), ("a", 1)]
        panel.deleteLater()

    def test_cross_page_drop_repages_element(self, qapp) -> None:  # type: ignore[no-untyped-def]
        panel = LayerTreePanel()
        panel.set_scene(_two_page_scene())
        # Emulate QListWidget InternalMove: drag c (row 5) into page 1's
        # section (row 2), then let the rowsMoved handler read the new order.
        item = panel.list_widget.takeItem(5)
        panel.list_widget.insertItem(2, item)
        panel.list_widget.setCurrentItem(item)
        moves: list[tuple[str, int]] = []
        repages: list[tuple[str, str]] = []
        panel.reorderRequested.connect(lambda eid, index: moves.append((eid, index)))
        panel.pageMoveRequested.connect(lambda eid, page: repages.append((eid, page)))
        panel._rows_moved(None, 5, 5, None, 2)
        assert repages == [("c", "p1")]
        assert moves == []
        panel.deleteLater()

    def test_same_page_drop_still_reorders(self, qapp) -> None:  # type: ignore[no-untyped-def]
        panel = LayerTreePanel()
        panel.set_scene(_two_page_scene())
        # Drag a (row 1) below b (row 2): still page 1 → plain z reorder.
        item = panel.list_widget.takeItem(1)
        panel.list_widget.insertItem(2, item)
        panel.list_widget.setCurrentItem(item)
        moves: list[tuple[str, int]] = []
        repages: list[tuple[str, str]] = []
        panel.reorderRequested.connect(lambda eid, index: moves.append((eid, index)))
        panel.pageMoveRequested.connect(lambda eid, page: repages.append((eid, page)))
        panel._rows_moved(None, 1, 1, None, 2)
        # Element order is now [b, a, c]; a sits at element position 1 →
        # tuple index = 3 - 1 - 1 = 1.
        assert moves == [("a", 1)]
        assert repages == []
        panel.deleteLater()


# ---------------------------------------------------------------------------
# page navigation strip
# ---------------------------------------------------------------------------

class TestPageNavigationBar:
    def test_native_multi_page_scene_shows_buttons(self, qapp) -> None:  # type: ignore[no-untyped-def]
        bar = PageNavigationBar()
        scene = _two_page_scene()
        bar.set_scene(scene, 0)
        assert bar.page_count == 2
        assert bar._buttons[0].isChecked()
        bar.set_frame(30)
        assert bar._buttons[1].isChecked()
        frames: list[int] = []
        bar.frameRequested.connect(frames.append)
        bar._request_page(1)
        assert frames == [24]
        bar.request_relative_page(24, -1)
        assert frames == [24, 0]
        bar.request_relative_page(0, -1)  # clamped: no further emit
        assert frames == [24, 0]
        bar.deleteLater()

    def test_single_page_scene_hides_strip(self, qapp) -> None:  # type: ignore[no-untyped-def]
        bar = PageNavigationBar()
        bar.set_scene(_native().with_element_added(_element("a")), 0)
        assert bar.page_count == 1
        assert bar.isHidden()
        bar.deleteLater()


# ---------------------------------------------------------------------------
# window page flows
# ---------------------------------------------------------------------------

class TestWindowPageFlows:
    def test_add_page_grows_duration_and_jumps(self, qapp) -> None:  # type: ignore[no-untyped-def]
        window = MotionGraphicEditorWindow(_native())
        try:
            window.editor_controller.mark_clean()
            window._add_page_requested()
            scene = window.editor_controller.scene
            assert len(scene.pages) == 2
            assert scene.pages[1].duration_frames == round(scene.fps * 3.0)
            assert scene.duration_frames == 48 + round(scene.fps * 3.0)
            assert window.editor_controller.current_page_id == scene.page_ids[1]
            assert window._timeline_panel.layout().indexOf(window.timeline) == 0
        finally:
            window.editor_controller.mark_clean()
            window.close()

    def test_new_elements_land_on_active_page(self, qapp) -> None:  # type: ignore[no-untyped-def]
        window = MotionGraphicEditorWindow(_native())
        try:
            window._add_page_requested()
            page2 = window.editor_controller.current_page_id
            window._create_layer("text")
            element_id = next(iter(window._selected_elements))
            scene = window.editor_controller.scene
            assert scene.element_by_id(element_id).page_id == page2
            # The canvas is page-scoped: page 1 active hides the new element.
            window._frame_changed(0)
            assert window._active_page_index == 0
            assert window._page_view.element_by_id(element_id) is None
            window._jump_to_page_by_id(page2)
            assert window._page_view.element_by_id(element_id) is not None
        finally:
            window.editor_controller.mark_clean()
            window.close()

    def test_layer_tree_repage_is_one_undo(self, qapp) -> None:  # type: ignore[no-untyped-def]
        window = MotionGraphicEditorWindow(_two_page_scene())
        try:
            window.editor_controller.mark_clean()
            window._move_elements_to_page("c", "p1")
            scene = window.editor_controller.scene
            assert scene.element_by_id("c").page_id == "p1"
            window.editor_controller.undo()
            assert window.editor_controller.scene.element_by_id("c").page_id == "p2"
        finally:
            window.editor_controller.mark_clean()
            window.close()

    def test_rename_and_duration_handlers(self, qapp, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from PyQt6.QtWidgets import QInputDialog
        window = MotionGraphicEditorWindow(_two_page_scene())
        try:
            window.editor_controller.mark_clean()
            monkeypatch.setattr(
                QInputDialog, "getText",
                staticmethod(lambda *args, **kwargs: ("封面", True)))
            window._rename_page_requested("p1")
            assert window.editor_controller.scene.page_by_id("p1").name == "封面"
            monkeypatch.setattr(
                QInputDialog, "getInt",
                staticmethod(lambda *args, **kwargs: (10, True)))
            window._page_duration_requested("p1")
            scene = window.editor_controller.scene
            assert scene.page_by_id("p1").duration_frames == 10
            assert scene.duration_frames == 34  # page durations sum
        finally:
            window.editor_controller.mark_clean()
            window.close()
