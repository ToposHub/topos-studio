"""VideoJobRunner — drive a single :class:`VideoExportJob` on a background thread.

The existing :class:`~src.features.deliver.core.export_queue.ExportQueue`
expects a *synchronous* runner callback
(``Callable[[ExportJob, ProgressCallback], bool]``) — it calls the runner
inside ``_process_next`` and treats the return value as the result. Video
export, however, is asynchronous by nature (the real work happens on a
``VideoExportWorker`` moved to a dedicated ``QThread``).

This module bridges the two with a :class:`VideoJobRunner` that:

1. Satisfies the synchronous ``ExportRunner`` protocol by blocking the
   caller's thread on a ``QEventLoop`` until the worker finishes.
2. Drives the worker from the *main* thread (the queue runs on the main
   thread, so this is safe — the worker still executes on its own thread).
3. Forwards progress to the queue's ``ProgressCallback``.
4. Honours cooperative cancellation via :meth:`request_cancel`, which the
   controller wires to the queue's ``cancel_current``.
5. Resolves the right ``EditProject`` / ``TimelineSequence`` for a job from
   a caller-supplied :class:`JobContextProvider` (so the runner stays free
   of UI / project-manager coupling).

The blocking-on-eventloop design is deliberate: it lets us reuse the
queue's serial scheduler verbatim (including pause / cancel / retry /
persistence) without rewriting it to be async, while keeping the actual
encoding off the main thread.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, Optional

from PyQt6.QtCore import QObject, QEventLoop, pyqtSignal

from src.features.deliver.core.export_queue import ExportJob, ProgressCallback
from src.features.deliver.core.video_export_job import SubtitleMode, VideoExportJob

logger = logging.getLogger(__name__)

__all__ = [
    "JobContext",
    "JobContextProvider",
    "VideoJobRunner",
]


# ── Context resolution ──
#
# The runner needs the live EditProject + TimelineSequence to feed the
# exporter, plus the current project revision for relink detection. We don't
# reach into the application singleton from here — the controller supplies a
# provider callback so the runner stays testable and UI-agnostic.


class JobContext:
    """Resolved runtime context for a job (project + sequence + revision)."""

    __slots__ = (
        "project",
        "sequence",
        "project_id",
        "project_revision",
        "reason",
        "timeline_sequences",
    )

    def __init__(
        self,
        project: Any = None,
        sequence: Any = None,
        project_id: str = "",
        project_revision: int = 0,
        reason: str = "",
        timeline_sequences: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.project = project
        self.sequence = sequence
        self.project_id = project_id
        self.project_revision = project_revision
        #: When ``project``/``sequence`` is None, a human-readable reason
        #: (used as the job error message).
        self.reason = reason
        #: U02（第六轮审计）：``{timeline_id: TimelineSequence}`` 全量映射，
        #: 供导出器解析嵌套子序列。None 表示宿主没有提供（嵌套片段降级）。
        self.timeline_sequences = timeline_sequences

    @property
    def is_resolvable(self) -> bool:
        return self.project is not None and self.sequence is not None


#: Callable that resolves a :class:`VideoExportJob` to a :class:`JobContext`.
#: Returning a context with ``is_resolvable == False`` causes the runner to
#: fail the job with ``context.reason`` as the error.
JobContextProvider = Callable[[VideoExportJob], JobContext]


def _default_context_provider(job: VideoExportJob) -> JobContext:
    """Default provider — always unresolved (no project wiring)."""
    return JobContext(reason="No project context available for this job")


# ── Runner ──


def _discard_sidecar_temp(srt_temp: Optional[str]) -> None:
    """失败/取消路径丢弃未发布的 sidecar 临时文件（best-effort）。"""
    if not srt_temp:
        return
    try:
        if os.path.exists(srt_temp):
            os.remove(srt_temp)
    except OSError:
        logger.debug("Failed to discard sidecar temp %s", srt_temp, exc_info=True)


class VideoJobRunner(QObject):
    """Synchronous-looking runner that drives an async ``VideoExporter``.

    Instantiate once (owned by the controller), pass
    ``runner.run_job`` to :meth:`ExportQueue.set_runner`. The runner will
    block the calling thread on a nested ``QEventLoop`` until each export
    finishes, so the queue's serial scheduler sees a normal blocking call.

    Signals:
        job_started(str): emitted when the underlying worker actually starts
            processing (after context resolution succeeds). Carries job_id.
        cancel_requested(str): emitted when cooperative cancellation was
            forwarded to the worker. Carries job_id.
        aborted(str): emitted when the nested event loop exited without any
            worker result (typically application shutdown). Carries job_id.
    """

    job_started = pyqtSignal(str)
    cancel_requested = pyqtSignal(str)
    aborted = pyqtSignal(str)

    def __init__(
        self,
        context_provider: Optional[JobContextProvider] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._context_provider: JobContextProvider = (
            context_provider or _default_context_provider
        )
        # The exporter for the currently-running job, if any. Used by
        # request_cancel() — guarded because cancel may arrive before the
        # worker exists.
        self._current_exporter: Optional[Any] = None
        self._current_job_id: Optional[str] = None
        # Result of the in-flight job — written by worker callbacks, read by
        # the blocking event loop.
        self._loop: Optional[QEventLoop] = None
        self._result_success: bool = False
        self._result_error: str = ""
        self._cancelled: bool = False
        self._pause_requested: bool = False
        # AUD-12012: cancel that arrives after run_job() accepted the job but
        # before the exporter exists — mirrored on pause's pending flag so the
        # request is applied instead of silently dropped.
        self._cancel_requested: bool = False
        self._progress_started_at: Optional[float] = None
        self._progress_pause_started_at: Optional[float] = None
        self._progress_paused_total: float = 0.0

    # ── configuration ──

    def set_context_provider(self, provider: JobContextProvider) -> None:
        """Swap the context provider (e.g. when the active project changes)."""
        self._context_provider = provider

    # ── ExportRunner protocol ──
    #
    # Signature: (ExportJob, ProgressCallback) -> bool.
    # The queue passes its own ExportJob; we recover the typed snapshot
    # from its config, resolve context, run the worker, and block until it
    # finishes.

    def run_job(self, legacy_job: ExportJob, progress_cb: ProgressCallback) -> bool:
        """Execute a single job (called by the ExportQueue).

        Returns ``True`` on success, ``False`` on cancellation.
        Raises ``RuntimeError`` on failure with a human-readable message —
        the queue's scheduler catches the exception and stores
        ``str(exception)`` on the job's ``error`` field, so raising is the
        only way to surface a specific error message (returning False makes
        the queue use a generic "export runner returned failure").
        """
        video_job = VideoExportJob.from_legacy_job(legacy_job)
        self._current_job_id = video_job.job_id
        self._current_exporter = None
        self._cancelled = False
        self._result_success = False
        self._result_error = ""
        # Clear any stale pending cancel from a previous aborted job; a
        # cancel that arrives *during* this job's exporter-construction
        # window is (re-)recorded by request_cancel below and applied
        # as soon as the exporter exists.
        self._cancel_requested = False

        # 1) Resolve runtime context (project + sequence).
        context = self._safe_resolve_context(video_job)
        if not context.is_resolvable:
            # Raise so the queue records the reason on the job's error field.
            raise RuntimeError(context.reason or "Cannot resolve export context")

        # 2) Build the real export config.
        try:
            export_config = video_job.to_timeline_export_config()
        except ImportError as e:
            raise RuntimeError(
                f"The edit feature is unavailable; cannot build the export config: {e}"
            ) from e
        except (ValueError, KeyError) as e:
            raise RuntimeError(
                f"Failed to map the task to the exporter config: {e}"
            ) from e

        # 3) Construct the worker + thread (reuse the project's proven
        # VideoExporter which moves work off the main thread).
        # M21.1b+：字幕 sidecar 产出接线——SIDECAR 模式在编码开始前把
        # .srt 渲染到临时文件（失败即零发布：旧成片/旧 sidecar 原样，
        # 编码根本不启动）；成片发布成功后 sidecar 经发布状态机成组
        # 就位（同一成功身份），失败显式报错不静默。
        srt_temp: Optional[str] = None
        srt_final: Optional[str] = None
        subtitle_mode = getattr(
            getattr(video_job, "subtitle_config", None), "mode", "")
        if subtitle_mode == SubtitleMode.SIDECAR:
            base, _ext = os.path.splitext(str(video_job.output_path))
            srt_final = base + ".srt"
            srt_temp = srt_final + ".topos-tmp"
            from src.features.deliver.srt_exporter import export_srt
            if not export_srt(context.sequence, srt_temp):
                try:
                    os.remove(srt_temp)
                except OSError:
                    pass
                raise RuntimeError(
                    "字幕 sidecar 渲染失败（未发布任何文件，旧产物原样）")
        try:
            exporter, thread = self._create_worker(
                context.project, context.sequence, export_config
            )
        except Exception as e:  # noqa: BLE001 — surface any startup failure
            raise RuntimeError(f"Failed to start the video export worker: {e}") from e

        self._current_exporter = exporter
        if self._cancel_requested:
            # AUD-12012: the cancel raced ahead of the exporter; apply it now
            # so the worker stops at its first frame check instead of running
            # the full encode before being marked cancelled.
            self._cancel_requested = False
            try:
                exporter.cancel()
            except Exception:  # noqa: BLE001 — a cancel request must not kill a job
                logger.exception("Failed to apply pending cancel to exporter")
        if self._pause_requested:
            try:
                exporter.pause()
            except Exception:  # noqa: BLE001 — a pause request must not kill a job
                logger.exception("Failed to apply pending pause to exporter")
        self.job_started.emit(video_job.job_id)

        # 4) Wire callbacks and spin a nested event loop until finished.
        #    The event loop keeps the main thread responsive (progress
        #    signals are delivered through it) while making this call block
        #    synchronously from the queue's point of view.
        self._loop = QEventLoop()
        self._progress_started_at = time.perf_counter()
        self._progress_pause_started_at = None
        self._progress_paused_total = 0.0

        def _on_finished(output_path: str) -> None:
            self._result_success = True
            self._result_error = ""
            self._quit_loop()

        def _on_error(error_msg: str) -> None:
            # "EXPORT_CANCELLED" is the worker's cancellation sentinel.
            if error_msg == "EXPORT_CANCELLED":
                self._cancelled = True
                self._result_error = ""
            else:
                self._result_error = error_msg or "Unknown export error"
            self._result_success = False
            self._quit_loop()

        def _on_progress(current: int, total: int, percent: float) -> None:
            elapsed = max(
                time.perf_counter() - (self._progress_started_at or time.perf_counter())
                - self._progress_paused_total,
                1e-6,
            )
            # The queue owns the legacy job object, so keep the measured rate
            # there for both persistence/debugging and the queue card UI.
            legacy_job.export_fps = max(0.0, float(current) / elapsed)
            try:
                # percent is 0..100 from the worker; the queue wants 0..1.
                progress_cb(max(0.0, min(1.0, percent / 100.0)))
            except Exception:  # noqa: BLE001 — progress must never kill a job
                logger.exception("progress callback raised; ignoring")

        # Actually kick the worker off (non-blocking; the event loop waits).
        # The callbacks are passed to export() which stores them itself —
        # we do NOT assign private attributes on the exporter (that would
        # be overwritten by export() and have no effect).
        try:
            exporter.export(
                project=context.project,
                sequence=context.sequence,
                config=export_config,
                on_finished=_on_finished,
                on_error=_on_error,
                on_progress=_on_progress,
                # U02：嵌套子序列解析与 preflight 同源；缺 map 时 worker 内
                # create_timeline_exporter 拿不到 resolver。
                timeline_sequences=getattr(context, "timeline_sequences", None),
            )
            self._loop.exec()
        except Exception as e:  # noqa: BLE001
            _discard_sidecar_temp(srt_temp)
            raise RuntimeError(f"The video export worker crashed: {e}") from e
        finally:
            # Always tear the worker down so we don't leak threads across
            # jobs in a long-running queue.
            try:
                exporter.cleanup()
            except Exception:  # noqa: BLE001
                logger.exception("Worker cleanup raised; continuing")
            finally:
                # AUD-12010: the exporter is parented to the runner; without
                # an explicit deleteLater it (plus its worker, QThread and
                # captured callbacks) accumulates on the runner for the whole
                # session — one object graph per queued job.
                if thread is not None and not thread.isFinished():
                    logger.warning(
                        "Export worker thread still running after cleanup (job %s)",
                        video_job.job_id,
                    )
                exporter.deleteLater()
            self._current_exporter = None
            self._current_job_id = None
            self._loop = None

        if self._cancelled:
            # Report cancellation as failure-but-not-error so the queue
            # marks the job CANCELLED rather than FAILED.
            _discard_sidecar_temp(srt_temp)
            return False
        if not self._result_success and not self._result_error:
            # AUD-12011: the nested loop exited without any worker callback —
            # not success, not error, not cancellation. That means the loop
            # was interrupted externally (typically application shutdown
            # interrupting all event loops). Fail this job but tell the owner
            # to stop scheduling instead of burning a fresh worker + thread
            # for every remaining job on the way down.
            _discard_sidecar_temp(srt_temp)
            self.aborted.emit(video_job.job_id)
            return False
        if not self._result_success and self._result_error:
            # Raise so the queue stores the real error message on the job
            # instead of the generic "export runner returned failure".
            _discard_sidecar_temp(srt_temp)
            raise RuntimeError(self._result_error)

        # N12 · 发布前质量门（按声明目标激活）：质量失败 ≠ DONE——job
        # 带 QC 原因失败，queue/manifest/UI 可定位；仅当 job 声明了质量
        # 目标（expected_lufs 等）才启用（未声明 ≠ 通过，门由交付声明
        # 显式触发，不冒充全量 QC）。
        qc_error = self._run_quality_gate(video_job)
        if qc_error:
            _discard_sidecar_temp(srt_temp)
            raise RuntimeError(qc_error)

        # M21.1b+：成片已就位 → sidecar 经发布状态机成组发布（旧
        # sidecar 备份/失败回滚；任何失败显式报错——成片与字幕保持
        # 同一成功身份，不静默留半套）。
        if srt_temp and srt_final:
            from src.shared.export.publish_transaction import (
                PublishFile,
                cleanup_group_backups,
                publish_file_group,
            )
            group = [PublishFile(temp_path=srt_temp, final_path=srt_final,
                                 kind="sidecar")]
            publish_result = publish_file_group(group)
            if not publish_result.ok:
                raise RuntimeError(
                    f"成片已发布，但字幕 sidecar 发布失败"
                    f"（{srt_final}）：{publish_result.error}")
            cleanup_group_backups(group)
            logger.info(
                "Subtitle sidecar published with media: %s", srt_final)
        return self._result_success

    # ── cancellation ──

    def _run_quality_gate(self, video_job) -> Optional[str]:
        """N12 · 声明目标的发布前质量门（失败返回错误串，None=通过/未启用）。

        读 job config 的 ``expected_lufs``（float，声明 LUFS 目标）；
        用交付质量探测器实测成片，超出容差（±1 LU，EBU R128 常规
        容差）或探测失败 → 返回错误（探测失败≠通过，fail-closed）。
        未声明目标 → None（门未启用）。
        """
        config = getattr(video_job, "config", None) or {}
        try:
            expected_lufs = float(config.get("expected_lufs"))
        except (TypeError, ValueError):
            return None  # 未声明质量目标——门不启用
        output_path = str(getattr(video_job, "output_path", "") or "")
        if not output_path or not os.path.exists(output_path):
            return f"QC 门失败：成片不存在（{output_path}）"
        try:
            from src.features.edit.ai_editing.delivery_quality_probe import (
                measure_delivery_quality,
            )
            probe = measure_delivery_quality(
                output_path, expected_lufs=expected_lufs)
        except Exception as exc:  # noqa: BLE001 — 探测异常 ≠ 通过
            return f"QC 门失败：探测异常（{exc}）"
        integrated = getattr(probe, "integrated_lufs", None)
        if integrated is None:
            return "QC 门失败：响度探测无结果（音频流缺失？）"
        if abs(integrated - expected_lufs) > 1.0:
            return (f"QC 门失败：综合响度 {integrated:.2f} LUFS 偏离"
                    f"声明目标 {expected_lufs:.2f}（容差 ±1 LU）")
        return None

    def request_cancel(self) -> None:
        """Cooperatively cancel the currently-running job (if any).

        Safe to call from any thread — it only forwards to the worker's
        ``cancel_export`` flag, which is checked between frames. If the job
        was accepted but the exporter is not constructed yet (the synchronous
        window at the top of :meth:`run_job`), the request is remembered and
        applied the moment the exporter exists (mirroring pause).
        """
        job_id = self._current_job_id
        exporter = self._current_exporter
        if exporter is not None:
            try:
                exporter.cancel()
            except Exception as e:  # noqa: BLE001
                logger.warning("cancel() failed on current exporter: %s", e)
        elif job_id is not None:
            # AUD-12012: exporter not ready yet — don't drop the request.
            self._cancel_requested = True
        if job_id is not None:
            self.cancel_requested.emit(job_id)

    def request_pause(self) -> None:
        """Pause the active video export after its current frame."""
        self._pause_requested = True
        if self._progress_started_at is not None and self._progress_pause_started_at is None:
            self._progress_pause_started_at = time.perf_counter()
        exporter = self._current_exporter
        if exporter is not None:
            try:
                exporter.pause()
            except Exception as e:  # noqa: BLE001
                logger.warning("pause() failed on current exporter: %s", e)

    def request_resume(self) -> None:
        """Resume the active video export, if it is paused."""
        self._pause_requested = False
        if self._progress_pause_started_at is not None:
            self._progress_paused_total += max(
                0.0, time.perf_counter() - self._progress_pause_started_at
            )
            self._progress_pause_started_at = None
        exporter = self._current_exporter
        if exporter is not None:
            try:
                exporter.resume()
            except Exception as e:  # noqa: BLE001
                logger.warning("resume() failed on current exporter: %s", e)

    # ── internals ──

    def _safe_resolve_context(self, job: VideoExportJob) -> JobContext:
        try:
            return self._context_provider(job)
        except Exception as e:  # noqa: BLE001 — context resolution must not kill the queue
            logger.exception("JobContextProvider raised; treating as unresolved")
            return JobContext(reason=f"Context resolution failed: {e}")

    def _create_worker(
        self,
        project: Any,
        sequence: Any,
        export_config: Any,
    ):
        """Construct the per-job VideoExporter (worker + thread).

        A fresh exporter is created per job. This keeps cancellation and
        cleanup scoped to a single job and avoids carrying state between
        unrelated exports. The real encoding still happens off the main
        thread via the worker's own QThread.
        """
        from src.shared.export.video_export_worker import VideoExporter

        exporter = VideoExporter(parent=self)
        thread = getattr(exporter, "_worker_thread", None)
        return exporter, thread

    def _quit_loop(self) -> None:
        loop = self._loop
        if loop is not None and loop.isRunning():
            # Defer the quit to avoid re-entering Qt during signal dispatch.
            from PyQt6.QtCore import QTimer

            QTimer.singleShot(0, loop.quit)
