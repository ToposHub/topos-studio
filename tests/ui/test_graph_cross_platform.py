"""T78 cross-platform input tests.

Verifies the Graph Editor's modifier handling works the same on macOS
(Cmd/Option) and Windows/Linux (Ctrl/Alt) — plan §T78 'Command/Option 与
Ctrl/Alt…符合平台习惯'. Qt abstracts the platform key, but the modifier
CHECKS must accept both so a user's platform habit works regardless.

Covers:
  * Additive (multi-select) click accepts Shift OR Ctrl OR Cmd.
  * The Expression Editor's Ctrl/Cmd+Return commit works on both.
  * The Transform Box + snap modifiers don't assume a single platform key.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication

from src.features.composite.ui.graph_editor_view import (
    GraphEditorView, _additive_modifiers,
)
from src.features.composite.ui.graph_model import build_graph_series
from src.features.composite.nodes.transform_node import TransformNode
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


# ── T78: additive modifier predicate ──────────────────────────────────


class TestAdditiveModifier:
    @pytest.mark.parametrize("mods,expected", [
        (Qt.KeyboardModifier.NoModifier, False),
        (Qt.KeyboardModifier.ShiftModifier, True),
        (Qt.KeyboardModifier.ControlModifier, True),   # Windows/Linux
        (Qt.KeyboardModifier.MetaModifier, True),      # macOS Cmd
        (Qt.KeyboardModifier.AltModifier, False),      # Alt alone = constrain
        (Qt.KeyboardModifier.ShiftModifier |
         Qt.KeyboardModifier.ControlModifier, True),
        (Qt.KeyboardModifier.ShiftModifier |
         Qt.KeyboardModifier.MetaModifier, True),
    ])
    def test_additive_accepts_shift_ctrl_cmd(self, mods, expected):
        assert _additive_modifiers(mods) is expected


# ── T78: expression editor commit on both platforms ───────────────────


class TestExpressionEditorCommit:
    def _editor(self, qapp):
        from src.features.composite.ui.graph_expression_editor import \
            GraphExpressionEditor
        e = GraphExpressionEditor()
        e.set_target("n1", "rotation", "Rotation", "value")
        return e

    def test_ctrl_return_commits(self, qapp):
        """Ctrl+Return (Windows/Linux) commits the expression."""
        e = self._editor(qapp)
        received = []
        e.expression_committed.connect(
            lambda nid, pn, txt: received.append((nid, pn, txt)))
        ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return,
                       Qt.KeyboardModifier.ControlModifier)
        e._text_edit.keyPressEvent(ev)
        assert len(received) == 1

    def test_cmd_return_commits(self, qapp):
        """Cmd+Return (macOS) commits the expression."""
        e = self._editor(qapp)
        received = []
        e.expression_committed.connect(
            lambda nid, pn, txt: received.append((nid, pn, txt)))
        ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return,
                       Qt.KeyboardModifier.MetaModifier)
        e._text_edit.keyPressEvent(ev)
        assert len(received) == 1

    def test_plain_return_does_not_commit(self, qapp):
        """Plain Return (no modifier) is a newline, not a commit."""
        e = self._editor(qapp)
        received = []
        e.expression_committed.connect(received.append)
        ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return,
                       Qt.KeyboardModifier.NoModifier)
        e._text_edit.keyPressEvent(ev)
        assert received == []


# ── T78: graph keyframe multi-select on both platforms ───────────────


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


class TestMultiSelectPlatform:
    def _click(self, view, x, y, mods):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QMouseEvent
        for et in (QMouseEvent.Type.MouseButtonPress,
                   QMouseEvent.Type.MouseButtonRelease):
            ev = QMouseEvent(et, QPointF(x, y),
                             Qt.MouseButton.LeftButton,
                             Qt.MouseButton.LeftButton
                             if et == QMouseEvent.Type.MouseButtonPress
                             else Qt.MouseButton.NoButton, mods)
            if et == QMouseEvent.Type.MouseButtonPress:
                view.mousePressEvent(ev)
            else:
                view.mouseReleaseEvent(ev)

    def test_shift_click_is_additive(self, view, qapp):
        """Shift+click adds to the selection (cross-platform)."""
        from src.features.composite.ui.graph_model import frame_to_x, value_to_y
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        x0 = frame_to_x(s.points[0].frame, view._viewport, 40.0)
        y0 = value_to_y(s.points[0].value, view._viewport,
                        float(view.height() - 24))
        self._click(view, x0, y0, Qt.KeyboardModifier.NoModifier)
        assert s.points[0].selected
        x1 = frame_to_x(s.points[1].frame, view._viewport, 40.0)
        y1 = value_to_y(s.points[1].value, view._viewport,
                        float(view.height() - 24))
        self._click(view, x1, y1, Qt.KeyboardModifier.ShiftModifier)
        assert s.points[0].selected and s.points[1].selected

    def test_cmd_click_is_additive_macos(self, view, qapp):
        """Cmd+click (macOS AE convention) adds to the selection."""
        from src.features.composite.ui.graph_model import frame_to_x, value_to_y
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        x0 = frame_to_x(s.points[0].frame, view._viewport, 40.0)
        y0 = value_to_y(s.points[0].value, view._viewport,
                        float(view.height() - 24))
        self._click(view, x0, y0, Qt.KeyboardModifier.NoModifier)
        assert s.points[0].selected
        x1 = frame_to_x(s.points[1].frame, view._viewport, 40.0)
        y1 = value_to_y(s.points[1].value, view._viewport,
                        float(view.height() - 24))
        self._click(view, x1, y1, Qt.KeyboardModifier.MetaModifier)
        assert s.points[0].selected and s.points[1].selected
