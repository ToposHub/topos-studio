"""Manual HTML/CSS source editor for a Motion Graphic HTML layer."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QLabel, QPlainTextEdit, QPushButton, QToolButton, QVBoxLayout, QWidget

from src.shared.i18n.i18n import I18N
from ..core.html_layer import FreeformHtmlMotionLayer


class HtmlSourceEditor(QWidget):
    """Edit the stored HTML source and let the host sanitize it on apply."""

    sourceEditRequested = pyqtSignal(str)
    draftChanged = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mgHtmlSourceEditor")
        self._layer: FreeformHtmlMotionLayer | None = None
        self._updating = False
        self._original = ""
        self._drafts: dict[str, str] = {}
        self._status = QLabel(I18N.tr("motion_graphic.source.hint", "Select an HTML scene to edit source"), self)
        self._status.setObjectName("mgMutedLabel")
        self.editor = QPlainTextEdit(self)
        self.editor.setObjectName("htmlSourceText")
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setPlaceholderText(I18N.tr("motion_graphic.source.placeholder", "Edit HTML/CSS here; it will be sanitized by the project HTML sanitizer when applied."))
        self.apply_button = QPushButton(I18N.tr("motion_graphic.source.apply", "Apply Source (sanitized and undoable)"), self)
        self.apply_button.setObjectName("mgPrimaryButton")
        self.reset_button = QPushButton(I18N.tr("motion_graphic.source.reset", "Restore Current Layer Source"), self)
        self.apply_button.clicked.connect(self._apply)
        self.reset_button.clicked.connect(self._reset)
        self.editor.textChanged.connect(self._on_text_changed)
        buttons = QWidget(self)
        button_layout = QVBoxLayout(buttons)
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.addWidget(self.apply_button)
        button_layout.addWidget(self.reset_button)
        # Read-only mirror of the editor-generated scene output (native DOM
        # overlay, token CSS, animation CSS).  Template authors inspect what
        # the compositor will build without touching the editable source.
        self.generated_toggle = QToolButton(self)
        self.generated_toggle.setText(I18N.tr(
            "motion_graphic.source.generated_title", "Generated Scene Output (read-only)"))
        self.generated_toggle.setCheckable(True)
        self.generated_toggle.setChecked(False)
        self.generated_toggle.setStyleSheet(
            "QToolButton { border: none; color: #9a9a9a; background: transparent; padding: 2px 0; }"
            "QToolButton:hover { color: #ffffff; }")
        self.generated_view = QPlainTextEdit(self)
        self.generated_view.setObjectName("htmlGeneratedOutput")
        self.generated_view.setReadOnly(True)
        self.generated_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.generated_view.setVisible(False)
        self.generated_view.setPlaceholderText(I18N.tr(
            "motion_graphic.source.generated_hint",
            "The DOM/CSS the editor generates for native layers appears here"))
        self.generated_toggle.toggled.connect(self.generated_view.setVisible)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(QLabel(I18N.tr("motion_graphic.source.title", "HTML / CSS Source"), self))
        layout.addWidget(self._status)
        layout.addWidget(self.editor, 1)
        layout.addWidget(self.generated_toggle)
        layout.addWidget(self.generated_view)
        layout.addWidget(buttons)
        self.setEnabled(False)

    def set_generated_output(self, markup: str) -> None:
        """Show what the compositor generates (native overlay + token CSS)."""
        text = str(markup or "").strip()
        self._updating = True
        try:
            self.generated_view.setPlainText(text)
        finally:
            self._updating = False

    def set_layer(self, layer: FreeformHtmlMotionLayer | None) -> None:
        self._remember_current_draft()
        self._layer = layer
        self._updating = True
        try:
            if layer is None:
                self._original = ""
                self.editor.clear()
                self._status.setText(I18N.tr("motion_graphic.source.hint", "Select an HTML scene to edit source"))
                self.setEnabled(False)
                self.draftChanged.emit(False)
                return
            self._original = str(layer.sanitized_html)
            key = self._layer_key(layer)
            draft = self._drafts.get(key, self._original)
            self.editor.setPlainText(draft)
            self._update_status()
            self.setEnabled(True)
        finally:
            self._updating = False
        self._emit_draft_state()

    @property
    def has_pending_draft(self) -> bool:
        return self._layer is not None and self.editor.toPlainText() != self._original

    def discard_pending_draft(self) -> None:
        """Restore the saved source for the visible layer and clear its draft."""
        if self._layer is None:
            return
        self._drafts.pop(self._layer_key(self._layer), None)
        self._updating = True
        try:
            self.editor.setPlainText(self._original)
            self._update_status()
        finally:
            self._updating = False
        self._emit_draft_state()

    @staticmethod
    def _layer_key(layer: FreeformHtmlMotionLayer) -> str:
        return str(layer.layer_id)

    def _remember_current_draft(self) -> None:
        if self._updating or self._layer is None:
            return
        key = self._layer_key(self._layer)
        draft = self.editor.toPlainText()
        if draft == self._original:
            self._drafts.pop(key, None)
        else:
            self._drafts[key] = draft

    def _on_text_changed(self) -> None:
        if self._updating:
            return
        self._remember_current_draft()
        self._update_status()
        self._emit_draft_state()

    def _update_status(self) -> None:
        if self._layer is None:
            return
        if self.has_pending_draft:
            self._status.setText(I18N.tr(
                "motion_graphic.source.draft", "Unapplied draft · Apply Source to save it to the Motion Graphic",
            ))
            return
        self._status.setText(I18N.tr(
            "motion_graphic.source.revision", "Revision {revision} · {characters:,} characters · sanitized when applied",
            revision=self._layer.revision, characters=len(self._original),
        ))

    def _emit_draft_state(self) -> None:
        self.draftChanged.emit(self.has_pending_draft)

    def _apply(self) -> None:
        if self._updating or self._layer is None:
            return
        source = self.editor.toPlainText()
        if not source.strip():
            self._status.setText(I18N.tr("motion_graphic.source.empty", "Source cannot be empty"))
            return
        self.sourceEditRequested.emit(source)

    def _reset(self) -> None:
        if self._layer is None:
            return
        self.discard_pending_draft()


__all__ = ["HtmlSourceEditor"]
