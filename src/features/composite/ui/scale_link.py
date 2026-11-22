"""Shared scale-link controls for the two editors of one composition."""

from __future__ import annotations

from weakref import WeakKeyDictionary

from PyQt6 import sip
from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QToolButton

from src.shared.assets.icon_manager import make_link_button
from src.shared.i18n.i18n import I18N


class ScaleLinkState(QObject):
    edit_started = pyqtSignal(str)
    value_changed = pyqtSignal(str, float)
    edit_finished = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._unlinked: set[str] = set()
        self._buttons: WeakKeyDictionary[QToolButton, str] = WeakKeyDictionary()

    def is_linked(self, node_id: str) -> bool:
        return node_id not in self._unlinked

    def make_button(self, node_id: str) -> QToolButton:
        label = I18N.tr("keyframes.link_scale_axes", "Link X and Y scale")
        button = make_link_button(self.is_linked(node_id), label)
        button.setAccessibleName(label)
        self._buttons[button] = node_id
        button.toggled.connect(lambda linked: self._set_linked(node_id, linked))
        return button

    def _set_linked(self, node_id: str, linked: bool) -> None:
        if self.is_linked(node_id) == linked:
            return
        if linked:
            self._unlinked.discard(node_id)
        else:
            self._unlinked.add(node_id)
        for button, owner in list(self._buttons.items()):
            if sip.isdeleted(button):
                self._buttons.pop(button, None)
            elif owner == node_id:
                button.setChecked(linked)
