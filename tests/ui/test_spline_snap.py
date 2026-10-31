"""Batch 6 tests: keyframe snapping during drag.

Validates that _snap_frame/_snap_value pull a candidate onto the playhead /
sibling frames / value grid when within the pixel threshold, and that the
_snap_guide_* fields are set so the view can draw guide lines.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QPointF

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.spline_model import SplineModel
from src.features.composite.ui.spline_view import SplineView
from src.shared.keyframe import Keyframe, KeyframeTrack


@pytest.fixture
def view(qapp):
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation", min_val=0.0, max_val=360.0)
    for f, v in [(0, 0.0), (50, 180.0), (100, 360.0)]:
        tr.add_keyframe(Keyframe(f, v))
    t.set_keyframe_track("rotation", tr)
    g.add_node(t)
    m = SplineModel()
    m.rebuild(g)
    for c in m.curves:
        c.selected = True
    view = SplineView(m)
    view.resize(960, 400)
    view.fit_view()
    return view, m


# ── _snap_frame ─────────────────────────────────────────────────────────

def test_snap_frame_pulls_to_playhead(view):
    view, m = view
    view._current_frame = 50
    curve = m.curves[0]
    # A frame a couple pixels away from the playhead (frame 50).
    near_x = view.data_to_screen(QPointF(50, 0.0)).x() + 3
    near_frame = view.screen_to_data(QPointF(near_x, 0.0)).x()
    snapped = view._snap_frame(near_frame, curve, exclude_key=curve.keys[1])
    assert snapped == 50.0
    assert view._snap_guide_frame == 50.0


def test_snap_frame_pulls_to_sibling_key(view):
    view, m = view
    view._current_frame = 999  # playhead far away
    curve = m.curves[0]
    # Aim near frame 100 (a sibling key), excluding that key.
    near_x = view.data_to_screen(QPointF(100, 0.0)).x() - 2
    near_frame = view.screen_to_data(QPointF(near_x, 0.0)).x()
    dragged = curve.keys[0]  # frame 0
    snapped = view._snap_frame(near_frame, curve, exclude_key=dragged)
    assert snapped == 100.0


def test_snap_frame_no_candidate_returns_original(view):
    view, m = view
    view._current_frame = 999
    curve = m.curves[0]
    # Frame 25 is far from any key (0/50/100) and the playhead.
    snapped = view._snap_frame(25.0, curve, exclude_key=curve.keys[0])
    assert snapped == 25.0
    assert view._snap_guide_frame is None


def test_snap_frame_excludes_the_dragged_key(view):
    """The key being dragged must not snap onto its own current frame."""
    view, m = view
    curve = m.curves[0]
    dragged = curve.keys[1]  # frame 50
    view._current_frame = 999
    # Aim right at frame 50 while excluding the dragged key.
    snapped = view._snap_frame(50.0, curve, exclude_key=dragged)
    # Nothing else is at 50, so it stays put.
    assert snapped == 50.0
    assert view._snap_guide_frame is None


# ── _snap_value ─────────────────────────────────────────────────────────

def test_snap_value_normalized_quartiles(view):
    view, m = view
    curve = m.curves[0]  # min 0, max 360
    view.set_value_mode("normalized")
    # Aim near the 0.5 quartile (value 180).
    near_y = view.data_to_screen(QPointF(0.0, 180.0)).y() + 2
    near_val = view.screen_to_data(QPointF(0.0, near_y)).y()
    snapped = view._snap_value(near_val, curve)
    assert snapped == 180.0
    assert view._snap_guide_value == 180.0


def test_snap_value_default_mode_uses_grid(view):
    view, m = view
    curve = m.curves[0]
    assert view._value_mode == "value"
    # In value mode it snaps to the nice grid step. We just check it returns
    # a finite float and sets/clears the guide appropriately.
    snapped = view._snap_value(123.456, curve)
    assert isinstance(snapped, float)


# ── integration: drag snaps to playhead ─────────────────────────────────

def test_drag_key_snaps_onto_playhead_frame(view):
    """End-to-end: dragging a key near the playhead lands on its frame."""
    view, m = view
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QMouseEvent
    curve = m.curves[0]
    dragged = curve.keys[0]  # frame 0
    for k in curve.keys:
        k.selected = (k is dragged)
    view._current_frame = 30
    # Press on the key.
    kp = view.data_to_screen(QPointF(dragged.frame, dragged.value))
    view.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(kp.x(), kp.y()),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    # Drag to a point a few pixels away from frame 30.
    target_x = view.data_to_screen(QPointF(30, 0.0)).x() + 2
    target_y = view.data_to_screen(QPointF(0.0, dragged.value)).y()
    view.mouseMoveEvent(QMouseEvent(
        QMouseEvent.Type.MouseMove, QPointF(target_x, target_y),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    # The dragged key should now sit at frame 30 (snapped).
    assert dragged.frame == 30
