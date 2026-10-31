"""[ARCH-1] Qt 桥接层：AppBridge 信号枢纽 + CopyWorker + ensure_runtime()。

`bridge` 单例被 routes/thumbs/window 与宿主（files_panel/adapter）共用。
[ARCH-6] import 本模块零副作用：插件/DB/工作线程初始化全部收在
ensure_runtime()；对 thumbs 的导入放在调用期（thumbs 模块级导入 bridge
实例，模块级互导会成环）。"""
import atexit
import logging
import os
import shutil
import threading
import time

from PyQt6.QtCore import QObject, QThread, QUrl, pyqtSignal

import utils
import const
import webapp
from plugin_system import plugin_manager
from webapp import app, find_available_port, _save_port_config
from db import init_db, load_config, save_config

logger = logging.getLogger(__name__)


class CopyWorker(QThread):
    """文件复制工作线程，处理大文件复制时不阻塞 UI

    [FUNC-13] 错误不再静默吞掉：逐文件收集、结束上报 UI；半成品文件
    清理；目录复制不跟随 symlink（防递归爆栈/重复复制）。
    """
    progress_signal = pyqtSignal(int, str)  # 进度(0-100), 速度字符串
    finished_signal = pyqtSignal(list)      # 错误信息列表（空 = 全部成功）

    def __init__(self, src_paths, target_dir):
        super().__init__()
        self.src_paths = src_paths
        self.target_dir = target_dir
        self.total_size = 0
        self.copied_size = 0
        self._is_running = True
        self.errors = []

    def cancel(self):
        """[BUG-P2-06] 请求取消：复制循环检测到后尽快退出。

        此前 _is_running 全仓库无代码置 False——"取消"是死代码，
        粘贴不可中断。取消中的文件按失败处理（记录错误 + 清理半成品）。"""
        self._is_running = False

    def run(self):
        # 计算总大小
        try:
            for path in self.src_paths:
                path = path.toLocalFile() if isinstance(path, QUrl) else path
                if os.path.isfile(path):
                    self.total_size += os.path.getsize(path)
                elif os.path.isdir(path):
                    for root, _, files in os.walk(path, followlinks=False):
                        for f in files:
                            self.total_size += os.path.getsize(os.path.join(root, f))
        except OSError: pass
        if self.total_size == 0: self.total_size = 1
        # 执行复制逻辑
        start_time = time.time()
        for url in self.src_paths:
            if not self._is_running: break
            src_path = url.toLocalFile() if isinstance(url, QUrl) else url
            if not os.path.exists(src_path): continue
            filename = os.path.basename(src_path)
            dest_path = os.path.join(self.target_dir, filename)
            # 自动重命名逻辑
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(dest_path):
                dest_path = os.path.join(self.target_dir, f"{base}_{counter}{ext}")
                counter += 1
            try:
                if os.path.isdir(src_path):
                    self._copy_tree(src_path, dest_path, start_time)
                else:
                    self._copy_file(src_path, dest_path, start_time)
            except OSError as e:
                self.errors.append(f"{filename}: {e.strerror or e}")
                self._cleanup_partial(dest_path)
        self.finished_signal.emit(self.errors)

    def _cleanup_partial(self, dest_path):
        """失败时清理半成品文件/目录，避免占位却不可用"""
        try:
            if os.path.isdir(dest_path): shutil.rmtree(dest_path, ignore_errors=True)
            elif os.path.exists(dest_path): os.remove(dest_path)
        except OSError: pass

    def _copy_file(self, src, dst, start_time):
        """文件流式复制；失败/取消抛 OSError 由上层收集并清理半成品"""
        with open(src, 'rb') as fsrc, open(dst, 'wb') as fdst:
            while True:
                if not self._is_running:
                    # [BUG-P2-06] 取消 = 有序失败：半成品由上层 _cleanup_partial 删除
                    raise OSError('已取消')
                buf = fsrc.read(1024*1024)
                if not buf: break
                fdst.write(buf)
                self.copied_size += len(buf)
                current_time = time.time()
                if current_time - start_time > 0.1:
                    speed = self.copied_size / (current_time - start_time)
                    speed_str = f"{utils.get_size_str(speed)}/s"
                    percent = int((self.copied_size / self.total_size) * 100)
                    self.progress_signal.emit(percent, speed_str)

    def _copy_tree(self, src, dst, start_time):
        """递归复制目录：不跟随 symlink，单文件失败记录后继续"""
        os.makedirs(dst, exist_ok=True)
        for item in os.listdir(src):
            s = os.path.join(src, item)
            d = os.path.join(dst, item)
            if os.path.islink(s):
                self.errors.append(f"{item}: 跳过符号链接")
                continue
            if os.path.isdir(s): self._copy_tree(s, d, start_time)
            else:
                try: self._copy_file(s, d, start_time)
                except OSError as e:
                    self.errors.append(f"{item}: {e.strerror or e}")
                    self._cleanup_partial(d)


class AppBridge(QObject):
    """前端 JS 与后端 Python 的通信桥梁信号"""
    thumbnail_updated = pyqtSignal(str)
    play_video_signal = pyqtSignal(str)
    copy_files_signal = pyqtSignal(list)
    paste_files_signal = pyqtSignal(str)
    cancel_paste_signal = pyqtSignal()  # [BUG-P2-06] 前端请求取消粘贴
    open_folder_dialog_signal = pyqtSignal()
    snapshot_signal = pyqtSignal()
    add_library_signal = pyqtSignal()
    rating_updated = pyqtSignal(str, int)  # 新增：星级评定更新信号

    def __init__(self, parent=None):
        super().__init__(parent)
        # Optional services are registered by the host application after its
        # Edit widgets/renderers are ready.  Flask/plugin code must treat this
        # as an opaque Qt-side object and never call GL APIs from a request
        # thread.
        self._edit_benchmark_service = None

    def register_edit_benchmark_service(self, service) -> None:
        """Register the host-owned live Edit benchmark service."""

        self._edit_benchmark_service = service

    def get_edit_benchmark_service(self):
        """Return the host service, or ``None`` when Edit is unavailable."""

        return self._edit_benchmark_service
    
    def dragFile(self, path): pass

    def openVideo(self, path):
        """前端调用：打开本地视频播放器"""
        self.play_video_signal.emit(path)

    # [FUNC-8] 嵌入模式 SyncBridge 有而独立模式 AppBridge 缺失的槽——
    # 前端 script.js:359/699 会调用，缺失时抛 TypeError
    def onDirectoryChanged(self, path): pass

    def onRatingChanged(self, path, rating): pass

# 实例化桥接对象
bridge = AppBridge()


# [模块级别修复 → ARCH-6] 嵌入模式没有 __main__，缩略图 worker 曾因此不启动，
# 于是插件加载/建 DB/起线程/写端口文件全被搬到模块级 import 时执行——
# 宿主 import 一次即背上全部后台资源。现在统一收进 ensure_runtime()，
# 由独立入口与宿主 adapter 在真正开始服务前显式调用；幂等可重入。
_runtime_init_lock = threading.Lock()
_runtime_initialized = False

def ensure_runtime(port=None):
    """[ARCH-6] 显式运行时初始化（import main.py 零副作用）。

    职责：端口探测 + 写共享端口配置、插件系统初始化、建 DB、
    关联应用后台预热、缩略图/meta 工作线程。

    [R1-6] port=None 时自行从 DEFAULT_PORT 起探测；嵌入模式由 adapter
    传入实际将绑定的端口，消除"探测端口写配置、实际绑定另一端口"的
    TOCTOU 漂移。"""
    global _runtime_initialized
    if _runtime_initialized:
        return
    with _runtime_init_lock:
        if _runtime_initialized:
            return
        if webapp.FLASK_PORT is None:
            webapp.FLASK_PORT = port if port is not None else find_available_port(const.DEFAULT_PORT)
        _save_port_config()
        # 初始化插件系统 (注入 app, socket/bridge, 配置方法)
        plugin_manager.init_app(
            app,
            socket=bridge,
            config_methods={'get': load_config, 'set': save_config}
        )
        # 初始化数据库
        init_db()
        # [FUNC-12] 后台预热关联应用扫描（配合 SEC-3 白名单，
        # 未就绪时 open_with 会失败，预热可基本避免）
        utils.preload_apps()
        # 调用期导入：thumbs 模块级导入 bridge 实例，模块级互导会成环
        from thumbs import _ensure_thumbnail_worker, shutdown_thumbnail_worker
        _ensure_thumbnail_worker()
        atexit.register(shutdown_thumbnail_worker)
        # [U6/GAP-05] 启动时物理清理超过保留期的应用内回收站条目
        try:
            import trash as _trash
            _trash.purge_expired(force=True)
        except Exception as e:
            logger.warning(f"[U6] 回收站过期清理失败（跳过）: {e}")
        # [V1/GAP-01] 全库索引器：延迟首扫 + 周期增量（TOPOS_TEST_MODE 不启动）
        try:
            import scanner as _scanner
            _scanner.start_autoscan()
        except Exception as e:
            logger.warning(f"[V1] 索引器启动失败（跳过）: {e}")
        # [V1/GAP-03] 目录监视：嵌入模式此时宿主 QApplication 已存在可直接启用；
        # 独立模式 ensure_runtime 先于 QApplication（main.py），由 MainWindow 再挂载
        try:
            import watcher as _watcher
            _watcher.setup_watcher()
        except Exception as e:
            logger.warning(f"[V1] 目录监视启动失败（跳过）: {e}")
        _runtime_initialized = True
