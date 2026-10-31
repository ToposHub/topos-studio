"""T123 Graph-mode undo transaction tests.

Headless tests locking two contracts:

1. Esc cancels an in-flight drag — the pre-drag snapshot is restored
   exactly, no keyframe_changed fires (the cancelled drag is discarded:
   no node write, no undo record). Covers keyframe drag, handle drag,
   and Transform Box move/scale.

2. drag_about_to_commit / drag_committed bracket a drag's commit burst
   so the host can wrap a multi-series drag in ONE undo group. The
   signals carry the affected-series count.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication

from src.features.composite.ui.graph_editor_view import GraphEditorView
from src.features.composite.ui.graph_model import build_graph_series
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
    v.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # ensure key events arrive
    return v


def _kp_pos(view, frame, value):
    from src.features.composite.ui.graph_model import frame_to_x, value_to_y
    x = frame_to_x(frame, view._viewport, 40.0)
    y = value_to_y(value, view._viewport, float(view.height() - 24))
    return x, y


def _press_move(view, x1, y1):
    """Press + start a drag (move) without releasing — leaves the drag
    active so Esc can cancel it."""
    from PyQt6.QtGui import QMouseEvent
    QET = QMouseEvent.Type
    ev = QMouseEvent(QET.MouseButtonPress, QPointF(x1, y1),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    view.mousePressEvent(ev)
    ev2 = QMouseEvent(QET.MouseMove, QPointF(x1 + 30, y1),
                      Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                      Qt.KeyboardModifier.NoModifier)
    view.mouseMoveEvent(ev2)


def _esc(view):
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape,
                   Qt.KeyboardModifier.NoModifier)
    view.keyPressEvent(ev)


# ── T123: Esc cancels keyframe drag ───────────────────────────────────


class TestEscCancelKeyframeDrag:
    def test_esc_restores_keyframe_position(self, view, qapp):
        """Esc mid-drag restores the keyframe to its pre-drag frame/value."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        kp = s.points[1]  # frame 50, value 90
        f0, v0 = kp.frame, kp.value
        kp.selected = True
        x, y = _kp_pos(view, kp.frame, kp.value)
        _press_move(view, x, y)
        assert view._drag_mode == "keyframe"
        # The move already mutated the point; Esc must restore it.
        _esc(view)
        assert kp.frame == pytest.approx(f0)
        assert kp.value == pytest.approx(v0)
        assert view._drag_mode == "none"

    def test_esc_does_not_emit_keyframe_changed(self, view, qapp):
        """A cancelled drag must NOT emit keyframe_changed (no node write,
        no undo record)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        received = []
        view.keyframe_changed.connect(received.append)
        s = view._series[0]
        kp = s.points[1]
        kp.selected = True
        x, y = _kp_pos(view, kp.frame, kp.value)
        _press_move(view, x, y)
        _esc(view)
        assert received == []


# ── T123: Esc cancels handle drag ─────────────────────────────────────


class TestEscCancelHandleDrag:
    def test_esc_restores_handles(self, view, qapp):
        """Esc mid-handle-drag restores both handles to their pre-drag state."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        kp = s.points[1]
        # Give it bezier handles so a handle drag is possible.
        kp.interpolation_in = "bezier"
        kp.interpolation_out = "bezier"
        kp.handle_left = (5.0, 10.0)
        kp.handle_right = (-5.0, -10.0)
        kp.selected = True
        hl_before = tuple(kp.handle_left)
        hr_before = tuple(kp.handle_right)
        # Press on the right handle.
        from src.features.composite.ui.graph_model import frame_to_x, value_to_y
        hf = kp.frame + kp.handle_right[0]
        hv = kp.value + kp.handle_right[1]
        hx = frame_to_x(hf, view._viewport, 40.0)
        hy = value_to_y(hv, view._viewport, float(view.height() - 24))
        _press_move(view, hx, hy)
        assert view._drag_mode == "handle"
        _esc(view)
        assert tuple(kp.handle_left) == hl_before
        assert tuple(kp.handle_right) == hr_before
        assert view._drag_mode == "none"


# ── T123: Esc cancels Transform Box move ──────────────────────────────


class TestEscCancelBoxMove:
    def test_esc_restores_box_move(self, view, qapp):
        """Esc mid-box-move restores every selected point."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        s.points[1].selected = True   # frame 50, value 90
        s.points[2].selected = True   # frame 100, value 0
        view._refresh_transform_box()
        f1_before = s.points[1].frame
        f2_before = s.points[2].frame
        core = view._box_core_rect_px()
        ix = core.left() + core.width() * 0.3
        iy = core.top() + core.height() * 0.3
        _press_move(view, ix, iy)
        assert view._drag_mode == "box_move"
        _esc(view)
        assert s.points[1].frame == pytest.approx(f1_before)
        assert s.points[2].frame == pytest.approx(f2_before)
        assert view._drag_mode == "none"


# ── T123: transaction signals ─────────────────────────────────────────


class TestTransactionSignals:
    def _full_drag(self, view, x1, y1, x2, y2):
        """Press → move → release (a complete drag that commits)."""
        from PyQt6.QtGui import QMouseEvent
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
                             Qt.KeyboardModifier.NoModifier)
            if etype == QET.MouseButtonPress:
                view.mousePressEvent(ev)
            elif etype == QET.MouseMove:
                view.mouseMoveEvent(ev)
            else:
                view.mouseReleaseEvent(ev)

    def test_single_series_drag_emits_count_1(self, view, qapp):
        """A single-series keyframe drag emits drag_about_to_commit(1) +
        drag_committed(1)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        kp = s.points[1]
        kp.selected = True
        before = []
        after = []
        view.drag_about_to_commit.connect(before.append)
        view.drag_committed.connect(after.append)
        x, y = _kp_pos(view, kp.frame, kp.value)
        self._full_drag(view, x, y, x + 30, y)
        assert before == [1]
        assert after == [1]

    def test_multi_series_box_drag_emits_count_2(self, view, qapp):
        """A Transform Box drag touching 2 series emits count=2 (the host
        uses this to decide whether to open an undo group)."""
        view.set_series([_series(param="rotation"),
                         _series(param="scale")])
        view._series[1].row_id = "param:n:scale"
        view.show()
        qapp.processEvents()
        s0, s1 = view._series
        s0.points[1].selected = True   # frame 50, value 90
        s1.points[2].selected = True   # frame 100, value 0
        view._refresh_transform_box()
        before = []
        after = []
        view.drag_about_to_commit.connect(before.append)
        view.drag_committed.connect(after.append)
        core = view._box_core_rect_px()
        ix = core.left() + core.width() * 0.3
        iy = core.top() + core.height() * 0.3
        self._full_drag(view, ix, iy, ix + 30, iy)
        assert before == [2]
        assert after == [2]

    def test_no_drag_no_transaction_signal(self, view, qapp):
        """A plain click (no drag commit) doesn't fire the transaction
        signals."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        before = []
        after = []
        view.drag_about_to_commit.connect(before.append)
        view.drag_committed.connect(after.append)
        # Click on empty area (no drag, no commit).
        from PyQt6.QtGui import QMouseEvent
        ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(5, 5),
                         Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier)
        view.mousePressEvent(ev)
        ev2 = QMouseEvent(QMouseEvent.Type.MouseButtonRelease, QPointF(5, 5),
                          Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier)
        view.mouseReleaseEvent(ev2)
        assert before == []
        assert after == []


# ── T123: Esc with no active drag is a no-op ─────────────────────────


class TestEscNoOp:
    def test_esc_with_no_drag_passes_through(self, view, qapp):
        """Esc with no active drag doesn't crash (the event propagates)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        assert view._drag_mode == "none"
        _esc(view)  # should not raise
        assert view._drag_mode == "none"
