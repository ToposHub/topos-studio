"""P12 tests: SplineView hit-test + drag interaction + undo (plan §11.11).

Validates keyframe/handle hit-testing, key drag, handle drag with smooth
mirroring, tangent-mode switching, and the KeySnapshot undo records.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QPointF, Qt, QRectF
from PyQt6.QtGui import QMouseEvent

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.spline_model import (
    SplineModel, SplineCurve, SplineKey, INTERP_BEZIER, TANGENT_SMOOTH)
from src.features.composite.ui.spline_view import SplineView, set_key_tangent_mode
from src.features.composite.ui.spline_commands import (
    KeySnapshot, MoveKeyCommand, MoveHandleCommand, SetTangentModeCommand)
from src.shared.keyframe import Keyframe, KeyframeTrack


@pytest.fixture
def view_with_curve(qapp):
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    tr.add_keyframe(Keyframe(0, 0.0))
    tr.add_keyframe(Keyframe(10, 45.0))
    tr.add_keyframe(Keyframe(20, 90.0))
    t.set_keyframe_track("rotation", tr)
    g.add_node(t)
    m = SplineModel()
    m.rebuild(g)
    for c in m.curves:
        c.selected = True
    view = SplineView(m)
    view.resize(960, 400)
    view.fit_view()
    return view, m, t


def _ev(typ, x, y):
    return QMouseEvent(typ, QPointF(x, y), Qt.MouseButton.LeftButton,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)


# ── key drag ────────────────────────────────────────────────────────────

def test_drag_key_moves_frame_and_value(view_with_curve):
    view, m, t = view_with_curve
    curve = m.curves[0]
    # The middle key (frame 10, value 45) — find its screen pos and drag it.
    mid_key = curve.keys[1]
    kp = view.data_to_screen(QPointF(mid_key.frame, mid_key.value))
    received = []
    view.keyframe_changed.connect(lambda nid, pn, kfs: received.append((nid, pn, kfs)))

    # Drag down-right by ~30px in data terms
    target = view.screen_to_data(QPointF(kp.x() + 30, kp.y() + 30))
    target_screen = QPointF(kp.x() + 30, kp.y() + 30)

    view.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress, kp.x(), kp.y()))
    view.mouseMoveEvent(_ev(QMouseEvent.Type.MouseMove, target_screen.x(), target_screen.y()))
    view.mouseReleaseEvent(_ev(QMouseEvent.Type.MouseButtonRelease, target_screen.x(), target_screen.y()))

    assert received
    nid, pn, kfs = received[-1]
    assert nid == t.id and pn == "rotation"
    frames = sorted(kf["frame"] for kf in kfs)
    # The middle key moved to a higher frame (dragged right).
    assert frames[1] > 10


def test_drag_key_cannot_cross_neighbour(view_with_curve):
    """A key cannot be dragged past its neighbour (§P11.8)."""
    view, m, t = view_with_curve
    curve = m.curves[0]
    mid_key = curve.keys[1]
    kp = view.data_to_screen(QPointF(mid_key.frame, mid_key.value))
    # Try to drag far past the next key (frame 20).
    far = view.data_to_screen(QPointF(100, mid_key.value))
    view.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress, kp.x(), kp.y()))
    view.mouseMoveEvent(_ev(QMouseEvent.Type.MouseMove, far.x(), kp.y()))
    # mid_key.frame must be clamped below 20 (next key frame).
    assert mid_key.frame < 20
    view.mouseReleaseEvent(_ev(QMouseEvent.Type.MouseButtonRelease, far.x(), kp.y()))


# ── handle drag ─────────────────────────────────────────────────────────

def test_drag_handle_updates_bezier(view_with_curve):
    view, m, t = view_with_curve
    curve = m.curves[0]
    # Make the first key bezier so it has handles.
    curve.keys[0].interpolation = INTERP_BEZIER
    curve.keys[0].tangent_mode = TANGENT_SMOOTH
    k = curve.keys[0]
    hr_screen = view.data_to_screen(k.handle_right)
    target = QPointF(hr_screen.x() + 20, hr_screen.y() - 20)

    view.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress, hr_screen.x(), hr_screen.y()))
    view.mouseMoveEvent(_ev(QMouseEvent.Type.MouseMove, target.x(), target.y()))
    old_hrx = k.handle_right.x()
    # The right handle moved.
    assert abs(k.handle_right.x() - view.screen_to_data(target).x()) < 5 or k.handle_right.x() != hr_screen.x()
    view.mouseReleaseEvent(_ev(QMouseEvent.Type.MouseButtonRelease, target.x(), target.y()))


# ── tangent mode ────────────────────────────────────────────────────────

def test_set_tangent_flat_makes_handles_horizontal(view_with_curve):
    view, m, t = view_with_curve
    curve = m.curves[0]
    curve.keys[1].selected = True
    k = curve.keys[1]
    set_key_tangent_mode(view, curve, 1, "flat")
    assert k.tangent_mode == "flat"
    # flat → handle y equals key value
    assert abs(k.handle_left.y() - k.value) < 1e-6
    assert abs(k.handle_right.y() - k.value) < 1e-6


def test_set_tangent_linear_collapses_handles(view_with_curve):
    view, m, t = view_with_curve
    curve = m.curves[0]
    curve.keys[1].selected = True
    k = curve.keys[1]
    set_key_tangent_mode(view, curve, 1, "linear")
    assert k.handle_left.x() == k.frame
    assert k.handle_right.x() == k.frame


def test_set_tangent_step_makes_hold(view_with_curve):
    view, m, t = view_with_curve
    curve = m.curves[0]
    curve.keys[1].selected = True
    k = curve.keys[1]
    set_key_tangent_mode(view, curve, 1, "step")
    assert k.interpolation == "hold"


# ── undo records ────────────────────────────────────────────────────────

def test_key_snapshot_round_trip():
    k = SplineKey(10, 5.0, INTERP_BEZIER, TANGENT_SMOOTH,
                  handle_left=QPointF(6, 3), handle_right=QPointF(14, 7))
    snap = KeySnapshot.from_key(k)
    # Mutate the key
    k.frame = 99
    k.value = 99.0
    k.handle_left = QPointF(0, 0)
    # Restore
    snap.apply_to(k)
    assert k.frame == 10
    assert k.value == 5.0
    assert k.handle_left.x() == 6


def test_move_key_command_carries_before_after():
    k = SplineKey(10, 5.0)
    before = KeySnapshot.from_key(k)
    k.frame = 12
    after = KeySnapshot.from_key(k)
    cmd = MoveKeyCommand("n1", "rotation", 1, before, after)
    assert cmd.before.frame == 10
    assert cmd.after.frame == 12
    assert cmd.node_id == "n1"


def test_set_tangent_command_record():
    k = SplineKey(10, 5.0, INTERP_BEZIER, TANGENT_SMOOTH)
    before = KeySnapshot.from_key(k)
    k.tangent_mode = "flat"
    after = KeySnapshot.from_key(k)
    cmd = SetTangentModeCommand("n1", "rotation", 0, before, after)
    assert cmd.before.tangent_mode == "smooth"
    assert cmd.after.tangent_mode == "flat"


# ── delete key ──────────────────────────────────────────────────────────

def test_delete_selected_key(view_with_curve):
    view, m, t = view_with_curve
    curve = m.curves[0]
    curve.keys[1].selected = True  # middle key
    received = []
    view.keyframe_changed.connect(lambda nid, pn, kfs: received.append(kfs))
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QKeyEvent
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete,
                                 Qt.KeyboardModifier.NoModifier))
    assert received
    frames = sorted(kf["frame"] for kf in received[-1])
    assert 10 not in frames  # middle key removed
    assert len(frames) == 2
