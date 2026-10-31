"""P9 tests: keyframes panel polish — locking, keyboard nav, node selection."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframes_model import KeyframesModel, FrameMapper
from src.features.composite.ui.keyframes_timeline_view import KeyframesTimelineView
from src.features.composite.ui.keyframes_tree_view import KeyframesTreeView
from src.shared.keyframe import Keyframe, KeyframeTrack


def _build(qapp):
    g = NodeGraph()
    t = TransformNode()
    track = KeyframeTrack(param_name="rotation")
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(20, 90.0))
    t.set_keyframe_track("rotation", track)
    g.add_node(t)
    model = KeyframesModel()
    model.rebuild(g)
    return g, t, model


# ── P9: locked rows are not pickable ─────────────────────────────────────

def test_locked_keyframe_not_pickable(qapp):
    g, t, model = _build(qapp)
    fm = FrameMapper(pixels_per_frame=10.0)
    view = KeyframesTimelineView(model, fm)
    view.resize(600, 400)
    # Find the rotation param row and lock it.
    rows = model.visible_rows()
    rot_row = next(r for r in rows if r.row_type == "param"
                   and r.param_name == "rotation")
    rot_row.locked = True
    y = 30 + rows.index(rot_row) * 24 + 12
    # A keyframe at frame 20 (x=200) is now unpickable.
    assert view._hit_keyframe(200.0, y) is None


def test_locked_range_bar_not_pickable(qapp):
    g = NodeGraph()
    t = TransformNode()
    t.time_range = {"comp_start": 0, "comp_end": 50}
    g.add_node(t)
    model = KeyframesModel()
    model.rebuild(g)
    rows = model.visible_rows()
    rows[0].locked = True  # lock the node row
    fm = FrameMapper(pixels_per_frame=10.0)
    view = KeyframesTimelineView(model, fm)
    view.resize(800, 400)
    # range bar body at x=250, y=42 should be unpickable.
    assert view._hit_range_bar(250.0, 42.0) is None


# ── P9: keyboard navigation ─────────────────────────────────────────────

def test_left_right_arrow_steps_frame(qapp):
    g, t, model = _build(qapp)
    fm = FrameMapper(pixels_per_frame=10.0)
    view = KeyframesTimelineView(model, fm)
    view.resize(600, 400)
    view.set_current_frame(10)

    received = []
    view.current_frame_changed.connect(received.append)

    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right,
                                 Qt.KeyboardModifier.NoModifier))
    assert 11 in received
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Left,
                                 Qt.KeyboardModifier.NoModifier))
    # 11 → 10
    assert 10 in received


def test_shift_arrow_steps_10_frames(qapp):
    g, t, model = _build(qapp)
    fm = FrameMapper(pixels_per_frame=10.0)
    view = KeyframesTimelineView(model, fm)
    view.resize(600, 400)
    view.set_current_frame(5)
    received = []
    view.current_frame_changed.connect(received.append)
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Right,
                                 Qt.KeyboardModifier.ShiftModifier))
    assert 15 in received


def test_home_end_jump_to_keyframe_boundary(qapp):
    g, t, model = _build(qapp)
    fm = FrameMapper(pixels_per_frame=10.0)
    view = KeyframesTimelineView(model, fm)
    view.resize(600, 400)
    view.set_current_frame(10)
    received = []
    view.current_frame_changed.connect(received.append)
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Home,
                                 Qt.KeyboardModifier.NoModifier))
    assert 0 in received  # first keyframe
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_End,
                                 Qt.KeyboardModifier.NoModifier))
    assert 20 in received  # last keyframe


# ── P9: tree node selection ─────────────────────────────────────────────

def test_tree_click_selects_node(qapp):
    g, t, model = _build(qapp)
    tree = KeyframesTreeView(model)
    tree.resize(232, 400)
    received = []
    tree.node_selected.connect(received.append)
    # Click on the node row (row 0) label area (x > lock+expand cols).
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress,
                     QPointF(80.0, 12.0), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    tree.mousePressEvent(ev)
    assert t.id in received
    rows = model.visible_rows()
    assert rows[0].selected is True


def test_tree_click_toggles_lock(qapp):
    g, t, model = _build(qapp)
    tree = KeyframesTreeView(model)
    tree.resize(232, 400)
    received = []
    tree.lock_changed.connect(lambda nid, lk: received.append((nid, lk)))
    # T28: the AE column order is Eye/Solo/Lock/…. With a 232 px tree the
    # column resolver gives eye/solo/lock each ~22 px, so the lock column
    # sits around x=44..66. Click its centre.
    geom = tree._column_geometry()
    lock_x0, lock_x1 = geom["lock"]
    lock_x = (lock_x0 + lock_x1) / 2
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress,
                     QPointF(float(lock_x), 12.0), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    tree.mousePressEvent(ev)
    assert received
    assert received[0][0] == t.id
    assert received[0][1] is True


def test_tree_click_toggles_eye(qapp):
    """T28: clicking the eye column (now first, was lock pre-rewrite) emits
    eye_changed instead of lock_changed."""
    g, t, model = _build(qapp)
    tree = KeyframesTreeView(model)
    tree.resize(232, 400)
    received = []
    tree.eye_changed.connect(lambda nid, vis: received.append((nid, vis)))
    # Eye column is the first column (x=5 lands inside it).
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress,
                     QPointF(5.0, 12.0), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    tree.mousePressEvent(ev)
    assert received
    assert received[0][0] == t.id
    # Eye defaults to visible (True), so first click turns it off.
    assert received[0][1] is False


# ── P10B: performance (F-12) ────────────────────────────────────────────

def test_nearest_keyframe_lookup_scales(qapp):
    """_nearest_keyframe (prev/next nav) handles 1000 keyframes quickly."""
    import time as _time
    from src.features.composite.ui.keyframes_panel import KeyframesPanel
    g = NodeGraph()
    for _ in range(10):
        t = TransformNode()
        track = KeyframeTrack(param_name="rotation")
        for i in range(100):
            track.add_keyframe(Keyframe(i * 2, float(i)))
        t.set_keyframe_track("rotation", track)
        g.add_node(t)
    panel = KeyframesPanel()
    panel.set_node_graph(g)
    start = _time.monotonic()
    nxt = panel._nearest_keyframe(50, +1)
    elapsed = _time.monotonic() - start
    assert elapsed < 0.1
    assert nxt is not None and nxt > 50

