"""P5 view tests: in-timeline expression editor (T50-T52/T54).

Verifies the TimelineExpressionEditor widget's lifecycle and signals
without launching a modal dialog: open/commit/cancel, the disable toggle,
Esc/Ctrl+Enter key handling, and the live status preview. The actual
expression application (validation, undo, lock) is the host's job via
TimelineCommandBuilder.set_expression — covered by test_timeline_commands.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QApplication

from src.features.composite.ui.timeline_expression import (
    EXPRESSION_DISABLED,
    EXPRESSION_ERROR,
    EXPRESSION_NONE,
    EXPRESSION_VALID,
    ExpressionStatus,
)
from src.features.composite.ui.timeline_expression_editor import (
    TimelineExpressionEditor,
)


@pytest.fixture
def editor(qapp):
    e = TimelineExpressionEditor()
    e.resize(400, 200)
    return e


# ── open / pre-fill ────────────────────────────────────────────────────


class TestOpenAndPreFill:
    def test_open_for_prefills_text(self, editor):
        editor.open_for("node1", "rotation", initial_text="value + 10")
        assert editor._editor.toPlainText() == "value + 10"

    def test_open_for_records_target(self, editor):
        editor.open_for("node1", "rotation", initial_text="value")
        assert editor._node_id == "node1"
        assert editor._param_name == "rotation"

    def test_open_for_selects_all_so_first_type_replaces(self, editor):
        """Plan §19.7: entering edit mode selects-all so the user can
        retype a fresh expression without manually clearing."""
        editor.open_for("n", "p", initial_text="old")
        cursor = editor._editor.textCursor()
        assert cursor.hasSelection()
        assert cursor.selectedText() == "old"

    def test_open_for_empty_text(self, editor):
        editor.open_for("n", "p", initial_text="")
        assert editor._editor.toPlainText() == ""

    def test_open_for_humanizes_title(self, editor):
        editor.open_for("n", "translate_x", initial_text="value")
        assert "Translate X" in editor._title.text()


# ── commit / cancel signals ────────────────────────────────────────────


class TestCommitCancelSignals:
    def test_commit_emits_expression_committed(self, editor):
        committed = []
        editor.expression_committed.connect(
            lambda n, p, t: committed.append((n, p, t)))
        editor.open_for("node1", "rotation", initial_text="value + 10")
        editor.commit()
        assert committed == [("node1", "rotation", "value + 10")]

    def test_commit_uses_current_text_not_initial(self, editor):
        committed = []
        editor.expression_committed.connect(
            lambda n, p, t: committed.append(t))
        editor.open_for("n", "p", initial_text="old")
        editor._editor.setPlainText("new * 2")
        editor.commit()
        assert committed == ["new * 2"]

    def test_cancel_emits_expression_cancelled(self, editor):
        cancelled = []
        editor.expression_cancelled.connect(lambda: cancelled.append(True))
        editor.open_for("n", "p", initial_text="x")
        editor.cancel()
        assert cancelled == [True]

    def test_commit_without_open_is_noop(self, editor):
        """Calling commit before open_for must not emit (defensive)."""
        committed = []
        editor.expression_committed.connect(
            lambda n, p, t: committed.append(t))
        editor.commit()  # no target set
        assert committed == []

    def test_apply_button_commits(self, editor):
        committed = []
        editor.expression_committed.connect(
            lambda n, p, t: committed.append(t))
        editor.open_for("n", "p", initial_text="value")
        editor._commit_btn.click()
        assert committed == ["value"]

    def test_cancel_button_cancels(self, editor):
        cancelled = []
        editor.expression_cancelled.connect(lambda: cancelled.append(True))
        editor.open_for("n", "p", initial_text="value")
        editor._cancel_btn.click()
        assert cancelled == [True]


# ── keyboard: Ctrl+Enter commits, Esc cancels ──────────────────────────


class TestKeyboardShortcuts:
    def _key(self, editor, key, mods=Qt.KeyboardModifier.NoModifier):
        ev = QKeyEvent(QKeyEvent.Type.KeyPress, key, mods)
        editor._editor.keyPressEvent(ev)

    def test_ctrl_enter_commits(self, editor):
        committed = []
        editor.expression_committed.connect(
            lambda n, p, t: committed.append(t))
        editor.open_for("n", "p", initial_text="value")
        self._key(editor, Qt.Key.Key_Return,
                  Qt.KeyboardModifier.ControlModifier)
        assert committed == ["value"]

    def test_cmd_enter_commits_on_macos(self, editor):
        committed = []
        editor.expression_committed.connect(
            lambda n, p, t: committed.append(t))
        editor.open_for("n", "p", initial_text="value")
        self._key(editor, Qt.Key.Key_Enter,
                  Qt.KeyboardModifier.MetaModifier)
        assert committed == ["value"]

    def test_plain_enter_does_not_commit(self, editor):
        """Plain Enter inserts a newline — multi-line expressions must be
        possible (plan §19.7)."""
        committed = []
        editor.expression_committed.connect(
            lambda n, p, t: committed.append(t))
        editor.open_for("n", "p", initial_text="value")
        self._key(editor, Qt.Key.Key_Return)
        assert committed == []  # no commit
        # And the editor still has focus / is editable.
        assert editor._editor.isEnabled()

    def test_escape_cancels(self, editor):
        cancelled = []
        editor.expression_cancelled.connect(lambda: cancelled.append(True))
        editor.open_for("n", "p", initial_text="value")
        self._key(editor, Qt.Key.Key_Escape)
        assert cancelled == [True]


# ── disable toggle (T49 4-state, plan §5.4 rule 5) ─────────────────────


class TestDisableToggle:
    def test_disable_button_emits_expression_disabled(self, editor):
        disabled = []
        editor.expression_disabled.connect(
            lambda n, p, d: disabled.append((n, p, d)))
        editor.open_for("n", "p", initial_text="value", enabled=True)
        # The disable button starts unchecked (expression enabled). Clicking
        # toggles it on → expression becomes disabled.
        editor._disable_btn.click()
        # The signal carries disabled=True (the new state).
        assert disabled[-1] == ("n", "p", True)

    def test_disabled_editor_is_read_only(self, editor):
        editor.open_for("n", "p", initial_text="value", enabled=False)
        assert editor._editor.isReadOnly() is True

    def test_enabled_editor_is_editable(self, editor):
        editor.open_for("n", "p", initial_text="value", enabled=True)
        assert editor._editor.isReadOnly() is False

    def test_disable_then_enable_round_trip(self, editor):
        disabled = []
        editor.expression_disabled.connect(
            lambda n, p, d: disabled.append(d))
        editor.open_for("n", "p", initial_text="value", enabled=True)
        # Disable.
        editor._disable_btn.click()
        # The button is checkable; click again to re-enable.
        editor._disable_btn.click()
        # Two signals fired: disable(True), then enable(False).
        assert disabled == [True, False]


# ── live status preview ────────────────────────────────────────────────


class TestStatusPreview:
    # AUD-11025: providers take (node_id, param, draft_text) so the preview
    # reflects the text being typed, not the committed expression.

    def test_provider_error_shown_in_status_label(self, editor):
        editor.open_for(
            "n", "p", initial_text="value +",
            status_provider=lambda n, p, t: ExpressionStatus(
                state=EXPRESSION_ERROR,
                error_code="expr_syntax",
                error_message="Syntax error near '+'"))
        assert "Syntax error" in editor._status_label.text()

    def test_provider_valid_shows_override_hint(self, editor):
        editor.open_for(
            "n", "p", initial_text="value + 10",
            status_provider=lambda n, p, t: ExpressionStatus(
                state=EXPRESSION_VALID, source_value="keyframes"))
        text = editor._status_label.text().lower()
        assert "valid" in text
        assert "keyframes" in text

    def test_provider_receives_draft_text(self, editor):
        """AUD-11025: the provider is polled with the text being typed so a
        syntax error in the draft shows immediately (previously the provider
        only saw (node_id, param) and re-reported the committed state)."""
        seen = []
        editor.open_for(
            "n", "p", initial_text="value + 10",
            status_provider=lambda n, p, t: (seen.append(t), ExpressionStatus(
                state=EXPRESSION_ERROR, error_code="expr_syntax",
                error_message="bad"))[1])
        editor._editor.setPlainText("value +++++")
        assert "value +++++" in seen

    def test_no_provider_falls_back_to_apply_hint(self, editor):
        editor.open_for("n", "p", initial_text="value + 10")
        assert "Apply" in editor._status_label.text()

    def test_no_provider_empty_text_shows_help(self, editor):
        editor.open_for("n", "p", initial_text="")
        text = editor._status_label.text().lower()
        assert "expression" in text or "value" in text or "enter" in text

    def test_buggy_provider_does_not_crash_preview(self, editor):
        def boom(n, p, t):
            raise RuntimeError("status provider crashed")
        # Must not raise on open (which triggers the preview).
        editor.open_for("n", "p", initial_text="value",
                        status_provider=boom)
        # Falls back to the no-provider path.
        assert "Apply" in editor._status_label.text()


# ── text-changed updates preview live ──────────────────────────────────


class TestLiveUpdate:
    def test_typing_updates_preview(self, editor):
        calls = []
        editor.open_for(
            "n", "p", initial_text="",
            status_provider=lambda n, p, t: (
                calls.append(n), ExpressionStatus(
                    state=EXPRESSION_NONE))[1])
        # Reset and type.
        calls.clear()
        editor._editor.setPlainText("value + 1")
        # The provider was polled on the text change.
        assert len(calls) >= 1
