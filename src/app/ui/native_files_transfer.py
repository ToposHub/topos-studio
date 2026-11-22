"""System file URLs and drop targets for the native Files workspace."""
from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from uuid import uuid4

from PyQt6.QtCore import QEvent, QMimeData, QModelIndex, QObject, QPersistentModelIndex, QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import QDropEvent
from PyQt6.QtWidgets import QAbstractItemView, QFrame, QLabel, QTreeView

from src.shared.i18n import I18N
from src.shared.ui.style_sheet import C

_CUT_FORMAT = "application/x-topos-files-cut"
_CUT_SESSION = uuid4().bytes
DRAG_SOURCE_PROPERTY = "toposFilesDragSource"


def local_file_paths(mime: QMimeData | None) -> list[str]:
    if mime is None:
        return []
    paths = (url.toLocalFile() for url in mime.urls() if url.isLocalFile())
    return list(dict.fromkeys(path for path in paths if "\0" not in path and Path(path).is_absolute()))


def mark_cut(mime: QMimeData, token: str = "") -> None:
    mime.setData(_CUT_FORMAT, bytes.fromhex(token) if token else _CUT_SESSION + uuid4().bytes)


def cut_token(mime: QMimeData | None) -> str:
    raw = bytes(mime.data(_CUT_FORMAT)) if mime is not None else b""
    return raw.hex() if len(raw) == 32 and raw.startswith(_CUT_SESSION) else ""


class FileDropTarget(QObject):
    """Handle file and reference drops without dropping into Qt's model."""

    def __init__(
        self, view: QAbstractItemView, *,
        destination_at: Callable[[QPoint], tuple[str, QRect] | None],
        transfer: Callable[[str, list[str], str], None], expand_folders: bool = False,
        reference_at: Callable[[QPoint, list[str]], tuple[QRect, Callable[[], None] | None] | None] | None = None,
    ) -> None:
        super().__init__(view)
        self._view = view
        self._destination_at = destination_at
        self._transfer = transfer
        self._reference_at = reference_at
        self._tree = view if expand_folders and isinstance(view, QTreeView) else None
        self._hovered = QPersistentModelIndex()
        self._expand_timer = QTimer(self, interval=550, singleShot=True)
        self._expand_timer.timeout.connect(self._expand_folder)
        viewport = view.viewport()
        self._outline = QFrame(viewport)
        self._outline.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._outline.setStyleSheet(
            f"background: transparent; border: 2px solid {C.ORANGE}; border-radius: 4px;")
        self._move_label = QLabel(I18N.tr("files.native.drop_move", "移动"), self._outline)
        self._move_label.setStyleSheet(
            f"background: {C.ORANGE}; border: 0; color: {C.BG_DEEPEST}; padding: 2px 6px;")
        self._move_label.adjustSize()
        self._outline.hide()
        view.setAcceptDrops(True)
        viewport.setAcceptDrops(True)
        viewport.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind == QEvent.Type.DragLeave:
            self._clear_hover()
            return True
        if kind in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.Drop):
            self._drop_event(event)
            return True
        return super().eventFilter(watched, event)

    def _drop_event(self, event: QDropEvent) -> None:
        paths = local_file_paths(event.mimeData())
        actions = event.possibleActions()
        if not paths or not actions & Qt.DropAction.CopyAction:
            self._clear_hover()
            event.ignore()
            return
        source = event.source()
        moving = (source is not None and source.property(DRAG_SOURCE_PROPERTY)
                  and event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        position = event.position().toPoint()
        target = self._destination_at(position)
        reference = self._reference_at(position, paths) if target is None and self._reference_at is not None else None
        if target is not None:
            destination, rect = target
            on_drop = partial(self._transfer, "move" if moving else "copy", paths, destination)
        elif reference is not None:
            rect, on_drop = reference
            moving = False
        else:
            self._clear_hover()
            if event.type() == QEvent.Type.DragEnter:
                event.setDropAction(Qt.DropAction.CopyAction)
                event.accept()
            else:
                event.ignore()
            return
        # The native drag always advertises copying. Topos alone handles a
        # Shift move when both ends of the drag belong to the workspace.
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        if event.type() == QEvent.Type.Drop:
            self._clear_hover()
            if on_drop is None:
                event.ignore()
            else:
                on_drop()
        else:
            self._outline.setGeometry(rect.intersected(self._view.viewport().rect()))
            self._move_label.move(max(2, self._outline.width() - self._move_label.width() - 3), 2)
            self._move_label.setVisible(bool(moving))
            self._outline.show()
            self._outline.raise_()
            if self._tree is not None:
                index = self._tree.indexAt(position)
                if index != self._hovered:
                    self._hovered = QPersistentModelIndex(index)
                    self._expand_timer.start()

    def _expand_folder(self) -> None:
        if self._tree is not None and self._hovered.isValid():
            self._tree.expand(QModelIndex(self._hovered))

    def _clear_hover(self) -> None:
        self._outline.hide()
        self._expand_timer.stop()
        self._hovered = QPersistentModelIndex()
