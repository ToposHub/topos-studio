"""Presentation-only LUT browser and node-targeted interaction intents.

Catalog loading, thumbnail evaluation, resource import and graph commands are
host responsibilities. This widget never reads a LUT file or renders pixels.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
import math

from PyQt6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt, QSignalBlocker, pyqtSignal
from PyQt6.QtGui import QColor, QDrag, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QDoubleSpinBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QSizePolicy,
    QSlider, QSplitter, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from src.shared.assets.icon_manager import icon_folder, icon_grid, icon_list, icon_search
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C, TOOLBAR_FLAT_BTN_STYLE
from .node_resource_mime import resource_mime_data


def tr(key: str, fallback: str) -> str:
    return I18N.tr('resource_browser.' + key, fallback)


def _placeholder_icon() -> QIcon:
    """Neutral resource glyph; deliberately does not imply LUT evaluation."""
    pixmap = QPixmap(128, 80)
    pixmap.fill(QColor(C.BG_INPUT))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(C.TEXT_HINT), 1.0))
    for offset in (0, 5, 10):
        painter.drawRect(44 + offset, 16 + offset, 23, 23)
    painter.setPen(QColor(C.TEXT_HINT))
    painter.drawText(pixmap.rect().adjusted(2, 50, -2, -3),
                     Qt.AlignmentFlag.AlignCenter, tr('no_preview', 'No preview'))
    painter.end()
    return QIcon(pixmap)


def _folder_tile_icon() -> QIcon:
    pixmap = QPixmap(128, 80)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(C.TEXT_HINT), 1))
    painter.setBrush(QColor(C.BG_BUTTON_HOVER))
    painter.drawRoundedRect(17, 20, 94, 51, 3, 3)
    painter.drawRoundedRect(17, 12, 42, 17, 2, 2)
    painter.end()
    return QIcon(pixmap)


def _toolbar_icon(kind: str) -> QIcon:
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setPen(QPen(QColor(C.TEXT_LABEL), 1.4))
    if kind == 'sidebar':
        painter.drawRect(4, 5, 16, 14)
        painter.drawLine(10, 5, 10, 19)
    else:
        for y, width in ((7, 9), (11, 7), (15, 5)):
            painter.drawLine(5, y, 5 + width, y)
        painter.drawLine(17, 6, 17, 17)
        painter.drawLine(14, 14, 17, 17)
        painter.drawLine(20, 14, 17, 17)
    painter.end()
    return QIcon(pixmap)


class _LutDelegate(QStyledItemDelegate):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.grid_mode = True

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex) -> None:
        if self.grid_mode:
            data = index.data(Qt.ItemDataRole.UserRole)
            if not isinstance(data, dict):
                return
            rect = option.rect
            painter.save()
            if option.state & QStyle.StateFlag.State_Selected:
                painter.fillRect(rect, QColor(C.BG_ACTIVE))
            icon = index.data(Qt.ItemDataRole.DecorationRole)
            width = max(1, min(rect.width() - 18, option.widget.iconSize().width())) if option.widget else 96
            height = round(width * 0.625)
            preview = QRect(rect.left() + (rect.width() - width) // 2,
                            rect.top() + 4, width, height)
            if isinstance(icon, QIcon):
                icon.paint(painter, preview)
            if data.get('kind') == 'lut':
                painter.setPen(QPen(QColor(C.BORDER_MID), 1))
                painter.drawRect(preview.adjusted(0, 0, -1, -1))
            name = str(data.get('name') or '')
            label = rect.adjusted(3, height + 9, -3, -2)
            painter.setPen(QColor(C.TEXT_LABEL))
            painter.drawText(label, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                             painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, label.width()))
            painter.restore()
            return
        data = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        rect = option.rect
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, QColor(C.BG_ACTIVE))
        painter.setPen(QColor(C.PANEL_DIVIDER))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        bounds = [0, 0.43, 0.56, 0.78, 1.0]
        values = ([str(data.get('path', '')).rsplit('/', 1)[-1], 'Folder', '', '']
                  if data.get('kind') == 'folder' else [
                      str(data.get('name') or ''),
                      str(data.get('type') or Path(str(data.get('path') or data.get('original_filename') or '')).suffix.lstrip('.').upper() or 'LUT'),
                      str(data.get('category') or ''),
                      str(data.get('path') or '')])
        painter.setPen(QColor(C.TEXT_PRIMARY))
        for col, value in enumerate(values):
            left = rect.left() + round(rect.width() * bounds[col])
            right = rect.left() + round(rect.width() * bounds[col + 1])
            cell = QRect(left + 5, rect.top(), max(0, right - left - 8), rect.height())
            if col == 0:
                icon = index.data(Qt.ItemDataRole.DecorationRole)
                if isinstance(icon, QIcon):
                    icon.paint(painter, QRect(cell.left(), cell.top() + 2, 16, 16))
                    cell.setLeft(cell.left() + 22)
            painter.drawText(cell, Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(value, Qt.TextElideMode.ElideRight, cell.width()))
        painter.restore()


class _LutList(QListWidget):
    """Only serialize resource identity, never paths or thumbnail pixels."""

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        item = self.currentItem()
        if item is None:
            return
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, dict) or entry.get('kind') != 'lut':
            return
        drag = QDrag(self)
        drag.setMimeData(resource_mime_data(entry))
        drag.setPixmap(item.icon().pixmap(QSize(96, 60)))
        drag.exec(Qt.DropAction.CopyAction)


class ColorLutBrowser(QWidget):
    """Dockable LUT library shell with local search and explicit node target."""

    resource_activated = pyqtSignal(object)
    draft_edit_requested = pyqtSignal(str, object)
    import_requested = pyqtSignal(object)
    refresh_requested = pyqtSignal()
    visible_entries_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName('ColorLutBrowser')
        self.setMinimumSize(280, 260)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._entries: dict[str, dict[str, Any]] = {}
        self._favorites: set[str] = set()
        self._target_id = ''
        self._editable = False
        self._current_lut: str | None = None
        self._strength = 1.0
        self._folder = ''
        self._history: list[str] = ['']
        self._history_index = 0
        self._sort_descending = False
        self._grid_mode = True
        self._placeholder = _placeholder_icon()
        self._setup_ui()
        self.set_target('', '', False)
        self.set_current_lut(None)

    def _tool(self, text: str, tooltip: str, name: str) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName(name)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setAutoRaise(True)
        button.setFixedSize(26, 26)
        button.setStyleSheet(TOOLBAR_FLAT_BTN_STYLE)
        return button

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        toolbar = QWidget(self)
        toolbar.setObjectName('LutBrowserToolbar')
        toolbar.setFixedHeight(29)
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(5, 0, 3, 0)
        bar.setSpacing(2)
        self.folders_button = self._tool('☷', tr('show_folders', 'Show folders'), 'LutFolders')
        self.folders_button.setCheckable(True)
        self.folders_button.setChecked(True)
        bar.addWidget(self.folders_button)
        self.folders_button.setText('')
        self.folders_button.setIcon(_toolbar_icon('sidebar'))
        self.back_button = self._tool('‹', tr('back', 'Back'), 'LutBack')
        self.forward_button = self._tool('›', tr('forward', 'Forward'), 'LutForward')
        bar.addWidget(self.back_button)
        bar.addWidget(self.forward_button)
        self.path_label = QLabel(tr('luts', 'LUTs'), toolbar)
        self.path_label.setMinimumWidth(35)
        bar.addWidget(self.path_label)
        bar.addStretch(1)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.zoom_slider.setObjectName('LutThumbnailZoom')
        self.zoom_slider.setRange(70, 150)
        self.zoom_slider.setValue(96)
        self.zoom_slider.setFixedWidth(76)
        self.zoom_slider.setToolTip(tr('thumbnail_size', 'Thumbnail size'))
        self.zoom_slider.setAccessibleName(tr('thumbnail_size', 'Thumbnail size'))
        bar.addWidget(self.zoom_slider)
        self.sort_button = self._tool('⇅', tr('sort', 'Sort LUTs'), 'LutSort')
        self.sort_button.setText('')
        self.sort_button.setIcon(_toolbar_icon('sort'))
        bar.addWidget(self.sort_button)
        self.grid_button = self._tool('▦', tr('grid_view', 'Thumbnail view'), 'LutGridView')
        self.list_button = self._tool('≡', tr('list_view', 'List view'), 'LutListView')
        self.grid_button.setText('')
        self.grid_button.setIcon(icon_grid())
        self.list_button.setText('')
        self.list_button.setIcon(icon_list())
        self.view_group = QButtonGroup(self)
        for button in (self.grid_button, self.list_button):
            button.setCheckable(True)
            self.view_group.addButton(button)
            bar.addWidget(button)
        self.grid_button.setChecked(True)
        self.search_button = self._tool('⌕', tr('search_luts', 'Search LUTs'), 'LutSearchButton')
        self.search_button.setText('')
        self.search_button.setIcon(icon_search())
        self.search_button.setCheckable(True)
        bar.addWidget(self.search_button)
        self.more_button = self._tool('⋯', tr('lut_options', 'LUT options'), 'LutBrowserOptions')
        bar.addWidget(self.more_button)
        layout.addWidget(toolbar)

        search_row = QWidget(self)
        search = QHBoxLayout(search_row)
        search.setContentsMargins(7, 5, 5, 5)
        search.setSpacing(4)
        self.search_edit = QLineEdit(self)
        self.search_edit.setObjectName('LutSearch')
        self.search_edit.setPlaceholderText(tr('search_luts', 'Search LUTs'))
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setAccessibleName(tr('search_luts', 'Search LUTs'))
        search.addWidget(self.search_edit, 1)
        self.favorite_button = self._tool('☆', tr('favorites_only', 'Show favorites only'), 'LutFavorites')
        self.favorite_button.setCheckable(True)
        search.addWidget(self.favorite_button)
        self.search_row = search_row
        self.search_row.setVisible(False)
        layout.addWidget(search_row)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.folder_tree = QTreeWidget(self.splitter)
        self.folder_tree.setObjectName('LutFolderTree')
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.setMinimumWidth(70)
        self.folder_tree.setIndentation(12)
        self.folder_tree.setUniformRowHeights(True)
        self.folder_tree.setStyleSheet(f'QTreeWidget {{background:{C.BG_PANEL}; border:0;}}')
        right = QWidget(self.splitter)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self.list_header = QWidget(right)
        header_layout = QHBoxLayout(self.list_header)
        header_layout.setContentsMargins(4, 0, 4, 0)
        header_layout.setSpacing(0)
        for title, stretch in ((tr('file_name', 'File Name'), 43), (tr('type', 'Type'), 13),
                               (tr('relative_path', 'Relative Path'), 22),
                               (tr('file_path', 'File Path'), 22)):
            label = QLabel(title, self.list_header)
            label.setStyleSheet(f'color:{C.TEXT_LABEL};border-right:1px solid {C.PANEL_DIVIDER};')
            header_layout.addWidget(label, stretch)
        self.list_header.setFixedHeight(22)
        self.list_header.hide()
        right_layout.addWidget(self.list_header)
        self.list_widget = _LutList(right)
        right_layout.addWidget(self.list_widget, 1)
        self.list_widget.setObjectName('LutResourceList')
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.setDragEnabled(True)
        self.list_widget.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.list_widget.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setMovement(QListWidget.Movement.Static)
        self.list_widget.setWordWrap(True)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._delegate = _LutDelegate(self.list_widget)
        self.list_widget.setItemDelegate(self._delegate)
        self.list_widget.setStyleSheet(
            f'QListWidget {{background:{C.BG_PANEL}; border:0;}}'
            f'QListWidget::item:selected {{background:{C.BG_ACTIVE}; color:{C.TEXT_PRIMARY};}}')
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([145, 560])
        layout.addWidget(self.splitter, 1)

        self.empty_label = QLabel(tr('no_luts', 'No LUTs in this folder'), self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet(f'color: {C.TEXT_HINT}; padding: 6px;')
        layout.addWidget(self.empty_label)

        self.inspector = QWidget(self, Qt.WindowType.Popup)
        self.inspector.setObjectName('LutNodeInspector')
        inspector_layout = QVBoxLayout(self.inspector)
        inspector_layout.setContentsMargins(8, 8, 8, 8)
        inspector_layout.setSpacing(5)
        self.inspector.setMinimumWidth(320)
        self.count_label = QLabel(self)
        self.count_label.setStyleSheet(f'color: {C.TEXT_HINT}; font-size: 10px;')
        self.count_label.hide()

        target_row = QWidget(self)
        target = QVBoxLayout(target_row)
        target.setContentsMargins(8, 5, 8, 4)
        target.setSpacing(2)
        self.target_label = QLabel(self)
        self.target_label.setObjectName('LutTargetNode')
        self.target_label.setWordWrap(True)
        target.addWidget(self.target_label)
        self.current_label = QLabel(self)
        self.current_label.setObjectName('CurrentNodeLut')
        self.current_label.setWordWrap(True)
        self.current_label.setStyleSheet(f'color: {C.TEXT_HINT}; font-size: 10px;')
        target.addWidget(self.current_label)
        inspector_layout.addWidget(target_row)

        strength_row = QWidget(self)
        strength = QHBoxLayout(strength_row)
        strength.setContentsMargins(8, 0, 5, 3)
        strength.setSpacing(5)
        strength.addWidget(QLabel(tr('strength', 'Strength'), self))
        self.strength_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.strength_slider.setObjectName('NodeLutStrength')
        self.strength_slider.setRange(0, 1000)
        self.strength_slider.setAccessibleName(tr('strength', 'Strength'))
        strength.addWidget(self.strength_slider, 1)
        self.strength_spin = QDoubleSpinBox(self)
        self.strength_spin.setObjectName('NodeLutStrengthValue')
        self.strength_spin.setRange(0.0, 1.0)
        self.strength_spin.setSingleStep(0.01)
        self.strength_spin.setDecimals(2)
        self.strength_spin.setFixedWidth(58)
        strength.addWidget(self.strength_spin)
        self.reset_button = self._tool('↺', tr('reset_strength', 'Reset strength'), 'ResetNodeLutStrength')
        self.remove_button = self._tool('×', tr('remove_lut', 'Remove LUT from node'), 'RemoveNodeLut')
        strength.addWidget(self.reset_button)
        strength.addWidget(self.remove_button)
        inspector_layout.addWidget(strength_row)
        self.status_label = QLabel(tr('interaction_preview', 'Interaction preview · not applied to the image'), self)
        self._production_mode = False
        self.status_label.setObjectName('LutInteractionStatus')
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f'color: {C.TEXT_HINT}; font-size: 10px; padding: 3px 8px;')
        inspector_layout.addWidget(self.status_label)

        self.search_edit.textChanged.connect(self._populate)
        self.favorite_button.toggled.connect(self._populate)
        self.folder_tree.currentItemChanged.connect(self._folder_changed)
        self.folders_button.toggled.connect(self.folder_tree.setVisible)
        self.back_button.clicked.connect(lambda: self._navigate_history(-1))
        self.forward_button.clicked.connect(lambda: self._navigate_history(1))
        self.search_button.toggled.connect(self._toggle_search)
        self.sort_button.clicked.connect(self._toggle_sort)
        self.grid_button.clicked.connect(lambda: self._set_view_mode(True))
        self.list_button.clicked.connect(lambda: self._set_view_mode(False))
        self.zoom_slider.valueChanged.connect(self._update_item_sizes)
        self.list_widget.itemDoubleClicked.connect(self._activate)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)
        self.more_button.clicked.connect(self._show_options)
        # R04：强度手势相位——sliderPressed/Released 界定一次拖动，
        # spin 键入是离散提交（一次 command）。
        self.strength_slider.sliderPressed.connect(
            lambda: self._set_strength_phase('start'))
        self.strength_slider.valueChanged.connect(
            lambda value: self._set_strength(value / 1000.0, 'change'))
        self.strength_slider.sliderReleased.connect(
            lambda: self._set_strength_phase('commit'))
        self.strength_spin.valueChanged.connect(
            lambda value: self._set_strength(float(value), 'commit'))
        self.reset_button.clicked.connect(self._reset_strength)
        self.remove_button.clicked.connect(self._remove_lut)
        self._set_view_mode(True)

    def set_resources(self, resources: Sequence[Mapping[str, Any]]) -> None:
        """Hydrate a read-only catalog; no file scan or resource import here."""
        selected = self.selected_resource_id()
        self._entries = {}
        for raw in resources:
            entry = dict(raw)
            if entry.get('kind') != 'lut' or not isinstance(entry.get('id'), str) or not entry['id']:
                continue
            entry['name'] = str(entry.get('name') or entry['id'])
            entry['category'] = str(entry.get('category') or tr('user_luts', 'User LUTs'))
            self._entries[entry['id']] = entry
        self._rebuild_folders()
        self._navigate(self._folder, record=False)
        self._select_resource(selected)
        self._update_current_label()

    def set_production_mode(self, enabled: bool) -> None:
        """R11：真实后端模式移除"交互预览"横幅（命令已落图）。"""
        self._production_mode = bool(enabled)
        self.status_label.setText('' if enabled else tr(
            'interaction_preview', 'Interaction preview · not applied to the image'))

    def browser_state(self) -> dict:
        """R10：用户库偏好（收藏/视图模式/缩略图缩放）——用户配置域。"""
        return {"favorites": sorted(self._favorites),
                "grid": bool(getattr(self, "_grid_mode", True)),
                "zoom": int(self.zoom_slider.value())}

    def set_browser_state(self, state: object) -> None:
        if not isinstance(state, dict):
            return
        favorites = state.get("favorites")
        if isinstance(favorites, list):
            self._favorites = {str(f) for f in favorites if isinstance(f, str)}
        zoom = state.get("zoom")
        if isinstance(zoom, int) and hasattr(self, "zoom_slider"):
            self.zoom_slider.setValue(max(self.zoom_slider.minimum(),
                                          min(self.zoom_slider.maximum(), zoom)))
        grid = state.get("grid")
        if isinstance(grid, bool) and grid is not self._grid_mode:
            self._set_view_mode(grid)
        self._populate()

    def missing_favorite_ids(self, known_ids: object) -> list[str]:
        known = set(self._entries) if known_ids is None else set(known_ids)
        return sorted(f for f in self._favorites if f not in known)

    def set_target(self, node_id: str, label: str, editable: bool, reason: str = '') -> None:
        self._target_id = str(node_id or '')
        self._editable = bool(editable and self._target_id)
        self.target_label.setText((tr('target_node', 'Target node') + ': ' + (label or self._target_id))
                                  if self._target_id else tr('select_node', 'Select a corrector node'))
        self.target_label.setToolTip(reason)
        self.status_label.setText(reason or tr('interaction_preview', 'Interaction preview · not applied to the image'))
        self._update_controls()

    def visible_entry_ids(self) -> list[str]:
        """当前过滤/文件夹下可见条目 id（R07 缩略图只处理可见资源）。"""
        return [data['id'] for i in range(self.list_widget.count())
                if isinstance(data := self.list_widget.item(i).data(Qt.ItemDataRole.UserRole), dict)
                and data.get('kind') == 'lut' and isinstance(data.get('id'), str)]

    def set_thumbnail(self, entry_id: str, image: Any) -> None:
        """GUI 线程接收缩略图（QImage→QPixmap；刷新对应条目）。"""
        from PyQt6.QtGui import QPixmap
        entry = self._entries.get(entry_id)
        if entry is None or image is None:
            return
        entry["thumbnail"] = QPixmap.fromImage(image)
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict) and data.get("id") == entry_id:
                item.setIcon(QIcon(entry["thumbnail"]))
                break

    def clear_thumbnails(self) -> None:
        """Discard previews from the previous source before a new source arrives."""
        changed = False
        for entry in self._entries.values():
            if entry.pop('thumbnail', None) is not None:
                changed = True
            if entry.pop('thumbnail_error', None) is not None:
                changed = True
        if changed:
            self._populate()

    def set_thumbnail_failed(self, entry_id: str, code: str) -> None:
        """预览失败：tooltip 标注（占位图标保持，不冒充结果）。"""
        entry = self._entries.get(entry_id)
        if entry is None:
            return
        entry["thumbnail_error"] = str(code)
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict) and data.get("id") == entry_id:
                item.setToolTip(item.toolTip() + "\n" + tr(
                    "thumbnail_failed", "Preview failed") + f" ({code})")
                break

    def set_current_lut(self, resource_id: str | None, strength: float = 1.0) -> None:
        """Projection hydration never sends edit signals."""
        self._current_lut = str(resource_id) if resource_id else None
        try:
            value = float(strength)
        except (TypeError, ValueError):
            value = 1.0
        self._strength = min(1.0, max(0.0, value)) if math.isfinite(value) else 1.0
        self._sync_strength()
        self._update_current_label()
        self._update_controls()

    def selected_resource_id(self) -> str | None:
        item = self.list_widget.currentItem()
        entry = item.data(Qt.ItemDataRole.UserRole) if item else None
        return entry.get('id') if isinstance(entry, dict) and entry.get('kind') == 'lut' else None

    def _rebuild_folders(self) -> None:
        blocker = QSignalBlocker(self.folder_tree)
        self.folder_tree.clear()
        root = QTreeWidgetItem([tr('luts', 'LUTs')])
        root.setData(0, Qt.ItemDataRole.UserRole, '')
        self.folder_tree.addTopLevelItem(root)
        by_path: dict[str, QTreeWidgetItem] = {}
        for category in sorted({entry['category'] for entry in self._entries.values()}, key=str.casefold):
            parent = root
            path = ''
            for part in category.split('/'):
                path = path + '/' + part if path else part
                if path not in by_path:
                    item = QTreeWidgetItem([part])
                    item.setData(0, Qt.ItemDataRole.UserRole, path)
                    parent.addChild(item)
                    by_path[path] = item
                parent = by_path[path]
        root.setExpanded(True)
        favorites = QTreeWidgetItem(['☆ ' + tr('favorites', 'Favorites')])
        favorites.setData(0, Qt.ItemDataRole.UserRole, '__favorites__')
        self.folder_tree.addTopLevelItem(favorites)
        current = favorites if self._folder == '__favorites__' else by_path.get(self._folder, root)
        self._folder = str(current.data(0, Qt.ItemDataRole.UserRole) or '')
        self.folder_tree.setCurrentItem(current)
        del blocker

    def _folder_changed(self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        self._navigate(str(current.data(0, Qt.ItemDataRole.UserRole) or '') if current else '')

    def _navigate(self, path: str, record: bool = True) -> None:
        if path != self._folder:
            self._folder = path
            if record:
                self._history = self._history[:self._history_index + 1] + [path]
                self._history_index += 1
        self.path_label.setText(tr('favorites', 'Favorites') if path == '__favorites__'
                                else path.rsplit('/', 1)[-1] if path else tr('luts', 'LUTs'))
        self.path_label.setToolTip(path or tr('luts', 'LUTs'))
        self.back_button.setEnabled(self._history_index > 0)
        self.forward_button.setEnabled(self._history_index < len(self._history) - 1)
        self._populate()

    def _navigate_history(self, direction: int) -> None:
        index = self._history_index + direction
        if 0 <= index < len(self._history):
            self._history_index = index
            path = self._history[index]
            self._folder = path
            self._select_folder(path)
            self._navigate(path, record=False)

    def _select_folder(self, path: str) -> None:
        def walk(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            if item.data(0, Qt.ItemDataRole.UserRole) == path:
                return item
            for child_index in range(item.childCount()):
                found = walk(item.child(child_index))
                if found is not None:
                    return found
            return None
        for index in range(self.folder_tree.topLevelItemCount()):
            found = walk(self.folder_tree.topLevelItem(index))
            if found is not None:
                blocker = QSignalBlocker(self.folder_tree)
                self.folder_tree.setCurrentItem(found)
                del blocker
                break

    def _toggle_search(self, visible: bool) -> None:
        self.search_row.setVisible(visible)
        if visible:
            self.search_edit.setFocus()
        else:
            self.search_edit.clear()

    def _toggle_sort(self) -> None:
        self._sort_descending = not self._sort_descending
        self._populate()

    def _populate(self, *_args: Any) -> None:
        selected = self.selected_resource_id()
        query = self.search_edit.text().strip().casefold()
        self.list_widget.clear()
        displayed: list[dict[str, Any]] = []
        folder_paths: set[str] = set()
        if not query and not self.favorite_button.isChecked() and self._folder != '__favorites__':
            prefix = self._folder + '/' if self._folder else ''
            for entry in self._entries.values():
                category = entry['category']
                if category.startswith(prefix):
                    remainder = category[len(prefix):]
                    if remainder:
                        folder_paths.add(prefix + remainder.split('/', 1)[0])
            displayed.extend({'kind': 'folder', 'path': path, 'name': path.rsplit('/', 1)[-1]}
                             for path in folder_paths)
        for entry in self._entries.values():
            category = entry['category']
            if self._folder == '__favorites__':
                if entry['id'] not in self._favorites:
                    continue
            elif not query and not self.favorite_button.isChecked():
                if self._folder and category != self._folder:
                    continue
            elif self._folder and category != self._folder and not category.startswith(self._folder + '/'):
                continue
            if self.favorite_button.isChecked() and entry['id'] not in self._favorites:
                continue
            haystack = ' '.join((entry['name'], category, str(entry.get('description', '')))).casefold()
            if query and query not in haystack:
                continue
            displayed.append(entry)
        for entry in sorted(displayed, key=lambda value: str(value['name']).casefold(),
                            reverse=self._sort_descending):
            if entry.get('kind') == 'folder':
                item = QListWidgetItem(_folder_tile_icon() if self._grid_mode else icon_folder(), entry['name'])
                item.setData(Qt.ItemDataRole.UserRole, entry)
                item.setToolTip(entry['path'])
                self.list_widget.addItem(item)
                continue
            category = entry['category']
            preview = entry.get('thumbnail')
            icon = QIcon(preview) if isinstance(preview, (QIcon, QPixmap)) else self._placeholder
            item = QListWidgetItem(icon, entry['name'])
            item.setData(Qt.ItemDataRole.UserRole, entry)
            item.setToolTip(entry['name'] + '\n' + category + ('\n' + str(entry['description']) if entry.get('description') else ''))
            self.list_widget.addItem(item)
        count = len(self.visible_entry_ids())
        self.empty_label.setVisible(self.list_widget.count() == 0)
        self.count_label.setText(str(count) + ' ' + tr('luts', 'LUTs'))
        self._update_item_sizes()
        self._select_resource(selected)
        self.visible_entries_changed.emit()

    def _select_resource(self, resource_id: str | None) -> None:
        if not resource_id:
            return
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict) and data.get('id') == resource_id:
                self.list_widget.setCurrentItem(item)
                break

    def _set_view_mode(self, grid: bool) -> None:
        self._grid_mode = bool(grid)
        self._delegate.grid_mode = bool(grid)
        self.list_header.setVisible(not grid)
        self.grid_button.setChecked(grid)
        self.list_button.setChecked(not grid)
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode if grid else QListWidget.ViewMode.ListMode)
        self.list_widget.setSpacing(5 if grid else 0)
        self.zoom_slider.setEnabled(grid)
        self._update_item_sizes()
        self._populate()

    def _update_item_sizes(self, *_args: Any) -> None:
        width = self.zoom_slider.value() if self._grid_mode else 18
        height = round(width * 0.625) if self._grid_mode else 18
        self.list_widget.setIconSize(QSize(width, height))
        self.list_widget.setGridSize(QSize(width + 18, height + 34) if self._grid_mode else QSize())

    def _activate(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            return
        if data.get('kind') == 'folder':
            path = str(data.get('path') or '')
            self._select_folder(path)
            self._navigate(path)
        elif self._editable and data.get('kind') == 'lut':
            self.resource_activated.emit(dict(data))

    def _show_context_menu(self, position: Any) -> None:
        item = self.list_widget.itemAt(position)
        if item is None:
            return
        self.list_widget.setCurrentItem(item)
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, dict):
            return
        menu = QMenu(self)
        if entry.get('kind') == 'folder':
            menu.addAction(tr('open_folder', 'Open Folder'), lambda: self._activate(item))
            menu.exec(self.list_widget.mapToGlobal(position))
            return
        apply_action = menu.addAction(tr('apply_to_node', 'Apply to selected node'))
        apply_action.setEnabled(self._editable)
        apply_action.triggered.connect(lambda: self._activate(item))
        favorite = menu.addAction(tr('remove_favorite', 'Remove from favorites') if entry['id'] in self._favorites else tr('add_favorite', 'Add to favorites'))
        favorite.triggered.connect(lambda: self._toggle_favorite(entry['id']))
        menu.exec(self.list_widget.mapToGlobal(position))

    def _toggle_favorite(self, resource_id: str) -> None:
        if resource_id in self._favorites:
            self._favorites.remove(resource_id)
        else:
            self._favorites.add(resource_id)
        self._populate()

    def _show_options(self) -> None:
        menu = QMenu(self)
        menu.addAction(tr('import_luts', 'Import LUTs…'), self._choose_import)
        menu.addAction(tr('refresh', 'Refresh'), self.refresh_requested.emit)
        menu.addSeparator()
        menu.addAction(tr('favorites_only', 'Show favorites only'),
                       lambda: self.favorite_button.setChecked(not self.favorite_button.isChecked()))
        menu.addAction(tr('node_lut_settings', 'Current node LUT…'), self._show_inspector)
        menu.addSeparator()
        menu.addAction(tr('grid_view', 'Thumbnail view'), lambda: self._set_view_mode(True))
        menu.addAction(tr('list_view', 'List view'), lambda: self._set_view_mode(False))
        menu.exec(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))

    def _show_inspector(self) -> None:
        self.inspector.adjustSize()
        anchor = self.more_button.mapToGlobal(QPoint(self.more_button.width(), self.more_button.height()))
        self.inspector.move(anchor.x() - self.inspector.width(), anchor.y())
        self.inspector.show()

    def _choose_import(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, tr('import_luts', 'Import LUTs'), '', 'LUT (*.cube *.3dl *.look);;All files (*)')
        if paths:
            self.import_requested.emit(paths)

    def _sync_strength(self) -> None:
        slider_lock = QSignalBlocker(self.strength_slider)
        spin_lock = QSignalBlocker(self.strength_spin)
        self.strength_slider.setValue(round(self._strength * 1000.0))
        self.strength_spin.setValue(self._strength)
        del slider_lock, spin_lock

    def _set_strength_phase(self, phase: str) -> None:
        if not self._editable or self._current_lut is None:
            return
        self.draft_edit_requested.emit(
            'set_lut_strength', {'strength': self._strength, 'phase': phase})

    def _set_strength(self, value: float, phase: str = 'change') -> None:
        if not self._editable or self._current_lut is None:
            return
        self._strength = min(1.0, max(0.0, float(value)))
        self._sync_strength()
        self.draft_edit_requested.emit(
            'set_lut_strength', {'strength': self._strength, 'phase': phase})

    def _reset_strength(self) -> None:
        if not self._editable or self._current_lut is None:
            return
        self._strength = 1.0
        self._sync_strength()
        self.draft_edit_requested.emit('reset_lut_strength', {'strength': 1.0})

    def _remove_lut(self) -> None:
        if not self._editable or self._current_lut is None:
            return
        self.set_current_lut(None)
        self.draft_edit_requested.emit('remove_lut', {})

    def _update_current_label(self) -> None:
        entry = self._entries.get(self._current_lut or '', {})
        name = entry.get('name') or (tr('missing_lut', 'Missing LUT resource') if self._current_lut else tr('no_lut', 'No LUT'))
        self.current_label.setText(tr('current_lut', 'Current LUT') + ': ' + str(name))

    def _update_controls(self) -> None:
        enabled = self._editable and self._current_lut is not None
        for widget in (self.strength_slider, self.strength_spin, self.reset_button, self.remove_button):
            widget.setEnabled(enabled)
