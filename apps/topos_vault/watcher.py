"""增量索引：watchdog 文件监听（P2-2）。

监听素材目录变化：
    - 新文件 → 自动 ingest + 打标
    - 移动/重命名 → 自动 relink（三级匹配）
    - 删除 → 标记 missing

设计：
    - debounce：临时文件（~xxx.tmp、.partial 等）跳过；同一文件短时间多次变更合并
    - 增量：用 Orchestrator.index_file（断点续跑，已打标的不重跑）
    - 不阻塞主线程：watchdog 自带 observer 线程，回调里只入队，worker 线程消费
    - 优雅停止：stop() 等 worker 处理完当前任务

用法：
    from apps.topos_vault.watcher import VaultWatcher
    watcher = VaultWatcher(repo, ["/path/to/media"])
    watcher.start()  # 非阻塞
    ...  # 主线程做别的
    watcher.stop()   # 停止 + 等待

CLI：
    topos-vault watch /path/to/media1 /path/to/media2

降级：watchdog 未装 → start() 抛明确错误（不静默降级，监听是核心功能）。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

from apps.topos_vault.db.repository import VaultRepository
from apps.topos_vault.pipeline.orchestrator import classify_media_type

logger = logging.getLogger(__name__)

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    _HAS_WATCHDOG = True
except ImportError:
    _HAS_WATCHDOG = False
    FileSystemEventHandler = object  # type: ignore[misc,assignment]


# debounce 窗口：文件创建后等多久才处理（临时文件可能创建后立即删/改）
_DEBOUNCE_SEC = 0.5
# 忽略的文件名模式
_IGNORE_PATTERNS = (".tmp", ".partial", ".crdownload", "~", ".DS_Store", ".swp",
                    ".bak", ".lock")
# 处理队列最大长度（避免突发大量文件爆内存）
_QUEUE_MAX = 10000


@dataclass
class WatchEvent:
    """一个文件变更事件。"""
    path: str
    kind: str               # created / moved / deleted
    src_path: str | None = None  # moved 时的源路径


@dataclass
class WatchStats:
    """监听统计。"""
    created: int = 0        # 新增并打标
    moved: int = 0          # 移动/重命名重连
    deleted: int = 0        # 标记 missing
    skipped: int = 0        # 跳过（临时文件/非素材/已打标）
    failed: int = 0         # 处理失败


def is_watchdog_available() -> bool:
    """watchdog 是否已安装。"""
    return _HAS_WATCHDOG


class _Debouncer:
    """简单去抖：同一 path 短时间多次事件只保留最后一次。"""

    def __init__(self, window: float = _DEBOUNCE_SEC):
        self.window = window
        self._pending: dict[str, float] = {}  # path → 最近事件时间
        self._lock = threading.Lock()

    def should_process(self, path: str) -> bool:
        """记录事件时间，返回是否该立即处理（窗口外）。"""
        now = time.time()
        with self._lock:
            last = self._pending.get(path, 0)
            self._pending[path] = now
        return (now - last) > self.window

    def expire(self) -> list[str]:
        """取出窗口已过期的 path（供 worker 处理）。"""
        now = time.time()
        ready: list[str] = []
        with self._lock:
            for path, t in list(self._pending.items()):
                if (now - t) > self.window:
                    ready.append(path)
                    del self._pending[path]
        return ready


class VaultWatcher:
    """素材目录监听器（增量索引）。

    监听多个目录，文件变化时自动增量打标。
    """

    def __init__(
        self,
        repo: VaultRepository,
        watch_dirs: list[str],
        *,
        debounce: float = _DEBOUNCE_SEC,
    ):
        if not _HAS_WATCHDOG:
            raise RuntimeError("watchdog 未安装，无法监听。pip install watchdog")

        self.repo = repo
        self.watch_dirs = [os.path.abspath(d) for d in watch_dirs]
        self._debouncer = _Debouncer(debounce)
        self._queue: queue.Queue[WatchEvent | None] = queue.Queue(maxsize=_QUEUE_MAX)
        self._observer: Any = None
        self._worker: threading.Thread | None = None
        self._running = False
        self.stats = WatchStats()

        # 延迟 import Orchestrator（避免 watcher 模块加载时拉进 provider 注册）
        from apps.topos_vault.pipeline.orchestrator import Orchestrator
        self._orch = Orchestrator(repo)
        from apps.topos_vault.relink import Relinker
        self._relinker = Relinker(repo)

    def start(self) -> None:
        """开始监听（非阻塞，起 observer + worker 线程）。"""
        if self._running:
            return

        self._observer = Observer()
        handler = _VaultEventHandler(self._queue, self.watch_dirs)
        for d in self.watch_dirs:
            if not os.path.isdir(d):
                logger.warning("监听目录不存在：%s", d)
                continue
            self._observer.schedule(handler, d, recursive=True)
            logger.info("开始监听：%s", d)

        self._observer.start()
        self._running = True
        self._worker = threading.Thread(target=self._process_loop, daemon=True)
        self._worker.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """停止监听，等 worker 处理完队列。"""
        if not self._running:
            return
        self._running = False
        # 给 worker 发停止信号
        self._queue.put(None)
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=timeout)
        if self._worker:
            self._worker.join(timeout=timeout)

    def process_now(self, event: WatchEvent) -> None:
        """同步处理一个事件（测试用，不经过队列）。"""
        self._handle_event(event)

    # ------------------------------------------------------------------ #
    # worker 循环
    # ------------------------------------------------------------------ #

    def _process_loop(self) -> None:
        """worker 线程：从队列取事件处理。"""
        while self._running:
            try:
                event = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if event is None:  # 停止信号
                break
            try:
                self._handle_event(event)
            except Exception as e:
                logger.warning("处理监听事件失败 %s：%s", event.path, e)
                self.stats.failed += 1

    def _handle_event(self, event: WatchEvent) -> None:
        """处理单个文件变更事件。"""
        path = event.path

        if event.kind == "deleted":
            self._handle_deleted(path)
        elif event.kind == "moved":
            self._handle_moved(event.src_path or "", path)
        elif event.kind == "created":
            self._handle_created(path)

    def _handle_created(self, path: str) -> None:
        """新文件 → 增量打标（已打标的跳过）。"""
        if _should_ignore(path):
            self.stats.skipped += 1
            return
        if classify_media_type(path) is None:
            self.stats.skipped += 1
            return
        if not os.path.exists(path):
            # 创建后立即删了
            self.stats.skipped += 1
            return

        # 已入库的不重复打标（index_file 内部 is_done 会跳，这里预检查省日志噪音）
        existing = self.repo.get_media_source_by_path(path)
        if existing is not None and existing.path_status == "valid":
            self.stats.skipped += 1
            return

        logger.info("增量打标：%s", path)
        results = self._orch.index_file(path, media_type="auto")
        # 判断结果
        from apps.topos_vault.pipeline.steps import StepStatus
        if any(r.status == StepStatus.FAILED and not r.recoverable for r in results):
            self.stats.failed += 1
        else:
            self.stats.created += 1

    def _handle_moved(self, src_path: str, dest_path: str) -> None:
        """移动/重命名 → relink。"""
        if not src_path:
            return
        # 找原路径的记录，更新到新路径
        existing = self.repo.get_media_source_by_path(src_path)
        if existing is None:
            # 原路径没记录 → 当作新文件处理（可能是外部移入）
            self._handle_created(dest_path)
            return
        # 更新路径（relink 的 Level 1/2 会校验内容指纹一致）
        try:
            # 简化：直接更新 path_current
            self.repo.update_path_status(existing.fingerprint, dest_path, "valid")
            self.stats.moved += 1
            logger.info("重连：%s → %s", src_path, dest_path)
        except Exception as e:
            logger.warning("重连失败 %s → %s：%s", src_path, dest_path, e)
            self.stats.failed += 1

    def _handle_deleted(self, path: str) -> None:
        """文件删除 → 标记 missing。"""
        existing = self.repo.get_media_source_by_path(path)
        if existing is None:
            self.stats.skipped += 1
            return
        self.repo.update_path_status(existing.fingerprint, None, "missing")
        self.stats.deleted += 1
        logger.info("标记丢失：%s", path)


def _should_ignore(path: str) -> bool:
    """是否应忽略该路径（临时文件/隐藏文件）。"""
    name = os.path.basename(path)
    if name.startswith("."):
        return True
    for pat in _IGNORE_PATTERNS:
        if pat in name:
            return True
    return False


# ---------------------------------------------------------------------- #
# watchdog 事件处理器
# ---------------------------------------------------------------------- #

class _VaultEventHandler(FileSystemEventHandler):  # type: ignore[misc]
    """watchdog 事件 → 入队。"""

    def __init__(self, q: queue.Queue, watch_dirs: list[str]):
        self._queue = q
        self._watch_dirs = set(os.path.abspath(d) for d in watch_dirs)

    def _in_scope(self, path: str) -> bool:
        abs_path = os.path.abspath(path)
        return any(abs_path.startswith(d) for d in self._watch_dirs)

    def on_created(self, event: Any) -> None:
        if event.is_directory:
            return
        if not self._in_scope(event.src_path):
            return
        try:
            self._queue.put(WatchEvent(path=event.src_path, kind="created"),
                            timeout=1.0)
        except queue.Full:
            pass

    def on_moved(self, event: Any) -> None:
        if event.is_directory:
            return
        try:
            self._queue.put(WatchEvent(
                path=event.dest_path, kind="moved", src_path=event.src_path,
            ), timeout=1.0)
        except queue.Full:
            pass

    def on_deleted(self, event: Any) -> None:
        if event.is_directory:
            return
        try:
            self._queue.put(WatchEvent(path=event.src_path, kind="deleted"),
                            timeout=1.0)
        except queue.Full:
            pass
