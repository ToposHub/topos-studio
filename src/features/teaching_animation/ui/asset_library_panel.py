"""MG 素材库面板 — categorized preset browser with live animated GIF thumbnails.

Browses the merged preset library (bundled + user) and manages the library on
disk: import/export zips, delete user presets, regenerate previews.  Each
card plays its looping ``preview.gif`` directly in the grid, but ONLY while
the card intersects the viewport — QMovie decoding for off-screen cards is
stopped so a full library costs no more than one screen of animation.
Presets enter the scene by dragging them onto the canvas
(``MG_PRESET_MIME``); double-click inserts at the canvas centre through the
same path.

The filter row carries the thumbnail zoom slider (persisted via QSettings);
search collapses into an icon and expands into its own row on click.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from PyQt6.QtCore import QEvent, QByteArray, QSettings, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon, QMovie
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..presets import (
    MG_PRESET_MIME,
    MgPresetLibrary,
    PresetError,
    load_preset_scene,
    render_preset_gif,
)
from src.shared.assets.icon_manager import IconColors, get_icon
from src.shared.i18n.i18n import I18N

_CATEGORY_LABELS = {
    "backgrounds": ("motion_graphic.library.cat.backgrounds", "Backgrounds"),
    "charts": ("motion_graphic.library.cat.charts", "Charts"),
    "text": ("motion_graphic.library.cat.text", "Text FX"),
    "particles": ("motion_graphic.library.cat.particles", "Particles"),
    "light": ("motion_graphic.library.cat.light", "Light FX"),
    "shapes3d": ("motion_graphic.library.cat.shapes3d", "3D Shapes"),
    "general": ("motion_graphic.library.cat.general", "General"),
    "html_templates": ("motion_graphic.library.cat.html_templates", "HTML Templates"),
}
#: Card layout.  Preview GIFs are 16:9 (320x180, see presets/preview.py), so
#: the card height derives from the computed width and QMovie's scaled frames
#: stay distortion-free.  The slider picks the COLUMN COUNT, not a pixel
#: size: card width = viewport width / columns, so cards always tile the full
#: panel width — no dead strip on either side, and no "one huge card + blank
#: half panel" dead zone at the top of the range (every position is a
#: visibly different packing).
_GRID_SPACING = 3           # scrollbar-only; pitch itself carries no gap
_CARD_GAP = 6               # visual breathing room around each card (the
                            # icon is inset by this within its grid cell)
_CAPTION_PIXELS = 10        # card-name font; must match the QSS rule — see
                            # the comment in motion_graphic_style.py
_THUMB_ASPECT = 9 / 16
_ZOOM_COLUMNS_MIN = 1
_ZOOM_COLUMNS_MAX = 8
_COLUMNS_DEFAULT = 4

#: Extra item-data slots beyond the preset-id/kind pair in _PresetGrid.mimeData.
_PREVIEW_ROLE = Qt.ItemDataRole.UserRole + 2

_SETTINGS_ORG = "ToposColor"
_SETTINGS_KEY = "MGLibraryPanel"
_SETTINGS_COLUMNS = "thumbnail_columns"


def category_label(category: str) -> str:
    key, default = _CATEGORY_LABELS.get(category, ("", category.replace("_", " ").title()))
    return I18N.tr(key, default) if key else default


class _PresetGrid(QListWidget):
    """Icon-mode grid whose drags carry the preset mime payload."""

    def mimeData(self, items: list[QListWidgetItem]) -> Any:  # noqa: N802 — Qt API
        mime = super().mimeData(items)
        for item in items:
            preset_id = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(preset_id, str):
                kind = item.data(Qt.ItemDataRole.UserRole + 1) or "scene_preset"
                payload = json.dumps({"preset_id": preset_id, "kind": str(kind)}).encode("utf-8")
                mime.setData(MG_PRESET_MIME, QByteArray(payload))
                mime.setText(item.text())
                break
        return mime


class PresetSaveDialog(QDialog):
    """Collect name / category / tags / description for 'Save as Preset'."""

    def __init__(self, parent: QWidget | None, *, categories: Sequence[str],
                 default_name: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(I18N.tr("motion_graphic.library.save_title", "Save as Preset"))
        form = QFormLayout(self)
        self.name_edit = QLineEdit(default_name.strip() or "My Preset", self)
        self.category_combo = QComboBox(self)
        self.category_combo.setEditable(True)
        known = list(categories) or ["general"]
        self.category_combo.addItems(known)
        if "general" not in known:
            self.category_combo.addItem("general")
        self.tags_edit = QLineEdit(self)
        self.tags_edit.setPlaceholderText(I18N.tr(
            "motion_graphic.library.tags_hint", "comma separated"))
        self.description_edit = QPlainTextEdit(self)
        self.description_edit.setFixedHeight(72)
        form.addRow(I18N.tr("motion_graphic.library.name", "Name"), self.name_edit)
        form.addRow(I18N.tr("motion_graphic.library.category", "Category"), self.category_combo)
        form.addRow(I18N.tr("motion_graphic.library.tags", "Tags"), self.tags_edit)
        form.addRow(I18N.tr("motion_graphic.library.description", "Description"), self.description_edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def payload(self) -> dict[str, Any]:
        tags = [tag.strip() for tag in self.tags_edit.text().split(",") if tag.strip()]
        return {
            "name": self.name_edit.text().strip(),
            "category": self.category_combo.currentText().strip() or "general",
            "tags": tags,
            "description": self.description_edit.toPlainText().strip(),
        }


class _PreviewRenderJob(QThread):
    """Render one preset's preview GIF off the UI thread (AUD-30600).

    ``render_preset_gif`` is a synchronous pure-CPU path — SVG rasterization
    at scene resolution (up to 4K), LANCZOS scaling and GIF encoding over up
    to 24 frames — which used to run directly in the tool button's slot and
    froze the whole editor for seconds.  The job reports through queued
    signals so Qt widgets are only touched on the UI thread; the panel holds
    the reference (unparented, so a panel closed mid-render never destroys a
    running thread) and clears it when the result lands.
    """

    succeeded = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, manifest: Any, folder: Path, out_path: Path) -> None:
        super().__init__()
        self._manifest = manifest
        self._folder = Path(folder)
        self._out_path = Path(out_path)

    def run(self) -> None:
        try:
            scene = load_preset_scene(self._manifest, self._folder)
            render_preset_gif(scene, self._out_path)
        except Exception as exc:  # noqa: BLE001 — any failure is surfaced on the UI thread
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit()


class AssetLibraryPanel(QWidget):
    """Categorized, searchable preset browser with drag-to-canvas support.

    Cards animate their preview GIF only while visible (viewport culling);
    the filter row hosts the thumbnail zoom slider, and search collapses
    behind a toggle icon that expands into the row beneath it.
    """

    presetActivated = pyqtSignal(str)  # double-click insert at canvas centre
    templateActivated = pyqtSignal(str)  # double-click on an HTML template package

    def __init__(self, parent: QWidget | None = None,
                 library: MgPresetLibrary | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("mgLibraryPanel")
        self._library = library if library is not None else MgPresetLibrary()
        # Live thumbnail animations, keyed by grid row (QListWidgetItem is
        # unhashable in PyQt6; rows are stable between refills and the map is
        # always cleared before grid.clear() rebuilds the rows).  Membership
        # tracks viewport visibility exactly (see _sync_thumbnail_movies).
        self._thumb_movies: dict[int, QMovie] = {}
        # In-flight off-thread preview regeneration (AUD-30600); None when idle.
        self._preview_job: _PreviewRenderJob | None = None
        # R4: one catalog over presets + HTML template packages.  Read-only;
        # save/delete/import stay owned by their systems.
        from ..core.mg_package_registry import MgPackageCatalog
        self._catalog = MgPackageCatalog(presets=self._library)
        self._catalog.refresh()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel(I18N.tr("motion_graphic.panels.library", "Library"), self)
        title.setObjectName("mgSectionTitle")
        header.addWidget(title)
        header.addStretch(1)
        self._refresh_button = self._tool_button("fa5s.sync-alt", "refresh", self._rescan)
        self._import_button = self._tool_button("fa5s.file-import", "import", self._import_triggered)
        self._export_button = self._tool_button("fa5s.file-export", "export", self._export_triggered)
        self._regenerate_button = self._tool_button("fa5s.film", "re-render preview",
                                                    self._regenerate_preview)
        self._delete_button = self._tool_button("fa5s.trash-alt", "delete", self._delete_triggered)
        for button in (self._refresh_button, self._import_button, self._export_button,
                       self._regenerate_button, self._delete_button):
            header.addWidget(button)
        layout.addLayout(header)

        # Filter row: category + zoom slider (where the search field used to
        # live) + collapsed search toggle.
        filters = QHBoxLayout()
        self._category_combo = QComboBox(self)
        self._category_combo.setObjectName("mgLibraryCategory")
        self._zoom_slider = QSlider(Qt.Orientation.Horizontal, self)
        self._zoom_slider.setObjectName("mgLibraryZoom")
        self._zoom_slider.setRange(_ZOOM_COLUMNS_MIN, _ZOOM_COLUMNS_MAX)
        self._columns = self._stored_columns()
        self._zoom_slider.setValue(self._columns)
        self._zoom_slider.setToolTip(I18N.tr(
            "motion_graphic.library.zoom_hint", "Thumbnail size"))
        self._zoom_slider.valueChanged.connect(self._apply_thumbnail_columns)
        self._zoom_slider.sliderReleased.connect(self._persist_thumbnail_zoom)
        self._search_toggle = QToolButton(self)
        self._search_toggle.setObjectName("mgLibrarySearchToggle")
        self._search_toggle.setIcon(get_icon("fa5s.search", IconColors.DEFAULT))
        self._search_toggle.setCheckable(True)
        self._search_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._search_toggle.setToolTip(I18N.tr(
            "motion_graphic.library.search_hint", "Search presets…"))
        self._search_toggle.toggled.connect(self._set_search_expanded)
        filters.addWidget(self._category_combo, 1)
        filters.addWidget(self._zoom_slider, 1)
        filters.addWidget(self._search_toggle)
        layout.addLayout(filters)

        # Search expands into its own row beneath the filters on demand.
        self._search_row = QWidget(self)
        self._search_row.setObjectName("mgLibrarySearchRow")
        search_layout = QHBoxLayout(self._search_row)
        search_layout.setContentsMargins(0, 0, 0, 0)
        self._search_edit = QLineEdit(self._search_row)
        self._search_edit.setPlaceholderText(I18N.tr(
            "motion_graphic.library.search_hint", "Search presets…"))
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.installEventFilter(self)
        search_layout.addWidget(self._search_edit)
        self._search_row.setVisible(False)
        layout.addWidget(self._search_row)

        self.grid = _PresetGrid(self)
        self.grid.setObjectName("mgLibraryGrid")
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setMovement(QListWidget.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(_GRID_SPACING)
        self.grid.setWordWrap(True)
        # Scrollbar policy is the load-bearing part of the flush-right
        # layout: an on-demand vertical scrollbar changes the viewport width
        # whenever it toggles, which would both leave a blank strip and set
        # up resize→retile→resize oscillation.  Always-on keeps the viewport
        # width constant; the (never needed) horizontal one is fully off so
        # a flooring remainder can't spawn a phantom h-bar.
        self.grid.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid.setDragEnabled(True)
        self.grid.setDragDropMode(QListWidget.DragDropMode.DragOnly)
        self.grid.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.grid.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.grid.itemDoubleClicked.connect(self._item_activated)
        self.grid.itemSelectionChanged.connect(self._on_selection_changed)
        # Scroll/resize can move cards in and out of the viewport; both must
        # re-run the thumbnail movie culling pass.
        self.grid.verticalScrollBar().valueChanged.connect(self._schedule_thumb_sync)
        self.grid.horizontalScrollBar().valueChanged.connect(self._schedule_thumb_sync)
        self.grid.installEventFilter(self)
        # The viewport (not just the grid widget) must be watched: scrollbar
        # geometry changes resize the viewport child directly.
        self.grid.viewport().installEventFilter(self)
        # Coalescing single-shot timer, parented here so a pending sync can
        # never fire into a destroyed panel when the editor window closes.
        self._thumb_sync_timer = QTimer(self)
        self._thumb_sync_timer.setSingleShot(True)
        self._thumb_sync_timer.setInterval(0)
        self._thumb_sync_timer.timeout.connect(self._sync_thumbnail_movies)
        # Re-tiling is deferred one event-loop turn: an event filter sees the
        # grid's Resize BEFORE QAbstractScrollArea has resized the viewport,
        # so reading viewport().width() synchronously would compute cells
        # from the stale pre-resize width.
        self._retile_timer = QTimer(self)
        self._retile_timer.setSingleShot(True)
        self._retile_timer.setInterval(0)
        self._retile_timer.timeout.connect(
            lambda: self._apply_thumbnail_columns(self._columns))
        layout.addWidget(self.grid, 1)
        # The card caption font is set in code, not QSS: font-size on the
        # ::item sub-control does not reliably propagate to item text, which
        # is why the names kept rendering at the default body size.
        caption_font = self.grid.font()
        caption_font.setPixelSize(_CAPTION_PIXELS)
        self.grid.setFont(caption_font)
        # Caption strip = two wrapped lines of the real caption font plus
        # padding.  Deriving it from metrics (instead of a hardcoded 24px,
        # which clipped 8px text on macOS metrics) is what keeps the next
        # row's card from covering a two-line name.
        from PyQt6.QtGui import QFontMetrics
        self._caption_reserve = QFontMetrics(caption_font).lineSpacing() * 2 + 8
        self._apply_thumbnail_columns(self._columns)

        self._status_label = QLabel("", self)
        self._status_label.setObjectName("mgMutedLabel")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._category_combo.currentIndexChanged.connect(lambda _index: self._refill())
        self._search_edit.textChanged.connect(lambda _text: self._refill())
        self._rebuild_categories()
        self._refill()

    # -- setup helpers -----------------------------------------------------

    @staticmethod
    def _stored_columns() -> int:
        stored = QSettings(_SETTINGS_ORG, _SETTINGS_KEY).value(
            _SETTINGS_COLUMNS, _COLUMNS_DEFAULT)
        try:
            columns = int(stored)
        except (TypeError, ValueError):
            return _COLUMNS_DEFAULT
        return max(_ZOOM_COLUMNS_MIN, min(_ZOOM_COLUMNS_MAX, columns))

    def _persist_thumbnail_zoom(self) -> None:
        QSettings(_SETTINGS_ORG, _SETTINGS_KEY).setValue(
            _SETTINGS_COLUMNS, self._columns)

    def _apply_thumbnail_columns(self, columns: int) -> None:
        """Tile ``columns`` cards across the exact viewport width.

        Icon-mode pitch equals the gridSize width (Qt adds no inter-column
        spacing once a gridSize is set), so flooring the viewport width by
        the column count fills each row flush with the right edge — the
        residue is below ``columns`` px.  ``doItemsLayout`` afterwards drops
        the per-item cached size hints from the previous card size, which
        otherwise keep stale (smaller) visual rects centred inside the new
        cells.
        """
        self._columns = max(_ZOOM_COLUMNS_MIN, min(_ZOOM_COLUMNS_MAX, int(columns)))
        # A not-yet-shown panel still reports a stub viewport; keep cells
        # sane until the first real Resize re-applies this with live width.
        width = max(self._columns * 40, self.grid.viewport().width())
        # Two px of slack: the icon-mode flow wraps a column whose right
        # edge lands exactly on the viewport edge, so an exact division
        # (e.g. 471/3 = 157) would drop the last card of each row.
        cell_w = max(24 + _CARD_GAP, (width - 2) // self._columns)
        # The gap lives inside the pitch: the icon (and its live QMovie
        # frames) is inset by _CARD_GAP, so the centered decoration leaves
        # gap/2 on each side and the extra cell height pads rows apart.
        icon = QSize(cell_w - _CARD_GAP,
                     round((cell_w - _CARD_GAP) * _THUMB_ASPECT))
        self.grid.setIconSize(icon)
        self.grid.setGridSize(
            QSize(cell_w, icon.height() + self._caption_reserve + _CARD_GAP))
        self._apply_item_size_hints()
        self.grid.doItemsLayout()
        self._zoom_slider.setToolTip(I18N.tr(
            "motion_graphic.library.zoom_columns",
            "Thumbnails: {count} columns", count=self._columns))
        for movie in self._thumb_movies.values():
            movie.setScaledSize(icon)
        self._schedule_thumb_sync()

    def _apply_item_size_hints(self) -> None:
        """Pin every item's rect to the full grid cell.

        Without explicit hints the view derives item rects from the iconSize
        that was current when the item was created (cached further by
        ``uniformItemSizes``), so after a re-tile the big cells contain small
        centred icons and the row ends far short of the right edge — exactly
        the "blank right side" symptom.  gridSize still governs the pitch;
        the hint only fixes each item's painted rect.
        """
        cell = self.grid.gridSize()
        for row in range(self.grid.count()):
            self.grid.item(row).setSizeHint(cell)

    def _set_search_expanded(self, expanded: bool) -> None:
        self._search_row.setVisible(expanded)
        if expanded:
            self._search_edit.setFocus()
            self._search_edit.selectAll()
        else:
            # Collapsing also drops the filter so the grid never stays
            # silently filtered behind a hidden field.
            self._search_edit.clear()

    def eventFilter(self, obj: Any, event: Any) -> bool:
        if event.type() == QEvent.Type.Resize and obj in (self.grid, self.grid.viewport()):
            # Re-tile on every width change (dock resized, window floated,
            # scrollbar geometry change …) so cards always end flush with the
            # right edge.  Any pending movie sync is cancelled first: it
            # would run against the stale pre-resize cells and transiently
            # start movies for cards that the re-tile pushes off-screen.
            # _apply_thumbnail_columns re-schedules the sync once the new
            # geometry is in place.  Deferred because an event filter sees
            # Resize before QAbstractScrollArea has resized the viewport.
            self._thumb_sync_timer.stop()
            self._retile_timer.start()
        elif (obj is self._search_edit and event.type() == QEvent.Type.KeyPress
                and event.key() == Qt.Key.Key_Escape):
            self._search_toggle.setChecked(False)
            return True
        return super().eventFilter(obj, event)

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        # A hidden panel may have missed a project switch; follow the default
        # root set (project-local mg_presets/ comes and goes with projects)
        # before laying out, then rebuild only when the roots really moved.
        if self._library.refresh_roots():
            self.refresh()
        # Route through the re-tile so the first movie sync observes the
        # laid-out geometry (show may or may not deliver a Resize, depending
        # on whether the size changed while hidden).
        self._retile_timer.start()

    def hideEvent(self, event: Any) -> None:
        self._clear_thumbnail_movies()
        self._persist_thumbnail_zoom()
        super().hideEvent(event)

    def _tool_button(self, icon_name: str, tooltip: str, handler: Any) -> QToolButton:
        button = QToolButton(self)
        button.setIcon(get_icon(icon_name, IconColors.DEFAULT))
        button.setToolTip(I18N.tr(f"motion_graphic.library.{tooltip}", tooltip.title()))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(handler)
        return button

    @property
    def library(self) -> MgPresetLibrary:
        return self._library

    def current_preset_id(self) -> str | None:
        item = self.grid.currentItem()
        if item is None:
            return None
        preset_id = item.data(Qt.ItemDataRole.UserRole)
        return str(preset_id) if isinstance(preset_id, str) else None

    def select_preset(self, preset_id: str) -> None:
        for row in range(self.grid.count()):
            item = self.grid.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == preset_id:
                self.grid.setCurrentItem(item)
                return

    # -- browsing ----------------------------------------------------------

    def refresh(self) -> None:
        """Rescan disk and rebuild the browsing UI, keeping the selection."""
        current = self.current_preset_id()
        self._rebuild_categories()
        self._refill()
        if current:
            self.select_preset(current)

    @property
    def catalog(self):
        """The unified package catalog (presets + HTML templates)."""
        return self._catalog

    def _rescan(self, checked: bool = False) -> None:
        del checked
        self._library.refresh_roots()
        count = self._library.rescan()
        self._catalog.refresh()
        self.refresh()
        self._set_status(I18N.tr("motion_graphic.library.rescanned",
                                 "Library rescanned: {count} presets", count=count))

    def _rebuild_categories(self) -> None:
        counts = self._library.categories()
        templates = self._catalog.packages(kind="html_template")
        blocked = self._category_combo.blockSignals(True)
        try:
            self._category_combo.clear()
            self._category_combo.addItem(I18N.tr("motion_graphic.library.all", "All Categories"), "")
            for category, count in counts.items():
                self._category_combo.addItem(f"{category_label(category)} ({count})", category)
            if templates:
                self._category_combo.addItem(
                    f"{category_label('html_templates')} ({len(templates)})", "html_templates")
        finally:
            self._category_combo.blockSignals(blocked)

    def _item_activated(self, item: QListWidgetItem) -> None:
        package_id = str(item.data(Qt.ItemDataRole.UserRole))
        kind = str(item.data(Qt.ItemDataRole.UserRole + 1) or "scene_preset")
        if kind == "html_template":
            self.templateActivated.emit(package_id)
        else:
            self.presetActivated.emit(package_id)

    def _append_template_items(self, wanted: str | None) -> None:
        """List HTML template packages under their unified category."""
        from ..core.mg_package_registry import KIND_HTML_TEMPLATE
        if wanted not in (None, "", "html_templates"):
            return
        needle = self._search_edit.text()
        for package in self._catalog.search(needle, kind=KIND_HTML_TEMPLATE):
            item = QListWidgetItem(package.display_name)
            item.setData(Qt.ItemDataRole.UserRole, package.package_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, KIND_HTML_TEMPLATE)
            tooltip = package.description or package.display_name
            tooltip += f"\n[{package.kind}] v{package.version}"
            if package.preview_path is not None:
                # Never leak the developer-machine absolute path; a stable
                # marker is all the tooltip needs.
                tooltip += "\n" + I18N.tr(
                    "motion_graphic.library.preview_available", "preview available")
            item.setToolTip(tooltip)
            preview = Path(package.preview_path) if package.preview_path else None
            item.setIcon(self._static_preview_icon(preview, fallback="fa5s.globe"))
            self._attach_preview(item, preview)
            self.grid.addItem(item)

    def _attach_preview(self, item: QListWidgetItem,
                        preview: Path | None) -> None:
        """Record the preview GIF for live thumbnail playback."""
        if preview is not None and preview.is_file():
            item.setData(_PREVIEW_ROLE, str(preview))

    def _static_preview_icon(self, preview: Path | None,
                             fallback: str = "fa5s.shapes") -> QIcon:
        """First-frame thumbnail used until the live QMovie takes over."""
        if preview is not None and preview.is_file():
            movie = QMovie(str(preview))
            movie.jumpToFrame(0)
            pixmap = movie.currentPixmap()
            if not pixmap.isNull():
                icon = QIcon(pixmap.scaled(
                    self.grid.iconSize(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation))
                movie.deleteLater()
                return icon
            movie.deleteLater()
        return QIcon(get_icon(fallback, IconColors.ORANGE).pixmap(self.grid.iconSize()))

    # -- live thumbnail playback (viewport-culled) --------------------------

    def _schedule_thumb_sync(self, *args: Any) -> None:
        # Deferred + coalesced so list relayout (resize/refill/scroll bursts)
        # settles first and one burst costs one sync.
        if not self._thumb_sync_timer.isActive():
            self._thumb_sync_timer.start()

    def _clear_thumbnail_movies(self) -> None:
        # Must run before grid.clear(): items are C++-owned and die with it.
        for movie in self._thumb_movies.values():
            movie.stop()
            movie.deleteLater()
        self._thumb_movies.clear()

    def _sync_thumbnail_movies(self) -> None:
        """Play preview GIFs only for cards intersecting the viewport.

        Off-screen cards keep their static first-frame icon; their QMovie is
        stopped and destroyed so a large library never decodes more than one
        screen of animation at a time.
        """
        if not self.isVisible():
            self._clear_thumbnail_movies()
            return
        viewport = self.grid.viewport().rect()
        icon_size = self.grid.iconSize()
        visible: set[int] = set()
        for row in range(self.grid.count()):
            item = self.grid.item(row)
            if not self.grid.visualItemRect(item).intersects(viewport):
                continue
            visible.add(row)
            if row in self._thumb_movies:
                continue
            preview = item.data(_PREVIEW_ROLE)
            if not preview:
                continue
            movie = QMovie(str(preview), QByteArray(), self)
            movie.setScaledSize(icon_size)
            movie.frameChanged.connect(
                lambda _frame, item=item, movie=movie:
                    item.setIcon(QIcon(movie.currentPixmap())))
            movie.start()
            self._thumb_movies[row] = movie
        for row in [entry for entry in self._thumb_movies if entry not in visible]:
            movie = self._thumb_movies.pop(row)
            movie.stop()
            movie.deleteLater()

    def _refresh_item_thumbnail(self) -> None:
        """Reload the current card's icon after its GIF was re-rendered."""
        item = self.grid.currentItem()
        if item is None:
            return
        preview = item.data(_PREVIEW_ROLE)
        path = Path(str(preview)) if preview else None
        item.setIcon(self._static_preview_icon(path))
        movie = self._thumb_movies.pop(self.grid.currentRow(), None)
        if movie is not None:
            movie.stop()
            movie.deleteLater()
        self._sync_thumbnail_movies()

    def _refill(self) -> None:
        category = self._category_combo.currentData()
        wanted = category if isinstance(category, str) and category else None
        self._clear_thumbnail_movies()
        self.grid.clear()
        self._append_template_items(wanted)
        records = self._library.search(self._search_edit.text(), wanted)
        from ..presets.manifest import localized_preset_description, localized_preset_name
        for record in records:
            name = localized_preset_name(record.manifest)
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, record.preset_id)
            tooltip = localized_preset_description(record.manifest) or name
            tags = " · ".join(record.manifest.tags)
            if tags:
                tooltip = f"{tooltip}\n{tags}"
            tooltip += f"\n[{record.origin}] {record.folder}"
            item.setToolTip(tooltip)
            preview = Path(record.preview_path) if record.preview_path else None
            item.setIcon(self._static_preview_icon(preview))
            self._attach_preview(item, preview)
            self.grid.addItem(item)
        if self.grid.count():
            self.grid.setCurrentRow(0)
        else:
            self._on_selection_changed()
        self._apply_item_size_hints()
        total = len(self._library.records()) + len(self._catalog.packages(kind="html_template"))
        self._set_status(I18N.tr("motion_graphic.library.count",
                                 "{shown} shown · {total} packages in library",
                                 shown=self.grid.count(), total=total))
        self._schedule_thumb_sync()

    def _current_kind(self) -> str:
        """The selected card's package kind (preset vs HTML template)."""
        item = self.grid.currentItem()
        if item is None:
            return ""
        kind = item.data(Qt.ItemDataRole.UserRole + 1)
        return str(kind) if kind else "scene_preset"

    def _on_selection_changed(self) -> None:
        """Toggle the disk-management buttons for the current card.

        The bottom preview pane and Insert button are gone — cards carry
        their animated preview inline and enter the scene by dragging — so
        selection now only decides whether the preset-only management
        actions (delete/export/regenerate) have a target.  HTML template
        packages stay read-only until they share the write protocol (R-06).
        """
        preset_id = self.current_preset_id()
        is_preset = preset_id is not None and self._current_kind() != "html_template"
        has_record = is_preset and self._library.get(preset_id) is not None
        self._delete_button.setEnabled(has_record)
        self._export_button.setEnabled(has_record)
        self._regenerate_button.setEnabled(has_record)

    # -- actions -----------------------------------------------------------

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)

    def _import_triggered(self, checked: bool = False) -> None:
        del checked
        path, _filter = QFileDialog.getOpenFileName(
            self, I18N.tr("motion_graphic.library.import_title", "Import Preset Archive"),
            str(Path.home()), "Preset Archives (*.zip);;All Files (*)")
        if not path:
            return
        try:
            record = self._library.import_from(Path(path))
        except PresetError as exc:
            QMessageBox.warning(self, I18N.tr("motion_graphic.library.import_failed",
                                              "Import Failed"), str(exc))
            return
        self.refresh()
        self.select_preset(record.preset_id)
        self._set_status(I18N.tr("motion_graphic.library.imported",
                                 "Imported preset: {name}", name=record.manifest.name))

    def _export_triggered(self, checked: bool = False) -> None:
        del checked
        preset_id = self.current_preset_id()
        if preset_id is None:
            return
        default = Path.home() / f"{preset_id}.zip"
        path, _filter = QFileDialog.getSaveFileName(
            self, I18N.tr("motion_graphic.library.export_title", "Export Preset"),
            str(default), "Preset Archives (*.zip)")
        if not path:
            return
        try:
            target = self._library.export_to(Path(path), [preset_id])
        except PresetError as exc:
            QMessageBox.warning(self, I18N.tr("motion_graphic.library.export_failed",
                                              "Export Failed"), str(exc))
            return
        self._set_status(I18N.tr("motion_graphic.library.exported",
                                 "Exported: {path}", path=target))

    def _delete_triggered(self, checked: bool = False) -> None:
        del checked
        preset_id = self.current_preset_id()
        if preset_id is None:
            return
        record = self._library.get(preset_id)
        if record is None:
            return
        confirm = QMessageBox.question(
            self, I18N.tr("motion_graphic.library.delete_title", "Delete Preset"),
            I18N.tr("motion_graphic.library.delete_confirm",
                    "Delete preset '{name}' from disk?", name=record.manifest.name))
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self._library.delete(preset_id)
        except PresetError as exc:
            QMessageBox.warning(self, I18N.tr("motion_graphic.library.delete_failed",
                                              "Delete Failed"), str(exc))
            return
        self.refresh()
        self._set_status(I18N.tr("motion_graphic.library.deleted",
                                 "Deleted preset: {id}", id=preset_id))

    def _regenerate_preview(self, checked: bool = False) -> None:
        """Re-render the selected preset's GIF on a worker thread (AUD-30600)."""
        del checked
        preset_id = self.current_preset_id()
        if preset_id is None:
            return
        record = self._library.get(preset_id)
        if record is None:
            return
        if self._preview_job is not None:
            return  # a regeneration is already running (button is disabled)
        job = _PreviewRenderJob(
            record.manifest, record.folder, record.folder / record.manifest.preview)
        job.succeeded.connect(self._preview_regenerated)
        job.failed.connect(self._preview_regenerate_failed)
        self._preview_job = job
        self._regenerate_button.setEnabled(False)
        self._set_status(I18N.tr("motion_graphic.library.preview_rebuilding",
                                 "Rebuilding preview for {id}…", id=preset_id))
        job.start()

    def _preview_regenerated(self) -> None:
        preset_id = self.current_preset_id()
        self._finish_preview_job()
        self._refresh_item_thumbnail()
        self._set_status(I18N.tr("motion_graphic.library.preview_rebuilt",
                                 "Preview regenerated for {id}",
                                 id=preset_id if preset_id is not None else ""))

    def _preview_regenerate_failed(self, message: str) -> None:
        self._finish_preview_job()
        QMessageBox.warning(self, I18N.tr("motion_graphic.library.preview_failed",
                                          "Preview Failed"), message)

    def _finish_preview_job(self) -> None:
        """Clear the finished job and restore the management buttons."""
        job = self._preview_job
        self._preview_job = None
        if job is not None:
            job.wait()
            job.deleteLater()
        # Restore per-selection enablement (the job disabled the button).
        self._on_selection_changed()


__all__ = ["AssetLibraryPanel", "PresetSaveDialog", "category_label"]
