"""Compact page navigation strip for the Motion Graphic editor.

Pages are first-class scene entities (``scene.pages``): the strip mirrors them
for every scene kind — native artboards as well as HTML section decks — and
stays hidden while the scene has a single page.  Page management intents
(add/remove/rename/duration/reorder) are emitted as signals; the editor window
owns the commands and dialogs.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QWidget

from src.shared.i18n.i18n import I18N
from ..core.model import ScenePage, TeachingSceneDocument
from .layer_tree_model import page_display_name


class PageNavigationBar(QWidget):
    """Page buttons mirror ``scene.pages``; clicks jump to the page start."""

    frameRequested = pyqtSignal(int)
    addRequested = pyqtSignal()
    removeRequested = pyqtSignal(str)
    renameRequested = pyqtSignal(str)
    durationRequested = pyqtSignal(str)
    reorderRequested = pyqtSignal(str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene: TeachingSceneDocument | None = None
        self._pages: tuple[ScenePage, ...] = ()
        self._buttons: list[QToolButton] = []
        # AUD-31002: no state lock here — the buttons are connected to
        # ``clicked``, which programmatic ``setChecked`` never emits, so
        # ``set_frame`` cannot echo back. A write-only flag would only
        # mislead maintainers into thinking a guard exists (e.g. when
        # switching the connection to ``toggled``).
        self.setObjectName("mgPageStrip")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(6, 1, 6, 1)
        self._layout.setSpacing(3)
        self._title = QLabel(I18N.tr("motion_graphic.sections.page", "Page"), self)
        self._title.setObjectName("mgSectionTitle")
        self._layout.addWidget(self._title)
        self._pages_layout = QHBoxLayout()
        self._pages_layout.setContentsMargins(0, 0, 0, 0)
        self._pages_layout.setSpacing(3)
        self._layout.addLayout(self._pages_layout)
        self._add_button = QToolButton(self)
        self._add_button.setObjectName("mgPageAddButton")
        self._add_button.setText("+")
        self._add_button.setToolTip(I18N.tr("motion_graphic.pages.add", "Add Page"))
        self._add_button.clicked.connect(lambda: self.addRequested.emit())
        self._layout.addWidget(self._add_button)
        self._layout.addStretch(1)
        self.setFixedHeight(28)
        self.setVisible(False)

    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def transport_layout(self) -> QHBoxLayout:
        """Right-aligned host for the editor's shared playback controls."""
        return self._layout

    def set_scene(self, scene: TeachingSceneDocument, frame: int = 0) -> None:
        if self._scene is scene and self._pages == scene.pages:
            self.set_frame(frame)
            return
        self._scene = scene
        self._pages = scene.pages
        while self._buttons:
            button = self._buttons.pop()
            self._pages_layout.removeWidget(button)
            button.deleteLater()
        multi = len(self._pages) > 1
        self.setVisible(multi)
        for index, page in enumerate(self._pages):
            button = QToolButton(self)
            button.setObjectName("mgPageButton")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            label = page_display_name(page, index)
            button.setText(label)
            seconds = page.duration_frames / max(0.001, scene.fps)
            button.setToolTip(f"{label} · {seconds:g} s")
            button.clicked.connect(lambda _checked=False, i=index: self._request_page(i))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda _pos, b=button, p=page.page_id, i=index: self._show_menu(b, p, i))
            self._buttons.append(button)
            self._pages_layout.addWidget(button)
        self.set_frame(frame)

    def set_frame(self, frame: int) -> None:
        if self._scene is None or not self._buttons:
            return
        # AUD-31002: no recursion guard needed — see the __init__ note
        # (setChecked does not emit clicked).
        index = self._scene.page_index_for_frame(frame)
        for button_index, button in enumerate(self._buttons):
            button.setChecked(button_index == index)

    def request_relative_page(self, current_frame: int, step: int) -> None:
        """Move to the adjacent page, clamping cleanly at either end."""
        if self._scene is None or len(self._pages) < 2 or step == 0:
            return
        current = self._scene.page_index_for_frame(current_frame)
        target = max(0, min(len(self._pages) - 1, current + (1 if step > 0 else -1)))
        if target != current:
            self._request_page(target)

    def _request_page(self, index: int) -> None:
        if self._scene is None:
            return
        starts = self._scene.page_start_frames()
        frame = starts[max(0, min(index, len(starts) - 1))]
        self.frameRequested.emit(max(0, min(self._scene.duration_frames - 1, frame)))

    def _show_menu(self, button: QToolButton, page_id: str, index: int) -> None:
        menu = QMenu(self)
        menu.addAction(I18N.tr("motion_graphic.pages.rename", "Rename Page…"),
                       lambda: self.renameRequested.emit(page_id))
        menu.addAction(I18N.tr("motion_graphic.pages.duration", "Set Page Duration…"),
                       lambda: self.durationRequested.emit(page_id))
        if index > 0:
            menu.addAction(I18N.tr("motion_graphic.pages.move_left", "Move Left"),
                           lambda: self.reorderRequested.emit(page_id, index - 1))
        if index < len(self._pages) - 1:
            menu.addAction(I18N.tr("motion_graphic.pages.move_right", "Move Right"),
                           lambda: self.reorderRequested.emit(page_id, index + 1))
        menu.addSeparator()
        menu.addAction(I18N.tr("motion_graphic.pages.remove", "Remove Page"),
                       lambda: self.removeRequested.emit(page_id))
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))


__all__ = ["PageNavigationBar"]
