"""Batch 3 tests: multi-key box-select drag + Shift additive selection.

Validates that after box-selecting several keys, dragging one of them moves
the whole group by the same delta, that keys stay ordered (no crossing), and
that Shift-click / Shift-box add to the selection instead of replacing it.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QMouseEvent

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.spline_model import SplineModel
from src.features.composite.ui.spline_view import SplineView
from src.shared.keyframe import Keyframe, KeyframeTrack


@pytest.fixture
def view(qapp):
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    for f, v in [(0, 0.0), (10, 30.0), (20, 60.0), (30, 90.0)]:
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


def _ev(typ, x, y, button=Qt.MouseButton.LeftButton,
        mods=Qt.KeyboardModifier.NoModifier):
    return QMouseEvent(typ, QPointF(x, y), button, button, mods)


# ── box-select then group drag ───────────────────────────────────────────

def test_box_select_then_drag_moves_all_selected(view):
    view, m = view
    curve = m.curves[0]
    # Select frames 10 and 20 manually (simulating a box-select result).
    for k in curve.keys:
        k.selected = k.frame in (10, 20)
    # Grab the frame-10 key and drag it right.
    kp10 = view.data_to_screen(QPointF(10, 30.0))
    target = view.screen_to_data(QPointF(kp10.x() + 40, kp10.y()))
    received = []
    view.keyframe_changed.connect(lambda nid, pn, kfs: received.append(kfs))
    view.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress, kp10.x(), kp10.y()))
    assert view._multi_drag is True
    view.mouseMoveEvent(_ev(QMouseEvent.Type.MouseMove, kp10.x() + 40, kp10.y()))
    view.mouseReleaseEvent(_ev(QMouseEvent.Type.MouseButtonRelease,
                               kp10.x() + 40, kp10.y()))
    # Both selected keys moved by the same delta.
    frames = [k.frame for k in curve.keys]
    assert 10 not in frames and 20 not in frames  # originals gone
    # The two selected keys stayed 10 frames apart.
    moved = sorted(f for f in frames if f > 0 and f != 30)
    assert len(moved) == 2
    assert moved[1] - moved[0] == 10
    assert received  # commit emitted


def test_drag_single_unselected_key_keeps_it_solo(view):
    """Clicking an unselected key drops the group and drags only that one."""
    view, m = view
    curve = m.curves[0]
    for k in curve.keys:
        k.selected = k.frame in (10, 20)
    # Click the unselected frame-30 key.
    kp30 = view.data_to_screen(QPointF(30, 90.0))
    view.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress, kp30.x(), kp30.y()))
    # Solo drag, not multi.
    assert view._multi_drag is False
    # The frame-30 key is now the only selected one.
    assert sum(1 for k in curve.keys if k.selected) == 1
    assert curve.keys[3].selected


def test_multi_drag_clamps_no_crossing(view):
    """Group drag must keep keys ordered — a key can't leapfrog its neighbour."""
    view, m = view
    curve = m.curves[0]
    # Select the first two keys (0 and 10) and drag the group far right.
    for k in curve.keys:
        k.selected = k.frame in (0, 10)
    kp0 = view.data_to_screen(QPointF(0, 0.0))
    far = view.data_to_screen(QPointF(60, 0.0))
    view.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress, kp0.x(), kp0.y()))
    view.mouseMoveEvent(_ev(QMouseEvent.Type.MouseMove, far.x(), kp0.y()))
    view.mouseReleaseEvent(_ev(QMouseEvent.Type.MouseButtonRelease,
                               far.x(), kp0.y()))
    frames = sorted(k.frame for k in curve.keys)
    # No two keys share a frame, and order is preserved (strictly increasing).
    assert frames == sorted(set(frames))


# ── Shift additive selection ─────────────────────────────────────────────

def test_shift_click_toggles_key_into_selection(view):
    view, m = view
    curve = m.curves[0]
    for k in curve.keys:
        k.selected = (k.frame == 0)
    kp10 = view.data_to_screen(QPointF(10, 30.0))
    view.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress, kp10.x(), kp10.y(),
                             mods=Qt.KeyboardModifier.ShiftModifier))
    assert sum(1 for k in curve.keys if k.selected) == 2
    # Shift-click again removes it.
    view.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress, kp10.x(), kp10.y(),
                             mods=Qt.KeyboardModifier.ShiftModifier))
    assert sum(1 for k in curve.keys if k.selected) == 1


def test_shift_box_adds_to_selection(view):
    """A Shift-drag box adds the boxed keys without clearing others."""
    view, m = view
    curve = m.curves[0]
    # Pre-select frame 0.
    for k in curve.keys:
        k.selected = (k.frame == 0)
    # Box that covers frame 30 only, with Shift held.
    p30 = view.data_to_screen(QPointF(30, 90.0))
    empty = view.data_to_screen(QPointF(35, 110.0))
    view._drag_mode = "box"
    view._box_start = p30
    view._box_end = empty
    view._box_additive = True
    view._finish_box_select()
    # Frame 0 stayed selected AND frame 30 was added.
    sel_frames = sorted(k.frame for k in curve.keys if k.selected)
    assert 0 in sel_frames
    assert 30 in sel_frames
