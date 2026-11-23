"""Library and Files share the native model, metadata and file-operation services."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import QEvent, QObject, QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QInputDialog, QMenu, QWidget
from src.app.ui.files_panel import FilesPanel
from src.app.ui.library_grade_badges import LibraryGradeBadges
from src.app.ui.native_files_panel import FilesEntryModel
from src.features.files.contracts import Availability, Entry
from src.shared.config.config_manager import ConfigManager
from src.shared.i18n.i18n import I18N
from src.shared.media.format_registry import is_image

if TYPE_CHECKING:
    from src.app.controllers.grade_sets import GradeSetModel


def _tr(key: str, fallback: str, **kwargs: object) -> str:
    return I18N.tr(f"grade_sets.{key}", fallback, **kwargs)


class LibraryPanel(FilesPanel):
    image_selected = pyqtSignal(str)
    video_selected = pyqtSignal(str)
    grade_applied = pyqtSignal(str)
    quick_project_requested = pyqtSignal(str)
    photo_grade_requested = pyqtSignal(str)
    photo_export_requested = pyqtSignal(str)
    image_lightbox_active = pyqtSignal(bool)

    def __init__(self, start_path: str | None = None, parent: QWidget | None = None) -> None:
        self.config = ConfigManager()
        super().__init__(parent, include_plugins=False)
        self._grade_sets: GradeSetModel | None = None
        self._grade_set_view: tuple[int, str, int] | None = None
        self._grade_set_rows: tuple[tuple[int, str, int], ...] = ()
        panel = self._native_panel
        self.list_view = panel.icon_view
        self.model = panel.model
        self.proxy_model = panel.proxy
        self.path_edit = panel.path_edit
        self._image_grade_badges = LibraryGradeBadges(self._native_transport._runtime, self)
        panel.card_delegate.is_graded = self._image_grade_badges.is_graded
        panel.proxy.is_graded = self._image_grade_badges.has_grade
        panel.card_delegate.image_for_entry = self._image_for_card
        panel.image_lightbox_active.connect(self.image_lightbox_active)
        panel.preview_grade_requested.connect(self._grade_photo)
        self._image_grade_badges.changed.connect(self._grade_badges_changed)
        self.model.modelAboutToBeReset.connect(self._image_grade_badges.reset)
        for view in (panel.icon_view, panel.detail_view):
            view.viewport().installEventFilter(self)
        self.directory_changed.connect(self._remember_directory)
        initial = start_path or self.config.get("last_library_path")
        if initial:
            self.set_root_path(str(initial))

    def _grade_badges_changed(self) -> None:
        panel = self._native_panel
        if panel.proxy.graded_only:
            panel.refresh_file_filters()
        self.list_view.viewport().update()

    def invalidate_image_grade_badge(self, path: str) -> None:
        """Recheck the affected card after a standalone image project is saved."""
        self._image_grade_badges.invalidate(path)

    def _image_for_card(self, entry: Entry) -> QImage | None:
        image = self._image_grade_badges.preview(entry.path)
        return image if image is not None else self._native_panel._image_for_entry(entry)

    def shutdown(self) -> None:
        self._image_grade_badges.shutdown()
        super().shutdown()

    def _remember_directory(self, path: str) -> None:
        self.config.set("last_library_path", path)

    def _open_native_file(self, path: str) -> None:
        from src.shared.media.format_registry import VIDEO_EXTENSIONS
        if self._grade_set_view is not None and is_image(path):
            self._grade_photo(path)
        elif Path(path).suffix.lower() != ".topos" and self._native_panel.open_path_native(path):
            return
        elif Path(path).suffix.lower() in VIDEO_EXTENSIONS and not (
                self._grade_sets is not None and is_image(path)):
            self.quick_project_requested.emit(path)
        else:
            self.image_selected.emit(path)

    def _grade_photo(self, path: str) -> None:
        if self._grade_sets is None:
            return
        if self._grade_set_view is not None:
            self._grade_sets.select(self._grade_set_view[0])
        self.photo_grade_requested.emit(path)

    def close_lightbox(self) -> None:
        self._native_panel.close_preview()

    @property
    def browsing_grade_set(self) -> bool:
        return self._grade_set_view is not None

    def set_grade_sets(self, model: GradeSetModel) -> None:
        """Attach the shared image collection model to the Library sidebar."""
        if self._grade_sets is model:
            return
        self._grade_sets = model
        panel = self._native_panel
        panel.enable_grade_lightbox()
        panel.inspector_export_button.clicked.connect(self._request_photo_export)
        panel.selection.selectionChanged.connect(self._update_inspector_export)
        panel.model.modelReset.connect(self._update_inspector_export)
        panel.enable_grade_set_locations(lambda: self._create_grade_set())
        panel.locations.grade_set_requested.connect(self._open_grade_set)
        panel.locations.grade_set_images_dropped.connect(model.add)
        panel.locations.customContextMenuRequested.connect(self._grade_set_context_menu)
        model.changed.connect(self._sync_grade_sets)
        model.failed.connect(panel.meta_status.setText)
        panel.selection_actions_requested.connect(self._add_grade_set_actions)
        panel.directory_requested.connect(self._leave_grade_set_view)
        panel.library_search_requested.connect(self._leave_grade_set_view)
        panel.collection_requested.connect(self._track_grade_set_view)
        panel.collection_remove_members_requested.disconnect(self._native_transport.remove_collection_members)
        panel.collection_remove_members_requested.connect(self._remove_collection_members)
        self._native_transport.collection_loaded.connect(self._sync_grade_sets)
        self._native_transport.collection_failed.connect(self._leave_grade_set_view)
        self._sync_grade_sets()

    def selected_grade_set_photo(self) -> str | None:
        view = self._grade_set_view
        panel = self._native_panel
        location = panel._view_location
        if view is None or location.get("kind") != "collection" or location.get("collection_id") != view[0]:
            return None
        if getattr(panel, "_pending_collection_id", None) != view[0] or not panel._ready:
            return None
        entries = panel._selected_entries()
        if len(entries) != 1:
            return None
        entry = entries[0]
        if entry.is_dir or entry.availability == Availability.OFFLINE or not is_image(entry.path):
            return None
        return entry.path

    def _update_inspector_export(self, *_args: object) -> None:
        self._native_panel.inspector_export_button.setVisible(self.selected_grade_set_photo() is not None)

    def _request_photo_export(self) -> None:
        path = self.selected_grade_set_photo()
        if path is not None:
            self.photo_export_requested.emit(path)

    def _sync_grade_sets(self) -> None:
        model = self._grade_sets
        if model is None:
            return
        panel = self._native_panel
        by_id = {collection.id: collection for collection in model.collections}
        rows = tuple((item.id, item.name, item.count) for item in model.collections)
        if rows != self._grade_set_rows:
            self._grade_set_rows = rows
            panel.locations.set_grade_sets(rows)

        # Keep a visible collection current without navigating away from
        # a directory or search the user has since opened.
        if self._grade_set_view is None:
            return
        collection_id = self._grade_set_view[0]
        if panel._view_location.get("collection_id") != collection_id:
            return
        collection = by_id.get(collection_id)
        if collection is None:
            self._grade_set_view = None
            self._update_inspector_export()
            panel.navigate(panel.current_path or panel.home_path)
            return
        panel.locations.select_grade_set(collection_id)
        current = (collection.id, collection.name, collection.count)
        members_changed = (
            model.active_id == collection.id and len(model.paths) == collection.count
            and model.paths != tuple(entry.path for entry in panel.model.entries)
        )
        if current != self._grade_set_view or members_changed:
            self._grade_set_view = current
            panel._open_collection(collection.id, collection.name)

    def _open_grade_set(self, collection_id: int) -> None:
        model = self._grade_sets
        if model is None:
            return
        for collection in model.collections:
            if collection.id == collection_id:
                view = (collection.id, collection.name, collection.count)
                if self._grade_set_view != view:
                    self._native_transport.cancel()
                    self._grade_set_view = view
                    self._native_panel._open_collection(collection.id, collection.name)
                model.select(collection.id)
                return

    def _leave_grade_set_view(self, *_args: object) -> None:
        self._grade_set_view = None
        self._update_inspector_export()

    def _track_grade_set_view(self, collection_id: int, _name: str) -> None:
        if self._grade_set_view is not None and self._grade_set_view[0] != collection_id:
            self._grade_set_view = None
        self._update_inspector_export()

    def _remove_collection_members(self, collection_id: int, paths: list[str]) -> None:
        model = self._grade_sets
        if model is not None and any(collection.id == collection_id for collection in model.collections):
            model.remove(collection_id, paths)
        else:
            self._native_transport.remove_collection_members(collection_id, paths)

    def _create_grade_set(self, paths: tuple[str, ...] = ()) -> None:
        model = self._grade_sets
        if model is None:
            return
        name, accepted = QInputDialog.getText(
            self, _tr("new", "新建调色集…"), _tr("name_prompt", "调色集名称"))
        if accepted and name.strip():
            model.create(name.strip(), paths)

    def _add_grade_set_actions(self, menu: QMenu, paths: list[str]) -> None:
        model = self._grade_sets
        images = tuple(path for path in paths if is_image(path))
        if model is None or not images:
            return
        submenu = menu.addMenu(_tr("add_to", "添加到调色集"))
        submenu.addAction(_tr("new", "新建调色集…"), lambda: self._create_grade_set(images))
        if model.collections:
            submenu.addSeparator()
        for collection in model.collections:
            submenu.addAction(collection.name, lambda _checked=False, cid=collection.id: model.add(cid, images))

    def _grade_set_context_menu(self, position: QPoint) -> None:
        locations = self._native_panel.locations
        index = locations.indexAt(position)
        model = self._grade_sets
        if model is None:
            return
        if locations.is_grade_set_group(index):
            menu = QMenu(locations)
            menu.addAction(_tr("new", "新建调色集…"), lambda: self._create_grade_set())
            menu.exec(locations.viewport().mapToGlobal(position))
            menu.deleteLater()
            return
        collection_id = index.data(locations.GRADE_SET_ROLE)
        current = next((entry for entry in model.collections if entry.id == collection_id), None)
        if current is None or current.is_default:
            return
        menu = QMenu(locations)

        def rename() -> None:
            name, accepted = QInputDialog.getText(
                self, _tr("rename", "重命名调色集…"), _tr("name_prompt", "调色集名称"),
                text=current.name)
            if accepted and name.strip() and name.strip() != current.name:
                model.rename(current.id, name.strip())

        menu.addAction(_tr("rename", "重命名调色集…"), rename)
        menu.addAction(_tr("delete", "删除调色集"), lambda: model.delete(current.id))
        menu.exec(locations.viewport().mapToGlobal(position))
        menu.deleteLater()

    def set_middle_click_grade_enabled(self, enabled: bool) -> None:
        for view in (self._native_panel.icon_view, self._native_panel.detail_view):
            if enabled:
                view.viewport().installEventFilter(self)
            else:
                view.viewport().removeEventFilter(self)

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
        self._native_panel.rename_visible_file(path)

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
