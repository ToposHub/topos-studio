"""G05／BR04：Gallery 异步查询服务——分层签名索引、代际守卫、有界队列。

既有基础（计划 §5 G05“已有”栏）：扫描已避免 eager QPixmap 解码、
delegate 有 256 条 LRU（含负缓存）、搜索对现有条目重放不重扫。

本模块补齐查询侧的异步与索引层（BR04 复查升级）：

- ``GalleryQueryIndex``：以 ``(album_id, 分层文件签名)`` 为键的线程安全
  索引。签名是**逐条目 stat 多重集**（文件名/大小/mtime_ns）——目录
  mtime 检测不到的原地修改 meta／JPEG／EXR／grade 也会失效；未变时
  复用已解析条目，**搜索/排序/翻页全部走内存**。缓存发布前复核失效
  代际（扫描途中 invalidate 的结果不回填缓存）；扫描失败的结果不缓存
  成"完整空库"。
- ``AsyncGalleryQueryRunner``：单工作线程 + **同 album 合并保留最新**
  （pending 按 album_id 归并，队列上界＝不同 album 数）；``shutdown``
  丢弃未执行任务；完成回调先复核代际与是否已被更新请求取代——快速
  切 album／工程后**旧结果不回填**。回调在工作线程触发，GUI 线程
  编组由 app 层适配器负责（core 不持 Qt 对象）。
- 同步路径 ``GalleryQueryService.query`` 供面板当前扫描使用（索引
  命中即零磁盘），异步 runner 供宿主接入大库（G11 布局/外部变化同步
  与 G12 大库验收）。

实测基线（1000/10000 条、搜索 p95、滚动帧率、CPU/内存）属 G12 设备
验收，本模块提供可复现的计数器（``stats``）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Optional, Tuple

from src.features.color.core.gallery_contracts import (
    GalleryQuery,
    GalleryQueryResult,
)

logger = logging.getLogger(__name__)

IndexEntry = Tuple[str, GalleryQueryResult]  # (folder_signature, result)

#: 签名聚合忽略的目录条目（事务目录/回收站等隐藏项，与扫描口径一致）
_SIGNATURE_SKIP_PREFIXES = (".",)


class GalleryQueryIndex:
    """线程安全的 album 查询索引（分层签名失效；条目级降级）。"""

    def __init__(self, repository) -> None:
        self._repository = repository
        self._lock = threading.Lock()
        self._cache: Dict[str, IndexEntry] = {}
        # 失效代际：invalidate/手动刷新 bump；扫描途中失效的结果不回填
        self._invalidations = 0
        self._stats = {"index_hits": 0, "index_misses": 0,
                       "invalidations": 0, "publish_rejected": 0}

    # ── 失效 ───────────────────────────────────────────────────────────

    def invalidate(self, album_id: Optional[str] = None) -> None:
        """显式失效（内部变更后调用；bump 代际拒绝迟到的缓存回填）。"""
        with self._lock:
            self._invalidations += 1
            if album_id is None:
                count = len(self._cache)
                self._cache.clear()
            else:
                count = int(self._cache.pop(album_id, None) is not None)
            self._stats["invalidations"] += count

    # ── 查询 ───────────────────────────────────────────────────────────

    def query(self, query: GalleryQuery, *,
              probe_grades: bool = False) -> GalleryQueryResult:
        """带索引的查询：分层签名未变即复用（搜索/排序内存完成）。

        缓存保存**未过滤全量**扫描结果（中性查询：空搜索／名称升序／
        无 limit）；本次查询参数（search/sort/limit）在内存重放——不同
        参数复用同一份磁盘事实，不重扫。
        """
        album_id = query.album_id
        folder = self._repository.album_folder(album_id)
        signature = self.folder_signature(folder)
        with self._lock:
            cached = self._cache.get(album_id)
        if cached is not None and cached[0] == signature and not probe_grades:
            with self._lock:
                self._stats["index_hits"] += 1
            base = cached[1]
        else:
            with self._lock:
                self._stats["index_misses"] += 1
                seen_invalidations = self._invalidations
            neutral = GalleryQuery(
                album_id=album_id,
                request_revision=query.request_revision)
            base = self._repository.query(neutral,
                                          probe_grades=probe_grades)
            with self._lock:
                if self._invalidations != seen_invalidations:
                    # 扫描途中发生失效：本结果作废，不回填缓存
                    self._stats["publish_rejected"] += 1
                elif base.scan_error is not None:
                    # 失败的扫描不缓存成"完整空库"（下次重试）
                    self._stats["publish_rejected"] += 1
                else:
                    self._cache[album_id] = (signature, base)
        stills = self._replay(base.stills, query)
        total_matched = len(stills)
        if query.limit is not None:
            stills = stills[:query.limit]
        return GalleryQueryResult(
            album=base.album, stills=stills,
            request_revision=query.request_revision,
            total_matched=total_matched,
            degraded_count=sum(1 for s in stills if not s.has_meta),
            scan_error=base.scan_error)

    def _replay(self, stills, query: GalleryQuery):
        """在内存重放 search/sort/limit（与 repository 口径一致）。"""
        from src.features.color.core.preset_meta import matches_query
        ordered = [s for s in stills
                   if matches_query(s.display_name, s.meta,
                                    query.search_text)]
        self._repository._sort_stills(ordered, query)
        return tuple(ordered)

    @staticmethod
    def folder_signature(folder: Optional[str]) -> str:
        """分层文件签名：逐条目 ``stat`` 多重集哈希（BR04）。

        目录 mtime 只反映条目增删/改名；原地修改 meta／JPEG／EXR／
        grade 不改目录 mtime 却改变文件 stat——签名覆盖这两层。隐藏
        条目（事务目录等）与扫描口径一致地忽略。
        """
        if folder is None:
            return "<missing>"
        try:
            entries = os.scandir(folder)
        except OSError:
            return "<missing>"
        parts = []
        with entries:
            for entry in entries:
                name = entry.name
                if name.startswith(_SIGNATURE_SKIP_PREFIXES):
                    continue
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    parts.append(f"{name}:gone")
                    continue
                parts.append(f"{name}:{stat.st_size}:{stat.st_mtime_ns}")
        digest = hashlib.sha1("\n".join(sorted(parts)).encode(
            "utf-8", "replace")).hexdigest()
        return f"{len(parts)}:{digest}"

    @property
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)


class AsyncGalleryQueryRunner:
    """后台单线程扫描 runner（代际守卫＋同 album 合并保留最新）。

    BR04：pending 按 ``album_id`` 归并——快速切 100 次相册/搜索时队列
    上界＝不同 album 数，同 album 旧请求被最新取代（不再逐条排队）；
    ``shutdown`` 后未执行任务直接丢弃。

    SR05（第二轮复查）：**代际归属随请求**——pending 项携带
    ``(generation, query)``，工作项消费的是**请求自己的代际**，不再沿用
    排队时刻旧工作项的代际（复现：旧任务排队→切代际→新请求并入
    pending→旧工作项读到新请求却带旧代际→最新结果被当 stale 丢弃）。
    submit／shutdown 在同一把锁内完成调度（executor 提交不再落到锁外，
    与 shutdown 竞态消失）；pending 超过上限时按 FIFO 丢弃最旧相册
    （``dropped_overflow``，该相册下次扫描会重新提交）。
    """

    #: pending 相册上限（不同 album 数的硬界；超出丢最旧）
    MAX_PENDING_ALBUMS = 128

    def __init__(self, index: GalleryQueryIndex, *,
                 on_result: Optional[Callable[
                     [GalleryQuery, GalleryQueryResult], None]] = None
                 ) -> None:
        self._index = index
        self._on_result = on_result
        self._lock = threading.Lock()
        self._generation = 0
        self._shutdown = False
        # album_id → (generation, query)：代际属于**这条请求**
        self._pending: Dict[str, Tuple[int, GalleryQuery]] = {}
        self._scheduled: set = set()
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="gallery-query")
        self._stats = {"submitted": 0, "delivered": 0,
                       "dropped_stale": 0, "dropped_shutdown": 0,
                       "dropped_superseded": 0, "dropped_overflow": 0}

    # ── 代际 ───────────────────────────────────────────────────────────

    def begin_generation(self) -> int:
        """上下文切换（album/工程切换）：在飞与排队结果作废。"""
        with self._lock:
            self._generation += 1
            self._pending.clear()
            return self._generation

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._pending.clear()
            self._executor.shutdown(wait=False)   # 原子：锁内完成调度

    @property
    def is_shutdown(self) -> bool:
        with self._lock:
            return self._shutdown

    # ── 提交 ───────────────────────────────────────────────────────────

    def submit(self, query: GalleryQuery) -> bool:
        """提交后台查询（同 album 合并保留最新）；回调携带代际复核。"""
        with self._lock:
            if self._shutdown:
                return False
            generation = self._generation
            superseded = self._pending.pop(query.album_id, None)
            if superseded is not None:
                self._stats["dropped_superseded"] += 1
            self._pending[query.album_id] = (generation, query)
            while len(self._pending) > self.MAX_PENDING_ALBUMS:
                oldest = next(iter(self._pending))
                del self._pending[oldest]
                self._stats["dropped_overflow"] += 1
            self._stats["submitted"] += 1
            if query.album_id in self._scheduled:
                return True          # 已有同 album 任务在途：合并进最新
            self._scheduled.add(query.album_id)
            try:
                self._executor.submit(self._run, query.album_id)
            except RuntimeError:
                # executor 在锁内 shutdown 后的提交竞态：任务视作丢弃
                self._scheduled.discard(query.album_id)
                return False
            return True

    def _run(self, album_id: str) -> None:
        with self._lock:
            pending = self._pending.pop(album_id, None)
            self._scheduled.discard(album_id)
            if self._shutdown:
                return
            if pending is None:
                # begin_generation／溢出把请求在执行前丢弃：如实计数
                self._stats["dropped_stale"] += 1
                return
            generation, query = pending      # 代际属于这条请求（SR05）
            if generation != self._generation:
                self._stats["dropped_stale"] += 1
                return     # 排队期间上下文已切走：执行前取消
        try:
            result = self._index.query(query)
        except Exception:
            logger.exception("gallery async query failed for %s", album_id)
            return
        with self._lock:
            if self._shutdown:
                self._stats["dropped_shutdown"] += 1
                return
            if generation != self._generation:
                self._stats["dropped_stale"] += 1
                return     # 上下文已切走：旧结果不回填
            if album_id in self._pending:
                # 扫描期间又提交了该 album 的新请求：本结果过时
                self._stats["dropped_superseded"] += 1
                return
            self._stats["delivered"] += 1
            callback = self._on_result
        if callback is not None:
            callback(query, result)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)


class GalleryQueryService:
    """面板/宿主共用入口：同步查询（索引）＋可选后台 runner。"""

    def __init__(self, repository) -> None:
        self.index = GalleryQueryIndex(repository)
        self.runner: Optional[AsyncGalleryQueryRunner] = None

    def query(self, query: GalleryQuery, *,
              probe_grades: bool = False) -> GalleryQueryResult:
        return self.index.query(query, probe_grades=probe_grades)

    def invalidate(self, album_id: Optional[str] = None) -> None:
        """显式失效（内部变更后调用；手动刷新走这里强制重建）。"""
        self.index.invalidate(album_id)
        if self.runner is not None:
            self.runner.begin_generation()

    def attach_runner(self, on_result: Callable[
            [GalleryQuery, GalleryQueryResult], None]
            ) -> AsyncGalleryQueryRunner:
        """启用后台扫描（回调在工作线程；GUI 编组由调用方负责）。"""
        self.runner = AsyncGalleryQueryRunner(self.index,
                                              on_result=on_result)
        return self.runner

    def shutdown(self) -> None:
        if self.runner is not None:
            self.runner.shutdown()

    @property
    def stats(self) -> Dict[str, int]:
        data = dict(self.index.stats)
        if self.runner is not None:
            for key, value in self.runner.stats.items():
                data[f"runner_{key}"] = value
        return data
