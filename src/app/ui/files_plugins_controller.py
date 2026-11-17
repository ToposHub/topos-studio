"""Host coordinator for native plugin views, legacy catalog and authenticated I/O."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QPoint, QRunnable, QThreadPool, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence, QShortcut
from PyQt6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QTextEdit, QWidget

from src.app.integrations.file_explorer.plugins import FileExplorerPluginCatalog
from src.app.ui.native_files_plugins import PluginCenterDialog, PluginLauncherDialog, _tr
from src.app.ui.native_files_prefs import NativeFilesPreferences
from src.app.ui.native_files_transport import NativeFilesTransport
from src.features.files.plugin_contracts import FilesPluginInfo
from src.features.files.plugin_launch_service import LegacyFilesCommandService
from src.shared.plugins.discovery import (
    PluginSourceConfig, current_host_facts, scan_sources,
)
from src.shared.plugins.inventory import InventorySnapshot, PluginInventory


class _TaskSignals(QObject):
    finished = pyqtSignal(int, str, object, str)


class _CatalogTask(QRunnable):
    def __init__(self, generation: int, operation: str, work: Callable[[], object]) -> None:
        super().__init__()
        self.signals = _TaskSignals()
        self.generation, self.operation, self.work = generation, operation, work

    def run(self) -> None:
        try:
            result, error = self.work(), ""
        except (OSError, ValueError, RuntimeError) as exc:
            result, error = None, str(exc)
        self.signals.finished.emit(self.generation, self.operation, result, error)


class FilesPluginsController(QObject):
    def __init__(self, catalog: FileExplorerPluginCatalog, transport: NativeFilesTransport,
                 preferences: NativeFilesPreferences, panel: QWidget,
                 selected_paths: Callable[[], list[str]], legacy: Callable[[], None],
                 parent: QObject | None = None,
                 sources_factory: Callable[[], PluginSourceConfig] | None = None) -> None:
        super().__init__(parent)
        self.catalog, self.transport, self.preferences = catalog, transport, preferences
        self.panel, self.selected_paths, self.legacy = panel, selected_paths, legacy
        # [F1] 单一 Inventory：sources_factory 提供三源配置（builtin/安装根/
        # 开发源）时，refresh 经 scan_sources → PluginInventory → 合并投影；
        # 缺省（旧接线/测试）保持纯 legacy catalog 投影不变
        self._sources_factory = sources_factory
        self._inventory: PluginInventory | None = None
        if sources_factory is not None:
            facts = current_host_facts()
            self._inventory = PluginInventory(
                host_api=facts["host_api"], topos_version=facts["topos_version"],
                platform=facts["platform"], arch=facts["arch"])
        # [PL-07] 所有启动入口（启动器/中心/快捷键/菜单）走同一命令服务：
        # prepare 冻结选择，dispatch 提交；完成语义仍由 transport 回报。
        self._commands = LegacyFilesCommandService(catalog, transport)
        self.center = PluginCenterDialog(panel.window())
        self.launcher = PluginLauncherDialog(panel)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._generation = 0
        self._closed = False
        self._infos: tuple[Any, ...] = ()
        self._busy: set[str] = set()
        self._writing = False
        self._actions: list[QAction] = []
        self.center.refresh_requested.connect(self.refresh)
        self.center.enabled_requested.connect(self._set_enabled)
        self.center.pin_requested.connect(self._pin)
        self.center.shortcut_requested.connect(self._set_shortcut)
        self.center.directory_requested.connect(self._open_directory)
        self.center.legacy_requested.connect(self._open_legacy)
        self.center.launch_requested.connect(self.launch)
        self.launcher.launch_requested.connect(self.launch)
        self.launcher.center_requested.connect(self.show_center)
        self.launcher.pin_requested.connect(self._pin)
        self.launcher.details_requested.connect(self._show_details)
        self.transport.plugin_launch_finished.connect(self._launch_finished)
        self._launcher_action = QAction(_tr("launcher", "插件快捷启动器"), panel)
        self._launcher_action.setShortcut(QKeySequence("Ctrl+Alt+P"))
        self._launcher_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._launcher_action.triggered.connect(self.show_launcher)
        panel.addAction(self._launcher_action)
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._focus_changed)
        QTimer.singleShot(0, self.refresh)

    def _submit(self, operation: str, work: Callable[[], object]) -> None:
        if self._closed:
            return
        self._generation += 1
        task = _CatalogTask(self._generation, operation, work)
        task.signals.finished.connect(self._catalog_finished)
        self._pool.start(task)

    def refresh(self) -> None:
        if self._writing:
            return
        self._submit("refresh", self._snapshot_and_merge)

    def _snapshot_and_merge(self) -> object:
        """legacy 快照 ×（可选）三源聚合 → 增强投影；worker 线程执行。"""
        rows = self.catalog.snapshot()
        if self._sources_factory is None or self._inventory is None:
            return tuple(rows)
        # [F1] 单一聚合：三源扫描 → inventory 原子快照（重复身份打诊断、
        # 声明/运行版本分槽）→ 与 legacy 行合并成增强投影
        records = scan_sources(rows, self._sources_factory(),
                               host_facts=current_host_facts())
        return self._merge_infos(tuple(rows), self._inventory.refresh(records))

    def _merge_infos(self, rows: tuple[Any, ...],
                     snapshot: InventorySnapshot) -> tuple[Any, ...]:
        """legacy ``FilesPluginInfo`` × inventory 快照 → 增强投影。

        匹配键：inventory 记录的 legacy_ids 或 ``files.<短id>``。仅存在于
        inventory 的记录（受管安装/开发源）合成为不可启动的占位行——
        ``launch_unavailable_reason`` 如实说明尚未接入命令入口，不冒充
        可用。
        """
        merged: list[Any] = []
        matched_ids: set[str] = set()
        for info in rows:
            record = snapshot.by_legacy_id(info.plugin_id)
            if record is None:
                record = next((item for item in snapshot.records
                               if item.plugin_id == f"files.{info.plugin_id}"), None)
            if record is None:
                merged.append(info)
                continue
            matched_ids.add(record.plugin_id)
            typed_notes = tuple(f"[{item.code}/{item.severity}] {item.message}"
                                for item in record.diagnostics)
            merged.append(_dc_replace(
                info, source=record.source,
                install_state=record.install_state,
                runtime_version=record.runtime_version,
                diagnostics=tuple(info.diagnostics) + typed_notes))
        for record in snapshot.records:
            if record.plugin_id in matched_ids or record.manifest is None:
                continue
            manifest = record.manifest
            typed_notes = tuple(f"[{item.code}/{item.severity}] {item.message}"
                                for item in record.diagnostics)
            merged.append(FilesPluginInfo(
                plugin_id=record.plugin_id, name=record.display_name,
                version=record.version, author=manifest.author,
                description=manifest.description,
                api_version=manifest.host_api[0],
                category=manifest.category or "扩展",
                capabilities=tuple(item.capability_id
                                   for item in manifest.capabilities),
                runtime_loaded=False,
                desired_enabled=record.desired_enabled,
                self_disabled=False, needs_restart=record.pending_restart,
                error=record.error_message, launch_path="",
                launch_payload_kind="none",
                launch_unavailable_reason=record.error_message or "尚未接入本宿主命令入口",
                path=Path(record.origin_path or "."),
                diagnostics=typed_notes,
                source=record.source, install_state=record.install_state,
                runtime_version=record.runtime_version))
        return tuple(merged)

    def _catalog_finished(self, generation: int, operation: str, result: object, error: str) -> None:
        if self._closed:
            return
        if operation.startswith("launch:"):
            # worker 已完成 prepare+revalidate；此处只做 Qt 线程安全的提交
            plugin_id = operation.partition(":")[2]
            if error:
                self._busy.discard(plugin_id)
                self._project()
                self._message(error)
            else:
                outcome = self._commands.dispatch(result)
                if not outcome.accepted:
                    self._busy.discard(plugin_id)
                    self._project()
                    self._message(outcome.message)
            return
        if operation == "enable":
            self._writing = False
            self.center.enabled.setEnabled(True)
        if generation != self._generation:
            return
        if error:
            self._project()
            self._message(error)
            return
        self._infos = tuple(result)
        self._project()
        self._rebuild_shortcuts()
        if operation == "enable":
            self._message(_tr("saved_restart", "设置已保存，重启后生效；当前会话的插件仍保持原来的加载状态。"))

    def _project(self) -> None:
        for view in (self.center, self.launcher):
            view.set_snapshot(self._infos, self.preferences.plugin_pins,
                              self.preferences.plugin_recent, self.preferences.plugin_shortcuts, self._busy)
        self.center.set_context(len(self.selected_paths()))
        self.launcher.context.setText(_tr("launcher_context", "已选 {count} 个文件 · 固定优先，最近使用其次").format(count=len(self.selected_paths())))
        if self._writing:
            self.center.enabled.setEnabled(False)

    def show_center(self) -> None:
        if self._closed:
            return
        self.launcher.hide()
        self._project()
        self.center.show()
        self.center.raise_()
        self.center.activateWindow()
        self.refresh()

    def show_launcher(self) -> None:
        if self._closed:
            return
        self.launcher.search.clear()
        self._project()
        self.launcher.move(self.panel.mapToGlobal(QPoint(max(0, self.panel.width() // 2 - 230), 48)))
        self.launcher.show()
        self.launcher.search.setFocus()
        self.refresh()

    def _message(self, message: str) -> None:
        self.center.notice.setText(message)
        self.launcher.context.setText(message)
        # Keep failures visible after the popup closes or a shortcut is used.
        if hasattr(self.panel, "meta_status"):
            self.panel.meta_status.setText(message)

    def _show_details(self, plugin_id: str) -> None:
        self.show_center()
        self.center.select_plugin(plugin_id)

    def _pin(self, plugin_id: str) -> None:
        pins = self.preferences.plugin_pins
        self.preferences.plugin_pins = [pid for pid in pins if pid != plugin_id] if plugin_id in pins else [*pins, plugin_id]
        self._project()

    def _set_enabled(self, plugin_id: str, enabled: bool) -> None:
        if self._writing:
            return
        self._writing = True
        self.center.enabled.setEnabled(False)
        def work() -> object:
            self.catalog.set_enabled(plugin_id, enabled)
            return self._snapshot_and_merge()   # [F1] 启停后同口径聚合
        self._submit("enable", work)

    def launch(self, plugin_id: str) -> None:
        if self._closed or plugin_id in self._busy or self._writing:
            return
        # Freeze the selection at the user's gesture, never at async completion.
        paths = list(self.selected_paths())
        self._busy.add(plugin_id)
        self._project()

        def work() -> object:
            prepared = self._commands.prepare(plugin_id, paths)
            # [PR-05] 手势冻结后在 worker 内做执行前权威复核（停用/接口
            # 变化在此拒绝）；dispatch 只剩 Qt 线程安全的提交，GUI 线程
            # 不补同步扫描。
            return self._commands.revalidate(prepared)

        self._submit(f"launch:{plugin_id}", work)

    def _launch_finished(self, plugin_id: str, success: bool, message: str) -> None:
        if self._closed:
            return
        self._busy.discard(plugin_id)
        if success:
            self.preferences.plugin_recent = [plugin_id, *[pid for pid in self.preferences.plugin_recent if pid != plugin_id]]
            self.launcher.hide()
        self._project()
        self._message(message)

    def _set_shortcut(self, plugin_id: str, text: str) -> None:
        sequence = QKeySequence(text, QKeySequence.SequenceFormat.PortableText)
        canonical = sequence.toString(QKeySequence.SequenceFormat.PortableText)
        if text and (sequence.isEmpty() or sequence.count() != 1 or not sequence[0].keyboardModifiers() & (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)):
            self._message(_tr("shortcut_modifier", "请使用包含 Ctrl、Alt 或 Command 的单组快捷键。"))
            return
        assigned = self.preferences.plugin_shortcuts
        conflicts = any(pid != plugin_id and QKeySequence(key) == sequence for pid, key in assigned.items()) if text else False
        reserved = ("Ctrl+C", "Ctrl+V", "Ctrl+X", "Ctrl+A", "Ctrl+F", "Ctrl+L", "Ctrl+R", "Ctrl+O", "Ctrl+N", "Ctrl+W", "Ctrl+Q", "Ctrl+Alt+P", "Meta+C", "Meta+V", "Meta+X", "Meta+A", "Meta+Q", "Meta+W")
        conflicts |= bool(text and any(sequence == QKeySequence(key) for key in reserved))
        for action in self.panel.window().findChildren(QAction):
            if action not in self._actions:
                conflicts |= bool(text and any(sequence == key for key in action.shortcuts()))
        for shortcut in self.panel.window().findChildren(QShortcut):
            conflicts |= bool(text and sequence == shortcut.key())
        if conflicts:
            self._message(_tr("shortcut_conflict", "此快捷键已被占用，请选择其他组合。"))
            return
        if canonical:
            assigned[plugin_id] = canonical
        else:
            assigned.pop(plugin_id, None)
        self.preferences.plugin_shortcuts = assigned
        self._project()
        self._rebuild_shortcuts()
        self._message(_tr("shortcut_saved", "快捷键已保存，仅在 Files 面板内生效。"))

    def _rebuild_shortcuts(self) -> None:
        for action in self._actions:
            self.panel.removeAction(action)
            action.deleteLater()
        self._actions.clear()
        assigned = self.preferences.plugin_shortcuts
        for info in self._infos:
            if not info.can_launch or info.plugin_id not in assigned:
                continue
            action = QAction(info.name, self.panel)
            action.setShortcut(QKeySequence(assigned[info.plugin_id]))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(lambda checked=False, pid=info.plugin_id: self.launch(pid))
            self.panel.addAction(action)
            self._actions.append(action)
        self._focus_changed(None, QApplication.focusWidget())

    def _focus_changed(self, old: QWidget | None, current: QWidget | None) -> None:
        editing = isinstance(current, (QLineEdit, QPlainTextEdit, QTextEdit))
        for action in self._actions:
            action.setEnabled(not editing and not self._closed)

    def _open_directory(self) -> None:
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        plugins_dir = self.catalog.plugins_dir
        if plugins_dir is None:
            self._message(_tr("directory_failed", "无法定位插件目录。"))
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(plugins_dir))):
            self._message(_tr("directory_failed", "无法打开插件目录。"))

    def _open_legacy(self) -> None:
        self.center.hide()
        self.launcher.hide()
        self.legacy()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pool.clear()
        self.center.close()
        self.launcher.close()
        self.center.deleteLater()
        self.launcher.deleteLater()
        self._launcher_action.setEnabled(False)
        for action in self._actions:
            action.setEnabled(False)
