"""Embedded Chromium DevTools workspace for Motion Graphic HTML scenes."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from src.shared.i18n.i18n import I18N


class HtmlDevToolsPanel(QWidget):
    """Host the real Qt WebEngine DevTools frontend.

    Elements/Console/Sources/Network are rendered by Chromium itself.  The
    panel only owns the editor-specific controls around it: opening the
    frontend and explicitly applying the live DOM/CSS state back to the
    versioned Motion Graphic HTML layer.
    """

    openRequested = pyqtSignal()
    applyDomRequested = pyqtSignal()
    discardDomRequested = pyqtSignal()
    draftChanged = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mgHtmlDevToolsPanel")
        self._devtools_widget: QWidget | None = None
        self._has_unapplied_changes = False
        self._placeholder = QLabel(
            I18N.tr("motion_graphic.devtools.placeholder", "After opening Developer Tools, Chromium Elements / Console / Sources will appear here."),
            self,
        )
        self._placeholder.setObjectName("mgMutedLabel")
        self._placeholder.setWordWrap(True)
        self._placeholder.setMinimumHeight(110)
        self._status = QLabel(I18N.tr("motion_graphic.devtools.not_connected", "Not connected to HTML preview"), self)
        self._status.setObjectName("mgMutedLabel")
        self._open_button = QPushButton(I18N.tr("motion_graphic.devtools.open_short", "Open"), self)
        self._open_button.setObjectName("mgDevToolsOpenButton")
        self._open_button.setToolTip(I18N.tr("motion_graphic.devtools.open", "Open Developer Tools"))
        self._open_button.clicked.connect(self.openRequested)
        self._apply_button = QPushButton(I18N.tr("motion_graphic.devtools.apply_short", "Apply"), self)
        self._apply_button.setObjectName("mgDevToolsApplyButton")
        self._apply_button.setToolTip(
            I18N.tr("motion_graphic.devtools.apply_tooltip", "Write the live DOM/CSS changed in DevTools Elements back to the HTML layer as an undoable edit")
        )
        self._apply_button.clicked.connect(self.applyDomRequested)
        self._apply_button.setEnabled(False)
        self._discard_button = QPushButton(I18N.tr("motion_graphic.devtools.discard_short", "Discard"), self)
        self._discard_button.setObjectName("mgDevToolsDiscardButton")
        self._discard_button.setToolTip(I18N.tr("motion_graphic.devtools.discard", "Discard DevTools Changes"))
        self._discard_button.clicked.connect(self.discardDomRequested)
        self._discard_button.setEnabled(False)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(4)
        for button in (self._open_button, self._apply_button, self._discard_button):
            button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        toolbar.addWidget(self._open_button)
        toolbar.addWidget(self._apply_button)
        toolbar.addWidget(self._discard_button)
        toolbar.addStretch(1)

        self._host = QWidget(self)
        host_layout = QVBoxLayout(self._host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.addWidget(self._placeholder, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addLayout(toolbar)
        layout.addWidget(self._status)
        layout.addWidget(self._host, 1)

    @property
    def devtools_widget(self) -> QWidget | None:
        return self._devtools_widget

    @property
    def has_unapplied_changes(self) -> bool:
        return self._has_unapplied_changes

    def set_has_unapplied_changes(self, changed: bool) -> None:
        changed = bool(changed) and self._devtools_widget is not None
        if changed == self._has_unapplied_changes:
            return
        self._has_unapplied_changes = changed
        self._discard_button.setEnabled(changed)
        if changed:
            self._status.setText(I18N.tr(
                "motion_graphic.devtools.unsaved", "DevTools changes are temporary — Apply them to save, or Discard to reload the saved layer."))
        elif self._devtools_widget is not None:
            self._status.setText(I18N.tr("motion_graphic.devtools.connected", "Chromium DevTools connected: inspect and edit HTML/CSS in Elements; debug JavaScript in Console/Sources."))
        self.draftChanged.emit(changed)

    def set_devtools_widget(self, widget: QWidget | None) -> None:
        if self._devtools_widget is widget:
            return
        if self._devtools_widget is not None:
            self._devtools_widget.setParent(None)
            self._devtools_widget.deleteLater()
        self._devtools_widget = widget
        if widget is None:
            self._placeholder.setVisible(True)
            self._status.setText(I18N.tr("motion_graphic.devtools.not_connected", "Not connected to HTML preview"))
            self._apply_button.setEnabled(False)
            self._discard_button.setEnabled(False)
            self.set_has_unapplied_changes(False)
            return
        self._placeholder.setVisible(False)
        self._host.layout().addWidget(widget)  # type: ignore[union-attr]
        self._status.setText(I18N.tr("motion_graphic.devtools.connected", "Chromium DevTools connected: inspect and edit HTML/CSS in Elements; debug JavaScript in Console/Sources."))
        self._apply_button.setEnabled(True)
        self._discard_button.setEnabled(False)
        self.set_has_unapplied_changes(False)

    def set_enabled_for_document(self, enabled: bool) -> None:
        self._open_button.setEnabled(bool(enabled))
        if not enabled:
            self._apply_button.setEnabled(False)
            self._discard_button.setEnabled(False)
            self.set_has_unapplied_changes(False)
            self._status.setText(I18N.tr("motion_graphic.devtools.no_html", "The current scene has no HTML layer"))


__all__ = ["HtmlDevToolsPanel"]
