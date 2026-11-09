"""Gallery comparison setup UI; rendering remains owned by the monitor host."""
from __future__ import annotations

import os
from PyQt6.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QAbstractItemView, QTabBar, QVBoxLayout, QWidget,
)
from src.shared.i18n.i18n import I18N


class ComparisonLayoutPreview(QWidget):
    """A setup diagram, never a substitute for a color-managed monitor frame."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedHeight(126)
        self.layout_name = "side_by_side"
        self.references: list[tuple[str, QIcon]] = []

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#191919"))
        columns = 2
        rows = 2 if self.layout_name == "grid_2x2" else 1
        gap = 4
        width = (self.width() - (columns + 1) * gap) // columns
        height = (self.height() - (rows + 1) * gap) // rows
        cells = [(I18N.tr("gallery.current_image", "Current Image"), QIcon()), *self.references]
        for index in range(columns * rows):
            rect = QRect(gap + (index % columns) * (width + gap),
                         gap + (index // columns) * (height + gap), width, height)
            painter.fillRect(rect, QColor("#242429"))
            painter.setPen(QColor("#424248"))
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            if index >= len(cells):
                continue
            label, icon = cells[index]
            if not icon.isNull():
                icon.paint(painter, rect.adjusted(2, 2, -2, -18))
            painter.setPen(QColor("#ccccd2"))
            painter.drawText(rect if index == 0 else QRect(rect.left(), rect.bottom() - 16, rect.width(), 16),
                             Qt.AlignmentFlag.AlignCenter, label)
        painter.end()


class GalleryCompareDialog(QDialog):
    """Select existing Gallery references without pretending a grid renderer exists."""

    wipe_requested = pyqtSignal(str)
    split_requested = pyqtSignal(object)

    def __init__(self, entries: list[tuple[str, str]], selected: list[str],
                 mode: str = "wipe", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(I18N.tr("gallery.compare_title", "Gallery Comparison"))
        self.resize(540, 390)
        self.setMinimumSize(360, 300)
        self.setStyleSheet("""
            QDialog { background: #28282e; color: #d7d7dc; }
            QLabel { color: #bfbfc6; background: transparent; font-size: 11px; }
            QListWidget { background: #202024; border: 1px solid #151518; }
            QListWidget::item:selected { background: #444047; border: 1px solid #e86351; }
            QTabBar::tab { background: #202024; color: #94949d; border: 0;
                border-bottom: 1px solid #111114; padding: 8px 22px; border-radius: 0; }
            QTabBar::tab:selected { color: #eeeef0; background: #29292e;
                border-bottom: 2px solid #e86351; }
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 12)
        self.tabs = QTabBar(self)
        self.tabs.addTab(I18N.tr("gallery.image_wipe", "Image Wipe"))
        self.tabs.addTab(I18N.tr("gallery.split_screen", "Split Screen"))
        root.addWidget(self.tabs)
        self.description = QLabel(self)
        self.description.setWordWrap(True)
        root.addWidget(self.description)
        self.preview = ComparisonLayoutPreview(self)
        root.addWidget(self.preview)
        self.references = QListWidget(self)
        self.references.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.references.setIconSize(QSize(96, 54))
        for label, path in entries:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, path)
            thumbnail = os.path.splitext(path)[0] + ".thumb.jpg"
            pixmap = QPixmap(thumbnail)
            if not pixmap.isNull():
                item.setIcon(QIcon(pixmap))
            self.references.addItem(item)
            item.setSelected(path in selected)
        root.addWidget(self.references, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel(I18N.tr("gallery.layout", "Layout"), self))
        self.layout_choice = QComboBox(self)
        self.layout_choice.addItem(I18N.tr("gallery.side_by_side", "Side by Side"), "side_by_side")
        self.layout_choice.addItem(I18N.tr("gallery.grid_2x2", "2 × 2 Grid"), "grid_2x2")
        row.addWidget(self.layout_choice, 1)
        self.layout_choice.currentIndexChanged.connect(lambda _index: self._update_state())
        root.addLayout(row)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok, self)
        self.buttons.rejected.connect(self.reject)
        self.buttons.accepted.connect(self._apply)
        root.addWidget(self.buttons)
        self.tabs.currentChanged.connect(self._update_mode)
        self.references.itemSelectionChanged.connect(self._update_state)
        self.tabs.setCurrentIndex(1 if mode == "split" else 0)
        if mode == "split" and len(selected) > 1:
            self.layout_choice.setCurrentIndex(1)
        self._update_mode()
        # Restore multi-selection after setting the selection policy.
        for i in range(self.references.count()):
            item = self.references.item(i)
            item.setSelected(item.data(Qt.ItemDataRole.UserRole) in selected)
        self._update_state()

    def _update_mode(self, *_args: object) -> None:
        split = self.tabs.currentIndex() == 1
        self.setMinimumHeight(500 if split else 300)
        self.references.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection if split else QAbstractItemView.SelectionMode.SingleSelection)
        self.layout_choice.setEnabled(split)
        self.preview.setVisible(split)
        self.description.setText(I18N.tr("gallery.compare_split_description", "Compare the current image with selected Gallery stills (up to three references).") if split else I18N.tr("gallery.compare_wipe_description", "Choose a Gallery still as the viewer's Image Wipe reference."))
        self._update_state()

    def _update_state(self) -> None:
        split = self.tabs.currentIndex() == 1
        selected = self.references.selectedItems()
        apply = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        apply.setText(I18N.tr("gallery.compare_start", "Start Comparison") if split else I18N.tr("gallery.use_reference", "Use as Wipe Reference"))
        # Grid setup is reviewable; committing it awaits the real presenter.
        apply.setEnabled(not split and len(selected) == 1)
        self.status.setText(I18N.tr("gallery.compare_backend_pending", "Split Screen rendering is not connected yet. Your layout and reference selection can be reviewed here.") if split else "")
        if split and len(selected) > 3:
            self.status.setText(I18N.tr("gallery.compare_limit", "Select at most three references alongside the current image."))
        elif split and len(selected) > 1 and self.layout_choice.currentData() == "side_by_side":
            self.status.setText(I18N.tr("gallery.compare_side_limit", "Side by Side uses one reference. Choose 2 × 2 Grid for multiple stills."))
        self._update_preview()

    def _update_preview(self, *_args: object) -> None:
        self.preview.layout_name = self.layout_choice.currentData()
        self.preview.references = [(item.text(), item.icon())
                                   for item in self.references.selectedItems()[:3]]
        self.preview.update()

    def _apply(self) -> None:
        selected = self.references.selectedItems()
        if self.tabs.currentIndex() == 0 and len(selected) == 1:
            self.wipe_requested.emit(selected[0].data(Qt.ItemDataRole.UserRole))
            self.accept()
