"""
跟踪任务服务门面（阶段 3，只在 UI 线程使用）。

职责：
- 开始 / 取消 / 清除 / 只重跟踪失败段；
- 同一形状（clip_id + shape_id）同时只允许一个任务（互斥），
  新任务到达时先取消旧任务；
- 换片段 / 换参考帧时通过 ``invalidate`` 作废旧任务；
- 陈旧结果丢弃：结果的 generation 必须与当前任务代次一致，否则丢弃；
- 命中缓存的结果直接以当前代次回发（不重新计算）。

服务本身不接触 ``TrackingStore``：结果通过信号交给调用方写入绑定，
保持「服务只负责调度」的边界。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Tuple

from PyQt6.QtCore import QObject, pyqtSignal

from src.shared.tracking.tracker_2d import TrackRequest, TrackResult, Tracker2D, TrackDirection
from src.shared.tracking.tracking_cache import TrackingCache, compute_media_fingerprint
from .tracking_worker import TrackingWorker
from src.shared.tracking.types import TrackStatus, TrackingMode

logger = logging.getLogger(__name__)

Region = Tuple[float, float, float, float]

# 重跟踪失败段时向两端外扩的帧数（提供光流上下文）
RETRACK_CONTEXT_FRAMES = 3


class TrackingService(QObject):
    """跟踪任务调度门面。

    Args:
        cache: 结果缓存；为 None 时创建默认实例。
        worker_factory: 创建 worker 的工厂（测试可注入假 worker）；
            签名 ``factory(request) -> worker``，worker 需提供
            ``progress/finished_with_result/failed_with_error`` 信号、
            ``start()/request_cancel()/wait(ms)`` 方法。
    """

    # task_key, generation
    task_started = pyqtSignal(str, int)
    # task_key, generation, done, total
    task_progress = pyqtSignal(str, int, int, int)
    # task_key, generation, TrackResult
    task_finished = pyqtSignal(str, int, object)
    # A user stop keeps completed frames without caching an incomplete pass.
    task_partial_result = pyqtSignal(str, int, object)
    # task_key, generation, message
    task_failed = pyqtSignal(str, int, str)
    # task_key, generation
    task_cancelled = pyqtSignal(str, int)
    # task_key, generation（陈旧结果被丢弃）
    stale_result_discarded = pyqtSignal(str, int)

    def __init__(
        self,
        cache: Optional[TrackingCache] = None,
        worker_factory: Optional[Callable[[TrackRequest], object]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.cache = cache or TrackingCache()
        self._tracker_factory: Optional[Callable[[], Tracker2D]] = None
        self._worker_factory = worker_factory or (
            lambda req: TrackingWorker(
                req,
                tracker=(self._tracker_factory() if self._tracker_factory else None),
            )
        )
        self._generation: Dict[str, int] = {}        # task_key -> 当前代次
        self._workers: Dict[str, List[object]] = {}  # task_key -> 运行中 workers
        # AUD-24904: 取消超时后仍在运行的 worker 的保活强引用。
        # wait() 超时意味着 QThread 还活着——若此时丢弃最后强引用，
        # GC 析构运行中的 QThread 会直接 Abort 进程（同 image_engine
        # 的 _TERMINATED_THREADS 防御，P1-THREAD-01）。
        self._zombie_workers: List[object] = []
        self._fingerprint_cache: Dict[str, str] = {}  # media_path -> fingerprint
        # (task_key, generation) -> 重跟踪失败段时允许合并的帧集合
        self._merge_filters: Dict[Tuple[str, int], set] = {}

    def set_tracker_factory(self, factory: Optional[Callable[[], Tracker2D]]) -> None:
        """设置默认 worker 使用的跟踪器工厂。

        UI 参数在每次开始跟踪前通过此接口注入；测试传入自定义
        ``worker_factory`` 时仍保持原有行为。
        """
        self._tracker_factory = factory

    # ================================================================
    # 查询
    # ================================================================

    @staticmethod
    def task_key(clip_id: str, shape_id: str) -> str:
        return f"{clip_id}:{shape_id}"

    def generation_of(self, clip_id: str, shape_id: str) -> int:
        return self._generation.get(self.task_key(clip_id, shape_id), 0)

    def is_running(self, clip_id: str, shape_id: str) -> bool:
        return bool(self._workers.get(self.task_key(clip_id, shape_id)))

    def running_keys(self) -> List[str]:
        return list(self._workers.keys())

    # ================================================================
    # 开始 / 取消
    # ================================================================

    def start(
        self,
        clip_id: str,
        shape_id: str,
        media_path: str,
        pattern_region: Region,
        search_region: Region,
        reference_frame: int,
        frame_start: int,
        frame_end: int,
        mode: TrackingMode = TrackingMode.TRANSLATION_ROTATION_SCALE,
        quality=None,
        directions: Tuple[TrackDirection, ...] = (
            TrackDirection.FORWARD, TrackDirection.BACKWARD,
        ),
        shape_geometry: Optional[dict] = None,
    ) -> int:
        """启动一次跟踪任务（同形状互斥：先取消旧任务）。返回新代次。"""
        from src.shared.tracking.frame_sampler import AnalysisQuality

        key = self.task_key(clip_id, shape_id)
        self._cancel_workers(key)
        gen = self._generation.get(key, 0) + 1
        self._generation[key] = gen
        self._workers[key] = []
        # AUD-24906: task_started 必须先于任何 task_finished 回发。
        # 旧实现把它放在方向循环之后，全部方向命中缓存时控制器先收到
        # finished（置 SUCCESS）再收到 started（无条件置 RUNNING），面板
        # 永久卡在 RUNNING 且无后续信号纠正。移到代次登记后、回发前，
        # 两种路径（缓存命中/新 worker）的信号顺序都是 started→finished。
        self.task_started.emit(key, gen)
        # 重跟踪失败段模式：一次性捕获合并过滤，供所有方向的结果回发使用
        merge_filter = self._merge_filters.pop((key, gen), None)

        fingerprint = self._fingerprint(media_path)
        quality = quality or AnalysisQuality.DRAFT

        emitted_cached = False
        for direction in directions:
            request = TrackRequest(
                media_path=media_path,
                pattern_region=pattern_region,
                search_region=search_region,
                reference_frame=reference_frame,
                frame_start=frame_start,
                frame_end=frame_end,
                mode=mode,
                direction=direction,
                quality=quality,
                generation=gen,
                shape_geometry=shape_geometry,
            )
            cache_key = self._cache_key_for(request, fingerprint)
            cached = self.cache.get(cache_key)
            if cached is not None:
                # 命中缓存：以当前代次回发，不重新计算；
                # 若处于重跟踪失败段模式，同样只合并允许的帧。
                result = replace(cached, generation=gen)
                if merge_filter is not None:
                    result = self._apply_merge_filter(result, merge_filter)
                self.task_finished.emit(key, gen, result)
                emitted_cached = True
                continue
            self._spawn_worker(key, gen, request, cache_key, merge_filter)

        if not emitted_cached and not self._workers.get(key):
            # 所有方向都命中缓存时不算运行中；这里处理空方向列表等异常
            logger.debug("任务 %s 没有可启动的方向", key)
        return gen

    def retrack_failed(
        self,
        clip_id: str,
        shape_id: str,
        binding_transforms: Dict[int, object],
        media_path: str,
        pattern_region: Region,
        search_region: Region,
        reference_frame: int,
        frame_start: int,
        frame_end: int,
        mode: TrackingMode = TrackingMode.TRANSLATION_ROTATION_SCALE,
        quality=None,
        directions: Tuple[TrackDirection, ...] = (
            TrackDirection.FORWARD, TrackDirection.BACKWARD,
        ),
        shape_geometry: Optional[dict] = None,
    ) -> int:
        """只重跟踪失败段：重跑双向跟踪，但结果仅合并进原先失败的帧。

        合并策略在服务层完成：本方法返回代次并注册合并规则，
        结果到达时仅把「之前失败/缺失」的帧写入回发结果。
        """
        failed = {
            f for f, t in binding_transforms.items()
            if getattr(t, "status", None) == TrackStatus.FAILED
        }
        if not failed:
            return self.generation_of(clip_id, shape_id)

        key = self.task_key(clip_id, shape_id)
        # 新代次可由当前代次确定推得（start 内部同样 +1），
        # 提前注册合并过滤：缓存命中路径会在 start 内同步回发结果。
        gen = self._generation.get(key, 0) + 1
        self._merge_filters[(key, gen)] = failed | self._gap_frames(
            failed, reference_frame, frame_start, frame_end
        )
        started_gen = self.start(
            clip_id=clip_id, shape_id=shape_id, media_path=media_path,
            pattern_region=pattern_region, search_region=search_region,
            reference_frame=reference_frame, frame_start=frame_start,
            frame_end=frame_end, mode=mode, quality=quality,
            directions=directions, shape_geometry=shape_geometry,
        )
        assert started_gen == gen, "代次推导与 start 实现不一致"
        return gen

    def cancel(self, clip_id: str, shape_id: str) -> None:
        """取消该形状的运行中任务（代次不变，但结果到达时按已取消处理）。"""
        key = self.task_key(clip_id, shape_id)
        self._cancel_workers(key)

    def invalidate(self, clip_id: Optional[str] = None) -> None:
        """作废旧任务：换片段 / 换参考帧时调用。

        取消相关运行中任务并使代次前进，之后到达的任何旧结果都将被丢弃。
        """
        keys = list(self._workers.keys())
        for key in keys:
            if clip_id is not None and not key.startswith(f"{clip_id}:"):
                continue
            self._cancel_workers(key)
            self._generation[key] = self._generation.get(key, 0) + 1

    def shutdown(self, wait_ms: int = 3000) -> None:
        """关闭服务：取消并等待所有工作线程退出。"""
        for key in list(self._workers.keys()):
            self._cancel_workers(key, wait_ms=wait_ms)

    # ================================================================
    # 内部
    # ================================================================

    def _cache_key_for(self, request: TrackRequest, fingerprint: str) -> str:
        """构造含形状几何摘要的缓存键（AUD-24907）。

        ``TrackingCache.key_for_request`` 的键不含 ``shape_geometry``，而
        tracker_2d 用它光栅化掩码限制初始特征点——几何变化后同参数重跟
        踪会命中按旧几何计算的缓存。shared 缓存键构造（tracking_cache.
        py）不属本文件可改范围，服务侧在键后追加几何摘要达到同等失效
        语义：不同几何 → 不同键。
        """
        return (
            TrackingCache.key_for_request(request, fingerprint)
            + ":"
            + self._geometry_digest(request.shape_geometry)
        )

    @staticmethod
    def _geometry_digest(shape_geometry: Optional[dict]) -> str:
        """形状几何的稳定短摘要（None/空 与不可序列化值区分处理）。"""
        if not shape_geometry:
            return "nogeo"
        try:
            raw = json.dumps(
                shape_geometry, sort_keys=True, separators=(",", ":"),
                default=repr,
            )
        except (TypeError, ValueError) as exc:
            # 无法稳定序列化的几何：记录后视为无几何键（与旧行为一致，
            # 宁可沿用旧缓存也不因键构造失败中断跟踪）。
            logger.warning(
                "shape_geometry 不可序列化，缓存键退化为无几何: %s", exc)
            return "nogeo"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def _fingerprint(self, media_path: str) -> str:
        fp = self._fingerprint_cache.get(media_path)
        if fp is None:
            fp = compute_media_fingerprint(media_path)
            self._fingerprint_cache[media_path] = fp
        return fp

    def _spawn_worker(
        self, key: str, gen: int, request: TrackRequest, cache_key: str,
        merge_filter: Optional[set] = None,
    ) -> None:
        worker = self._worker_factory(request)
        worker.progress.connect(
            lambda g, done, total, k=key: self._on_progress(k, g, done, total)
        )
        # AUD-24910: 把 worker 引用绑定进闭包，结束回调按对象身份移除；
        # isRunning() 过滤在「信号已送达、线程尚未完全退出」的窗口内
        # 会把刚结束的 worker 残留在运行列表且此后无人清理。
        worker.finished_with_result.connect(
            lambda g, result, k=key, ck=cache_key, mf=merge_filter, w=worker:
                self._on_finished(k, g, result, ck, mf, w)
        )
        worker.failed_with_error.connect(
            lambda g, msg, k=key, w=worker: self._on_failed(k, g, msg, w)
        )
        self._workers.setdefault(key, []).append(worker)
        worker.start()

    def _cancel_workers(self, key: str, wait_ms: int = 2000) -> None:
        self._prune_zombie_workers()
        workers = self._workers.pop(key, [])
        for worker in workers:
            worker.request_cancel()
        for worker in workers:
            if not worker.wait(wait_ms):
                # 注意：这里刻意不强杀。QThread.terminate 可能在任意点
                # 终止持锁线程导致堆损坏/进程 Abort（P0-COLOR-01 要消灭
                # 的正是这类崩溃）；运行中路径宁可让卡死 worker 自然泄漏
                # 到进程退出，强杀兜底只用于关闭路径。
                # AUD-24904: 超时后必须保留强引用——局部列表随函数返回
                # 释放时，GC 析构运行中的 QThread 同样会导致进程 Abort。
                self._zombie_workers.append(worker)
                logger.warning(
                    "TrackingWorker %s did not stop within %dms after "
                    "cancel; keeping a reference until it exits",
                    worker, wait_ms)

    def _prune_zombie_workers(self) -> None:
        """移除已退出的保活 worker，避免保活列表随时间无限增长。"""
        if self._zombie_workers:
            self._zombie_workers = [
                w for w in self._zombie_workers if w.isRunning()
            ]

    def _current(self, key: str, gen: int) -> bool:
        return self._generation.get(key) == gen

    def _on_progress(self, key: str, gen: int, done: int, total: int) -> None:
        if not self._current(key, gen):
            return
        self.task_progress.emit(key, gen, done, total)

    def _on_finished(
        self, key: str, gen: int, result: TrackResult, cache_key: str,
        merge_filter: Optional[set] = None,
        worker: object = None,
    ) -> None:
        self._drop_worker(key, worker)
        if not self._current(key, gen):
            # 陈旧结果：任务已被新任务/作废取代，直接丢弃
            self.stale_result_discarded.emit(key, gen)
            return
        if result.cancelled:
            partial = self._apply_merge_filter(result, merge_filter) if merge_filter is not None else result
            if partial.transforms:
                self.task_partial_result.emit(key, gen, partial)
            self.task_cancelled.emit(key, gen)
            return

        if merge_filter is not None:
            # AUD-24905: 缓存必须写入过滤前的完整结果——retrack_failed
            # 重跑的是全范围双向跟踪，worker 返回完整 transforms；合并
            # 过滤只作用于回发给调用方的部分结果。旧实现把过滤后的残缺
            # 结果写入与全量任务相同的键，此后同参数完整跟踪命中缓存只
            # 拿到失败帧子集（蒙版跟踪结果大面积消失）。
            emitted = self._apply_merge_filter(result, merge_filter)
        else:
            emitted = result

        # 未取消且代次有效：写入缓存（分方向键由请求决定，这里用结果携带的键）
        self.cache.put(cache_key, result)
        self.task_finished.emit(key, gen, emitted)

    def _on_failed(self, key: str, gen: int, message: str,
                   worker: object = None) -> None:
        self._drop_worker(key, worker)
        if not self._current(key, gen):
            self.stale_result_discarded.emit(key, gen)
            return
        self.task_failed.emit(key, gen, message)

    def _drop_worker(self, key: str, worker: object) -> None:
        """任务结束后按对象身份把该 worker 从运行列表移除（AUD-24910）。

        ``gen`` 无法区分同 key 的多个 worker，QThread 也没有身份可比；
        结束信号把 worker 引用带回来，这里按 ``is`` 精确摘除，不再依赖
        ``isRunning()``（信号送达时线程可能尚未完全退出）。若摘除时线程
        仍在收尾，转入保活列表，由 ``_prune_zombie_workers`` 回收。
        """
        # 顺带回收已退出的保活 worker（AUD-24904）
        self._prune_zombie_workers()
        workers = self._workers.get(key)
        if workers is None or worker is None:
            return
        remaining = [w for w in workers if w is not worker]
        if len(remaining) != len(workers) and bool(
                getattr(worker, "isRunning", lambda: False)()):
            self._zombie_workers.append(worker)
        if remaining:
            self._workers[key] = remaining
        else:
            self._workers.pop(key, None)

    @staticmethod
    def _apply_merge_filter(result: TrackResult, allowed: set) -> TrackResult:
        """重跟踪失败段：只保留允许合并的帧，避免覆盖已有有效结果。"""
        transforms = {
            f: t for f, t in result.transforms.items() if f in allowed
        }
        observations = {
            f: o for f, o in result.observations.items() if f in allowed
        }
        diagnostics = {
            f: d for f, d in result.diagnostics.items() if f in allowed
        }
        return TrackResult(
            generation=result.generation,
            cancelled=result.cancelled,
            transforms=transforms,
            observations=observations,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _gap_frames(
        failed: set, reference_frame: int, frame_start: int, frame_end: int
    ) -> set:
        """失败段两端外扩上下文帧（重跟踪结果需要覆盖衔接帧以便合并平滑）。

        衔接帧本身也允许被新结果覆盖（它们邻接失败段，属于重跟踪语义）。
        """
        extra = set()
        for f in failed:
            for delta in range(-RETRACK_CONTEXT_FRAMES, RETRACK_CONTEXT_FRAMES + 1):
                nf = f + delta
                if nf == reference_frame:
                    continue
                if frame_start <= nf <= frame_end:
                    extra.add(nf)
        return extra
