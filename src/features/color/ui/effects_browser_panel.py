"""Effects library and per-node interaction drafts; no rendering/project writes.

The host injects resource metadata and instance snapshots. Every editable action
is an intent, so this panel can be used before command/render integration exists.
"""
from __future__ import annotations

from copy import deepcopy
from functools import cache
import math
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QModelIndex, QMimeData, QRect, QSignalBlocker, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QDrag, QIcon, QPaintEvent, QPainter, QPen, QResizeEvent
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListView, QListWidget, QListWidgetItem, QMenu, QPushButton,
    QScrollArea, QSizePolicy, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QTabWidget, QToolButton, QVBoxLayout, QWidget,
)

from src.features.color.ui.node_resource_mime import resource_mime_data
from src.shared.assets.icon_manager import get_icon, icon_grid, icon_list, icon_search
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import C, TOOLBAR_FLAT_BTN_STYLE
from src.shared.ui.widgets.color_picker_control import ColorPickerControl, qcolor_to_linear_rgba
from src.shared.ui.widgets.param_slider_row import ParamSliderRow
from src.shared.ui.widgets.toggle_switch import ToggleSwitch


_EFFECT_ICON_DIR = Path(__file__).with_name('effect_icons')


def _tr(key: str, fallback: str) -> str:
    return I18N.tr('resource_browser.' + key, fallback)


def _number(value: Any, fallback: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


@cache
def _effect_icon_paths() -> dict[str, Path]:
    return {path.stem: path for path in _EFFECT_ICON_DIR.glob('*.svg')}


def _effect_icon(resource_id: str) -> QIcon:
    """Match the catalog's stable ID independently of the display language."""
    plugin_id = resource_id.partition('@')[0]
    path = _effect_icon_paths().get(plugin_id, _EFFECT_ICON_DIR / 'generic.svg')
    return QIcon(str(path))


class _EffectsTabs(QTabWidget):
    """Draw one continuous header behind the tabs and corner actions."""

    HEADER_HEIGHT = 36

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setDocumentMode(True)
        self.tabBar().setDrawBase(False)
        self.tabBar().setFixedHeight(self.HEADER_HEIGHT)
        self.tabBar().setExpanding(False)
        self.setStyleSheet(f"""
            QTabWidget::pane {{ border: 0; }}
            QTabBar {{ background: transparent; border: 0; }}
            QTabBar::tab {{
                background: transparent; color: {C.TEXT_LABEL};
                border: 0; border-bottom: 3px solid transparent;
                border-radius: 0; margin: 0; padding: 0 15px;
                height: {self.HEADER_HEIGHT - 3}px; font-size: 12px;
            }}
            QTabBar::tab:first, QTabBar::tab:last, QTabBar::tab:only-one {{
                border-radius: 0;
            }}
            QTabBar::tab:hover {{ color: {C.TEXT_PRIMARY}; background: {C.BG_ACTIVE}; }}
            QTabBar::tab:selected {{
                color: {C.TEXT_PRIMARY}; font-weight: bold;
                background: transparent; border-bottom: 3px solid {C.ACCENT_SURFACE};
            }}
            QTabBar::tab:disabled {{ color: {C.TEXT_HINT}; }}
        """)
        self._header_rule = QFrame(self)
        self._header_rule.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._header_rule.setStyleSheet(f'background: {C.PANEL_DIVIDER}; border: 0;')

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._header_rule.setGeometry(0, self.HEADER_HEIGHT - 1, self.width(), 1)
        self._header_rule.raise_()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.fillRect(0, 0, self.width(), self.HEADER_HEIGHT - 1, QColor(C.BG_INPUT))
        painter.end()


class _ResourceList(QListWidget):
    def mimeData(self, items: list[QListWidgetItem]) -> QMimeData:
        if items:
            entry = items[0].data(Qt.ItemDataRole.UserRole)
            if isinstance(entry, dict) and entry.get('available', True):
                return resource_mime_data(entry)
        return QMimeData()

    def startDrag(self, supportedActions: Qt.DropAction) -> None:
        item = self.currentItem()
        if item is None or not isinstance(item.data(Qt.ItemDataRole.UserRole), dict) or not item.data(Qt.ItemDataRole.UserRole).get('available', True):
            return
        drag = QDrag(self)
        drag.setMimeData(self.mimeData([item]))
        drag.setPixmap(item.icon().pixmap(36, 30))
        drag.exec(Qt.DropAction.CopyAction)


class _ResolveEffectDelegate(QStyledItemDelegate):
    """Compact Resolve-style group headers and outlined effect rows."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem,
              index: QModelIndex) -> None:
        category = index.data(Qt.ItemDataRole.UserRole + 1)
        painter.save()
        rect = option.rect
        if isinstance(category, str):
            painter.setPen(QColor(C.PANEL_DIVIDER))
            painter.drawLine(rect.left() + 4, rect.bottom(), rect.right() - 4, rect.bottom())
            painter.setPen(QColor(C.TEXT_PRIMARY))
            font = painter.font()
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(rect.adjusted(5, 0, -22, 0),
                             Qt.AlignmentFlag.AlignVCenter, category)
            collapsed = bool(index.data(Qt.ItemDataRole.UserRole + 2))
            painter.setPen(QColor(C.TEXT_LABEL))
            painter.drawText(rect.adjusted(0, 0, -8, 0),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                             '⌄' if collapsed else '⌃')
        else:
            row = rect.adjusted(5, 2, -6, -2)
            entry = index.data(Qt.ItemDataRole.UserRole)
            available = isinstance(entry, dict) and entry.get('available', True)
            selected = bool(option.state & QStyle.StateFlag.State_Selected)
            painter.setPen(QPen(QColor(C.TEXT_LABEL if selected else C.PANEL_DIVIDER), 1))
            painter.setBrush(QColor(C.BG_ACTIVE if selected else C.BG_PANEL))
            painter.drawRoundedRect(row, 2, 2)
            well = QRect(row.left(), row.top(), 43, row.height())
            painter.setPen(QColor(C.PANEL_DIVIDER))
            painter.drawLine(well.right(), well.top(), well.right(), well.bottom())
            icon = index.data(Qt.ItemDataRole.DecorationRole)
            if isinstance(icon, QIcon):
                icon.paint(painter, well.adjusted(8, 3, -8, -3), mode=(
                    QIcon.Mode.Normal if available else QIcon.Mode.Disabled))
            painter.setPen(QColor(C.TEXT_PRIMARY if available else C.TEXT_HINT))
            painter.drawText(row.adjusted(53, 0, -5, 0),
                             Qt.AlignmentFlag.AlignVCenter,
                             str(index.data(Qt.ItemDataRole.DisplayRole) or ''))
        painter.restore()


class ColorEffectsBrowser(QWidget):
    """Floating-friendly Effects Library / Settings with UI-only node drafts."""
    resource_activated = pyqtSignal(object)
    draft_edit_requested = pyqtSignal(str, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._resources: list[dict[str, Any]] = []
        self._instances: list[dict[str, Any]] = []
        self._favorites: set[str] = set()
        self._collapsed: set[str] = set()
        self._target_id = ''
        self._editable = False
        self._syncing = False
        self._grid = False
        self.setMinimumWidth(220)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.tabs = _EffectsTabs()
        corner = QWidget()
        corner.setObjectName('EffectsHeaderActions')
        corner.setStyleSheet(
            f'#EffectsHeaderActions {{ background: {C.BG_INPUT}; border: 0; }}')
        corner_bar = QHBoxLayout(corner)
        corner_bar.setContentsMargins(0, 0, 0, 0)
        corner_bar.setSpacing(0)
        self.search_button = self._tool('⌕', _tr('search_effects', 'Search Effects'))
        self.search_button.setText('')
        self.search_button.setIcon(icon_search(C.TEXT_LABEL))
        self.search_button.setCheckable(True)
        self.more_button = self._tool('⋯', _tr('effect_options', 'Effect options'))
        self.more_button.setText('')
        self.more_button.setIcon(get_icon('fa5s.ellipsis-h', color=C.TEXT_LABEL))
        corner.setFixedHeight(self.tabs.HEADER_HEIGHT - 1)
        for button in (self.search_button, self.more_button):
            button.setFixedSize(32, self.tabs.HEADER_HEIGHT - 1)
            button.setIconSize(QSize(14, 14))
            button.setStyleSheet(f"""
                QToolButton {{
                    background: transparent; border: 0; border-radius: 0;
                    border-left: 1px solid {C.PANEL_DIVIDER}; padding: 0;
                }}
                QToolButton:hover, QToolButton:checked {{ background: {C.BG_ACTIVE}; }}
                QToolButton:pressed {{ background: {C.BG_BUTTON_PRESS}; }}
                QToolButton:focus {{ border-bottom: 2px solid {C.ACCENT_SURFACE}; }}
            """)
        corner_bar.addWidget(self.search_button)
        corner_bar.addWidget(self.more_button)
        self.tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        root.addWidget(self.tabs, 1)
        self.library_page = QWidget()
        library = QVBoxLayout(self.library_page)
        library.setContentsMargins(0, 0, 0, 0)
        library.setSpacing(0)
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(10, 7, 10, 7)
        toolbar.setSpacing(3)
        self.search_edit = QLineEdit()
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setPlaceholderText(_tr('search_effects', 'Search Effects'))
        self.search_edit.setAccessibleName(_tr('search_effects', 'Search Effects'))
        toolbar.addWidget(self.search_edit, 1)
        self.favorites_button = self._tool('☆', _tr('favorites_only', 'Favorites only'))
        self.favorites_button.setCheckable(True)
        toolbar.addWidget(self.favorites_button)
        self.favorites_button.hide()
        self.view_button = self._tool('', _tr('toggle_view', 'Switch list / tile view'))
        self.view_button.setIcon(icon_grid())
        self.view_button.setCheckable(True)
        toolbar.addWidget(self.view_button)
        self.view_button.hide()
        self.search_row = QWidget()
        self.search_row.setLayout(toolbar)
        self.search_row.setVisible(False)
        library.addWidget(self.search_row)
        self.category_combo = QComboBox()
        self.category_combo.setAccessibleName(_tr('category', 'Effect category'))
        library.addWidget(self.category_combo)
        self.category_combo.hide()
        self.resource_list = _ResourceList()
        self.resource_list.setObjectName('effects_resource_list')
        self.resource_list.setFrameShape(QFrame.Shape.NoFrame)
        self.resource_list.setItemDelegate(_ResolveEffectDelegate(self.resource_list))
        self.resource_list.setIconSize(QSize(30, 22))
        self.resource_list.setDragEnabled(True)
        self.resource_list.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.resource_list.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.resource_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.resource_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.resource_list.setStyleSheet(f'QListWidget {{background:{C.BG_PANEL}; border:0;}}')
        library.addWidget(self.resource_list, 1)
        self.empty_label = QLabel(_tr('no_matching_effects', 'No matching effects'))
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        library.addWidget(self.empty_label)
        self.description_label = QLabel(_tr('effects_hint', 'Drag an effect onto a node or an image connection.'))
        self.description_label.setWordWrap(True)
        self.description_label.setMinimumHeight(34)
        self.description_label.setStyleSheet(f'color:{C.TEXT_HINT};')
        library.addWidget(self.description_label)
        self.description_label.hide()
        self.add_button = QPushButton(_tr('add_selected_node', 'Add to selected node'))
        self.add_button.setEnabled(False)
        library.addWidget(self.add_button)
        self.add_button.hide()
        self.tabs.addTab(self.library_page, _tr('library', 'Library'))
        self.settings_scroll = QScrollArea()
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_scroll.setStyleSheet(f'QScrollArea {{background:{C.BG_PANEL}; border:0;}}')
        self.tabs.addTab(self.settings_scroll, _tr('settings', 'Settings'))
        footer = QWidget()
        footer.setStyleSheet(f'border-top:1px solid {C.PANEL_DIVIDER};')
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(9, 6, 9, 6)
        footer_layout.setSpacing(3)
        self.target_label = QLabel()
        self.target_label.setWordWrap(True)
        self.status_label = QLabel(_tr('interaction_preview', 'Interaction preview · not applied to the image'))
        self._production_mode = False
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f'color:{C.TEXT_HINT};border:0;font-size:10px;')
        self.target_label.setStyleSheet('border:0;')
        footer_layout.addWidget(self.target_label)
        footer_layout.addWidget(self.status_label)
        root.addWidget(footer)
        footer.hide()
        self.search_edit.textChanged.connect(self._refresh_resources)
        self.category_combo.currentIndexChanged.connect(self._refresh_resources)
        self.favorites_button.toggled.connect(self._refresh_resources)
        self.view_button.toggled.connect(self._set_grid)
        self.resource_list.currentItemChanged.connect(self._selection_changed)
        self.resource_list.itemClicked.connect(self._category_clicked)
        self.resource_list.itemDoubleClicked.connect(self._double_clicked)
        self.resource_list.customContextMenuRequested.connect(self._resource_menu)
        self.add_button.clicked.connect(self._activate)
        self.search_button.toggled.connect(self._toggle_search)
        self.more_button.clicked.connect(self._show_options)
        self.set_resources([])
        self.set_instances([])
        self.set_target('', '', False)

    def _tool(self, text: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setStyleSheet(TOOLBAR_FLAT_BTN_STYLE)
        button.setFixedSize(25, 25)
        return button

    def _toggle_search(self, visible: bool) -> None:
        if not visible and self.tabs.currentWidget() is self.settings_scroll:
            with QSignalBlocker(self.search_button):
                self.search_button.setChecked(True)
            visible = True
        self.search_row.setVisible(visible)
        if visible:
            self.tabs.setCurrentWidget(self.library_page)
            self.search_edit.setFocus()
        else:
            self.search_edit.clear()

    def _show_options(self) -> None:
        menu = QMenu(self)
        menu.addAction(_tr('favorites_only', 'Favorites only'),
                       lambda: self.favorites_button.setChecked(not self.favorites_button.isChecked()))
        menu.addAction(_tr('expand_all', 'Expand all'), self._expand_all)
        menu.addAction(_tr('collapse_all', 'Collapse all'), self._collapse_all)
        menu.exec(self.more_button.mapToGlobal(self.more_button.rect().bottomLeft()))

    def _expand_all(self) -> None:
        self._collapsed.clear()
        self._refresh_resources()

    def _collapse_all(self) -> None:
        self._collapsed = {str(entry.get('category') or _tr('other', 'Other')) for entry in self._resources}
        self._refresh_resources()

    def set_resources(self, entries: list[dict[str, Any]]) -> None:
        self._resources = [deepcopy(entry) for entry in entries
                           if isinstance(entry, dict) and isinstance(entry.get('id'), str)
                           and entry.get('kind', 'effect') == 'effect'
                           and entry.get('disabled_reason') != 'color_backend_not_wired']
        for entry in self._resources:
            entry['kind'] = 'effect'
        current_category = self.category_combo.currentData()
        self.category_combo.blockSignals(True)
        self.category_combo.clear()
        self.category_combo.addItem(_tr('all_effects', 'All Effects'), '')
        categories = sorted({str(entry.get('category') or _tr('other', 'Other')) for entry in self._resources})
        for category in categories:
            self.category_combo.addItem(category, category)
        index = self.category_combo.findData(current_category)
        self.category_combo.setCurrentIndex(max(0, index))
        self.category_combo.blockSignals(False)
        self._refresh_resources()

    def set_production_mode(self, enabled: bool) -> None:
        """R11：真实后端模式移除"交互预览"横幅（命令已落图）。"""
        self._production_mode = bool(enabled)
        self.status_label.setText('' if enabled else _tr(
            'interaction_preview', 'Interaction preview · not applied to the image'))

    def browser_state(self) -> dict:
        """R10：用户库偏好（收藏/分类折叠/视图模式）——存用户配置，
        不进工程 undo。"""
        return {"favorites": sorted(self._favorites),
                "collapsed": sorted(self._collapsed),
                "grid": bool(self._grid)}

    def set_browser_state(self, state: object) -> None:
        if not isinstance(state, dict):
            return
        favorites = state.get("favorites")
        if isinstance(favorites, list):
            self._favorites = {str(f) for f in favorites if isinstance(f, str)}
        collapsed = state.get("collapsed")
        if isinstance(collapsed, list):
            self._collapsed = {str(c) for c in collapsed if isinstance(c, str)}
        # Resolve's Effects Library is a category list. Ignore a legacy tile
        # preference saved by earlier versions of this panel.
        self._grid = False
        self.view_button.setChecked(False)
        self._refresh_resources()

    def missing_favorite_ids(self, known_ids: object) -> list[str]:
        """收藏的资源 ID 缺失时可诊断（重命名/卸载的条目）。"""
        known = {e.get("id") for e in self._resources} if known_ids is None \
            else set(known_ids)
        return sorted(f for f in self._favorites if f not in known)

    def set_target(self, node_id: str, label: str, editable: bool, reason: str = '') -> None:
        self._target_id = node_id
        self._editable = bool(node_id and editable)
        self.target_label.setText(
            _tr('target', 'Target') + ': ' + (label or _tr('no_node', 'Select a color node'))
            + (('\n' + reason) if reason else ''))
        self._selection_changed()
        self.settings_scroll.widget().setEnabled(self._editable)

    def set_instances(self, instances: list[dict[str, Any]]) -> None:
        self._instances = deepcopy(instances)
        self._syncing = True
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(8, 0, 8, 8)
        layout.setSpacing(0)
        for index, instance in enumerate(self._instances):
            if isinstance(instance, dict):
                layout.addWidget(self._instance_card(instance, index))
        if not self._instances:
            label = QLabel(_tr('no_node_effects', 'No effects on this node.\nAdd an effect from the Library.'))
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setWordWrap(True)
            label.setStyleSheet(f'color:{C.TEXT_HINT};padding:24px 4px;')
            layout.addWidget(label)
        layout.addStretch(1)
        old = self.settings_scroll.takeWidget()
        self.settings_scroll.setWidget(content)
        if old is not None:
            old.deleteLater()
        content.setEnabled(self._editable)
        self.tabs.setTabEnabled(1, bool(self._instances))
        self._syncing = False

    def show_settings(self) -> None:
        if self.tabs.isTabEnabled(1):
            self.tabs.setCurrentWidget(self.settings_scroll)

    def _set_grid(self, enabled: bool) -> None:
        self._grid = enabled
        self.resource_list.setViewMode(QListView.ViewMode.IconMode if enabled else QListView.ViewMode.ListMode)
        self.resource_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.resource_list.setMovement(QListView.Movement.Static)
        # QListView's view and movement setters reset the drag mode.
        self.resource_list.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.resource_list.setWordWrap(enabled)
        self.resource_list.setGridSize(QSize(132, 72) if enabled else QSize())
        self.view_button.setIcon(icon_list() if enabled else icon_grid())
        self._refresh_resources()

    def _refresh_resources(self, *_args: Any) -> None:
        selected = self.resource_list.currentItem()
        selected_entry = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        selected_id = selected_entry.get('id') if isinstance(selected_entry, dict) else None
        query = self.search_edit.text().strip().casefold()
        category = self.category_combo.currentData() or ''
        self.resource_list.clear()
        previous_category = None
        visible_count = 0
        for entry in sorted(self._resources, key=lambda e: (str(e.get('category', '')).casefold(), str(e.get('name', '')).casefold())):
            entry_category = str(entry.get('category') or _tr('other', 'Other'))
            if category and entry_category != category:
                continue
            if self.favorites_button.isChecked() and entry['id'] not in self._favorites:
                continue
            if query and query not in ' '.join(str(entry.get(k, '')) for k in ('name', 'category', 'description')).casefold():
                continue
            if not self._grid and entry_category != previous_category:
                header = QListWidgetItem(('▸  ' if entry_category in self._collapsed and not query else '▾  ') + entry_category)
                header.setFlags(Qt.ItemFlag.ItemIsEnabled)
                header.setData(Qt.ItemDataRole.UserRole + 1, entry_category)
                header.setData(Qt.ItemDataRole.UserRole + 2, entry_category in self._collapsed and not query)
                header.setSizeHint(QSize(0, 35))
                font = header.font()
                font.setBold(True)
                header.setFont(font)
                self.resource_list.addItem(header)
                previous_category = entry_category
            visible_count += 1
            if not self._grid and entry_category in self._collapsed and not query:
                continue
            name = str(entry.get('name', entry['id']))
            item = QListWidgetItem(_effect_icon(entry['id']), ('★  ' if entry['id'] in self._favorites else '') + name)
            item.setSizeHint(QSize(0, 36))
            item.setData(Qt.ItemDataRole.UserRole, entry)
            item.setToolTip(name + '\n' + entry_category + '\n' + str(entry.get('description', '')))
            if not entry.get('available', True):
                item.setToolTip(item.toolTip() + '\n' + _tr('unavailable', 'Unavailable'))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
            self.resource_list.addItem(item)
            if entry['id'] == selected_id:
                self.resource_list.setCurrentItem(item)
        self.empty_label.setVisible(visible_count == 0)
        self._selection_changed()

    def _category_clicked(self, item: QListWidgetItem) -> None:
        category = item.data(Qt.ItemDataRole.UserRole + 1)
        if isinstance(category, str):
            if category in self._collapsed:
                self._collapsed.remove(category)
            else:
                self._collapsed.add(category)
            self._refresh_resources()

    def _selection_changed(self, *_args: Any) -> None:
        item = self.resource_list.currentItem()
        entry = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.add_button.setEnabled(bool(self._editable and entry and entry.get('available', True)))
        self.description_label.setText(str(entry.get('description') or entry.get('category', '')) if entry else
                                       _tr('effects_hint', 'Drag an effect onto a node or an image connection.'))

    def _double_clicked(self, _item: QListWidgetItem) -> None:
        if not self._production_mode:
            self._activate()

    def _activate(self) -> None:
        item = self.resource_list.currentItem()
        if self.add_button.isEnabled() and item is not None:
            self.resource_activated.emit(deepcopy(item.data(Qt.ItemDataRole.UserRole)))

    def _resource_menu(self, position: Any) -> None:
        item = self.resource_list.itemAt(position)
        if item is None or not isinstance(item.data(Qt.ItemDataRole.UserRole), dict):
            return
        self.resource_list.setCurrentItem(item)
        entry = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        add = menu.addAction(_tr('add_selected_node', 'Add to selected node'))
        add.setEnabled(self.add_button.isEnabled())
        add.triggered.connect(self._activate)
        favorite = menu.addAction(_tr('remove_favorite', 'Remove from Favorites') if entry['id'] in self._favorites
                                  else _tr('add_favorite', 'Add to Favorites'))
        favorite.triggered.connect(lambda: self.toggle_favorite(entry['id']))
        menu.exec(self.resource_list.mapToGlobal(position))

    def toggle_favorite(self, resource_id: str) -> None:
        if resource_id in self._favorites:
            self._favorites.remove(resource_id)
        else:
            self._favorites.add(resource_id)
        self._refresh_resources()

    def _intent(self, operation: str, payload: dict[str, Any]) -> None:
        if not self._syncing and self._editable:
            self.draft_edit_requested.emit(operation, deepcopy(payload))

    def _instance_card(self, instance: dict[str, Any], index: int) -> QWidget:
        iid = str(instance.get('instance_id', ''))
        card = QFrame()
        card.setObjectName('effect_instance_' + iid)
        card.setStyleSheet(f'QFrame#effect_instance_{iid} {{border:0; border-bottom:1px solid {C.PANEL_DIVIDER};}}')
        card.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        card.customContextMenuRequested.connect(
            lambda position: self._instance_menu(card, position, iid, index))
        box = QVBoxLayout(card)
        box.setContentsMargins(4, 4, 4, 10)
        box.setSpacing(5)
        header = QHBoxLayout()
        header.setSpacing(3)
        toggle = ToggleSwitch(bool(instance.get('enabled', True)))
        toggle.setObjectName('effect_enable_' + iid)
        toggle.setAccessibleName(_tr('enable_effect', 'Enable effect'))
        toggle.toggled.connect(lambda enabled: self._intent('set_enabled', {'instance_id': iid, 'enabled': enabled}))
        header.addWidget(toggle)
        title = QLabel(str(instance.get('name', instance.get('resource_id', 'Effect'))))
        title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title.setToolTip(title.text())
        font = title.font()
        font.setBold(True)
        title.setFont(font)
        header.addWidget(title, 1)
        for text, tooltip, operation, payload, enabled in (
            ('↺', _tr('reset_effect', 'Reset effect'), 'reset_instance', {'instance_id': iid}, True),
            ('×', _tr('remove_effect', 'Remove effect'), 'remove_instance', {'instance_id': iid}, True),
        ):
            button = self._tool(text, tooltip)
            button.setFixedSize(21, 23)
            button.setObjectName('effect_' + operation + '_' + iid)
            button.setEnabled(enabled)
            button.clicked.connect(lambda _checked=False, op=operation, data=payload: self._intent(op, data))
            header.addWidget(button)
        box.addLayout(header)
        parameters = instance.get('parameters')
        if not isinstance(parameters, list):
            parameters = []
        for metadata in parameters:
            if isinstance(metadata, dict) and isinstance(metadata.get('var_name'), str):
                box.addWidget(self._parameter_widget(iid, metadata, instance.get('params', {})))
        if not parameters:
            label = QLabel(_tr('no_parameters', 'No editable parameters'))
            label.setStyleSheet(f'color:{C.TEXT_HINT};')
            box.addWidget(label)
        return card

    def _instance_menu(self, card: QWidget, position: Any, iid: str, index: int) -> None:
        menu = QMenu(card)
        up = menu.addAction(_tr('move_up', 'Move effect up'))
        up.setEnabled(index > 0)
        up.triggered.connect(lambda: self._intent('move_instance', {'instance_id': iid, 'index': index - 1}))
        down = menu.addAction(_tr('move_down', 'Move effect down'))
        down.setEnabled(index < len(self._instances) - 1)
        down.triggered.connect(lambda: self._intent('move_instance', {'instance_id': iid, 'index': index + 1}))
        menu.exec(card.mapToGlobal(position))

    def _parameter_widget(self, iid: str, metadata: dict[str, Any], params: Any) -> QWidget:
        name = metadata['var_name']
        label = str(metadata.get('label', name))
        kind = str(metadata.get('type', 'slider'))
        value = params.get(name, metadata.get('default')) if isinstance(params, dict) else metadata.get('default')
        def change(current: Any, phase: str = 'commit') -> None:
            self._intent('set_parameter', {'instance_id': iid, 'name': name, 'value': current, 'phase': phase})
        if kind in ('slider', 'float', 'int', 'integer', 'number'):
            low = _number(metadata.get('min'), 0.0)
            high = max(low, _number(metadata.get('max'), 1.0))
            current = min(high, max(low, _number(value, low)))
            integer = kind in ('int', 'integer')
            row = ParamSliderRow(label, low, high, current, step=1.0 if integer else max(0.01, (high-low)/1000),
                                 decimals=0 if integer else 3, label_width=80, value_fixed_width=54)
            # R04：手势相位完整化——start/change/commit（ParamSliderRow 的
            # interactionStarted 即拖动起点；取消拾色/未确认颜色不发射）。
            row.interactionStarted.connect(lambda: change(
                int(round(row.value())) if integer else row.value(), 'start'))
            row.valueChanged.connect(lambda v: change(int(round(v)) if integer else v, 'change'))
            row.interactionEnded.connect(lambda: change(int(round(row.value())) if integer else row.value(), 'commit'))
            row.setObjectName('effect_param_' + iid + '_' + name)
            return row
        row = QWidget()
        row.setObjectName('effect_param_' + iid + '_' + name)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        caption = QLabel(label)
        if kind in ('checkbox', 'bool', 'boolean'):
            control = QCheckBox()
            control.setChecked(bool(value))
            control.setAccessibleName(label)
            control.toggled.connect(change)
            layout.addStretch(1)
            layout.addWidget(control)
            layout.addWidget(caption)
            layout.addStretch(1)
        elif kind in ('select', 'enum', 'choice'):
            caption.setFixedWidth(80)
            caption.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            layout.addWidget(caption)
            combo = QComboBox()
            combo.setMinimumWidth(0)
            options = metadata.get('options', [])
            if isinstance(options, list):
                for index, option in enumerate(options):
                    if isinstance(option, dict):
                        combo.addItem(str(option.get('label', option.get('name', index))), option.get('value', index))
                    else:
                        combo.addItem(str(option), index)
            selected = combo.findData(value)
            combo.setCurrentIndex(max(0, selected))
            combo.currentIndexChanged.connect(lambda _index: change(combo.currentData()))
            combo.setAccessibleName(label)
            layout.addWidget(combo, 1)
        elif kind == 'color':
            caption.setFixedWidth(80)
            caption.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            layout.addWidget(caption)
            rgb = value if isinstance(value, (list, tuple)) and len(value) >= 3 else [1.0, 1.0, 1.0]
            picker = ColorPickerControl(tuple(_number(v, 1.0) for v in rgb[:3]) + (1.0,))
            picker.color_changed.connect(lambda color: change(list(qcolor_to_linear_rgba(color)[:3])))
            layout.addWidget(picker, 1)
        else:
            caption.setFixedWidth(80)
            caption.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            layout.addWidget(caption)
            unsupported = QLabel(_tr('unsupported_parameter', 'Parameter UI unavailable') + ' (' + kind + ')')
            unsupported.setWordWrap(True)
            unsupported.setStyleSheet(f'color:{C.TEXT_HINT};')
            layout.addWidget(unsupported, 1)
        return row
