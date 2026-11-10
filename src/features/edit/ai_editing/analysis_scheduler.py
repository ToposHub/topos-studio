"""TAEP AnalysisJobScheduler — 统一分析任务调度（M3 / §17.7 / §18.3）。

现状问题：``jobs.py`` 的 ``JobRegistry`` 只是 job 数据合约（不调度线程）；各 worker
（SceneCutWorker(QThread) / WaveformExtractor / MediaImportWorker）各自为政，无统一
队列/优先级/取消。产品计划 §17.7 / §18.3 要求资源池分级
（decode_cpu / local_gpu / cloud_network / render_export），**实时播放/导出优先级
高于后台分析**——否则后台 ASR/OCR 会抢占播放帧解码，导致卡顿。

本模块是 M3 的**接入层核心**：把分散的分析任务收口到一个带优先级、并发上限、统一
取消的调度器。生产装配可启用受控线程池；测试默认保持同步。

设计（最小可用，首版只做**优先级队列 + 并发上限 + 统一取消**）：

- 默认不拥有 worker 线程，只做队列管理 + ``JobHandle`` 状态驱动；启用
  ``asynchronous=True`` 时使用受控 ``ThreadPoolExecutor`` 执行纯 Python work。
- **playback_active 优先级**：播放期间，``ANALYSIS_*`` lane 的新任务**不出队**（已运行
  的不强制中断，但新任务排队等待），保证播放不被后台分析卡顿（M3 验收第 3 项）。
- **并发上限按 lane 独立**：ANALYSIS_LOCAL 默认 2（不抢占播放/导出）；ANALYSIS_CLOUD
  默认 1；EXPORT/PLAYBACK 不受限。
- **统一 cancel**：``cancel_job`` / ``cancel_all``（工程关闭用）；running 标记为
  CANCELLING，实际中断由 worker 的 cancel token 协作。

线程模型：本类用 ``RLock`` 保护队列字典；``submit``/``cancel`` 可在任意线程调用。
真实 work 执行默认同步在 ``submit`` 调用线程跑（测试用）；生产环境由组合根启用
``asynchronous=True``，避免阻塞 Qt 主线程。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import Any, Callable

from src.features.edit.ai_editing.jobs import JobHandle, JobStatus


# ---------------------------------------------------------------------- #
# 资源池（§18.3）
# ---------------------------------------------------------------------- #


class ResourceLane(str, Enum):
    """资源池分级（§18.3）：实时播放/导出优先级高于后台分析。

    ANALYSIS_* 受并发上限 + playback_active 约束；EXPORT_RENDER/PLAYBACK 不受限
    （它们是用户主动发起的高优先级动作）。
    """

    ANALYSIS_LOCAL = "analysis_local"
    """本地 CPU/GPU 分析（scene cut / ASR / OCR / 响度 / 波形）。"""

    ANALYSIS_CLOUD = "analysis_cloud"
    """云网络分析（受 network 并发上限 + 上传同意约束）。"""

    EXPORT_RENDER = "export_render"
    """导出/渲染（高优先级；用户主动发起，不受 playback_active 暂停）。"""

    PLAYBACK = "playback"
    """实时播放（最高优先级；本调度器不调度播放，但 playback_active 会暂停分析）。"""

    @property
    def is_analysis(self) -> bool:
        """是否为后台分析 lane（受 playback_active + 并发上限约束）。"""
        return self in (ResourceLane.ANALYSIS_LOCAL, ResourceLane.ANALYSIS_CLOUD)


@dataclass(frozen=True)
class LaneLimits:
    """各 lane 的并发上限（§10.2 滥用保护 / §17.7）。

    超过上限的任务入队等待；None 表示不限（仅 EXPORT/PLAYBACK）。
    """

    analysis_local_concurrency: int = 2
    """本地分析并发上限。默认 2——不抢占播放帧解码，留给用户操作空间。"""

    analysis_cloud_concurrency: int = 1
    """云分析并发上限。默认 1——避免并发上传/费用失控。"""

    def limit_for(self, lane: ResourceLane) -> int | None:
        """取某 lane 的并发上限（None = 不限）。"""
        if lane == ResourceLane.ANALYSIS_LOCAL:
            return self.analysis_local_concurrency
        if lane == ResourceLane.ANALYSIS_CLOUD:
            return self.analysis_cloud_concurrency
        return None  # EXPORT/PLAYBACK 不受限


# ---------------------------------------------------------------------- #
# 队列项
# ---------------------------------------------------------------------- #


@dataclass
class _QueueItem:
    """调度队列中的一项（job + 出队时执行的 work 回调）。"""

    lane: ResourceLane
    job: JobHandle
    work: Callable[[], Any] | None = None
    # R2：job 被 scheduler 取消（pending 直接取消 / active 标 CANCELLING）时调用，
    # 让提交方（如 AnalysisService._execute_via_scheduler）有机会收尾依赖状态
    # （例如把 AnalysisRequest 从 RUNNING 迁移到 CANCELLED），避免状态机卡死。
    # 可为 None（无依赖收尾时）。回调签名 () -> None；不应抛（异常被吞并记日志）。
    on_cancel: Callable[[], Any] | None = None
    # 入队顺序（FIFO；同 lane 按此排序）
    seq: int = 0
    # P0-8：按域取消的标签（如 asset_id）——per-asset 取消经
    # cancel_jobs_by_tags 定位，不与 job_id 耦合。
    tags: tuple[str, ...] = ()


# ---------------------------------------------------------------------- #
# AnalysisJobScheduler
# ---------------------------------------------------------------------- #


class AnalysisJobScheduler:
    """统一分析任务调度（M3 / §17.7 / §18.3）。

    职责：

    - 按 ``ResourceLane`` 排队 + 并发上限（ANALYSIS 受限；EXPORT/PLAYBACK 抢占）。
    - ``playback_active`` 标志：播放期间暂停 ANALYSIS lane 出队（不卡顿播放）。
    - 统一 cancel：``cancel_job`` / ``cancel_all``（工程关闭用，无 orphan worker）。
    - 默认同步执行 work（测试用）；生产由组合根启用 ``asynchronous=True``，交给
      受控线程池执行。

    线程安全（``RLock``）。
    """

    def __init__(
        self, limits: LaneLimits | None = None, *, asynchronous: bool = False,
    ) -> None:
        self._limits = limits or LaneLimits()
        self._asynchronous = asynchronous
        self._executor = (
            ThreadPoolExecutor(
                max_workers=max(
                    1,
                    self._limits.analysis_local_concurrency
                    + self._limits.analysis_cloud_concurrency,
                ),
                thread_name_prefix="topos-analysis",
            )
            if asynchronous else None
        )
        self._lock = RLock()
        self._pending: deque[_QueueItem] = deque()
        self._active: dict[str, _QueueItem] = {}  # job_id → item（正在执行/已出队）
        self._all: dict[str, _QueueItem] = {}      # job_id → item（含 pending+active，供查询）
        self._playback_active: bool = False
        self._seq_counter: int = 0

    # ---------------- 配置 ---------------- #

    @property
    def limits(self) -> LaneLimits:
        return self._limits

    @property
    def asynchronous(self) -> bool:
        """生产调度器是否把 work 放入受控线程池。"""
        return self._asynchronous

    def set_playback_active(self, active: bool) -> None:
        """标记实时播放是否进行中。

        ``True`` 时，ANALYSIS lane 的新任务**不出队**（已运行的不中断），避免后台
        分析抢占播放帧解码（M3 验收第 3 项：播放不被后台任务卡顿）。
        ``False`` 时恢复出队。
        """
        with self._lock:
            changed = self._playback_active != active
            self._playback_active = active
        # 播放结束 → 尝试出队积压的分析任务
        if changed and not active:
            self._try_dispatch()

    @property
    def playback_active(self) -> bool:
        with self._lock:
            return self._playback_active

    # ---------------- 提交 ---------------- #

    def submit(
        self,
        lane: ResourceLane,
        job: JobHandle,
        *,
        work: Callable[[], Any] | None = None,
        on_cancel: Callable[[], Any] | None = None,
        tags: tuple[str, ...] = (),
    ) -> JobHandle:
        """提交一个 job 到指定 lane。

        - 若 lane 有空闲容量且（非分析 lane 或未在播放）→ 立即出队执行 work。
        - 否则入队等待（``job.status`` 保持 QUEUED）。

        ``work`` 是出队时执行的回调（如"把 SceneCutWorker 提交到 QThread"）。为 None
        时只登记 job（状态由外部驱动）。

        ``on_cancel``（R2）：job 被 scheduler 取消（pending 直接取消 / cancel_all
        收尾）时调用，让提交方收尾依赖状态（如 AnalysisRequest 状态迁移）。回调在
        scheduler 锁外执行，签名 ``() -> None``；抛出的异常被吞并记日志（取消路径
        不应因回调失败而中断清理）。

        ``tags``（P0-8）：按域取消标签（如 asset_id），供
        :meth:`cancel_jobs_by_tags` 定位；空元组 = 不参与按域取消。
        """
        with self._lock:
            self._seq_counter += 1
            item = _QueueItem(
                lane=lane, job=job, work=work, on_cancel=on_cancel,
                seq=self._seq_counter, tags=tuple(tags),
            )
            self._all[job.job_id] = item

            can_run_now = self._can_dispatch_locked(item)
            if can_run_now:
                self._active[job.job_id] = item
            else:
                self._pending.append(item)
        # 锁外执行 work（避免 work 内回调死锁）
        if can_run_now:
            self._run_work(item)
        return job

    # ---------------- 取消 ---------------- #

    def cancel_job(self, job_id: str) -> bool:
        """取消单个 job（pending 直接移除；running 标 CANCELLING，由 worker 协作中断）。

        R2：pending job 取消时调用其 ``on_cancel`` 回调，让提交方收尾依赖状态
        （此前 pending 取消不通知提交方，AnalysisRequest 会永久卡 RUNNING）。

        Returns:
            True 若 job 存在且被取消/标记取消。
        """
        on_cancel_cb: Callable[[], Any] | None = None
        with self._lock:
            item = self._all.get(job_id)
            if item is None:
                return False
            # pending：检查是否在 pending 队列（按 job_id，非 deque 成员相等）
            is_pending = any(i.job.job_id == job_id for i in self._pending)
            if is_pending:
                self._pending = deque(i for i in self._pending if i.job.job_id != job_id)
                if item.job.status == JobStatus.QUEUED:
                    item.job.request_cancel()  # QUEUED → CANCELLING
                    # QUEUED 状态下 complete() 会走 CANCELLED 分支
                    item.job.complete({})
                on_cancel_cb = item.on_cancel  # 锁外调用
            elif job_id in self._active:
                # running：标 CANCELLING，实际中断由 worker cancel token 协作
                item.job.request_cancel()
                # running job 的协作中断由 worker 完成（notify_finished），
                # 不在此处触发 on_cancel——避免与 worker 完成回调重复收尾。
                return True
            else:
                return False
        # 锁外执行 on_cancel（避免回调内回调 scheduler 造成死锁）
        _safe_invoke(on_cancel_cb, job_id)
        return True

    def cancel_jobs_by_tags(self, tags: tuple[str, ...] | set | list) -> int:
        """按标签取消匹配的 pending + active job（P0-8 per-asset 取消）。

        语义与 :meth:`cancel_job` 一致（pending 直接移除并收尾；running 标
        CANCELLING 由 worker 协作中断），只是按 ``submit(tags=...)`` 的域
        标签筛选。返回被取消/标记取消的 job 数。
        """
        wanted = set(tags)
        if not wanted:
            return 0
        with self._lock:
            job_ids = [
                item.job.job_id for item in self._all.values()
                if wanted.intersection(item.tags)
                and item.job.status.is_active
            ]
        cancelled = 0
        for job_id in job_ids:
            if self.cancel_job(job_id):
                cancelled += 1
        return cancelled

    def cancel_all(self) -> int:
        """取消所有 pending + active job（工程关闭用，无 orphan worker）。

        pending 直接移除并标 CANCELLED；running 标 CANCELLING 并从 active 容量释放
        （scheduler 不拥有 worker 线程，关闭时不再等待 worker 协作中断；实际 worker
        中断由其独立的 cancel token / dialog 生命周期保证）。

        R2：所有 pending job 的 ``on_cancel`` 回调在锁外被调用，使提交方能正确收尾
        （例如把 AnalysisRequest 从 RUNNING 迁移到 CANCELLED，避免重启后显示"中断，
        需重试"时状态仍为 RUNNING 的不一致）。

        Returns:
            被取消/标记取消的 job 数。
        """
        on_cancel_cbs: list[Callable[[], Any]] = []
        with self._lock:
            count = 0
            # pending 全部移除并标 CANCELLED
            for item in list(self._pending):
                if item.job.status == JobStatus.QUEUED:
                    item.job.request_cancel()
                    item.job.complete({})
                if item.on_cancel is not None:
                    on_cancel_cbs.append(item.on_cancel)
                count += 1
            self._pending.clear()
            # active 标 CANCELLING；释放容量（关闭语义：不等待 worker）
            for item in list(self._active.values()):
                if item.job.status.is_active:
                    item.job.request_cancel()
                    count += 1
            # 清空 active（容量释放；job 句柄仍在 _all 供查询历史）
            self._active.clear()
        # 锁外执行所有 on_cancel（顺序无关；异常隔离）
        for cb in on_cancel_cbs:
            _safe_invoke(cb, "(cancel_all)")
        return count

    # ---------------- 查询 ---------------- #

    def active_count(self, lane: ResourceLane | None = None) -> int:
        """正在执行的 job 数（可选按 lane 过滤）。

        只计 ``is_active`` 状态（QUEUED/RUNNING/CANCELLING）的 job；已终结的
        （SUCCEEDED/FAILED/CANCELLED）不计。
        """
        with self._lock:
            count = 0
            for i in self._active.values():
                if not i.job.status.is_active:
                    continue
                if lane is None or i.lane == lane:
                    count += 1
            return count

    def pending_count(self, lane: ResourceLane | None = None) -> int:
        """排队等待的 job 数（可选按 lane 过滤）。"""
        with self._lock:
            if lane is None:
                return len(self._pending)
            return sum(1 for i in self._pending if i.lane == lane)

    def get_job(self, job_id: str) -> JobHandle | None:
        with self._lock:
            item = self._all.get(job_id)
            return item.job if item is not None else None

    def is_idle(self) -> bool:
        """无任何 active 或 pending job（工程关闭清理的判定用）。

        已 cancel/complete 但未 ``notify_finished`` 的残留 active 条目不计为活跃。
        """
        with self._lock:
            has_active = any(i.job.status.is_active for i in self._active.values())
            return not has_active and not self._pending

    # ---------------- work 完成通知 ---------------- #

    def notify_finished(self, job_id: str) -> None:
        """通知某 job 已完成（worker 调用，释放 lane 容量并尝试出队下一个）。

        worker 在 ``work`` 回调内驱动 job 到 terminal 状态后调本方法，让调度器释放
        容量并 dispatch 积压任务。
        """
        with self._lock:
            self._active.pop(job_id, None)
        self._try_dispatch()

    def shutdown(self, *, wait: bool = False) -> None:
        """关闭生产线程池；工程切换时调用。

        AUD-12901：
        ① executor 交换在 ``self._lock`` 内完成，消除与 ``_run_work`` 读-用
           序列的竞态（此后 submit 不再抛未捕获 RuntimeError）；
        ② 被 ``cancel_futures=True`` 丢弃的在途 work 对应 job 统一
           ``fail("SCHEDULER_SHUTDOWN", ...)``，job 不再永久停留 RUNNING；
        ③ pending 队列同步终结（QUEUED → CANCELLED），避免关闭后残留任务
           在后续 ``_try_dispatch`` 中被同步执行；
        ④ pending 项的 ``on_cancel`` 在锁外触发（与 cancel_all 语义一致，
           active 项不触发——其实际完成回调仍可能到来，notify_finished 幂等）。
        """
        on_cancel_cbs: list[Callable[[], Any]] = []
        with self._lock:
            executor = self._executor
            self._executor = None
            # 在途/已出队：cancel_futures 会丢弃尚未执行的 future，job 必须
            # 在此终结（仍活跃的统一 FAILED；已终结的保持原终态不被覆盖）。
            for item in list(self._active.values()):
                if item.job.status.is_active:
                    item.job.fail(
                        "SCHEDULER_SHUTDOWN",
                        "scheduler shut down before work finished",
                    )
            self._active.clear()
            # pending：与 cancel_job 的 pending 分支同语义（QUEUED→CANCELLING→
            # CANCELLED），并触发提交方收尾回调。
            for item in list(self._pending):
                if item.job.status == JobStatus.QUEUED:
                    item.job.request_cancel()
                    item.job.complete({})
                elif item.job.status.is_active:
                    item.job.fail(
                        "SCHEDULER_SHUTDOWN",
                        "scheduler shut down before work started",
                    )
                if item.on_cancel is not None:
                    on_cancel_cbs.append(item.on_cancel)
            self._pending.clear()
        # executor.shutdown 不持锁调用（wait=False 时立即返回；期间并发的
        # _run_work 只会拿到 None → 同步降级，不会再 submit 到已交换的池）。
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=True)
        for cb in on_cancel_cbs:
            _safe_invoke(cb, "(shutdown)")

    # ---------------- internal ---------------- #

    def _can_dispatch_locked(self, item: _QueueItem) -> bool:
        """判断某 item 是否可立即出队（容量 + playback 约束）。"""
        lane = item.lane
        if lane.is_analysis and self._playback_active:
            return False  # 播放期间不出队分析任务
        limit = self._limits.limit_for(lane)
        if limit is None:
            return True  # EXPORT/PLAYBACK 不受限
        # 只计仍在活跃状态的 job（已终结未清理的不占容量）
        active_in_lane = sum(
            1 for i in self._active.values()
            if i.lane == lane and i.job.status.is_active
        )
        return active_in_lane < limit

    def _try_dispatch(self) -> None:
        """尝试出队 pending 中可执行的任务（锁内决策，锁外执行 work）。"""
        to_run: list[_QueueItem] = []
        with self._lock:
            remaining: deque[_QueueItem] = deque()
            for item in self._pending:
                if self._can_dispatch_locked(item):
                    self._active[item.job.job_id] = item
                    to_run.append(item)
                else:
                    remaining.append(item)
            self._pending = remaining
        # 锁外执行 work
        for item in to_run:
            self._run_work(item)

    def _run_work(self, item: _QueueItem) -> None:
        """执行 item 的 work 回调（若提供）。work 内应驱动 job 状态 + notify_finished。"""
        if item.job.status == JobStatus.QUEUED:
            item.job.mark_running()
        if item.work is None:
            return
        with self._lock:
            executor = self._executor
        if executor is None:
            self._run_work_sync(item)
            return
        try:
            executor.submit(self._run_work_sync, item)
        except RuntimeError:
            # AUD-12901：读到 executor 后、submit 前发生并发 shutdown——
            # 同步降级执行，保证 job 有终结状态而不是把 RuntimeError 抛给
            # 调用线程（notify_finished / set_playback_active / submit）。
            self._run_work_sync(item)

    def _run_work_sync(self, item: _QueueItem) -> None:
        """执行单个 work，并在异常时释放调度槽位。"""
        try:
            item.work()
        except Exception as exc:
            # work 抛异常 → 标记失败并释放容量
            item.job.fail("SCHEDULER_WORK_ERROR", str(exc))
            self.notify_finished(item.job.job_id)


def _safe_invoke(cb: Callable[[], Any] | None, job_hint: str) -> None:
    """安全调用 on_cancel 回调：异常被吞并记日志（取消路径不应中断清理）。

    取消是清理语义，回调失败（如 AnalysisService 已被销毁）不能阻断后续 job 的
    取消清理。job_hint 仅用于日志定位。
    """
    if cb is None:
        return
    try:
        cb()
    except Exception:
        logging.getLogger(__name__).debug(
            "on_cancel callback failed for job %s", job_hint, exc_info=True,
        )
