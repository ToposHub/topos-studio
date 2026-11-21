"""Effect option icons for the application's shared dropdown widget."""
from __future__ import annotations

from collections.abc import Callable, Sequence

from PyQt6.QtCore import QModelIndex, QRectF, QSignalBlocker, QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem, QWidget

from src.shared.ui.widgets.chevron_combo import ChevronComboBox


class _GlyphMenuDelegate(QStyledItemDelegate):
    def initStyleOption(self, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().initStyleOption(option, index)
        option.text = index.data(Qt.ItemDataRole.UserRole)
        option.features |= QStyleOptionViewItem.ViewItemFeature.HasDisplay


class GlyphComboBox(ChevronComboBox):
    def __init__(
        self,
        options: Sequence[str],
        current: str,
        accent: str,
        draw_glyph: Callable[[QPainter, QRectF, str, QColor], None],
        parent: QWidget | None = None,
        *,
        icon_only: bool = False,
    ) -> None:
        super().__init__(parent)
        self._accent = QColor(accent)
        self._draw_glyph = draw_glyph
        self._active = True
        self.setIconSize(QSize(18, 14))
        if icon_only:
            self.setItemDelegate(_GlyphMenuDelegate(self))
        for name in options:
            self.addItem(self._icon(name, self._accent), "" if icon_only else name, name)
            self.setItemData(self.count() - 1, name, Qt.ItemDataRole.AccessibleTextRole)
        self.set_choice(current)
        self.currentIndexChanged.connect(self._refresh_tooltip)

    def set_choice(self, name: str) -> None:
        index = self.findData(name)
        if index >= 0:
            with QSignalBlocker(self):
                self.setCurrentIndex(index)
            self._refresh_tooltip(index)

    def set_glyph_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        color = self._accent if active else QColor("#6E6E76")
        for index in range(self.count()):
            self.setItemIcon(index, self._icon(self.itemData(index), color))

    def _refresh_tooltip(self, index: int) -> None:
        self.setToolTip(self.itemData(index) if index >= 0 else "")

    def _icon(self, name: str, color: QColor) -> QIcon:
        size = self.iconSize()
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(size * ratio)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_glyph(painter, QRectF(0.0, 0.0, size.width(), size.height()), name, color)
        painter.end()
        return QIcon(pixmap)
