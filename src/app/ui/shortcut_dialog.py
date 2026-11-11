"""
ShortcutDialog - DaVinci Resolve-style keyboard shortcut customization dialog.

Features:
- Left panel: Category tree for browsing shortcut groups
- Right panel: Table listing actions with editable shortcuts
- Search bar for quick filtering across all categories
- Double-click or press Enter to edit a shortcut via inline QKeySequenceEdit
- Conflict detection with visual warning (red highlight)
- Reset Selected / Reset All / Save / Cancel buttons
- Inherits application dark theme
"""

import logging
from typing import List, Optional, Dict

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTreeWidget,
    QTreeWidgetItem, QTableWidget, QTableWidgetItem, QHeaderView,
    QPushButton, QLineEdit, QLabel, QKeySequenceEdit, QWidget,
    QAbstractItemView, QMessageBox, QStyledItemDelegate, QStyleOptionViewItem,
    QFrame
)
from PyQt6.QtGui import QKeySequence, QColor
from PyQt6.QtCore import Qt

from src.shared.ui.panel_splitter import PanelSplitter
from src.shared.ui.shortcut_manager import ShortcutManager, ShortcutAction
from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)


class ShortcutEditDelegate(QStyledItemDelegate):
    """Custom delegate for editing shortcuts in the table.
    
    Shows a QKeySequenceEdit widget when the user double-clicks the shortcut column.
    Supports clearing the shortcut with Backspace/Delete.
    """

    def __init__(self, dialog: 'ShortcutDialog', parent=None) -> None:
        super().__init__(parent)
        self._dialog = dialog

    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index) -> QWidget:
        """Create a QKeySequenceEdit for the shortcut column."""
        editor = QKeySequenceEdit(parent)
        editor.setStyleSheet("""
            QKeySequenceEdit {
                background-color: #252525;
                border: 2px solid #ff8800;
                border-radius: 3px;
                padding: 4px;
                color: #E0E0E0;
                font-size: 13px;
            }
        """)
        return editor

    def setEditorData(self, editor: QKeySequenceEdit, index) -> None:
        """Set the current shortcut in the editor."""
        shortcut_str = index.data(Qt.ItemDataRole.DisplayRole)
        if shortcut_str and shortcut_str != "—":
            editor.setKeySequence(QKeySequence(shortcut_str))
        else:
            editor.setKeySequence(QKeySequence())

    def setModelData(self, editor: QKeySequenceEdit, model, index) -> None:
        """Apply the edited shortcut."""
        key_seq = editor.keySequence()
        shortcut_str = key_seq.toString(QKeySequence.SequenceFormat.NativeText)

        # Get the action_id from the UserRole data of the first column
        action_id_index = model.index(index.row(), 0)
        action_id = action_id_index.data(Qt.ItemDataRole.UserRole)

        if action_id and self._dialog._manager:
            self._dialog._apply_shortcut(action_id, shortcut_str)

    def updateEditorGeometry(self, editor: QWidget, option: QStyleOptionViewItem, index) -> None:
        """Position the editor within the table cell."""
        editor.setGeometry(option.rect)


class ShortcutDialog(QDialog):
    """DaVinci Resolve-style keyboard shortcut customization dialog.

    Provides a searchable, categorized view of all registered shortcuts
    with inline editing, conflict detection, and reset capabilities.
    """

    # Column indices
    COL_ACTION = 0
    COL_SHORTCUT = 1
    COL_DEFAULT = 2

    @staticmethod
    def _config_bool(value, default: bool = False) -> bool:
        """Safely read boolean config value, handling None correctly.

        Unlike `value or default`, this returns False when value is explicitly False.
        """
        return default if value is None else bool(value)

    def __init__(self, manager: ShortcutManager, parent=None,
                 enabled_features: Optional[set] = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._enabled_features = enabled_features
        self._search_query: str = ""
        self._current_category: str = ""
        # AUD-4103: no write-only "_updating_table" flag here — the table
        # has no itemChanged connection and the fill below is purely
        # programmatic QTableWidgetItem construction, which cannot echo
        # any signal. A set-but-never-read flag only suggested a recursion
        # guard existed when it did not.

        # [AUD-4100] Cancel 语义：ShortcutManager 的 set_shortcut/
        # remove_shortcut/reset_all/import_preset 每次调用都即时落盘
        # config.json 并同步 QAction，纯 reject() 会让本会话全部改动
        # 保留生效。进入对话框时快照自定义快捷键映射，取消时经公开
        # API 重放撤销。（直接读 _custom_shortcuts 私有成员的封装问题
        # 见 AUD-4102；在 ShortcutManager 上补公开快照 API 不在本批
        # 白名单内。）
        self._entry_custom_shortcuts: Dict[str, str] = dict(manager._custom_shortcuts)

        # [AUD-4101] 关闭即销毁：两个调用点（menu_toolbar_controller 的
        # Shortcuts 菜单、偏好设置 Shortcuts 页）均以父窗口为 parent +
        # exec()，无销毁标记时 C++ 对象随打开次数永久累积（welcome_
        # dialog [R2-2] 同款问题）。注意实测 QDialog.accept()/reject()
        # 走的 done() 路径不触发 WA_DeleteOnClose 的删除（只有 close()
        # 路径触发），故再挂 finished→deleteLater 兜底两条关闭路径。
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.finished.connect(self.deleteLater)

        # Read conflict display config
        from src.shared.config.config_manager import ConfigManager
        cfg = ConfigManager()
        self._show_conflicts = self._config_bool(cfg.get("show_shortcut_conflicts"), default=True)

        self.setWindowTitle(I18N.tr("shortcut_dialog.title", "Keyboard Shortcuts"))
        self.setMinimumSize(850, 600)
        self.resize(900, 650)
        self._setup_ui()
        self._apply_styles()

        # Populate initial data
        self._populate_categories()
        self._select_first_category()

    # ── UI Setup ──────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        """Build the dialog UI layout."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(10)

        # ── Header ──
        header_layout = QHBoxLayout()
        self._title_label = QLabel(
            I18N.tr("shortcut_dialog.title", "Keyboard Shortcuts")
        )
        self._title_label.setObjectName("DialogTitle")
        header_layout.addWidget(self._title_label)
        header_layout.addStretch()
        main_layout.addLayout(header_layout)

        # ── Search Bar ──
        search_container = QFrame()
        search_container.setObjectName("SearchBox")
        search_layout = QHBoxLayout(search_container)
        search_layout.setContentsMargins(10, 6, 10, 6)
        search_layout.setSpacing(8)

        search_label = QLabel("🔍")
        search_label.setStyleSheet("font-size: 14px; background: transparent; border: none;")
        search_layout.addWidget(search_label)

        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(
            I18N.tr("shortcut_dialog.search_placeholder", "Search shortcuts...")
        )
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.setFrame(False)
        self._search_edit.textChanged.connect(self._on_search_changed)
        search_layout.addWidget(self._search_edit)

        main_layout.addWidget(search_container)

        # ── Main Content: Splitter ──
        splitter = PanelSplitter(Qt.Orientation.Horizontal)

        # Left: Category Tree
        self._category_tree = QTreeWidget()
        self._category_tree.setHeaderLabel(
            I18N.tr("shortcut_dialog.category", "Category")
        )
        self._category_tree.setMinimumWidth(180)
        self._category_tree.setMaximumWidth(250)
        self._category_tree.currentItemChanged.connect(self._on_category_changed)
        splitter.addWidget(self._category_tree)

        # Right: Shortcut Table
        table_container = QWidget()
        table_layout = QVBoxLayout(table_container)
        table_layout.setContentsMargins(0, 0, 0, 0)

        self._table = QTableWidget()
        self._table.setColumnCount(3)
        self._table.setHorizontalHeaderLabels([
            I18N.tr("shortcut_dialog.action", "Action"),
            I18N.tr("shortcut_dialog.shortcut", "Shortcut"),
            I18N.tr("shortcut_dialog.default", "Default"),
        ])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | 
                                     QAbstractItemView.EditTrigger.EditKeyPressed)
        self._table.setAlternatingRowColors(True)

        # Install delegate for shortcut column editing
        self._shortcut_delegate = ShortcutEditDelegate(self)
        self._table.setItemDelegateForColumn(self.COL_SHORTCUT, self._shortcut_delegate)

        table_layout.addWidget(self._table)
        splitter.addWidget(table_container)

        # Set splitter proportions (20% tree, 80% table)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 8)

        main_layout.addWidget(splitter, stretch=1)

        # ── Conflict Warning Label ──
        self._conflict_label = QLabel("")
        self._conflict_label.setObjectName("ConflictLabel")
        self._conflict_label.setWordWrap(True)
        self._conflict_label.hide()
        main_layout.addWidget(self._conflict_label)

        # ── Info Label ──
        info_label = QLabel(
            I18N.tr("shortcut_dialog.help_text", 
                    "Double-click the Shortcut column to edit. Press Backspace in the editor to clear.")
        )
        info_label.setObjectName("InfoLabel")
        main_layout.addWidget(info_label)

        # ── Bottom Buttons ──
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self._reset_selected_btn = QPushButton(
            I18N.tr("shortcut_dialog.reset_selected", "Reset Selected")
        )
        self._reset_selected_btn.setObjectName("ResetButton")
        self._reset_selected_btn.clicked.connect(self._on_reset_selected)

        self._reset_all_btn = QPushButton(
            I18N.tr("shortcut_dialog.reset_all", "Reset All")
        )
        self._reset_all_btn.setObjectName("ResetButton")
        self._reset_all_btn.clicked.connect(self._on_reset_all)

        btn_layout.addWidget(self._reset_selected_btn)
        btn_layout.addWidget(self._reset_all_btn)

        # P2-01: preset export/import — move a shortcut map between
        # machines or share it as a team standard.
        self._export_btn = QPushButton(
            I18N.tr("shortcut_dialog.export_preset", "Export…")
        )
        self._export_btn.clicked.connect(self._on_export_preset)
        self._import_btn = QPushButton(
            I18N.tr("shortcut_dialog.import_preset", "Import…")
        )
        self._import_btn.clicked.connect(self._on_import_preset)
        btn_layout.addWidget(self._export_btn)
        btn_layout.addWidget(self._import_btn)
        btn_layout.addStretch()

        self._cancel_btn = QPushButton(
            I18N.tr("shortcut_dialog.cancel", "Cancel")
        )
        self._cancel_btn.clicked.connect(self.reject)

        self._save_btn = QPushButton(
            I18N.tr("shortcut_dialog.save", "OK")
        )
        self._save_btn.setObjectName("PrimaryButton")
        self._save_btn.setDefault(True)
        self._save_btn.clicked.connect(self.accept)

        btn_layout.addWidget(self._cancel_btn)
        btn_layout.addWidget(self._save_btn)
        main_layout.addLayout(btn_layout)

    def _on_export_preset(self) -> None:
        """Write the current shortcut map to a JSON preset file (P2-01)."""
        import json

        from PyQt6.QtWidgets import QFileDialog

        manager = ShortcutManager.instance()
        if manager is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            I18N.tr("shortcut_dialog.export_title", "Export Shortcut Preset"),
            "shortcuts.json",
            "JSON (*.json)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(manager.export_preset(), fh, indent=2, ensure_ascii=False)
        except OSError as e:
            QMessageBox.warning(
                self,
                I18N.tr("shortcut_dialog.export_failed", "Export Failed"),
                str(e),
            )

    def _on_import_preset(self) -> None:
        """Load a preset with conflict re-validation (P2-01)."""
        import json

        from PyQt6.QtWidgets import QFileDialog

        manager = ShortcutManager.instance()
        if manager is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self,
            I18N.tr("shortcut_dialog.import_title", "Import Shortcut Preset"),
            "",
            "JSON (*.json)",
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            QMessageBox.warning(
                self,
                I18N.tr("shortcut_dialog.import_failed", "Import Failed"),
                str(e),
            )
            return

        replace = QMessageBox.question(
            self,
            I18N.tr("shortcut_dialog.import_mode_title", "Import Mode"),
            I18N.tr(
                "shortcut_dialog.import_mode_question",
                "Yes = Replace all shortcuts with the preset, "
                "No = Merge (keep shortcuts absent from the preset)",
            ),
        )
        mode = "replace" if replace == QMessageBox.StandardButton.Yes else "merge"
        report = manager.import_preset(data, mode=mode)

        parts = [
            I18N.tr(
                "shortcut_dialog.import_applied",
                "Applied: {n}",
                n=len(report.applied),
            )
        ]
        if report.unknown:
            parts.append(
                I18N.tr(
                    "shortcut_dialog.import_unknown",
                    "Unknown actions skipped: {ids}",
                    ids=", ".join(report.unknown),
                )
            )
        for seq, action_ids in report.conflicts:
            parts.append(
                I18N.tr(
                    "shortcut_dialog.import_conflict",
                    "Conflict on {seq}: {ids} (skipped)",
                    seq=seq,
                    ids=", ".join(action_ids),
                )
            )
        parts.extend(report.errors)
        QMessageBox.information(
            self,
            I18N.tr("shortcut_dialog.import_done", "Import Complete"),
            "\n".join(parts),
        )
        self._refresh_table()

    def _apply_styles(self) -> None:
        """Apply dark theme styles matching the project's global theme.
        
        Color reference (from style_sheet.py):
        - Accent orange: #ff8800 / hover: #ffaa44 / pressed: #cc6600
        - Deepest bg: #1a1a1a
        - Panel bg: #27272b
        - Input bg: #252525
        - Button bg: #3d3d3d
        - Borders: #3a3a3a ~ #555555
        - Text primary: #E0E0E0
        - Text secondary: #cccccc
        - Text hint: #888888
        """
        self.setStyleSheet("""
            /* === Dialog Background === */
            QDialog {
                background-color: #27272b;
                color: #E0E0E0;
            }

            /* === Title Label === */
            QLabel#DialogTitle {
                font-size: 16px;
                font-weight: bold;
                color: #ff8800;
                background: transparent;
                border: none;
            }

            /* === Search Box Container === */
            QFrame#SearchBox {
                background-color: #252525;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
            }
            QFrame#SearchBox:focus-within {
                border-color: #ff8800;
            }

            /* === Search Input === */
            QLineEdit {
                background-color: transparent;
                border: none;
                padding: 4px 0px;
                color: #E0E0E0;
                font-size: 13px;
            }
            QLineEdit:focus {
                border: none;
            }

            /* === Category Tree (Left Panel) === */
            QTreeWidget {
                background-color: #252525;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                color: #E0E0E0;
                font-size: 13px;
                outline: none;
                padding: 4px;
            }
            QTreeWidget::item {
                padding: 6px 8px;
                border-radius: 3px;
            }
            QTreeWidget::item:selected {
                background-color: #ff8800;
                color: #ffffff;
            }
            QTreeWidget::item:hover {
                background-color: #333333;
            }
            QTreeWidget::branch {
                background-color: transparent;
            }

            /* === Shortcut Table (Right Panel) === */
            QTableWidget {
                background-color: #252525;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                color: #E0E0E0;
                font-size: 13px;
                gridline-color: #3a3a3a;
                outline: none;
            }
            QTableWidget::item {
                padding: 6px 10px;
            }
            QTableWidget::item:selected {
                background-color: #333333;
            }
            QTableWidget::item:alternate {
                background-color: #28282c;
            }

            /* === Table Header === */
            QHeaderView::section {
                background-color: #1e1e1e;
                color: #888888;
                font-weight: bold;
                font-size: 12px;
                padding: 8px 10px;
                border: none;
                border-bottom: 1px solid #ff8800;
            }

            /* === Standard Buttons === */
            QPushButton {
                background-color: #3d3d3d;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 6px 16px;
                color: #dddddd;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #4d4d4d;
                border-color: #ff8800;
            }
            QPushButton:pressed {
                background-color: #2d2d2d;
            }

            /* === Primary (Save) Button === */
            QPushButton#PrimaryButton {
                background-color: #ff8800;
                border: none;
                border-radius: 3px;
                padding: 6px 24px;
                color: #ffffff;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton#PrimaryButton:hover {
                background-color: #ffaa44;
            }
            QPushButton#PrimaryButton:pressed {
                background-color: #cc6600;
            }

            /* === Reset Buttons === */
            QPushButton#ResetButton {
                background-color: transparent;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 5px 12px;
                color: #888888;
                font-size: 12px;
            }
            QPushButton#ResetButton:hover {
                border-color: #ff8800;
                color: #ff8800;
            }

            /* === Conflict Warning Label === */
            QLabel#ConflictLabel {
                color: #ff4444;
                font-size: 12px;
                padding: 4px;
                background: transparent;
                border: none;
            }

            /* === Info Hint Label === */
            QLabel#InfoLabel {
                color: #888888;
                font-size: 11px;
                padding: 2px 4px;
                background: transparent;
                border: none;
            }

            /* === Scrollbars (match global) === */
            QScrollBar:vertical {
                border: none;
                background: #1e1e1e;
                width: 10px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: #3d3d3d;
                min-height: 20px;
                border-radius: 5px;
                margin: 2px;
            }
            QScrollBar::handle:vertical:hover {
                background: #555555;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar:horizontal {
                border: none;
                background: #1e1e1e;
                height: 10px;
                margin: 0px;
            }
            QScrollBar::handle:horizontal {
                background: #3d3d3d;
                min-width: 20px;
                border-radius: 5px;
                margin: 2px;
            }
            QScrollBar::handle:horizontal:hover {
                background: #555555;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0px;
            }
        """)

    # ── Data Population ───────────────────────────────────────────

    def _populate_categories(self) -> None:
        """Populate the category tree with all shortcut categories.

        Categories are derived from the filtered set of actions (respecting
        enabled_features) so disabled-module shortcuts never appear.
        """
        self._category_tree.clear()

        # Add "All" category at top
        all_item = QTreeWidgetItem()
        all_item.setText(0, I18N.tr("shortcut_dialog.all", "All"))
        all_item.setData(0, Qt.ItemDataRole.UserRole, "__all__")
        all_item.setExpanded(True)
        self._category_tree.addTopLevelItem(all_item)

        # Derive categories from the filtered action list
        filtered_actions = self._manager.get_actions_by_feature(self._enabled_features)
        seen_cats: list[str] = []
        for sa in filtered_actions:
            if sa.category not in seen_cats:
                seen_cats.append(sa.category)

        for cat in seen_cats:
            cat_item = QTreeWidgetItem()
            cat_item.setText(0, cat)
            cat_item.setData(0, Qt.ItemDataRole.UserRole, cat)
            all_item.addChild(cat_item)

        # Auto-expand "All"
        all_item.setExpanded(True)

    def _select_first_category(self) -> None:
        """Select the 'All' category by default."""
        root = self._category_tree.topLevelItem(0)
        if root:
            self._category_tree.setCurrentItem(root)
            self._on_category_changed(root, None)

    def _populate_table(self, actions: List[ShortcutAction]) -> None:
        """Fill the table with the given actions.
        
        Args:
            actions: List of ShortcutAction objects to display
        """
        # AUD-4103: no recursion guard needed — programmatic fills don't
        # emit itemChanged and no such handler is connected (see __init__).
        self._table.setRowCount(len(actions))

        for row, sa in enumerate(actions):
            # Action Name column
            name_item = QTableWidgetItem(sa.label)
            name_item.setData(Qt.ItemDataRole.UserRole, sa.id)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setToolTip(sa.id)

            # Check if custom shortcut is active (different from default)
            current_shortcut = self._manager.get_shortcut(sa.id)
            is_custom = (sa.id in self._manager._custom_shortcuts)

            # Shortcut column
            shortcut_item = QTableWidgetItem(current_shortcut if current_shortcut else "—")
            shortcut_item.setData(Qt.ItemDataRole.UserRole, sa.id)
            if is_custom and current_shortcut:
                # Highlight custom shortcuts
                shortcut_item.setForeground(QColor("#ff8800"))
                font = shortcut_item.font()
                font.setBold(True)
                shortcut_item.setFont(font)

            # Check for conflicts
            if current_shortcut and self._show_conflicts:
                conflicts = self._manager.find_conflicts(current_shortcut, sa.id)
                if conflicts:
                    shortcut_item.setBackground(QColor(80, 20, 20))
                    shortcut_item.setForeground(QColor("#ff4444"))
                    conflict_names = ", ".join(c.label for c in conflicts)
                    shortcut_item.setToolTip(I18N.tr(
                        "shortcut_dialog.conflict_tooltip",
                        "⚠ Conflict with: {names}",
                        names=conflict_names
                    ))

            # Default shortcut column
            default_item = QTableWidgetItem(sa.default_shortcut if sa.default_shortcut else "—")
            default_item.setFlags(default_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            default_item.setForeground(QColor("#888888"))

            self._table.setItem(row, self.COL_ACTION, name_item)
            self._table.setItem(row, self.COL_SHORTCUT, shortcut_item)
            self._table.setItem(row, self.COL_DEFAULT, default_item)

    # ── Event Handlers ────────────────────────────────────────────

    def _on_category_changed(self, current: Optional[QTreeWidgetItem],
                              previous: Optional[QTreeWidgetItem]) -> None:
        """Handle category selection change in the tree."""
        if not current:
            return

        category = current.data(0, Qt.ItemDataRole.UserRole)
        self._current_category = category

        if category == "__all__":
            if self._search_query:
                actions = self._filtered_search(self._search_query)
            else:
                actions = self._manager.get_actions_by_feature(self._enabled_features)
        else:
            if self._search_query:
                all_in_cat = self._get_filtered_category_actions(category)
                query_lower = self._search_query.lower()
                actions = [
                    sa for sa in all_in_cat
                    if query_lower in sa.label.lower() or query_lower in sa.id.lower()
                ]
            else:
                actions = self._get_filtered_category_actions(category)

        self._populate_table(actions)

    def _on_search_changed(self, text: str) -> None:
        """Handle search text change."""
        self._search_query = text.strip()

        if self._search_query:
            # Search across all categories (filtered by enabled features)
            actions = self._filtered_search(self._search_query)
            self._populate_table(actions)
        else:
            # Re-apply current category filter
            current = self._category_tree.currentItem()
            if current:
                self._on_category_changed(current, None)

    def _on_reset_selected(self) -> None:
        """Reset the currently selected row's shortcut to default."""
        row = self._table.currentRow()
        if row < 0:
            return

        action_item = self._table.item(row, self.COL_ACTION)
        if not action_item:
            return

        action_id = action_item.data(Qt.ItemDataRole.UserRole)
        if action_id:
            self._manager.remove_shortcut(action_id)
            self._refresh_table()

    def _on_reset_all(self) -> None:
        """Reset all shortcuts to defaults with confirmation."""
        reply = QMessageBox.question(
            self,
            I18N.tr("shortcut_dialog.reset_all_title", "Reset All Shortcuts"),
            I18N.tr("shortcut_dialog.reset_all_confirm", 
                    "Are you sure you want to reset all shortcuts to their defaults?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._manager.reset_all()
            self._refresh_table()

    # ── Feature-filtered helpers ──────────────────────────────────

    def _filtered_search(self, query: str) -> List[ShortcutAction]:
        """Search actions filtered by enabled features.

        Args:
            query: Search string

        Returns:
            List of matching ShortcutActions within enabled features.
        """
        all_matches = self._manager.find_actions(query)
        if self._enabled_features is None:
            return all_matches
        return [
            sa for sa in all_matches
            if sa.feature_id is None or sa.feature_id in self._enabled_features
        ]

    def _get_filtered_category_actions(self, category: str) -> List[ShortcutAction]:
        """Get actions for a category, filtered by enabled features.

        Args:
            category: Category name

        Returns:
            List of ShortcutActions in the category for enabled features.
        """
        cat_actions = self._manager.get_actions_by_category(category)
        if self._enabled_features is None:
            return cat_actions
        return [
            sa for sa in cat_actions
            if sa.feature_id is None or sa.feature_id in self._enabled_features
        ]

    # ── Shortcut Application ──────────────────────────────────────

    def _apply_shortcut(self, action_id: str, key_sequence: str) -> None:
        """Apply a shortcut change with conflict detection.
        
        Args:
            action_id: The action to modify
            key_sequence: The new key sequence string
        """
        # Check for conflicts
        conflicts = self._manager.find_conflicts(key_sequence, action_id) if self._show_conflicts else []
        if conflicts:
            conflict_names = ", ".join(c.label for c in conflicts)

            self._conflict_label.setText(I18N.tr(
                "shortcut_dialog.conflict_warning",
                "⚠ This shortcut is already used by: {names}",
                names=conflict_names
            ))
            self._conflict_label.show()

            # Ask user if they want to proceed (will override the conflict)
            reply = QMessageBox.warning(
                self,
                I18N.tr("shortcut_dialog.conflict_title", "Shortcut Conflict"),
                I18N.tr(
                    "shortcut_dialog.conflict_message",
                    "This shortcut is already assigned to:\n\n{names}\n\n"
                    "Do you want to reassign it? The conflicting action will lose its shortcut.",
                    names=conflict_names
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                self._refresh_table()
                return

            # Clear conflicting shortcuts
            for conflict_sa in conflicts:
                self._manager.set_shortcut(conflict_sa.id, "")
        else:
            self._conflict_label.hide()

        # Apply the new shortcut
        self._manager.set_shortcut(action_id, key_sequence)
        self._refresh_table()

    def _refresh_table(self) -> None:
        """Refresh the table to reflect current shortcut state."""
        current = self._category_tree.currentItem()
        if current:
            self._on_category_changed(current, None)
        self._conflict_label.hide()

    # ── Cancel Semantics (AUD-4100) ───────────────────────────────

    def reject(self) -> None:
        """Cancel: undo every shortcut change made in this session.

        Edits apply live through ShortcutManager (each write persists
        immediately), so Cancel must replay the entry snapshot taken in
        __init__ before closing.
        """
        self._restore_entry_shortcuts()
        super().reject()

    def _restore_entry_shortcuts(self) -> None:
        """Replay the entry snapshot of custom shortcuts via public API.

        Keys added during the session (import/reset side effects) are
        removed; keys changed/removed are set back. ``set_shortcut`` and
        ``remove_shortcut`` re-apply to QActions and persist to config.json
        themselves, leaving manager state exactly as before the dialog.
        """
        manager = self._manager
        current = dict(manager._custom_shortcuts)
        if current == self._entry_custom_shortcuts:
            return  # nothing changed this session — no disk churn
        for action_id, key_sequence in self._entry_custom_shortcuts.items():
            if current.get(action_id) == key_sequence:
                continue  # unchanged — skip a redundant config write
            manager.set_shortcut(action_id, key_sequence)
        for action_id in current:
            if action_id not in self._entry_custom_shortcuts:
                manager.remove_shortcut(action_id)

    # ── Keyboard Events ───────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        """Handle key press events for the dialog.
        
        Escape closes the dialog. Other keys are forwarded to the search bar
        if it has focus.
        """
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)
