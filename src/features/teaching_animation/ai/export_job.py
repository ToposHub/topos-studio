"""Background export job + atomic output + postflight (MG-17).

MG-17 turns the export from a blocking synchronous call into a background job
with atomic output, explicit overwrite approval, cancel safety, disk
preflight, ffprobe-based postflight and a manifest.  The actual render +
ffmpeg invocation lives in the host adapters; this module defines the job
contract and the postflight checks so the export quality gate can rely on a
typed result instead of a bare success boolean.

Design rules:

* the job writes to a ``.tmp`` file and ``os.replace``-s on success, so a
  cancelled / crashed export never leaves a half-written file that looks
  finished;
* overwrite requires explicit approval (``overwrite_approval``) — the job
  refuses to clobber an existing path otherwise;
* postflight checks (ffprobe + file existence + size + optional audio /
  black-frame probes) produce a :class:`ExportPostflightReport` with stable
  codes; the quality gate treats any error finding as blocking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import threading
import time
import uuid
from typing import Any, Mapping

from .error_codes import (
    MOTION_GRAPHIC_CLIP_INSERT_FAILED,
    MotionGraphicFailure,
)


# --------------------------------------------------------------------------- #
# Job state
# --------------------------------------------------------------------------- #


EXPORT_PENDING = "pending"
EXPORT_RUNNING = "running"
EXPORT_SUCCEEDED = "succeeded"
EXPORT_FAILED = "failed"
EXPORT_CANCELLED = "cancelled"

TERMINAL_EXPORT_STATES: frozenset[str] = frozenset({
    EXPORT_SUCCEEDED, EXPORT_FAILED, EXPORT_CANCELLED,
})


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExportRequest:
    """Inputs for one export job."""

    scene_id: str
    output_path: str
    profile_id: str
    container: str = "mp4"
    codec: str = "h264"
    fps: float = 24.0
    width: int = 1920
    height: int = 1080
    transparent: bool = False
    overwrite_existing: bool = False
    audio_plan_hash: str | None = None
    estimated_duration_seconds: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Postflight
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PostflightFinding:
    code: str
    severity: str               # error / warning / info
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ExportPostflightReport:
    findings: tuple[PostflightFinding, ...] = ()
    file_size_bytes: int = 0
    duration_seconds: float | None = None
    frame_count: int | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    file_hash: str | None = None

    @property
    def errors(self) -> tuple[PostflightFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def is_blocking(self) -> bool:
        return bool(self.errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "file_size_bytes": self.file_size_bytes,
            "duration_seconds": self.duration_seconds,
            "frame_count": self.frame_count,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "file_hash": self.file_hash,
        }


EXPORT_FINALIZE_FAILED = "EXPORT_FINALIZE_FAILED"


def _remove_temp_file(path: str | None) -> None:
    """Best-effort removal of a leftover ``.tmp`` render file (never raises).

    A rejected / cancelled / failed export must not leave multi-GB temp files
    behind (AUD-26814); an unremovable temp file (locked by the OS, permissions)
    must not mask the job's own terminal state transition, so OSError is
    swallowed deliberately and only here.
    """
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


#: Returned by :func:`_publish_output` when the target exists and overwriting
#: was not approved.  Kept as a module constant so the diagnostic code stays a
#: stable vocabulary across preflight/finish.
EXPORT_OVERWRITE_REQUIRED = "EXPORT_OVERWRITE_REQUIRED"


def _publish_output(
    temp_path: str,
    final_path: str,
    *,
    allow_overwrite: bool,
) -> str | None:
    """Atomically move the rendered ``temp_path`` onto ``final_path``.

    Returns ``EXPORT_OVERWRITE_REQUIRED`` when the target exists and overwriting
    was not approved; ``None`` on success; raises ``OSError`` on IO failure.

    Closes the check-then-act window between the overwrite approval check and
    the publish (AUD-26816): on filesystems that support hard links the first
    attempt is ``os.link``, which fails *atomically* with ``FileExistsError``
    when the target appeared, so a file created by another process/task in the
    race window can never be clobbered without explicit approval.  Where hard
    links are unsupported (cross-device temp volume, Windows share, exotic
    filesystems) we fall back to the historical exists-check + ``os.replace``
    path and accept the narrow residual race.
    """

    try:
        os.link(temp_path, final_path)
    except FileExistsError:
        if not allow_overwrite:
            return EXPORT_OVERWRITE_REQUIRED
        os.replace(temp_path, final_path)
    except OSError:
        # Hard links unavailable here: keep the legacy check + replace path.
        if os.path.exists(final_path) and not allow_overwrite:
            return EXPORT_OVERWRITE_REQUIRED
        os.replace(temp_path, final_path)
    else:
        # The content now lives at final_path via the same inode; drop the
        # temp directory entry.  Failure here must not fail a finished export.
        try:
            os.unlink(temp_path)
        except OSError:
            pass
    return None


# --------------------------------------------------------------------------- #
# Job
# --------------------------------------------------------------------------- #


@dataclass
class ExportJob:
    """Live state of one background export job.

    The host's render worker drives this object: :meth:`start` → render →
    :meth:`report_progress` → :meth:`finish`.  The object itself does not own
    a thread; it is the shared state the worker mutates under a lock.
    """

    job_id: str
    request: ExportRequest
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    status: str = EXPORT_PENDING
    progress: float = 0.0
    cancel_requested: bool = False
    cancel_reason: str = ""
    output_temp_path: str | None = None
    postflight: ExportPostflightReport | None = None
    diagnostic_code: str = ""
    diagnostic_message: str = ""
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def __post_init__(self) -> None:
        if not self.job_id:
            raise ValueError("ExportJob.job_id is required")

    @property
    def is_terminal(self) -> bool:
        # Read under the lock for consistency with the rest of the state
        # protocol (AUD-26819); the underlying str read is atomic on CPython,
        # so this is about keeping the "worker mutates under lock" contract
        # uniform, not about torn reads.
        with self._lock:
            return self.status in TERMINAL_EXPORT_STATES

    def start(self, *, now: float | None = None) -> None:
        with self._lock:
            if self.status != EXPORT_PENDING:
                raise MotionGraphicFailure(
                    MOTION_GRAPHIC_CLIP_INSERT_FAILED,
                    f"导出任务当前状态 {self.status}，无法开始。",
                    details={"job_id": self.job_id, "status": self.status},
                )
            if self.cancel_requested:
                self.status = EXPORT_CANCELLED
                self.finished_at = now if now is not None else time.time()
                return
            self.status = EXPORT_RUNNING
            self.started_at = now if now is not None else time.time()

    def report_progress(self, fraction: float) -> None:
        with self._lock:
            self.progress = max(0.0, min(1.0, float(fraction)))

    def request_cancel(self, *, reason: str = "") -> None:
        with self._lock:
            self.cancel_requested = True
            self.cancel_reason = reason

    def finish(
        self,
        *,
        postflight: ExportPostflightReport,
        output_temp_path: str | None = None,
        now: float | None = None,
    ) -> None:
        """Move the job to a terminal state and publish the rendered output.

        Every non-succeeded path also removes the temp file: cancelled,
        quality-blocked, overwrite-refused and finalize-failed exports never
        leave a ``.tmp`` half-product on disk (the ``os.replace`` on success
        consumes it).  ``os.makedirs``/``os.replace`` failures transition to
        ``EXPORT_FAILED`` with ``EXPORT_FINALIZE_FAILED`` instead of letting an
        OSError escape with the job half-mutated (AUD-26815).  The publish
        itself is atomic no-clobber on hard-link-capable filesystems, so a
        target appearing mid-race is refused instead of overwritten
        (AUD-26816).
        """
        with self._lock:
            timestamp = now if now is not None else time.time()
            self.finished_at = timestamp
            self.postflight = postflight
            if self.cancel_requested:
                self.status = EXPORT_CANCELLED
                _remove_temp_file(output_temp_path)
                return
            if postflight.is_blocking:
                self.status = EXPORT_FAILED
                _remove_temp_file(output_temp_path)
                return
            # Atomic publish: temp → final (the host rendered into the temp).
            if output_temp_path is not None:
                final_path = self.request.output_path
                try:
                    os.makedirs(os.path.dirname(final_path) or ".", exist_ok=True)
                    refusal = _publish_output(
                        output_temp_path, final_path,
                        allow_overwrite=self.request.overwrite_existing,
                    )
                except OSError as exc:
                    self.status = EXPORT_FAILED
                    self.diagnostic_code = EXPORT_FINALIZE_FAILED
                    self.diagnostic_message = f"导出产物落盘失败：{exc}"
                    _remove_temp_file(output_temp_path)
                    return
                if refusal == EXPORT_OVERWRITE_REQUIRED:
                    self.status = EXPORT_FAILED
                    self.diagnostic_code = EXPORT_OVERWRITE_REQUIRED
                    self.diagnostic_message = "目标文件已存在且未批准覆盖。"
                    _remove_temp_file(output_temp_path)
                    return
            self.status = EXPORT_SUCCEEDED

    def fail(self, *, code: str, message: str, now: float | None = None) -> None:
        with self._lock:
            self.status = EXPORT_FAILED
            self.diagnostic_code = code
            self.diagnostic_message = message
            self.finished_at = now if now is not None else time.time()
            _remove_temp_file(self.output_temp_path)

    def cleanup_temp(self) -> None:
        """Remove the recorded temp file if it still exists (host cancel hook)."""
        with self._lock:
            _remove_temp_file(self.output_temp_path)

    def to_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "job_id": self.job_id,
                "status": self.status,
                "progress": self.progress,
                "cancel_requested": self.cancel_requested,
                "cancel_reason": self.cancel_reason,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "diagnostic_code": self.diagnostic_code,
                "diagnostic_message": self.diagnostic_message,
                "postflight": self.postflight.to_dict() if self.postflight else None,
            }


# --------------------------------------------------------------------------- #
# Disk preflight + postflight helpers
# --------------------------------------------------------------------------- #


def preflight_disk(
    output_path: str,
    *,
    estimated_bytes: int,
    free_bytes: int | None = None,
    overwrite_approved: bool = False,
) -> tuple[PostflightFinding, ...]:
    """Return findings for disk-space + overwrite checks before rendering."""
    findings: list[PostflightFinding] = []
    if os.path.exists(output_path) and not overwrite_approved:
        findings.append(PostflightFinding(
            code=EXPORT_OVERWRITE_REQUIRED,
            severity="error",
            message="目标文件已存在；需要明确的覆盖批准。",
            details={"path": output_path},
        ))
    if free_bytes is not None and estimated_bytes > free_bytes:
        findings.append(PostflightFinding(
            code="EXPORT_INSUFFICIENT_DISK",
            severity="error",
            message=f"估算需要 {estimated_bytes} 字节，剩余 {free_bytes} 字节。",
            details={"estimated": estimated_bytes, "free": free_bytes},
        ))
    return tuple(findings)


def postflight_from_stat(
    path: str,
    *,
    expected_duration_seconds: float | None = None,
    expected_frame_count: int | None = None,
    file_hash: str | None = None,
) -> ExportPostflightReport:
    """Build a postflight report from filesystem metadata (no ffprobe required).

    Used by tests and by hosts that don't have ffprobe available; the real
    export adapter layers ffprobe results on top of this base report.
    """

    findings: list[PostflightFinding] = []
    if not os.path.exists(path):
        findings.append(PostflightFinding(
            code="EXPORT_FILE_MISSING",
            severity="error",
            message="导出文件不存在。",
        ))
        return ExportPostflightReport(findings=tuple(findings))
    size = os.path.getsize(path)
    if size <= 0:
        findings.append(PostflightFinding(
            code="EXPORT_EMPTY_FILE",
            severity="error",
            message="导出文件为空。",
            details={"size": size},
        ))
    return ExportPostflightReport(
        findings=tuple(findings),
        file_size_bytes=size,
        duration_seconds=expected_duration_seconds,
        frame_count=expected_frame_count,
        file_hash=file_hash,
    )


def new_export_job(request: ExportRequest) -> ExportJob:
    return ExportJob(job_id=f"mg-export-{uuid.uuid4().hex}", request=request)


__all__ = [
    "ExportJob",
    "ExportPostflightReport",
    "ExportRequest",
    "PostflightFinding",
    "EXPORT_PENDING",
    "EXPORT_RUNNING",
    "EXPORT_SUCCEEDED",
    "EXPORT_FAILED",
    "EXPORT_CANCELLED",
    "TERMINAL_EXPORT_STATES",
    "EXPORT_FINALIZE_FAILED",
    "EXPORT_OVERWRITE_REQUIRED",
    "new_export_job",
    "postflight_from_stat",
    "preflight_disk",
]
