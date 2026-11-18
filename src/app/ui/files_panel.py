"""Native Files host: direct domain services, with no browser or HTTP runtime."""
from __future__ import annotations

import logging
import os

from PyQt6.QtCore import QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QStackedWidget, QFileDialog, QMenu, QDockWidget
from src.shared.i18n.i18n import I18N

logger = logging.getLogger(__name__)


class FilesPanel(QWidget):
    directory_changed = pyqtSignal(str)
    image_opened = pyqtSignal(str)
    video_opened = pyqtSignal(str)
    rating_changed = pyqtSignal(str, int)

    def __init__(self, parent: QWidget | None = None, *, include_plugins: bool = True) -> None:
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self._main_window = parent
        self._files_root_path: str | None = None
        self._native_panel = None
        self._native_transport = None
        self._files_jobs = None
        self._jobs_dialog = None
        self._maintenance_dialog = None
        self._native_active = True
        self._include_plugins = include_plugins
        self._closed = False
        from src.app.ui.native_files_prefs import NativeFilesPreferences
        self._files_prefs = NativeFilesPreferences()
        self.content_stack = QStackedWidget(self)
        self.layout.addWidget(self.content_stack)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self._initialize_native()

    def get_main_window(self) -> QWidget | None:
        return self._main_window

    def _show_context_menu(self, pos: QPoint) -> None:
        menu = QMenu(self)
        dock = self.parent()
        while dock and not isinstance(dock, QDockWidget):
            dock = dock.parent()
        if dock:
            text = I18N.tr("dock.dock", "Dock") if dock.isFloating() else I18N.tr("dock.float", "Float")
            menu.addAction(text).triggered.connect(lambda: dock.setFloating(not dock.isFloating()))
        if not menu.isEmpty():
            menu.exec(self.mapToGlobal(pos))

    def refresh_layout(self) -> None:
        self.layout.activate()
        self.updateGeometry()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        plugins = getattr(self, "_files_plugins", None)
        if plugins is not None:
            plugins.shutdown()
        if self._jobs_dialog is not None:
            self._jobs_dialog.close()
        if self._maintenance_dialog is not None:
            self._maintenance_dialog.close()
        if self._native_panel is not None:
            self._native_panel.shutdown()
        if self._native_transport is not None:
            self._native_transport.shutdown()

    def wait_for_shutdown(self, timeout: float) -> bool:
        transport = self._native_transport
        return transport is None or transport.wait_for_shutdown(timeout)

    def set_root_path(self, path: str) -> None:
        if not path or self._closed:
            return
        if (self._native_panel is not None
                and self._native_panel._requested_path != str(path)):
            self._native_panel.navigate(str(path))

    def _initialize_native(self) -> None:
        if self._native_panel is None:
            from src.app.ui.native_files_panel import NativeFilesPanel
            from src.app.ui.native_files_transport import NativeFilesTransport

            self._native_panel = NativeFilesPanel(self, preferences=self._files_prefs)
            self._native_transport = NativeFilesTransport(
                lambda: '', None, self)
            self._native_panel.directory_requested.connect(
                lambda path: self._native_transport.load_directory(path))
            self._native_panel.directory_more_requested.connect(
                lambda path, offset: self._native_transport.load_directory(
                    path, offset=offset))
            self._native_transport.page_meta.connect(
                self._native_panel.set_page_meta)
            self._native_panel.thumbnail_requested.connect(self._native_transport.request_thumbnail)
            self._native_panel.thumbnail_released.connect(self._native_transport.release_thumbnail)
            self._native_transport.thumbnail_loaded.connect(self._native_panel.set_thumbnail)
            self._native_transport.thumbnail_failed.connect(self._native_panel.thumbnail_failed)
            self._native_transport.thumbnail_pending.connect(self._native_panel.thumbnail_pending)
            self._native_panel.tree_requested.connect(self._native_transport.load_tree)
            # U03：预览 —— 每次请求都取消上一次未完成的取数，避免错过图串台
            self._native_panel.preview_requested.connect(self._native_transport.load_preview)
            self._native_transport.preview_loaded.connect(self._native_panel.show_preview_bytes)
            self._native_transport.preview_failed.connect(self._native_panel.preview_load_failed)
            self._native_panel.preview_closed.connect(self._native_transport.cancel_preview)
            self._native_panel.preview_external_requested.connect(self._open_native_file)
            # U05：元数据写入 —— 只有服务确认后才更新模型
            self._native_panel.metadata_patch_requested.connect(
                self._native_transport.patch_metadata)
            self._native_transport.metadata_applied.connect(
                self._native_panel.apply_metadata_result)
            self._native_transport.metadata_applied.connect(self._on_metadata_applied)
            self._native_transport.metadata_failed.connect(
                self._native_panel.metadata_failed)
            # [FR-11] 服务端最终 user 快照：UI 回显一律以落库状态为准
            self._native_transport.metadata_snapshot.connect(
                self._native_panel.apply_metadata_snapshot)
            # B01：能力清单 —— 服务不支持的能力在 UI 上直接禁用并给原因
            self._native_transport.capabilities_loaded.connect(
                self._native_panel.apply_capabilities)
            # B07 / F10：文件任务 —— 提交后轮询，逐项结果回到面板，不做假成功
            self._native_panel.file_task_requested.connect(self._on_native_file_task)
            self._native_transport.task_started.connect(self._native_panel.apply_task_progress)
            self._native_transport.task_finished.connect(self._native_panel.apply_task_result)
            self._native_transport.task_failed.connect(self._native_panel.task_failed)
            # B05：全库搜索 —— 只读索引，结果显式带 total/truncated
            self._native_panel.library_search_requested.connect(
                self._native_transport.search_library)
            self._native_transport.search_loaded.connect(self._native_panel.show_library_results)
            self._native_transport.search_failed.connect(
                self._native_panel.library_search_failed)
            # B11：集合浏览 —— 成员语义沿用后端集合，不在 UI 复制规则
            self._native_panel.collections_requested.connect(
                self._native_transport.load_collections)
            self._native_transport.collections_loaded.connect(
                self._native_panel.show_collections)
            self._native_panel.collection_requested.connect(
                lambda cid, _name: self._native_transport.load_collection(cid))
            self._native_transport.collection_loaded.connect(
                self._native_panel.show_collection_results)
            self._native_transport.collection_failed.connect(
                self._native_panel.collection_failed)
            # D2：集合写入与回收站入口（写入全部经 B/C 服务）
            self._native_panel.collection_create_requested.connect(
                self._native_transport.create_collection)
            self._native_panel.collection_rename_requested.connect(
                self._native_transport.rename_collection)
            self._native_panel.collection_add_members_requested.connect(
                self._native_transport.add_collection_members)
            self._native_panel.collection_remove_members_requested.connect(
                self._native_transport.remove_collection_members)
            self._native_panel.collection_delete_requested.connect(
                self._native_transport.delete_collection)
            self._native_transport.collection_changed.connect(
                self._on_collection_changed)
            self._native_panel.trash_requested.connect(self._open_trash)
            # D5：文件任务台账——切目录/关窗不丢归属（registry 常驻本面板）
            from src.app.ui.native_files_jobs import FilesJobsRegistry
            self._files_jobs = FilesJobsRegistry(self)
            self._native_transport.task_started.connect(self._files_jobs.on_task_started)
            self._native_transport.task_finished.connect(self._files_jobs.on_task_finished)
            self._native_transport.task_failed.connect(self._files_jobs.on_task_failed)
            self._native_transport.recovery_loaded.connect(
                self._files_jobs.on_recovery_loaded)
            self._native_transport.recovery_applied.connect(
                self._on_recovery_applied)
            self._native_panel.jobs_requested.connect(self._open_jobs)
            self._native_panel.maintenance_requested.connect(self._open_maintenance)
            # B3：上次运行遗留的未终态任务进入台账（待恢复可操作）
            self._native_transport.load_recoveries()
            # F11 / B10：常用位置、索引状态、缩略图重试
            self._native_panel.libraries_requested.connect(
                self._native_transport.load_libraries)
            self._native_transport.libraries_loaded.connect(
                self._native_panel.show_libraries)
            self._native_panel.library_pin_requested.connect(
                self._native_transport.pin_library)
            self._native_panel.library_unpin_requested.connect(
                self._native_transport.unpin_library)
            self._native_transport.library_changed.connect(
                self._native_panel.show_library_change)
            self._native_panel.scan_requested.connect(self._native_transport.start_scan)
            self._native_panel.scan_cancel_requested.connect(
                self._native_transport.cancel_scan)
            self._native_transport.scan_status_loaded.connect(
                self._native_panel.show_scan_status)
            self._native_transport.scan_failed.connect(self._native_panel.scan_failed)
            self._native_panel.thumbnail_retry_requested.connect(
                self._native_transport.retry_thumbnail)
            self._native_transport.thumbnail_retry_done.connect(
                self._on_thumbnail_retry_done)
            self._native_panel.strip_requested.connect(
                self._native_transport.request_strip)
            self._native_transport.strip_loaded.connect(self._native_panel.set_strip)
            self._native_panel.batch_rename_requested.connect(
                self._open_batch_rename)
            self._native_panel.compare_requested.connect(self._open_compare)
            self._native_transport.tree_loaded.connect(self._native_panel.locations.set_children)
            self._native_transport.tree_failed.connect(self._native_panel.locations.set_error)
            self._native_panel.file_activated.connect(self._open_native_file)
            self._native_panel.legacy_requested.connect(self._show_native_tools)
            if self._include_plugins:
                self._connect_native_plugins()
            else:
                self._native_panel.plugins_button.hide()
            self._native_transport.loading.connect(self._native_panel.set_loading)
            self._native_transport.failed.connect(self._native_panel.show_error)
            self._native_transport.loaded.connect(self._on_native_directory_loaded)
            self.content_stack.addWidget(self._native_panel)
        self.content_stack.setCurrentWidget(self._native_panel)
        self._native_transport.load_capabilities()
        self._native_transport.load_libraries()
        self._native_transport.load_scan_status()
        self._native_panel.request_libraries()
        self._native_panel.start_home_tree()
        path = self._files_root_path
        if not path or "://" in path or not self._native_panel.is_browsable_path(path):
            path = os.path.expanduser("~")
        self._native_panel.navigate(path)


    def _connect_native_plugins(self) -> None:
        from src.features.files.builtin_plugins import NativePluginCatalog
        from src.app.ui.files_plugins_controller import FilesPluginsController
        from src.features.files.repository import FilesDataLocator
        from src.features.files.plugin_broker_host import PluginConfigStore
        from src.shared.plugins.discovery import (
            DesiredStateStore, DevSourceRegistry, PluginSourceConfig,
        )
        locator = FilesDataLocator.default()
        desired_store = DesiredStateStore(locator.root / "plugin_desired.json")
        catalog = NativePluginCatalog(desired_store=desired_store)
        # Builtins, installed packages and explicit development sources share one inventory.
        dev_registry = DevSourceRegistry(locator.root / "plugin_dev_sources.json")

        def sources() -> PluginSourceConfig:
            return PluginSourceConfig.for_host(
                builtin_ids=tuple(row.plugin_id for row in catalog.snapshot()),
                install_root=locator.plugins_install_root,
                dev_roots=dev_registry.list())

        self._files_plugins = FilesPluginsController(
            catalog, self._native_transport, self._files_prefs, self._native_panel,
            lambda: [entry.path for entry in self._native_panel._selected_entries() if not entry.is_dir],
            self._show_native_tools, self,
            sources_factory=sources,
            desired_store=desired_store)
        self._native_panel.plugins_requested.connect(self._files_plugins.show_launcher)
        self._native_panel.plugin_center_requested.connect(self._files_plugins.show_center)
        # [F11] 安装/更新/卸载编排：停用→安装事务→Inventory 重扫；
        # 数据（配置命名空间）/凭据（secret 撤权）分开，素材不触及
        from src.shared.plugins.installer import LocalPackageInstaller
        from src.shared.plugins.package_lifecycle import PackageLifecycle
        from src.shared.plugins.discovery import current_host_facts
        config_store = PluginConfigStore(locator.root / "plugin_config.json")
        self._files_plugins.host_session.config_store = config_store
        self._files_plugins.package_lifecycle = PackageLifecycle(
            LocalPackageInstaller(locator.plugins_install_root,
                                  host_facts=current_host_facts()),
            self._files_plugins.host_session,
            desired_store=desired_store,
            delete_data=config_store.clear_namespace,
            revoke_credentials=self._files_plugins.host_session.secrets.revoke_all,
            refresh_inventory=self._files_plugins.refresh)

    def _on_collection_changed(self, ok: bool, message: str) -> None:
        """集合写反馈：面板提示；成功后刷新集合清单与当前视图。"""
        panel = self._native_panel
        if panel is None:
            return
        panel.show_collection_change(ok, message)
        if ok:
            panel.refresh()

    def _open_trash(self) -> None:
        """D2 回收站：非模态清单；恢复/彻底删除走文件任务并回刷。"""
        if self._native_transport is None:
            return
        from src.app.ui.native_files_trash import TrashDialog
        dialog = TrashDialog(self)
        transport = self._native_transport
        dialog.refresh_requested.connect(transport.load_trash)
        dialog.restore_requested.connect(
            lambda record_id, policy: transport.run_task(
                "restore", [str(record_id)], policy=policy))
        dialog.purge_requested.connect(
            lambda record_id: transport.run_task("purge", [str(record_id)]))
        transport.trash_loaded.connect(dialog.show_items)
        transport.trash_failed.connect(dialog.show_error)
        transport.task_finished.connect(dialog.show_task_result)
        dialog.finished.connect(lambda *_: self._native_panel and self._native_panel.refresh())
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.show()
        transport.load_trash()

    def _on_recovery_applied(self, verdict: dict) -> None:
        registry = getattr(self, "_files_jobs", None)
        if registry is not None:
            registry.on_recovery_applied(verdict)
        if self._native_panel is not None and verdict.get("state") == "completed":
            self._native_panel.refresh()

    def _open_maintenance(self) -> None:
        if self._maintenance_dialog is not None:
            self._maintenance_dialog.show()
            self._maintenance_dialog.raise_()
            self._maintenance_dialog.activateWindow()
            return
        facade = self._native_transport._runtime()
        if facade is None:
            self._native_panel.meta_status.setText("素材库尚未就绪，无法打开维护工具。")
            return
        from src.app.ui.native_files_maintenance import FilesMaintenanceDialog
        dialog = FilesMaintenanceDialog(facade, parent=self)
        self._maintenance_dialog = dialog
        dialog.finished.connect(lambda *_: setattr(self, "_maintenance_dialog", None))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.show()

    def _open_jobs(self) -> None:
        """D5 任务列表：注册表常驻，对话框可关/重开不丢账。"""
        if self._native_transport is None:
            return
        existing = getattr(self, "_jobs_dialog", None)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return
        from src.app.ui.native_files_jobs import JobsDialog
        registry = getattr(self, "_files_jobs", None)
        if registry is None:
            return
        dialog = JobsDialog(registry, self)
        dialog.cancel_requested.connect(self._native_transport.cancel_task)
        dialog.recover_requested.connect(self._native_transport.recover_job)
        dialog.retry_requested.connect(
            lambda operation, targets, policy, options:
            self._native_transport.run_task(
                str(operation), list(targets), policy=str(policy),
                options=dict(options or {})))
        dialog.finished.connect(lambda *_: setattr(self, "_jobs_dialog", None))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._jobs_dialog = dialog
        dialog.show()

    def _open_batch_rename(self, paths: list[str]) -> None:
        from src.app.ui.native_files_batch_rename import BatchRenameDialog
        if len(paths) < 2:
            return
        dialog = BatchRenameDialog(paths, self)
        dialog.preview_requested.connect(
            lambda template, session: self._native_transport.preview_batch_rename(
                paths, "template", {"template": template}, session_id=session))
        dialog.execute_requested.connect(
            lambda template, session: self._native_transport.execute_batch_rename(
                paths, "template", {"template": template}, session_id=session))
        self._native_transport.batch_rename_preview_ready.connect(dialog.show_preview)
        self._native_transport.batch_rename_preview_failed.connect(dialog.show_preview_error)
        self._native_transport.batch_rename_finished.connect(dialog.show_result)
        dialog.finished.connect(lambda *_: self._native_panel.refresh())
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.show()

    def _open_compare(self, paths: list[str]) -> None:
        from src.app.ui.native_files_compare import CompareDialog
        if not 2 <= len(paths) <= 4:
            return
        dialog = CompareDialog(paths, self)
        dialog.images_requested.connect(
            lambda p, session: self._native_transport.load_compare_images(
                p, session_id=session))
        self._native_transport.compare_image_loaded.connect(dialog.set_image)
        self._native_transport.compare_image_failed.connect(dialog.set_error)
        # [FR-15/17] 关闭即取消本会话在途请求与剩余队列
        dialog.finished.connect(
            lambda *_: self._native_transport.cancel_compare(dialog.session_id))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.show()

    def _on_thumbnail_retry_done(self, path: str, ok: bool) -> None:
        """F11 重试：服务确认后才重新请求缩略图，失败不改状态。"""
        if not ok or self._native_panel is None or self._native_transport is None:
            return
        self._native_panel._thumbnail_failed.discard(path)
        self._native_panel.icon_view.viewport().update()

    def _on_native_file_task(self, operation: str, targets: list, options: dict) -> None:
        """面板只表达意图；任务编号、进度与逐项结果由服务端决定。"""
        transport = self._native_transport
        if transport is None or not targets:
            return
        options = dict(options or {})
        transport.run_task(
            operation, list(targets),
            policy=str(options.pop("policy", "error")),
            options=dict(options.get("options") or {}),
        )

    def _on_native_directory_loaded(self, path: str, entries: list) -> None:
        self._native_panel.show_directory_page(path, entries)
        changed = self._files_root_path != path
        self._files_root_path = path
        if changed:
            self.directory_changed.emit(path)

    def _on_metadata_applied(self, paths: list, patch: dict, message: str = "") -> None:
        if "rating" in patch:
            for path in paths:
                self.rating_changed.emit(str(path), int(patch["rating"]))

    def _open_native_file(self, path: str) -> None:
        """Open supported media in-process; unsupported formats stay explicit."""
        from pathlib import Path
        from src.shared.media.format_registry import AUDIO_EXTENSIONS
        plugins = getattr(self, "_files_plugins", None)
        if Path(path).suffix.lower() in AUDIO_EXTENSIONS and plugins is not None:
            try:
                plugins.open_audio(path)
            except (ValueError, OSError, RuntimeError) as exc:
                self._native_panel.meta_status.setText(str(exc))
            return
        if self._native_panel.open_path_native(path):
            return
        self._native_panel.meta_status.setText(
            I18N.tr("files.native.unsupported", "此格式暂无原生预览，请使用插件工具打开。"))
        self._show_native_tools()

    def _show_native_tools(self) -> None:
        plugins = getattr(self, "_files_plugins", None)
        if plugins is not None:
            plugins.show_center()
        else:
            self._native_panel.meta_status.setText("素材工具请从 Files 插件中心打开。")

    def refresh_file_rating(self, path: str, rating: int) -> None:
        """Refresh the service snapshot; never write metadata during sync."""
        if self._native_panel is not None:
            self._native_panel.refresh()

    def _handle_add_library(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择素材库目录", os.path.expanduser("~"))
        if folder:
            self._native_transport.pin_library(folder)

    def _handle_open_folder_dialog(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择目录", os.path.expanduser("~"))
        if folder:
            self.set_root_path(folder)

    def _handle_play_video(self, path: str) -> None:
        self._open_native_file(path)
