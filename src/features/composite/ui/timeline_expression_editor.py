# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""In-timeline expression editor widget (P5 view, T50-T52/T54).

A small, embeddable QWidget for editing one parameter's expression inline
on the timeline (plan §19.7 — 'expression field belongs under the property
row, not a permanently detached window'). It is intentionally NOT a modal
dialog: the host opens it positioned under a parameter row and the user
commits with Ctrl/Cmd+Enter, cancels with Esc.

Why a dedicated widget instead of reusing KeyframeEditorWidget:

* KeyframeEditorWidget is a per-parameter dialog (modal, separate window)
  — the plan explicitly wants expressions edited in-place under the row.
* this editor consumes batch-5's ExpressionStatus / ExpressionPolicy and
  emits batch-2 TimelineCommand results, so validation + commit share the
  exact path the timeline uses for keyframe edits (no second validation
  logic).
* Auto-complete / language-menu hooks (T89) plug in later without
  touching the editor's commit path.

Lifecycle (plan §19.7):

* open(node_id, param_name, initial_text) → shows the editor, pre-fills
  the text, selects-all so a fresh expression replaces the old on type.
* editing → live status preview (valid / error) via derive_expression_status.
* Ctrl/Cmd+Enter → commit; emit expression_committed(node_id, param,
  text). Esc → cancel; emit expression_cancelled. The host decides whether
  to actually apply (it routes through TimelineCommandBuilder.set_expression
  for undo + lock enforcement).
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C
from .timeline_expression import (
    EXPRESSION_DISABLED,
    EXPRESSION_ERROR,
    EXPRESSION_NONE,
    EXPRESSION_VALID,
    ExpressionStatus,
)


class _ExpressionTextArea(QPlainTextEdit):
    """Multi-line editor with Ctrl/Cmd+Enter-to-commit / Esc-to-cancel.

    Plain Enter inserts a newline (so multi-line expressions are possible);
    Ctrl+Enter (or Cmd+Enter on macOS) commits. Esc always cancels.
    """

    commit_requested = pyqtSignal()
    cancel_requested = pyqtSignal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        mods = event.modifiers()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if mods & (Qt.KeyboardModifier.ControlModifier
                       | Qt.KeyboardModifier.MetaModifier):
                self.commit_requested.emit()
                event.accept()
                return
            # Plain Enter → newline (default behavior).
        elif key == Qt.Key.Key_Escape:
            self.cancel_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class TimelineExpressionEditor(QFrame):
    """Inline expression editor for one parameter row.

    Signals:
      expression_committed(node_id, param_name, text) — host applies via
        TimelineCommandBuilder.set_expression (gets undo + lock check).
      expression_cancelled() — host closes the editor without changes.
      expression_disabled(node_id, param_name, disabled) — host toggles the
        'enabled' flag without deleting the text (plan §5.4 rule 5 / T49).
    """

    expression_committed = pyqtSignal(str, str, str)
    expression_cancelled = pyqtSignal()
    expression_disabled = pyqtSignal(str, str, bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("TimelineExpressionEditor")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._node_id: Optional[str] = None
        self._param_name: Optional[str] = None
        self._status_provider = None  # (node_id, param) -> ExpressionStatus
        self._enabled = True
        self._build_ui()
        self._apply_style()

    # ── build ──

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(6)
        self._label = QLabel("=")
        self._label.setStyleSheet(
            f"color: {C.TEXT_SECONDARY}; font-family: monospace;")
        self._title = QLabel("")
        self._title.setStyleSheet(
            f"color: {C.TEXT_PRIMARY}; font-weight: 500;")
        header.addWidget(self._label)
        header.addWidget(self._title, 1)
        # Disable/enable toggle (T49 4-state, plan §5.4 rule 5).
        self._disable_btn = QPushButton()
        self._disable_btn.setCheckable(True)
        self._disable_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._disable_btn.clicked.connect(self._on_disable_toggled)
        header.addWidget(self._disable_btn)
        layout.addLayout(header)

        self._editor = _ExpressionTextArea()
        self._editor.setPlaceholderText(
            I18N.tr("expr_editor.placeholder", "Expression — e.g. value * 2 + sin(time)"))
        self._editor.setTabChangesFocus(True)
        self._editor.commit_requested.connect(self.commit)
        self._editor.cancel_requested.connect(self.cancel)
        self._editor.textChanged.connect(self._on_text_changed)
        # Default to a single comfortable line; the user can drag taller.
        self._editor.setFixedHeight(48)
        layout.addWidget(self._editor)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {C.TEXT_HINT};")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self._cancel_btn = QPushButton(I18N.tr("common.cancel", "Cancel"))
        self._cancel_btn.clicked.connect(self.cancel)
        self._commit_btn = QPushButton(I18N.tr("expr_editor.apply", "Apply"))
        self._commit_btn.setDefault(True)
        self._commit_btn.clicked.connect(self.commit)
        footer.addWidget(self._cancel_btn)
        footer.addWidget(self._commit_btn)
        layout.addLayout(footer)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            f"TimelineExpressionEditor {{"
            f"  background-color: {C.BG_DARK};"
            f"  border: 1px solid {C.BORDER_DARK};"
            f"  border-radius: 2px;"
            f"}}"
            f"QPlainTextEdit {{"
            f"  background-color: {C.BG_INPUT};"
            f"  color: {C.TEXT_PRIMARY};"
            f"  border: 1px solid {C.BORDER_DARK};"
            f"  font-family: monospace;"
            f"}}"
            f"QPushButton {{"
            f"  background-color: {C.BG_PANEL};"
            f"  color: {C.TEXT_SECONDARY};"
            f"  border: 1px solid {C.BORDER_DARK};"
            f"  padding: 2px 10px;"
            f"}}"
            f"QPushButton:hover {{ background-color: {C.BG_HOVER}; }}"
            f"QPushButton:default {{ border-color: {C.BORDER_MID}; }}"
        )

    # ── open / close ──

    def open_for(self, node_id: str, param_name: str,
                 initial_text: str = "",
                 status_provider=None,
                 enabled: bool = True) -> None:
        """Show the editor pre-filled for ``(node_id, param_name)``.

        ``status_provider`` is an optional ``(node_id, param, draft_text) ->
        ExpressionStatus`` callback the editor polls on every keystroke for
        a live valid/error preview of the text CURRENTLY being typed
        (AUD-11025 — the provider used to receive only the target ids and
        could only report the already-committed expression). ``enabled=False``
        opens the editor in 'disabled' mode so the user can re-enable
        without re-typing.
        """
        self._node_id = node_id
        self._param_name = param_name
        self._status_provider = status_provider
        self._enabled = bool(enabled)
        self._title.setText(f"{param_name.replace('_', ' ').title()}")
        self._editor.setPlainText(initial_text or "")
        # Select-all so the first typed char replaces the old expression.
        cursor = self._editor.textCursor()
        cursor.select(cursor.SelectionType.Document)
        self._editor.setTextCursor(cursor)
        self._editor.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self._refresh_disable_btn()
        self._update_status_preview(initial_text or "")
        self.show()

    def close(self) -> None:  # type: ignore[override]
        self._node_id = None
        self._param_name = None
        super().close()

    # ── commit / cancel ──

    def commit(self) -> None:
        if self._node_id is None or self._param_name is None:
            return
        text = self._editor.toPlainText()
        self.expression_committed.emit(self._node_id, self._param_name, text)

    def cancel(self) -> None:
        self.expression_cancelled.emit()

    # ── disable toggle (T49) ──

    def _on_disable_toggled(self, checked: bool) -> None:
        # checked == "disable" pressed → expression disabled.
        self._enabled = not checked
        if self._node_id is not None and self._param_name is not None:
            self.expression_disabled.emit(
                self._node_id, self._param_name, not self._enabled)
        self._refresh_disable_btn()

    def _refresh_disable_btn(self) -> None:
        self._disable_btn.setChecked(not self._enabled)
        self._disable_btn.setText("Enable" if not self._enabled else "Disable")
        # Read-only when disabled so the user sees the state.
        self._editor.setReadOnly(not self._enabled)

    # ── live status preview ──

    def _on_text_changed(self) -> None:
        text = self._editor.toPlainText()
        self._update_status_preview(text)

    def _update_status_preview(self, text: str) -> None:
        """Show a live valid/error hint under the editor.

        AUD-11025: the provider receives the DRAFT text so the preview
        reflects what the user is currently typing, not the expression
        already committed on the node. Uses the host-supplied status
        provider when available; otherwise falls back to a simple
        empty-check so the editor is useful even without a graph (e.g. in
        tests).
        """
        if not self._enabled:
            self._status_label.setText(
                I18N.tr(
                    "expr_editor.status_disabled",
                    "Expression disabled — value uses the keyframe/static base."))
            self._status_label.setStyleSheet(f"color: {C.TEXT_HINT};")
            return
        if self._status_provider is not None:
            try:
                status = self._status_provider(self._node_id, self._param_name,
                                               text)
            except Exception:
                status = None
            if isinstance(status, ExpressionStatus):
                self._apply_status(status, text)
                return
        # Fallback: empty text → hint, otherwise assume valid (the host's
        # commit path is the real validator).
        if not text.strip():
            self._status_label.setText(
                I18N.tr(
                    "expr_editor.hint_enter",
                    "Enter an expression. Use value, time, frame, comp.width…"))
            self._status_label.setStyleSheet(f"color: {C.TEXT_HINT};")
        else:
            self._status_label.setText(I18N.tr("expr_editor.hint_apply_validate", "Apply to validate."))
            self._status_label.setStyleSheet(f"color: {C.TEXT_SECONDARY};")

    def _apply_status(self, status: ExpressionStatus, text: str) -> None:
        from . import timeline_layout
        if not text.strip():
            # An empty draft has nothing to validate — showing the
            # provider's stale valid/error state here would mislead
            # (AUD-11025).
            self._status_label.setText(
                I18N.tr(
                    "expr_editor.hint_enter",
                    "Enter an expression. Use value, time, frame, comp.width…"))
            self._status_label.setStyleSheet(f"color: {C.TEXT_HINT};")
            return
        if status.state == EXPRESSION_ERROR:
            self._status_label.setText(
                f"Error: {status.error_message or status.error_code}")
            self._status_label.setStyleSheet(
                f"color: {timeline_layout.resolve_token('error')};")
        elif status.state == EXPRESSION_VALID:
            src = status.source_value or "static"
            self._status_label.setText(
                f"Valid — overrides {src} value at the playhead.")
            self._status_label.setStyleSheet(f"color: {C.TEXT_SECONDARY};")
        elif status.state == EXPRESSION_NONE:
            self._status_label.setText(I18N.tr("expr_editor.status_new", "New expression."))
            self._status_label.setStyleSheet(f"color: {C.TEXT_HINT};")
        elif status.state == EXPRESSION_DISABLED:
            self._status_label.setText(
                I18N.tr(
                    "expr_editor.status_disabled_short",
                    "Disabled — value uses the keyframe/static base."))
            self._status_label.setStyleSheet(f"color: {C.TEXT_HINT};")


__all__ = ["TimelineExpressionEditor"]
