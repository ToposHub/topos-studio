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
    DesiredStateStore, PluginSourceConfig, current_host_facts, scan_sources,
)
from src.shared.plugins.host_session import PluginHostSession
from src.shared.plugins.inventory import InventorySnapshot, PluginInventory


# [F10] 当前插件启动管线（经旧服务 HTTP）的**实际**隔离级别——平台有
# sandbox-exec 不等于本管线用了它；G 阶段受限执行接入后升级此常量。
LAUNCH_PIPELINE_ISOLATION = "none"


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
                 sources_factory: Callable[[], PluginSourceConfig] | None = None,
                 desired_store: DesiredStateStore | None = None) -> None:
        super().__init__(parent)
        self.catalog, self.transport, self.preferences = catalog, transport, preferences
        self.panel, self.selected_paths, self.legacy = panel, selected_paths, legacy
        # [F1] 单一 Inventory：sources_factory 提供三源配置（builtin/安装根/
        # 开发源）时，refresh 经 scan_sources → PluginInventory → 合并投影；
        # 缺省（旧接线/测试）保持纯 legacy catalog 投影不变。
        # [F2] 新 id 模型：合并行携带命名空间 plugin_id（files.<短id>），
        # 固定/最近/快捷键/启停偏好全部按新 id 建档（不做旧偏好迁移）；
        # 新模型插件的启停偏好持久化在 desired_store
        self._sources_factory = sources_factory
        self._desired_store = desired_store
        self._inventory: PluginInventory | None = None
        if sources_factory is not None:
            facts = current_host_facts()
            self._inventory = PluginInventory(
                host_api=facts["host_api"], topos_version=facts["topos_version"],
                platform=facts["platform"], arch=facts["arch"])
        # [PL-07] 所有启动入口（启动器/中心/快捷键/菜单）走同一命令服务：
        # prepare 冻结选择，dispatch 提交；完成语义仍由 transport 回报。
        # [F3] 库 revision 复核：手势时冻结、复核/提交时比对（重扫/切库
        # 后的过期调用拒绝重发）；transport 未就绪时如实跳过（不冒装）
        revision_provider = getattr(transport, "library_revision", None)
        self._commands = LegacyFilesCommandService(
            catalog, transport,
            revision_provider=revision_provider)
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
        # [F5] 宿主会话：快捷键贡献所有权（注销/失败重试）+ 窗口注册表
        # （owner/singleton——供 G 阶段原生窗口宿主消费）
        self.host_session = PluginHostSession()
        self._shortcut_plugins: set[str] = set()
        self._launch_jobs: dict[str, str] = {}          # [F6] ui_id -> job_id
        self._retire_polls: dict[str, int] = {}          # [F6] 异步退役轮次
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
        # 声明/运行版本分槽）→ 与 legacy 行合并成增强投影。
        # [F2] 新模型插件的启停偏好从持久化 store 读入发现侧
        desired = self._desired_store.load() if self._desired_store else None
        records = scan_sources(rows, self._sources_factory(),
                               host_facts=current_host_facts(),
                               desired_by_id=desired)
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
            # [F2] 合并行携带命名空间 plugin_id：固定/最近/快捷键/启停偏好
            # 全部按新 id 建档（旧短 id 偏好自然失效，不做迁移）；
            # 命令键保留 legacy 短 id（同快照内解析，不持久化别名）
            merged.append(_dc_replace(
                info, plugin_id=record.plugin_id,
                command_key=info.plugin_id,
                source=record.source,
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
        info = next((item for item in self._infos if item.plugin_id == plugin_id), None)
        command_key = info.command_key if info is not None and info.command_key else ""
        if not command_key and self._inventory is None:
            command_key = plugin_id   # 旧接线：id 即命令键
        if command_key:
            # legacy 目录插件：启停偏好走旧 catalog 配置（命令键寻址）
            self._writing = True
            self.center.enabled.setEnabled(False)

            def work() -> object:
                # [F4] 启停经权威边界：与在途提交原子串行
                self._commands.set_enabled_atomic(command_key, enabled)
                return self._snapshot_and_merge()   # [F1] 启停后同口径聚合

            if not enabled:
                # [F5] 停用即释放快捷键/窗口贡献（restart-bound：不走
                # lifecycle 不可逆停用，再启用时贡献可重建）
                self.host_session.release_contributions(plugin_id)
                self._shortcut_plugins.discard(plugin_id)
            self._submit("enable", work)
            return
        # [F2] 新模型插件（受管安装/开发源）：desired 按 plugin_id 持久化
        # 到 store 并写入 inventory（同一快照），不碰旧 catalog 配置
        if self._inventory is not None:
            try:
                self._inventory.set_desired(plugin_id, enabled)
            except KeyError:
                self._message(_tr("unknown_plugin", "未知插件，无法更改启停。"))
                return
            if self._desired_store is not None:
                desired = self._desired_store.load()
                desired[plugin_id] = bool(enabled)
                self._desired_store.save(desired)
            self.refresh()

    def launch(self, plugin_id: str) -> None:
        if self._closed or plugin_id in self._busy or self._writing:
            return
        # [F6] 停新调用：已进入停用流程的插件拒绝新调用（不排队）
        if not self.host_session.lifecycle.accepting_calls(plugin_id) and \
                self.host_session.lifecycle.is_stopped(plugin_id):
            self._message(_tr("plugin_stopping", "该插件正在停用，暂不能发起新调用。"))
            return
        info = next((item for item in self._infos if item.plugin_id == plugin_id), None)
        if info is not None and not info.can_launch:
            self._message(info.launch_unavailable_reason or "该插件没有可用命令入口。")
            return
        # [F10] 未保护第三方执行入口禁用：非 builtin 源的执行必须经
        # seatbelt 级隔离。判定用**当前启动管线实际具备的隔离**（不是
        # 平台能力）：受限执行路径 G 阶段接入前为 "none"——第三方一律
        # 拒绝并如实说明；接入后此处升为 seatbelt 并走受限执行。
        if info is not None and getattr(info, "source", "builtin") != "builtin":
            from src.shared.plugins.sandbox import ExecutionGate
            gate = ExecutionGate()
            allowed, reason = gate.can_execute(
                trusted=False,
                isolated_level=LAUNCH_PIPELINE_ISOLATION)
            if not allowed:
                self._message(reason)
                return
        # [F2] 命令键：新 id（偏好/中心建档）→ legacy 短 id（命令层寻址），
        # 从同一合并快照解析；旧接线（无 factory）id 即命令键
        command_key = info.command_key if info is not None and info.command_key else plugin_id
        # Freeze the selection at the user's gesture, never at async completion.
        paths = list(self.selected_paths())
        self._busy.add(plugin_id)
        # [F6] 启动即登记为归属该插件的任务：停用时按 owner 取消、
        # 完成回报（transport 应答）确认终止——贡献保留到确认之后
        job_id = f"launch:{plugin_id}:{len(self._launch_jobs) + 1}"
        try:
            self.host_session.lifecycle.jobs.start(job_id, plugin_id=plugin_id)
        except RuntimeError as exc:
            self._busy.discard(plugin_id)
            self._message(str(exc))
            return
        self._launch_jobs[plugin_id] = job_id
        self._project()

        def work() -> object:
            prepared = self._commands.prepare(command_key, paths)
            # [PR-05] 手势冻结后在 worker 内做执行前权威复核（停用/接口
            # 变化在此拒绝）；dispatch 只剩 Qt 线程安全的提交，GUI 线程
            # 不补同步扫描。
            return self._commands.revalidate(prepared)

        self._submit(f"launch:{plugin_id}", work)

    def _launch_finished(self, plugin_id: str, success: bool, message: str) -> None:
        if self._closed:
            return
        # [F2] 回报携带命令键（线上短 id）→ 换回 UI 新 id（同快照解析）
        ui_id = next((item.plugin_id for item in self._infos
                      if item.command_key == plugin_id), plugin_id)
        # [F6] 应答到达 = 任务终态确认（取消请求若有也已就此收口）
        job_id = self._launch_jobs.pop(ui_id, None)
        if job_id is not None:
            self.host_session.lifecycle.jobs.confirm_cancelled(job_id) \
                if self.host_session.lifecycle.jobs.state(job_id) == "cancel_requested" \
                else self.host_session.lifecycle.jobs.finish(job_id)
        self._busy.discard(ui_id)
        if success:
            self.preferences.plugin_recent = [ui_id, *[pid for pid in self.preferences.plugin_recent if pid != ui_id]]
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
        # [F5] 快捷键成为有所有权的贡献：QAction 的注销只由释放回调承载
        # （整组替换时旧集先释放，失败保留可重试）——不设第二条清理路径
        assigned = self.preferences.plugin_shortcuts
        entries_by_plugin: dict[str, list[tuple[str, Callable[[], None]]]] = {}
        self._actions = []
        for info in self._infos:
            if not info.can_launch or info.plugin_id not in assigned:
                continue
            action = QAction(info.name, self.panel)
            action.setShortcut(QKeySequence(assigned[info.plugin_id]))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(lambda checked=False, pid=info.plugin_id: self.launch(pid))
            self.panel.addAction(action)
            self._actions.append(action)
            entries_by_plugin.setdefault(info.plugin_id, []).append(
                (assigned[info.plugin_id],
                 lambda a=action: self._release_action(a)))
        for plugin_id in self._shortcut_plugins - set(entries_by_plugin):
            self.host_session.replace_shortcuts(plugin_id, [])   # 不再可启动：整组注销
        for plugin_id, entries in entries_by_plugin.items():
            self.host_session.replace_shortcuts(plugin_id, entries)
        self._shortcut_plugins = set(entries_by_plugin)
        self._focus_changed(None, QApplication.focusWidget())

    def _release_action(self, action: QAction) -> None:
        try:
            action.setEnabled(False)
            self.panel.removeAction(action)
        except RuntimeError:
            return   # 动作已随面板销毁：无事可做
        action.deleteLater()

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

    def _retire_plugin_async(self, plugin_id: str, attempt: int = 0) -> None:
        """[F6] 异步推进停用：停新→按 owner 取消→等确认→清理贡献。

        QTimer 轮询重入（GUI 不 sleep）；清理失败/任务未确认时按次重试
        （上限后如实报告残留，不假装 stopped）。
        """
        if self._closed and attempt == 0:
            pass   # 关停路径也走同一异步推进（应用退出由进程兜底）
        report = self.host_session.retire_plugin(plugin_id)
        if report.get("fully_stopped"):
            self._retire_polls.pop(plugin_id, None)
            residue = report.get("jobs_still_terminating") or ()
            if residue or report.get("cleanup_failures"):
                self._message(_tr(
                    "retire_pending",
                    "插件停用仍在收尾（任务终止/清理重试），稍后自动完成。"))
            return
        attempt += 1
        self._retire_polls[plugin_id] = attempt
        if attempt > 50:   # ~5s：如实报告，不再无限轮询
            self._message(_tr("retire_stuck",
                              "插件停用未完成：任务尚未确认终止或清理失败（可重试）。"))
            return
        QTimer.singleShot(100, lambda: self._retire_plugin_async(plugin_id, attempt))

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        # [F5/F6] 关停：全部插件贡献退役（快捷键释放 + 名下窗口关闭 +
        # 停用状态机收尾，失败可重试语义由会话保留；GUI 不 sleep——
        # 未确认终止的任务由异步轮询推进，如实报告残留）
        for plugin_id in list(self._shortcut_plugins):
            self.host_session.release_contributions(plugin_id)
        self._shortcut_plugins.clear()
        for plugin_id in list(self._launch_jobs):
            self._retire_plugin_async(plugin_id)
        self._pool.clear()
        self.center.close()
        self.launcher.close()
        self.center.deleteLater()
        self.launcher.deleteLater()
        self._launcher_action.setEnabled(False)
        for action in self._actions:
            try:
                action.setEnabled(False)
            except RuntimeError:
                pass   # 已随释放回调销毁
