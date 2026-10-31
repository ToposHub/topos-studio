"""Regression tests for CompositeViewer transform-handle drag (mouseMoveEvent).

Covers ``_handle_transform_move``. Two bugs lived here:

1. ``NameError: name 'handle_type' is not defined`` — the snapping block
   referenced an undefined ``handle_type`` in an ``elif``. It was only
   evaluated when ``is_translate`` was False, i.e. while dragging a *scale*
   or *rotate* handle, so those drags aborted the whole app.
2. ``is_translate = handle in ("center", "pivot")`` lumped the pivot handle
   in with the center/translate handle, so dragging the pivot snapped
   against ``translate_x``/``translate_y`` instead of ``pivot_x``/``pivot_y``
   (the consumer in composite_page treats them as distinct operations).
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QKeyEvent, QMouseEvent

from src.features.composite.ui import composite_viewport as cv
from src.features.composite.ui.composite_viewport import CompositeViewer
from src.features.composite.ui.viewer_tool_system import (
    SnapResult,
    ViewerToolMode,
)


@pytest.fixture
def viewer(qapp):
    """A CompositeViewer with GL/geometry neutralized for pure logic tests."""
    v = CompositeViewer(parent=None)
    v._tex_width = 100
    v._tex_height = 100
    v.resize(200, 200)
    v._selected_node_params = {
        "translate_x": 0.0, "translate_y": 0.0,
        "scale_x": 1.0, "scale_y": 1.0,
        "rotation": 0.0,
        "pivot_mode": "custom", "pivot_x": 50.0, "pivot_y": 50.0,
    }
    # Bypass view→image geometry math; mirror view pos as image pos so the
    # drag delta is simply (move - start). Signature mirrors the real method
    # (incl. the check_bounds kwarg passed by _handle_transform_move).
    v.get_image_pos_at_view_pos = lambda vp, check_bounds=True: QPointF(vp.x(), vp.y())
    yield v
    v.deleteLater()


def _start_drag(viewer, handle):
    start = QPointF(50, 50)
    viewer._interaction_state.start_drag(
        tool=ViewerToolMode.TRANSFORM,
        node_id="n1",
        handle=handle,
        view_pos=start,
        ndc_pos=QPointF(0, 0),
        image_pos=QPointF(50, 50),
        format_pos=QPointF(),
        original_params=viewer._selected_node_params,
        modifiers=0,
    )


class TestTransformMoveNoCrash:
    """Dragging any transform handle must not raise (regression: scale/rotate
    dragged the app into a NameError abort)."""

    @pytest.mark.parametrize("hid", [
        "scale_corner_tl", "scale_corner_br",
        "scale_x_left", "scale_x_right",
        "scale_y_top", "scale_y_bottom",
        "rotate",
        "center", "pivot",
    ])
    def test_move_does_not_raise(self, viewer, hid):
        _start_drag(viewer, hid)
        # Non-zero delta so the snapping branch is exercised.
        viewer._handle_transform_move(QPointF(70, 80), Qt.KeyboardModifier.NoModifier)


class TestSnappingReferenceParams:
    """Snapping must use the params matching the handle: center→translate,
    pivot→pivot. Captures the position handed to apply_snapping."""

    def _capture_snap_pos(self, monkeypatch):
        captured = {}

        def fake_apply_snapping(pos, cfg, w, h):
            captured["pos"] = QPointF(pos.x(), pos.y())
            return SnapResult(snapped_position=QPointF(pos.x(), pos.y()))

        monkeypatch.setattr(cv, "apply_snapping", fake_apply_snapping)
        return captured

    def test_center_snaps_against_translate(self, viewer, monkeypatch):
        viewer._selected_node_params.update(translate_x=10.0, translate_y=10.0)
        captured = self._capture_snap_pos(monkeypatch)

        _start_drag(viewer, "center")
        viewer._handle_transform_move(QPointF(70, 80), Qt.KeyboardModifier.NoModifier)
        # delta = (20, 30); translate ref (10, 10) -> (30, 40)
        assert captured["pos"].x() == pytest.approx(30.0)
        assert captured["pos"].y() == pytest.approx(40.0)

    def test_pivot_snaps_against_pivot(self, viewer, monkeypatch):
        viewer._selected_node_params.update(
            translate_x=0.0, translate_y=0.0, pivot_x=50.0, pivot_y=50.0)
        captured = self._capture_snap_pos(monkeypatch)

        _start_drag(viewer, "pivot")
        viewer._handle_transform_move(QPointF(70, 80), Qt.KeyboardModifier.NoModifier)
        # delta = (20, 30); pivot ref (50, 50) -> (70, 80), NOT translate (0,0)->(20,30)
        assert captured["pos"].x() == pytest.approx(70.0)
        assert captured["pos"].y() == pytest.approx(80.0)


class TestTransformDragFeedback:
    """Regression tests for transform drag feedback and end-of-drag state."""

    def test_overlay_params_track_preview_delta_immediately(self, viewer):
        _start_drag(viewer, "center")
        viewer._interaction_state.preview_params = {
            "delta_x": 20.0,
            "delta_y": 30.0,
        }

        params = viewer._current_transform_params_for_overlay()

        assert params["translate_x"] == pytest.approx(20.0)
        assert params["translate_y"] == pytest.approx(30.0)

    def test_overlay_params_track_body_move_delta_immediately(self, viewer):
        _start_drag(viewer, "move")
        viewer._interaction_state.preview_params = {
            "delta_x": 20.0,
            "delta_y": 30.0,
        }

        params = viewer._current_transform_params_for_overlay()

        assert params["translate_x"] == pytest.approx(20.0)
        assert params["translate_y"] == pytest.approx(30.0)

    @pytest.mark.skipif(
        os.environ.get("QT_QPA_PLATFORM") == "offscreen",
        reason="QOpenGLWidget is not supported on the offscreen platform, so "
               "the body-press → virtual-move path cannot engage; run this "
               "test on a real desktop GL session (macOS release matrix)",
    )
    def test_body_press_starts_virtual_move_drag(self, viewer):
        viewer.set_selected_node("n1")
        viewer.set_node_info("transform", viewer._selected_node_params)

        handled = viewer._handle_transform_press(
            QPointF(120, 130), Qt.KeyboardModifier.NoModifier)

        assert handled
        assert viewer._interaction_state.is_dragging
        assert viewer._interaction_state.active_handle == "move"

    def test_pending_drag_update_processes_latest_position(self, viewer, monkeypatch):
        captured = []

        def fake_handle_tool_move(view_pos, modifiers):
            captured.append((QPointF(view_pos), modifiers))

        monkeypatch.setattr(viewer, "_handle_tool_move", fake_handle_tool_move)
        _start_drag(viewer, "center")
        viewer._pending_drag_update = True
        viewer._pending_drag_view_pos = QPointF(70, 80)
        viewer._pending_drag_modifiers = Qt.KeyboardModifier.ShiftModifier

        viewer._process_pending_drag_update()

        assert len(captured) == 1
        assert captured[0][0] == QPointF(70, 80)
        assert captured[0][1] == Qt.KeyboardModifier.ShiftModifier
        assert viewer._pending_drag_view_pos is None

    def test_release_keeps_transform_handles_visible(self, viewer):
        viewer.set_selected_node("n1")
        viewer.set_node_info("transform", viewer._selected_node_params)
        _start_drag(viewer, "center")
        ended = []
        viewer.interaction_ended.connect(
            lambda state, original: ended.append((state.selected_node_id, dict(original)))
        )

        event = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(70, 80),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        viewer.mouseReleaseEvent(event)

        assert ended
        assert ended[-1][0] == "n1"
        assert viewer.get_selected_node() == "n1"
        assert not viewer._interaction_state.is_dragging
        assert "center" not in viewer._tool_handles
        assert "pivot" in viewer._tool_handles
        assert "rotate" in viewer._tool_handles

    def test_release_clears_snap_guide_lines(self, viewer):
        """Regression: a snap guide line that was active at release must be
        cleared, otherwise it persists on screen until the next press.

        Root cause: the live release path (mouseReleaseEvent) reset drag
        state and repainted but never cleared ``_snap_result``; the clears
        lived only in the per-tool release handlers, which are never invoked
        (``_handle_tool_release`` has no caller).
        """
        viewer.set_selected_node("n1")
        viewer.set_node_info("transform", viewer._selected_node_params)
        _start_drag(viewer, "center")
        # delta=(0,0) -> translate current_pos=(0,0) -> snaps to format edges.
        viewer._handle_transform_move(QPointF(50, 50), Qt.KeyboardModifier.NoModifier)
        assert viewer._snap_result is not None
        assert viewer._snap_result.is_snapped()  # a guide line WOULD draw now

        event = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(50, 50),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        viewer.mouseReleaseEvent(event)

        assert viewer._snap_result is None  # guide line must be gone

    def test_escape_cancel_clears_snap_guide_lines(self, viewer):
        """Esc-cancel of a snapped drag must also clear the guide line
        (same root cause as release: the drag-end path skipped the clear)."""
        viewer.set_selected_node("n1")
        viewer.set_node_info("transform", viewer._selected_node_params)
        _start_drag(viewer, "center")
        viewer._handle_transform_move(QPointF(50, 50), Qt.KeyboardModifier.NoModifier)
        assert viewer._snap_result is not None and viewer._snap_result.is_snapped()

        esc = QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        viewer.keyPressEvent(esc)

        assert not viewer._interaction_state.is_dragging
        assert viewer._snap_result is None
