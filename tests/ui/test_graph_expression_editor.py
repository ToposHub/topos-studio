"""T120 Graph Expression Editor tests.

Headless widget tests for the bottom expression editor (plan §22.11 /
§19.7). The editor is plain Qt (a label + a QPlainTextEdit), so these
tests drive it through its public API and assert the signal contract:
the editor never edits the node directly — it broadcasts the committed
text so the panel can validate + push undo.

Covers:
  * set_target / clear_target bind the field and seed the text.
  * The multi-select hint shows when count > 1.
  * Editing + focus-out emits expression_committed with the payload.
  * Ctrl/Cmd+Return commits immediately.
  * Programmatic seeding doesn't fire a spurious commit (reentry guard).
  * show_error toggles the error label + red border without losing text.
  * Empty target (no param) keeps the editor disabled.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import Qt, QEvent
from PyQt6.QtGui import QFocusEvent, QKeyEvent
from PyQt6.QtWidgets import QApplication

from src.features.composite.ui.graph_expression_editor import (
    GraphExpressionEditor, _CommitTextEdit,
)


pytestmark = pytest.mark.composite_core


@pytest.fixture
def editor(qapp):
    e = GraphExpressionEditor()
    e.resize(400, 120)
    return e


# ── target binding ────────────────────────────────────────────────────


class TestTargetBinding:
    def test_initial_state_is_empty(self, editor, qapp):
        """A fresh editor has no target and the field is disabled."""
        assert editor.target() == (None, None)
        assert editor.current_text() == ""
        assert editor._text_edit.isEnabled() is False

    def test_set_target_seeds_text(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation", "value * 2")
        assert editor.target() == ("n1", "rotation")
        assert editor.current_text() == "value * 2"
        assert editor._text_edit.isEnabled() is True

    def test_set_target_uses_display_name(self, editor, qapp):
        editor.set_target("n1", "translate_x", "Translate X")
        assert "Translate X" in editor._title_label.text()

    def test_set_target_no_expression_seeds_empty(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation", expression=None)
        assert editor.current_text() == ""
        assert editor._enabled_button.isEnabled() is False

    def test_set_target_restores_disabled_expression_state(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation", "value * 2",
                          expression_enabled=False)
        assert editor._enabled_button.isChecked() is True
        assert editor._enabled_button.text() == "Enable"

    def test_clear_target_resets(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation", "value * 2")
        editor.clear_target()
        assert editor.target() == (None, None)
        assert editor.current_text() == ""
        assert editor._text_edit.isEnabled() is False

    def test_set_target_none_clears(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation", "value * 2")
        editor.set_target(None, None)
        assert editor.target() == (None, None)


# ── multi-select hint ─────────────────────────────────────────────────


class TestMultiSelectHint:
    def test_single_select_no_hint(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation",
                          multi_select_count=1)
        assert editor._hint_label.isVisible() is False

    def test_multi_select_shows_hint(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation",
                          multi_select_count=3)
        # isVisible() is False offscreen until the parent is shown; check the
        # widget isn't explicitly hidden instead (the setVisible contract).
        assert editor._hint_label.isHidden() is False
        assert "3" in editor._hint_label.text()


# ── commit signal ─────────────────────────────────────────────────────


class TestCommit:
    def test_focus_out_emits_committed(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation", "value")
        received = []
        editor.expression_committed.connect(
            lambda nid, pn, txt: received.append((nid, pn, txt)))
        # Change the text + fire focus-out.
        editor._text_edit.setPlainText("value * 3")
        # Simulate focus loss.
        ev = QFocusEvent(QEvent.Type.FocusOut)
        editor._text_edit.focusOutEvent(ev)
        assert received == [("n1", "rotation", "value * 3")]

    def test_ctrl_return_emits_committed(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation", "value")
        received = []
        editor.expression_committed.connect(
            lambda nid, pn, txt: received.append((nid, pn, txt)))
        editor._text_edit.setPlainText("wiggle(2, 10)")
        # Ctrl+Return.
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                       Qt.KeyboardModifier.ControlModifier)
        editor._text_edit.keyPressEvent(ev)
        assert received == [("n1", "rotation", "wiggle(2, 10)")]

    def test_cmd_return_emits_committed(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation", "value")
        received = []
        editor.expression_committed.connect(received.append)
        # Cmd+Return (macOS).
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                       Qt.KeyboardModifier.MetaModifier)
        editor._text_edit.keyPressEvent(ev)
        assert len(received) == 1

    def test_plain_return_does_not_commit(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation", "value")
        received = []
        editor.expression_committed.connect(received.append)
        # Plain Return (no modifier) → inserts newline, no commit.
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                       Qt.KeyboardModifier.NoModifier)
        editor._text_edit.keyPressEvent(ev)
        assert received == []


class TestExpressionEnabled:
    def test_toggle_emits_enabled_state(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation", "value")
        received = []
        editor.expression_enabled_changed.connect(
            lambda nid, param, enabled: received.append((nid, param, enabled)))

        editor._enabled_button.click()

        assert received == [("n1", "rotation", False)]
        assert editor._enabled_button.text() == "Enable"

    def test_toggle_without_expression_does_not_emit(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation")
        received = []
        editor.expression_enabled_changed.connect(received.append)
        assert editor._enabled_button.isEnabled() is False
        editor._enabled_button.click()
        assert received == []

    def test_no_target_does_not_emit(self, editor, qapp):
        """With no target bound, focus-out is a no-op (no spurious emit)."""
        received = []
        editor.expression_committed.connect(received.append)
        editor._text_edit.setPlainText("value")
        ev = QFocusEvent(QEvent.Type.FocusOut)
        editor._text_edit.focusOutEvent(ev)
        assert received == []

    def test_programmatic_seed_does_not_emit(self, editor, qapp):
        """set_target seeding the text must not fire a commit (reentry guard)."""
        editor.set_target("n1", "rotation", "Rotation", "value * 2")
        received = []
        editor.expression_committed.connect(received.append)
        # Re-seed — the guard should suppress any commit from focus changes
        # during seeding.
        editor.set_target("n1", "rotation", "Rotation", "value * 5")
        assert received == []


# ── error display ─────────────────────────────────────────────────────


class TestErrorDisplay:
    def test_show_error_displays_message(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation", "value +")
        editor.show_error("Syntax error")
        assert editor._error_label.isHidden() is False
        assert "Syntax error" in editor._error_label.text()

    def test_show_error_none_clears(self, editor, qapp):
        editor.show_error("err")
        editor.show_error(None)
        assert editor._error_label.isHidden() is True

    def test_error_keeps_text(self, editor, qapp):
        """An invalid expression's text is preserved so the user can fix it."""
        editor.set_target("n1", "rotation", "Rotation", "value +")
        editor.show_error("Syntax error")
        assert editor.current_text() == "value +"


# ── paint ─────────────────────────────────────────────────────────────


class TestPaint:
    def test_paint_with_target_no_crash(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation", "value * 2")
        editor.show()
        qapp.processEvents()
        editor.repaint()

    def test_paint_empty_state_no_crash(self, editor, qapp):
        editor.clear_target()
        editor.show()
        qapp.processEvents()
        editor.repaint()

    def test_paint_with_error_no_crash(self, editor, qapp):
        editor.set_target("n1", "rotation", "Rotation", "value +")
        editor.show_error("Syntax error")
        editor.show()
        qapp.processEvents()
        editor.repaint()
