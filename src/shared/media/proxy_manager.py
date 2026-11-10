"""
Proxy Manager Module
代理管理器，管理代理文件的完整生命周期。

## 职责（P10.3）

- ProxyManager: 代理生命周期管理
- ProxyTask: 代理生成任务
- ProxyStats: 代理统计信息

## 设计原则

- 管理多个代理生成任务
- 自动验证代理有效性（mtime/size 检查）
- 支持批量生成
- 后台线程执行，不阻塞 UI
- 完整类型注解

## 使用示例

```python
from src.shared.media.proxy_manager import ProxyManager
from src.shared.media.proxy import DiskProxyConfig, ProxyFormat

# 创建管理器
manager = ProxyManager(DiskProxyConfig())

# 提交生成任务
manager.queue_proxy_generation(
    asset_id='asset-001',
    source_path='/path/to/video.mp4',
    on_complete=lambda result: print(f"Done: {result.proxy_path}"),
    on_error=lambda err: print(f"Error: {err}"),
)

# 获取代理路径（如果有效）
proxy_path = manager.get_valid_proxy_path('/path/to/video.mp4')

# 清理过期代理
manager.cleanup_expired_proxies()
```

## 架构位置

```text
MediaPool Panel
    └── ProxyManager
            ├── ProxyGenerator (per task)
            ├── Proxy Cache (validation)
            └── Task Queue (background threads)
```
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Dict, List, Any

from .proxy import (
    DiskProxyConfig,
    get_source_file_info,
)
from .proxy_generator import (
    ProxyGenerator,
    ProxyGenerationResult,
    ProxyProgress,
)

logger = logging.getLogger(__name__)


# =============================================================================
# 数据类
# =============================================================================

@dataclass
class ProxyTask:
    """
    代理生成任务。

    回调为列表（AUD-35610）：同源重复入队时后来调用方的回调被追加
    到既有任务，任务结束时逐个调用，不再静默丢弃。

    Attributes:
        task_id: 任务唯一标识
        asset_id: 关联的素材 ID
        source_path: 源文件路径
        status: 任务状态（queued, generating, complete, failed, cancelled）
        generator: 生成器实例
        result: 生成结果
        progress: 当前进度
        created_at: 创建时间
        started_at: 开始时间
        completed_at: 完成时间
        on_complete: 完成回调列表
        on_error: 错误回调列表
        on_progress: 进度回调列表
    """
    task_id: str
    asset_id: str
    source_path: str
    status: str = "queued"
    generator: Optional[ProxyGenerator] = None
    result: Optional[ProxyGenerationResult] = None
    progress: ProxyProgress = field(default_factory=ProxyProgress)
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    on_complete: List[Callable[[ProxyGenerationResult], None]] = field(default_factory=list)
    on_error: List[Callable[[str], None]] = field(default_factory=list)
    on_progress: List[Callable[[ProxyProgress], None]] = field(default_factory=list)


@dataclass
class ProxyStats:
    """
    代理统计信息。

    Attributes:
        total_assets: 总素材数
        assets_with_proxy: 有代理的素材数
        pending_tasks: 待处理任务数
        generating_tasks: 生成中任务数
        failed_tasks: 失败任务数
        total_proxy_size_bytes: 代理文件总大小
        cache_hit_rate: 缓存命中率
    """
    total_assets: int = 0
    assets_with_proxy: int = 0
    pending_tasks: int = 0
    generating_tasks: int = 0
    failed_tasks: int = 0
    total_proxy_size_bytes: int = 0
    cache_hit_rate: float = 0.0


# =============================================================================
# ProxyManager
# =============================================================================

class ProxyManager:
    """
    代理管理器。

    管理代理文件的完整生命周期：
    - 队列管理：支持多个并发生成任务
    - 有效性验证：检查源文件是否修改
    - 自动清理：删除过期代理
    - 统计信息：代理覆盖率、缓存命中率

    Attributes:
        config: 代理配置
        max_concurrent: 最大并发任务数
        _tasks: 任务字典 {task_id: ProxyTask}
        _source_to_task: 源文件到任务的映射
        _lock: 线程锁

    Examples:
        >>> manager = ProxyManager(DiskProxyConfig())
        >>> manager.is_proxy_valid('/path/to/video.mp4')
        False
    """

    def __init__(
        self,
        config: DiskProxyConfig,
        max_concurrent: int = 2,
    ) -> None:
        """
        初始化代理管理器。

        Args:
            config: 磁盘代理配置
            max_concurrent: 最大并发编码任务数（AUD-35607：以信号量
                实施限流——此前每任务即时起独立线程，该值从未被读取，
                批量导入即 N 路并行编码抢占 CPU；<=0 按 1 处理）
        """
        self.config = config
        self.max_concurrent = max(1, int(max_concurrent))
        self._tasks: Dict[str, ProxyTask] = {}
        self._source_to_task: Dict[str, str] = {}  # source_path -> task_id
        self._lock = threading.RLock()
        self._proxy_cache: Dict[str, str] = {}  # source_path -> proxy_path
        self._cache_version: Dict[str, tuple[float, int]] = {}  # source_path -> (mtime, size)
        self._stats = ProxyStats()
        # AUD-35607：并发编码槽位。_run_task 在真正开始编码前获取，
        # 完成后释放；排队任务在等待槽位期间保持 "queued" 且可取消。
        self._concurrency_slots = threading.Semaphore(self.max_concurrent)
        # 前台交互任务（场景剪切探测等）要求代理生成让出 CPU 时置位，
        # 排队任务在 _run_task 中等待此标志清零后才启动。
        self._generation_deferred: bool = False
        # AUD-35611：终态任务保留额度（超过后按 completed_at 淘汰最旧）。
        # 保留少量终态任务使 get_task_status 在完成后仍可短暂查询。
        self._terminal_task_retention: int = 32

    def queue_proxy_generation(
        self,
        asset_id: str,
        source_path: str,
        on_complete: Optional[Callable[[ProxyGenerationResult], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[ProxyProgress], None]] = None,
    ) -> Optional[str]:
        """
        队列代理生成任务。

        如果源文件已有有效代理，直接返回。否则创建新任务。

        Args:
            asset_id: 素材 ID
            source_path: 源文件路径
            on_complete: 完成回调
            on_error: 错误回调
            on_progress: 进度回调

        Returns:
            任务 ID，如果已有有效代理则返回 None
        """
        sync_callback: Optional[Callable[[], None]] = None
        return_id: Optional[str] = None
        with self._lock:
            # 检查是否已有有效代理
            if self.is_proxy_valid(source_path):
                existing_proxy = self._proxy_cache.get(source_path)
                if existing_proxy:
                    logger.info(f"Valid proxy already exists: {existing_proxy}")
                    if on_complete:
                        # AUD-35608：回调在管理器锁外调用——on_complete 链
                        # 会获取上层服务锁，锁内调用与其反序获取构成
                        # AB-BA 死锁条件。记录待发回调，出锁后执行。
                        result = ProxyGenerationResult(
                            success=True,
                            source_path=source_path,
                            proxy_path=existing_proxy,
                        )

                        def _fire(result: ProxyGenerationResult = result,
                                  cb: Callable[[ProxyGenerationResult], None] = on_complete) -> None:
                            cb(result)

                        sync_callback = _fire
            else:
                # 检查是否已有任务在处理此源文件
                if source_path in self._source_to_task:
                    existing_task_id = self._source_to_task[source_path]
                    existing_task = self._tasks.get(existing_task_id)
                    if existing_task and existing_task.status in ("queued", "generating"):
                        # AUD-35610：后来调用方的回调追加到既有任务，任务
                        # 结束时逐个调用——此前被静默丢弃，第二个调用方的
                        # UI 素材项永远收不到完成/失败通知。
                        if on_complete:
                            existing_task.on_complete.append(on_complete)
                        if on_error:
                            existing_task.on_error.append(on_error)
                        if on_progress:
                            existing_task.on_progress.append(on_progress)
                        logger.info(
                            f"Task already exists for {source_path}: "
                            f"{existing_task_id}（回调已合并，当前 "
                            f"{len(existing_task.on_complete)} 个完成回调）")
                        return_id = existing_task_id

                if return_id is None:
                    # 创建新任务
                    import uuid
                    task_id = f"proxy-{uuid.uuid4().hex[:8]}"
                    task = ProxyTask(
                        task_id=task_id,
                        asset_id=asset_id,
                        source_path=source_path,
                        status="queued",
                        generator=ProxyGenerator(self.config),
                        on_complete=[on_complete] if on_complete else [],
                        on_error=[on_error] if on_error else [],
                        on_progress=[on_progress] if on_progress else [],
                    )

                    self._tasks[task_id] = task
                    self._source_to_task[source_path] = task_id

                    # AUD-35611：入队时按保留额度淘汰最旧终态任务，
                    # 防止 _tasks 长会话无界增长（终态任务此前只从
                    # _source_to_task 移除，_tasks 中的 ProxyTask 永驻）。
                    self._prune_terminal_tasks_locked()

                    # 启动后台线程（并发上限由 _run_task 中的信号量实施，
                    # 此处只负责排队）
                    thread = threading.Thread(
                        target=self._run_task,
                        args=(task,),
                        daemon=True,
                        name=f"ProxyGen-{task_id}",
                    )
                    thread.start()

                    logger.info(f"Queued proxy generation task: {task_id} for {source_path}")
                    return_id = task_id

        if sync_callback is not None:
            sync_callback()
        return return_id

    def _prune_terminal_tasks_locked(self) -> int:
        """
        清理超出保留额度的终态任务（AUD-35611）。

        必须持有 self._lock 调用。按 completed_at 从旧到新淘汰
        complete/failed/cancelled 任务，使 _tasks 中终态任务数不超过
        _terminal_task_retention；活跃任务（queued/generating）不受影响。

        Returns:
            淘汰的任务数量
        """
        terminal: list[tuple[float, str]] = [
            (task.completed_at if task.completed_at is not None else 0.0, task_id)
            for task_id, task in self._tasks.items()
            if task.status in ("complete", "failed", "cancelled")
        ]
        overflow = len(terminal) - self._terminal_task_retention
        if overflow <= 0:
            return 0

        terminal.sort()
        pruned = 0
        for _, task_id in terminal[:overflow]:
            task = self._tasks.get(task_id)
            if task is not None:
                # 防御：仅当映射仍指向本任务时才清理
                if self._source_to_task.get(task.source_path) == task_id:
                    self._source_to_task.pop(task.source_path, None)
                self._tasks.pop(task_id, None)
                pruned += 1
        if pruned:
            logger.debug(f"Pruned {pruned} terminal proxy task(s)")
        return pruned

    def pause_all_generation(self) -> int:
        """
        挂起所有生成中的代理任务，并让排队任务延迟启动。

        通过 SIGSTOP 暂停运行中的 ffmpeg 进程（保留已编码进度，不丢弃
        工作），用于前台交互任务（如场景剪切探测）运行期间让出 CPU；
        配合 resume_all_generation() 恢复。

        Returns:
            被挂起的运行中任务数量
        """
        paused = 0
        with self._lock:
            self._generation_deferred = True
            for task in self._tasks.values():
                if task.status == "generating" and task.generator is not None:
                    if task.generator.pause():
                        paused += 1
        logger.info(f"Deferred proxy generation: {paused} running task(s) paused")
        return paused

    def resume_all_generation(self) -> None:
        """恢复被 pause_all_generation() 挂起/延迟的代理生成。"""
        resumed = 0
        with self._lock:
            self._generation_deferred = False
            for task in self._tasks.values():
                if task.status == "generating" and task.generator is not None:
                    if task.generator.resume():
                        resumed += 1
        logger.info(f"Resumed proxy generation: {resumed} task(s)")

    def cancel_task(self, task_id: str) -> bool:
        """
        取消代理生成任务。

        Args:
            task_id: 任务 ID

        Returns:
            是否成功取消
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False

            if task.status in ("complete", "failed", "cancelled"):
                return False

            task.status = "cancelled"
            if task.generator:
                task.generator.cancel()

            # 清理映射
            self._source_to_task.pop(task.source_path, None)

            logger.info(f"Cancelled task: {task_id}")
            return True

    def cancel_all_tasks(self) -> int:
        """
        取消所有任务。

        Returns:
            取消的任务数
        """
        cancelled = 0
        with self._lock:
            for task_id, task in self._tasks.items():
                if task.status in ("queued", "generating"):
                    self.cancel_task(task_id)
                    cancelled += 1
        return cancelled

    def retry_failed(self, source_path: str) -> Optional[str]:
        """P0-7：失败任务的显式重试（不自动重试，对齐 Resolve 手动 Retry）。

        把该源最近一次失败任务的回调（含 AUD-35610 合并进来的多方回调）
        原样带入一次全新入队；失败任务已被保留额度淘汰或无失败记录时
        返回 None——调用方可直接 queue_proxy_generation 重新排队。

        Args:
            source_path: 源文件路径

        Returns:
            新任务 ID；无失败记录可重试时 None
        """
        with self._lock:
            task_id = self._source_to_task.get(source_path)
            task = self._tasks.get(task_id) if task_id else None
            if task is None or task.status != "failed":
                # 终态任务到达时即从 _source_to_task 摘除（AUD-35611 保留
                # 期内仍留在 _tasks）——按源直接检索失败任务，取最近完成者。
                candidates = [
                    t for t in self._tasks.values()
                    if t.source_path == source_path and t.status == "failed"
                ]
                task = (max(candidates, key=lambda t: t.completed_at or 0.0)
                        if candidates else None)
            if task is None:
                return None
            task_id = task.task_id
            asset_id = task.asset_id
            completes = [cb for cb in task.on_complete if callable(cb)]
            errors = [cb for cb in task.on_error if callable(cb)]
            progresses = [cb for cb in task.on_progress if callable(cb)]
            # 清掉旧记录，避免同源占用 _source_to_task 槽位
            self._source_to_task.pop(source_path, None)
            self._tasks.pop(task_id, None)

        def _dispatch(cbs: list) -> Optional[Callable]:
            if not cbs:
                return None
            return lambda *args: [cb(*args) for cb in cbs]

        return self.queue_proxy_generation(
            asset_id=asset_id,
            source_path=source_path,
            on_complete=_dispatch(completes),
            on_error=_dispatch(errors),
            on_progress=_dispatch(progresses),
        )

    def failed_sources(self) -> list[str]:
        """当前仍在任务表中的失败任务源列表（供重试 UI 枚举）。"""
        with self._lock:
            return [
                task.source_path for task in self._tasks.values()
                if task.status == "failed"
            ]

    def is_proxy_valid(self, source_path: str) -> bool:
        """
        检查源文件的代理是否有效。

        验证条件：
        1. 代理文件存在
        2. 源文件未修改（mtime 和 size 匹配）

        Args:
            source_path: 源文件路径

        Returns:
            代理是否有效
        """
        # AUD-35613：锁内只做字典快照，文件系统 stat（is_proxy_valid 原先
        # 在 self._lock 内 isfile/getsize 源文件）全部移到锁外执行，避免
        # 慢速盘/网络卷上的 UI 状态查询阻塞其他线程。失效写回时校验映射
        # 未被并发修改，避免误删新写入的有效条目。
        with self._lock:
            proxy_path = self._proxy_cache.get(source_path)
            if proxy_path is None:
                return False
            cached_version = self._cache_version.get(source_path)

        # 锁外：代理文件存在性检查
        if not os.path.isfile(proxy_path):
            with self._lock:
                if self._proxy_cache.get(source_path) == proxy_path:
                    self._invalidate_proxy_cache(source_path)
            return False

        # 锁外：源文件 mtime/size 校验
        if cached_version is not None:
            cached_mtime, cached_size = cached_version
            try:
                current_mtime, current_size = get_source_file_info(source_path)
                if current_mtime != cached_mtime or current_size != cached_size:
                    # 源文件已修改，代理失效
                    with self._lock:
                        if self._proxy_cache.get(source_path) == proxy_path:
                            self._invalidate_proxy_cache(source_path)
                    return False
            except (OSError, FileNotFoundError):
                return False

        return True

    def get_valid_proxy_path(self, source_path: str) -> Optional[str]:
        """
        获取有效的代理文件路径。

        Args:
            source_path: 源文件路径

        Returns:
            代理文件路径，如果无效则返回 None
        """
        # AUD-35612：校验与取值必须持同一把锁——原实现 is_proxy_valid
        # 释放锁后再读 _proxy_cache，中间另一线程可 invalidate/pop，
        # 返回已失效路径或与其他方法的加锁口径不一致（RLock 可重入，
        # is_proxy_valid 内部再加锁安全）。
        with self._lock:
            if self.is_proxy_valid(source_path):
                return self._proxy_cache.get(source_path)
            return None

    def get_task_status(self, task_id: str) -> Optional[str]:
        """
        获取任务状态。

        Args:
            task_id: 任务 ID

        Returns:
            任务状态字符串
        """
        with self._lock:
            task = self._tasks.get(task_id)
            return task.status if task else None

    def get_task_progress(self, task_id: str) -> Optional[ProxyProgress]:
        """
        获取任务进度。

        Args:
            task_id: 任务 ID

        Returns:
            任务进度
        """
        with self._lock:
            task = self._tasks.get(task_id)
            return task.progress if task else None

    def get_stats(self) -> ProxyStats:
        """
        获取代理统计信息。

        Returns:
            代理统计信息
        """
        with self._lock:
            stats = ProxyStats()

            # 计算任务统计
            for task in self._tasks.values():
                if task.status == "queued":
                    stats.pending_tasks += 1
                elif task.status == "generating":
                    stats.generating_tasks += 1
                elif task.status == "failed":
                    stats.failed_tasks += 1

            # AUD-35613：锁内只拷贝路径快照，isfile/getsize 在锁外执行
            proxy_paths = list(self._proxy_cache.values())

        # 锁外：代理文件总大小统计（慢速盘不阻塞其他查询）
        for proxy_path in proxy_paths:
            try:
                if os.path.isfile(proxy_path):
                    stats.total_proxy_size_bytes += os.path.getsize(proxy_path)
                    stats.assets_with_proxy += 1
            except OSError as exc:
                logger.debug("proxy stat failed for %s: %s", proxy_path, exc)

        with self._lock:
            self._stats = stats
            return stats

    def cleanup_expired_proxies(self) -> int:
        """
        清理过期代理文件。

        删除源文件已修改或源文件不存在的代理。

        Returns:
            清理的文件数
        """
        cleaned = 0
        # AUD-35613：锁内只做字典快照与失效写回，isfile/getsize/os.remove
        # 全部移到锁外——cleanup 批量删除期间原先所有查询停摆。
        with self._lock:
            cache_snapshot: List[tuple[str, str]] = list(self._proxy_cache.items())
            version_snapshot = dict(self._cache_version)

        expired_sources: List[str] = []
        for source_path, _proxy_path in cache_snapshot:
            # 锁外：检查源文件是否存在
            if not os.path.isfile(source_path):
                expired_sources.append(source_path)
                continue

            # 锁外：检查源文件是否修改
            try:
                if source_path in version_snapshot:
                    cached_mtime, cached_size = version_snapshot[source_path]
                    current_mtime, current_size = get_source_file_info(source_path)
                    if current_mtime != cached_mtime or current_size != cached_size:
                        expired_sources.append(source_path)
            except OSError:
                expired_sources.append(source_path)

        # 锁内：失效缓存（先取当前代理路径，锁外删除文件）
        removals: List[tuple[str, str]] = []
        with self._lock:
            for source_path in expired_sources:
                proxy_path = self._proxy_cache.get(source_path)
                if proxy_path:
                    removals.append((source_path, proxy_path))
                self._invalidate_proxy_cache(source_path)

        # 锁外：删除过期代理文件
        for _source_path, proxy_path in removals:
            if os.path.isfile(proxy_path):
                try:
                    os.remove(proxy_path)
                    logger.info(f"Cleaned up expired proxy: {proxy_path}")
                    cleaned += 1
                except OSError as e:
                    logger.warning(f"Failed to remove proxy {proxy_path}: {e}")

        return cleaned

    def cleanup_all_proxies(self) -> int:
        """
        清理所有代理文件。

        Returns:
            清理的文件数
        """
        cleaned = 0
        # AUD-35613：锁内只拷贝路径快照并清空映射，isfile/os.remove 锁外执行
        with self._lock:
            paths_to_remove = list(self._proxy_cache.values())
            self._proxy_cache.clear()
            self._cache_version.clear()

        for proxy_path in paths_to_remove:
            if os.path.isfile(proxy_path):
                try:
                    os.remove(proxy_path)
                    cleaned += 1
                except OSError as e:
                    logger.warning(f"Failed to remove proxy {proxy_path}: {e}")

        return cleaned

    def update_proxy_for_asset(
        self,
        asset: Any,
        proxy_path: str,
    ) -> Any:
        """
        更新素材的代理信息。

        Args:
            asset: 素材对象
            proxy_path: 代理文件路径

        Returns:
            更新后的素材对象
        """
        with self._lock:
            # 更新缓存
            self._proxy_cache[asset.path] = proxy_path
            try:
                mtime, size = get_source_file_info(asset.path)
                self._cache_version[asset.path] = (mtime, size)
            except OSError:
                pass

            # 返回更新后的素材
            return asset.with_proxy_path(proxy_path).with_proxy_status("ready")

    def remove_proxy_for_asset(
        self,
        asset: Any,
    ) -> Any:
        """
        删除素材的代理。

        Args:
            asset: 素材对象

        Returns:
            更新后的素材对象
        """
        with self._lock:
            proxy_path = self._proxy_cache.get(asset.path)
            if proxy_path and os.path.isfile(proxy_path):
                try:
                    os.remove(proxy_path)
                    logger.info(f"Removed proxy for asset {asset.asset_id}: {proxy_path}")
                except OSError as e:
                    logger.warning(f"Failed to remove proxy: {e}")

            self._invalidate_proxy_cache(asset.path)
            return asset.with_proxy_path(None).with_proxy_status("none")

    def _run_task(self, task: ProxyTask) -> None:
        """
        运行代理生成任务（后台线程）。

        Args:
            task: 任务对象
        """
        # 等待启动条件：前台任务要求让出 CPU（_generation_deferred）
        # 期间排队等待；并发槽位（AUD-35607：max_concurrent 限流，
        # 此前每任务即时起线程，并发编码数无上限）满时排队等待。
        # 0.2s 粒度轮询保证取消仍然即时生效。
        while True:
            with self._lock:
                if task.status == "cancelled":
                    return
                deferred = self._generation_deferred
            if not deferred and self._concurrency_slots.acquire(timeout=0.2):
                break
            time.sleep(0.2)

        try:
            with self._lock:
                if task.status == "cancelled":
                    return
                task.status = "generating"
                task.started_at = time.time()

            def progress_wrapper(progress: ProxyProgress) -> None:
                """进度回调包装器（回调在锁外调用，见 AUD-35608）"""
                with self._lock:
                    task.progress = progress
                for cb in task.on_progress:
                    cb(progress)

            # 执行生成
            assert task.generator is not None
            result = task.generator.generate(
                source_path=task.source_path,
                progress_callback=progress_wrapper,
            )

            # 更新任务状态
            with self._lock:
                task.result = result
                task.completed_at = time.time()

                if result.success:
                    task.status = "complete"
                    # 更新缓存
                    self._proxy_cache[task.source_path] = result.proxy_path
                    try:
                        mtime, size = get_source_file_info(task.source_path)
                        self._cache_version[task.source_path] = (mtime, size)
                    except OSError:
                        pass
                elif result.cancelled:
                    task.status = "cancelled"
                else:
                    task.status = "failed"

                # 清理映射
                self._source_to_task.pop(task.source_path, None)

            # AUD-35608：on_complete/on_error 在管理器锁外调用——
            # 回调链（DiskProxyService._on_generation_complete 等）会获取
            # 上层服务锁并继续分发 UI 状态，锁内调用与"服务侧持服务锁
            # 查询 manager"的路径构成 AB-BA 死锁；也避免持锁执行重活。
            # AUD-35610：回调是列表，同源重复入队的所有调用方逐个通知。
            if result.success:
                for cb in task.on_complete:
                    cb(result)
            elif not result.cancelled:
                for cb in task.on_error:
                    cb(result.error_message)

            logger.info(
                f"Task {task.task_id} completed: status={task.status}, "
                f"success={result.success}"
            )
        finally:
            # AUD-35607：无论成功/失败/取消都释放并发槽位
            self._concurrency_slots.release()

    def _invalidate_proxy_cache(self, source_path: str) -> None:
        """
        使代理缓存失效。

        Args:
            source_path: 源文件路径
        """
        self._proxy_cache.pop(source_path, None)
        self._cache_version.pop(source_path, None)

    def get_pending_tasks(self) -> List[ProxyTask]:
        """
        获取所有待处理/生成中的任务。

        Returns:
            任务列表
        """
        with self._lock:
            return [
                task for task in self._tasks.values()
                if task.status in ("queued", "generating")
            ]

    def get_task_by_asset_id(self, asset_id: str) -> Optional[ProxyTask]:
        """
        根据素材 ID 获取任务。

        Args:
            asset_id: 素材 ID

        Returns:
            任务对象，如果不存在则返回 None
        """
        with self._lock:
            for task in self._tasks.values():
                if task.asset_id == asset_id:
                    return task
            return None

    def get_task_by_source_path(self, source_path: str) -> Optional[ProxyTask]:
        """
        根据源文件路径获取活动任务。

        Args:
            source_path: 源文件路径

        Returns:
            任务对象（queued/generating），如果不存在则返回 None
        """
        with self._lock:
            task_id = self._source_to_task.get(source_path)
            if task_id is None:
                return None
            task = self._tasks.get(task_id)
            if task is None or task.status in ("complete", "failed", "cancelled"):
                return None
            return task
