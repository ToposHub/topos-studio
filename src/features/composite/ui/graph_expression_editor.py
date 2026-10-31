"""GraphExpressionEditor — T120 bottom expression editor for Graph mode.

A small resizable panel that sits under the Graph canvas and edits the
PRIMARY selected parameter's expression. Plan §22.11 / §19.7: the field
lives below the property; multi-select shows only the primary property
(avoid stacking several editors).

Contract:
  * ``set_target(node, param_name, display_name)`` — bind to a param
    (None clears the editor and shows an empty-state hint).
  * ``expression_committed(node_id, param_name, text)`` — emitted when the
    user edits the text (debounced on focus-out / return); the panel
    validates via ``node.set_expression`` and rebuilds the post curve.
  * The editor never edits the node directly — it broadcasts the intent so
    the panel (the canonical owner) can validate + push undo.

The widget is plain Qt (QPlainTextEdit + QLabel) so it's headless-
testable: tests set the text, fire ``editingFinished``, and assert the
signal payload. No expression evaluation happens here.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFocusEvent, QKeyEvent
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPlainTextEdit, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C


class _CommitTextEdit(QPlainTextEdit):
    """QPlainTextEdit that fires ``committed`` on focus-out and on
    Ctrl/Cmd+Return. Keeps the editor's commit logic in one place.

    The signal carries ``explicit``: True for an explicit Ctrl/Cmd+Return
    (always commits — the user asked for it), False for a focus-out (only
    commits when the text changed, so switching selection can't re-commit
    the old value — Bug-3)."""

    committed = pyqtSignal(bool)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        self.committed.emit(False)  # implicit (focus-out): dedupe applies

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and
                event.modifiers() & (Qt.KeyboardModifier.ControlModifier |
                                     Qt.KeyboardModifier.MetaModifier)):
            self.committed.emit(True)  # explicit: always commits
            event.accept()
            return
        super().keyPressEvent(event)


class GraphExpressionEditor(QWidget):
    """T120: bottom expression editor bound to the primary selected param.

    Shows the param's display name + a multi-line text field. Editing the
    field emits ``expression_committed`` so the panel can validate + apply.
    Invalid expressions keep their text (the panel reports the error back
    via ``show_error``) — the user's work-in-progress is never discarded.
    """

    # Emitted with (node_id, param_name, text) when the user commits an edit.
    expression_committed = pyqtSignal(str, str, str)
    # Emitted with (node_id, param_name, enabled) when the expression switch
    # changes. The host owns the graph mutation and undo record.
    expression_enabled_changed = pyqtSignal(str, str, bool)
    # Emitted by the compact header close button so the host can collapse the
    # editor without changing Graph/Timeline mode.
    collapse_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("GraphExpressionEditor")
        self.setMinimumHeight(64)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self._node_id: Optional[str] = None
        self._param_name: Optional[str] = None
        self._guard = False  # reentry guard while seeding text programmatically
        self._expression_enabled = True
        # Bug-3 fix: the text the panel last seeded (or we last emitted).
        # A focusOut with unchanged text is a no-op so switching selection
        # away from a param can't re-commit (and re-mutate) the old value.
        self._last_committed_text: str = ""
        self._build_ui()
        self.clear_target()  # start in the empty state

    # ── UI construction ──

    def _build_ui(self) -> None:
        self.setStyleSheet(
            f"QWidget#GraphExpressionEditor {{"
            f"  background-color: {C.BG_PANEL};"
            f"  border-top: 1px solid {C.BORDER_DARK};"
            f"}}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(2)

        # Header: property label + (multi-select hint, hidden by default).
        header = QHBoxLayout()
        header.setSpacing(8)
        self._title_label = QLabel("", self)
        self._title_label.setStyleSheet(
            f"color: {C.TEXT_PRIMARY}; font-size: 11px; font-weight: bold;")
        header.addWidget(self._title_label)
        self._hint_label = QLabel("", self)
        self._hint_label.setStyleSheet(
            f"color: {C.TEXT_HINT}; font-size: 10px;")
        header.addWidget(self._hint_label)
        header.addStretch()
        self._error_label = QLabel("", self)
        self._error_label.setStyleSheet(
            "color: #ff6666; font-size: 10px;")
        self._error_label.setVisible(False)
        header.addWidget(self._error_label)
        self._enabled_button = QToolButton(self)
        self._enabled_button.setObjectName("ExpressionEnabledButton")
        self._enabled_button.setCheckable(True)
        self._enabled_button.setToolTip(
            I18N.tr("keyframes.expression_toggle",
                    "Enable or disable expression")
        )
        self._enabled_button.setAccessibleName(
            I18N.tr("keyframes.expression_toggle",
                    "Enable or disable expression")
        )
        self._enabled_button.clicked.connect(self._on_enabled_toggled)
        header.addWidget(self._enabled_button)
        self._collapse_button = QToolButton(self)
        self._collapse_button.setObjectName("ExpressionEditorCollapseButton")
        self._collapse_button.setText("×")
        self._collapse_button.setToolTip(
            I18N.tr("keyframes.expression_collapse", "Collapse expression editor")
        )
        self._collapse_button.setAccessibleName(
            I18N.tr("keyframes.expression_collapse", "Collapse expression editor")
        )
        self._collapse_button.setFixedSize(20, 20)
        self._collapse_button.setStyleSheet(
            f"QToolButton {{ color: {C.TEXT_HINT}; border: none;"
            " background: transparent; font-size: 15px; }"
            f"QToolButton:hover {{ color: {C.TEXT_PRIMARY};"
            f" background: {C.BG_HOVER}; border-radius: 3px; }}"
        )
        self._collapse_button.clicked.connect(self.collapse_requested)
        header.addWidget(self._collapse_button)
        layout.addLayout(header)

        # The expression text field. Multi-line; fixed min height so it's
        # resizable but doesn't eat the whole canvas. The _CommitTextEdit
        # subclass emits `committed` on focus-out and Ctrl/Cmd+Return.
        self._text_edit = _CommitTextEdit(self)
        self._text_edit.setPlaceholderText(I18N.tr(
            "keyframes.expression_placeholder",
            "Expression (e.g. value * 2, wiggle(2, 10), frame)"))
        self._text_edit.setMinimumHeight(40)
        self._text_edit.setStyleSheet(
            f"QPlainTextEdit {{"
            f"  background-color: {C.BG_INPUT};"
            f"  color: {C.TEXT_PRIMARY};"
            f"  border: 1px solid {C.BORDER};"
            f"  font-family: monospace;"
            f"  font-size: 12px;"
            f"}}")
        self._text_edit.committed.connect(self._on_committed)
        layout.addWidget(self._text_edit)

    # ── public API ──

    def set_target(self, node_id: Optional[str], param_name: Optional[str],
                   display_name: Optional[str] = None,
                   expression: Optional[str] = None,
                   multi_select_count: int = 1,
                   expression_enabled: bool = True) -> None:
        """Bind the editor to a param (the primary selection). Pass
        ``node_id=None`` to clear. ``multi_select_count > 1`` shows a hint
        that only the primary property is editable."""
        self._guard = True
        try:
            self._node_id = node_id
            self._param_name = param_name
            self._expression_enabled = bool(expression_enabled)
            if node_id is None or param_name is None:
                self.clear_target()
                return
            label = display_name or param_name.replace("_", " ").title()
            self._title_label.setText(
                I18N.tr("keyframes.expression_for", "Expression") +
                f": {label}")
            # Seed the text field with the current expression (empty string
            # when the param has none).
            self._text_edit.setPlainText(expression or "")
            self._text_edit.setEnabled(True)
            self._refresh_enabled_button(bool(expression),
                                         self._expression_enabled)
            self.show_error(None)  # clear any prior error
            # Bug-3: treat the seeded text as "already committed" so a
            # subsequent focusOut with unchanged text is a no-op.
            self._last_committed_text = expression or ""
            # Multi-select hint.
            if multi_select_count > 1:
                self._hint_label.setText(I18N.tr(
                    "keyframes.expression_multi_hint",
                    f"(editing primary of {multi_select_count} selected)"))
                self._hint_label.setVisible(True)
            else:
                self._hint_label.setVisible(False)
        finally:
            self._guard = False

    def clear_target(self) -> None:
        """Show the empty-state (no param selected)."""
        self._guard = True
        try:
            self._node_id = None
            self._param_name = None
            self._expression_enabled = True
            self._title_label.setText(I18N.tr(
                "keyframes.expression_none",
                "Select a parameter to edit its expression"))
            self._text_edit.setPlainText("")
            self._text_edit.setEnabled(False)
            self._refresh_enabled_button(False, True)
            self._hint_label.setVisible(False)
            self.show_error(None)
            self._last_committed_text = ""
        finally:
            self._guard = False

    def show_error(self, message: Optional[str]) -> None:
        """Display (or clear) an expression-validation error. The text
        field's text is preserved so the user can fix the typo."""
        if message:
            self._error_label.setText(message)
            self._error_label.setVisible(True)
            # Red border on the text field to signal the error.
            self._text_edit.setStyleSheet(
                f"QPlainTextEdit {{"
                f"  background-color: {C.BG_INPUT};"
                f"  color: {C.TEXT_PRIMARY};"
                f"  border: 1px solid #ff6666;"
                f"  font-family: monospace;"
                f"  font-size: 12px;"
                f"}}")
        else:
            self._error_label.setVisible(False)
            self._text_edit.setStyleSheet(
                f"QPlainTextEdit {{"
                f"  background-color: {C.BG_INPUT};"
                f"  color: {C.TEXT_PRIMARY};"
                f"  border: 1px solid {C.BORDER};"
                f"  font-family: monospace;"
                f"  font-size: 12px;"
                f"}}")

    def current_text(self) -> str:
        """Read-only access to the field's current text."""
        return self._text_edit.toPlainText()

    def target(self) -> tuple:
        """The (node_id, param_name) currently bound, or (None, None)."""
        return (self._node_id, self._param_name)

    def _refresh_enabled_button(self, has_expression: bool,
                                enabled: bool) -> None:
        """Update the expression switch without emitting its signal."""
        self._enabled_button.blockSignals(True)
        try:
            self._enabled_button.setEnabled(has_expression)
            self._enabled_button.setChecked(not enabled)
            self._enabled_button.setText(
                I18N.tr("keyframes.expression_enable", "Enable")
                if not enabled else
                I18N.tr("keyframes.expression_disable", "Disable"))
        finally:
            self._enabled_button.blockSignals(False)

    def _on_enabled_toggled(self, disabled: bool) -> None:
        """Broadcast an expression enable/disable intent to the host."""
        if self._node_id is None or self._param_name is None:
            return
        if not self._text_edit.toPlainText().strip():
            return
        self._expression_enabled = not bool(disabled)
        self._refresh_enabled_button(True, self._expression_enabled)
        self.expression_enabled_changed.emit(
            self._node_id, self._param_name, self._expression_enabled)

    # ── internals ──

    def _on_committed(self, explicit: bool = False) -> None:
        """The text field lost focus (implicit) or got Ctrl/Cmd+Return
        (explicit). Broadcast the edit so the panel can validate + apply.
        Reentry-guarded so programmatic seeding doesn't fire a spurious commit.

        Bug-3 fix: an implicit focusOut dedupes against
        ``_last_committed_text`` — a focus-out that doesn't change the text
        (e.g. clicking another param while this one still has focus) emits
        nothing, so the old param can't be re-committed (and, via A-15,
        re-stripped of its keyframes). An explicit Ctrl/Cmd+Return always
        commits so the user can force a re-evaluation."""
        if self._guard:
            return
        if self._node_id is None or self._param_name is None:
            return
        text = self._text_edit.toPlainText()
        if not explicit and text == self._last_committed_text:
            return
        self._last_committed_text = text
        self.expression_committed.emit(self._node_id, self._param_name, text)
