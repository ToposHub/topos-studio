"""P5 tests: Keyframes panel interaction (drag / add / delete keyframes).

Uses simulated mouse events to verify the timeline view's hit-testing,
keyframe drag, double-click-add, and delete-key behavior, plus the
end-to-end signal flow into the NodeGraph via CompositePage slots.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframes_model import KeyframesModel, FrameMapper
from src.features.composite.ui.keyframes_timeline_view import KeyframesTimelineView
from src.shared.keyframe import Keyframe, KeyframeTrack


def _rotation_row_y(view, t):
    """Find the widget y of the rotation param row's vertical center."""
    rows = view._model.visible_rows()
    for i, row in enumerate(rows):
        if row.row_type == "param" and row.param_name == "rotation":
            # row center y = _RULER_H + i*_ROW_H - scroll_y + _ROW_H/2
            return 30 + i * 24 - view._scroll_y + 12
    raise AssertionError("rotation param row not found")


@pytest.fixture
def timeline_with_animation(qapp):
    """A timeline view bound to a model with one animated transform param."""
    g = NodeGraph()
    t = TransformNode()
    track = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(20, 90.0))
    t.set_keyframe_track("rotation", track)
    g.add_node(t)

    model = KeyframesModel()
    model.rebuild(g)
    fm = FrameMapper(pixels_per_frame=10.0)  # 10 px per frame → easy hit-testing
    view = KeyframesTimelineView(model, fm)
    view.resize(600, 400)
    return view, model, t


# ── hit testing ─────────────────────────────────────────────────────────

def test_keyframe_hit_test_finds_diamond(timeline_with_animation):
    view, model, t = timeline_with_animation
    row_id = f"param:{t.id}:rotation"
    y = _rotation_row_y(view, t)
    # frame 0 at x=0, frame 20 at x=200. Click near x=200.
    hit = view._hit_keyframe(200.0, y)
    assert hit is not None
    assert hit[0] == row_id
    assert hit[1] == 20


def test_keyframe_hit_test_misses_far_click(timeline_with_animation):
    view, model, t = timeline_with_animation
    y = _rotation_row_y(view, t)
    # x=150 is between frames 0 and 20, no keyframe there.
    assert view._hit_keyframe(150.0, y) is None


# ── double-click adds a keyframe ─────────────────────────────────────────

def test_double_click_adds_keyframe(timeline_with_animation):
    view, model, t = timeline_with_animation
    y = _rotation_row_y(view, t)
    received = []
    view.track_changed.connect(
        lambda nid, pn, kfs: received.append((nid, pn, kfs)))

    # Double-click at frame 10 (x=100) on the param row.
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonDblClick,
                     QPointF(100.0, y), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.mouseDoubleClickEvent(ev)

    assert received
    nid, pn, kfs = received[0]
    assert nid == t.id and pn == "rotation"
    frames = sorted(kf["frame"] for kf in kfs)
    assert 10 in frames
    assert frames == [0, 10, 20]


# ── drag keyframe ───────────────────────────────────────────────────────

def test_drag_keyframe_moves_frame(timeline_with_animation):
    view, model, t = timeline_with_animation
    y = _rotation_row_y(view, t)
    received = []
    view.track_changed.connect(
        lambda nid, pn, kfs: received.append((nid, pn, kfs)))

    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    def _ev(typ, x, yv):
        return QMouseEvent(typ, QPointF(x, yv), Qt.MouseButton.LeftButton,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)

    view.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress, 200.0, y))
    view.mouseMoveEvent(_ev(QMouseEvent.Type.MouseMove, 250.0, y))
    view.mouseReleaseEvent(_ev(QMouseEvent.Type.MouseButtonRelease, 250.0, y))

    assert received
    nid, pn, kfs = received[-1]
    frames = sorted(kf["frame"] for kf in kfs)
    assert 25 in frames
    assert 20 not in frames  # old frame 20 is gone


# ── delete keyframe ─────────────────────────────────────────────────────

def test_delete_key_removes_selected(timeline_with_animation):
    view, model, t = timeline_with_animation
    # Select the frame-20 keyframe first.
    view._select_keyframe(f"param:{t.id}:rotation", 20)

    received = []
    view.track_changed.connect(
        lambda nid, pn, kfs: received.append((nid, pn, kfs)))

    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QKeyEvent
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete,
                   Qt.KeyboardModifier.NoModifier)
    view.keyPressEvent(ev)

    assert received
    nid, pn, kfs = received[-1]
    frames = [kf["frame"] for kf in kfs]
    assert 20 not in frames
    assert 0 in frames  # the other keyframe survived


# ── range bar drag ──────────────────────────────────────────────────────

def test_range_bar_edge_drag_emits_range(qapp):
    g = NodeGraph()
    t = TransformNode()
    t.time_range = {"comp_start": 0, "comp_end": 50}
    g.add_node(t)
    model = KeyframesModel()
    model.rebuild(g)
    fm = FrameMapper(pixels_per_frame=10.0)
    view = KeyframesTimelineView(model, fm)
    view.resize(800, 400)

    received = []
    view.range_changed.connect(
        lambda nid, tr: received.append((nid, tr)))

    # Node row is row 0 → y = 30 + 0*24 + 12 = 42
    # range bar: start frame 0 → x=0, end frame 50 → x=500.
    # Right edge at x=500; drag to x=600 → frame 60.
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    def _ev(typ, x, y):
        return QMouseEvent(typ, QPointF(x, y), Qt.MouseButton.LeftButton,
                           Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)

    view.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress, 500.0, 42.0))
    view.mouseMoveEvent(_ev(QMouseEvent.Type.MouseMove, 600.0, 42.0))
    view.mouseReleaseEvent(_ev(QMouseEvent.Type.MouseButtonRelease, 600.0, 42.0))

    assert received
    nid, tr = received[-1]
    assert nid == t.id
    assert tr["comp_end"] == 60


# ── playhead drag ───────────────────────────────────────────────────────

def test_playhead_drag_emits_frame(timeline_with_animation):
    view, model, t = timeline_with_animation
    received = []
    view.current_frame_changed.connect(received.append)

    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    # Click on ruler at x=150 → frame 15
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress,
                     QPointF(150.0, 10.0), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.mousePressEvent(ev)
    assert 15 in received
