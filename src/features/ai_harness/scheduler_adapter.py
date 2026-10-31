"""Scheduler adapter —— 把现有 JobRegistry / AnalysisJobScheduler 桥接到 RunRecord。

plan §0.4 / §10.2 P0.6：``RunRecord`` 订阅 ``JobRegistry`` 与 TaskGraph 事件；
修复取消为真实 cancel token 传播。

现状（审计结论）：

- ``AssistantService`` **不**持有 ``JobRegistry``；``JobRegistry`` 由
  ``ExportService`` / ``AnalysisService`` / ``MediaGenerationService`` 各自拥有。
- ``AssistantStateSnapshot.jobs`` 恒为空（``assistant_service.py`` 第 248 行）。
- 取消在 ``AssistantService`` 层只是状态翻转，不传 cancel token。

本 adapter 提供：

- :func:`sync_job_to_run`：把一个 ``JobHandle`` 的当前状态同步成 ``RunRecord``
  里的 :class:`JobRef`，并按需发布 ``job.*`` 事件。
- :class:`JobRunBridge`：长期订阅 ``JobRegistry``（或 AnalysisJobScheduler），
  自动把 Job 进度 / 状态映射到对应 run，并调用 ``HarnessRuntime.cancel`` 路径
  把 run 的 cancel 反向传播到 Job。
- :func:`poll_cancel_to_job`：在 provider 检查点把 run 的 cancel token 转成
  ``JobHandle.request_cancel()``（plan §10.2 P0.6 真实取消贯通）。

本模块桥接 *现有* 纯数据合约（``JobHandle`` / ``JobStatus``）；不引入 Qt / 网络。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from src.features.ai_harness.invocation_models import JobRef
from src.features.ai_harness.runtime import HarnessRuntime


_LOG = logging.getLogger(__name__)

#: run_id <-> job_id 的双向映射由调用方维护（adapter 不假设两者一一对应）。
#: 一个 run 可能挂多个 Job（分析 + 生成 + 渲染），一个 Job 也可能被多 run 引用
#: （不常见，但 adapter 不阻止）。


def _kind_from_job_kind(job_kind: Any) -> str:
    """把 ``JobKind`` enum（或字符串）转成稳定字符串。"""
    return getattr(job_kind, "value", str(job_kind))


def _status_from_job_status(job_status: Any) -> str:
    return getattr(job_status, "value", str(job_status))


def job_ref_from_handle(handle: Any) -> JobRef:
    """从一个 ``JobHandle`` 构造只读 :class:`JobRef`。

    ``JobHandle`` 是可变的；本函数读其当前快照字段。调用方应在状态变化时再次
    调用以刷新。
    """
    return JobRef(
        job_id=getattr(handle, "job_id", ""),
        kind=_kind_from_job_kind(getattr(handle, "kind", "")),
        label=getattr(handle, "label", ""),
        status=_status_from_job_status(getattr(handle, "status", "")),
        progress=float(getattr(handle, "progress", 0.0) or 0.0),
    )


def sync_job_to_run(
    runtime: HarnessRuntime, run_id: str, handle: Any,
    *, emit_progress: bool = True,
) -> bool:
    """把 ``JobHandle`` 当前状态同步进 run（attach 或 update）。

    首次见到该 job_id 走 :meth:`HarnessRuntime.attach_job`；后续走
    :meth:`update_job_progress`。返回是否成功。
    """
    record = runtime.get_run(run_id)
    if record is None:
        return False
    ref = job_ref_from_handle(handle)
    existing = any(j.job_id == ref.job_id for j in record.jobs)
    if not existing:
        return runtime.attach_job(run_id, ref)
    if emit_progress:
        return runtime.update_job_progress(
            run_id, ref.job_id, ref.status, ref.progress,
        )
    return True


def poll_cancel_to_job(runtime: HarnessRuntime, run_id: str, handle: Any) -> bool:
    """把 run 的 cancel token 转成 ``JobHandle.request_cancel()``。

    在 provider 的检查点调用：如果 run 已被取消，则把对应 Job 也标 cancelling
    （协作取消；worker 在自己的检查点退出）。已 terminal 的 job 不再操作。

    Returns:
        是否触发了 request_cancel（首次）。
    """
    if not runtime.is_cancelled(run_id):
        return False
    status = _status_from_job_status(getattr(handle, "status", ""))
    # JobStatus.is_active：queued/running/cancelling。
    if status not in ("queued", "running", "cancelling"):
        return False
    if status == "cancelling":
        return False  # 已经在取消中
    request_cancel = getattr(handle, "request_cancel", None)
    if callable(request_cancel):
        request_cancel()
        _LOG.info(
            "cancel token propagated: run %s -> job %s", run_id, getattr(handle, "job_id", ""),
        )
        return True
    return False


class JobRunBridge:
    """把一个 ``JobRegistry``（或 AnalysisJobScheduler）的 Job 状态桥接到 run。

    使用方式（宿主装配侧）::

        bridge = JobRunBridge(runtime)
        bridge.bind(run_id, analysis_service.jobs)
        # provider 完成回调里调用 bridge.sync(run_id, job_id)
        # 取消时 bridge.propagate_cancel(run_id)

    本类不持有 Qt 对象；不阻塞 runtime。

    线程契约（AUD-22319 / AUD-21911）：``sync`` 可从 worker 线程回调（如
    ``JobWorkerPool`` future 回调）调用，``bind`` / ``track`` /
    ``propagate_cancel`` 通常在宿主（UI）线程——内部状态由 ``RLock`` 保护，
    不再依赖 GIL 单操作原子性；``propagate_cancel`` 在锁内对 job_id 做快照，
    锁外逐个同步，避免迭代期间并发写入。
    """

    def __init__(self, runtime: HarnessRuntime) -> None:
        self._runtime = runtime
        # RLock：sync → track 自身嵌套调用需要可重入（AUD-22319）。
        self._lock = threading.RLock()
        # run_id -> {job_id -> registry}
        self._bindings: dict[str, dict[str, Any]] = {}
        # job_id -> set(run_id)（反向索引，便于 cancel 广播）
        self._job_to_runs: dict[str, set[str]] = {}

    def bind(self, run_id: str, registry: Any) -> None:
        """声明 run 将在 ``registry`` 中创建 Job。后续 :meth:`sync` 据此查找。"""
        with self._lock:
            self._bindings.setdefault(run_id, {})["_registry"] = registry

    def track(self, run_id: str, job_id: str, registry: Any | None = None) -> None:
        """登记 run <-> job 关联（用于 cancel 反向广播）。"""
        with self._lock:
            self._bindings.setdefault(run_id, {})
            if registry is not None:
                self._bindings[run_id]["_registry"] = registry
            self._job_to_runs.setdefault(job_id, set()).add(run_id)

    def sync(self, run_id: str, job_id: str) -> bool:
        """根据已 bind 的 registry，把 job 的当前状态同步进 run。"""
        with self._lock:
            binding = self._bindings.get(run_id)
            if binding is None:
                return False
            registry = binding.get("_registry")
        if registry is None:
            return False
        # 兼容 JobRegistry.get 与 AnalysisJobScheduler.get_job。
        # registry 的方法调用在锁外（外部对象可能在回调里再入本类）。
        get = getattr(registry, "get", None) or getattr(registry, "get_job", None)
        if not callable(get):
            return False
        handle = get(job_id)
        if handle is None:
            return False
        self.track(run_id, job_id)
        return sync_job_to_run(self._runtime, run_id, handle)

    def propagate_cancel(self, run_id: str) -> int:
        """run 取消时，把其所有已知 Job 都标 cancelling。返回处理数。"""
        with self._lock:
            binding = self._bindings.get(run_id, {})
            registry = binding.get("_registry")
            # 锁内快照，锁外迭代（AUD-22319）：并发 track 不再使迭代中途
            # 看到"迭代中字典被修改"（RuntimeError）或漏绑定的 run。
            job_ids = [
                job_id for job_id, runs in self._job_to_runs.items()
                if run_id in runs
            ]
        if registry is None:
            return 0
        get = getattr(registry, "get", None) or getattr(registry, "get_job", None)
        if not callable(get):
            return 0
        n = 0
        for job_id in job_ids:
            handle = get(job_id)
            if handle is None:
                continue
            if poll_cancel_to_job(self._runtime, run_id, handle):
                n += 1
        return n


__all__ = [
    "JobRunBridge",
    "job_ref_from_handle",
    "poll_cancel_to_job",
    "sync_job_to_run",
]
