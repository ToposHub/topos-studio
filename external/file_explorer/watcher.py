"""[V1/GAP-03] 库目录监视：QFileSystemWatcher → 2s 去抖 → 缓存失效 + 单目录增量。

此前 bridge.onDirectoryChanged 是空桩：外部程序（相机导入、下载、其他编辑器）
写入库目录后，前端要等 60s DIR_CACHE 过期才可见。

约束：
- QFileSystemWatcher 非递归：注册库根 + 逐级子目录（上限 WATCH_MAX_PATHS，
  新目录在父目录变更时动态补注册）；
- 依赖 Qt 事件循环投递信号：无 QCoreApplication（headless 测试/工具脚本）
  时 setup_watcher() 自动跳过，不报错；
- 实际扫描逻辑在 scanner.scan_directory（纯枚举+DB，无子进程，可在 GUI 线程跑）；
  30 分钟兜底全量增量扫描由 scanner.start_autoscan 保证，监视只是加速通道。
"""
import logging
import os

import const

logger = logging.getLogger(__name__)

_watcher = None
_watched = set()
_pending = {}  # dir -> QTimer（去抖）


def setup_watcher():
    """启用目录监视（幂等）。返回 QFileSystemWatcher 或 None（无 Qt/已启用/失败）。"""
    global _watcher
    if _watcher is not None:
        return _watcher
    try:
        from PyQt6.QtCore import QCoreApplication, QFileSystemWatcher, QTimer, QThread
        app = QCoreApplication.instance()
        if app is None:
            logger.info('[Watcher] 无 Qt 事件循环，目录监视停用')
            return None
        # [V5 复查] 宿主在非 GUI 线程调 ensure_runtime 时，watcher/QTimer 会
        # 挂在没有事件循环的线程上——信号永远不投递，监视静默失灵
        if QThread.currentThread() is not app.thread():
            logger.warning('[Watcher] 当前线程非 GUI 线程，目录监视停用')
            return None
        _watcher = QFileSystemWatcher()
        _watcher.directoryChanged.connect(_on_dir_changed)
        import db
        for lib in (l.get('path') for l in db.load_config().get('libraries', []) if l.get('path')):
            add_tree(lib)
        logger.info(f"[Watcher] 目录监视已启用（{_watched.__len__()} 个目录）")
        return _watcher
    except Exception as e:
        logger.warning(f"[Watcher] 初始化失败（停用）: {e}")
        _watcher = None
        return None


def add_tree(root):
    """注册目录树（一级级 walk，超上限截断；隐藏/.trash/.cache 不监视）。"""
    if _watcher is None or not root or not os.path.isdir(root):
        return
    # [V5 复查] 以 watcher 实际监视集为准修剪本地缓存：目录被删后 Qt 会
    # 内部移除监视，但 _watched 仍保留旧路径——目录重建后 addPath 被缓存
    # 挡住，重建目录从此不再被监视
    try:
        _watched &= set(_watcher.directories())
    except RuntimeError:
        pass
    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith('.') and d not in {'.trash', '.cache'}]
        if dirpath not in _watched:
            if len(_watched) >= const.WATCH_MAX_PATHS:
                logger.warning(f"[Watcher] 监视目录数达到上限 {const.WATCH_MAX_PATHS}，"
                               f"余量交给周期全量扫描兜底")
                return
            if _watcher.addPath(dirpath):
                _watched.add(dirpath)


def _on_dir_changed(path):
    """去抖合并：同一目录 2s 内的连续变更只处理一次。"""
    from PyQt6.QtCore import QTimer
    old = _pending.pop(path, None)
    if old is not None:
        old.stop()
    t = QTimer()
    t.setSingleShot(True)
    t.timeout.connect(lambda p=path: _fire(p))
    _pending[path] = t
    t.start(const.WATCH_DEBOUNCE_MS)


def _fire(path):
    _pending.pop(path, None)
    try:
        import db
        import scanner
        db.invalidate_cache(path)
        if not scanner.status()['running']:
            scanner.scan_directory(path)
        # 变更可能是新建子目录：补注册
        add_tree(path)
    except Exception as e:
        logger.warning(f"[Watcher] 处理目录变更失败 {path}: {e}")


def watched_count():
    return len(_watched)
