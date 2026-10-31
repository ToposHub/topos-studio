"""
Relink Media Dialog - UI for relinking offline media assets.

P6.3 Offline/Relink/Proxy functionality.
P3 统一工程包架构方案 - Relink/离线素材统一入口。

Provides a dialog for relinking offline media assets to new file paths.
Supports single asset relink and batch relink with directory mapping.
支持 MediaAsset (EditProject) 和 MediaAssetRef (ToposProject) 两种素材类型。

Usage:
    from src.features.edit.ui.relink_media_dialog import RelinkMediaDialog

    # MediaAsset (EditProject) - 单个素材重链接
    dialog = RelinkMediaDialog(assets=[offline_asset], parent=self)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        relink_map = dialog.get_relink_map()
        # relink_map: {asset_id: new_path}

    # MediaAssetRef (ToposProject) - 批量重链接
    dialog = RelinkMediaDialog.from_asset_refs(asset_refs, parent=self)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        relink_map = dialog.get_relink_map()
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Dict, List, Union, TYPE_CHECKING, Any

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QFileDialog, QHeaderView,
    QDialogButtonBox, QAbstractItemView, QMessageBox, QWidget,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

from src.features.edit.core.project import MediaAsset
from src.features.edit.ui.edit_style import (
    get_color,
)

if TYPE_CHECKING:
    from src.shared.project.topos_project import MediaAssetRef

logger = logging.getLogger(__name__)


class RelinkMediaDialog(QDialog):
    """
    Dialog for relinking offline media assets.

    Displays a table of offline assets with their current paths.
    Users can select new paths for each asset individually or
    use batch relink to update multiple assets at once.

    支持 MediaAsset (EditProject) 和 MediaAssetRef (ToposProject) 两种素材类型。

    Attributes:
        _assets: List of offline assets to relink (MediaAsset or MediaAssetRef)
        _relink_map: Mapping of asset_id -> new_path
        _asset_type: 素材类型 ("MediaAsset" 或 "MediaAssetRef")
    """

    def __init__(
        self,
        assets: List[MediaAsset],
        parent: Optional[QWidget] = None
    ) -> None:
        """
        Initialize Relink Media Dialog (for MediaAsset from EditProject).

        Args:
            assets: List of offline MediaAsset to relink
            parent: Parent widget
        """
        super().__init__(parent)
        self._assets: List[Union[MediaAsset, Any]] = assets
        self._relink_map: Dict[str, str] = {}
        self._asset_type: str = "MediaAsset"

        self._setup_ui()
        self._populate_table()

    @classmethod
    def from_asset_refs(
        cls,
        asset_refs: List["MediaAssetRef"],
        parent: Optional[QWidget] = None
    ) -> "RelinkMediaDialog":
        """
        Factory method to create dialog for MediaAssetRef from ToposProject.

        Args:
            asset_refs: List of offline MediaAssetRef to relink
            parent: Parent widget

        Returns:
            RelinkMediaDialog instance
        """
        # 创建一个临时实例
        dialog = cls.__new__(cls)
        QDialog.__init__(dialog, parent)
        dialog._assets = asset_refs
        dialog._relink_map: Dict[str, str] = {}
        dialog._asset_type = "MediaAssetRef"

        dialog._setup_ui()
        dialog._populate_table()

        return dialog

    @classmethod
    def from_offline_report(
        cls,
        offline_assets: List[Dict[str, Any]],
        parent: Optional[QWidget] = None
    ) -> "RelinkMediaDialog":
        """
        Factory method to create dialog from OfflineAssetReport.offline_assets.

        Args:
            offline_assets: List of offline asset dicts from OfflineAssetReport
            parent: Parent widget

        Returns:
            RelinkMediaDialog instance
        """
        dialog = cls.__new__(cls)
        QDialog.__init__(dialog, parent)
        dialog._assets = offline_assets
        dialog._relink_map: Dict[str, str] = {}
        dialog._asset_type = "Dict"

        dialog._setup_ui()
        dialog._populate_table()

        return dialog

    def _setup_ui(self) -> None:
        """Set up the dialog UI."""
        from src.shared.i18n.i18n import I18N
        self.setWindowTitle(I18N.tr("relink.dialog_title", "Relink Media"))
        self.setMinimumWidth(700)
        self.setMinimumHeight(400)
        self.resize(800, 500)

        # Apply dark theme styling consistent with the rest of the app
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {get_color("bg_panel")};
                color: {get_color("text")};
            }}
            QLabel {{
                color: {get_color("text")};
                font-size: 12px;
            }}
            QTableWidget {{
                background-color: {get_color("bg_panel")};
                alternate-background-color: {get_color("bg_panel_alt")};
                border: 1px solid {get_color("border_subtle")};
                gridline-color: {get_color("border_subtle")};
                color: {get_color("text")};
                font-size: 12px;
            }}
            QTableWidget::item {{
                padding: 4px;
            }}
            QTableWidget::item:selected {{
                background-color: {get_color("accent")};
                color: #ffffff;
            }}
            QHeaderView::section {{
                background-color: {get_color("bg_toolbar")};
                color: {get_color("text_muted")};
                padding: 6px 8px;
                border: none;
                border-bottom: 1px solid {get_color("border_subtle")};
                font-size: 11px;
                font-weight: bold;
            }}
            QPushButton {{
                background-color: {get_color("bg_panel_alt")};
                border: 1px solid {get_color("border")};
                border-radius: 4px;
                padding: 6px 16px;
                color: {get_color("text")};
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {get_color("border")};
                border: 1px solid {get_color("text_muted")};
            }}
            QPushButton:pressed {{
                background-color: {get_color("bg_input")};
            }}
            QDialogButtonBox QPushButton {{
                min-width: 80px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # Info label
        info_label = QLabel(
            f"The following {len(self._assets)} asset(s) are offline. "
            "Select new file locations to relink them."
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        # Assets table
        self._table = QTableWidget()
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels([
            "Asset Name", "Original Path", "New Path", "Status"
        ])
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self._table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Fixed
        )
        self._table.setColumnWidth(0, 150)
        self._table.setColumnWidth(3, 80)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self._table)

        from src.shared.i18n.i18n import I18N

        # Buttons group
        btn_layout = QHBoxLayout()

        self.btn_relink_selected = QPushButton(I18N.tr("relink.relink_selected", "Relink Selected"))
        self.btn_relink_selected.setToolTip(
            I18N.tr("relink.tooltip_relink_selected", "Select a new file for the selected asset"))
        self.btn_relink_selected.clicked.connect(self._on_relink_selected)
        btn_layout.addWidget(self.btn_relink_selected)

        self.btn_relink_directory = QPushButton(I18N.tr("relink.relink_directory", "Relink by Directory"))
        self.btn_relink_directory.setToolTip(
            I18N.tr(
                "relink.tooltip_relink_directory",
                "Select a directory to search for all missing files")
        )
        self.btn_relink_directory.clicked.connect(self._on_relink_by_directory)
        btn_layout.addWidget(self.btn_relink_directory)

        self.btn_auto_search = QPushButton(I18N.tr("relink.auto_search_btn", "Auto Search"))
        self.btn_auto_search.setToolTip(
            I18N.tr(
                "relink.tooltip_auto_search",
                "Automatically search for files with matching names")
        )
        self.btn_auto_search.clicked.connect(self._on_auto_search)
        btn_layout.addWidget(self.btn_auto_search)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # Dialog buttons
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _populate_table(self) -> None:
        """Populate the table with offline assets."""
        from src.shared.i18n.i18n import I18N

        self._table.setRowCount(len(self._assets))

        for row, asset in enumerate(self._assets):
            # 获取素材属性（支持 MediaAsset、MediaAssetRef、Dict）
            asset_id = self._get_asset_attr(asset, "asset_id")
            display_name = self._get_asset_attr(asset, "display_name")
            asset_path = self._get_asset_attr(asset, "path")

            # Asset name
            name_item = QTableWidgetItem(display_name)
            name_item.setData(Qt.ItemDataRole.UserRole, asset_id)
            self._table.setItem(row, 0, name_item)

            # Original path
            path_item = QTableWidgetItem(asset_path)
            path_item.setForeground(QColor(get_color("danger")))
            self._table.setItem(row, 1, path_item)

            # New path (empty initially)
            new_path_item = QTableWidgetItem("")
            self._table.setItem(row, 2, new_path_item)

            # Status
            status_item = QTableWidgetItem(I18N.tr("relink.status_offline", "Offline"))
            status_item.setForeground(QColor(get_color("danger")))
            self._table.setItem(row, 3, status_item)

    def _get_asset_attr(self, asset: Any, attr: str) -> Any:
        """
        获取素材属性（支持多种素材类型）。

        Args:
            asset: 素材对象（MediaAsset、MediaAssetRef 或 Dict）
            attr: 属性名称

        Returns:
            属性值
        """
        if isinstance(asset, dict):
            return asset.get(attr, "")
        else:
            return getattr(asset, attr, "")

    def _get_asset_by_id(self, asset_id: str) -> Any | None:
        """
        通过 ID 获取素材对象。

        Args:
            asset_id: 素材 ID

        Returns:
            素材对象或 None
        """
        for asset in self._assets:
            if self._get_asset_attr(asset, "asset_id") == asset_id:
                return asset
        return None

    def _on_relink_selected(self) -> None:
        """Handle Relink Selected button click."""
        selected_rows = self._get_selected_rows()
        if not selected_rows:
            QMessageBox.information(
                self, "No Selection",
                "Please select an asset to relink."
            )
            return

        for row in selected_rows:
            asset_id = self._table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            asset = self._get_asset_by_id(asset_id)
            if not asset:
                continue

            # Open file dialog
            asset_path = self._get_asset_attr(asset, "path")
            display_name = self._get_asset_attr(asset, "display_name")
            original_dir = os.path.dirname(asset_path) if os.path.exists(os.path.dirname(asset_path)) else os.path.expanduser("~")

            new_path, _ = QFileDialog.getOpenFileName(
                self,
                f"Relink: {display_name}",
                original_dir,
                "Media files (*.mp4 *.mov *.avi *.mkv *.jpg *.jpeg *.png *.tif *.tiff *.exr *.wav *.mp3 *.aac *.flac);;All files (*)"
            )

            if new_path:
                self._update_relink_path(row, new_path)

    def _on_relink_by_directory(self) -> None:
        """Handle Relink by Directory button click."""
        # Select a directory to search
        search_dir = QFileDialog.getExistingDirectory(
            self,
            "Select Directory to Search",
            os.path.expanduser("~"),
        )

        if not search_dir:
            return

        matched_count = 0
        ambiguous_count = 0
        # AUD-20509: scan the directory once and reuse the index for every row
        # instead of calling _search_file_in_directory per asset.
        for row in range(self._table.rowCount()):
            asset_id = self._table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            asset = self._get_asset_by_id(asset_id)
            if not asset:
                continue

            # Check if already relinked
            new_path_item = self._table.item(row, 2)
            if new_path_item and new_path_item.text():
                continue

            # M12.3：可靠匹配器（名/大小/短哈希三级）替代 basename-only
            # ——auto（唯一候选名+大小+哈希全一致）自动填入；同名异内容
            # /多候选保留人工确认（不静默错连）
            asset_path = self._get_asset_attr(asset, "path")
            found_path, resolved_by = self._reliable_match(
                search_dir, asset_path, asset)
            if found_path:
                self._update_relink_path(row, found_path)
                matched_count += 1
                if resolved_by != "auto":
                    ambiguous_count += 1

        if matched_count > 0:
            suffix = (""
                      if not ambiguous_count
                      else f"（其中 {ambiguous_count} 个同名异内容/多候选"
                           "——已填入最高分候选，请人工核验）")
            QMessageBox.information(
                self, "Directory Search",
                f"Found {matched_count} file(s) in the selected "
                f"directory.{suffix}"
            )
        else:
            QMessageBox.information(
                self, "Directory Search",
                "No matching files found in the selected directory."
            )

    def _reliable_match(self, scan_dir: str, asset_path: str,
                        asset) -> "tuple[str | None, str]":
        """M12.3：经 F6.2 可靠匹配器解析一个缺失素材。

        Returns:
            (found_path, resolution)——resolution ∈
            "auto"（名+大小+哈希唯一一致）/ "manual"（同名异内容或多
            候选，填入最高分供人工核验）/ "none"。
        """
        try:
            from src.shared.project.relink_match import scan_candidates
        except ImportError:
            return None, "none"
        original_size = None
        try:
            original_size = os.path.getsize(asset_path) \
                if os.path.exists(asset_path) else None
        except OSError:
            original_size = None
        result = scan_candidates(
            scan_dir, os.path.basename(asset_path),
            original_size=original_size)
        if not result.candidates:
            return None, "none"
        if result.auto_relinkable:
            return result.candidates[0].path, "auto"
        # 同名异内容/多候选：填最高分候选但明示人工核验
        return result.candidates[0].path, "manual"

    def _on_auto_search(self) -> None:
        """Handle Auto Search button click - search in original directory and parent directories."""
        matched_count = 0
        ambiguous_count = 0
        # AUD-20509: 目录名→路径索引每目录只建一次——无同名文件的行经
        # 索引直接排除（不做逐行 listdir）；命中同名才进 M12.3b 可靠
        # 匹配验证（名/大小/短哈希）。
        directory_indexes: Dict[str, Dict[str, str]] = {}

        def _index_for(directory: str) -> Dict[str, str]:
            if directory not in directory_indexes:
                directory_indexes[directory] = self._build_directory_index(directory)
            return directory_indexes[directory]

        home = os.path.expanduser("~")
        for subdir in ["Desktop", "Documents", "Downloads", "Movies", "Pictures"]:
            _index_for(os.path.join(home, subdir))

        for row in range(self._table.rowCount()):
            asset_id = self._table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            asset = self._get_asset_by_id(asset_id)
            if not asset:
                continue

            # Check if already relinked
            new_path_item = self._table.item(row, 2)
            if new_path_item and new_path_item.text():
                continue

            # Try to find file in common locations
            asset_path = self._get_asset_attr(asset, "path")
            original_dir = os.path.dirname(asset_path)

            # Search directories to try
            search_dirs = []

            # Original directory (parent may still exist)
            parent_dir = os.path.dirname(original_dir)
            if os.path.isdir(parent_dir):
                search_dirs.append(parent_dir)

            # Home directory common locations
            for subdir in ["Desktop", "Documents", "Downloads", "Movies", "Pictures"]:
                d = os.path.join(home, subdir)
                if os.path.isdir(d):
                    search_dirs.append(d)

            # M12.3b：auto search 同款可靠匹配——缓存索引（含一级子目录）
            # 只做同名预过滤；命中文件在其**实际所在目录**跑名/大小/短哈希
            # 三级验证（auto 级直接填入；唯一同名但原身份不可验证——原
            # 文件已移走且无持久化 size/hash——填入最高分并计入"请人工
            # 核验"，与目录搜索按钮同语义。此前 basename 索引命中即静默
            # 自动填入，同名异内容会错连）。AUD-20509 逐行 listdir 防护
            # 保留（每目录索引一次）。
            for search_dir in search_dirs:
                index = _index_for(search_dir)
                hit_path = index.get(os.path.basename(asset_path))
                if not hit_path:
                    continue
                found_path, resolved_by = self._reliable_match(
                    os.path.dirname(hit_path) or search_dir,
                    asset_path, asset)
                if found_path:
                    self._update_relink_path(row, found_path)
                    matched_count += 1
                    if resolved_by != "auto":
                        ambiguous_count += 1
                    break

        if matched_count > 0:
            suffix = (""
                      if not ambiguous_count
                      else f"（其中 {ambiguous_count} 个同名但未验证身份"
                           "——请人工核验）")
            QMessageBox.information(
                self, "Auto Search",
                f"Automatically found {matched_count} file(s).{suffix}"
            )
        else:
            QMessageBox.information(
                self, "Auto Search",
                "Could not automatically locate any missing files."
            )

    def _build_directory_index(self, directory: str) -> Dict[str, str]:
        """
        Build a filename -> full path index for a directory in one pass.

        Covers direct entries plus one level of subdirectories, matching the
        lookup semantics of :meth:`_search_file_in_directory`. Building the
        index once and reusing it across many asset rows avoids repeated
        ``os.listdir`` scans on the UI thread (AUD-20509).

        Args:
            directory: Directory to index

        Returns:
            Mapping of filename to full path (first occurrence wins)
        """
        index: Dict[str, str] = {}
        if not os.path.isdir(directory):
            return index
        try:
            for entry in os.listdir(directory):
                full_path = os.path.join(directory, entry)
                if os.path.isdir(full_path):
                    try:
                        for sub_entry in os.listdir(full_path):
                            sub_path = os.path.join(full_path, sub_entry)
                            if os.path.isfile(sub_path):
                                index.setdefault(sub_entry, sub_path)
                    except OSError:
                        continue
                elif os.path.isfile(full_path):
                    index.setdefault(entry, full_path)
        except OSError:
            pass
        return index

    def _search_file_in_directory(self, directory: str, filename: str) -> str | None:
        """
        Search for a file by name in a directory (non-recursive, then one level deep).

        Args:
            directory: Directory to search in
            filename: Filename to search for

        Returns:
            Full path if found, None otherwise
        """
        # AUD-20509: delegate to the single-pass index so both entry points
        # share identical lookup semantics.
        return self._build_directory_index(directory).get(filename)

    def _update_relink_path(self, row: int, new_path: str) -> None:
        """Update the relink path for a table row."""
        from src.shared.i18n.i18n import I18N

        asset_id = self._table.item(row, 0).data(Qt.ItemDataRole.UserRole)

        # Update new path column
        new_path_item = self._table.item(row, 2)
        if new_path_item:
            new_path_item.setText(new_path)
            new_path_item.setForeground(QColor(get_color("success")))

        # Update status
        status_item = self._table.item(row, 3)
        if status_item:
            if os.path.exists(new_path):
                status_item.setText(I18N.tr("relink.status_found", "Found"))
                status_item.setForeground(QColor(get_color("success")))
            else:
                status_item.setText(I18N.tr("relink.status_missing", "Missing"))
                status_item.setForeground(QColor(get_color("danger")))

        # Update relink map
        self._relink_map[asset_id] = new_path

    def _get_selected_rows(self) -> list[int]:
        """Get list of selected row indices."""
        rows = set()
        for item in self._table.selectedItems():
            rows.add(item.row())
        return sorted(rows)

    def _on_accept(self) -> None:
        """Handle OK button click."""
        # Validate that all relinked paths exist
        invalid_paths = []
        for asset_id, new_path in self._relink_map.items():
            if not os.path.exists(new_path):
                invalid_paths.append(new_path)

        if invalid_paths:
            reply = QMessageBox.question(
                self,
                "Invalid Paths",
                f"{len(invalid_paths)} path(s) do not exist. Relink anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                return

        self.accept()

    def get_relink_map(self) -> Dict[str, str]:
        """
        Get the relink mapping.

        Returns:
            Dictionary mapping asset_id -> new_path
        """
        return dict(self._relink_map)
