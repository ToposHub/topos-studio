"""[ARCH-1] 独立模式 Qt 主窗口：WebEngine 容器 + 播放引擎挂载 + 桥接信号处理。"""
import json
import logging
import os
import unicodedata

from PyQt6.QtCore import QTimer, QMimeData, QUrl
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QFileDialog, QMainWindow
from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel

import webapp
from plugin_system import plugin_manager
from webapp import ROOT_PATH
from db import load_config, save_config, invalidate_cache
from bridge import bridge, CopyWorker

logger = logging.getLogger(__name__)


def _js_quote(value) -> str:
    """[SEC-7] Python 值 → 安全的 JS 字符串字面量。

    json.dumps 覆盖引号/反斜杠/换行/反引号全部转义（ensure_ascii 输出
    \\uXXXX），替代旧的单引号手工转义——路径含特殊字符即可注入 JS。
    """
    return json.dumps(str(value))

class MainWindow(QMainWindow):
    """程序主窗口，承载 WebEngine 和播放器"""
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AssetDeck Pro")
        self._play_engines = []  # plugins/video_pro 经 mainwindow_ready 钩子装载
        # 连接信号与槽
        bridge.play_video_signal.connect(self.start_player)
        bridge.copy_files_signal.connect(self.handle_copy_files)
        bridge.paste_files_signal.connect(self.handle_paste_files)
        bridge.cancel_paste_signal.connect(self.handle_paste_cancel)
        bridge.open_folder_dialog_signal.connect(self.handle_open_folder_dialog)
        bridge.snapshot_signal.connect(self.handle_snapshot)
        bridge.add_library_signal.connect(self.handle_add_library)
        bridge.thumbnail_updated.connect(self.handle_thumbnail_updated)
        bridge.rating_updated.connect(self.handle_rating_updated)  # 新增：星级更新信号
        # 初始化界面
        conf = load_config()
        self.resize(conf.get("window_width", 1400), conf.get("window_height", 900))
        
        # Restore window position if available
        wx = conf.get("window_x")
        wy = conf.get("window_y")
        if wx is not None and wy is not None:
            self.move(wx, wy)
            
        profile = QWebEngineProfile.defaultProfile()
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
        profile.clearHttpCache()
        
        # 设置 QWebChannel 以支持前端调用本地播放器
        self.browser = QWebEngineView()
        self.browser.page().setWebChannel(QWebChannel(self.browser))
        self.browser.page().webChannel().registerObject('pyBridge', bridge)
        
        self.browser.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.browser.settings().setAttribute(QWebEngineSettings.WebAttribute.AllowRunningInsecureContent, True)
        self.browser.setStyleSheet("background: #121212;")
        self.browser.page().setBackgroundColor(QColor(18, 18, 18))  # #121212 - 防止白屏闪烁
        self.setCentralWidget(self.browser)
        self.browser.loadFinished.connect(self.on_page_loaded)
        self.show()
        # [V1/GAP-03] 独立模式 QApplication 此刻已存在：补挂目录监视
        # （ensure_runtime 先于 QApplication 调用时 watcher 会自动跳过，这里幂等补启）
        try:
            import watcher
            watcher.setup_watcher()
        except Exception as e:
            logger.warning(f"[V1] 目录监视补挂失败（跳过）: {e}")
        QTimer.singleShot(600, self.load_home_page)
        # 主窗口就绪：Qt 型插件此时才创建自己的部件（如 video_pro 播放器）
        plugin_manager.fire_event('mainwindow_ready', window=self)

    def attach_play_engine(self, engine):
        """播放引擎注册接缝（plugins/video_pro 调用）"""
        self._play_engines.append(engine)

    def load_home_page(self): self.browser.setUrl(QUrl(f"http://127.0.0.1:{webapp.FLASK_PORT}"))
    def on_page_loaded(self, success):
        if success: logger.debug("WebEngine: 页面加载成功")
    def start_player(self, path):
        for engine in self._play_engines:
            if engine.can_play(path):
                engine.play(path)
                return
        self.trigger_js("window.showToast('没有可用的视频播放引擎（video_pro 插件未加载）', 'error');")

    def trigger_js(self, code):
        # [ARCH-4] runJavaScript 失败可见化：None 结果（页面未就绪/JS 抛错）记日志
        def _cb(result):
            if result is None:
                logger.warning("trigger_js 未返回结果: %s", code[:120])
        self.browser.page().runJavaScript(code, 0, _cb)
    def resizeEvent(self, event):
        for engine in self._play_engines:
            engine.resize(self.width(), self.height())
        super().resizeEvent(event)
    def closeEvent(self, event):
        for engine in self._play_engines:
            engine.shutdown()
        # [BUG-P0-01] 退出路径补 CopyWorker 等待：复制进行中关窗会让
        # 运行中的 QThread 随窗口析构（"Destroyed while running" 崩溃）
        for t in getattr(self, '_copy_threads', []):
            if t.isRunning():
                t.wait(5000)
        save_config({
            'window_width': self.width(), 
            'window_height': self.height(),
            'window_x': self.x(),
            'window_y': self.y()
        })
        event.accept()
    def handle_copy_files(self, paths):
        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile(p) for p in paths])
        QApplication.clipboard().setMimeData(mime_data)
        # Frontend handles the toast notification
    def handle_open_folder_dialog(self):
        folder = QFileDialog.getExistingDirectory(self, "选择目录", ROOT_PATH)
        if folder:
            self.trigger_js(
                f"window.__FE.loadPath({_js_quote(folder.replace('\\', '/'))})")
    def handle_snapshot(self):
        for engine in self._play_engines:
            if engine.snapshot():
                return
        self.trigger_js("window.showToast('播放器未激活', 'error');")
    def handle_add_library(self):
        path = QFileDialog.getExistingDirectory(self, "选择库文件夹", ROOT_PATH)
        if path:
            # 1. 规范化路径
            norm_path = unicodedata.normalize('NFC', path.replace('\\', '/'))
            # 2. 获取配置
            conf = load_config()
            libs = conf.get("libraries", [])
            # 3. 检查重复
            if any(lib['path'] == norm_path for lib in libs):
                self.trigger_js("window.showToast('该文件夹已在库中', 'warning');")
                return
            # 4. 添加并保存
            name = os.path.basename(norm_path) or norm_path
            libs.append({"name": name, "path": norm_path})
            save_config({"libraries": libs})
            # 5. 通知前端刷新
            self.trigger_js("loadPinnedLibraries()")
    def handle_paste_files(self, target_dir):
        mime_data = QApplication.clipboard().mimeData()
        if mime_data.hasUrls():
            # [CRASH-7] 运行中的 CopyWorker 统一持表管理：直接覆盖引用会让
            # 进行中的 QThread 被 GC（"QThread: Destroyed while running" 崩溃）
            worker = CopyWorker(mime_data.urls(), target_dir)
            worker.progress_signal.connect(self.update_copy_progress)
            # [U10] 按信号发送者绑定（此前 copy_finished 取"最新 worker"：
            # 两个重叠粘贴时先结束者误清另一者的目录缓存、把仍在运行的
            # worker 移出等待表——退出等待失效，P0-01 崩溃面回归）
            worker.finished_signal.connect(
                lambda errs, w=worker: self.copy_finished(errs, w))
            if not hasattr(self, '_copy_threads'):
                self._copy_threads = []
            # [BUG-P0-01] 顺序必须是 append → start() → 过滤：未启动的
            # QThread isRunning() 恒 False，先过滤会把刚加入的 worker 立即
            # 移除，第二次粘贴覆盖 copy_thread 引用后运行中的线程被 GC
            # （"QThread: Destroyed while running" 崩溃）
            self._copy_threads.append(worker)
            self.copy_thread = worker  # 兼容 copy_finished 的旧引用
            worker.start()
            self._copy_threads = [t for t in self._copy_threads if t.isRunning()]
            self.trigger_js("showPasteProgress()")
    def handle_paste_cancel(self):
        """[BUG-P2-06] 取消所有进行中的复制（半成品由 worker 自行清理）"""
        for t in getattr(self, '_copy_threads', []):
            if t.isRunning():
                t.cancel()

    def update_copy_progress(self, percent, speed): self.trigger_js(f"updatePasteProgress({int(percent)}, {_js_quote(speed)})")
    def copy_finished(self, errors=None, worker=None):
        # [U10] 多个粘贴重叠时：只在本 worker 结束且无其它运行中的复制时
        # 才收起进度条，避免先结束者把后结束者的进度条提前藏掉
        if worker is not None:
            invalidate_cache(worker.target_dir)
            if worker in getattr(self, '_copy_threads', []):
                self._copy_threads.remove(worker)
        still_running = any(t.isRunning() for t in getattr(self, '_copy_threads', []))
        if not still_running:
            self.trigger_js("hidePasteProgress()")
        self.trigger_js("reloadFiles()")
        # [FUNC-13] 失败项上报 UI（此前磁盘满/权限错都显示"成功"）
        if errors:
            head = '、'.join(errors[:3])
            msg = head + (f" 等 {len(errors)} 项失败" if len(errors) > 3 else '')
            self.trigger_js(f"window.showToast({_js_quote(msg)}, 'error');")
        if getattr(self, 'copy_thread', None) is worker:
            self.copy_thread = None

    def handle_thumbnail_updated(self, path):
        # [SEC-7] json 字面量转义（旧实现只转义单引号，换行/反引号即注入）
        safe_path = _js_quote(path)
        self.trigger_js(f"if(window.refreshThumbnail) window.refreshThumbnail({safe_path});")

    def handle_rating_updated(self, path, rating):
        """处理星级评定更新，通知前端刷新"""
        try:
            rating_val = int(rating)
        except (TypeError, ValueError):
            return
        self.trigger_js(f"if(window.updateFileRating) window.updateFileRating({_js_quote(path)}, {rating_val});")
