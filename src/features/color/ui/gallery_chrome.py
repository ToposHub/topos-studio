"""Gallery presentation: pixel-sized stills, album navigation and list mode.

All filesystem writes, scans, cached metadata and public signals remain owned
by PresetsPanel. This module changes layout and selection presentation only.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PyQt6.QtCore import QEvent, QObject, QRect, QRectF, QSize, Qt, QSignalBlocker, QTimer
from PyQt6.QtGui import QActionGroup, QColor, QFont, QKeyEvent, QKeySequence, QPainter, QPainterPath, QPen, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMenu, QSlider, QSplitter, QStyle, QToolButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.chevron_combo import ChevronComboBox
from src.features.color.ui.gallery_icons import gallery_icon

if TYPE_CHECKING:
    from src.features.color.ui.presets_panel import PresetsPanel, PresetDelegate


_BUTTON_STYLE = """
QToolButton { background: transparent; border: none; border-radius: 2px;
    padding: 0; margin: 0; color: #a2a2a6; }
QToolButton:hover { background: #35353b; }
QToolButton:checked { background: #3b3b40; }
QToolButton::menu-indicator { image: none; }
"""


def _button(parent: QWidget, name: str, text: str) -> QToolButton:
    button = QToolButton(parent)
    button.setIcon(gallery_icon(name))
    button.setIconSize(QSize(20, 20))
    button.setFixedSize(32, 28)
    button.setStyleSheet(_BUTTON_STYLE)
    button.setToolTip(text)
    button.setAccessibleName(text)
    button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return button


class GalleryListHeader(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedHeight(20)

    def paintEvent(self, event: QEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#202024"))
        painter.setPen(QColor("#b4b4b9"))
        font = QFont(self.font())
        font.setPixelSize(10)
        painter.setFont(font)
        titles = (
            I18N.tr("gallery.still_id", "Still ID"),
            I18N.tr("gallery.label", "Label"),
            I18N.tr("gallery.timeline_name", "Timeline Name"),
        )
        for index, text in enumerate(titles):
            left = (0, int(self.width() * 0.4), int(self.width() * 0.7))[index]
            right = (int(self.width() * 0.4), int(self.width() * 0.7), self.width())[index]
            painter.drawText(QRect(left + 10, 0, max(0, right - left - 16), 20), Qt.AlignmentFlag.AlignVCenter, text)
            if index:
                painter.setPen(QPen(QColor("#35353a"), 1))
                painter.drawLine(left, 2, left, 18)
                painter.setPen(QColor("#b4b4b9"))
        painter.end()


class GalleryGridController(QObject):
    """Keep the selected thumbnail width fixed and only change column count."""

    def __init__(self, view: QListWidget, slider: QSlider) -> None:
        super().__init__(view)
        self.view = view
        self.slider = slider
        self.columns = 1
        self.thumb_width = 108
        self.list_mode = False
        self.header = GalleryListHeader(view)
        self.header.hide()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.retile)
        slider.setRange(108, 256)
        slider.setValue(self.thumb_width)
        slider.valueChanged.connect(self.set_thumbnail_width)
        view.installEventFilter(self)
        view.viewport().installEventFilter(self)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.retile()

    def set_thumbnail_width(self, width: int) -> None:
        self.thumb_width = max(108, min(256, int(width)))
        self.retile()

    def set_list_mode(self, enabled: bool) -> None:
        self.list_mode = enabled
        self.slider.setEnabled(not enabled)
        self.view.setViewMode(QListWidget.ViewMode.ListMode if enabled else QListWidget.ViewMode.IconMode)
        self.view.setWrapping(not enabled)
        self.view.setFlow(QListWidget.Flow.TopToBottom if enabled else QListWidget.Flow.LeftToRight)
        self.header.setVisible(enabled)
        self.retile()

    def retile(self) -> None:
        view = self.view
        full_width = max(1, view.width() - 2 * view.frameWidth() - 2)
        count = sum(not view.item(i).isHidden() for i in range(view.count()))
        # Predict overflow from the full width first. Depending on the scrollbar's
        # current visibility causes resize/retile oscillation at row thresholds.
        if self.list_mode:
            content_height = count * 27
            available_height = view.height() - 2 * view.frameWidth() - 20
        else:
            thumb = min(self.thumb_width, max(32, full_width - 24))
            columns = max(1, (full_width + 20) // (thumb + 36))
            rows = (count + columns - 1) // columns
            content_height = rows * (round(thumb * 9 / 16) + 37)
            available_height = view.height() - 2 * view.frameWidth() - 14
        scrollbar_width = (view.verticalScrollBar().sizeHint().width()
                           if content_height > available_height else 0)
        width = max(1, full_width - scrollbar_width)
        if self.list_mode:
            view.setViewportMargins(0, 20, 0, 0)
            self.header.setGeometry(0, 0, view.width(), 20)
            self.header.raise_()
            view.setGridSize(QSize())
            cell = QSize(width, 27)
        else:
            thumbnail_width = min(self.thumb_width, max(32, width - 24))
            self.columns = max(1, (width + 20) // (thumbnail_width + 36))
            # Keep spacing inside each cell. A left viewport margin is deducted
            # again by Qt's AsNeeded icon layout and can unexpectedly lose a column.
            pitch = max(thumbnail_width + 4, (width - 16) // self.columns)
            view.setViewportMargins(0, 14, 0, 0)
            view.setIconSize(QSize(thumbnail_width, round(thumbnail_width * 9 / 16)))
            cell = QSize(pitch, view.iconSize().height() + 37)
            view.setGridSize(cell)
        for index in range(view.count()):
            view.item(index).setSizeHint(cell)
        view.doItemsLayout()
        view.viewport().update()

    def pin_items(self) -> None:
        """Compatibility hook used by the existing filesystem scan owner."""
        self.retile()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not hasattr(self, "view"):
            return False
        if event.type() == QEvent.Type.Resize and watched in (self.view, self.view.viewport()):
            self.timer.start(0)
        return super().eventFilter(watched, event)


class GalleryChrome(QObject):
    def __init__(self, panel: PresetsPanel) -> None:
        super().__init__(panel)
        self.panel = panel
        self._sort_order = Qt.SortOrder.AscendingOrder
        self._refreshing_folders = False
        panel.setObjectName("PresetsGallery")
        panel.setMinimumWidth(220)
        panel.setStyleSheet("""
            QWidget#PresetsGallery, QFrame#gallerySearchRow { background: #28282e; }
            QFrame#galleryToolbar { background: #202024; border-bottom: 1px solid #111114; }
            QListWidget#galleryStills { background: #28282e; border: none; padding: 0; outline: none; }
            QListWidget#galleryStills::item { background: transparent; border: 0; padding: 0; }
            QTreeWidget#galleryAlbums { background: #242429; border: 0; color: #c6c6cb; outline: none; font-size: 11px; }
            QTreeWidget#galleryAlbums::item { height: 30px; border: none; }
            QTreeWidget#galleryAlbums::item:selected { background: #3a3a40; color: #eee; }
            QScrollBar:vertical { background: #28282e; width: 10px; margin: 0; }
            QScrollBar::handle:vertical { background: #4b4b52; min-height: 24px; border-radius: 4px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        """)
        root = QVBoxLayout(panel)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.toolbar = QFrame(panel)
        self.toolbar.setObjectName("galleryToolbar")
        self.toolbar.setFixedHeight(28)
        tools = QHBoxLayout(self.toolbar)
        tools.setContentsMargins(3, 0, 3, 0)
        tools.setSpacing(0)
        self.sidebar_button = _button(self.toolbar, "sidebar", I18N.tr("gallery.sidebar", "Show Albums"))
        self.sidebar_button.setCheckable(True)
        self.import_button = _button(self.toolbar, "import", I18N.tr("gallery.import", "Import Still"))
        self.import_button.setEnabled(False)
        self.import_button.setToolTip(I18N.tr("gallery.import_unavailable", "Import Still — not available in this version"))
        tools.addWidget(self.sidebar_button)
        tools.addWidget(self.import_button)
        tools.addStretch(1)
        panel.zoom_slider = QSlider(Qt.Orientation.Horizontal, self.toolbar)
        panel.zoom_slider.setFixedWidth(76)
        panel.zoom_slider.setToolTip(I18N.tr("gallery.thumbnail_size", "Thumbnail Size"))
        panel.zoom_slider.setAccessibleName(panel.zoom_slider.toolTip())
        # Inherit the application's orange round handle, groove and interaction
        # states; only the widget's surrounding background must be transparent.
        panel.zoom_slider.setStyleSheet("QSlider { background: transparent; }")
        tools.addWidget(panel.zoom_slider)
        self.sort_button = _button(self.toolbar, "sort", I18N.tr("gallery.sort", "Sort Stills"))
        self.grid_button = _button(self.toolbar, "grid", I18N.tr("gallery.grid", "Thumbnail View"))
        self.list_button = _button(self.toolbar, "list", I18N.tr("gallery.list", "List View"))
        self.search_button = _button(self.toolbar, "search", I18N.tr("gallery.search", "Search Stills"))
        self.expand_button = _button(self.toolbar, "expand", I18N.tr("gallery.expand", "Gallery View"))
        self.more_button = _button(self.toolbar, "more", I18N.tr("gallery.more", "Gallery Options"))
        for button in (self.grid_button, self.list_button, self.search_button):
            button.setCheckable(True)
        self.grid_button.setChecked(True)
        self.sort_menu = QMenu(self.sort_button)
        group = QActionGroup(self.sort_menu)
        for text, order in ((I18N.tr("gallery.sort_name_ascending", "Name: A–Z"), Qt.SortOrder.AscendingOrder), (I18N.tr("gallery.sort_name_descending", "Name: Z–A"), Qt.SortOrder.DescendingOrder)):
            action = self.sort_menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(order == self._sort_order)
            group.addAction(action)
            action.triggered.connect(lambda checked=False, value=order: self.sort(value))
        self.sort_button.setMenu(self.sort_menu)
        self.sort_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_menu = QMenu(self.more_button)
        self.more_menu.aboutToShow.connect(self._rebuild_more_menu)
        self.more_button.setMenu(self.more_menu)
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        for button in (self.sort_button, self.grid_button, self.list_button, self.search_button, self.expand_button, self.more_button):
            tools.addWidget(button)
        root.addWidget(self.toolbar)

        self.search_row = QFrame(panel)
        self.search_row.setObjectName("gallerySearchRow")
        self.search_row.setFixedHeight(50)
        search_layout = QHBoxLayout(self.search_row)
        search_layout.setContentsMargins(20, 15, 20, 15)
        panel.search_edit = QLineEdit(self.search_row)
        panel.search_edit.setPlaceholderText(I18N.tr("presets.search", "Search stills…"))
        panel.search_edit.setClearButtonEnabled(True)
        panel.search_edit.setFixedHeight(20)
        panel.search_edit.setStyleSheet("QLineEdit { background: #1c1c20; color: #d4d4d8; border: 1px solid #414148; border-radius: 3px; padding: 0 5px; font-size: 11px; }")
        panel.search_edit.textChanged.connect(panel._on_search_changed)
        panel.search_edit.installEventFilter(self)
        search_layout.addWidget(panel.search_edit)
        root.addWidget(self.search_row)
        self.search_row.hide()

        self.splitter = QSplitter(Qt.Orientation.Horizontal, panel)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)
        self.splitter.setStyleSheet("QSplitter::handle { background: #17171b; }")
        self.albums = QTreeWidget(self.splitter)
        self.albums.setObjectName("galleryAlbums")
        self.albums.setHeaderHidden(True)
        self.albums.setMinimumWidth(72)
        self.albums.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.albums.setIndentation(14)
        panel.list_widget = QListWidget(self.splitter)
        panel.list_widget.setObjectName("galleryStills")
        panel.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        panel.list_widget.setResizeMode(QListWidget.ResizeMode.Adjust)
        panel.list_widget.setMovement(QListWidget.Movement.Static)
        panel.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        panel.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        panel.list_widget.customContextMenuRequested.connect(panel._show_context_menu)
        panel.list_widget.itemDoubleClicked.connect(self.apply_item)
        self.splitter.addWidget(self.albums)
        self.splitter.addWidget(panel.list_widget)
        self.splitter.setSizes([150, 500])
        self.splitter.setStretchFactor(1, 1)
        self.albums.hide()
        root.addWidget(self.splitter, 1)
        self.grid = GalleryGridController(panel.list_widget, panel.zoom_slider)
        panel.folder_combo = ChevronComboBox(panel)
        panel.folder_combo.currentTextChanged.connect(panel._on_folder_changed)
        panel.folder_combo.hide()
        panel.delete_btn = QToolButton(panel)
        panel.delete_btn.clicked.connect(panel._delete_selected)
        panel.delete_btn.hide()
        self.sidebar_button.toggled.connect(self.albums.setVisible)
        self.grid_button.clicked.connect(lambda: self.set_view_mode("grid"))
        self.list_button.clicked.connect(lambda: self.set_view_mode("list"))
        self.search_button.toggled.connect(self.set_search_visible)
        self.expand_button.clicked.connect(self.toggle_float)
        self.albums.itemClicked.connect(self._open_album)
        self.albums.itemExpanded.connect(self._expand_album)
        panel.installEventFilter(self)
        self._apply_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Return), panel.list_widget)
        self._apply_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._apply_shortcut.activated.connect(lambda: self.apply_item(panel.list_widget.currentItem()))
        self._delete_shortcut = QShortcut(QKeySequence.StandardKey.Delete, panel.list_widget)
        self._delete_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._delete_shortcut.activated.connect(panel._delete_selected)
        self._search_shortcut = QShortcut(QKeySequence.StandardKey.Find, panel)
        self._search_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._search_shortcut.activated.connect(lambda: self.set_search_visible(True))
        self._update_responsive()

    def apply_item(self, item: object) -> None:
        if item is not None:
            path = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(path, str) and os.path.isfile(path):
                self.panel.grade_applied.emit(path)

    def set_view_mode(self, mode: str) -> None:
        enabled = mode == "list"
        for button, checked in ((self.grid_button, not enabled), (self.list_button, enabled)):
            blocker = QSignalBlocker(button)
            button.setChecked(checked)
            del blocker
        self.grid.set_list_mode(enabled)

    def set_search_visible(self, visible: bool) -> None:
        self.search_row.setVisible(visible)
        blocker = QSignalBlocker(self.search_button)
        self.search_button.setChecked(visible)
        del blocker
        if visible:
            self.panel.search_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.panel.search_edit.clear()
            self.panel.list_widget.setFocus(Qt.FocusReason.OtherFocusReason)

    def sort(self, order: Qt.SortOrder) -> None:
        self._sort_order = order
        self.panel.list_widget.sortItems(order)

    def after_scan(self) -> None:
        self.sort(self._sort_order)
        self.refresh_folders()
        self.grid.pin_items()

    def refresh_folders(self) -> None:
        self._refreshing_folders = True
        self.albums.clear()
        for name, label in (("User", I18N.tr("gallery.stills_one", "Stills 1")), ("System", "System")):
            path = os.path.join(self.panel.presets_root, name)
            item = QTreeWidgetItem(self.albums, [label])
            item.setIcon(0, gallery_icon("album"))
            item.setData(0, Qt.ItemDataRole.UserRole, path)
            self._fill_children(item)
            current = self.panel.get_current_folder()
            if os.path.commonpath([os.path.abspath(current), os.path.abspath(path)]) == os.path.abspath(path):
                item.setExpanded(True)
                branch = item
                while current != branch.data(0, Qt.ItemDataRole.UserRole):
                    match = next((branch.child(i) for i in range(branch.childCount()) if os.path.commonpath([os.path.abspath(current), os.path.abspath(branch.child(i).data(0, Qt.ItemDataRole.UserRole))]) == os.path.abspath(branch.child(i).data(0, Qt.ItemDataRole.UserRole))), None)
                    if match is None:
                        break
                    self._fill_children(match)
                    match.setExpanded(True)
                    branch = match
                if branch.data(0, Qt.ItemDataRole.UserRole) == current:
                    self.albums.setCurrentItem(branch)
        for name, icon in (("PowerGrade", "power_grade"), ("Timelines", "timeline"), ("Memories", "memories")):
            item = QTreeWidgetItem(self.albums, [I18N.tr("gallery." + name.lower(), name)])
            item.setIcon(0, gallery_icon(icon))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled & ~Qt.ItemFlag.ItemIsSelectable)
            item.setToolTip(0, I18N.tr("gallery.album_unavailable", "Not available in this version"))
        self._refreshing_folders = False

    def _fill_children(self, item: QTreeWidgetItem) -> None:
        if item.data(0, Qt.ItemDataRole.UserRole + 2):
            return
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(path, str):
            return
        for name in sorted(self.panel._list_subdirs(path)):
            child = QTreeWidgetItem(item, [name])
            child.setData(0, Qt.ItemDataRole.UserRole, os.path.join(path, name))
            child.setIcon(0, gallery_icon("album"))
            child.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        item.setData(0, Qt.ItemDataRole.UserRole + 2, True)

    def _expand_album(self, item: QTreeWidgetItem) -> None:
        if not self._refreshing_folders:
            self._fill_children(item)

    def _open_album(self, item: QTreeWidgetItem, column: int) -> None:
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if not self._refreshing_folders and isinstance(path, str) and os.path.isdir(path):
            self.panel._current_folder = path
            self.panel._populate_folder_combo()
            self.panel.scan_presets()

    def toggle_float(self) -> None:
        dock = self.panel._find_dock_widget()
        if dock is not None:
            self.panel._dock_panel() if dock.isFloating() else self.panel._toggle_float()

    def _rebuild_more_menu(self) -> None:
        menu = self.more_menu
        menu.clear()
        menu.setToolTipsVisible(True)
        menu.addAction(I18N.tr("presets.capture_still", "Capture Still"), self.panel.capture_requested.emit)
        menu.addAction(I18N.tr("gallery.image_wipe", "Image Wipe") + "…", lambda: self.panel.open_comparison("wipe"))
        menu.addAction(I18N.tr("gallery.split_screen", "Split Screen") + "…", lambda: self.panel.open_comparison("split"))
        menu.addAction(I18N.tr("presets.new_folder", "New Folder"), self.panel.create_folder)
        menu.addAction(I18N.tr("presets.go_up", "Go Up"), self.panel._go_up)
        folders = menu.addMenu(I18N.tr("gallery.albums", "Albums"))
        combo = self.panel.folder_combo
        for index in range(combo.count()):
            action = folders.addAction(combo.itemText(index))
            action.setCheckable(True)
            action.setChecked(combo.itemData(index) == self.panel.get_current_folder())
            action.triggered.connect(lambda checked=False, value=index: combo.setCurrentIndex(value))
        for text in ("PowerGrade", "Timelines", "Memories"):
            action = folders.addAction(I18N.tr("gallery." + text.lower(), text))
            action.setEnabled(False)
            action.setToolTip(I18N.tr("gallery.album_unavailable", "Not available in this version"))
        menu.addMenu(self.sort_menu).setText(I18N.tr("gallery.sort", "Sort Stills"))
        menu.addAction(I18N.tr("presets.refresh", "Refresh"), self.panel.scan_presets)
        menu.addAction(I18N.tr("presets.delete", "Delete"), self.panel._delete_selected).setEnabled(bool(self.panel.list_widget.selectedItems()))
        menu.addSeparator()
        action = menu.addAction(I18N.tr("gallery.import", "Import Still"))
        action.setEnabled(False)
        action.setToolTip(self.import_button.toolTip())
        float_action = menu.addAction(I18N.tr("gallery.expand", "Gallery View"), self.toggle_float)
        float_action.setEnabled(self.panel._find_dock_widget() is not None)

    def _update_responsive(self) -> None:
        compact = self.panel.width() < 400
        for button in (self.import_button, self.sort_button, self.expand_button):
            button.setVisible(not compact)
        self.panel.zoom_slider.setFixedWidth(48 if compact else 76)
        self.expand_button.setEnabled(self.panel._find_dock_widget() is not None)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not hasattr(self, "panel"):
            return False
        if watched is self.panel and event.type() in {QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.ParentChange}:
            self._update_responsive()
        if watched is self.panel.search_edit and event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.set_search_visible(False)
            event.accept()
            return True
        return super().eventFilter(watched, event)


def paint_gallery_item(painter: QPainter, option: object, index: object, delegate: PresetDelegate) -> None:
    """Draw an unframed 16:9 still and caption, or a real three-column row."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = QFont(painter.font())
    font.setPixelSize(11)
    painter.setFont(font)
    selected = bool(option.state & QStyle.StateFlag.State_Selected)
    view = delegate.panel.list_widget
    if delegate.panel._gallery.grid.list_mode:
        # G02：三列 = Still ID／Label／Timeline Name。身份来自 meta 的
        # 稳定 still_id（未补齐的旧条目显示 "—"，不把文件名当 ID）；
        # Label 独立于文件名（未设置时退回文件主名显示）；timeline 名是
        # 抓帧时的快照，缺失留空——不推断成当前 timeline。
        row = option.rect
        painter.fillRect(row, QColor("#454044" if selected else "#25252a" if index.row() % 2 == 0 else "#28282e"))
        meta = index.data(Qt.ItemDataRole.UserRole + 1)
        still_id = getattr(meta, "still_id", None)
        label = getattr(meta, "label", None)
        timeline_name = getattr(meta, "timeline_name", None)
        file_name = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        columns = (
            (still_id if still_id else "—", 0.40, QColor("#e0e0e3")),
            (label if label else file_name, 0.30, QColor("#e0e0e3")),
            (timeline_name if timeline_name else "", 0.30, QColor("#9a9aa2")),
        )
        left = row.left() + 10
        for text, fraction, color in columns:
            width = max(0, int(row.width() * fraction) - 18)
            painter.setPen(color)
            rect = QRect(left, row.top(), width, 27)
            painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(
                                 text, Qt.TextElideMode.ElideRight, width))
            left += width + 18
        if selected:
            painter.setPen(QPen(QColor("#e86351"), 1))
            painter.drawLine(row.left(), row.top(), row.left(), row.bottom())
    else:
        width = view.iconSize().width()
        height = view.iconSize().height()
        inset = max(0, (option.rect.width() - width) // 2)
        rect = QRect(option.rect.left() + inset, option.rect.top(), width, height)
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), 2.0, 2.0)
        painter.fillPath(path, QColor("#17171b"))
        pixmap = delegate._thumbnail_pixmap(index.data(Qt.ItemDataRole.UserRole)) if index.data(Qt.ItemDataRole.UserRole) else None
        if pixmap is not None:
            # Preserve the still's aspect ratio; non-16:9 media are letterboxed.
            scaled = pixmap.scaled(rect.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            painter.save()
            painter.setClipPath(path)
            painter.drawPixmap(rect.left() + (width - scaled.width()) // 2, rect.top() + (height - scaled.height()) // 2, scaled)
            painter.restore()
        else:
            gallery_icon("capture").paint(painter, rect.adjusted(width // 3, height // 4, -width // 3, -height // 4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#e86351" if selected else "#0c0c0f"), 2.0 if selected else 1.0))
        painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 2.0, 2.0)
        painter.setPen(QColor("#d7d7dc"))
        caption = QRect(rect.left(), rect.bottom() + 5, width, 14)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        painter.drawText(caption, Qt.AlignmentFlag.AlignCenter, painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideMiddle, width))
    painter.restore()
