"""CornerPin drag defaults to a live warp preview; Shift = Nuke-style freeze.

Moving a source (from) corner re-anchors the deformation, so the image must
warp live during the drag. For the rare precise-alignment case where the live
warp drifts under the pointer, holding Shift switches to Nuke's "freeze":
the image stays at its current state, only the viewer overlay (quad outline +
handles) follows the pointer, and the accumulated params are flushed once on
release.
"""
import os

import pytest  # noqa: F401  (qapp fixture)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, Qt

from src.features.composite.nodes.cornerpin_node import CornerPinNode
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.viewer_tool_system import (
    ViewerInteractionState,
    ViewerToolMode,
)

SHIFT = Qt.KeyboardModifier.ShiftModifier
ALT = Qt.KeyboardModifier.AltModifier
ALT_SHIFT = Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ShiftModifier


def _make_page_with_cornerpin():
    """Real CompositePage + CornerPin node, heavy flush side effects stubbed."""
    page = CompositePage(parent=None)
    node = CornerPinNode(node_id="cp1")
    page._node_graph.add_node(node)
    page._last_texture_width = 100
    page._last_texture_height = 100
    # Stub the flush/render tail so the test never touches the GL path.
    page._evaluate_graph_now = lambda: None
    page._refresh_properties_panel = lambda node_id: None
    page.update_viewer_handles = lambda: None
    return page, node


def _drag_state(delta=(10.0, 5.0), modifiers=0, handle="to1"):
    """ViewerInteractionState simulating a CornerPin corner drag."""
    state = ViewerInteractionState()
    state.start_drag(
        tool=ViewerToolMode.CORNERPIN,
        node_id="cp1",
        handle=handle,
        view_pos=QPointF(0.0, 0.0),
        ndc_pos=QPointF(0.0, 0.0),
        image_pos=QPointF(0.0, 0.0),
        format_pos=QPointF(0.0, 0.0),
        original_params={},
        modifiers=modifiers,
    )
    state.update_drag(
        QPointF(*delta), image_pos=QPointF(*delta), modifiers=modifiers)
    return state


def test_cornerpin_drag_is_live_by_default(qapp):
    """Default drag: the throttle timer is armed so the warp follows the
    pointer in real time (moving a from corner re-anchors the deformation)."""
    page, node = _make_page_with_cornerpin()
    try:
        state = _drag_state()
        page._on_viewer_interaction_started(state)
        page._on_viewer_interaction_updated(state)

        assert page._pending_interaction_update is not None
        pending = page._pending_interaction_update["params"]
        assert pending["to1_x"] == pytest.approx(0.1)   # 10px / 100px
        assert pending["to1_y"] == pytest.approx(0.05)  # 5px / 100px
        # Live path: throttle timer armed → image re-renders during the drag.
        assert page._interaction_debounce_timer.isActive()
        page._interaction_debounce_timer.stop()
    finally:
        page.deleteLater()


def test_shift_freezes_image_during_cornerpin_drag(qapp):
    """Shift-drag (Nuke-style freeze): params accumulate but the node stays
    untouched and no render is scheduled — the image is frozen."""
    page, node = _make_page_with_cornerpin()
    try:
        state = _drag_state(modifiers=SHIFT)
        page._on_viewer_interaction_started(state)
        page._on_viewer_interaction_updated(state)

        # The new corner position is tracked for the release flush...
        assert page._pending_interaction_update is not None
        pending = page._pending_interaction_update["params"]
        assert pending["to1_x"] == pytest.approx(0.1)
        # ...but the node params (and thus the rendered image) are unchanged.
        assert node.parameters["to1_x"] == pytest.approx(0.0)
        assert node.parameters["to1_y"] == pytest.approx(0.0)
        # No throttled render pending — the image really stays frozen.
        assert not page._interaction_debounce_timer.isActive()
    finally:
        page.deleteLater()


def test_shift_drag_flushes_on_release(qapp):
    """After a frozen (Shift) drag, release applies the accumulated params
    in one shot."""
    page, node = _make_page_with_cornerpin()
    try:
        state = _drag_state(modifiers=SHIFT)
        page._on_viewer_interaction_started(state)
        page._on_viewer_interaction_updated(state)
        assert node.parameters["to1_x"] == pytest.approx(0.0)

        original_params = state.end_drag()
        page._on_viewer_interaction_ended(state, original_params)

        assert node.parameters["to1_x"] == pytest.approx(0.1)
        assert node.parameters["to1_y"] == pytest.approx(0.05)
        assert page._pending_interaction_update is None
    finally:
        page.deleteLater()


def test_alt_from_drag_carries_to_corner_along(qapp):
    """Alignment mode (screen replacement): Alt-dragging a source corner moves
    the matching destination corner by the same delta, so from==to is kept and
    the homography stays identity — the image never warps while pinning the
    corners onto the content (e.g. a TV screen)."""
    page, node = _make_page_with_cornerpin()
    try:
        state = _drag_state(handle="from1", modifiers=ALT)
        page._on_viewer_interaction_started(state)
        page._on_viewer_interaction_updated(state)

        pending = page._pending_interaction_update["params"]
        assert pending["from1_x"] == pytest.approx(0.1)
        assert pending["from1_y"] == pytest.approx(0.05)
        # Twin destination corner follows by the same delta.
        assert pending["to1_x"] == pytest.approx(0.1)
        assert pending["to1_y"] == pytest.approx(0.05)
        # Other corners untouched.
        assert "to2_x" not in pending
        # Alignment stays live (at identity the image simply doesn't warp).
        assert page._interaction_debounce_timer.isActive()
        page._interaction_debounce_timer.stop()

        # Committing keeps from1 == to1 → identity → no warp.
        original_params = state.end_drag()
        page._on_viewer_interaction_ended(state, original_params)
        assert node.parameters["from1_x"] == pytest.approx(node.parameters["to1_x"])
        assert node.parameters["from1_y"] == pytest.approx(node.parameters["to1_y"])
        assert node._is_identity()
    finally:
        page.deleteLater()


def test_alt_shift_from_drag_moves_source_only(qapp):
    """Alt+Shift opts out of alignment mode: only the source corner moves and
    the drag stays live (Shift-freeze applies to destination handles only)."""
    page, node = _make_page_with_cornerpin()
    try:
        state = _drag_state(handle="from1", modifiers=ALT_SHIFT)
        page._on_viewer_interaction_started(state)
        page._on_viewer_interaction_updated(state)

        pending = page._pending_interaction_update["params"]
        assert pending["from1_x"] == pytest.approx(0.1)
        assert "to1_x" not in pending
        assert "to1_y" not in pending
        # Not frozen: live resampling feedback for source-region editing.
        assert page._interaction_debounce_timer.isActive()
        page._interaction_debounce_timer.stop()
    finally:
        page.deleteLater()


def test_transform_drag_is_not_frozen(qapp):
    """The Shift freeze is CornerPin-only; a Transform drag stays live even
    with Shift held (Shift has its own meaning there: free-axis scaling)."""
    from src.features.composite.nodes.transform_node import TransformNode

    page = CompositePage(parent=None)
    try:
        node = TransformNode(node_id="t1")
        page._node_graph.add_node(node)
        page._evaluate_graph_now = lambda: None
        page._refresh_properties_panel = lambda node_id: None
        page.update_viewer_handles = lambda: None

        state = ViewerInteractionState()
        state.start_drag(
            tool=ViewerToolMode.TRANSFORM,
            node_id="t1",
            handle="center",
            view_pos=QPointF(50.0, 50.0),
            ndc_pos=QPointF(0.0, 0.0),
            image_pos=QPointF(50.0, 50.0),
            format_pos=QPointF(0.0, 0.0),
            original_params={"translate_x": 0.0, "translate_y": 0.0},
            modifiers=SHIFT,
        )
        state.update_drag(
            QPointF(60.0, 60.0), image_pos=QPointF(60.0, 60.0),
            modifiers=SHIFT)
        page._interaction_active = True
        page._on_viewer_interaction_updated(state)

        # Live path even with Shift: throttle timer armed.
        assert page._interaction_debounce_timer.isActive()
        page._interaction_debounce_timer.stop()
    finally:
        page.deleteLater()
