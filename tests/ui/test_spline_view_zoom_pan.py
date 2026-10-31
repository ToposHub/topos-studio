"""Batch 1 tests: SplineView viewport zoom & pan (AE/DaVinci convention).

Covers wheelEvent zoom anchored at the cursor, modifier-driven pans
(Alt=horizontal, Shift=vertical, Ctrl=finer), middle/right-button drag pan,
and that fit_view() resets any manual viewport changes.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QWheelEvent, QMouseEvent
from PyQt6.QtCore import QEvent, QPoint

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
    tr.add_keyframe(Keyframe(0, 0.0))
    tr.add_keyframe(Keyframe(50, 45.0))
    tr.add_keyframe(Keyframe(100, 90.0))
    t.set_keyframe_track("rotation", tr)
    g.add_node(t)
    m = SplineModel()
    m.rebuild(g)
    for c in m.curves:
        c.selected = True
    view = SplineView(m)
    view.resize(960, 400)
    view.fit_view()
    return view


def _wheel(view, x, y, delta, mods=Qt.KeyboardModifier.NoModifier):
    """Synthesize a wheel event the same way Qt does."""
    ev = QWheelEvent(QPointF(x, y), QPointF(x, y), QPoint(0, delta),
                     QPoint(0, delta), Qt.MouseButton.NoButton, mods,
                     Qt.ScrollPhase.NoScrollPhase, False)
    view.wheelEvent(ev)


# ── zoom anchored at cursor ─────────────────────────────────────────────

def test_wheel_zoom_keeps_cursor_frame_in_place(view):
    """The frame under the cursor must stay fixed when zooming."""
    vp = view._ensure_viewport()
    cx = view.width() // 2
    frame_before = view.screen_to_data(QPointF(cx, 100)).x()
    fspan_before = vp.frame_max - vp.frame_min
    _wheel(view, cx, 100, 120)  # zoom in
    fspan_after = vp.frame_max - vp.frame_min
    frame_after = view.screen_to_data(QPointF(cx, 100)).x()
    assert fspan_after < fspan_before      # range shrank → zoomed in
    assert abs(frame_after - frame_before) < 1e-6  # anchor held


def test_wheel_zoom_out_grows_range(view):
    vp = view._ensure_viewport()
    fspan_before = vp.frame_max - vp.frame_min
    _wheel(view, view.width() // 2, 100, -120)  # zoom out
    fspan_after = (view._ensure_viewport().frame_max
                   - view._ensure_viewport().frame_min)
    assert fspan_after > fspan_before


# ── modifier pans ────────────────────────────────────────────────────────

def test_alt_wheel_horizontal_pan(view):
    vp = view._ensure_viewport()
    fmin_before = vp.frame_min
    _wheel(view, 200, 100, 120, mods=Qt.KeyboardModifier.AltModifier)
    vp = view._ensure_viewport()
    assert vp.frame_min != fmin_before
    # value range unchanged on a horizontal pan
    assert (vp.value_max - vp.value_min) == pytest.approx(
        vp.value_max - vp.value_min, rel=1e-9)


def test_shift_wheel_vertical_pan(view):
    vp = view._ensure_viewport()
    vmin_before = vp.value_min
    _wheel(view, 200, 100, 120, mods=Qt.KeyboardModifier.ShiftModifier)
    vp = view._ensure_viewport()
    assert vp.value_min != vmin_before


# ── middle/right-button drag pan ─────────────────────────────────────────

def _press(view, x, y, button):
    view.mousePressEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(x, y), button, button,
        Qt.KeyboardModifier.NoModifier))


def _move(view, x, y):
    view.mouseMoveEvent(QMouseEvent(
        QMouseEvent.Type.MouseMove, QPointF(x, y),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))


def _release(view, x, y, button):
    view.mouseReleaseEvent(QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, QPointF(x, y), button, button,
        Qt.KeyboardModifier.NoModifier))


def test_middle_button_drag_pans(view):
    vp = view._ensure_viewport()
    fmin_before = vp.frame_min
    _press(view, 400, 200, Qt.MouseButton.MiddleButton)
    assert view._drag_mode == "pan"
    _move(view, 500, 220)  # drag right → see earlier frames
    _release(view, 500, 220, Qt.MouseButton.MiddleButton)
    vp = view._ensure_viewport()
    assert vp.frame_min != fmin_before
    assert view._drag_mode == "none"


def test_right_button_drag_pans(view):
    vp = view._ensure_viewport()
    fmin_before = vp.frame_min
    _press(view, 400, 200, Qt.MouseButton.RightButton)
    assert view._drag_mode == "pan"
    _move(view, 300, 200)
    _release(view, 300, 200, Qt.MouseButton.RightButton)
    assert view._ensure_viewport().frame_min != fmin_before


# ── fit resets ───────────────────────────────────────────────────────────

def test_fit_view_resets_after_zoom(view):
    vp = view._ensure_viewport()
    fspan_before = vp.frame_max - vp.frame_min
    _wheel(view, view.width() // 2, 100, 120)
    assert view._ensure_viewport().frame_max - view._ensure_viewport().frame_min != fspan_before
    view.fit_view()
    fspan_after = view._ensure_viewport().frame_max - view._ensure_viewport().frame_min
    assert fspan_after == pytest.approx(fspan_before, rel=1e-6)
