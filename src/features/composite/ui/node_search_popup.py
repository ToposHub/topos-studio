"""
Node search popup for quick node creation in the composite editor.

Features:
- Tab-based search (All, Input, Output, Effect, etc.)
- Keyword filtering with real-time results
- Alias support (merge/over/plus, read/image, cc/colorcorrect, etc.)
- Recent nodes tracking
- Favorites system
- Keyboard navigation (↑↓ Enter Esc)
"""

from typing import Optional, Dict, List, Tuple, Callable

from PyQt6.QtCore import (
    Qt, pyqtSignal, QSettings,
)
from PyQt6.QtGui import (
    QFont, )
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
    QLabel, QTabWidget, QWidget, QSizePolicy,
)

from src.shared.i18n.i18n import I18N


# ── Alias mappings (fallback for nodes without SEARCH_ALIASES) ─────────────

# Central alias mapping kept as fallback for nodes that don't define SEARCH_ALIASES
# New nodes should define metadata in their class (SEARCH_ALIASES, DESCRIPTION, TAGS)
NODE_ALIASES: Dict[str, str] = {
    # Merge aliases (fallback)
    "merge": "merge",
    "over": "merge",
    "plus": "merge",
    "add": "merge",
    "under": "merge",
    "multiply": "merge",
    "screen": "merge",
    # Input aliases (fallback)
    "read": "image_input",
    "image": "image_input",
    "load": "image_input",
    "file": "image_input",
    "bg": "constant",
    "constant": "constant",
    # Color correction aliases (fallback)
    "cc": "grade",
    "colorcorrect": "color_correct",
    "colorcorrectnode": "color_correct",
    "grade": "grade",
    "primaries": "grade",
    # Premult aliases (fallback)
    "premult": "premult",
    "unpremult": "unpremult",
    "premultiply": "premult",
    "unpremultiply": "unpremult",
    "straight": "unpremult",
    # Transform aliases (fallback)
    "transform": "transform",
    "move": "transform",
    "scale": "transform",
    "rotate": "transform",
    "reformat": "reformat",
    "resize": "reformat",
    "crop": "crop",
    # Channel aliases (fallback)
    "shuffle": "shuffle",
    "copychannel": "copy_channel",
    "setalpha": "set_alpha",
    "alpha": "set_alpha",
    # Filter aliases (fallback)
    "blur": "blur",
    "gaussian": "blur",
    "sharpen": "sharpen",
    "invert": "invert",
    "clamp": "clamp",
    # Keyer aliases (fallback)
    "keyer": "chroma_keyer",
    "chromakey": "chroma_keyer",
    "greencreen": "chroma_keyer",
    "luma": "luma_keyer",
    "lumakey": "luma_keyer",
    # Topos Color aliases (fallback)
    "toposcolor": "topos_color",
    "colorstack": "topos_color",
    "调色": "topos_color",
    "调色栈": "topos_color",
    # Utility aliases (fallback)
    "noop": "noop",
    "pass": "noop",
    "dot": "dot",
    "switch": "switch",
    "dissolve": "dissolve",
    # Output aliases (fallback)
    "output": "output",
    "display": "output",
    "write": "file_output",
    "save": "file_output",
    "export": "file_output",
}


class NodeSearchPopup(QDialog):
    """Modal popup for searching and adding nodes to the graph."""

    # Emitted when a node is selected
    node_selected = pyqtSignal(str)  # node_type

    def __init__(
        self,
        node_categories: Dict[str, List[Tuple[str, type]]],
        parent: Optional[QWidget] = None,
        on_node_added: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(parent)
        self._node_categories = node_categories
        self._on_node_added = on_node_added

        # State
        self._recent_nodes: List[str] = self._load_recent_nodes()
        self._favorites: List[str] = self._load_favorites()
        self._current_filter = ""

        self._setup_ui()
        self._populate_tabs()

        # Restore favorites tab if has favorites
        if self._favorites:
            self._tab_widget.setCurrentWidget(self._favorites_tab)

    def set_node_categories(
        self, node_categories: Dict[str, List[Tuple[str, type]]]
    ) -> None:
        """Replace the node category map and refresh the popup lists.

        Used so the Add Node menu can react to the active work mode (image
        mode hides time-driven nodes) without recreating the dialog.
        """
        self._node_categories = node_categories
        # Guard: _populate_tabs touches UI widgets that only exist after
        # _setup_ui. Ignore calls that arrive before the dialog is built.
        if hasattr(self, "_all_list"):
            self._populate_tabs()

    # ── UI Setup ────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        """Build the popup UI."""
        self.setWindowTitle(I18N.tr("node_search.add_node", "Add Node"))
        self.setFixedWidth(480)
        self.setFixedHeight(520)
        self.setModal(True)

        # Window flags: tool popup style
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Popup
        )

        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Search bar
        search_layout = QHBoxLayout()
        search_layout.setContentsMargins(12, 12, 12, 8)
        search_layout.setSpacing(8)

        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(
            I18N.tr("node_search.placeholder", "Search nodes... (type to filter)"))
        self._search_input.setFont(QFont("Segoe UI", 11))
        self._search_input.textChanged.connect(self._on_search_changed)
        self._search_input.returnPressed.connect(self._on_return_pressed)
        search_layout.addWidget(self._search_input)

        main_layout.addLayout(search_layout)

        # Tabs
        self._tab_widget = QTabWidget()
        self._tab_widget.setFont(QFont("Segoe UI", 9))
        self._tab_widget.currentChanged.connect(self._on_tab_changed)

        # Create tabs
        self._all_tab = QWidget()
        self._favorites_tab = QWidget()
        self._recent_tab = QWidget()

        self._all_list = QListWidget()
        self._favorites_list = QListWidget()
        self._recent_list = QListWidget()

        # Setup list widgets
        for lst in (self._all_list, self._favorites_list, self._recent_list):
            lst.setFont(QFont("Segoe UI", 10))
            lst.setUniformItemSizes(True)
            lst.setResizeMode(QListWidget.ResizeMode.Adjust)
            lst.setItemAlignment(Qt.AlignmentFlag.AlignLeft)
            lst.currentRowChanged.connect(self._on_selection_changed)
            lst.itemDoubleClicked.connect(self._on_item_activated)

        # Layout for each tab
        for tab, list_widget, label_text in [
            (self._all_tab, self._all_list, "All Nodes"),
            (self._favorites_tab, self._favorites_list, "Favorites"),
            (self._recent_tab, self._recent_list, "Recent"),
        ]:
            tab_layout = QVBoxLayout(tab)
            tab_layout.setContentsMargins(8, 4, 8, 8)
            tab_layout.setSpacing(4)

            tab_label = QLabel(label_text)
            tab_label.setFont(QFont("Segoe UI", 8))
            tab_label.setStyleSheet("color: #888; padding: 4px 0;")
            tab_layout.addWidget(tab_label)
            tab_layout.addWidget(list_widget)

        # Add tabs
        self._tab_widget.addTab(self._all_tab, "All")
        self._tab_widget.addTab(self._favorites_tab, "★")
        self._tab_widget.addTab(self._recent_tab, "Recent")

        main_layout.addWidget(self._tab_widget)

        # Bottom bar with help
        bottom_layout = QHBoxLayout()
        bottom_layout.setContentsMargins(12, 8, 12, 12)
        bottom_layout.setSpacing(12)

        help_label = QLabel(
            I18N.tr(
                "node_search.help",
                "↑↓ Navigate  •  Enter Add  •  Tab Switch  •  Esc Close"))
        help_label.setFont(QFont("Segoe UI", 8))
        help_label.setStyleSheet("color: #999;")
        help_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        bottom_layout.addWidget(help_label)

        main_layout.addLayout(bottom_layout)

        # Styling
        self._apply_stylesheet()

    def _apply_stylesheet(self) -> None:
        """Apply dark theme styling."""
        self.setStyleSheet("""
            QDialog {
                background-color: #2b2b2b;
                border: 1px solid #555;
                border-radius: 6px;
            }
            QLineEdit {
                background-color: #3c3c3c;
                color: #e0e0e0;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 6px 10px;
                selection-background-color: #4a9eff;
            }
            QLineEdit:focus {
                border-color: #4a9eff;
            }
            QTabWidget::pane {
                border: 1px solid #555;
                border-radius: 4px;
                background-color: #2b2b2b;
                top: -2px;
            }
            QTabBar::tab {
                background-color: #3c3c3c;
                color: #aaa;
                border: 1px solid #555;
                border-bottom: none;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                padding: 6px 16px;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background-color: #2b2b2b;
                color: #e0e0e0;
                border-color: #555;
            }
            QListWidget {
                background-color: #2b2b2b;
                color: #e0e0e0;
                border: none;
                outline: none;
            }
            QListWidget::item {
                padding: 6px 8px;
                border-radius: 4px;
            }
            QListWidget::item:selected {
                background-color: #4a9eff;
                color: white;
            }
            QListWidget::item:hover {
                background-color: #3a3a3a;
            }
            QLabel {
                color: #e0e0e0;
            }
        """)

    # ── Population ──────────────────────────────────────────────────────────

    def _populate_tabs(self) -> None:
        """Populate all tabs with nodes."""
        self._populate_all_tab()
        self._populate_favorites_tab()
        self._populate_recent_tab()

    def _populate_all_tab(self) -> None:
        """Populate the 'All' tab with all nodes."""
        self._all_list.clear()
        for category, nodes in self._node_categories.items():
            # Add category header
            header_item = QListWidgetItem(f"  ── {category} ──")
            header_item.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
            header_item.setForeground(Qt.GlobalColor.gray)
            header_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._all_list.addItem(header_item)

            for node_type, node_class in nodes:
                if node_class is None:
                    continue  # Skip placeholders

                # Get display name
                display_name = self._get_display_name(node_type, node_class)
                
                # Mark placeholder nodes with warning indicator
                is_placeholder = getattr(node_class, 'IS_PLACEHOLDER', False)
                if is_placeholder:
                    display_name = f"{display_name} ⚠ (N/A)"

                item = QListWidgetItem(f"  {display_name}")
                item.setData(Qt.ItemDataRole.UserRole, node_type)
                tooltip = self._get_tooltip(node_type, node_class)
                if is_placeholder:
                    tooltip = f"{tooltip}\n[Placeholder - Rendering not implemented]"
                item.setToolTip(tooltip)
                self._all_list.addItem(item)

    def _populate_favorites_tab(self) -> None:
        """Populate the favorites tab."""
        self._favorites_list.clear()
        if not self._favorites:
            item = QListWidgetItem(I18N.tr("node_search.no_favorites", "  No favorites yet"))
            item.setForeground(Qt.GlobalColor.gray)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._favorites_list.addItem(item)
            return

        for node_type in self._favorites:
            node_class = self._get_node_class(node_type)
            display_name = self._get_display_name(node_type, node_class)
            # Mark placeholder nodes
            is_placeholder = getattr(node_class, 'IS_PLACEHOLDER', False) if node_class else False
            if is_placeholder:
                display_name = f"{display_name} ⚠ (N/A)"
            item = QListWidgetItem(f"  ★ {display_name}")
            item.setData(Qt.ItemDataRole.UserRole, node_type)
            self._favorites_list.addItem(item)

    def _populate_recent_tab(self) -> None:
        """Populate the recent tab."""
        self._recent_list.clear()
        if not self._recent_nodes:
            item = QListWidgetItem(I18N.tr("node_search.no_recent", "  No recent nodes"))
            item.setForeground(Qt.GlobalColor.gray)
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._recent_list.addItem(item)
            return

        for node_type in self._recent_nodes:
            node_class = self._get_node_class(node_type)
            display_name = self._get_display_name(node_type, node_class)
            # Mark placeholder nodes
            is_placeholder = getattr(node_class, 'IS_PLACEHOLDER', False) if node_class else False
            if is_placeholder:
                display_name = f"{display_name} ⚠ (N/A)"
            item = QListWidgetItem(f"  {display_name}")
            item.setData(Qt.ItemDataRole.UserRole, node_type)
            self._recent_list.addItem(item)

    # ── Display helpers ─────────────────────────────────────────────────────

    def _get_display_name(self, node_type: str, node_class: Optional[type]) -> str:
        """Get the display name for a node type.

        Priority:
        1. i18n_title_key resolved via the i18n system (localized title)
        2. node_class.node_title (class-defined English title)
        3. node_class.DESCRIPTION (metadata description)
        4. Convert snake_case to Title Case (fallback)
        """
        if node_class:
            # Prefer localized title when an i18n key is defined.
            i18n_key = getattr(node_class, "i18n_title_key", None)
            if i18n_key:
                try:
                    from src.shared.i18n.i18n import tr_or_none
                    translated = tr_or_none(i18n_key)
                    if translated:
                        return translated
                except Exception:
                    pass
            # Fall back to the English class title.
            if hasattr(node_class, "node_title") and node_class.node_title:
                return node_class.node_title
            # Use DESCRIPTION as fallback
            if hasattr(node_class, "DESCRIPTION") and node_class.DESCRIPTION:
                return node_class.DESCRIPTION
        # Convert snake_case to Title Case
        return node_type.replace("_", " ").title()

    def _get_tooltip(self, node_type: str, node_class: Optional[type]) -> str:
        """Get tooltip text for a node.

        Priority:
        1. node_class.DESCRIPTION (metadata description)
        2. node_class.HELP_TEXT (detailed help)
        3. node_type (fallback)
        """
        if node_class:
            # Use DESCRIPTION as primary tooltip
            if hasattr(node_class, "DESCRIPTION") and node_class.DESCRIPTION:
                return node_class.DESCRIPTION
            # Use HELP_TEXT as fallback for detailed info
            if hasattr(node_class, "HELP_TEXT") and node_class.HELP_TEXT:
                return node_class.HELP_TEXT
        return node_type

    # ── Search / Filter ─────────────────────────────────────────────────────

    def _on_search_changed(self, text: str) -> None:
        """Handle search text changes."""
        self._current_filter = text.lower().strip()

        # Determine which list to filter
        current_tab = self._tab_widget.currentIndex()
        if current_tab == 0:
            self._filter_list(self._all_list, self._current_filter)
        elif current_tab == 1:
            self._filter_list(self._favorites_list, self._current_filter)
        else:
            self._filter_list(self._recent_list, self._current_filter)

    def _filter_list(self, list_widget: QListWidget, filter_text: str) -> None:
        """Filter list items based on search text.

        Matching criteria (checked in order):
        1. Filter in display name
        2. Filter in node_type
        3. Filter matches node's SEARCH_ALIASES (metadata)
        4. Filter matches centralized NODE_ALIASES (fallback)
        """
        if not filter_text:
            for i in range(list_widget.count()):
                list_widget.item(i).setHidden(False)
            return

        # Check if filter matches a centralized alias (fallback)
        resolved = NODE_ALIASES.get(filter_text, filter_text)

        # First pass: match nodes. AUD-10802: NoItemFlags == 0, so the old
        # `flags() & NoItemFlags` guard was always falsy and headers fell
        # through to text matching (typing "color" matched the "── Color ──"
        # header itself). Headers are identified by exact flag equality and
        # excluded from matching; their visibility is derived below from
        # whether any child in their group is visible.
        matched: List[bool] = []
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            if item.flags() == Qt.ItemFlag.NoItemFlags:
                matched.append(False)  # header placeholder
                continue

            node_type = item.data(Qt.ItemDataRole.UserRole) or ""
            display_name = item.text().lower()

            # Get node class from categories for metadata lookup
            node_class = self._get_node_class(node_type)

            # Check multiple matching criteria
            matches = (
                filter_text in display_name
                or filter_text in node_type
                or node_type == resolved
                or self._matches_search_aliases(node_type, filter_text, node_class)
                or self._matches_alias(node_type, filter_text)
            )

            matched.append(matches)
            item.setHidden(not matches)

        # Second pass (bottom-up): a header stays visible only while it has
        # at least one visible node below it, so keyword filtering never
        # leaves orphan headers behind.
        group_has_visible = False
        for i in range(list_widget.count() - 1, -1, -1):
            item = list_widget.item(i)
            if item.flags() == Qt.ItemFlag.NoItemFlags:
                item.setHidden(not group_has_visible)
                group_has_visible = False  # reset for the group above
            elif matched[i]:
                group_has_visible = True

    def _get_node_class(self, node_type: str) -> Optional[type]:
        """Get the node class for a given node_type from categories."""
        for category, nodes in self._node_categories.items():
            for ntype, nclass in nodes:
                if ntype == node_type:
                    return nclass
        return None

    def _matches_search_aliases(self, node_type: str, filter_text: str, node_class: Optional[type]) -> bool:
        """Check if filter text matches node's SEARCH_ALIASES metadata."""
        if node_class and hasattr(node_class, "SEARCH_ALIASES"):
            aliases = node_class.SEARCH_ALIASES
            if aliases:
                # Check if filter matches any alias (exact or partial)
                for alias in aliases:
                    if filter_text == alias.lower() or filter_text in alias.lower():
                        return True
        return False

    def _matches_alias(self, node_type: str, filter_text: str) -> bool:
        """Check if filter text is an alias for this node type in centralized mapping.

        This is a fallback for nodes without SEARCH_ALIASES metadata.
        """
        for alias, resolved in NODE_ALIASES.items():
            if resolved == node_type and filter_text in alias:
                return True
        return False

    # ── Navigation ──────────────────────────────────────────────────────────

    def _on_tab_changed(self, index: int) -> None:
        """Handle tab switching."""
        # Apply current filter to new tab
        if self._current_filter:
            lists = [self._all_list, self._favorites_list, self._recent_list]
            if index < len(lists):
                self._filter_list(lists[index], self._current_filter)

    def _on_selection_changed(self, row: int) -> None:
        """Handle selection change (for keyboard navigation)."""
        pass  # QListWidget handles visual selection

    def _on_return_pressed(self) -> None:
        """Handle Enter key press."""
        current_tab = self._tab_widget.currentIndex()
        lists = [self._all_list, self._favorites_list, self._recent_list]

        if current_tab < len(lists):
            list_widget = lists[current_tab]
            current_item = list_widget.currentItem()
            if current_item and current_item.flags() & Qt.ItemFlag.ItemIsSelectable:
                node_type = current_item.data(Qt.ItemDataRole.UserRole)
                if node_type:
                    self._confirm_selection(node_type)

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        """Handle double-click activation."""
        # AUD-10802: equality test — `flags() & NoItemFlags` is always 0.
        if item.flags() == Qt.ItemFlag.NoItemFlags:
            return
        node_type = item.data(Qt.ItemDataRole.UserRole)
        if node_type:
            self._confirm_selection(node_type)

    def _confirm_selection(self, node_type: str) -> None:
        """Confirm node selection and close popup."""
        # Track recent nodes
        self._track_recent_node(node_type)

        # Emit signal
        self.node_selected.emit(node_type)

        # Call callback if provided
        if self._on_node_added:
            self._on_node_added(node_type)

        self.accept()

    # ── Keyboard handling ───────────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        """Handle keyboard events."""
        key = event.key()

        # Escape: close
        if key == Qt.Key.Key_Escape:
            self.reject()
            return

        # Enter/Return: confirm selection
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._on_return_pressed()
            return

        # Tab: switch tabs
        if key == Qt.Key.Key_Tab:
            current = self._tab_widget.currentIndex()
            next_tab = (current + 1) % self._tab_widget.count()
            self._tab_widget.setCurrentIndex(next_tab)
            return

        # Shift+Tab: switch tabs backwards
        if key == Qt.Key.Key_Backtab:
            current = self._tab_widget.currentIndex()
            prev_tab = (current - 1) % self._tab_widget.count()
            self._tab_widget.setCurrentIndex(prev_tab)
            return

        # Ctrl+D: toggle favorite on current item
        if key == Qt.Key.Key_D and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._toggle_favorite_current()
            return

        # Let QListWidget handle arrow keys
        super().keyPressEvent(event)

    def _toggle_favorite_current(self) -> None:
        """Toggle favorite status of current item."""
        current_tab = self._tab_widget.currentIndex()
        lists = [self._all_list, self._favorites_list, self._recent_list]

        if current_tab < len(lists):
            list_widget = lists[current_tab]
            current_item = list_widget.currentItem()
            # AUD-10802: equality test — `not (flags() & NoItemFlags)` was
            # always True (NoItemFlags == 0), so headers passed the guard.
            if current_item and current_item.flags() != Qt.ItemFlag.NoItemFlags:
                node_type = current_item.data(Qt.ItemDataRole.UserRole)
                if node_type:
                    self._toggle_favorite(node_type)
                    # Refresh UI
                    self._populate_favorites_tab()

    # ── Favorites management ────────────────────────────────────────────────

    def _toggle_favorite(self, node_type: str) -> None:
        """Toggle a node type in favorites."""
        if node_type in self._favorites:
            self._favorites.remove(node_type)
        else:
            self._favorites.append(node_type)
        self._save_favorites()

    def _load_favorites(self) -> List[str]:
        """Load favorites from settings."""
        settings = QSettings("Topos", "ToposColor")
        favorites = settings.value("node_search/favorites", [])
        if isinstance(favorites, list):
            return favorites
        return []

    def _save_favorites(self) -> None:
        """Save favorites to settings."""
        settings = QSettings("Topos", "ToposColor")
        settings.setValue("node_search/favorites", self._favorites)

    # ── Recent nodes management ─────────────────────────────────────────────

    def _track_recent_node(self, node_type: str) -> None:
        """Track a node as recently used."""
        if node_type in self._recent_nodes:
            self._recent_nodes.remove(node_type)
        self._recent_nodes.insert(0, node_type)
        # Keep only last 20
        self._recent_nodes = self._recent_nodes[:20]
        self._save_recent_nodes()

    def _load_recent_nodes(self) -> List[str]:
        """Load recent nodes from settings."""
        settings = QSettings("Topos", "ToposColor")
        recent = settings.value("node_search/recent", [])
        if isinstance(recent, list):
            return recent
        return []

    def _save_recent_nodes(self) -> None:
        """Save recent nodes to settings."""
        settings = QSettings("Topos", "ToposColor")
        settings.setValue("node_search/recent", self._recent_nodes)

    # ── Focus management ────────────────────────────────────────────────────

    def showEvent(self, event) -> None:
        """Focus search input when shown."""
        super().showEvent(event)
        self._search_input.setFocus()
        self._search_input.selectAll()
