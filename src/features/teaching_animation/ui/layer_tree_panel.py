"""UX-10: layer-tree panel — elements + lock/hide/solo + selection sync.

A custom ``QWidget`` (matching the house ``QListWidget(InternalMove)`` pattern of
``StoryboardPanel`` / ``color/layer_panel.py``) that renders the Qt-free
``build_layer_rows`` view-model.  It owns NO selection state itself: row clicks
emit ``elementSelected`` so the editor window (the mediator) keeps the canvas +
inspector + this panel in three-way sync.  Eye / lock / solo icons emit the
toggle signals the window turns into ``SetHierarchyCommand`` writes.

The HTML source remains available through the HTML inspector, while the layer
tree exposes page-local and global Background rows as structural, read-only
rows.  Individually editable HTML nodes remain an inspector mode (FH-06).
"""

from __future__ import annotations

from collections.abc import Iterable

from PyQt6.QtCore import QItemSelectionModel, QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMenu,
    QToolButton, QVBoxLayout, QWidget,
)

from ..core.model import TeachingSceneDocument
from src.shared.assets.icon_manager import (
    IconColors,
    icon_add,
    icon_delete,
    icon_down,
    icon_duplicate,
    icon_lock_track,
    icon_solo,
    icon_unlock_track,
    icon_up,
    icon_visible,
    icon_hidden,
)
from src.shared.i18n.i18n import I18N
from .layer_tree_model import (
    BACKGROUND_ROW_PREFIX,
    GLOBAL_BACKGROUND_ROW_ID,
    PAGE_ROW_PREFIX,
    LayerRow,
    build_layer_rows,
)

_ROLE_ROW_ID = 256


class LayerItemWidget(QWidget):
    """A compact, colour-page-style layer row with MG hierarchy controls."""

    visibilityToggled = pyqtSignal(str)   # element_id
    lockToggled = pyqtSignal(str)
    soloToggled = pyqtSignal(str)
    pageExpansionToggled = pyqtSignal(str)

    def __init__(self, row: LayerRow, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._row_id = row.id
        self._readonly = row.readonly
        self._page_id = row.page_id if row.kind == "page" else ""

        label_text = ("  " * max(0, int(row.depth))) + row.label
        if row.kind == "page":
            # Page header: bold, with a dot for the active page.  The arrow is
            # reserved for the single expand/collapse affordance.
            label_text = ("● " if row.active else "  ") + label_text
            if row.element_count:
                label_text += f"  ·  {row.element_count}"
        elif row.kind == "background" and row.page_id and not row.background_value.strip():
            label_text += "  ·  Global"
        self.expand_button: QToolButton | None = None
        if row.kind == "page":
            self.expand_button = QToolButton(self)
            self.expand_button.setText("▾" if row.expanded else "▸")
            self.expand_button.setFixedSize(22, 22)
            self.expand_button.setAutoRaise(True)
            self.expand_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self.expand_button.setToolTip(
                I18N.tr("motion_graphic.page.toggle_children", "Expand/collapse page elements")
            )
            # Background is also a page child. Empty newly-created pages must
            # remain collapsible before their first element is added.
            self.expand_button.setEnabled(True)
            self.expand_button.clicked.connect(
                lambda _checked=False: self.pageExpansionToggled.emit(self._page_id)
            )
        self.label = QLabel(label_text, self)
        self.label.setToolTip(row.id)
        self.label.setObjectName("mgLayerRowLabel")
        if row.kind == "page":
            font = self.label.font()
            font.setBold(True)
            self.label.setFont(font)
        # List selection and drag should work when users click the label, just
        # as it does in the Color page's layer list.
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.vis_button = self._toggle(
            icon_visible() if row.visible else icon_hidden(),
            I18N.tr("layer.toggle_visible", "Show/Hide"), checked=row.visible,
        )
        self.lock_button = self._toggle(
            icon_lock_track(IconColors.ORANGE) if row.locked else icon_unlock_track(),
            I18N.tr("layer.toggle_lock", "Lock/Unlock"), checked=row.locked,
        )
        self.solo_button = self._toggle(
            icon_solo(IconColors.ORANGE) if row.solo else icon_solo(),
            I18N.tr("layer.toggle_solo", "Solo"), checked=row.solo,
        )

        if not row.readonly:
            self.vis_button.toggled.connect(lambda _c, rid=row.id: self.visibilityToggled.emit(rid))
            self.lock_button.toggled.connect(lambda _c, rid=row.id: self.lockToggled.emit(rid))
            self.solo_button.toggled.connect(lambda _c, rid=row.id: self.soloToggled.emit(rid))
        else:
            # Structural rows: icons are inert indicators only.
            for btn in (self.vis_button, self.lock_button, self.solo_button):
                btn.setEnabled(False)
                btn.setCheckable(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 1, 4, 1)
        layout.setSpacing(2)
        if self.expand_button is not None:
            layout.addWidget(self.expand_button)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.vis_button)
        layout.addWidget(self.lock_button)
        layout.addWidget(self.solo_button)

    def _toggle(self, icon: object, tooltip: str, *, checked: bool) -> QToolButton:
        btn = QToolButton(self)
        btn.setIcon(icon)  # type: ignore[arg-type]
        btn.setIconSize(QSize(14, 14))
        btn.setFixedSize(22, 22)
        btn.setObjectName("mgLayerRowTool")
        btn.setToolTip(tooltip)
        btn.setCheckable(True)
        btn.setChecked(checked)
        btn.setAutoRaise(True)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return btn


class LayerTreePanel(QWidget):
    """The layer-tree panel (UX-10)."""

    elementSelected = pyqtSignal(str)
    selectionChanged = pyqtSignal(tuple)      # multi-select: the full selected id set
    visibilityToggled = pyqtSignal(str)
    lockToggled = pyqtSignal(str)
    soloToggled = pyqtSignal(str)
    removeRequested = pyqtSignal(str)
    reorderRequested = pyqtSignal(str, int)   # element_id, new tuple index
    reparentRequested = pyqtSignal(str, str)  # child_id, parent_id
    unparentRequested = pyqtSignal(str)
    createLayerRequested = pyqtSignal(str)
    duplicateRequested = pyqtSignal(str)
    pageMoveRequested = pyqtSignal(str, str)  # element_id, target page_id
    pageActivated = pyqtSignal(str)           # page_id: jump the playhead there
    backgroundSelected = pyqtSignal(str)      # page id, empty for global background
    pageCreateRequested = pyqtSignal()        # create a new page
    pageRenameRequested = pyqtSignal(str)
    pageRemoveRequested = pyqtSignal(str)
    pageDurationRequested = pyqtSignal(str)
    pageReorderRequested = pyqtSignal(str, int)
    pageExpansionChanged = pyqtSignal(tuple)  # page ids whose children are visible

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._updating = False
        self._rows: list[LayerRow] = []
        self._scene: TeachingSceneDocument | None = None
        self._active_page_id = ""
        self._expanded_pages: frozenset[str] = frozenset()
        self._expansion_initialized = False

        title = QLabel(I18N.tr("motion_graphic.panels.layers", "Layers"), self)
        title.setObjectName("mgLayerPanelTitle")
        header = QFrame(self)
        header.setObjectName("mgLayerHeader")
        header.setFixedHeight(30)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 0, 8, 0)
        header_layout.addWidget(title)
        header_layout.addStretch()

        self.list_widget = QListWidget(self)
        self.list_widget.setObjectName("mgLayerList")
        # ExtendedSelection enables ctrl/shift multi-row selection; the panel
        # is a thin renderer, so selection authority stays with the window.
        self.list_widget.itemSelectionChanged.connect(self._selection_changed)
        self.list_widget.model().rowsMoved.connect(self._rows_moved)
        self.list_widget.itemClicked.connect(self._item_clicked)
        self.list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)

        footer = QFrame(self)
        footer.setObjectName("mgLayerFooter")
        footer.setFixedHeight(34)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(6, 2, 6, 2)
        footer_layout.setSpacing(5)
        self.add_button = self._tool_button(icon_add(), I18N.tr("tooltip.add_layer", "Add Layer"))
        self.add_button.setMenu(self._build_add_menu())
        self.add_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.delete_button = self._tool_button(icon_delete(), I18N.tr("layer.delete", "Delete Selected"))
        self.up_button = self._tool_button(icon_up(), I18N.tr("tooltip.move_up", "Move Up"))
        self.down_button = self._tool_button(icon_down(), I18N.tr("tooltip.move_down", "Move Down"))
        self.duplicate_button = self._tool_button(icon_duplicate(), I18N.tr("tooltip.duplicate_layer", "Duplicate Layer"))
        self.nest_button = self._tool_button(icon_solo(), I18N.tr("motion_graphic.layer.nest_tooltip", "Nest selected layers"))
        self.unnest_button = self._tool_button(icon_unlock_track(), I18N.tr("layer.unnest", "Unnest"))
        for button in (
            self.add_button, self.delete_button, self.up_button, self.down_button,
            self.duplicate_button, self.nest_button, self.unnest_button,
        ):
            footer_layout.addWidget(button)
        footer_layout.addStretch()
        self.delete_button.clicked.connect(self._delete_current)
        self.up_button.clicked.connect(lambda: self._move_current(-1))
        self.down_button.clicked.connect(lambda: self._move_current(1))
        self.duplicate_button.clicked.connect(self._duplicate_current)
        self.nest_button.clicked.connect(self._nest_selected)
        self.unnest_button.clicked.connect(self._unnest_selected)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(header)
        layout.addWidget(self.list_widget, 1)
        layout.addWidget(footer)

    # ---------------- public slots ---------------- #

    def set_scene(self, scene: TeachingSceneDocument,
                  *, active_page_id: str = "",
                  expanded_page_ids: Iterable[str] | None = None) -> None:
        """Rebuild the row list from ``scene`` (keeps the selected set)."""
        selected = self.selected_row_ids
        self._scene = scene
        self._active_page_id = active_page_id
        if expanded_page_ids is not None:
            self._expanded_pages = frozenset(expanded_page_ids) & frozenset(scene.page_ids)
            self._expansion_initialized = True
        elif not self._expansion_initialized:
            self._expanded_pages = frozenset(scene.page_ids)
            self._expansion_initialized = True
        self._rows = build_layer_rows(
            scene,
            active_page_id=active_page_id,
            expanded_page_ids=self._expanded_pages,
        )
        self._rebuild(selected)

    @property
    def expanded_page_ids(self) -> tuple[str, ...]:
        if self._scene is None:
            return tuple(self._expanded_pages)
        return tuple(page_id for page_id in self._scene.page_ids
                     if page_id in self._expanded_pages)

    def set_expanded_pages(self, page_ids: Iterable[str]) -> None:
        """Apply expansion from the sibling keyframes tree without emitting."""
        if self._scene is None:
            self._expanded_pages = frozenset(page_ids)
            return
        self._expanded_pages = frozenset(page_ids) & frozenset(self._scene.page_ids)
        self._rows = build_layer_rows(
            self._scene,
            active_page_id=self._active_page_id,
            expanded_page_ids=self._expanded_pages,
        )
        self._rebuild(self.selected_row_ids)

    def set_selection(self, element_ids: str | Iterable[str] | None) -> None:
        """Highlight row(s) without emitting (external sync).

        Accepts a single id (``str``), an iterable of ids (multi-select), or
        ``None`` to clear.  Non-existent ids are ignored.
        """
        if isinstance(element_ids, str):
            ids = {element_ids}
        elif element_ids is None:
            ids = set()
        else:
            ids = set(element_ids)
        self._updating = True
        try:
            self.list_widget.clearSelection()
            first_item: QListWidgetItem | None = None
            if ids:
                for index in range(self.list_widget.count()):
                    item = self.list_widget.item(index)
                    if str(item.data(_ROLE_ROW_ID)) in ids:
                        item.setSelected(True)
                        if first_item is None:
                            first_item = item
            # Keep the "current" row (keyboard focus / current_row_id) on the
            # first selected row.  Use NoUpdate so setting current does NOT
            # alter the selection (the one-arg setCurrentItem would ClearAndSelect
            # in ExtendedSelection, wiping the other selected rows).
            if first_item is not None:
                self.list_widget.setCurrentItem(first_item, QItemSelectionModel.SelectionFlag.NoUpdate)
            else:
                self.list_widget.setCurrentRow(-1)
        finally:
            self._updating = False
        self._update_action_state()

    @property
    def selected_row_ids(self) -> tuple[str, ...]:
        """All selected row ids (multi-select aware)."""
        return tuple(str(item.data(_ROLE_ROW_ID))
                     for item in self.list_widget.selectedItems())

    @property
    def current_row_id(self) -> str | None:
        item = self.list_widget.currentItem()
        return str(item.data(_ROLE_ROW_ID)) if item is not None else None

    # ---------------- internal ---------------- #

    def _rebuild(self, selected_ids: Iterable[str] | str | None) -> None:
        self._updating = True
        try:
            self.list_widget.clear()
            for row in self._rows:
                # The custom row widget owns the label.  Keep the item text
                # empty so QListWidget's delegate does not paint it a second
                # time underneath the QLabel.
                item = QListWidgetItem()
                item.setData(_ROLE_ROW_ID, row.id)
                # Page/background rows are never draggable; page headers and
                # background rows are not selectable (clicks trigger their
                # structural action instead).
                if row.readonly:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
                if row.kind in {"page", "background"}:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                self.list_widget.addItem(item)
                widget = LayerItemWidget(row, self.list_widget)
                widget.visibilityToggled.connect(self.visibilityToggled)
                widget.lockToggled.connect(self.lockToggled)
                widget.soloToggled.connect(self.soloToggled)
                widget.pageExpansionToggled.connect(self._toggle_page_expanded)
                self.list_widget.setItemWidget(item, widget)
        finally:
            self._updating = False
        if selected_ids:
            self.set_selection(selected_ids)
        self._update_action_state()

    def _selection_changed(self) -> None:
        if self._updating:
            return
        ids = self.selected_row_ids
        self.selectionChanged.emit(ids)
        # Backward-compat: emit the primary (current) selection as a single id.
        primary = self.current_row_id
        if primary is not None:
            self.elementSelected.emit(primary)
        self._update_action_state()

    def _tool_button(self, icon: object, tooltip: str) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("mgLayerFooterButton")
        button.setIcon(icon)  # type: ignore[arg-type]
        button.setIconSize(QSize(16, 16))
        button.setFixedSize(28, 26)
        button.setToolTip(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    def _build_add_menu(self) -> QMenu:
        menu = QMenu(self)
        page_action = menu.addAction(
            I18N.tr("motion_graphic.page.new_page", "New Page"),
        )
        page_action.triggered.connect(self.pageCreateRequested.emit)
        menu.addSeparator()
        text_action = menu.addAction(I18N.tr("motion_graphic.layer.new_text", "Text Layer"))
        text_action.triggered.connect(lambda: self.createLayerRequested.emit("text"))
        frame_action = menu.addAction(I18N.tr("motion_graphic.layer.new_frame", "Frame (Flex / Grid)"))
        frame_action.triggered.connect(lambda: self.createLayerRequested.emit("frame"))
        menu.addSeparator()
        shape_actions = (
            ("rect", "motion_graphic.layer.new_rectangle", "Rectangle"),
            ("rounded_rect", "motion_graphic.layer.new_rounded_rectangle", "Rounded Rectangle"),
            ("ellipse", "motion_graphic.layer.new_ellipse", "Ellipse"),
            ("line", "motion_graphic.layer.new_line", "Line"),
            ("arrow", "motion_graphic.layer.new_arrow", "Arrow"),
            ("triangle", "motion_graphic.layer.new_triangle", "Triangle"),
            ("polygon", "motion_graphic.layer.new_polygon", "Polygon"),
            ("star", "motion_graphic.layer.new_star", "Star"),
        )
        for kind, translation_key, fallback in shape_actions:
            action = menu.addAction(I18N.tr(translation_key, fallback))
            action.triggered.connect(lambda _checked=False, shape_kind=kind:
                                     self.createLayerRequested.emit(shape_kind))
        return menu

    def _update_action_state(self) -> None:
        current_id = self.current_row_id
        editable = (current_id is not None
                    and not self._is_html_row(current_id)
                    and not self._is_page_row(current_id)
                    and not self._is_background_row(current_id))
        current_layer = next((row for row in self._rows if row.id == current_id), None)
        same_page_rows = [
            index for index, row in enumerate(self._rows)
            if not row.readonly
            and current_layer is not None
            and row.page_id == current_layer.page_id
        ]
        try:
            same_page_position = same_page_rows.index(self.list_widget.currentRow())
        except ValueError:
            same_page_position = -1
        self.delete_button.setEnabled(editable)
        self.up_button.setEnabled(editable and same_page_position > 0)
        self.down_button.setEnabled(
            editable and 0 <= same_page_position < len(same_page_rows) - 1,
        )
        self.duplicate_button.setEnabled(editable)
        self.nest_button.setEnabled(editable and len(self.selected_row_ids) > 1)
        self.unnest_button.setEnabled(editable)

    def _delete_current(self) -> None:
        # Batch delete (multi-select): emit remove for every selected element
        # row.  The HTML-layer row is read-only and never removed here.
        for rid in self.selected_row_ids:
            if not self._is_html_row(rid) and not self._is_background_row(rid):
                self.removeRequested.emit(rid)

    def _move_current(self, direction: int) -> None:
        """Move the active layer in the displayed (top-most first) order."""
        current_id = self.current_row_id
        if (current_id is None or self._is_html_row(current_id)
                or self._is_page_row(current_id) or self._is_background_row(current_id)):
            return
        current_layer = next((row for row in self._rows if row.id == current_id), None)
        if current_layer is None or self._scene is None:
            return
        # Page ownership is part of the hierarchy now: footer moves may change
        # z-order inside one page, but must never silently move a layer across
        # an artboard.  Cross-page movement has its own explicit drop path.
        page_rows = [
            index for index, row in enumerate(self._rows)
            if not row.readonly and row.page_id == current_layer.page_id
        ]
        try:
            position = page_rows.index(self.list_widget.currentRow())
        except ValueError:
            return
        target = position + direction
        if not 0 <= target < len(page_rows):
            return
        # AUD-30702: the document tuple is paint-ascending and covers every
        # element, while the list shows top-most first and only the visible
        # rows.  Map the page-local target through the full page-grouped
        # display sequence (what the tree shows with every page expanded), so
        # a collapsed page can no longer silently shift the emitted index
        # into the wrong tuple position.
        full_ids = self._full_display_element_ids()
        total = len(full_ids)
        if total <= 0:
            return
        block_offset = self._elements_before_page(current_layer.page_id)
        full_position = min(block_offset + target, total - 1)
        self.reorderRequested.emit(current_id, total - 1 - full_position)
    def _duplicate_current(self) -> None:
        current_id = self.current_row_id
        if (current_id is not None and not self._is_html_row(current_id)
                and not self._is_background_row(current_id)):
            self.duplicateRequested.emit(current_id)

    def _nest_selected(self) -> None:
        parent_id = self.current_row_id
        if (parent_id is None or self._is_html_row(parent_id)
                or self._is_background_row(parent_id)):
            return
        for child_id in self.selected_row_ids:
            if (child_id != parent_id and not self._is_html_row(child_id)
                    and not self._is_background_row(child_id)):
                self.reparentRequested.emit(child_id, parent_id)

    def _unnest_selected(self) -> None:
        for element_id in self.selected_row_ids:
            if (not self._is_html_row(element_id)
                    and not self._is_background_row(element_id)):
                self.unparentRequested.emit(element_id)

    def _rows_moved(self, _parent: object, start: int, end: int,
                    _destination: object, _row: int) -> None:
        if self._updating:
            return
        # AUD-30703: QListWidget has already applied the visual move by the
        # time rowsMoved fires.  The commit path below maps exactly one row
        # (the current item); a multi-row move cannot be mapped that way, so
        # committing nothing would leave the list silently diverged from the
        # document (undo stack / serialization / paint order).  Rebuild from
        # the authoritative rows instead — the drop visibly snaps back.
        # This covers both Qt granularities: a single block move
        # (start != end) and a multi-select drag that Qt splits into several
        # single-row moves (more than one element row selected).
        if start != end or len(self.selected_row_ids) > 1:
            self._rebuild(self.selected_row_ids)
            return
        # QListWidget has already applied the move.  Read the new visual order
        # (headers included) and map it back: a drop into another page's
        # section re-pages the element; a drop inside its own page reorders z.
        current = self.list_widget.currentItem()
        if current is None:
            return
        rid = str(current.data(_ROLE_ROW_ID))
        if self._is_html_row(rid) or self._is_page_row(rid):
            return
        page_headers = {r.id: r.page_id for r in self._rows if r.kind == "page"}
        readonly_ids = {r.id for r in self._rows if r.readonly}
        element_ids: list[str] = []
        current_element_pos = -1
        target_page = ""
        for index in range(self.list_widget.count()):
            item_id = str(self.list_widget.item(index).data(_ROLE_ROW_ID))
            if item_id in page_headers:
                # Only the last header ABOVE the element names its section;
                # headers below belong to the following page.
                if current_element_pos < 0:
                    target_page = page_headers[item_id]
                continue
            if item_id in readonly_ids:
                continue
            if item_id == rid:
                current_element_pos = len(element_ids)
            element_ids.append(item_id)
        if current_element_pos < 0:
            return
        if not target_page and page_headers:
            # Dropped above the first header: it belongs to the first page.
            target_page = next(iter(page_headers.values()))
        old_page = next((r.page_id for r in self._rows if r.id == rid), "")
        if target_page and old_page and target_page != old_page:
            self.pageMoveRequested.emit(rid, target_page)
            return
        # AUD-30702: the emitted index is a full-document tuple index.  The
        # visible drop position only counts rows the user can see, so every
        # element hidden inside a collapsed page that sits above this element
        # in the full page-grouped display sequence must be added back before
        # the visible→tuple reversal; otherwise the index is silently pressed
        # into the wrong (still valid) tuple slot.
        full_ids = self._full_display_element_ids()
        total = len(full_ids)
        if total <= 0 or self._scene is None:
            return
        collapsed_above = 0
        for page in self._scene.pages:
            if page.page_id == old_page:
                break
            if page.page_id not in self._expanded_pages:
                # The whole collapsed page block sits above the drop in the
                # full display sequence; none of its rows were counted in
                # ``current_element_pos``.
                collapsed_above += sum(
                    1 for element in self._scene.elements
                    if element.page_id == page.page_id
                )
        full_position = min(current_element_pos + collapsed_above, total - 1)
        # Element sequence is z-descending; the document tuple is
        # paint-ascending. new_tuple_index = N-1-position over the FULL
        # document element set, not the visible rows.
        new_tuple_index = total - 1 - full_position
        self.reorderRequested.emit(rid, new_tuple_index)

    def _full_display_element_ids(self) -> list[str]:
        """Element ids in the full page-grouped display order (AUD-30702).

        This is the sequence ``build_layer_rows`` shows when every page is
        expanded: pages in timeline order, elements z-descending inside each
        page (same ``(z_index, tuple_index)`` sort).  Reorder index maths
        must run against this full sequence — the consumer
        (``ReorderElementCommand`` → ``with_elements_reordered``) interprets
        the emitted number as an index into the complete ``scene.elements``
        tuple, so any formula derived from the currently visible rows writes
        the element into a wrong tuple position whenever a page is collapsed.
        """
        if self._scene is None:
            return []
        ordered = sorted(
            enumerate(self._scene.elements),
            key=lambda pair: (pair[1].z_index, pair[0]),
            reverse=True,
        )
        ids: list[str] = []
        for page in self._scene.pages:
            ids.extend(
                element.element_id for _index, element in ordered
                if element.page_id == page.page_id
            )
        return ids

    def _elements_before_page(self, page_id: str) -> int:
        """Count ALL elements displayed above ``page_id``'s own block.

        Collapsed or expanded makes no difference here — this is the full
        document count, used to translate a page-local display position into
        the full page-grouped display position (AUD-30702).
        """
        if self._scene is None:
            return 0
        total = 0
        for page in self._scene.pages:
            if page.page_id == page_id:
                return total
            total += sum(
                1 for element in self._scene.elements
                if element.page_id == page.page_id
            )
        return total

    def _item_clicked(self, item: QListWidgetItem) -> None:
        rid = str(item.data(_ROLE_ROW_ID))
        if self._is_page_row(rid):
            page_id = rid[len(PAGE_ROW_PREFIX):]
            if page_id:
                self.pageActivated.emit(page_id)
        elif self._is_background_row(rid):
            page_id = "" if rid == GLOBAL_BACKGROUND_ROW_ID else rid[len(BACKGROUND_ROW_PREFIX):]
            self.backgroundSelected.emit(page_id)

    def _show_context_menu(self, position: object) -> None:
        """Expose page management where pages now live: the Layers tree."""
        item = self.list_widget.itemAt(position)  # type: ignore[arg-type]
        if item is None:
            return
        row_id = str(item.data(_ROLE_ROW_ID))
        row = next((candidate for candidate in self._rows if candidate.id == row_id), None)
        if row is None or row.kind != "page":
            return
        menu = QMenu(self)
        menu.addAction(
            I18N.tr("motion_graphic.pages.rename", "Rename Page…"),
            lambda page_id=row.page_id: self.pageRenameRequested.emit(page_id),
        )
        menu.addAction(
            I18N.tr("motion_graphic.pages.duration", "Set Page Duration…"),
            lambda page_id=row.page_id: self.pageDurationRequested.emit(page_id),
        )
        if row.page_index > 0:
            menu.addAction(
                I18N.tr("motion_graphic.pages.move_left", "Move Left"),
                lambda page_id=row.page_id, index=row.page_index:
                self.pageReorderRequested.emit(page_id, index - 1),
            )
        if self._scene is not None and row.page_index < len(self._scene.pages) - 1:
            menu.addAction(
                I18N.tr("motion_graphic.pages.move_right", "Move Right"),
                lambda page_id=row.page_id, index=row.page_index:
                self.pageReorderRequested.emit(page_id, index + 1),
            )
        menu.addSeparator()
        menu.addAction(
            I18N.tr("motion_graphic.pages.remove", "Remove Page"),
            lambda page_id=row.page_id: self.pageRemoveRequested.emit(page_id),
        )
        menu.exec(self.list_widget.viewport().mapToGlobal(position))  # type: ignore[arg-type]

    def _toggle_page_expanded(self, page_id: str) -> None:
        if self._scene is None or page_id not in self._scene.page_ids:
            return
        expanded = set(self._expanded_pages)
        if page_id in expanded:
            expanded.remove(page_id)
        else:
            expanded.add(page_id)
        self._expanded_pages = frozenset(expanded)
        selected = self.selected_row_ids
        self._rows = build_layer_rows(
            self._scene,
            active_page_id=self._active_page_id,
            expanded_page_ids=self._expanded_pages,
        )
        self._rebuild(selected)
        self.pageExpansionChanged.emit(self.expanded_page_ids)

    def _element_count(self) -> int:
        return sum(1 for r in self._rows if not r.readonly)

    def _is_html_row(self, rid: str) -> bool:
        return any(r.id == rid and r.is_html for r in self._rows)

    def _is_page_row(self, rid: str) -> bool:
        return any(r.id == rid and r.kind == "page" for r in self._rows)

    def _is_background_row(self, rid: str) -> bool:
        return any(r.id == rid and r.kind == "background" for r in self._rows)
