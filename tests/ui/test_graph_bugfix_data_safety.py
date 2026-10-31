"""Regression tests for the Graph Editor data-safety bug fixes (batch 1).

Locks three contracts that were violated by hidden bugs found in the audit:

1. Bug-2: a multi-series drag commit always fires ``drag_committed`` to close
   the undo group, even when a downstream slot raises mid-burst. Without this
   the host's undo stack stays permanently locked in begin_group mode.

2. Bug-3: an implicit focus-out from the expression editor does NOT re-commit
   when the text is unchanged (so switching selection can't re-mutate the old
   param / strip its keyframes via A-15). An explicit Ctrl/Cmd+Return still
   commits regardless.

3. Bug-1 wiring: the Keyframes panel forwards expression edits via
   ``graph_expression_changed`` rather than writing the node directly, so the
   page can own the undo record. (The full page-level undo path is exercised
   via the panel→page signal contract here.)
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import Qt, QPointF, QEvent
from PyQt6.QtGui import QFocusEvent, QKeyEvent
from PyQt6.QtWidgets import QApplication

from src.features.composite.ui.graph_editor_view import GraphEditorView
from src.features.composite.ui.graph_model import build_graph_series
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


# ── Bug-2: drag_committed always fires (even on downstream error) ────


class TestDragCommittedAlwaysFires:
    """Bug-2: a raising slot in the keyframe_changed chain must not swallow
    the matching drag_committed — the host relies on it to end_group()."""

    def test_drag_committed_fires_on_clean_commit(self, view, qapp):
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        s.points[0].selected = True
        s.points[2].selected = True
        view._refresh_transform_box()
        about = []
        committed = []
        view.drag_about_to_commit.connect(lambda c: about.append(c))
        view.drag_committed.connect(lambda c: committed.append(c))
        # Drive a box-move drag directly through the API.
        view._begin_box_move_drag(QPointF(400, 200))
        view._do_box_move_drag(QPointF(420, 200))
        view._commit_drag()
        assert len(about) == 1
        assert len(committed) == 1

    def test_drag_committed_fires_even_when_slot_raises(self, view, qapp):
        """Bug-2: a raising slot in the keyframe_changed chain must not
        swallow the matching drag_committed.

        Driving a real exception through a Qt signal/slot can crash the Qt
        runtime under offscreen, so we verify the contract structurally:
        ``_commit_drag`` must wrap the emit burst in try/finally so the
        matching ``drag_committed`` is guaranteed. (The pairing on the happy
        path is covered by the test above.)"""
        import inspect
        src = inspect.getsource(GraphEditorView._commit_drag)
        # The emit burst must be inside a try: ...
        assert "try:" in src, (
            "_commit_drag must wrap the keyframe_changed emit burst in try.")
        # ... and drag_committed must be in the finally: clause.
        finally_idx = src.index("finally:")
        committed_idx = src.index("drag_committed.emit")
        assert committed_idx > finally_idx, (
            "drag_committed.emit must sit inside the finally: block so it "
            "fires even when a downstream slot raises (Bug-2).")


# ── Bug-3: focus-out dedupe; explicit commit always fires ────────────


class TestExpressionEditorDedupe:
    """Bug-3: focusOut must not re-commit unchanged text."""

    def test_focusout_with_unchanged_text_does_not_emit(self, qapp):
        from src.features.composite.ui.graph_expression_editor import \
            GraphExpressionEditor
        ed = GraphExpressionEditor()
        ed.resize(400, 120)
        ed.set_target("n1", "rotation", "Rotation", "value * 2")
        received = []
        ed.expression_committed.connect(
            lambda nid, pn, txt: received.append((nid, pn, txt)))
        # Simulate a focus-out with no text change (e.g. user clicked the
        # tree to select another param).
        ed._text_edit.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
        assert received == [], (
            "focusOut with unchanged text must not re-commit — otherwise "
            "switching selection re-mutates the old param (and via A-15 "
            "strips its keyframes).")

    def test_focusout_with_changed_text_emits(self, qapp):
        from src.features.composite.ui.graph_expression_editor import \
            GraphExpressionEditor
        ed = GraphExpressionEditor()
        ed.resize(400, 120)
        ed.set_target("n1", "rotation", "Rotation", "value")
        received = []
        ed.expression_committed.connect(
            lambda nid, pn, txt: received.append((nid, pn, txt)))
        ed._text_edit.setPlainText("value * 3")
        ed._text_edit.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
        assert received == [("n1", "rotation", "value * 3")]

    def test_explicit_ctrl_return_always_commits(self, qapp):
        """Ctrl/Cmd+Return is an explicit commit — it fires even when the
        text equals the seeded value (user may want a forced re-eval)."""
        from src.features.composite.ui.graph_expression_editor import \
            GraphExpressionEditor
        from PyQt6.QtCore import QEvent
        ed = GraphExpressionEditor()
        ed.resize(400, 120)
        ed.set_target("n1", "rotation", "Rotation", "value")
        received = []
        ed.expression_committed.connect(
            lambda nid, pn, txt: received.append((nid, pn, txt)))
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                       Qt.KeyboardModifier.ControlModifier)
        ed._text_edit.keyPressEvent(ev)
        assert len(received) == 1

    def test_set_target_resets_dedupe_baseline(self, qapp):
        """Switching to a new param seeds its text as the new baseline, so a
        later unchanged focusOut on the NEW param is a no-op."""
        from src.features.composite.ui.graph_expression_editor import \
            GraphExpressionEditor
        ed = GraphExpressionEditor()
        ed.resize(400, 120)
        ed.set_target("n1", "rotation", "Rotation", "value * 2")
        # Switch to a second param.
        ed.set_target("n2", "scale", "Scale", "value * 5")
        received = []
        ed.expression_committed.connect(
            lambda nid, pn, txt: received.append((nid, pn, txt)))
        ed._text_edit.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
        assert received == [], (
            "The newly-seeded text should be treated as already-committed.")


# ── Bug-1: panel forwards expression edits (no direct node write) ────


class TestPanelForwardsExpression:
    """Bug-1: the Keyframes panel must emit graph_expression_changed instead
    of writing the node directly, so the page can own the undo record.

    These are structural checks (signal presence + page slot presence) to
    avoid the known tests/ui full-panel instantiation Qt global-state leak;
    the behavioural loop is covered by the editor-level dedupe tests above
    and the existing page integration tests."""

    def test_panel_has_graph_expression_changed_signal(self):
        from src.features.composite.ui.keyframes_panel import KeyframesPanel
        # The signal must exist on the class for the page to wire it.
        assert hasattr(KeyframesPanel, "graph_expression_changed"), (
            "KeyframesPanel must expose graph_expression_changed so "
            "CompositePage can route Graph expression edits through the "
            "authoritative undo path (Bug-1).")

    def test_page_has_graph_expression_handler(self):
        from src.features.composite.ui.composite_page import CompositePage
        # The page must define the handler that owns the undo record.
        assert hasattr(CompositePage, "_on_graph_expression_changed"), (
            "CompositePage must define _on_graph_expression_changed to own "
            "the Graph expression undo record (Bug-1).")

    def test_panel_on_committed_forwards_via_signal(self):
        """The panel's _on_expression_committed must emit the text via
        graph_expression_changed, not return early or write the node. We
        verify the method body delegates to the signal (structural check on
        the source avoids instantiating the full panel under offscreen Qt)."""
        import inspect
        from src.features.composite.ui.keyframes_panel import KeyframesPanel
        src = inspect.getsource(KeyframesPanel._on_expression_committed)
        assert "graph_expression_changed.emit" in src, (
            "_on_expression_committed must forward via the signal so the "
            "page owns the authoritative write + undo (Bug-1).")
        # And it must NOT call node.set_expression directly (old buggy path).
        assert "node.set_expression" not in src, (
            "_on_expression_committed must not write the node directly "
            "(that bypassed the undo stack — Bug-1).")
