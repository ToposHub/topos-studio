"""Presentation-only LUT browser and node-targeted interaction intents.

Catalog loading, thumbnail evaluation, resource import and graph commands are
host responsibilities. This widget never reads a LUT file or renders pixels.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
import math

from PyQt6.QtCore import QSize, Qt, QSignalBlocker, pyqtSignal
from PyQt6.QtGui import QColor, QDrag, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QDoubleSpinBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QSizePolicy,
    QSlider, QSplitter, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from src.shared.assets.icon_manager import icon_folder, icon_grid, icon_list
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


class _LutList(QListWidget):
    """Only serialize resource identity, never paths or thumbnail pixels."""

    def startDrag(self, supported_actions: Qt.DropAction) -> None:
        item = self.currentItem()
        if item is None:
            return
        entry = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(entry, dict):
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
        self.folders_button.setIcon(icon_folder())
        title = QLabel(tr('luts', 'LUTs'), toolbar)
        bar.addWidget(title)
        bar.addStretch(1)
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
        layout.addWidget(search_row)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.folder_tree = QTreeWidget(self.splitter)
        self.folder_tree.setObjectName('LutFolderTree')
        self.folder_tree.setHeaderHidden(True)
        self.folder_tree.setMinimumWidth(70)
        self.folder_tree.setIndentation(12)
        self.folder_tree.setUniformRowHeights(True)
        self.list_widget = _LutList(self.splitter)
        self.list_widget.setObjectName('LutResourceList')
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.setDragEnabled(True)
        self.list_widget.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.list_widget.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list_widget.setMovement(QListWidget.Movement.Static)
        self.list_widget.setWordWrap(True)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([92, 268])
        layout.addWidget(self.splitter, 1)

        self.empty_label = QLabel(tr('no_luts', 'No LUTs in this folder'), self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet(f'color: {C.TEXT_HINT}; padding: 6px;')
        layout.addWidget(self.empty_label)

        zoom_row = QWidget(self)
        zoom = QHBoxLayout(zoom_row)
        zoom.setContentsMargins(8, 1, 8, 3)
        zoom.setSpacing(5)
        self.count_label = QLabel(self)
        self.count_label.setStyleSheet(f'color: {C.TEXT_HINT}; font-size: 10px;')
        zoom.addWidget(self.count_label, 1)
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.zoom_slider.setObjectName('LutThumbnailZoom')
        self.zoom_slider.setRange(70, 150)
        self.zoom_slider.setValue(96)
        self.zoom_slider.setFixedWidth(100)
        self.zoom_slider.setToolTip(tr('thumbnail_size', 'Thumbnail size'))
        self.zoom_slider.setAccessibleName(tr('thumbnail_size', 'Thumbnail size'))
        zoom.addWidget(self.zoom_slider)
        layout.addWidget(zoom_row)

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
        layout.addWidget(target_row)

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
        layout.addWidget(strength_row)
        self.status_label = QLabel(tr('interaction_preview', 'Interaction preview · not applied to the image'), self)
        self.status_label.setObjectName('LutInteractionStatus')
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f'color: {C.TEXT_HINT}; font-size: 10px; padding: 3px 8px;')
        layout.addWidget(self.status_label)

        self.search_edit.textChanged.connect(self._populate)
        self.favorite_button.toggled.connect(self._populate)
        self.folder_tree.currentItemChanged.connect(self._folder_changed)
        self.folders_button.toggled.connect(self.folder_tree.setVisible)
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
        self._populate()
        self._select_resource(selected)
        self._update_current_label()

    def set_target(self, node_id: str, label: str, editable: bool, reason: str = '') -> None:
        self._target_id = str(node_id or '')
        self._editable = bool(editable and self._target_id)
        self.target_label.setText((tr('target_node', 'Target node') + ': ' + (label or self._target_id))
                                  if self._target_id else tr('select_node', 'Select a corrector node'))
        self.target_label.setToolTip(reason)
        self.status_label.setText(reason or tr('interaction_preview', 'Interaction preview · not applied to the image'))
        self._update_controls()

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
        return entry.get('id') if isinstance(entry, dict) else None

    def _rebuild_folders(self) -> None:
        blocker = QSignalBlocker(self.folder_tree)
        self.folder_tree.clear()
        root = QTreeWidgetItem([tr('all_luts', 'All LUTs')])
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
        current = by_path.get(self._folder, root)
        self._folder = str(current.data(0, Qt.ItemDataRole.UserRole) or '')
        self.folder_tree.setCurrentItem(current)
        del blocker

    def _folder_changed(self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        self._folder = str(current.data(0, Qt.ItemDataRole.UserRole) or '') if current else ''
        self._populate()

    def _populate(self, *_args: Any) -> None:
        selected = self.selected_resource_id()
        query = self.search_edit.text().strip().casefold()
        self.list_widget.clear()
        for entry in sorted(self._entries.values(), key=lambda value: value['name'].casefold()):
            category = entry['category']
            if self._folder and category != self._folder and not category.startswith(self._folder + '/'):
                continue
            if self.favorite_button.isChecked() and entry['id'] not in self._favorites:
                continue
            haystack = ' '.join((entry['name'], category, str(entry.get('description', '')))).casefold()
            if query and query not in haystack:
                continue
            preview = entry.get('thumbnail')
            icon = QIcon(preview) if isinstance(preview, (QIcon, QPixmap)) else self._placeholder
            item = QListWidgetItem(icon, ('★ ' if entry['id'] in self._favorites else '') + entry['name'])
            item.setData(Qt.ItemDataRole.UserRole, entry)
            item.setToolTip(entry['name'] + '\n' + category + ('\n' + str(entry['description']) if entry.get('description') else ''))
            self.list_widget.addItem(item)
        count = self.list_widget.count()
        self.empty_label.setVisible(count == 0)
        self.count_label.setText(str(count) + ' ' + tr('luts', 'LUTs'))
        self._update_item_sizes()
        self._select_resource(selected)

    def _select_resource(self, resource_id: str | None) -> None:
        if not resource_id:
            return
        for index in range(self.list_widget.count()):
            item = self.list_widget.item(index)
            if item.data(Qt.ItemDataRole.UserRole)['id'] == resource_id:
                self.list_widget.setCurrentItem(item)
                break

    def _set_view_mode(self, grid: bool) -> None:
        self._grid_mode = bool(grid)
        self.grid_button.setChecked(grid)
        self.list_button.setChecked(not grid)
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode if grid else QListWidget.ViewMode.ListMode)
        self.list_widget.setSpacing(5 if grid else 1)
        self.zoom_slider.setEnabled(grid)
        self._update_item_sizes()

    def _update_item_sizes(self, *_args: Any) -> None:
        width = self.zoom_slider.value() if self._grid_mode else 22
        height = round(width * 0.625) if self._grid_mode else 18
        self.list_widget.setIconSize(QSize(width, height))
        self.list_widget.setGridSize(QSize(width + 14, height + 34) if self._grid_mode else QSize())

    def _activate(self, item: QListWidgetItem) -> None:
        if self._editable:
            self.resource_activated.emit(dict(item.data(Qt.ItemDataRole.UserRole)))

    def _show_context_menu(self, position: Any) -> None:
        item = self.list_widget.itemAt(position)
        if item is None:
            return
        self.list_widget.setCurrentItem(item)
        entry = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
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
        menu.addAction(tr('grid_view', 'Thumbnail view'), lambda: self._set_view_mode(True))
        menu.addAction(tr('list_view', 'List view'), lambda: self._set_view_mode(False))
        menu.exec(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))

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
