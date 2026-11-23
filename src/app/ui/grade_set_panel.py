"""Dock content for switching between still images in a shared grade set."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import (
    QAbstractListModel, QMimeData, QModelIndex, QPoint, QSize, QSignalBlocker,
    QSortFilterProxyModel, Qt, QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QAction, QActionGroup, QDragEnterEvent, QDragMoveEvent, QDropEvent, QHideEvent,
    QImage, QKeyEvent,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QMenu, QSizePolicy, QSlider,
    QToolButton, QVBoxLayout, QWidget,
)
import qtawesome as qta

from src.app.ui.native_files_gallery import FilesCardDelegate, RATING_CAPTION_HEIGHT
from src.app.ui.native_files_gallery_view import GALLERY_ENTRY_ROLE, GALLERY_MARGIN, FilesGalleryView, GallerySpacing
from src.app.ui.native_files_transport import NativeFilesTransport
from src.features.files.contracts import Entry, UserMetadata
from src.features.files.repository.catalog import nfc
from src.shared.i18n.i18n import I18N
from src.shared.media.format_registry import IMAGE_EXTENSIONS, is_image
from src.shared.media.graded_thumbnail import GRADED_PREVIEW_JPEG_HEADER, GRADED_PREVIEW_MAX_EDGE, is_webp_preview
from src.shared.ui.style_sheet import C
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

if TYPE_CHECKING:
    from src.app.controllers.grade_sets import GradeSetModel

_PATH_ROLE = int(Qt.ItemDataRole.UserRole)
_RATING_ROLE = _PATH_ROLE + 2
_FORMAT_ROLE = _PATH_ROLE + 3
_MIN_THUMBNAIL_SIZE = 82
_DEFAULT_THUMBNAIL_SIZE = 118
_CARD_SPACING = GallerySpacing(inset=3, gap=3, caption_height=0)
_NAMED_CARD_SPACING = replace(_CARD_SPACING, caption_height=20)
_THUMBNAIL_SIZE = 224
_MAX_CACHED_THUMBNAILS = 256
_MAX_PENDING_THUMBNAILS = 4
_SELECT_MODIFIERS = (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier
                     | Qt.KeyboardModifier.MetaModifier)


def _tr(key: str, fallback: str, **kwargs: object) -> str:
    return I18N.tr(f"grade_sets.{key}", fallback, **kwargs)


def _menu_choices(menu: QMenu, group: QActionGroup, choices: Iterable[tuple[str, str | int]],
                  selected: str | int) -> None:
    for label, value in choices:
        action = menu.addAction(label)
        action.setData(value)
        action.setCheckable(True)
        group.addAction(action)
        action.setChecked(value == selected)


def _dropped_images(mime: QMimeData) -> list[str]:
    return [path for url in mime.urls()
            if url.isLocalFile() and is_image(path := url.toLocalFile())]


class _GradeSetImages(QAbstractListModel):
    def __init__(self, grade_sets: GradeSetModel, parent: QWidget) -> None:
        super().__init__(parent)
        self._grade_sets = grade_sets
        self.paths: tuple[str, ...] = ()
        self._rows_by_path: dict[str, int] = {}
        self._entries: tuple[Entry, ...] = ()
        self._formats: tuple[str, ...] = ()
        self._grade_sets.ratings_changed.connect(self._refresh_ratings)

    def set_paths(self, paths: Iterable[str]) -> None:
        self.beginResetModel()
        self.paths = tuple(paths)
        self._entries = tuple(Entry(path, Path(path).name, "image") for path in self.paths)
        self._formats = tuple(Path(entry.name).suffix[1:].upper() for entry in self._entries)
        self._rows_by_path = {path: row for row, path in enumerate(self.paths)}
        self.endResetModel()

    def formats(self) -> list[str]:
        return sorted({ext for ext in self._formats if ext})

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.paths)

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        return flags | Qt.ItemFlag.ItemIsDragEnabled if index.isValid() else flags

    def supportedDragActions(self) -> Qt.DropAction:
        return Qt.DropAction.CopyAction

    def mimeTypes(self) -> list[str]:
        return ["text/uri-list"]

    def mimeData(self, indexes: Iterable[QModelIndex]) -> QMimeData:
        mime = QMimeData()
        rows = sorted({index.row() for index in indexes if index.isValid()})
        mime.setUrls([QUrl.fromLocalFile(self.paths[row]) for row in rows])
        return mime

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Entry | str | int | None:
        if not index.isValid() or not 0 <= index.row() < len(self.paths):
            return None
        entry = self._entries[index.row()]
        path = entry.path
        if role == _PATH_ROLE:
            return path
        if role == Qt.ItemDataRole.DisplayRole:
            return entry.name
        if role == _FORMAT_ROLE:
            return self._formats[index.row()]
        if role == GALLERY_ENTRY_ROLE:
            rating = self._grade_sets.ratings.get(path, 0)
            return entry.with_user(UserMetadata(rating=rating)) if rating else entry
        if role in (_RATING_ROLE, Qt.ItemDataRole.ToolTipRole, Qt.ItemDataRole.AccessibleDescriptionRole):
            rating = self._grade_sets.ratings.get(path, 0)
            if role == _RATING_ROLE:
                return rating
            description = _tr("rating", "Rating {rating} of 5", rating=rating) if rating else ""
            if role == Qt.ItemDataRole.ToolTipRole:
                return f"{path}\n{description}" if description else path
            return description
        return None

    def path_index(self, path: str | None) -> QModelIndex:
        return self.index(self._rows_by_path.get(path, -1)) if path is not None else QModelIndex()

    def _refresh_ratings(self) -> None:
        if self.paths:
            self.dataChanged.emit(self.index(0), self.index(len(self.paths) - 1),
                                  [GALLERY_ENTRY_ROLE, _RATING_ROLE, Qt.ItemDataRole.ToolTipRole,
                                   Qt.ItemDataRole.AccessibleDescriptionRole])


class _GradeSetOrder(QSortFilterProxyModel):
    def __init__(self, source: _GradeSetImages, parent: QWidget) -> None:
        super().__init__(parent)
        self.file_format = ""
        self.rating = -1  # All ratings
        self.order = "added"
        self.setSourceModel(source)
        self.setFilterRole(_RATING_ROLE)
        self.setSortRole(_RATING_ROLE)

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        model = self.sourceModel()
        index = model.index(row, 0, parent)
        if self.file_format and index.data(_FORMAT_ROLE) != self.file_format:
            return False
        if self.rating == -1:
            return True
        rating = index.data(_RATING_ROLE)
        return rating == 0 if self.rating == 0 else rating >= self.rating

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        if self.order == "rating":
            return (-left.data(_RATING_ROLE), left.row()) < (-right.data(_RATING_ROLE), right.row())
        if self.order in ("name", "name_desc"):
            lhs, rhs = left.data().casefold(), right.data().casefold()
            if lhs != rhs:
                return lhs < rhs if self.order == "name" else lhs > rhs
        return left.row() < right.row()


class _GradeSetView(FilesGalleryView):
    image_requested = pyqtSignal(str)
    images_dropped = pyqtSignal(list)

    def __init__(self, parent: QWidget, spacing: GallerySpacing) -> None:
        super().__init__(parent, spacing=spacing, adaptive_tail_scale=1.35)
        self.setObjectName("gradeSetImages")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet("QAbstractItemView#gradeSetImages { border: none; background: transparent; }")
        self.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setMinimumWidth(_MIN_THUMBNAIL_SIZE)
        self.setThumbnailSize(_DEFAULT_THUMBNAIL_SIZE)
        self.setDropIndicatorShown(False)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.accept_files = False
        self.clicked.connect(self._on_click)

    def _rebuild_layout(self) -> None:
        if self._laying_out:
            return
        super()._rebuild_layout()
        height = _MIN_THUMBNAIL_SIZE
        if self._rectangles:
            first = self._rectangles[0]
            bottom_margin = (self._spacing.gap if self._rectangles[-1].top() > first.top()
                             else GALLERY_MARGIN)
            height = first.bottom() + 1 + bottom_margin
        self.setMinimumHeight(height)

    def set_initial_aspect(self, path: str, image: QImage) -> None:
        if path not in self._aspect_hints:
            self.setAspectRatio(path, image.width() / image.height())

    def _on_click(self, index: QModelIndex) -> None:
        if not QApplication.keyboardModifiers() & _SELECT_MODIFIERS:
            self._open_index(index)

    def _open_index(self, index: QModelIndex) -> None:
        path = index.data(_PATH_ROLE)
        if path:
            self.image_requested.emit(path)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._open_index(self.currentIndex())
            event.accept()
            return
        previous = self.currentIndex()
        super().keyPressEvent(event)
        if (event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down)
                and not event.modifiers() & _SELECT_MODIFIERS and self.currentIndex() != previous):
            self._open_index(self.currentIndex())

    def _accept_image_drag(self, event: QDropEvent) -> None:
        if (event.source() is not self and self.accept_files
                and event.possibleActions() & Qt.DropAction.CopyAction
                and _dropped_images(event.mimeData())):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        self._accept_image_drag(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        self._accept_image_drag(event)

    def dropEvent(self, event: QDropEvent) -> None:
        allow_copy = (event.source() is not self and self.accept_files
                      and event.possibleActions() & Qt.DropAction.CopyAction)
        files = _dropped_images(event.mimeData()) if allow_copy else []
        if not files:
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.images_dropped.emit(files)


class GradeSetPanel(QWidget):
    """Presenter for an ordered set of local photo references."""

    image_requested = pyqtSignal(str)
    active_photo_name_changed = pyqtSignal(str)
    filename_visibility_changed = pyqtSignal(bool)
    rating_at_bottom_changed = pyqtSignal(bool)
    copy_grade_requested = pyqtSignal(str)
    paste_grade_requested = pyqtSignal(str)
    export_queue_requested = pyqtSignal(list)

    def __init__(self, model: GradeSetModel, parent: QWidget | None = None,
                 *, show_filenames: bool = False, rating_at_bottom: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("gradeSetPanel")
        self._model = model
        self._active_id: int | None = None
        self.active_path: str | None = None
        self._transport: NativeFilesTransport | None = None
        self._source_previews: OrderedDict[str, QImage] = OrderedDict()
        self._graded_previews: OrderedDict[str, QImage] = OrderedDict()
        self._candidates: dict[str, None] = {}
        self._pending: set[str] = set()
        self._failed: set[str] = set()
        self._closed = False
        self._grade_clipboard_available = False
        self._request_timer = QTimer(self)
        self._request_timer.setSingleShot(True)
        self._request_timer.timeout.connect(self._request_visible_thumbnails)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(3)
        self.message = QLabel(self)
        self.message.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setStyleSheet(f"color: {C.TEXT_HINT};")
        spacing = self._caption_spacing(show_filenames, rating_at_bottom)
        self.view = _GradeSetView(self, spacing)
        self.view.setAccessibleName(_tr("images", "Grade Set Images"))
        self._images = _GradeSetImages(model, self.view)
        self._order = _GradeSetOrder(self._images, self.view)
        self.view.setModel(self._order)
        self._card_delegate = FilesCardDelegate(
            GALLERY_ENTRY_ROLE, lambda entry: self.thumbnail_for_paint(entry.path), self.view,
            is_active=lambda path: path == self.active_path, spacing=spacing, title_font_size=9.0)
        self._card_delegate.show_filename = show_filenames
        self._card_delegate.rating_at_bottom = rating_at_bottom
        self.view.setItemDelegate(self._card_delegate)
        self.view.image_requested.connect(self._open_image)
        self.view.images_dropped.connect(self._add_images)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._show_context_menu)
        layout.addLayout(self._build_toolbar())
        divider = QFrame(self)
        divider.setObjectName("gradeSetToolbarDivider")
        divider.setFixedHeight(1)
        divider.setStyleSheet(f"background-color: {C.PANEL_DIVIDER}; border: none;")
        layout.addWidget(divider)
        layout.addWidget(self.message)
        layout.addWidget(self.view, 1)
        self._order.rowsInserted.connect(self._refresh_message)
        self._order.rowsRemoved.connect(self._refresh_message)
        self._model.changed.connect(self._refresh)
        self._model.failed.connect(self._show_error)
        self._refresh()

    def _build_toolbar(self) -> QHBoxLayout:
        tools = QHBoxLayout()
        tools.setSpacing(4)
        self.set_selector = ChevronComboBox(self)
        self.set_selector.setAccessibleName(_tr("title", "Grade Sets"))
        self.set_selector.setSizeAdjustPolicy(ChevronComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.set_selector.currentIndexChanged.connect(self._select_set)
        tools.addWidget(self.set_selector, 1)
        size_label = _tr("thumbnail_size", "Thumbnail Size")
        self.size_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.size_slider.setObjectName("gradeSetThumbnailSize")
        self.size_slider.setRange(_MIN_THUMBNAIL_SIZE, 192)
        self.size_slider.setValue(_DEFAULT_THUMBNAIL_SIZE)
        self.size_slider.setSingleStep(4)
        self.size_slider.setPageStep(16)
        self.size_slider.setMinimumWidth(40)
        self.size_slider.setMaximumWidth(108)
        self.size_slider.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.size_slider.setAccessibleName(size_label)
        self.size_slider.setToolTip(size_label)
        self.size_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{ height: 3px; border-radius: 1px; background: {C.BORDER}; }}
            QSlider::handle:horizontal {{ background: {C.TEXT_SECONDARY}; width: 9px;
                margin: -3px 0; border-radius: 4px; }}
            QSlider::handle:horizontal:hover, QSlider::handle:horizontal:focus {{ background: {C.ORANGE}; }}
        """)
        self.size_slider.valueChanged.connect(self._resize_cards)
        tools.addWidget(self.size_slider)
        self.filter_button = self._menu_button(_tr("filter", "Filter Photos"), "fa5s.filter")
        self.filter_button.setObjectName("gradeSetFilter")
        tools.addWidget(self.filter_button)
        filter_menu = self.filter_button.menu()
        self._format_menu = filter_menu.addMenu(_tr("format", "File Format"))
        self._formats = QActionGroup(self._format_menu)
        self._formats.triggered.connect(self._set_format_filter)
        self._refresh_format_options()
        rating_menu = filter_menu.addMenu(_tr("rating_filter", "Star Rating"))
        self._ratings = QActionGroup(rating_menu)
        self._ratings.triggered.connect(self._set_rating_filter)
        _menu_choices(rating_menu, self._ratings, (
            (_tr("all_ratings", "All Ratings"), -1), (_tr("unrated", "Unrated"), 0),
            (_tr("rated", "Rated"), 1),
            *((_tr("rating_minimum", "{rating} Stars and Up", rating=stars), stars)
              for stars in (2, 3, 4)),
            (_tr("five_stars", "5 Stars"), 5),
        ), self._order.rating)
        filter_menu.addSeparator()
        self._clear_filter_action = filter_menu.addAction(_tr("clear_filters", "Clear Filters"),
                                                          self._clear_filters)
        self._clear_filter_action.setEnabled(False)

        self.sort_button = self._menu_button(_tr("sort", "Sort Order"), "fa5s.sort-amount-down")
        self.sort_button.setObjectName("gradeSetSort")
        tools.addWidget(self.sort_button)
        sort_menu = self.sort_button.menu()
        sorts = QActionGroup(sort_menu)
        sorts.triggered.connect(self._set_order)
        _menu_choices(sort_menu, sorts, (
            (_tr("sort_added", "Order Added"), "added"),
            (_tr("sort_name", "Name A–Z"), "name"),
            (_tr("sort_name_desc", "Name Z–A"), "name_desc"),
            (_tr("sort_rating", "Highest Rating"), "rating"),
        ), self._order.order)

        self.layout_button = self._menu_button(_tr("layout", "Photo Layout"), "fa5s.th-large")
        self.layout_button.setObjectName("gradeSetLayout")
        tools.addWidget(self.layout_button)
        layout_menu = self.layout_button.menu()
        layouts = QActionGroup(layout_menu)
        layouts.triggered.connect(self._set_layout)
        _menu_choices(layout_menu, layouts, (
            (_tr("layout_adaptive", "Adaptive"), "adaptive"),
            (_tr("layout_grid", "Grid"), "grid"),
        ), self.view.layoutMode())
        return tools

    def _menu_button(self, label: str, icon: str) -> QToolButton:
        button = QToolButton(self)
        button.setMenu(QMenu(button))
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setIcon(qta.icon(icon, color=C.TEXT_SECONDARY))
        button.setIconSize(QSize(13, 13))
        button.setFixedSize(27, 23)
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.setStyleSheet(f"""
            QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 4px; }}
            QToolButton:hover, QToolButton:pressed {{ background: {C.BG_ACTIVE}; }}
            QToolButton:focus {{ border-bottom-color: {C.ORANGE}; }}
            QToolButton::menu-indicator {{ image: none; }}
        """)
        return button

    def _set_layout(self, action: QAction) -> None:
        self.view.setLayoutMode(action.data())
        self._keep_current_visible()

    def _resize_cards(self, size: int) -> None:
        self.view.setThumbnailSize(size)
        self._keep_current_visible()

    def _set_filenames_visible(self, visible: bool) -> None:
        self._card_delegate.show_filename = visible
        self._update_caption_spacing()
        self.filename_visibility_changed.emit(visible)

    def _set_rating_at_bottom(self, enabled: bool) -> None:
        self._card_delegate.rating_at_bottom = enabled
        self._update_caption_spacing()
        self.rating_at_bottom_changed.emit(enabled)

    @staticmethod
    def _caption_spacing(show_filenames: bool, rating_at_bottom: bool) -> GallerySpacing:
        spacing = _NAMED_CARD_SPACING if show_filenames else _CARD_SPACING
        if rating_at_bottom:
            return replace(spacing, caption_height=spacing.caption_height + RATING_CAPTION_HEIGHT)
        return spacing

    def _update_caption_spacing(self) -> None:
        spacing = self._caption_spacing(self._card_delegate.show_filename, self._card_delegate.rating_at_bottom)
        self._card_delegate.spacing = spacing
        self.view.setGallerySpacing(spacing)
        self._keep_current_visible()

    def _keep_current_visible(self) -> None:
        current = self.view.currentIndex()
        if current.isValid():
            self.view.scrollTo(current)

    def _refresh_format_options(self) -> None:
        current = self._order.file_format
        self._format_menu.clear()
        formats = self._images.formats()
        if current and current not in formats:
            formats.append(current)
        _menu_choices(self._format_menu, self._formats,
                      [(_tr("all_formats", "All Formats"), ""),
                       *((ext, ext) for ext in formats)], current)

    def _set_format_filter(self, action: QAction) -> None:
        self._order.file_format = action.data()
        self._apply_filters()

    def _set_rating_filter(self, action: QAction) -> None:
        self._order.rating = action.data()
        self._apply_filters()

    def _clear_filters(self) -> None:
        self._formats.actions()[0].setChecked(True)
        self._ratings.actions()[0].setChecked(True)
        self._order.file_format = ""
        self._order.rating = -1
        self._refresh_format_options()
        self._apply_filters()

    def _apply_filters(self) -> None:
        filtered = bool(self._order.file_format) or self._order.rating >= 0
        self._clear_filter_action.setEnabled(filtered)
        self.filter_button.setIcon(qta.icon("fa5s.filter", color=C.ORANGE if filtered else C.TEXT_SECONDARY))
        self._order.invalidate()
        self._refresh_message()

    def _set_order(self, action: QAction) -> None:
        self._order.order = action.data()
        self._order.sort(-1 if self._order.order == "added" else 0)
        self._order.invalidate()
        self._keep_current_visible()

    def _refresh(self) -> None:
        if self._closed:
            return
        collections = self._model.collections
        active_id = self._model.active_id
        with QSignalBlocker(self.set_selector):
            self.set_selector.clear()
            for collection in collections:
                self.set_selector.addItem(f"{collection.name} ({collection.count})", collection.id)
            self.set_selector.setCurrentIndex(self.set_selector.findData(active_id))
        self.set_selector.setToolTip(self.set_selector.currentText())
        self.set_selector.setEnabled(bool(collections))
        self.view.accept_files = active_id is not None
        paths = self._model.paths
        if active_id != self._active_id or paths != self._images.paths:
            self._cancel_thumbnail_requests()
            self._active_id = active_id
            self._images.set_paths(paths)
            self._refresh_format_options()
            active = self._path_index(self.active_path)
            if active.isValid():
                self.view.setCurrentIndex(active)
        self._refresh_message()

    def _refresh_message(self, *_args: object) -> None:
        if self._order.rowCount():
            self.message.hide()
        elif self._images.paths:
            self._show_error(_tr("no_matches", "No photos match the filters."))
        elif self._active_id is not None:
            self._show_error(_tr("empty", "Drop photos here or right-click to add them."))
        else:
            self._show_error(_tr("loading", "Loading grade sets…"))

    def _show_error(self, message: str) -> None:
        self.message.setText(message)
        self.message.setToolTip(message)
        self.message.show()

    def _select_set(self, index: int) -> None:
        if index >= 0:
            collection_id = self.set_selector.itemData(index)
            if collection_id is not None:
                self._model.select(collection_id)

    def _pick_images(self) -> None:
        patterns = " ".join(f"*{suffix}" for suffix in sorted(IMAGE_EXTENSIONS))
        paths, _filter = QFileDialog.getOpenFileNames(
            self, _tr("add_images", "Add Images…"), "", f"{_tr('images', 'Images')} ({patterns})")
        self._add_images(paths)

    def _add_images(self, paths: list[str]) -> None:
        if self._active_id is not None and paths:
            self._model.add(self._active_id, paths)

    def _open_image(self, path: str) -> None:
        self.image_requested.emit(path)

    def set_active_path(self, path: str | None) -> None:
        """Highlight only after the host accepted the image switch."""
        path = nfc(path) if path is not None else None
        if path == self.active_path:
            return
        previous = self.active_path
        self.active_path = path
        for changed in (previous, path):
            self._update_card(changed)
        self.active_photo_name_changed.emit(Path(path).name if path else "")

    def following_photo(self, path: str) -> str | None:
        """Use the same visible sort and filters as photo navigation."""
        index = self._order.mapFromSource(self._images.path_index(path))
        count = self._order.rowCount()
        if not index.isValid() or count < 2:
            return None
        return self._order.index((index.row() + 1) % count, 0).data(_PATH_ROLE)

    def _show_context_menu(self, pos: QPoint) -> None:
        menu = self._build_context_menu(pos)
        if menu is not None:
            menu.exec(self.view.viewport().mapToGlobal(pos))
            menu.deleteLater()

    def set_grade_clipboard_available(self, available: bool) -> None:
        self._grade_clipboard_available = bool(available)

    def _build_context_menu(self, pos: QPoint) -> QMenu | None:
        collection_id = self._active_id
        if collection_id is None:
            return None
        selected = self.view.selectedIndexes()
        under_mouse = self.view.indexAt(pos)
        if under_mouse.isValid() and under_mouse not in selected:
            selected = [under_mouse]
        paths = [index.data(_PATH_ROLE) for index in sorted(selected, key=lambda index: index.row())]
        menu = QMenu(self)
        add = menu.addAction(_tr("add_images", "Add Images…"), self._pick_images)
        add.setObjectName("gradeSetAddImages")
        show_names = menu.addAction(_tr("show_filenames", "Show File Names"))
        show_names.setObjectName("gradeSetShowFilenames")
        show_names.setCheckable(True)
        show_names.setChecked(self._card_delegate.show_filename)
        show_names.toggled.connect(self._set_filenames_visible)
        rating_at_bottom = menu.addAction(_tr("rating_at_bottom", "Show Rating Below Card"))
        rating_at_bottom.setObjectName("gradeSetRatingAtBottom")
        rating_at_bottom.setCheckable(True)
        rating_at_bottom.setChecked(self._card_delegate.rating_at_bottom)
        rating_at_bottom.toggled.connect(self._set_rating_at_bottom)
        target = under_mouse.data(_PATH_ROLE)
        if target:
            menu.addSeparator()
            copy = menu.addAction(_tr("copy_grade", "Copy This Photo’s Grade"),
                                  lambda: self.copy_grade_requested.emit(target))
            copy.setObjectName("gradeSetCopyGrade")
            copy.setEnabled(target == self.active_path)
            paste = menu.addAction(_tr("paste_grade", "Paste Grade to This Photo"),
                                   lambda: self.paste_grade_requested.emit(target))
            paste.setObjectName("gradeSetPasteGrade")
            paste.setEnabled(self._grade_clipboard_available)
        if paths:
            menu.addSeparator()
            enqueue = menu.addAction(_tr("add_to_export_queue", "Add to Export Queue"),
                                     lambda: self.export_queue_requested.emit(paths))
            enqueue.setObjectName("gradeSetAddToExportQueue")
            menu.addAction(_tr("remove", "Remove from Grade Set"),
                           lambda: self._model.remove(collection_id, paths))
        return menu

    def _update_card(self, path: str | None) -> None:
        index = self._path_index(path)
        if index.isValid():
            self.view.update(index)

    def _path_index(self, path: str | None) -> QModelIndex:
        return self._order.mapFromSource(self._images.path_index(path))

    def thumbnail_for_paint(self, path: str) -> QImage | None:
        for cache in (self._graded_previews, self._source_previews):
            if path in cache:
                cache.move_to_end(path)
                return cache[path]
        if not self._closed and path not in self._pending and path not in self._failed:
            self._candidates[path] = None
            if not self._request_timer.isActive():
                self._request_timer.start(0)
        return None

    def _request_visible_thumbnails(self) -> None:
        if self._closed or not self.view.isVisible():
            self._candidates.clear()
            return
        viewport_rect = self.view.viewport().rect()
        for path in tuple(self._candidates):
            index = self._path_index(path)
            if (not index.isValid() or not self.view.visualRect(index).intersects(viewport_rect)
                    or path in self._graded_previews or path in self._source_previews):
                self._candidates.pop(path, None)
                continue
            if len(self._pending) >= _MAX_PENDING_THUMBNAILS:
                continue
            self._candidates.pop(path, None)
            if self._transport is None:
                self._transport = NativeFilesTransport(
                    lambda: "", None, self, facade_workers=2, prefer_graded_thumbnails=True)
                self._transport.thumbnail_loaded.connect(self._thumbnail_loaded)
                self._transport.thumbnail_failed.connect(self._thumbnail_failed)
            self._pending.add(path)
            self._transport.request_thumbnail(path)

    def _thumbnail_loaded(self, path: str, raw: bytes) -> None:
        if path not in self._pending:
            return
        image = QImage.fromData(raw)
        if image.isNull():
            self._thumbnail_failed(path)
            return
        self._pending.remove(path)
        graded = is_webp_preview(raw) or raw.startswith(GRADED_PREVIEW_JPEG_HEADER)
        self._cache(self._source_previews, path, image, graded=graded)
        self.view.setAspectRatio(path, image.width() / image.height())
        self._update_card(path)
        self._request_timer.start(0)

    def _thumbnail_failed(self, path: str) -> None:
        if path in self._pending:
            self._pending.remove(path)
            self._failed.add(path)
            self._request_timer.start(0)

    def _cache(self, cache: OrderedDict[str, QImage], path: str, image: QImage, *, graded: bool) -> None:
        edge = GRADED_PREVIEW_MAX_EDGE if graded else _THUMBNAIL_SIZE
        size = QSize(edge, edge)
        if image.width() > size.width() or image.height() > size.height():
            image = image.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        elif not graded or image.format() == QImage.Format.Format_RGB888:
            # Rendered previews may wrap a temporary pixel buffer.
            image = image.copy()
        if graded:
            image = image.convertToFormat(QImage.Format.Format_RGB888)
        cache[path] = image
        cache.move_to_end(path)
        if len(cache) > _MAX_CACHED_THUMBNAILS:
            evicted, _image = cache.popitem(last=False)
            if cache is self._source_previews and self._transport is not None:
                self._transport.release_thumbnail(evicted)

    def set_graded_thumbnail(self, path: str, image: QImage) -> None:
        """Cache a display image from the established render path."""
        if not self._closed and not image.isNull():
            path = nfc(path)
            self._cache(self._graded_previews, path, image, graded=True)
            self.view.set_initial_aspect(path, image)
            self._update_card(path)

    def invalidate_graded_thumbnail(self, path: str) -> None:
        """Discard a preview after its underlying grade changes out of view."""
        path = nfc(path)
        self._graded_previews.pop(path, None)
        self._update_card(path)

    def _cancel_thumbnail_requests(self) -> None:
        self._request_timer.stop()
        self._pending.clear()
        self._failed.clear()
        self._candidates.clear()
        if self._transport is not None:
            self._transport.cancel()

    def hideEvent(self, event: QHideEvent) -> None:
        self._cancel_thumbnail_requests()
        super().hideEvent(event)

    def shutdown(self) -> None:
        self._closed = True
        self._cancel_thumbnail_requests()
        if self._transport is not None:
            self._transport.shutdown()
