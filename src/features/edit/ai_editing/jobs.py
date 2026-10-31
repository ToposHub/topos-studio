"""TAEP Job 合约（Phase 4）。

为预览/导出/分析等长任务提供统一的 job contract：job ID、进度、日志、取消、
result manifest、错误 code（TAEP §12 P4）。

设计要点：

- job 是**异步、可取消、可恢复**的；绝不阻塞 Qt UI 线程（TAEP §11.1）。
- job 状态机：``queued → running → cancelling → succeeded/failed/cancelled``。
- 预览 job 使用短范围、低质量 preset；最终 deliver 使用明确 preset，且只在确认后
  写文件（TAEP §5.6 ``mode=dry_run_export`` 与最终 export 分离）。
- 取消、编码失败、preflight 失败、离线素材、字体缺失均不会产出“看似成功”的文件。
- 本模块定义纯数据合约；实际 worker 执行（线程/进程）由宿主或 P5+ 的 provider 接。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
import json
import os
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from src.features.edit.ai_editing.privacy_guard import redact_mapping, redact_text


class JobKind(str, Enum):
    """job 类型。"""

    PREVIEW = "preview"  # 短范围、低质量预览
    EXPORT = "export"  # 最终成片导出
    ANALYSIS = "analysis"  # 素材分析（P5）
    GENERATION = "generation"  # 生成媒体（视频/音乐/语音/图像，M11 / §8.2）


class JobStatus(str, Enum):
    """job 状态机。"""

    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)

    @property
    def is_active(self) -> bool:
        return self in (JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCELLING)


@dataclass(frozen=True)
class JobEvent:
    """Redacted, ordered progress event for UI reconnect and diagnostics."""

    sequence: int
    job_id: str
    kind: str
    status: JobStatus
    progress: float
    message: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "job_id": self.job_id,
            "kind": self.kind,
            "status": self.status.value,
            "progress": self.progress,
            "message": redact_text(self.message)[:300],
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class JobMetrics:
    """Aggregated counters suitable for a provider/workflow dashboard."""

    total: int
    active: int
    succeeded: int
    failed: int
    cancelled: int
    success_rate: float
    average_duration_seconds: float
    cost_units: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "active": self.active,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "success_rate": self.success_rate,
            "average_duration_seconds": self.average_duration_seconds,
            "cost_units": self.cost_units,
        }


class JobWorkerPool:
    """Shared non-Qt worker pool for long-running provider/export work.

    The worker owns only job state transitions.  The supplied callback owns
    domain validation and materialization, so a failed callback is visible as a
    failed JobHandle rather than being mistaken for success.
    """

    def __init__(self, registry: "JobRegistry", *, max_workers: int = 2) -> None:
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        self._registry = registry
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="topos-ai-job")
        self._futures: dict[str, Future[Any]] = {}
        # AUD-13702：_futures 需要并发提交时的一致性，且已完成条目必须可回收。
        self._futures_lock = threading.Lock()
        self._max_retained_futures = 256

    def submit(
        self,
        job: "JobHandle",
        work: Callable[["JobHandle"], Any],
    ) -> Future[Any]:
        """Run ``work`` away from the UI thread and return its Future."""
        future = self._executor.submit(self._run, job, work)
        with self._futures_lock:
            # 先回收已完成 job 的 Future（连同其持有的 result 返回值），
            # 防止长会话中 _futures 只增不减的内存泄漏。
            self._prune_done_locked()
            self._futures[job.job_id] = future
        return future

    def _prune_done_locked(self) -> None:
        done_ids = [
            job_id for job_id, future in self._futures.items() if future.done()
        ]
        for job_id in done_ids:
            self._futures.pop(job_id, None)
        # 兜底上限：即使全部未完成也不让字典无界增长。
        while len(self._futures) > self._max_retained_futures:
            self._futures.pop(next(iter(self._futures)))

    def cleanup_done(self) -> int:
        """移除已完成的 Future 条目；返回回收数量（宿主可在空闲时调用）。"""
        with self._futures_lock:
            before = len(self._futures)
            self._prune_done_locked()
            return before - len(self._futures)

    @staticmethod
    def _run(job: "JobHandle", work: Callable[["JobHandle"], Any]) -> Any:
        if job.status == JobStatus.QUEUED:
            job.mark_running()
        if job.status == JobStatus.CANCELLING:
            job.complete({})
            return None
        try:
            result = work(job)
            # Domain callbacks may complete/fail explicitly.  If they return
            # while still active, a manifest is the only implicit success.
            if job.status == JobStatus.RUNNING:
                job.complete(dict(result) if isinstance(result, dict) else {})
            return result
        except Exception as exc:  # noqa: BLE001 — worker failure must be observable
            if job.status.is_active:
                job.fail("JOB_WORKER_ERROR", f"{type(exc).__name__}: {redact_text(exc)}")
            return None

    def future(self, job_id: str) -> Future[Any] | None:
        with self._futures_lock:
            return self._futures.get(job_id)

    def shutdown(self, *, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)
        with self._futures_lock:
            self._futures.clear()


@dataclass
class JobHandle:
    """单个 job 的可观察句柄。

    纯数据 + 可选回调；不持有线程/文件资源。宿主或 worker 通过
    ``update_progress`` / ``complete`` / ``fail`` / ``cancel`` 驱动状态。
    """

    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    kind: JobKind = JobKind.EXPORT
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0  # 0.0..1.0
    label: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    # 结果（成功时填）
    result_manifest: dict[str, Any] | None = None
    checkpoint: dict[str, Any] | None = None
    # 错误（失败时填）
    error_code: str | None = None
    error_message: str | None = None
    # 日志（可选，不保存 secret/绝对路径）
    logs: list[str] = field(default_factory=list)
    # 内部：取消信号回调（worker 检查）
    _cancel_token: Any = None
    _event_sink: Callable[["JobHandle", str, str], None] | None = field(
        default=None, repr=False, compare=False,
    )

    def _emit(self, kind: str, message: str = "") -> None:
        if self._event_sink is not None:
            self._event_sink(self, kind, redact_text(message)[:300])

    # ---------------- 状态迁移（由 worker 调用）---------------- #

    def mark_running(self) -> None:
        if self.status != JobStatus.QUEUED:
            return
        self.status = JobStatus.RUNNING
        self.started_at = time.time()
        self._emit("running")

    def update_progress(self, progress: float, message: str | None = None) -> None:
        if not self.status.is_active:
            return
        self.progress = max(0.0, min(1.0, progress))
        if message:
            self.logs.append(message)
        self._emit("progress", message or f"progress={self.progress:.3f}")

    def complete(self, result_manifest: dict[str, Any]) -> None:
        if self.status == JobStatus.CANCELLING:
            self.status = JobStatus.CANCELLED
            self.finished_at = time.time()
            self._emit("cancelled", "cancel requested")
            return
        self.status = JobStatus.SUCCEEDED
        self.progress = 1.0
        self.result_manifest = result_manifest
        self.finished_at = time.time()
        self._emit("succeeded")

    def set_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        """Persist a redacted provider checkpoint without changing job state."""
        if not self.status.is_active:
            return
        self.checkpoint = redact_mapping(checkpoint)
        self._emit("checkpoint", "provider checkpoint updated")

    def fail(self, error_code: str, error_message: str) -> None:
        self.status = JobStatus.FAILED
        self.error_code = error_code
        self.error_message = error_message
        self.finished_at = time.time()
        self._emit("failed", error_message)

    def request_cancel(self) -> None:
        """请求取消（非阻塞）；worker 在下一个检查点真正停止。"""
        if self.status.is_active:
            self.status = JobStatus.CANCELLING
            self._emit("cancelling")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "job_id": self.job_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "progress": self.progress,
            "label": self.label,
            "created_at": self.created_at,
        }
        if self.started_at is not None:
            d["started_at"] = self.started_at
        if self.finished_at is not None:
            d["finished_at"] = self.finished_at
        if self.result_manifest is not None:
            d["result_manifest"] = redact_mapping(self.result_manifest)
        if self.checkpoint is not None:
            d["checkpoint"] = redact_mapping(self.checkpoint)
        if self.error_code is not None:
            d["error_code"] = self.error_code
            d["error_message"] = redact_text(self.error_message or "")[:500]
        if self.logs:
            d["logs"] = [redact_text(item) for item in self.logs[-50:]]
        return d

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], *, terminate_active: bool = True,
    ) -> "JobHandle":
        """Restore one job without reviving an orphan worker.

        Queued/running/cancelling jobs are conservatively terminated by default:
        an application restart cannot prove that a provider-side operation is
        idempotently resumable.  The original state remains in the error message
        so a host can explicitly retry through its own idempotency policy.
        """
        try:
            kind = JobKind(str(data.get("kind", JobKind.EXPORT.value)))
        except ValueError:
            kind = JobKind.EXPORT
        try:
            status = JobStatus(str(data.get("status", JobStatus.QUEUED.value)))
        except ValueError:
            status = JobStatus.QUEUED
        original_status = status
        error_code = data.get("error_code")
        error_message = data.get("error_message")
        if terminate_active and status.is_active:
            status = JobStatus.FAILED
            error_code = "JOB_INTERRUPTED"
            error_message = (
                "job safely terminated during application restart; "
                f"previous status: {original_status.value}"
            )
        handle = cls(
            job_id=str(data.get("job_id", "")) or str(uuid.uuid4()),
            kind=kind,
            status=status,
            progress=float(data.get("progress", 0.0) or 0.0),
            label=str(data.get("label", "")),
            created_at=float(data.get("created_at", time.time()) or time.time()),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            result_manifest=(
                dict(data["result_manifest"])
                if isinstance(data.get("result_manifest"), dict) else None
            ),
            checkpoint=(
                redact_mapping(data["checkpoint"])
                if isinstance(data.get("checkpoint"), dict) else None
            ),
            error_code=str(error_code) if error_code else None,
            error_message=redact_text(str(error_message))[:500] if error_message else None,
            logs=[redact_text(str(item)) for item in data.get("logs", []) if item],
        )
        if terminate_active and original_status.is_active:
            handle.finished_at = handle.finished_at or time.time()
            handle.logs.append(
                redact_text(f"recovery: terminated active job from {original_status.value}")
            )
        return handle


class JobRegistry:
    """job 注册表：管理多个 job 的生命周期，供 UI 查询/取消。

    线程模型：状态迁移由 worker 线程通过 JobHandle 方法调用；UI 只读
    ``to_dict()``。JobWorkerPool 默认多个 worker 并发驱动 JobHandle，
    因此注册表自身的共享状态（事件序号、事件表、job 表）由内部
    ``RLock`` 保护（AUD-13701）；宿主无需额外加锁。
    """

    def __init__(self) -> None:
        self._jobs: dict[str, JobHandle] = {}
        self._events: list[JobEvent] = []
        self._event_sequence = 0
        # RLock：submit/cancel 等持锁方法会经由 JobHandle._emit 重入
        # _record_event。
        self._lock = threading.RLock()

    def submit(self, kind: JobKind, label: str = "") -> JobHandle:
        handle = JobHandle(kind=kind, label=label)
        handle._event_sink = self._record_event
        with self._lock:
            self._jobs[handle.job_id] = handle
        self._record_event(handle, "queued", label)
        return handle

    def _record_event(self, job: JobHandle, kind: str, message: str = "") -> None:
        with self._lock:
            self._event_sequence += 1
            self._events.append(JobEvent(
                sequence=self._event_sequence,
                job_id=job.job_id,
                kind=kind,
                status=job.status,
                progress=job.progress,
                message=redact_text(message)[:300],
            ))
            if len(self._events) > 500:
                del self._events[:-500]

    def events_since(self, sequence: int = 0) -> tuple[JobEvent, ...]:
        """Return ordered events after a UI/client cursor."""
        with self._lock:
            events = tuple(self._events)
        return tuple(event for event in events if event.sequence > sequence)

    def metrics(self, kind: JobKind | None = None) -> JobMetrics:
        with self._lock:
            jobs = [job for job in self._jobs.values() if kind is None or job.kind == kind]
        terminal = [job for job in jobs if job.status.is_terminal]
        succeeded = sum(job.status == JobStatus.SUCCEEDED for job in jobs)
        failed = sum(job.status == JobStatus.FAILED for job in jobs)
        cancelled = sum(job.status == JobStatus.CANCELLED for job in jobs)
        durations = [
            max(0.0, (job.finished_at or 0.0) - (job.started_at or job.created_at))
            for job in terminal
            if job.finished_at is not None
        ]
        cost = sum(
            float(job.result_manifest.get("cost_units", 0.0))
            for job in jobs
            if isinstance(job.result_manifest, dict)
            and isinstance(job.result_manifest.get("cost_units", 0.0), (int, float))
        )
        return JobMetrics(
            total=len(jobs),
            active=sum(job.status.is_active for job in jobs),
            succeeded=succeeded,
            failed=failed,
            cancelled=cancelled,
            success_rate=succeeded / len(terminal) if terminal else 0.0,
            average_duration_seconds=sum(durations) / len(durations) if durations else 0.0,
            cost_units=cost,
        )

    def get(self, job_id: str) -> JobHandle | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job.request_cancel()
            return True

    def list_active(self) -> list[JobHandle]:
        with self._lock:
            return [j for j in self._jobs.values() if j.status.is_active]

    def list_all(self) -> list[JobHandle]:
        with self._lock:
            return list(self._jobs.values())

    def cleanup_terminal(
        self, keep: int = 50, *, max_age_seconds: float | None = None,
    ) -> None:
        """清理已终结的 job，保留最近 ``keep`` 个并可按年龄淘汰。

        ``max_age_seconds`` 是可选的运维保留门；默认行为与旧 API 一致。
        活动 job 永远不会被该方法删除。
        """
        with self._lock:
            terminal = [j for j in self._jobs.values() if j.status.is_terminal]
        now = time.time()
        expired: set[str] = set()
        if max_age_seconds is not None and max_age_seconds >= 0:
            expired = {
                job.job_id for job in terminal
                if job.finished_at is not None
                and now - job.finished_at >= max_age_seconds
            }
        # 按 finished_at 升序，先删超过年龄的，再按数量保留最近记录。
        terminal.sort(key=lambda j: j.finished_at or 0)
        remove_ids = set(expired)
        survivors = [job for job in terminal if job.job_id not in remove_ids]
        if len(survivors) > max(0, keep):
            remove_ids.update(job.job_id for job in survivors[: len(survivors) - max(0, keep)])
        with self._lock:
            for job_id in remove_ids:
                self._jobs.pop(job_id, None)

    def prune_events(self, max_events: int = 500) -> None:
        """Bound the in-memory event log without touching job state."""
        if max_events < 0:
            raise ValueError("max_events must be non-negative")
        with self._lock:
            if len(self._events) > max_events:
                self._events = self._events[-max_events:] if max_events else []

    # ---------------- P7: 持久化与安全恢复 ---------------- #

    def to_dict(self) -> dict[str, Any]:
        """Return a versioned, secret-redacted registry snapshot."""
        with self._lock:
            jobs = [job.to_dict() for job in list(self._jobs.values())]
            event_sequence = self._event_sequence
            events = [event.to_dict() for event in self._events[-200:]]
        return {
            "schema_version": "topos-job-registry/1.1",
            "jobs": jobs,
            "event_sequence": event_sequence,
            "events": events,
        }

    def persist(self, path: str | os.PathLike[str]) -> bool:
        """Atomically persist the current job registry.

        The temporary file is placed beside the target so ``os.replace`` is
        atomic on the local filesystem.  A failed write leaves the old snapshot
        untouched.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd: int | None = None
        temp_path: str | None = None
        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = None
                json.dump(self.to_dict(), stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, target)
            temp_path = None
            return True
        except (OSError, TypeError, ValueError):
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def restore(
        self, path: str | os.PathLike[str], *, terminate_active: bool = True,
    ) -> bool:
        """Restore a snapshot; active jobs are safely terminated by default."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return False
        return self.restore_dict(data, terminate_active=terminate_active)

    def restore_dict(
        self, data: dict[str, Any], *, terminate_active: bool = True,
    ) -> bool:
        """Restore from an already-loaded project payload."""
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            return False
        try:
            restored = {
                handle.job_id: handle
                for item in data["jobs"]
                if isinstance(item, dict)
                for handle in (JobHandle.from_dict(item, terminate_active=terminate_active),)
            }
        except (TypeError, ValueError):
            return False
        restored_events: list[JobEvent] = []
        for item in data.get("events", ()):
            if not isinstance(item, dict):
                continue
            try:
                status = JobStatus(str(item.get("status", JobStatus.QUEUED.value)))
            except ValueError:
                status = JobStatus.QUEUED
            restored_events.append(JobEvent(
                sequence=int(item.get("sequence", 0) or 0),
                job_id=str(item.get("job_id", "")),
                kind=str(item.get("kind", "")),
                status=status,
                progress=float(item.get("progress", 0.0) or 0.0),
                message=redact_text(str(item.get("message", "")))[:300],
                timestamp=float(item.get("timestamp", time.time()) or time.time()),
            ))
        restored_events.sort(key=lambda event: event.sequence)
        restored_events = restored_events[-500:]
        with self._lock:
            self._jobs = restored
            for handle in self._jobs.values():
                handle._event_sink = self._record_event
            self._event_sequence = int(data.get("event_sequence", 0) or 0)
            self._events = restored_events
            if self._events:
                self._event_sequence = max(self._event_sequence, self._events[-1].sequence)
        return True


__all__ = [
    "JobEvent", "JobHandle", "JobKind", "JobMetrics",
    "JobRegistry", "JobStatus", "JobWorkerPool",
]
