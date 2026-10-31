import os
import json
import threading
import unicodedata
import logging

logger = logging.getLogger(__name__)
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QFileDialog, QMenu, QDockWidget
from src.shared.i18n.i18n import I18N

from PyQt6.QtWebEngineWidgets import QWebEngineView

from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtCore import QUrl, pyqtSlot, pyqtSignal, QObject, Qt, QTimer
from src.app.ui.library_adapter import LibraryAdapter

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

        # Right-click context menu for floating toggle
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

        self.browser = QWebEngineView(self)
        self.layout.addWidget(self.browser)

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
        self.bridge.directoryChanged.connect(self.directory_changed)
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
            if self._pending_path:
                self._set_path_js(self._pending_path)
                self._pending_path = None
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
            self.browser.setGeometry(self.rect())
            self.browser.updateGeometry()
            self.browser.repaint()
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
            if self._page_loaded:
                # 页面已加载，直接调用
                self._set_path_js(path)
            else:
                # 页面未加载，保存路径等待加载完成
                self._pending_path = path

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
