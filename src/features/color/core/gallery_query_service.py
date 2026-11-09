"""G05：Gallery 异步查询服务——代际守卫、mtime 索引复用、后台扫描。

既有基础（计划 §5 G05“已有”栏）：扫描已避免 eager QPixmap 解码、
delegate 有 256 条 LRU（含负缓存）、搜索对现有条目重放不重扫。

本模块补齐查询侧的异步与索引层：

- ``GalleryQueryIndex``：以 ``(album_id, folder_mtime_ns)`` 为键的线程
  安全索引——目录未变（rename/delete/capture 都会改目录 mtime）时
  复用已解析条目，**搜索/排序/翻页全部走内存**，不重扫磁盘；单条
  bundle 的 meta 解析失败只降级该条（degraded），不拖垮整库。
- ``AsyncGalleryQueryRunner``：单工作线程后台扫描；每次提交携带
  ``generation``，完成回调先复核代际——快速切 album／工程后**旧结果
  不回填**；``shutdown()`` 后不再接收/派发。回调在工作线程触发，
  GUI 线程编组由 app 层适配器负责（core 不持 Qt 对象）。
- 同步路径 ``GalleryQueryService.query`` 供面板当前扫描使用（索引
  命中即零磁盘），异步 runner 供后续宿主接入（G11 布局/外部变化同步
  与 G12 大库验收）。

实测基线（1000/10000 条、搜索 p95、滚动帧率、CPU/内存）属 G12 设备
验收，本模块提供可复现的计数器（``stats``）。
"""

from __future__ import annotations

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

IndexEntry = Tuple[int, GalleryQueryResult]  # (folder_mtime_ns, result)


class GalleryQueryIndex:
    """线程安全的 album 查询索引（mtime 失效；条目级降级）。"""

    def __init__(self, repository) -> None:
        self._repository = repository
        self._lock = threading.Lock()
        self._cache: Dict[str, IndexEntry] = {}
        self._stats = {"index_hits": 0, "index_misses": 0,
                       "invalidations": 0}

    # ── 失效 ───────────────────────────────────────────────────────────

    def invalidate(self, album_id: Optional[str] = None) -> None:
        with self._lock:
            if album_id is None:
                count = len(self._cache)
                self._cache.clear()
            else:
                count = int(self._cache.pop(album_id, None) is not None)
            self._stats["invalidations"] += count

    # ── 查询 ───────────────────────────────────────────────────────────

    def query(self, query: GalleryQuery, *,
              probe_grades: bool = False) -> GalleryQueryResult:
        """带索引的查询：目录 mtime 未变即复用（搜索/排序内存完成）。

        缓存保存**未过滤全量**扫描结果（中性查询：空搜索／名称升序／
        无 limit）；本次查询参数（search/sort/limit）在内存重放——不同
        参数复用同一份磁盘事实，不重扫。
        """
        album_id = query.album_id
        folder = self._repository.album_folder(album_id)
        mtime = self._folder_mtime(folder)
        with self._lock:
            cached = self._cache.get(album_id)
        if cached is not None and cached[0] == mtime and not probe_grades:
            with self._lock:
                self._stats["index_hits"] += 1
            base = cached[1]
        else:
            with self._lock:
                self._stats["index_misses"] += 1
            neutral = GalleryQuery(
                album_id=album_id,
                request_revision=query.request_revision)
            base = self._repository.query(neutral,
                                          probe_grades=probe_grades)
            with self._lock:
                self._cache[album_id] = (mtime, base)
        stills = self._replay(base.stills, query)
        return GalleryQueryResult(
            album=base.album, stills=stills,
            request_revision=query.request_revision,
            total_matched=len(stills),
            degraded_count=sum(1 for s in stills if not s.has_meta),
            scan_error=base.scan_error)

    def _replay(self, stills, query: GalleryQuery):
        """在内存重放 search/sort/limit（与 repository 口径一致）。"""
        from src.features.color.core.preset_meta import matches_query
        ordered = [s for s in stills
                   if matches_query(s.display_name, s.meta,
                                    query.search_text)]
        self._repository._sort_stills(ordered, query)
        if query.limit is not None:
            ordered = ordered[:query.limit]
        return tuple(ordered)

    @staticmethod
    def _folder_mtime(folder: Optional[str]) -> int:
        if folder is None:
            return -1
        try:
            return os.stat(folder).st_mtime_ns
        except OSError:
            return -1

    @property
    def stats(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._stats)


class AsyncGalleryQueryRunner:
    """后台单线程扫描 runner（代际守卫：旧结果不回填）。"""

    def __init__(self, index: GalleryQueryIndex, *,
                 on_result: Optional[Callable[
                     [GalleryQuery, GalleryQueryResult], None]] = None
                 ) -> None:
        self._index = index
        self._on_result = on_result
        self._lock = threading.Lock()
        self._generation = 0
        self._shutdown = False
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="gallery-query")
        self._stats = {"submitted": 0, "delivered": 0,
                       "dropped_stale": 0, "dropped_shutdown": 0}

    # ── 代际 ───────────────────────────────────────────────────────────

    def begin_generation(self) -> int:
        """上下文切换（album/工程切换）：在飞结果作废。"""
        with self._lock:
            self._generation += 1
            return self._generation

    def shutdown(self) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
        self._executor.shutdown(wait=False)

    @property
    def is_shutdown(self) -> bool:
        with self._lock:
            return self._shutdown

    # ── 提交 ───────────────────────────────────────────────────────────

    def submit(self, query: GalleryQuery) -> bool:
        """提交后台查询；完成时回调（工作线程）携带代际复核。"""
        with self._lock:
            if self._shutdown:
                return False
            generation = self._generation
            self._stats["submitted"] += 1
        self._executor.submit(self._run, generation, query)
        return True

    def _run(self, generation: int, query: GalleryQuery) -> None:
        try:
            result = self._index.query(query)
        except Exception:
            logger.exception("gallery async query failed for %s",
                             query.album_id)
            return
        with self._lock:
            if self._shutdown:
                self._stats["dropped_shutdown"] += 1
                return
            if generation != self._generation:
                self._stats["dropped_stale"] += 1
                return     # 上下文已切走：旧结果不回填
            self._stats["delivered"] += 1
            callback = self._on_result
        if callback is not None:
            callback(query, result)

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
