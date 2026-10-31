"""T115 (Speed Graph drag) + T117 (Transform Box) integration tests.

View-level tests that drive GraphEditorView through real press/move/release
event sequences (offscreen) and assert the data contract:

T115 — Speed Graph single-point drag: in a speed projection the Y axis is
derived (slope), so dragging a keyframe must move only its frame and must
NOT touch its value (which would mis-edit the original keyframe).

T117 — Transform Box: with ≥2 selected keyframes spanning ≥2 frames a box
appears; dragging inside translates, dragging an edge/corner scales around
the anchor, dragging the anchor moves only the pivot, and cross-parameter
scaling is time-only (values frozen).
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QMouseEvent

from src.features.composite.ui.graph_editor_view import GraphEditorView
from src.features.composite.ui.graph_model import (
    build_graph_series, frame_to_x, value_to_y,
)
from src.features.composite.nodes.transform_node import TransformNode
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


# ── fixtures ──────────────────────────────────────────────────────────


def _series(param="rotation", kfs=((0, 0.0), (50, 90.0), (100, 0.0)),
            graph_type="value"):
    node = TransformNode()
    track = KeyframeTrack(param_name=param)
    for f, v in kfs:
        track.add_keyframe(Keyframe(f, v))
    node.set_keyframe_track(param, track)
    s = build_graph_series(node, param, node_color="#ff8800")
    s.graph_type = graph_type
    return s


@pytest.fixture
def view(qapp):
    v = GraphEditorView()
    v.resize(800, 400)
    v.set_frame_range(0, 100)
    return v


def _kp_pos(view, frame, value):
    x = frame_to_x(frame, view._viewport, 40.0)        # _MARGIN_LEFT
    y = value_to_y(value, view._viewport,
                    float(view.height() - 24))          # _MARGIN_BOTTOM
    return x, y


def _press_move_release(view, x1, y1, x2, y2, mods=None):
    if mods is None:
        mods = Qt.KeyboardModifier.NoModifier
    QET = QMouseEvent.Type
    for etype, x, y in (
            (QET.MouseButtonPress, x1, y1),
            (QET.MouseMove, x2, y2),
            (QET.MouseButtonRelease, x2, y2)):
        ev = QMouseEvent(etype, QPointF(x, y),
                         Qt.MouseButton.LeftButton,
                         Qt.MouseButton.LeftButton
                         if etype == QET.MouseButtonPress
                         else Qt.MouseButton.NoButton,
                         mods)
        if etype == QET.MouseButtonPress:
            view.mousePressEvent(ev)
        elif etype == QET.MouseMove:
            view.mouseMoveEvent(ev)
        else:
            view.mouseReleaseEvent(ev)


# ── T115: Speed Graph drag is frame-only ──────────────────────────────


class TestSpeedGraphDrag:
    def test_speed_drag_moves_frame(self, view, qapp):
        """T115: in a speed graph, horizontal drag still moves the frame."""
        view.set_series([_series(graph_type="speed")])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        kp = s.points[1]  # frame 50
        kp.selected = True
        kp_x, kp_y = _kp_pos(view, kp.frame, kp.value)
        _press_move_release(view, kp_x, kp_y, kp_x + 30, kp_y)
        assert kp.frame > 50  # frame moved

    def test_speed_drag_freezes_value(self, view, qapp):
        """T115: in a speed graph, vertical drag must NOT change the value
        (the Y axis is derived — editing it would mis-edit the keyframe)."""
        view.set_series([_series(graph_type="speed")])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        kp = s.points[1]
        value_before = kp.value
        kp.selected = True
        kp_x, kp_y = _kp_pos(view, kp.frame, kp.value)
        # Drag down by 50px (would change value in a value graph).
        _press_move_release(view, kp_x, kp_y, kp_x, kp_y + 50)
        assert kp.value == pytest.approx(value_before)

    def test_value_drag_does_change_value(self, view, qapp):
        """Sanity: in a value graph, vertical drag DOES change the value
        (confirms the speed-graph freeze is a speed-only behaviour)."""
        view.set_series([_series(graph_type="value")])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        kp = s.points[1]
        value_before = kp.value
        kp.selected = True
        kp_x, kp_y = _kp_pos(view, kp.frame, kp.value)
        _press_move_release(view, kp_x, kp_y, kp_x, kp_y + 40)
        assert kp.value != pytest.approx(value_before)


# ── T117: Transform Box visibility ────────────────────────────────────


class TestTransformBoxVisibility:
    def test_box_hidden_with_single_selection(self, view, qapp):
        """One selected keyframe → no box (needs ≥2 across ≥2 frames)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        view._series[0].points[1].selected = True
        view._refresh_transform_box()
        assert view._tbox_bbox is None

    def test_box_hidden_with_same_frame_selection(self, view, qapp):
        """Two selected points at the same frame → no box (zero time span)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        # Both at frame 0 isn't possible here, so force same-frame.
        s = view._series[0]
        s.points[0].selected = True
        s.points[0].frame = 10.0
        # Make points[1] also frame 10 temporarily.
        orig = s.points[1].frame
        s.points[1].frame = 10.0
        s.points[1].selected = True
        view._refresh_transform_box()
        assert view._tbox_bbox is None
        s.points[1].frame = orig  # restore

    def test_box_shown_with_two_distinct_frames(self, view, qapp):
        """Two selected keyframes across ≥2 frames → box appears."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        s.points[0].selected = True
        s.points[2].selected = True
        view._refresh_transform_box()
        assert view._tbox_bbox is not None
        fmin, fmax, vmin, vmax = view._tbox_bbox
        assert fmin == 0.0 and fmax == 100.0

    def test_box_anchor_defaults_to_centre(self, view, qapp):
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        s.points[0].selected = True
        s.points[2].selected = True
        view._refresh_transform_box()
        af, av = view._tbox_anchor
        assert af == pytest.approx(50.0)  # (0+100)/2

    def test_box_paints_without_crash(self, view, qapp):
        """The box + anchor + grips must render without throwing."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        s.points[0].selected = True
        s.points[2].selected = True
        view._refresh_transform_box()
        view.repaint()  # force a paint
        # No exception = pass.


# ── T117: hit-testing the box ─────────────────────────────────────────


class TestTransformBoxHitTest:
    def _setup_box(self, view, qapp):
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        # points[1] (frame 50, value 90) + points[2] (frame 100, value 0)
        # gives a genuine 2-D box (distinct frames AND distinct values).
        s.points[1].selected = True
        s.points[2].selected = True
        view._refresh_transform_box()
        return view._box_core_rect_px()

    def test_hit_interior(self, view, qapp):
        core = self._setup_box(view, qapp)
        # A point strictly inside, away from the centre (which is the
        # anchor) and away from every edge.
        ix = core.left() + core.width() * 0.25
        iy = core.top() + core.height() * 0.25
        grip = view._hit_transform_box(QPointF(ix, iy))
        assert grip == "inside"

    def test_hit_corner(self, view, qapp):
        core = self._setup_box(view, qapp)
        grip = view._hit_transform_box(QPointF(core.topLeft()))
        assert grip == "nw"

    def test_hit_edge(self, view, qapp):
        core = self._setup_box(view, qapp)
        # Top edge, off-centre so we don't hit the anchor.
        grip = view._hit_transform_box(
            QPointF(core.left() + core.width() * 0.25, core.top()))
        assert grip == "n"

    def test_hit_anchor(self, view, qapp):
        self._setup_box(view, qapp)
        af, av = view._tbox_anchor
        ax = frame_to_x(af, view._viewport, 40.0)
        ay = value_to_y(av, view._viewport, float(view.height() - 24))
        grip = view._hit_transform_box(QPointF(ax, ay))
        assert grip == "anchor"

    def test_miss_outside_box(self, view, qapp):
        self._setup_box(view, qapp)
        # Well outside the box.
        grip = view._hit_transform_box(QPointF(5, 5))
        assert grip is None

    def test_no_hit_when_box_hidden(self, view, qapp):
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        # No selection → no box.
        assert view._hit_transform_box(QPointF(400, 200)) is None


# ── T117: interior move drag ──────────────────────────────────────────


class TestTransformBoxMove:
    def test_interior_drag_moves_all_selected(self, view, qapp):
        """Dragging inside the box translates every selected point."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        # points[1] (50, 90) + points[2] (100, 0) → 2-D box.
        s.points[1].selected = True
        s.points[2].selected = True
        view._refresh_transform_box()
        f1_before = s.points[1].frame
        f2_before = s.points[2].frame
        core = view._box_core_rect_px()
        # Click an interior point OFF the anchor (centre) so we grab the
        # box, not the anchor handle.
        ix = core.left() + core.width() * 0.3
        iy = core.top() + core.height() * 0.3
        # Drag right by 30px → frames increase.
        _press_move_release(view, ix, iy, ix + 30, iy)
        assert s.points[1].frame > f1_before
        assert s.points[2].frame > f2_before

    def test_interior_drag_commits_per_series(self, view, qapp):
        """On release, keyframe_changed fires once per affected series."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        received = []
        view.keyframe_changed.connect(
            lambda nid, pn, kfs: received.append((nid, pn, kfs)))
        s = view._series[0]
        s.points[1].selected = True
        s.points[2].selected = True
        view._refresh_transform_box()
        core = view._box_core_rect_px()
        ix = core.left() + core.width() * 0.3
        iy = core.top() + core.height() * 0.3
        _press_move_release(view, ix, iy, ix + 30, iy)
        assert len(received) == 1  # one series affected


# ── T117: edge/corner scale drag ──────────────────────────────────────


class TestTransformBoxScale:
    def test_right_edge_drag_scales_time(self, view, qapp):
        """Dragging the right edge outward stretches the time span."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        s.points[0].selected = True
        s.points[2].selected = True
        view._refresh_transform_box()
        span_before = s.points[2].frame - s.points[0].frame
        core = view._box_core_rect_px()
        # Grab the right edge and drag it further right.
        rx, ry = core.right(), core.center().y()
        _press_move_release(view, rx, ry, rx + 60, ry)
        span_after = s.points[2].frame - s.points[0].frame
        assert span_after > span_before

    def test_corner_drag_scales_both_axes(self, view, qapp):
        """Dragging a corner scales both time and value (single param)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        s.points[0].selected = True
        s.points[2].selected = True
        view._refresh_transform_box()
        v_range_before = (s.points[2].value, s.points[0].value)
        core = view._box_core_rect_px()
        # Grab the NE corner and drag it up-right (expand).
        nx, ny = core.right(), core.top()
        _press_move_release(view, nx, ny, nx + 50, ny - 50)
        # Value range should have changed (both points' values rescaled).
        v_range_after = (s.points[2].value, s.points[0].value)
        assert v_range_after != v_range_before

    def test_scale_clamps_frame_to_zero(self, view, qapp):
        """Scaling can't push a keyframe to a negative frame."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        s.points[0].selected = True
        s.points[2].selected = True
        view._refresh_transform_box()
        core = view._box_core_rect_px()
        # Drag the left edge far left (compress toward anchor at centre).
        lx, ly = core.left(), core.center().y()
        _press_move_release(view, lx, ly, lx - 500, ly)
        assert all(p.frame >= 0 for p in s.points)


# ── T117: anchor drag ─────────────────────────────────────────────────


class TestTransformBoxAnchor:
    def test_anchor_drag_moves_only_anchor(self, view, qapp):
        """Dragging the anchor changes the pivot but NOT the keyframes."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        s.points[0].selected = True
        s.points[2].selected = True
        view._refresh_transform_box()
        af_before = view._tbox_anchor[0]
        f0_before = s.points[0].frame
        f2_before = s.points[2].frame
        af, av = view._tbox_anchor
        ax = frame_to_x(af, view._viewport, 40.0)
        ay = value_to_y(av, view._viewport, float(view.height() - 24))
        _press_move_release(view, ax, ay, ax + 20, ay)
        assert view._tbox_anchor[0] != pytest.approx(af_before)
        # Keyframes untouched.
        assert s.points[0].frame == f0_before
        assert s.points[2].frame == f2_before

    def test_anchor_clamped_to_bbox(self, view, qapp):
        """The anchor can't be dragged outside the box bounds."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        s.points[0].selected = True
        s.points[2].selected = True
        view._refresh_transform_box()
        af, av = view._tbox_anchor
        ax = frame_to_x(af, view._viewport, 40.0)
        ay = value_to_y(av, view._viewport, float(view.height() - 24))
        # Drag far beyond the right edge.
        _press_move_release(view, ax, ay, ax + 9999, ay)
        fmin, fmax, _vmin, _vmax = view._tbox_bbox
        assert view._tbox_anchor[0] <= fmax


# ── T117: cross-parameter (value frozen) ──────────────────────────────


class TestTransformBoxCrossParam:
    def test_cross_param_move_freezes_value(self, view, qapp):
        """When the selection spans two parameters, an interior move only
        shifts time — values stay put (plan §22.9 cross-param rule)."""
        view.set_series([_series(param="rotation"),
                         _series(param="scale")])
        view._series[1].row_id = "param:n:scale"  # distinct param
        view.show()
        qapp.processEvents()
        s0, s1 = view._series
        # points[1] (frame 50, value 90) + points[2] (frame 100, value 0):
        # distinct frames AND values so the box is a real 2-D rectangle.
        s0.points[1].selected = True
        s1.points[2].selected = True
        view._refresh_transform_box()
        assert view._tbox_bbox is not None  # box formed
        v0_before = s0.points[1].value
        v1_before = s1.points[2].value
        core = view._box_core_rect_px()
        ix = core.left() + core.width() * 0.3
        iy = core.top() + core.height() * 0.3
        # Drag down (would change values in a same-param move).
        _press_move_release(view, ix, iy, ix, iy + 60)
        assert s0.points[1].value == pytest.approx(v0_before)
        assert s1.points[2].value == pytest.approx(v1_before)

    def test_cross_param_scale_freezes_value(self, view, qapp):
        """Cross-parameter corner scaling only scales time, not values."""
        view.set_series([_series(param="rotation"),
                         _series(param="scale")])
        view._series[1].row_id = "param:n:scale"
        view.show()
        qapp.processEvents()
        s0, s1 = view._series
        s0.points[1].selected = True   # frame 50, value 90
        s1.points[2].selected = True   # frame 100, value 0
        view._refresh_transform_box()
        assert view._tbox_bbox is not None
        v0_before = s0.points[1].value
        v1_before = s1.points[2].value
        core = view._box_core_rect_px()
        nx, ny = core.right(), core.top()
        _press_move_release(view, nx, ny, nx + 50, ny - 50)
        assert s0.points[1].value == pytest.approx(v0_before)
        assert s1.points[2].value == pytest.approx(v1_before)
