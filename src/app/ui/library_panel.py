"""Library and Files share the native model, metadata and file-operation services."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QWidget, QInputDialog
from src.app.ui.files_panel import FilesPanel
from src.app.ui.native_files_panel import FilesEntryModel
from src.shared.config.config_manager import ConfigManager


class LibraryPanel(FilesPanel):
    image_selected = pyqtSignal(str)
    video_selected = pyqtSignal(str)
    grade_applied = pyqtSignal(str)
    quick_project_requested = pyqtSignal(str)

    def __init__(self, start_path: str | None = None, parent: QWidget | None = None) -> None:
        self.config = ConfigManager()
        super().__init__(parent, include_plugins=False)
        panel = self._native_panel
        self.list_view = panel.icon_view
        self.model = panel.model
        self.proxy_model = panel.proxy
        self.path_edit = panel.path_edit
        for view in (panel.icon_view, panel.detail_view):
            view.viewport().installEventFilter(self)
        self.directory_changed.connect(self._remember_directory)
        initial = start_path or self.config.get("last_library_path")
        if initial:
            self.set_root_path(str(initial))

    def _remember_directory(self, path: str) -> None:
        self.config.set("last_library_path", path)

    def _open_native_file(self, path: str) -> None:
        from src.shared.media.format_registry import VIDEO_EXTENSIONS
        if Path(path).suffix.lower() in VIDEO_EXTENSIONS:
            self.quick_project_requested.emit(path)
        else:
            self.image_selected.emit(path)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.MiddleButton:
            for view in (self._native_panel.icon_view, self._native_panel.detail_view):
                if watched is view.viewport():
                    index = view.indexAt(event.position().toPoint())
                    entry = index.data(FilesEntryModel.ENTRY_ROLE) if index.isValid() else None
                    if entry is not None and not entry.is_dir:
                        self.grade_applied.emit(entry.path)
                        return True
        return super().eventFilter(watched, event)

    def set_file_rating(self, path: str, rating: int) -> None:
        self._native_transport.patch_metadata([path], {"rating": int(rating)}, "replace")

    def rename_item(self, path: str) -> None:
        name, accepted = QInputDialog.getText(self, "重命名", "新名称：", text=Path(path).name)
        if accepted and name and name != Path(path).name:
            self._native_transport.run_task("rename", [path], options={"new_name": name})

    def delete_item(self, path: str) -> None:
        self._native_transport.run_task("trash", [path])

    def create_folder(self) -> None:
        name, accepted = QInputDialog.getText(self, "新建文件夹", "名称：")
        if accepted and name:
            self._native_transport.run_task("mkdir", [self._native_panel.current_path], options={"name": name})

    def set_view_mode(self, mode: str) -> None:
        combo = self._native_panel.view_combo
        index = combo.findData(mode)
        if index >= 0:
            combo.setCurrentIndex(index)
