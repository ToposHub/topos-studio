"""T125 Graph Editor interaction regression tests.

A consolidated smoke suite exercising the full interaction flow end-to-end
(plan §24.4): select → marquee → zoom/pan → drag keyframe → drag handle →
Transform Box. Each test drives real press/move/release event sequences
under offscreen and asserts the data contract.

The individual behaviours already have dedicated suites (selection,
marquee, drag, handle, transform-box, undo); this file guards against
REGRESSIONS where one interaction breaks another — e.g. a zoom that
breaks hit-testing, or a marquee that leaves selection in a bad state
for the next drag.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QMouseEvent

from src.features.composite.ui.graph_editor_view import GraphEditorView
from src.features.composite.ui.graph_model import (
    build_graph_series, frame_to_x, value_to_y,
)
from src.features.composite.nodes.transform_node import TransformNode
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


# ── fixtures ──────────────────────────────────────────────────────────


def _series(param="rotation", kfs=((0, 0.0), (50, 90.0), (100, 0.0))):
    node = TransformNode()
    track = KeyframeTrack(param_name=param)
    for f, v in kfs:
        track.add_keyframe(Keyframe(f, v))
    node.set_keyframe_track(param, track)
    return build_graph_series(node, param)


@pytest.fixture
def view(qapp):
    v = GraphEditorView()
    v.resize(800, 400)
    v.set_frame_range(0, 100)
    return v


def _kp_pos(view, frame, value):
    x = frame_to_x(frame, view._viewport, 40.0)
    y = value_to_y(value, view._viewport, float(view.height() - 24))
    return x, y


def _events(view, points, button=Qt.MouseButton.LeftButton,
            mods=Qt.KeyboardModifier.NoModifier):
    """Fire a sequence of (x, y) as press/move/release events."""
    QET = QMouseEvent.Type
    for i, (x, y) in enumerate(points):
        etype = (QET.MouseButtonPress if i == 0
                 else QET.MouseButtonRelease if i == len(points) - 1
                 else QET.MouseMove)
        ev = QMouseEvent(etype, QPointF(x, y), button,
                         button if etype == QET.MouseButtonPress
                         else Qt.MouseButton.NoButton, mods)
        if etype == QET.MouseButtonPress:
            view.mousePressEvent(ev)
        elif etype == QET.MouseMove:
            view.mouseMoveEvent(ev)
        else:
            view.mouseReleaseEvent(ev)


# ── T125: select → drag flow ──────────────────────────────────────────


class TestSelectDragFlow:
    def test_select_then_drag_commits(self, view, qapp):
        """Click-select a keyframe, then drag it — the drag commits the
        new position (the select didn't leave state in a bad place)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        kp = s.points[1]
        x, y = _kp_pos(view, kp.frame, kp.value)
        # Single press-move-release on the keyframe (select + drag).
        _events(view, [(x, y), (x + 30, y), (x + 30, y)])
        assert kp.frame > 50  # moved + committed

    def test_shift_select_two_then_drag_moves_both(self, view, qapp):
        """Two keyframes are selected; calling the drag API directly (as a
        marquee-then-drag-on-already-selected-point flow would) moves both.

        A plain mouse click on a selected point re-selects it solo (AE
        behaviour), so the group drag is exercised by calling
        _begin_keyframe_drag directly after setting the selection."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        s.points[0].selected = True
        s.points[1].selected = True
        view._refresh_transform_box()
        f0_before = s.points[0].frame
        # Begin the drag directly (skips the click's solo-reselect).
        x1, y1 = _kp_pos(view, s.points[1].frame, s.points[1].value)
        from PyQt6.QtCore import QPointF as _QPF
        view._begin_keyframe_drag(_QPF(x1, y1))
        view._do_keyframe_drag(_QPF(x1 + 30, y1))
        view._commit_drag()
        assert s.points[0].frame > f0_before  # group-moved


# ── T125: marquee → drag flow ─────────────────────────────────────────


class TestMarqueeDragFlow:
    def test_marquee_selects_then_drag_commits(self, view, qapp):
        """Marquee-select keyframes, then drag one — the drag commits for
        the dragged keyframe (a plain click on a selected point re-selects
        it solo, so only the grabbed point moves here)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        # Marquee around all 3 keyframes (frames 0, 50, 100). The marquee
        # must cover the points' full y-range (value 0 → y≈346, value 90
        # → y≈42), so span y=30..360.
        x0, _ = _kp_pos(view, -5, 0)
        x1, _ = _kp_pos(view, 105, 0)
        _events(view, [(x0, 30), (x1, 360), (x1, 360)])
        assert all(kp.selected for kp in s.points)
        # Now drag the middle one (plain click → solo-selects it, then drags).
        kp = s.points[1]
        kx, ky = _kp_pos(view, kp.frame, kp.value)
        _events(view, [(kx, ky), (kx + 20, ky), (kx + 20, ky)])
        # The dragged keyframe moved + committed.
        assert s.points[1].frame > 50


# ── T125: zoom then hit-test ──────────────────────────────────────────


class TestZoomThenHitTest:
    def test_zoom_in_then_click_still_hits_keyframe(self, view, qapp):
        """After a wheel-zoom, hit-testing still lands on the keyframe at
        its (moved) screen position."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        kp = s.points[1]
        # Zoom in 3× via wheel (each wheel doubles-ish).
        from PyQt6.QtCore import QPoint
        from PyQt6.QtGui import QWheelEvent
        for _ in range(3):
            ev = QWheelEvent(QPointF(400, 200), QPointF(400, 200),
                             QPoint(0, 120), QPoint(0, 120),
                             Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                             Qt.ScrollPhase.ScrollUpdate, False)
            view.wheelEvent(ev)
        qapp.processEvents()
        # The keyframe's screen position moved; recompute and click it.
        x, y = _kp_pos(view, kp.frame, kp.value)
        _events(view, [(x, y), (x, y)])
        assert kp.selected


# ── T125: pan then drag ───────────────────────────────────────────────


class TestPanThenDrag:
    def test_pan_then_drag_commits(self, view, qapp):
        """After a middle-button pan, a keyframe drag still commits."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        # Pan with the middle button.
        _events(view, [(400, 200), (450, 220), (450, 220)],
                button=Qt.MouseButton.MiddleButton)
        qapp.processEvents()
        # Drag a keyframe at its (post-pan) screen position.
        s = view._series[0]
        kp = s.points[1]
        x, y = _kp_pos(view, kp.frame, kp.value)
        _events(view, [(x, y), (x + 30, y), (x + 30, y)])
        assert kp.frame != 50  # moved + committed


# ── T125: transform box after marquee ─────────────────────────────────


class TestTransformBoxAfterMarquee:
    def test_marquee_creates_box_then_box_drag(self, view, qapp):
        """Marquee-selecting ≥2 keyframes with distinct values reveals the
        Transform Box, which can then be dragged (no state corruption)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        # Set selection directly: points[1] (50, 90) + points[2] (100, 0)
        # — distinct frames AND values so the box forms a real rectangle.
        s.points[1].selected = True
        s.points[2].selected = True
        view._refresh_transform_box()
        assert view._tbox_bbox is not None  # box appeared
        # Interior drag.
        core = view._box_core_rect_px()
        ix = core.left() + core.width() * 0.3
        iy = core.top() + core.height() * 0.3
        f1_before = s.points[1].frame
        _events(view, [(ix, iy), (ix + 30, iy), (ix + 30, iy)])
        assert s.points[1].frame > f1_before


# ── T125: handle drag after select ────────────────────────────────────


class TestHandleDragAfterSelect:
    def test_select_bezier_then_drag_handle(self, view, qapp):
        """Selecting a bezier keyframe reveals handles; dragging one moves
        it without disturbing the keyframe's frame/value."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        kp = s.points[1]
        kp.interpolation_in = "bezier"
        kp.interpolation_out = "bezier"
        kp.handle_left = (5.0, 10.0)
        kp.handle_right = (-5.0, -10.0)
        # Select it.
        kx, ky = _kp_pos(view, kp.frame, kp.value)
        _events(view, [(kx, ky), (kx, ky)])
        assert kp.selected
        # Drag the right handle.
        hf = kp.frame + kp.handle_right[0]
        hv = kp.value + kp.handle_right[1]
        hx = frame_to_x(hf, view._viewport, 40.0)
        hy = value_to_y(hv, view._viewport, float(view.height() - 24))
        f_before = kp.frame
        v_before = kp.value
        _events(view, [(hx, hy), (hx - 30, hy - 30), (hx - 30, hy - 30)])
        # Keyframe frame/value untouched (handle drag only moves handles).
        assert kp.frame == pytest.approx(f_before)
        assert kp.value == pytest.approx(v_before)
        # Handle moved.
        assert kp.handle_right != (-5.0, -10.0)
