"""
Graph Node Search Popup - Search and navigate nodes within the current graph.

Features:
- Search by node name/title
- Search by node type
- Search by parameter values
- Keyboard navigation (↑↓ Enter Esc)
- Jump to selected node with centering
- Highlight matched nodes
- Navigate between search results (Enter for next)
"""

from typing import TYPE_CHECKING, Optional, Dict, List, Tuple, Callable
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
    QLabel, QWidget, QPushButton,
)

if TYPE_CHECKING:  # pragma: no cover - annotation-only imports
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.ui.node_item import NodeItem

from src.shared.i18n.i18n import I18N


class GraphNodeSearchPopup(QDialog):
    """Modal popup for searching and navigating nodes within the current graph.

    Unlike NodeSearchPopup which adds new nodes, this popup searches
    existing nodes in the graph and allows jumping to them.
    """

    # Signal emitted when a node is selected for navigation
    node_navigate_requested = pyqtSignal(str)  # node_id

    # Signal emitted when search results change (for highlighting)
    search_results_changed = pyqtSignal(list)  # list of node_ids

    def __init__(
        self,
        node_items: Dict[str, "NodeItem"],
        node_graph: "NodeGraph",
        parent: Optional[QWidget] = None,
        on_navigate: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Initialize the graph node search popup.

        Args:
            node_items: Dict of node_id -> NodeItem from NodeEditorWidget
            node_graph: NodeGraph instance for accessing node data
            parent: Parent widget
            on_navigate: Callback when a node is selected for navigation
        """
        super().__init__(parent)
        self._node_items = node_items
        self._node_graph = node_graph
        self._on_navigate = on_navigate

        # Search results
        self._matched_node_ids: List[str] = []
        self._current_match_index: int = 0

        # Build searchable data
        self._build_searchable_data()

        self._setup_ui()
        self._apply_stylesheet()

    # ── Data Building ───────────────────────────────────────────────────────

    def _build_searchable_data(self) -> None:
        """Build searchable data from all nodes in the graph."""
        self._searchable_items: List[Tuple[str, str, str, str, List[str]]] = []

        for node_id, node_item in self._node_items.items():
            node = self._node_graph.get_node(node_id)
            if not node:
                continue

            # Get node title
            title = node_item.node_title

            # Get node type
            node_type = node.node_type

            # Build searchable parameter strings
            param_strings: List[str] = []
            if node.parameters:
                for key, value in node.parameters.items():
                    # Convert value to searchable string
                    if value is not None:
                        param_str = f"{key}:{value}"
                        param_strings.append(param_str.lower())

            # Create searchable entry: (node_id, title, node_type, combined_search_text, param_strings)
            combined = f"{title.lower()} {node_type.lower()} {node_id.lower()} {' '.join(param_strings)}"
            self._searchable_items.append((node_id, title, node_type, combined, param_strings))

    # ── UI Setup ─────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        """Build the popup UI."""
        self.setWindowTitle(I18N.tr("graph_search.title", "Search in Graph"))
        self.setFixedWidth(480)
        self.setFixedHeight(400)
        self.setModal(True)

        # Window flags: tool popup style
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Popup
        )

        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)

        # Search bar
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(
            I18N.tr(
                "graph_search.placeholder",
                "Search nodes by name, type, or parameters... (e.g., 'blur', 'grade.gain', 'output')")
        )
        self._search_input.setFont(QFont("Segoe UI", 11))
        self._search_input.textChanged.connect(self._on_search_changed)
        self._search_input.returnPressed.connect(self._on_return_pressed)
        main_layout.addWidget(self._search_input)

        # Results list
        self._results_list = QListWidget()
        self._results_list.setFont(QFont("Segoe UI", 10))
        self._results_list.setUniformItemSizes(True)
        self._results_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._results_list.setItemAlignment(Qt.AlignmentFlag.AlignLeft)
        self._results_list.currentRowChanged.connect(self._on_selection_changed)
        self._results_list.itemDoubleClicked.connect(self._on_item_activated)
        main_layout.addWidget(self._results_list)

        # Info bar
        info_layout = QHBoxLayout()
        self._result_count_label = QLabel(I18N.tr("graph_search.results_initial", "0 results"))
        self._result_count_label.setFont(QFont("Segoe UI", 9))
        self._result_count_label.setStyleSheet("color: #888;")
        info_layout.addWidget(self._result_count_label)

        info_layout.addStretch()

        # Navigate buttons
        self._prev_btn = QPushButton(I18N.tr("graph_search.prev", "Prev"))
        self._prev_btn.setFont(QFont("Segoe UI", 9))
        self._prev_btn.setFixedWidth(60)
        self._prev_btn.clicked.connect(self._navigate_previous)
        self._prev_btn.setEnabled(False)
        info_layout.addWidget(self._prev_btn)

        self._next_btn = QPushButton(I18N.tr("graph_search.next", "Next"))
        self._next_btn.setFont(QFont("Segoe UI", 9))
        self._next_btn.setFixedWidth(60)
        self._next_btn.clicked.connect(self._navigate_next)
        self._next_btn.setEnabled(False)
        info_layout.addWidget(self._next_btn)

        main_layout.addLayout(info_layout)

        # Help bar
        help_label = QLabel(
            I18N.tr(
                "graph_search.help",
                "↑↓ Navigate  •  Enter Jump  •  Shift+Enter Jump & Close  •  Esc Close")
        )
        help_label.setFont(QFont("Segoe UI", 8))
        help_label.setStyleSheet("color: #999;")
        main_layout.addWidget(help_label)

        # Populate initial results (all nodes)
        self._populate_results()

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
            QPushButton {
                background-color: #3c3c3c;
                color: #e0e0e0;
                border: 1px solid #555;
                border-radius: 3px;
                padding: 4px 8px;
            }
            QPushButton:hover {
                background-color: #4a4a4a;
            }
            QPushButton:pressed {
                background-color: #5a5a5a;
            }
            QPushButton:disabled {
                background-color: #2c2c2c;
                color: #666;
            }
        """)

    # ── Population & Search ──────────────────────────────────────────────────

    def _populate_results(self) -> None:
        """Populate the results list with all nodes."""
        self._results_list.clear()

        for node_id, title, node_type, combined, params in self._searchable_items:
            item = QListWidgetItem(f"{title}  [{node_type}]")
            item.setData(Qt.ItemDataRole.UserRole, node_id)
            # Show node type and ID in tooltip
            item.setToolTip(f"ID: {node_id}\nType: {node_type}")
            self._results_list.addItem(item)

        self._matched_node_ids = [item[0] for item in self._searchable_items]
        self._update_result_count()

    def _on_search_changed(self, text: str) -> None:
        """Handle search text changes and filter results."""
        filter_text = text.lower().strip()

        self._results_list.clear()
        self._matched_node_ids.clear()

        if not filter_text:
            # Show all nodes when no filter
            self._populate_results()
            # Emit signal to clear highlights
            self.search_results_changed.emit([])
            return

        # Search by different criteria
        for node_id, title, node_type, combined, params in self._searchable_items:
            matches = self._check_match(filter_text, title, node_type, node_id, params)

            if matches:
                # Build display text showing what matched
                display_text = self._build_display_text(
                    node_id, title, node_type, filter_text, params
                )

                item = QListWidgetItem(display_text)
                item.setData(Qt.ItemDataRole.UserRole, node_id)
                item.setToolTip(f"ID: {node_id}\nType: {node_type}")
                self._results_list.addItem(item)
                self._matched_node_ids.append(node_id)

        self._update_result_count()

        # Emit signal for highlighting matched nodes
        self.search_results_changed.emit(self._matched_node_ids)

        # Select first result if any
        if self._results_list.count() > 0:
            self._results_list.setCurrentRow(0)
            self._current_match_index = 0
            self._navigate_to_current()

    def _check_match(
        self,
        filter_text: str,
        title: str,
        node_type: str,
        node_id: str,
        params: List[str],
    ) -> bool:
        """Check if filter text matches any searchable field.

        Matching rules:
        - Exact/partial match in title
        - Exact/partial match in node_type
        - Match in node_id (case-insensitive)
        - Match in parameter format: "key:value"
        - Match in parameter format: "key:" (any value)
        """
        title_lower = title.lower()
        type_lower = node_type.lower()
        id_lower = node_id.lower()

        # Direct matches
        if filter_text in title_lower or title_lower.startswith(filter_text):
            return True
        if filter_text in type_lower or type_lower.startswith(filter_text):
            return True
        if filter_text in id_lower:
            return True

        # Parameter matches
        # Support "key:value" format
        if "." in filter_text or ":" in filter_text:
            for param_str in params:
                if filter_text in param_str:
                    return True
        else:
            # Check if filter matches any parameter key or value
            for param_str in params:
                key, sep, value = param_str.partition(":")
                if filter_text in key or (sep and filter_text in value):
                    return True

        return False

    def _build_display_text(
        self,
        node_id: str,
        title: str,
        node_type: str,
        filter_text: str,
        params: List[str],
    ) -> str:
        """Build display text showing matched parts."""
        # Base display
        base = f"{title}  [{node_type}]"

        # Find matching parameters to show in display
        matching_params: List[str] = []
        for param_str in params:
            if filter_text in param_str:
                # Extract key:value for display
                key, sep, value = param_str.partition(":")
                matching_params.append(f"{key}:{value}")

        if matching_params:
            # Show up to 2 matching parameters
            param_display = ", ".join(matching_params[:2])
            if len(matching_params) > 2:
                param_display += "..."
            base += f"  • {param_display}"

        return base

    def _update_result_count(self) -> None:
        """Update the result count label and navigation buttons."""
        count = len(self._matched_node_ids)
        self._result_count_label.setText(f"{count} results")

        self._prev_btn.setEnabled(count > 1)
        self._next_btn.setEnabled(count > 1)

    # ── Navigation ───────────────────────────────────────────────────────────

    def _on_selection_changed(self, row: int) -> None:
        """Handle selection change in results list."""
        if row >= 0 and row < len(self._matched_node_ids):
            self._current_match_index = row
            self._navigate_to_current()

    def _navigate_to_current(self) -> None:
        """Navigate to the currently selected node without closing popup."""
        if self._current_match_index < len(self._matched_node_ids):
            node_id = self._matched_node_ids[self._current_match_index]
            if self._on_navigate:
                self._on_navigate(node_id)
            self.node_navigate_requested.emit(node_id)

    def _navigate_previous(self) -> None:
        """Navigate to previous result."""
        if len(self._matched_node_ids) > 0:
            self._current_match_index -= 1
            if self._current_match_index < 0:
                self._current_match_index = len(self._matched_node_ids) - 1
            self._results_list.setCurrentRow(self._current_match_index)
            self._navigate_to_current()

    def _navigate_next(self) -> None:
        """Navigate to next result."""
        if len(self._matched_node_ids) > 0:
            self._current_match_index += 1
            if self._current_match_index >= len(self._matched_node_ids):
                self._current_match_index = 0
            self._results_list.setCurrentRow(self._current_match_index)
            self._navigate_to_current()

    def _on_return_pressed(self) -> None:
        """Handle Enter key - navigate and close."""
        current_item = self._results_list.currentItem()
        if current_item:
            node_id = current_item.data(Qt.ItemDataRole.UserRole)
            if self._on_navigate:
                self._on_navigate(node_id)
            self.node_navigate_requested.emit(node_id)

        # Check modifiers: Shift+Enter keeps popup open
        if not (self._search_input.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.accept()

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        """Handle double-click - navigate and close."""
        node_id = item.data(Qt.ItemDataRole.UserRole)
        if node_id:
            if self._on_navigate:
                self._on_navigate(node_id)
            self.node_navigate_requested.emit(node_id)
            self.accept()

    # ── Keyboard handling ────────────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        """Handle keyboard events."""
        key = event.key()

        # Escape: close without navigation
        if key == Qt.Key.Key_Escape:
            # Clear highlights before closing
            self.search_results_changed.emit([])
            self.reject()
            return

        # Enter/Return: navigate and close (or Shift+Enter navigate without close)
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._on_return_pressed()
            return

        # Up/Down: navigate results
        if key == Qt.Key.Key_Up:
            self._navigate_previous()
            return
        if key == Qt.Key.Key_Down:
            self._navigate_next()
            return

        # AUD-9913: 原 Ctrl+Up/Ctrl+Down 分支不可达 —— 上方 Up/Down 分支
        # 不区分修饰键先行处理并 return，此处永不执行，按死代码移除。

        # Let parent handle other keys
        super().keyPressEvent(event)

    # ── Focus management ─────────────────────────────────────────────────────

    def showEvent(self, event) -> None:
        """Focus search input when shown."""
        super().showEvent(event)
        self._search_input.setFocus()
        self._search_input.selectAll()

    def closeEvent(self, event) -> None:
        """Clear highlights when popup closes."""
        self.search_results_changed.emit([])
        super().closeEvent(event)