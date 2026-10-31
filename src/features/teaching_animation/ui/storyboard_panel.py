"""Scene list/storyboard panel."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

from src.shared.i18n.i18n import I18N
from ..core.model import TeachingSceneDocument


class StoryboardPanel(QWidget):
    sceneSelected = pyqtSignal(str)
    createRequested = pyqtSignal()
    duplicateRequested = pyqtSignal(str)
    deleteRequested = pyqtSignal(str)
    reorderRequested = pyqtSignal(str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.list_widget = QListWidget(self)
        self.list_widget.currentItemChanged.connect(self._selection_changed)
        self.list_widget.model().rowsMoved.connect(self._rows_moved)
        self.list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.add_button = QPushButton(I18N.tr("motion_graphic.storyboard.new", "New Scene"), self)
        self.copy_button = QPushButton(I18N.tr("motion_graphic.storyboard.duplicate", "Duplicate Scene"), self)
        self.delete_button = QPushButton(I18N.tr("motion_graphic.storyboard.delete", "Delete Scene"), self)
        self.add_button.clicked.connect(self.createRequested)
        self.copy_button.clicked.connect(self._duplicate_current)
        self.delete_button.clicked.connect(self._delete_current)
        buttons = QVBoxLayout()
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.delete_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.list_widget)
        layout.addLayout(buttons)

    def set_scenes(self, scenes: tuple[TeachingSceneDocument, ...] | list[TeachingSceneDocument],
                   selected_scene_id: str | None = None) -> None:
        selected = selected_scene_id or self.current_scene_id
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for scene in scenes:
            item = QListWidgetItem(scene.name)
            item.setData(256, scene.scene_id)
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)
        if selected:
            self.select_scene(selected)
        elif self.list_widget.count():
            self.list_widget.setCurrentRow(0)

    @property
    def current_scene_id(self) -> str | None:
        item = self.list_widget.currentItem()
        return str(item.data(256)) if item is not None else None

    def select_scene(self, scene_id: str) -> None:
        for index in range(self.list_widget.count()):
            if self.list_widget.item(index).data(256) == scene_id:
                self.list_widget.setCurrentRow(index)
                return

    def _selection_changed(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is not None:
            self.sceneSelected.emit(str(current.data(256)))

    def _duplicate_current(self) -> None:
        if self.current_scene_id:
            self.duplicateRequested.emit(self.current_scene_id)

    def _delete_current(self) -> None:
        if self.current_scene_id:
            self.deleteRequested.emit(self.current_scene_id)

    def _rows_moved(self, _parent: object, start: int, end: int, _destination: object, row: int) -> None:
        if start != end:
            return
        # QListWidget has already applied the move when rowsMoved fires.  Use
        # the current item/row rather than Qt's destination-row convention,
        # which differs for downward moves.
        current = self.list_widget.currentItem()
        current_row = self.list_widget.currentRow()
        if current is not None and current_row >= 0:
            self.reorderRequested.emit(str(current.data(256)), current_row)
