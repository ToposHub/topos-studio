"""Regression tests for CompositeViewer CornerPin handle display and drag.

Covers the gaps that made CornerPin handles unusable compared to the mature
Transform overlay:

1. CornerPin handles were only drawn in the CORNERPIN tool mode; the handle
   table (``_tool_handles``) must be populated for a selected cornerpin node
   so the always-on overlay can draw/hit-test them in any tool mode.
2. ``_handle_cornerpin_press`` relied on the stale ``_hover_handle`` and
   passed ``original_params={}`` into the drag, so the page-side commit math
   baselined on defaults (corner jump) and undo recorded an empty snapshot.
3. During a drag the overlay must track the pointer live
   (``_current_cornerpin_params_for_overlay``), mirroring the Transform
   overlay's throttling-proof feedback.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPointF, Qt

from src.features.composite.ui.composite_viewport import CompositeViewer
from src.features.composite.ui.viewer_tool_system import ViewerToolMode


CORNERPIN_PARAMS = {
    "to1_x": 0.1, "to1_y": 0.1,
    "to2_x": 0.9, "to2_y": 0.1,
    "to3_x": 0.9, "to3_y": 0.9,
    "to4_x": 0.1, "to4_y": 0.9,
    "from1_x": 0.0, "from1_y": 0.0,
    "from2_x": 1.0, "from2_y": 0.0,
    "from3_x": 1.0, "from3_y": 1.0,
    "from4_x": 0.0, "from4_y": 1.0,
}


@pytest.fixture
def viewer(qapp):
    """A CompositeViewer with GL/geometry neutralized for pure logic tests."""
    v = CompositeViewer(parent=None)
    v._tex_width = 100
    v._tex_height = 100
    v.resize(200, 200)
    # Bypass view→image geometry math; mirror view pos as image pos so the
    # drag delta is simply (move - start) in "image" pixels.
    v.get_image_pos_at_view_pos = lambda vp, check_bounds=True: QPointF(vp.x(), vp.y())
    yield v
    v.deleteLater()


def _select_cornerpin(viewer, params=None):
    params = dict(params or CORNERPIN_PARAMS)
    viewer.set_node_info("cornerpin", params)
    viewer.set_selected_node("cp1")
    return params


class TestCornerPinHandleTable:
    def test_selected_cornerpin_populates_handle_table(self, viewer):
        _select_cornerpin(viewer)

        for hid in ("to1", "to2", "to3", "to4", "from1", "from2", "from3", "from4"):
            assert hid in viewer._tool_handles, f"missing handle {hid}"

    def test_handles_follow_node_params(self, viewer):
        params = dict(CORNERPIN_PARAMS)
        params["to1_x"], params["to1_y"] = 0.5, 0.5
        _select_cornerpin(viewer, params)

        # to1 moved to the image centre: its handle must differ from the
        # default-corner position and be a finite view point.
        pos = viewer._tool_handles["to1"]
        assert pos.x() == pytest.approx(viewer.width() / 2.0, abs=2.0)
        assert pos.y() == pytest.approx(viewer.height() / 2.0, abs=2.0)


class TestCornerPinPress:
    def test_press_on_to_handle_starts_drag_with_original_params(self, viewer):
        params = _select_cornerpin(viewer)
        handle_pos = viewer._tool_handles["to1"]

        handled = viewer._handle_cornerpin_press(
            QPointF(handle_pos), Qt.KeyboardModifier.NoModifier)

        assert handled
        st = viewer._interaction_state
        assert st.is_dragging
        assert st.tool == ViewerToolMode.CORNERPIN
        assert st.active_handle == "to1"
        # Undo/preview snapshot must carry the node params (regression: {}).
        assert st.original_params["to1_x"] == pytest.approx(params["to1_x"])
        assert st.original_params["to1_y"] == pytest.approx(params["to1_y"])

    def test_press_on_from_handle_is_rejected(self, viewer):
        _select_cornerpin(viewer)
        handle_pos = viewer._tool_handles["from1"]

        handled = viewer._handle_cornerpin_press(
            QPointF(handle_pos), Qt.KeyboardModifier.NoModifier)

        assert not handled
        assert not viewer._interaction_state.is_dragging

    def test_press_on_empty_space_is_rejected(self, viewer):
        _select_cornerpin(viewer)
        # Far outside any handle's hit radius.
        handled = viewer._handle_cornerpin_press(
            QPointF(-1000.0, -1000.0), Qt.KeyboardModifier.NoModifier)

        assert not handled

    def test_alt_press_grabs_source_corner(self, viewer):
        """Alt + drag edits the from* (source) corners."""
        params = _select_cornerpin(viewer)
        handle_pos = viewer._tool_handles["from1"]

        handled = viewer._handle_cornerpin_press(
            QPointF(handle_pos), Qt.KeyboardModifier.AltModifier)

        assert handled
        st = viewer._interaction_state
        assert st.is_dragging
        assert st.active_handle == "from1"
        assert st.original_params["from1_x"] == pytest.approx(params["from1_x"])

    def test_alt_press_at_coincident_corners_prefers_source(self, viewer):
        """At identity the to/from corners coincide; Alt must pick from*."""
        ident = {
            "to1_x": 0.0, "to1_y": 0.0,
            "to2_x": 1.0, "to2_y": 0.0,
            "to3_x": 1.0, "to3_y": 1.0,
            "to4_x": 0.0, "to4_y": 1.0,
            "from1_x": 0.0, "from1_y": 0.0,
            "from2_x": 1.0, "from2_y": 0.0,
            "from3_x": 1.0, "from3_y": 1.0,
            "from4_x": 0.0, "from4_y": 1.0,
        }
        _select_cornerpin(viewer, ident)
        handle_pos = viewer._tool_handles["to1"]  # same spot as from1

        assert viewer._handle_cornerpin_press(
            QPointF(handle_pos), Qt.KeyboardModifier.AltModifier)
        assert viewer._interaction_state.active_handle == "from1"

        viewer._interaction_state.reset()
        assert viewer._handle_cornerpin_press(
            QPointF(handle_pos), Qt.KeyboardModifier.NoModifier)
        assert viewer._interaction_state.active_handle == "to1"


class TestCornerPinDragTracking:
    def _start_to1_drag(self, viewer, start=None):
        _select_cornerpin(viewer)
        start = start or viewer._tool_handles["to1"]
        assert viewer._handle_cornerpin_press(
            QPointF(start), Qt.KeyboardModifier.NoModifier)
        return QPointF(start)

    def test_overlay_params_track_pointer_during_drag(self, viewer):
        start = self._start_to1_drag(viewer)

        viewer._handle_cornerpin_move(
            QPointF(start.x() + 10.0, start.y() + 20.0),
            Qt.KeyboardModifier.NoModifier)

        params = viewer._current_cornerpin_params_for_overlay()
        # Image delta (10, 20) normalized by the 100x100 texture.
        assert params["to1_x"] == pytest.approx(CORNERPIN_PARAMS["to1_x"] + 0.1)
        assert params["to1_y"] == pytest.approx(CORNERPIN_PARAMS["to1_y"] + 0.2)
        # Other corners untouched.
        assert params["to3_x"] == pytest.approx(CORNERPIN_PARAMS["to3_x"])
        assert params["to3_y"] == pytest.approx(CORNERPIN_PARAMS["to3_y"])

    def test_overlay_params_ignore_throttled_node_updates(self, viewer):
        """Live overlay math must baseline on the drag-start snapshot, not the
        (throttled) node params, otherwise the handle drifts mid-drag."""
        start = self._start_to1_drag(viewer)
        viewer._handle_cornerpin_move(
            QPointF(start.x() + 10.0, start.y() + 20.0),
            Qt.KeyboardModifier.NoModifier)

        # Simulate a throttled partial node update landing mid-drag.
        viewer._selected_node_params["to1_x"] = 0.33

        params = viewer._current_cornerpin_params_for_overlay()
        assert params["to1_x"] == pytest.approx(CORNERPIN_PARAMS["to1_x"] + 0.1)

    def test_release_returns_original_params_for_undo(self, viewer):
        self._start_to1_drag(viewer)

        original = viewer._handle_cornerpin_release(QPointF(0.0, 0.0))

        assert original["to1_x"] == pytest.approx(CORNERPIN_PARAMS["to1_x"])
        assert not viewer._interaction_state.is_dragging

    def test_alt_drag_alignment_carries_twin_to_corner(self, viewer):
        """Alignment mode: Alt-dragging a source corner carries the matching
        destination corner along, so from==to stays true and the warp remains
        identity — the image never deforms while pinning corners to content."""
        _select_cornerpin(viewer)
        start = viewer._tool_handles["from1"]
        assert viewer._handle_cornerpin_press(
            QPointF(start), Qt.KeyboardModifier.AltModifier)

        viewer._handle_cornerpin_move(
            QPointF(start.x() + 10.0, start.y() - 20.0),
            Qt.KeyboardModifier.AltModifier)

        params = viewer._current_cornerpin_params_for_overlay()
        assert params["from1_x"] == pytest.approx(CORNERPIN_PARAMS["from1_x"] + 0.1)
        assert params["from1_y"] == pytest.approx(CORNERPIN_PARAMS["from1_y"] - 0.2)
        # Twin destination corner follows by the same delta.
        assert params["to1_x"] == pytest.approx(CORNERPIN_PARAMS["to1_x"] + 0.1)
        assert params["to1_y"] == pytest.approx(CORNERPIN_PARAMS["to1_y"] - 0.2)
        # Other corners untouched.
        assert params["to3_x"] == pytest.approx(CORNERPIN_PARAMS["to3_x"])

    def test_alt_shift_drag_moves_source_corner_only(self, viewer):
        """Alt+Shift opts out of alignment mode: only the source corner moves
        (redefine the source region)."""
        _select_cornerpin(viewer)
        start = viewer._tool_handles["from1"]
        mods = (Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.ShiftModifier)
        assert viewer._handle_cornerpin_press(QPointF(start), mods)

        viewer._handle_cornerpin_move(
            QPointF(start.x() + 10.0, start.y() - 20.0), mods)

        params = viewer._current_cornerpin_params_for_overlay()
        assert params["from1_x"] == pytest.approx(CORNERPIN_PARAMS["from1_x"] + 0.1)
        assert params["from1_y"] == pytest.approx(CORNERPIN_PARAMS["from1_y"] - 0.2)
        # Destination corners untouched.
        assert params["to1_x"] == pytest.approx(CORNERPIN_PARAMS["to1_x"])
        assert params["to1_y"] == pytest.approx(CORNERPIN_PARAMS["to1_y"])

    def test_move_does_not_raise_for_any_to_handle(self, viewer):
        for hid in ("to1", "to2", "to3", "to4"):
            viewer._interaction_state.reset()
            _select_cornerpin(viewer)
            pos = viewer._tool_handles[hid]
            assert viewer._handle_cornerpin_press(
                QPointF(pos), Qt.KeyboardModifier.NoModifier)
            viewer._handle_cornerpin_move(
                QPointF(pos.x() + 5.0, pos.y() - 5.0),
                Qt.KeyboardModifier.NoModifier)
