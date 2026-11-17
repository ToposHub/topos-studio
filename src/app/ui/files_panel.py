import os
import json
import threading
import unicodedata
import logging

logger = logging.getLogger(__name__)
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QFileDialog, QMenu, QDockWidget,
                            QHBoxLayout, QLabel, QStackedWidget)
from src.shared.i18n.i18n import I18N

from PyQt6.QtWebEngineWidgets import QWebEngineView

from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtCore import QUrl, pyqtSlot, pyqtSignal, QObject, Qt, QTimer
from src.app.ui.library_adapter import LibraryAdapter
from src.shared.ui.widgets.chevron_combo import ChevronComboBox

# 导入 file_explorer 的全局桥接对象和配置函数 (optional: may not be installed)
try:
    from file_explorer.main import bridge as fe_bridge, load_config, save_config
    from file_explorer.main import APP_TOKEN as _FE_APP_TOKEN  # [ARCH-10] 缓存失效用
except ImportError:
    fe_bridge = None
    load_config = None
    save_config = None
    _FE_APP_TOKEN = None

class SyncBridge(QObject):
    directoryChanged = pyqtSignal(str)
    imageOpened = pyqtSignal(str)
    videoOpened = pyqtSignal(str)
    ratingChanged = pyqtSignal(str, int)  # 新增：星级评定变更信号

    @pyqtSlot(str)
    def onDirectoryChanged(self, path):
        """Called by JavaScript when the user navigates folders."""
        self.directoryChanged.emit(path)

    @pyqtSlot(str)
    def openImage(self, path):
        """Called by JavaScript to open an image."""
        self.imageOpened.emit(path)

    @pyqtSlot(str)
    def openVideo(self, path):
        """Called by JavaScript to play a video in embedded mode."""
        self.videoOpened.emit(path)

    @pyqtSlot(str, int)
    def onRatingChanged(self, path, rating):
        """Called by JavaScript when user changes file rating."""
        self.ratingChanged.emit(path, rating)

class _ServerHealthProbe(QObject):
    """[ARCH-7] 后台探测 file_explorer /api/health，结果经队列信号回 GUI 线程。

    替代旧的"盲等 100ms 再加载首屏"：冷启动时服务未必就绪，盲等必然
    失败一轮再靠 CRASH-6 退避补救；改为探活成功后才发起首屏加载。"""
    probe_finished = pyqtSignal(bool)

    def probe(self, health_url: str) -> None:
        def _run():
            ok = False
            try:
                import urllib.request
                with urllib.request.urlopen(health_url, timeout=0.8) as resp:
                    ok = resp.status == 200
            except Exception:
                ok = False
            self.probe_finished.emit(ok)
        threading.Thread(target=_run, daemon=True, name="fx-health-probe").start()

class FilesPanel(QWidget):
    # Public signals — MainWindow should use these, NOT self.bridge
    directory_changed = pyqtSignal(str)
    image_opened = pyqtSignal(str)
    video_opened = pyqtSignal(str)
    rating_changed = pyqtSignal(str, int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self._files_root_path: str | None = None
        self._native_panel = None
        self._native_transport = None
        self._files_jobs = None
        self._jobs_dialog = None
        self._native_active = False
        self._pending_native_open: tuple[str, str] | None = None
        from src.app.ui.native_files_prefs import NativeFilesPreferences
        self._files_prefs = NativeFilesPreferences()
        self._mode_switch_programmatic = False
        mode_bar = QHBoxLayout()
        mode_bar.setContentsMargins(8, 4, 8, 4)
        mode_bar.addWidget(QLabel(I18N.tr("files.mode", "浏览方式"), self))
        self.mode_combo = ChevronComboBox(self)
        self.mode_combo.addItems([
            I18N.tr("files.mode.full", "完整素材管理"),
            I18N.tr("files.mode.native", "原生浏览（预览）"),
        ])
        self.mode_combo.setAccessibleName(I18N.tr("files.mode", "浏览方式"))
        mode_bar.addWidget(self.mode_combo)
        mode_bar.addStretch()
        self.layout.addLayout(mode_bar)
        self.content_stack = QStackedWidget(self)
        self.layout.addWidget(self.content_stack, 1)

        # Right-click context menu for floating toggle
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        self.browser = QWebEngineView(self)
        self.content_stack.addWidget(self.browser)
        self.mode_combo.currentIndexChanged.connect(self._set_browser_mode)

        # 设置深色背景，防止页面切换时白屏闪烁
        self.browser.setStyleSheet("background: #121212;")
        from PyQt6.QtGui import QColor
        self.browser.page().setBackgroundColor(QColor(18, 18, 18))  # #121212

        # Set up WebChannel for communication (双向通信)
        self.bridge = SyncBridge()
        self.bridge.videoOpened.connect(self._handle_play_video)
        self.channel = QWebChannel()
        self.channel.registerObject("pyBridge", self.bridge)
        self.browser.page().setWebChannel(self.channel)

        # Bridge internal SyncBridge signals to public FilesPanel signals
        self.bridge.directoryChanged.connect(self._on_legacy_directory_changed)
        self.bridge.imageOpened.connect(self.image_opened)
        self.bridge.videoOpened.connect(self.video_opened)
        self.bridge.ratingChanged.connect(self.rating_changed)

        # [CRASH-6] 页面加载状态跟踪：重试次数有上限 + 指数退避
        # [AUD-3804] 错误页标志 + 启动失败独立计数：setHtml 渲染的错误页
        # 同样触发 loadFinished(True)，此前会把 _page_loaded 置 True、
        # _load_retries 清零——依赖缺失（start_server 持续失败）时每 4 秒
        # 无限重载错误页，绕过 [CRASH-6] 的有上限退避设计。
        # （必须在下方首次 _show_error_page() 之前初始化。）
        self._page_loaded = False
        self._load_retries = 0
        self._pending_path = None  # 等待页面加载后设置的路径
        self._is_error_page = False
        self._start_failures = 0

        # Initialize LibraryAdapter to start the server (singleton)
        self.adapter = LibraryAdapter.instance()
        started = self.adapter.start_server()
        if not started:
            self._show_error_page()

        # 预加载：初始化时立即加载 URL，避免切换面板时闪白
        self._server_url = self.adapter.get_server_url()

        # 启动失败时先保留错误页；[ARCH-7] 健康探针就绪或兜底重试会覆盖它。
        # [CRASH-6] loadFinished 只连一次——重试路径复用，避免重复连接叠加
        self.browser.loadFinished.connect(self._on_page_loaded)
        self.browser.renderProcessTerminated.connect(self._on_render_process_terminated)
        # [ARCH-7] 轮询 /api/health 就绪后再加载首屏（替代盲等 100ms；
        # 超时兜底交由 _load_url 的启动检查 + CRASH-6 退避）
        self._health_checks = 0
        self._probe_inflight = False
        self._health_probe = _ServerHealthProbe(self)
        self._health_probe.probe_finished.connect(self._on_health_probe)
        self._wait_server_healthy()

        # 连接 file_explorer 全局桥接信号（嵌入式模式下这些信号无 MainWindow 接收）
        if fe_bridge is not None:
            fe_bridge.add_library_signal.connect(self._handle_add_library)
            fe_bridge.open_folder_dialog_signal.connect(self._handle_open_folder_dialog)
            fe_bridge.play_video_signal.connect(self._handle_play_video)  # 视频播放（灯箱模式）

        # 保存主窗口引用，用于创建播放器
        self._main_window = parent
        # U06：默认入口切换（B10 持久化）。程序化恢复不算用户选择，不回写。
        self._mode_switch_programmatic = True
        try:
            self.mode_combo.setCurrentIndex(1 if self._files_prefs.native_default else 0)
        finally:
            self._mode_switch_programmatic = False

    # [ARCH-7] 健康探测节奏：100ms 一次，最多 50 次（5s）后兜底走 _load_url
    _HEALTH_CHECK_INTERVAL_MS = 100
    _HEALTH_CHECK_MAX = 50

    def _wait_server_healthy(self):
        """[ARCH-7] 轮询 /api/health，服务就绪后才加载页面首屏"""
        if self._page_loaded or self._probe_inflight:
            return
        self._health_checks += 1
        if self._health_checks > self._HEALTH_CHECK_MAX:
            self._load_url()
            return
        base = self.adapter.get_server_url()
        if not base:
            QTimer.singleShot(self._HEALTH_CHECK_INTERVAL_MS, self._wait_server_healthy)
            return
        self._probe_inflight = True
        self._health_probe.probe(base.rstrip('/') + '/api/health')

    def _on_health_probe(self, ok: bool):
        self._probe_inflight = False
        if ok:
            self._load_url()
        else:
            QTimer.singleShot(self._HEALTH_CHECK_INTERVAL_MS, self._wait_server_healthy)

    def _load_url(self) -> None:
        """延迟加载 URL，确保服务器已启动"""
        if not self.adapter.start_server():
            # [AUD-3804] 启动失败重试有独立上限：不与页面加载的
            # _load_retries 混用，也不被错误页的 loadFinished(True) 重置；
            # 超限后停表保留错误页（正是该错误页设计针对的
            # "依赖缺失"场景）。
            self._start_failures += 1
            if self._start_failures > 5:
                logger.error(
                    "file_explorer server failed to start after %d attempts, "
                    "keeping error page",
                    self._start_failures - 1,
                )
                self._show_error_page()
                return
            self._show_error_page()
            QTimer.singleShot(4000, self._load_url)
            return
        self._start_failures = 0
        # [CRASH-6] 每次加载前刷新 URL（服务器可能在换端口后自愈）
        self._server_url = self.adapter.get_server_url()
        self._is_error_page = False
        self.browser.setUrl(QUrl(self._server_url))

    def _on_page_loaded(self, success: bool) -> None:
        """页面加载完成后的回调"""
        if self._is_error_page:
            # [AUD-3804] 错误页 setHtml 也触发 loadFinished(True)——不得
            # 据此认定页面就绪（否则后续 set_root_path 向错误页发 JS、
            # _wait_server_healthy 的探测被短路、重试计数被清零）。
            return
        if success:
            self._page_loaded = True
            self._load_retries = 0
            # 如果有等待设置的路径，现在设置它
            if (self._pending_path and not self.__dict__.get("_native_active", False)
                    and self.__dict__.get("_pending_native_open") is None):
                self._set_path_js(self._pending_path)
                self._pending_path = None
            if self.__dict__.get("_pending_native_open") is not None:
                self._open_pending_native_file()
        else:
            # [CRASH-6] 重试有上限 + 指数退避；超限保留错误页不再覆盖
            self._load_retries += 1
            if self._load_retries > 5:
                logger.error(f"Page failed to load from {self._server_url} "
                             f"after {self._load_retries - 1} retries, giving up")
                self._show_error_page()
                return
            delay_ms = min(3200, 200 * (2 ** (self._load_retries - 1)))
            logger.warning(f"Page load failed (attempt {self._load_retries}), "
                           f"retrying in {delay_ms}ms")
            QTimer.singleShot(delay_ms, self._load_url)

    def _on_render_process_terminated(self, status, exit_code):
        """[CRASH-6] 渲染进程崩溃后自动恢复页面（此前无任何处理，面板永久白屏）。"""
        logger.error(f"FilesPanel render process terminated: status={status}, "
                     f"exit={exit_code}; reloading")
        self._page_loaded = False
        QTimer.singleShot(500, self._load_url)

    def get_main_window(self):
        """获取主窗口引用，用于创建播放器"""
        return self._main_window

    def _show_error_page(self) -> None:
        """Display an error message when file_explorer server cannot start."""
        # [AUD-3804] 置错误页标志：loadFinished 对本页的成功回调一律忽略
        self._is_error_page = True
        self.browser.setHtml("""
        <html>
          <body style="background:#121212;color:#ddd;font-family:sans-serif;padding:16px;">
            <h3>素材浏览器启动失败</h3>
            <p>external/file_explorer 未能加载，请检查：</p>
            <ul>
              <li>external/file_explorer 目录是否存在</li>
              <li>Flask / werkzeug 是否已安装</li>
              <li>查看日志获取详细错误信息</li>
            </ul>
          </body>
        </html>
        """)

    def _show_context_menu(self, pos):
        """Right-click context menu with float/dock toggle."""
        menu = QMenu(self)
        dock = self.parent()
        while dock and not isinstance(dock, QDockWidget):
            dock = dock.parent()
        if dock:
            act_text = I18N.tr("dock.dock", "Dock") if dock.isFloating() else I18N.tr("dock.float", "Float")
            menu.addAction(act_text).triggered.connect(lambda: dock.setFloating(not dock.isFloating()))
        menu.exec(self.mapToGlobal(pos))

    def resizeEvent(self, event):
        """当 FilesPanel 大小改变时，更新播放器大小"""
        super().resizeEvent(event)
        # 通知主窗口调整播放器大小
        if self._main_window and hasattr(self._main_window, 'video_player') and self._main_window.video_player is not None:
            player = self._main_window.video_player
            if player.isVisible():
                player.setGeometry(self.rect())

    def refresh_layout(self):
        """强制刷新布局，确保 QWebEngineView 正确填充整个面板"""
        # 确保 browser 的 geometry 与 panel 一致
        if self.browser:
            self.browser.updateGeometry()
        self.layout.activate()
        self.updateGeometry()
        self.repaint()

    def closeEvent(self, event):
        # Stop the server on panel close
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self):
        """[CRASH-2] 停止后台 Flask 服务（幂等）。

        FilesPanel 装在 QDockWidget 里，dock 关闭/主窗口退出不会触发本控件
        的 closeEvent——真正的清理入口是 MainWindow.closeEvent。
        """
        plugins = getattr(self, "_files_plugins", None)
        if plugins is not None:
            plugins.shutdown()
        native_transport = getattr(self, "_native_transport", None)
        if native_transport is not None:
            native_transport.shutdown()
        jobs_dialog = getattr(self, "_jobs_dialog", None)
        if jobs_dialog is not None:
            jobs_dialog.close()
        native_panel = getattr(self, "_native_panel", None)
        if native_panel is not None:
            native_panel.shutdown()
        try:
            self.adapter.stop_server()
        except Exception:
            logger.exception("FilesPanel: stop_server failed")

    def _run_js(self, js_code: str, purpose: str = "") -> None:
        """[ARCH-4] runJavaScript 失败可见化：统一回调并记录无结果的情况。

        QWebEngine 的 runJavaScript 不带回调时异常全部静默；即便带回调，
        页面未就绪/渲染进程崩溃/JS 抛错时结果为 None——统一记录，
        避免桥接指令消失无痕。"""
        tag = f" [{purpose}]" if purpose else ""

        def _cb(result):
            if result is None:
                logger.warning("runJavaScript%s 未返回结果（页面未就绪或 JS 抛错）: %s",
                               tag, js_code[:120])

        self.browser.page().runJavaScript(js_code, 0, _cb)

    def _set_path_js(self, path):
        """实际执行 JavaScript 调用

        [SEC-7] json.dumps 生成字面量：路径含引号/反斜杠/换行都不会破语法或注入。
        """
        path_literal = json.dumps(str(path))
        self._run_js(
            "(() => { "
            "const api = window.__FE; "
            "if (!api || typeof api.loadPath !== 'function') return null; "
            f"api.loadPath({path_literal}, false, false, true); "
            "return true; "
            "})()",
            purpose="set_path",
        )

    def set_root_path(self, path):
        """通过 JavaScript 调用前端 loadPath() 函数同步目录"""
        if path:
            self._files_root_path = str(path)
            if (self._native_panel is not None
                    and self.mode_combo.currentIndex() == 1
                    and self._native_panel._requested_path != str(path)):
                self._native_panel.navigate(str(path))
            if self._native_active:
                self._pending_path = path
                return
            if self._page_loaded:
                # 页面已加载，直接调用
                self._set_path_js(path)
            else:
                # 页面未加载，保存路径等待加载完成
                self._pending_path = path

    def _on_legacy_directory_changed(self, path: str) -> None:
        if self._native_active:
            return
        self._files_root_path = path
        self.directory_changed.emit(path)

    def _set_browser_mode(self, mode: int) -> None:
        if not self._mode_switch_programmatic:
            # 只有用户主动切换才改默认入口；预览借道旧宿主不算
            self._files_prefs.native_default = mode == 1
        self._native_active = mode == 1
        if mode == 1 and self._native_transport is not None:
            self._native_transport.load_libraries()
            self._native_transport.load_scan_status()
        if mode == 0:
            self.content_stack.setCurrentWidget(self.browser)
            if self._native_transport is not None:
                self._native_transport.cancel()
            if self._page_loaded and self._pending_path and self._pending_native_open is None:
                self._set_path_js(self._pending_path)
                self._pending_path = None
            return
        if self._native_panel is None:
            from src.app.ui.native_files_panel import NativeFilesPanel
            from src.app.ui.native_files_transport import NativeFilesTransport

            self._native_panel = NativeFilesPanel(self, preferences=self._files_prefs)
            self._native_transport = NativeFilesTransport(
                self.adapter.get_server_url, _FE_APP_TOKEN, self)
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
            self._native_panel.legacy_requested.connect(lambda: self.mode_combo.setCurrentIndex(0))
            self._connect_native_plugins()
            self._native_transport.loading.connect(self._native_panel.set_loading)
            self._native_transport.failed.connect(self._native_panel.show_error)
            self._native_transport.loaded.connect(self._on_native_directory_loaded)
            self.content_stack.addWidget(self._native_panel)
        self._pending_native_open = None
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
        from pathlib import Path
        from src.app.integrations.file_explorer.plugins import (
            LEGACY_BUILTIN_IDS, FileExplorerPluginCatalog,
        )
        from src.app.ui.files_plugins_controller import FilesPluginsController
        from src.features.files.repository import FilesDataLocator
        from src.shared.plugins.discovery import (
            DesiredStateStore, DevSourceRegistry, PluginSourceConfig,
        )
        try:
            import file_explorer.main as fe_main
        except ImportError:
            self._native_panel.plugins_button.setEnabled(False)
            self._native_panel.plugins_button.setToolTip("素材插件宿主不可用。")
            return
        catalog = FileExplorerPluginCatalog(
            Path(fe_main.__file__).parent / "plugins", fe_main.plugin_manager,
            fe_main.load_config, fe_main.save_config)
        # [F1] 单一 Inventory 生产接线：受管安装根与开发源注册表放在
        # files-data 配置根下（与 DB/库注册同一定位器），builtin 名单为
        # 宿主政策；legacy 目录继续作为发现根（builtin/未识别两分）
        locator = FilesDataLocator.default()
        dev_registry = DevSourceRegistry(locator.root / "plugin_dev_sources.json")

        def sources() -> PluginSourceConfig:
            return PluginSourceConfig.for_host(
                builtin_ids=LEGACY_BUILTIN_IDS,
                install_root=locator.root / "plugins",
                dev_roots=dev_registry.list())

        self._files_plugins = FilesPluginsController(
            catalog, self._native_transport, self._files_prefs, self._native_panel,
            lambda: [entry.path for entry in self._native_panel._selected_entries() if not entry.is_dir],
            lambda: self.mode_combo.setCurrentIndex(0), self,
            sources_factory=sources,
            desired_store=DesiredStateStore(locator.root / "plugin_desired.json"))
        self._native_panel.plugins_requested.connect(self._files_plugins.show_launcher)
        self._native_panel.plugin_center_requested.connect(self._files_plugins.show_center)

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
        transport.task_finished.connect(
            lambda job: (dialog.show_task_result(job),
                         transport.load_trash()) if job.get("operation") in
            ("restore", "purge") else None)
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
        self._native_panel.thumbnail_requested.emit(path, 0)

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
        # Hydrate the compatibility host only when it becomes visible. Native
        # navigation must not also render a hidden grid and request its thumbs.
        self._pending_path = path
        if changed:
            self.directory_changed.emit(path)

    def _open_native_file(self, path: str) -> None:
        """Native mode keeps image/video in-process; other media stays legacy."""
        native_panel = getattr(self, "_native_panel", None)
        if self._native_active and native_panel is not None:
            if native_panel.open_path_native(path):
                return
        self._pending_native_open = (self._native_panel.current_path, path)
        self._mode_switch_programmatic = True
        try:
            self.mode_combo.setCurrentIndex(0)
        finally:
            self._mode_switch_programmatic = False
        if self._page_loaded:
            self._open_pending_native_file()

    def _open_pending_native_file(self) -> None:
        pending, self._pending_native_open = self._pending_native_open, None
        if pending is None:
            return
        directory, path = pending
        self._pending_path = None
        self._run_js(
            "(() => { const api = window.__FE; "
            "if (!api || !api.STATE || typeof api.loadPath !== 'function') return null; "
            f"const directory = {json.dumps(directory)}, path = {json.dumps(path)}; "
            "api.STATE.pendingSelection = path; "
            "const pending = api.loadPath(directory, false, false, true); "
            "const sequence = window._loadSeq; pending.then(() => { "
            "if (window._loadSeq !== sequence || api.STATE.currentPath !== directory) return; "
            "const index = api.STATE.visibleFiles.findIndex(file => file.path === path); "
            "if (index >= 0) api.openItem(index); "
            "else api.showToast('文件不可见，请检查筛选条件或刷新目录', 'info'); "
            "}); return true; })()",
            purpose="native_file_preview",
        )

    def refresh_file_rating(self, path, rating):
        """刷新指定文件的星级显示（从 Library Panel 同步）"""
        # 页面未加载时不执行
        if not self._page_loaded:
            return

        # 通过 JavaScript 调用前端更新星级显示
        # 先更新本地文件数据中的星级，然后刷新视图
        # [SEC-7] 路径经 json.dumps 转义；rating 强制为 int
        js_code = f"""
            (function() {{
                // 查找对应的文件并更新其星级数据
                const file = STATE.visibleFiles.find(f => f.path === {json.dumps(str(path))});
                if (file) {{
                    file.rating = {int(rating)};
                    // 刷新列表以显示更新后的星级
                    if (typeof reloadFiles === 'function') {{
                        reloadFiles();
                    }}
                }}
            }})();
        """
        self._run_js(js_code, purpose="refresh_rating")
        # [ARCH-10] 通知 file_explorer 丢弃该文件所在目录的列表缓存——
        # 否则 DIR_CACHE 60s TTL 内重新导航仍显示旧评分
        threading.Thread(target=self._invalidate_fe_dir_cache, args=(str(path),),
                         daemon=True, name="fx-cache-invalidate").start()

    def _invalidate_fe_dir_cache(self, file_path: str) -> None:
        """[ARCH-10] POST /api/cache/invalidate 失效文件所在目录的缓存。"""
        if not _FE_APP_TOKEN:
            return
        try:
            import urllib.request
            base = self.adapter.get_server_url()
            if not base:
                return
            req = urllib.request.Request(
                base.rstrip('/') + '/api/cache/invalidate',
                data=json.dumps({'path': os.path.dirname(file_path)}).encode(),
                headers={'Content-Type': 'application/json',
                         'X-Auth-Token': _FE_APP_TOKEN},
                method='POST')
            with urllib.request.urlopen(req, timeout=2.0):
                pass
        except Exception:
            logger.debug("fx cache invalidate failed", exc_info=True)

    def _get_top_level_widget(self) -> QWidget:
        """获取顶层窗口部件，作为对话框的父级"""
        widget: QWidget = self
        while widget.parent() is not None:
            widget = widget.parent()
        return widget

    def _handle_add_library(self) -> None:
        """处理添加素材库文件夹信号：打开文件夹选择对话框并保存到配置"""
        if load_config is None or save_config is None:
            logger.warning("file_explorer not available, cannot add library")
            return
        parent_widget: QWidget = self._get_top_level_widget()
        root_path: str = os.path.expanduser("~")
        path: str = QFileDialog.getExistingDirectory(parent_widget, "选择库文件夹", root_path)
        if not path:
            return
        # 规范化路径
        norm_path: str = unicodedata.normalize('NFC', path.replace('\\', '/'))
        # 获取当前配置
        conf: dict = load_config()
        libs: list = conf.get("libraries", [])
        # 检查重复
        if any(lib.get('path') == norm_path for lib in libs):
            self._run_js("window.showToast('该文件夹已在库中', 'warning');",
                         purpose="toast_dup_lib")
            return
        # 添加并保存
        name: str = os.path.basename(norm_path) or norm_path
        libs.append({"name": name, "path": norm_path})
        save_config({"libraries": libs})
        # 通知前端刷新素材库列表
        self._run_js("loadPinnedLibraries()", purpose="reload_pinned")

    def _handle_open_folder_dialog(self) -> None:
        """处理打开目录对话框信号：选择目录后跳转到该路径"""
        parent_widget: QWidget = self._get_top_level_widget()
        root_path: str = os.path.expanduser("~")
        folder: str = QFileDialog.getExistingDirectory(parent_widget, "选择目录", root_path)
        if folder:
            self._set_path_js(folder)

    def _handle_play_video(self, path: str):
        """处理视频播放请求（调用主窗口的播放器）"""
        if self._main_window and hasattr(self._main_window, 'play_video_in_panel'):
            self._main_window.play_video_in_panel(path)
