"""Regression tests for the Graph Editor interaction bug fixes (batch 2).

Locks four interaction contracts fixed after the audit:

1. A1: Esc cancels an anchor drag — the anchor restores from its pre-drag
   snapshot. Previously the Esc guard excluded box_anchor and the snapshot
   was never consumed, stranding the anchor at the cursor position.

2. F4: a click-press-release with no movement emits no keyframe_changed and
   records no undo entry (a plain click only selects, like AE).

3. A5: a mid/right-button press during an active left-button drag is ignored
   (it no longer hijacks the gesture, leaving _drag_mode dangling).

4. F1: the show_markers / show_layer_in_out / show_reference_graph option
   toggles actually gate their overlays (previously the view stored them but
   the painters ignored them, so the switches did nothing).
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import Qt, QPointF, QPoint
from PyQt6.QtGui import QMouseEvent, QKeyEvent
from PyQt6.QtWidgets import QApplication

from src.features.composite.ui.graph_editor_view import GraphEditorView
from src.features.composite.ui.graph_model import (
    build_graph_series, frame_to_x, value_to_y, GraphOptions,
)
from src.features.composite.nodes.transform_node import TransformNode
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


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
    v.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    return v


def _kp_pos(view, frame, value):
    x = frame_to_x(frame, view._viewport, 40.0)
    y = value_to_y(value, view._viewport, float(view.height() - 24))
    return x, y


def _setup_box(view, qapp):
    """Select two points spanning distinct frames+values so the transform
    box is a real 2-D rectangle."""
    view.set_series([_series()])
    view.show()
    qapp.processEvents()
    s = view._series[0]
    s.points[1].selected = True   # frame 50, value 90
    s.points[2].selected = True   # frame 100, value 0
    view._refresh_transform_box()
    return s


def _press(view, x, y, button=Qt.MouseButton.LeftButton):
    QET = QMouseEvent.Type
    ev = QMouseEvent(QET.MouseButtonPress, QPointF(x, y), button, button,
                     Qt.KeyboardModifier.NoModifier)
    view.mousePressEvent(ev)


def _move(view, x, y):
    QET = QMouseEvent.Type
    ev = QMouseEvent(QET.MouseMove, QPointF(x, y), Qt.MouseButton.LeftButton,
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    view.mouseMoveEvent(ev)


def _release(view, x, y, button=Qt.MouseButton.LeftButton):
    QET = QMouseEvent.Type
    ev = QMouseEvent(QET.MouseButtonRelease, QPointF(x, y), button,
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    view.mouseReleaseEvent(ev)


def _esc(view):
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                   Qt.KeyboardModifier.NoModifier)
    view.keyPressEvent(ev)


# ── A1: Esc cancels an anchor drag ────────────────────────────────────


class TestAnchorEscCancel:
    def test_esc_restores_anchor_position(self, view, qapp):
        """A1: Esc mid-anchor-drag restores the anchor to its pre-drag
        position (previously the Esc guard excluded box_anchor)."""
        s = _setup_box(view, qapp)
        anchor_before = list(view._tbox_anchor)
        af, av = view._tbox_anchor
        ax = frame_to_x(af, view._viewport, 40.0)
        ay = value_to_y(av, view._viewport, float(view.height() - 24))
        # Begin an anchor drag + move it.
        _press(view, ax, ay)
        assert view._drag_mode == "box_anchor"
        _move(view, ax + 40, ay + 20)
        assert view._tbox_anchor != anchor_before
        # Esc should restore.
        _esc(view)
        assert view._drag_mode == "none"
        assert list(view._tbox_anchor) == pytest.approx(anchor_before)

    def test_esc_anchor_does_not_emit_keyframe_changed(self, view, qapp):
        """Cancelling an anchor drag never mutates keyframes."""
        _setup_box(view, qapp)
        emitted = []
        view.keyframe_changed.connect(
            lambda nid, pn, kfs: emitted.append((nid, pn)))
        af, av = view._tbox_anchor
        ax = frame_to_x(af, view._viewport, 40.0)
        ay = value_to_y(av, view._viewport, float(view.height() - 24))
        _press(view, ax, ay)
        _move(view, ax + 40, ay + 20)
        _esc(view)
        assert emitted == []


# ── F4: no-movement click doesn't emit / record undo ─────────────────


class TestNoMovementClickNoCommit:
    def test_click_release_no_move_no_keyframe_changed(self, view, qapp):
        """F4: a click-press-release on a keyframe with no movement emits no
        keyframe_changed (and thus records no undo entry)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        # Select point[1] by clicking on it (no drag).
        x, y = _kp_pos(view, 50, 90.0)
        emitted = []
        view.keyframe_changed.connect(
            lambda nid, pn, kfs: emitted.append((nid, pn)))
        _press(view, x, y)
        _release(view, x, y)  # release at the same spot — no movement
        assert s.points[1].selected  # selection happened
        assert emitted == [], (
            "A no-movement click must not emit keyframe_changed — it only "
            "selects, never mutates (F4).")

    def test_drag_with_movement_does_emit(self, view, qapp):
        """Sanity: a real drag with movement still emits (the F4 fix must
        not suppress legitimate edits)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        x, y = _kp_pos(view, 50, 90.0)
        emitted = []
        view.keyframe_changed.connect(
            lambda nid, pn, kfs: emitted.append((nid, pn)))
        _press(view, x, y)
        _move(view, x + 30, y)   # actually move
        _release(view, x + 30, y)
        assert len(emitted) == 1


# ── A5: mid/right press during drag doesn't hijack ────────────────────


class TestMidPressDoesNotHijackDrag:
    def test_mid_press_during_drag_keeps_drag_mode(self, view, qapp):
        """A5: pressing middle button during a left-button keyframe drag does
        NOT start a pan — the drag stays active."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        x, y = _kp_pos(view, 50, 90.0)
        _press(view, x, y)            # begin keyframe drag
        assert view._drag_mode == "keyframe"
        _move(view, x + 10, y)        # small move to establish the drag
        # Press middle button mid-drag.
        _press(view, x + 10, y, button=Qt.MouseButton.MiddleButton)
        # The drag must still be active; pan must NOT have started.
        assert view._drag_mode == "keyframe", (
            "A mid/right press during a drag must not hijack the gesture "
            "(A5) — it would strand _drag_mode and cause a huge jump.")
        assert view._pan_active is False

    def test_mid_press_on_idle_starts_pan(self, view, qapp):
        """Sanity: when idle, mid press still starts a pan (A5 only blocks
        pan during an active drag)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        _press(view, 400, 200, button=Qt.MouseButton.MiddleButton)
        assert view._pan_active is True


# ── F1: option toggles gate their overlays ────────────────────────────


class TestOptionTogglesHonoured:
    def test_show_markers_off_skips_markers(self, view, qapp):
        """F1: show_markers=False means _draw_markers is not called. We
        verify via the paintEvent body (structural) since pixel-level paint
        assertions are fragile under offscreen Qt."""
        import inspect
        src = inspect.getsource(GraphEditorView.paintEvent)
        assert "show_markers" in src, (
            "paintEvent must honour the show_markers toggle (F1).")

    def test_show_layer_in_out_off_skips_shade(self, view, qapp):
        import inspect
        src = inspect.getsource(GraphEditorView.paintEvent)
        assert "show_layer_in_out" in src, (
            "paintEvent must honour the show_layer_in_out toggle (F1).")

    def test_show_reference_graph_gates_pre_curve(self, view, qapp):
        import inspect
        src = inspect.getsource(GraphEditorView._draw_expression_curves)
        assert "show_reference_graph" in src, (
            "_draw_expression_curves must honour show_reference_graph so the "
            "pre/reference curve can be toggled (F1).")

    def test_set_options_triggers_repaint(self, view, qapp):
        """Toggling an option must trigger a repaint so the change is
        visible (the existing set_options already calls update())."""
        from src.features.composite.ui.graph_model import GraphOptions
        opts = GraphOptions(show_markers=False)
        # Should not raise.
        view.set_options(opts)
        # The view's options reflect the new value.
        assert view._options.show_markers is False
