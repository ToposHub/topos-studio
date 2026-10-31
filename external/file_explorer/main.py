"""[ARCH-1] 组合根与兼容外观。

实现已按职责拆分：webapp（Flask 外壳/鉴权/端口）、db（SQLite/config.json/
目录缓存）、bridge（Qt 信号桥 + CopyWorker + ensure_runtime）、thumbs（后台
生成线程）、routes（全部路由）、window（独立模式主窗口）、__main__（独立
入口）。本文件只按依赖序导入各模块完成路由注册，并再导出公开符号——宿主
与测试的 ``from file_explorer.main import ...`` / ``import main`` 无需改动。
注意：routes 以平铺名导入一次，勿再以 ``file_explorer.routes`` 重复导入。
"""
import logging
import os
import sys
import threading
import time

os.environ['QT_MAC_WANTS_LAYER'] = '1'

# ⚠️ CRITICAL for Windows: QtWebEngineWidgets must be imported before QCoreApplication
# is created. This module is imported early, so we need to ensure proper initialization.
try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
except ImportError:
    pass  # WebEngine not available, will fail gracefully later

from PyQt6.QtWidgets import QApplication

import const
import utils
from thumb_engine import engine as thumb_engine
from plugin_system import plugin_manager

# 依赖序：webapp(零内部依赖) → db → bridge → thumbs → routes(注册路由) → window
import webapp
from webapp import app, APP_TOKEN, ROOT_PATH, find_available_port
from db import (
    DB_FILE, CONFIG_FILE, DIR_CACHE, CACHE_TTL, CACHE_LOCK, CONFIG_LOCK,
    _db, _meta_key, invalidate_cache, _absorb_nfd_twin, init_db,
    load_config, save_config,
)
from bridge import AppBridge, CopyWorker, bridge, ensure_runtime
from thumbs import (
    ffmpeg_semaphore, thumbnail_queue, thumbnail_pending, thumbnail_lock,
    _meta_queue, _rating_update_queue, _rating_update_lock,
    _thumbnail_update_queue, _thumbnail_queue_lock,
    _process_meta_queue, process_thumbnail_queue,
    shutdown_thumbnail_worker, _ensure_thumbnail_worker,
)
import routes  # noqa: F401  # import 即完成 @app.route 注册
from routes import classify_ext, execute_smart_query, _volume_available
from window import MainWindow, _js_quote

logger = logging.getLogger(__name__)

# 将 invalidate_cache 挂载到 app 实例，供插件调用
app.invalidate_cache = invalidate_cache


def __getattr__(name):
    """可变模块状态的动态转发：FLASK_PORT 在 ensure_runtime() 后才被赋值，
    from-import 会绑定过期的 None，这里保证 main.FLASK_PORT 恒取当前值。"""
    if name == 'FLASK_PORT':
        return webapp.FLASK_PORT
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def run_standalone():
    """独立模式进程入口（python main.py / python -m file_explorer 共用）。"""
    # [ARCH-4] 独立模式控制台可见的最低日志级别（嵌入模式由宿主统一配置）
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # [ARCH-6] 显式运行时初始化：端口/插件/DB/工作线程（不再依赖 import 副作用）
    ensure_runtime()
    # 启动 Flask（使用自动选择的端口）
    # [ARCH-8] waitress 优先（生产级 WSGI），未安装时回退 werkzeug dev server
    try:
        from waitress import serve as _waitress_serve
        flask_thread = threading.Thread(
            target=lambda: _waitress_serve(app, host='127.0.0.1', port=webapp.FLASK_PORT, threads=4),
            daemon=True)
    except ImportError:
        flask_thread = threading.Thread(target=lambda: app.run(port=webapp.FLASK_PORT, use_reloader=False), daemon=True)
    flask_thread.start()
    # 等待 Flask 就绪（[ARCH-7] 打 /api/health 而非业务首页，避免渲染整页只为探活）
    import urllib.request
    import socket
    _flask_ready = False
    for i in range(const.HEALTH_PROBE_ATTEMPTS):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{webapp.FLASK_PORT}/api/health', timeout=1.0) as response:
                if response.status == 200:
                    _flask_ready = True
                    break
        except Exception:
            time.sleep(0.1)
    if not _flask_ready:
        # [R1-7] bind 失败此前照常起 Qt → 白屏窗口无提示；现在弹窗并退出
        logger.error(f"[file_explorer] Flask server failed to start on port {webapp.FLASK_PORT}")
        try:
            from PyQt6.QtWidgets import QMessageBox
            if QApplication.instance() is None:
                QApplication(sys.argv)
            QMessageBox.critical(
                None, "file_explorer",
                f"Server failed to start on port {webapp.FLASK_PORT}.\nThe application will now exit.")
        except Exception:
            pass
        sys.exit(1)
    logger.info(f"[file_explorer] Flask server started on port {webapp.FLASK_PORT}")
    # 启动 Qt
    qt_app = QApplication(sys.argv)

    # [BUG-P2-05] 退出路径补 CopyWorker 等待：复制进行中退出应用会触发
    # QThread 析构崩溃（"Destroyed while running"）
    def _shutdown_on_quit():
        shutdown_thumbnail_worker()
        for t in getattr(globals().get('_main_window'), '_copy_threads', []):
            if t.isRunning():
                t.wait(5000)
    qt_app.aboutToQuit.connect(_shutdown_on_quit)
    global _main_window
    window = MainWindow()
    _main_window = window
    sys.exit(qt_app.exec())


if __name__ == '__main__':
    run_standalone()
