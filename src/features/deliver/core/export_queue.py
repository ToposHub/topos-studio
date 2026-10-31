"""Deliver export queue — manage multiple timeline export jobs.

Each :class:`ExportJob` wraps a serialised :class:`TimelineExportConfig`
plus queueing metadata (status, timestamps, retry count). The
:class:`ExportQueue` runs jobs serially, supports pause/resume/cancel/retry,
and persists the queue to disk so it survives a restart (mirroring the
existing image-batch queue in ``src/shared/export/batch_queue_store.py``).

This module is deliberately PyQt-light: ``ExportQueue`` is a plain
``QObject`` that emits signals, but the actual export execution is delegated
to a caller-supplied runner callback (so the same queue drives preview, the
deliver page, or headless tests).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)

__all__ = [
    "JobStatus",
    "ExportJob",
    "ExportQueue",
    "save_export_queue",
    "load_export_queue",
    "clear_export_queue",
    "has_restorable_jobs",
]


# ── Status constants ──


class JobStatus:
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"  # queue-level pause marker on the head job
    NEEDS_RELINK = "needs_relink"  # timeline missing; never auto-runs (see VideoJobStatus)


RESTORABLE_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.FAILED, JobStatus.NEEDS_RELINK)


# ── Data model ──


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce an untrusted JSON scalar to float, falling back to default."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    """Coerce an untrusted JSON scalar to int, falling back to default."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class ExportJob:
    """A single queued export task.

    Attributes:
        job_id: stable unique id
        name: human-readable label (e.g. output filename or sequence name)
        config: serialised TimelineExportConfig (a plain dict). Stored as a
            dict so the queue file stays JSON-friendly and version-tolerant.
        output_path: destination file (also present in config, duplicated for
            fast listing / dedupe checks)
        status: current :class:`JobStatus`
        progress: 0.0..1.0 (only meaningful while RUNNING)
        export_fps: measured encoded frames per second for the current/last run
        error: error message if FAILED
        retry_count: how many times this job has been retried
        created_at / started_at / finished_at: epoch seconds
    """

    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    output_path: str = ""
    status: str = JobStatus.PENDING
    progress: float = 0.0
    export_fps: float = 0.0
    error: str = ""
    retry_count: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.job_id:
            raise ValueError("job_id cannot be empty")

    @property
    def is_restorable(self) -> bool:
        return self.status in RESTORABLE_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ExportJob:
        """Rebuild a job from an untrusted persisted dict.

        The queue file is external data: numeric fields may be malformed
        strings and ``config`` may not be a dict. Malformed values fall
        back to defaults instead of raising (AUD-11905).
        """
        config = data.get("config")
        if not isinstance(config, dict):
            config = {}
        job_id = data.get("job_id")
        return ExportJob(
            job_id=str(job_id) if job_id else str(uuid.uuid4()),
            name=str(data.get("name") or ""),
            config=config,
            output_path=str(data.get("output_path") or ""),
            status=str(data.get("status") or JobStatus.PENDING),
            progress=_safe_float(data.get("progress", 0.0)),
            export_fps=_safe_float(data.get("export_fps", 0.0)),
            error=str(data.get("error") or ""),
            retry_count=_safe_int(data.get("retry_count", 0)),
            created_at=_safe_float(data.get("created_at", 0.0)),
            started_at=_safe_float(data.get("started_at", 0.0)),
            finished_at=_safe_float(data.get("finished_at", 0.0)),
        )


# ── Persistence ──


_QUEUE_FILENAME = "deliver_export_queue.json"
_store_lock = threading.Lock()


def _get_queue_path() -> Path:
    """Resolve the queue file path (honouring TOPOS_CONFIG_PATH override).

    ``TOPOS_CONFIG_PATH`` points at the **config file** (e.g.
    ``PROJECT_ROOT/.topos/config.json``), not a directory — see
    ``src/main.py`` and ``ConfigManager.get_default_config_path``. The queue
    file is written next to that config file, matching the existing
    ``batch_queue_store`` behaviour for the image batch queue.
    """
    override = os.environ.get("TOPOS_CONFIG_PATH")
    if override:
        # The override is a file path; put the queue next to it.
        base = Path(os.path.dirname(os.path.abspath(os.path.expanduser(override))))
    else:
        # Default: ~/.topos/ (a directory).
        base = Path.home() / ".topos"

    # Ensure the base directory exists. ``exist_ok=True`` handles the
    # already-exists case; if ``base`` happens to be a file (misconfiguration),
    # fall back to its parent so we never crash startup.
    try:
        base.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError):
        base = base.parent
        base.mkdir(parents=True, exist_ok=True)
    return base / _QUEUE_FILENAME


def save_export_queue(jobs: list[ExportJob]) -> None:
    """Atomically persist the current queue (jobs list)."""
    data = {"jobs": [j.to_dict() for j in jobs], "saved_at": time.time()}
    path = _get_queue_path()
    with _store_lock:
        try:
            # Atomic write: temp file in same dir + os.replace
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning(f"Failed to save export queue: {e}")


def load_export_queue() -> list[ExportJob]:
    """Load the persisted queue. Jobs whose RUNNING status is stale are
    demoted to PENDING (the process that marked them RUNNING is gone)."""
    path = _get_queue_path()
    if not path.exists():
        return []
    try:
        with _store_lock:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Failed to load export queue: {e}")
        return []
    # AUD-11905: valid JSON can still have the wrong *shape* — a non-dict
    # top level, a non-list "jobs", or non-dict entries must degrade to a
    # warning + skip, never crash the startup restore path.
    if not isinstance(data, dict):
        logger.warning(
            f"Ignoring malformed export queue file (top-level "
            f"{type(data).__name__}, expected object): {path}"
        )
        return []
    raw_jobs = data.get("jobs", [])
    if not isinstance(raw_jobs, list):
        logger.warning(
            f"Ignoring malformed export queue file (jobs is "
            f"{type(raw_jobs).__name__}, expected array): {path}"
        )
        return []
    jobs: list[ExportJob] = []
    for index, entry in enumerate(raw_jobs):
        if not isinstance(entry, dict):
            logger.warning(
                f"Skipping malformed queue entry #{index} "
                f"({type(entry).__name__}): {path}"
            )
            continue
        try:
            jobs.append(ExportJob.from_dict(entry))
        except (TypeError, ValueError, AttributeError, KeyError) as e:
            logger.warning(f"Skipping unloadable queue entry #{index}: {e}")
            continue
    # Stale RUNNING jobs from a crashed session become PENDING again.
    for j in jobs:
        if j.status == JobStatus.RUNNING:
            j.status = JobStatus.PENDING
            j.progress = 0.0
    return jobs


def clear_export_queue() -> None:
    """Delete the persisted queue file."""
    path = _get_queue_path()
    with _store_lock:
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"Failed to clear export queue: {e}")


def has_restorable_jobs() -> bool:
    """Whether any persisted job can be restored on startup."""
    return any(j.is_restorable for j in load_export_queue())


# ── Queue controller ──


# Runner signature: given an ExportJob, execute it. The runner must call the
# supplied progress callback and return a bool (success) or raise on error.
ProgressCallback = Callable[[float], None]
ExportRunner = Callable[[ExportJob, ProgressCallback], bool]


class ExportQueue(QObject):
    """Serial export queue with pause/resume/cancel/retry.

    Signals:
        job_added(job_id): a job was enqueued
        job_started(job_id): a job began running
        job_progress(job_id, progress): progress update (0..1)
        job_completed(job_id, success): a job finished (success bool)
        job_removed(job_id): a job was removed from the queue
        queue_drained(): all jobs finished (queue empty, none running)
    """

    job_added = pyqtSignal(str)
    job_started = pyqtSignal(str)
    job_progress = pyqtSignal(str, float)
    job_completed = pyqtSignal(str, bool)
    job_removed = pyqtSignal(str)
    queue_drained = pyqtSignal()

    def __init__(self, runner: Optional[ExportRunner] = None, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._jobs: list[ExportJob] = []
        self._runner: Optional[ExportRunner] = runner
        self._paused: bool = False
        self._cancelled_current: bool = False
        self._running: bool = False
        self._by_id: dict[str, ExportJob] = {}

    # ── inspection ──

    @property
    def jobs(self) -> list[ExportJob]:
        return list(self._jobs)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    def get_job(self, job_id: str) -> Optional[ExportJob]:
        return self._by_id.get(job_id)

    def pending_count(self) -> int:
        return sum(1 for j in self._jobs if j.status in (JobStatus.PENDING, JobStatus.RUNNING))

    # ── mutation ──

    def set_runner(self, runner: ExportRunner) -> None:
        """Set the export runner (decouples queue from the concrete exporter)."""
        self._runner = runner

    def enqueue(self, job: ExportJob, *, preserve_status: bool = False) -> None:
        """Add a job to the end of the queue.

        By default the job is forced to ``PENDING`` (a freshly-added job
        should never inherit an arbitrary status). Pass ``preserve_status=True``
        when restoring from disk so a job already marked ``NEEDS_RELINK`` is
        not silently reset to ``PENDING`` and auto-run.
        """
        if job.job_id in self._by_id:
            raise ValueError(f"Duplicate job_id: {job.job_id}")
        # M21.1c：双任务争同一输出路径——对**进行中**（PENDING/RUNNING/
        # PAUSED）的已有任务同路径入队明确拒绝（不排队到同名输出后静默
        # 互相覆盖——发布事务保证单文件原子性，队列层防止的是两个任务
        # 都成功后一次 rename 覆盖另一任务的成果）。
        active = (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.PAUSED)
        out_path = str(getattr(job, "output_path", "") or "")
        if out_path:
            for existing in self._jobs:
                if existing.status not in active:
                    continue
                existing_out = str(
                    getattr(existing, "output_path", "") or "")
                if existing_out == out_path:
                    raise ValueError(
                        f"输出路径已被进行中的任务占用：{out_path}"
                        f"（job {existing.job_id}）——等其完成或移除后再入队")
        if not preserve_status:
            job.status = JobStatus.PENDING
            job.progress = 0.0
        self._jobs.append(job)
        self._by_id[job.job_id] = job
        self.job_added.emit(job.job_id)
        self._persist()

    def remove(self, job_id: str) -> bool:
        """Remove a job (not allowed for the currently-running job)."""
        job = self._by_id.get(job_id)
        if job is None or job.status == JobStatus.RUNNING:
            return False
        self._jobs = [j for j in self._jobs if j.job_id != job_id]
        del self._by_id[job_id]
        self.job_removed.emit(job_id)
        self._persist()
        return True

    def clear_completed(self) -> None:
        """Remove all DONE / FAILED / CANCELLED jobs."""
        # AUD-11907: 按状态集合判断移除对象，而不是 ``j not in keep`` 值
        # 相等成员判断——后者是 O(n²)，且按字段值比较时两个字段完全同值
        # 的作业（同参数重复作业）会被误删、只保留先入队那条。
        active_statuses = (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.PAUSED)
        keep = [j for j in self._jobs if j.status in active_statuses]
        removed = [j.job_id for j in self._jobs if j.status not in active_statuses]
        self._jobs = keep
        self._by_id = {j.job_id: j for j in keep}
        for rid in removed:
            self.job_removed.emit(rid)
        self._persist()

    # ── control ──

    def start(self) -> None:
        """Begin processing pending jobs (no-op if already running)."""
        if self._running or self._runner is None:
            return
        self._paused = False
        self._running = True
        self._process_next()

    def pause(self) -> None:
        """Pause scheduling after the current runner invocation finishes."""
        self._paused = True

    def resume(self) -> None:
        """Resume from pause and continue processing."""
        if not self._paused:
            return
        self._paused = False
        if not self._is_currently_executing():
            # AUD-11904: the pause drain path clears ``_running`` when it
            # parks the scheduler, so — like retry() — we must re-arm it
            # before continuing, otherwise the queue can never restart.
            self._running = True
            self._process_next()

    def cancel_current(self) -> None:
        """Request cancellation of the currently-running job (cooperative)."""
        self._cancelled_current = True

    def retry(self, job_id: str) -> bool:
        """Re-queue a FAILED/CANCELLED/NEEDS_RELINK job at the end of the queue.

        NEEDS_RELINK jobs are re-queued only after the caller has resolved
        the timeline reference (typically the user picked a replacement).
        ``retry`` does not verify the relink itself — it trusts the caller.

        If the queue is idle (not running), retrying also kicks off processing
        so the user does not need to call start() separately.
        """
        job = self._by_id.get(job_id)
        if job is None or job.status not in (JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.NEEDS_RELINK):
            return False
        job.status = JobStatus.PENDING
        job.progress = 0.0
        job.error = ""
        job.retry_count += 1
        self._persist()
        if not self._paused and not self._is_currently_executing():
            # Resume processing — start() is idempotent if already running.
            self._running = True
            self._process_next()
        return True

    # ── internal ──

    def _is_currently_executing(self) -> bool:
        return any(j.status == JobStatus.RUNNING for j in self._jobs)

    def _process_next(self) -> None:
        # AUD-11906: iterative scheduling. The previous implementation
        # tailed into ``self._process_next()`` after each job, so a queue
        # with thousands of jobs (INDIVIDUAL_CLIPS renders) grew the stack
        # one frame per job until RecursionError escaped ``start()`` into
        # the UI slot. A plain loop keeps stack depth constant.
        while True:
            if self._paused:
                self._running = False
                return
            # Find the next pending job in order.
            next_job: Optional[ExportJob] = None
            for j in self._jobs:
                if j.status == JobStatus.PENDING:
                    next_job = j
                    break
            if next_job is None:
                self._running = False
                self.queue_drained.emit()
                return

            next_job.status = JobStatus.RUNNING
            next_job.started_at = time.time()
            next_job.progress = 0.0
            next_job.export_fps = 0.0
            # NOTE: do not reset _cancelled_current here — a cancel requested
            # before start() must still apply to this job. The flag is
            # consumed below when classifying the result.
            self.job_started.emit(next_job.job_id)
            self._persist()

            try:
                assert self._runner is not None
                success = bool(self._runner(next_job, lambda p: self._on_progress(next_job.job_id, p)))
                next_job.status = JobStatus.DONE if success and not self._cancelled_current else (
                    JobStatus.CANCELLED if self._cancelled_current else JobStatus.FAILED
                )
                if next_job.status == JobStatus.FAILED and not success:
                    next_job.error = "export runner returned failure"
            except Exception as e:
                next_job.status = JobStatus.FAILED
                next_job.error = str(e)
                logger.warning(f"Export job {next_job.job_id} failed: {e}")
            finally:
                next_job.finished_at = time.time()
                self.job_completed.emit(next_job.job_id, next_job.status == JobStatus.DONE)
                self._persist()

            # Consume the cancel flag so it doesn't bleed into the next job.
            self._cancelled_current = False

            # Continue to the next job if not paused (loop, not recursion).

    def _on_progress(self, job_id: str, progress: float) -> None:
        job = self._by_id.get(job_id)
        if job is not None:
            job.progress = max(0.0, min(1.0, float(progress)))
            self.job_progress.emit(job_id, job.progress)

    def _persist(self) -> None:
        save_export_queue(self._jobs)
